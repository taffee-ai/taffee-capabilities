"""Conformance runner (S-001 LANGUAGES.md v2 §6, §7; M5).

Runs one implementation on a case and checks the result with a small, fixed set of
expectation operators. Runners are looked up by name in RUNNERS, so a new language
that fits an existing runner needs only a data entry in policy/languages.yaml.

Safety rules, all enforced here:
- the argv is a list, executed without a shell;
- the entry file must resolve inside its implementation folder, and links are refused;
- the run has a timeout, and stdout is bounded;
- the environment is an allow-list, not inherited.
"""

import hashlib
import json
import os
import subprocess
import tempfile
import threading
from pathlib import Path

from .calltime import CallError, validate_output
from .canon import canonical_bytes

OPERATORS = ("ok", "contains", "not_contains", "json_key", "exit_code")
TIMEOUT_SECONDS = 5
OUTPUT_LIMIT_BYTES = 64 * 1024
ENV_ALLOW_LIST = ("PATH",)
STDIN_LIMIT_BYTES = 64 * 1024


class ConformanceError(Exception):
    """A run could not be made safely: bad entry, missing toolchain, or a bad expectation."""


def _python(entry: Path) -> list[str]:
    return ["python3", str(entry)]


def _bash(entry: Path) -> list[str]:
    return ["bash", str(entry)]


def _node_strip_types(entry: Path) -> list[str]:
    return ["node", "--experimental-strip-types", str(entry)]


RUNNERS = {"python": _python, "bash": _bash, "node-strip-types": _node_strip_types}


def check_toolchain(argv: list[str]) -> None:
    """Run the language's declared toolchain check. Missing or failing means unavailable."""
    try:
        completed = subprocess.run(argv, capture_output=True, timeout=TIMEOUT_SECONDS, check=False)
    except FileNotFoundError as exc:
        raise ConformanceError(f"toolchain unavailable: {argv[0]} is not installed") from exc
    except subprocess.TimeoutExpired as exc:
        raise ConformanceError(f"toolchain unavailable: {' '.join(argv)} did not answer") from exc
    if completed.returncode != 0:
        raise ConformanceError(f"toolchain unavailable: {' '.join(argv)} exited {completed.returncode}")


def _read_bounded(stream, limit: int, label: str, proc: subprocess.Popen, problems: dict, out: dict) -> None:
    """Read a pipe in chunks. Past the limit, kill the child and record why."""
    total = 0
    chunks: list[bytes] = []
    while True:
        chunk = stream.read(4096)
        if not chunk:
            break
        total += len(chunk)
        if total > limit:
            problems[label] = f"{label} exceeded {limit} bytes"
            proc.kill()
            break
        chunks.append(chunk)
    out[label] = b"".join(chunks)


def run_implementation(folder: Path, entry_name: str, runner: str, stdin_text: str,
                       toolchain: list[str]) -> subprocess.CompletedProcess:
    """Run one implementation with a structured argv inside its folder, with bounded output."""
    if runner not in RUNNERS:
        raise ConformanceError(f"no runner named {runner!r}")
    entry = (folder / entry_name).resolve()
    if folder.resolve() not in entry.parents or (folder / entry_name).is_symlink():
        raise ConformanceError(f"entry {entry_name} escapes its implementation folder or is a link")
    if not toolchain:
        raise ConformanceError("a declared toolchain is required")
    check_toolchain(toolchain)
    payload = stdin_text.encode("utf-8")
    if len(payload) > STDIN_LIMIT_BYTES:
        raise ConformanceError(f"input exceeds {STDIN_LIMIT_BYTES} bytes")
    env = {key: os.environ[key] for key in ENV_ALLOW_LIST if key in os.environ}
    try:
        proc = subprocess.Popen(
            RUNNERS[runner](entry),
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            cwd=str(folder),
            env=env,
        )
    except FileNotFoundError as exc:
        raise ConformanceError(f"toolchain for runner {runner!r} is not available: {exc}") from exc

    problems: dict[str, str] = {}
    out: dict[str, bytes] = {}
    readers = [
        threading.Thread(target=_read_bounded, args=(proc.stdout, OUTPUT_LIMIT_BYTES, "stdout", proc, problems, out)),
        threading.Thread(target=_read_bounded, args=(proc.stderr, OUTPUT_LIMIT_BYTES, "stderr", proc, problems, out)),
    ]
    for reader in readers:
        reader.start()
    def _feed() -> None:
        try:
            proc.stdin.write(payload)
        except (BrokenPipeError, OSError):
            pass
        finally:
            try:
                proc.stdin.close()
            except (BrokenPipeError, OSError):
                pass

    writer = threading.Thread(target=_feed)
    writer.start()
    try:
        returncode = proc.wait(timeout=TIMEOUT_SECONDS)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait()
        raise ConformanceError(f"run exceeded {TIMEOUT_SECONDS}s")
    finally:
        writer.join(timeout=TIMEOUT_SECONDS)
        for reader in readers:
            reader.join()
        for pipe in (proc.stdout, proc.stderr):
            pipe.close()
    if problems:
        raise ConformanceError("; ".join(sorted(problems.values())))
    return subprocess.CompletedProcess(args=[], returncode=returncode,
                                        stdout=out.get("stdout", b""), stderr=out.get("stderr", b""))


def _json_has_key(text: str, dotted: str) -> bool:
    try:
        value = json.loads(text)
    except ValueError:
        return False
    for part in dotted.split("."):
        if not isinstance(value, dict) or part not in value:
            return False
        value = value[part]
    return True


def check_expectations(completed: subprocess.CompletedProcess, expect: dict) -> list[str]:
    """Return the expectations that failed. An unknown operator is an error, not a failure."""
    stdout = completed.stdout.decode("utf-8", "replace")
    failures: list[str] = []
    for operator, wanted in expect.items():
        if operator not in OPERATORS:
            raise ConformanceError(f"unknown expectation operator {operator!r}")
        if operator == "ok":
            try:
                ok = json.loads(stdout).get("ok")
            except (ValueError, AttributeError):
                ok = None
            if ok is not wanted:
                failures.append(f"ok: wanted {wanted}, got {ok}")
        elif operator == "contains" and wanted not in stdout:
            failures.append(f"contains {wanted!r} not found")
        elif operator == "not_contains" and wanted in stdout:
            failures.append(f"not_contains {wanted!r} was found")
        elif operator == "json_key" and not _json_has_key(stdout, wanted):
            failures.append(f"json_key {wanted!r} missing")
        elif operator == "exit_code" and completed.returncode != wanted:
            failures.append(f"exit_code: wanted {wanted}, got {completed.returncode}")
    return failures


# ---------------------------------------------------------------------------------------------
# Cases (T-001). A case file is a JSON list. A case without "files" feeds its "input" to the
# implementation as stdin, unchanged (the S-001 fixture format). A case with "files" describes a
# throw-away repository: the runner builds it in a temporary directory and feeds the tool contract
#   {"root": "<that directory>", "input": <input>}
# so a tool is tested against a repository it did not see before and cannot touch anything else.
# ---------------------------------------------------------------------------------------------

CASE_KEYS = {"id", "input", "expect", "files", "links", "outside", "git"}
GIT_ENV = {
    "GIT_AUTHOR_NAME": "Conformance", "GIT_AUTHOR_EMAIL": "conformance@example.invalid",
    "GIT_COMMITTER_NAME": "Conformance", "GIT_COMMITTER_EMAIL": "conformance@example.invalid",
    "GIT_AUTHOR_DATE": "2026-01-01T00:00:00Z", "GIT_COMMITTER_DATE": "2026-01-01T00:00:00Z",
}


def _safe_relative(name: object, what: str) -> Path:
    if not isinstance(name, str) or not name:
        raise ConformanceError(f"{what}: path must be a non-empty string")
    path = Path(name)
    if path.is_absolute() or ".." in path.parts:
        raise ConformanceError(f"{what}: {name!r} must be relative and stay inside the repository")
    if not path.parts:
        raise ConformanceError(f"{what}: {name!r} must name a path inside the repository, not the repository itself")
    return path


def load_cases(path: Path) -> list[dict]:
    """Read and check a case file. Any problem is a ConformanceError."""
    try:
        cases = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise ConformanceError(f"{path}: cannot read cases: {exc}") from exc
    if not isinstance(cases, list) or not cases:
        raise ConformanceError(f"{path}: cases must be a non-empty list")
    seen: set[str] = set()
    for case in cases:
        if not isinstance(case, dict):
            raise ConformanceError(f"{path}: every case must be an object")
        extra = set(case) - CASE_KEYS
        if extra:
            raise ConformanceError(f"{path}: unknown case keys {sorted(extra)}")
        case_id = case.get("id")
        if not isinstance(case_id, str) or not case_id or case_id in seen:
            raise ConformanceError(f"{path}: every case needs a unique string id")
        seen.add(case_id)
        if "input" not in case or not isinstance(case.get("expect"), dict) or not case["expect"]:
            raise ConformanceError(f"{path}: case {case_id} needs 'input' and a non-empty 'expect'")
        for operator in case["expect"]:
            if operator not in OPERATORS:
                raise ConformanceError(f"{path}: case {case_id} uses unknown operator {operator!r}")
        if ("links" in case or "outside" in case or "git" in case) and "files" not in case:
            raise ConformanceError(f"{path}: case {case_id} describes a repository, so it needs 'files'")
        for key in ("files", "outside"):
            if key in case and not isinstance(case[key], dict):
                raise ConformanceError(f"{path}: case {case_id}: '{key}' must be an object")
    return cases


def cases_digest(cases: list[dict]) -> str:
    """The digest that ties a label to the exact cases it was earned on."""
    return "sha256:" + hashlib.sha256(canonical_bytes(cases)).hexdigest()


def _git(root: Path, home: Path, *args: str) -> None:
    env = {"PATH": os.environ.get("PATH", ""), "HOME": str(home), **GIT_ENV}
    try:
        subprocess.run(["git", *args], cwd=root, env=env, check=True, capture_output=True, timeout=TIMEOUT_SECONDS)
    except FileNotFoundError as exc:
        raise ConformanceError("git is not installed, so a git fixture cannot be built") from exc
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired) as exc:
        raise ConformanceError(f"git fixture failed: git {' '.join(args)}") from exc


def _place(base: Path, relative: Path, what: str) -> Path:
    """Create the parents of ``base/relative`` one component at a time and return the final path.

    Refuses a path that passes through a link or a file, a final component that already exists, and a
    result that resolves outside ``base``. Creating parents with ``mkdir(parents=True)`` would follow a
    link made earlier in the same case, so a later entry could write outside the temporary repository.
    """
    if not relative.parts:
        raise ConformanceError(f"{what}: a path must have at least one component")
    walk = base
    for part in relative.parts[:-1]:
        walk = walk / part
        if walk.is_symlink():
            raise ConformanceError(f"{what}: {relative} passes through a link")
        if not walk.exists():
            walk.mkdir()
        elif not walk.is_dir():
            raise ConformanceError(f"{what}: {relative} passes through a file")
    final = walk / relative.parts[-1]
    if final.exists() or final.is_symlink():
        raise ConformanceError(f"{what}: {relative} already exists")
    try:
        final.parent.resolve().relative_to(base.resolve())
    except ValueError:
        raise ConformanceError(f"{what}: {relative} resolves outside the repository") from None
    return final


def build_repository(case: dict, base: Path) -> Path:
    """Materialise a case's repository under ``base`` and return its root."""
    root = base / "repo"
    root.mkdir()
    outside = base / "outside"
    outside.mkdir()
    for name, text in (case.get("outside") or {}).items():
        _place(outside, _safe_relative(name, "outside"), "outside").write_text(str(text), encoding="utf-8")
    for name, text in case["files"].items():
        _place(root, _safe_relative(name, "files"), "files").write_text(str(text), encoding="utf-8")
    for name, target in (case.get("links") or {}).items():
        _place(root, _safe_relative(name, "links"), "links").symlink_to(str(target))
    git = case.get("git")
    if git is not None:
        _git(root, base, "init", "-q", "-b", "main")
        for number in range(int(git.get("commits", 0))):
            (root / "history.txt").write_text(f"change {number}\n", encoding="utf-8")
            _git(root, base, "add", "history.txt")
            _git(root, base, "commit", "-q", "-m", f"commit number {number}")
    return root


def _postcondition_failures(case_id: str, done: subprocess.CompletedProcess, spec: dict | None) -> list[str]:
    """Apply the tool's declared postconditions to a case that returned ok (S-009, CAP-CALL-9)."""
    if not spec or not spec.get("postconditions"):
        return []
    try:
        result = json.loads(done.stdout.decode("utf-8", "replace"))
    except ValueError:
        return []  # the case's own expectations report an unreadable result
    if not isinstance(result, dict) or result.get("ok") is not True:
        return []
    try:
        validate_output(spec, result)
    except CallError as exc:
        return [f"{case_id}: {problem.field}: {problem.message}" for problem in exc.problems]
    return []


def run_cases(folder: Path, entry_name: str, runner: str, toolchain: list[str], cases: list[dict],
              spec: dict | None = None) -> list[str]:
    """Run every case on one implementation. Returns the failures as readable strings.

    When ``spec`` is given, its postconditions are applied to every case that returns ok (S-009).

    A missing toolchain is a ConformanceError (it says nothing about the implementation).
    A case that cannot be run safely, or that fails an expectation, is a failure.
    """
    check_toolchain(toolchain)
    failures: list[str] = []
    for case in cases:
        try:
            if "files" in case:
                with tempfile.TemporaryDirectory() as tmp:
                    root = build_repository(case, Path(tmp).resolve())
                    stdin = json.dumps({"root": str(root), "input": case["input"]})
                    done = run_implementation(folder, entry_name, runner, stdin, toolchain)
            else:
                done = run_implementation(folder, entry_name, runner, json.dumps(case["input"]), toolchain)
            failures.extend(f"{case['id']}: {problem}" for problem in check_expectations(done, case["expect"]))
            failures.extend(_postcondition_failures(case["id"], done, spec))
        except ConformanceError as exc:
            failures.append(f"{case['id']}: {exc}")
    return failures
