"""Evidence records for the Tested label (T-001 SPEC §5.4).

A record says: this tool identity, with these conformance cases, passed this test suite on this
host with nothing failed and nothing skipped. It lives outside the implementation folder, so
writing it does not change the tool's identity:

    evidence/tools/<name>/<version>/summary.json
    evidence/tools/<name>/<version>/output.txt      the test log, trimmed

`fingerprint` is the SHA-256 of what was run and where: the identity, the cases digest, the
suite file's digest, the policy version and the environment (Python, OS, architecture, and the
versions of git, grep and find, because the tools' output depends on them). Results are not part
of it. Two records with the same fingerprint are the same claim, and re-running on the same host
from a fresh copy reproduces it.

`evidence_digest` binds what happened: the fingerprint, the suite, the result counts and the
digest of `output.txt`. It differs on every run (the log carries timings), so it is not a
reproducibility check; it makes the counts and the log tamper-evident, because the check recomputes
it and compares the log on disk with `log_digest`.

The label check does not re-run the tests. It checks that the record is consistent, belongs to the
tool as it is now, and was earned on the suite as it is now: the suite file named in the record must
exist and match its digest. A changed tool has a new identity and a changed suite has a new digest,
so in either case the old record no longer applies and the tool falls back to Validated. The record
is not a signature: whoever can edit the repository can write a consistent record, which is why
reviews read the record and its log.
"""

import argparse
import hashlib
import json
import platform
import subprocess
import sys
from pathlib import Path

from .canon import canonical_bytes

SCHEMA = "capability-evidence/1"
EVIDENCE_DIR = Path("evidence") / "tools"
SUITE = "tests/test_tools_t001.py"
# The suites a record may name. A record that names any other file, even one that exists and matches its
# own digest, cannot earn Tested: otherwise any file in the tree could stand in for the approved suite.
APPROVED_SUITES = (SUITE,)
OUTPUT_LIMIT_BYTES = 200 * 1024
SUMMARY_KEYS = {"schema", "name", "version", "identity", "policy_version", "cases_digest",
                "suite", "environment", "tests", "fingerprint", "log_digest", "evidence_digest"}
TEST_KEYS = {"run", "passed", "failed", "errors", "skipped", "skipped_reasons"}


def _first_line(argv: list[str]) -> str | None:
    try:
        done = subprocess.run(argv, capture_output=True, text=True, timeout=10, check=False)
    except (OSError, subprocess.TimeoutExpired):
        return None
    lines = (done.stdout or done.stderr).splitlines()
    return lines[0].strip() if lines else None


def environment() -> dict:
    """The facts about this host that can change what the tools print."""
    return {
        "python": platform.python_version(),
        "os": platform.system(),
        "arch": platform.machine(),
        "git": _first_line(["git", "--version"]),
        "grep": _first_line(["grep", "--version"]),
        "find": _first_line(["find", "--version"]),
    }


def file_digest(path: Path) -> str:
    return "sha256:" + hashlib.sha256(Path(path).read_bytes()).hexdigest()


def fingerprint(identity: str, cases_digest: str, suite_digest: str, policy_version: int, env: dict) -> str:
    body = {"identity": identity, "cases_digest": cases_digest, "suite_digest": suite_digest,
            "policy_version": policy_version, "environment": env}
    return "sha256:" + hashlib.sha256(canonical_bytes(body)).hexdigest()


_CHILD = """
import io, json, sys, unittest
root, suite = sys.argv[1], sys.argv[2]
from pathlib import Path
suite_path = Path(root) / suite
sys.path.insert(0, root)
tests = unittest.TestLoader().discover(str(suite_path.parent), pattern=suite_path.name,
                                       top_level_dir=str(suite_path.parent))
stream = io.StringIO()
result = unittest.TextTestRunner(stream=stream, verbosity=2).run(tests)
failed = len(result.failures) + len(result.unexpectedSuccesses)
errors, skipped = len(result.errors), len(result.skipped)
counts = {"run": result.testsRun, "passed": result.testsRun - failed - errors - skipped,
          "failed": failed, "errors": errors, "skipped": skipped,
          "skipped_reasons": sorted({reason for _, reason in result.skipped})}
sys.stdout.write(json.dumps({"counts": counts, "log": stream.getvalue()}))
"""
SUITE_TIMEOUT_SECONDS = 1800


def evidence_digest(fingerprint_value: str, suite: dict, counts: dict, log_digest: str) -> str:
    """Binds the fingerprint, the suite, the result counts and the log into one digest."""
    body = {"fingerprint": fingerprint_value, "suite": suite, "tests": counts, "log_digest": log_digest}
    return "sha256:" + hashlib.sha256(canonical_bytes(body)).hexdigest()


def run_suite(root: Path, suite: str = SUITE) -> tuple[dict, str]:
    """Run one test file in a fresh child process. Returns (counts, log).

    A child process keeps the run clean: nothing is shared with the caller's imports, and a hang
    or crash cannot take the caller down. A child that fails to report counts is an error.
    """
    import os

    root = Path(root).resolve()
    env = {"PATH": os.environ.get("PATH", ""), "PYTHONDONTWRITEBYTECODE": "1", "PYTHONPATH": str(root)}
    try:
        done = subprocess.run([sys.executable, "-c", _CHILD, str(root), suite], cwd=root, env=env,
                              capture_output=True, text=True, timeout=SUITE_TIMEOUT_SECONDS, check=False)
        report = json.loads(done.stdout)
        counts, log = report["counts"], report["log"]
    except (subprocess.TimeoutExpired, ValueError, KeyError) as exc:
        raise RuntimeError(f"the test run did not report counts: {type(exc).__name__}") from exc
    return counts, log.encode("utf-8", "replace")[:OUTPUT_LIMIT_BYTES].decode("utf-8", "replace")


def write_record(root: Path, name: str, version: str, identity: str, cases_digest: str,
                 policy_version: int, counts: dict, log: str, suite: str = SUITE) -> Path:
    env = environment()
    suite_digest = file_digest(Path(root) / suite)
    folder = Path(root) / EVIDENCE_DIR / name / version
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "output.txt").write_text(log, encoding="utf-8")
    log_digest = file_digest(folder / "output.txt")
    claim = fingerprint(identity, cases_digest, suite_digest, policy_version, env)
    summary = {
        "schema": SCHEMA,
        "name": name,
        "version": version,
        "identity": identity,
        "policy_version": policy_version,
        "cases_digest": cases_digest,
        "suite": {"path": suite, "digest": suite_digest},
        "environment": env,
        "tests": counts,
        "fingerprint": claim,
        "log_digest": log_digest,
        "evidence_digest": evidence_digest(claim, {"path": suite, "digest": suite_digest}, counts, log_digest),
    }
    (folder / "summary.json").write_bytes(canonical_bytes(summary))
    return folder


def _link_free_file(root: Path, relative: Path) -> str | None:
    """Why `root/relative` is not a plain file inside root, or None. No component may be a link."""
    root = Path(root)
    walk = root
    for part in relative.parts:
        walk = walk / part
        if walk.is_symlink():
            return "the test suite path passes through a link"
    if not walk.is_file():
        return "the test suite named by the evidence is missing, so the record cannot be tied to the current tests"
    try:
        walk.resolve().relative_to(root.resolve())
    except ValueError:
        return "the test suite resolves outside the root"
    return None


def check_record(root: Path, name: str, version: str, identity: str, cases_digest: str,
                 policy_version: int, suites: tuple | None = None) -> str | None:
    """Return why the record does not support Tested, or None when it does.

    ``suites`` is the allow-list of suite paths a record may name (default: APPROVED_SUITES).
    """
    path = Path(root) / EVIDENCE_DIR / name / version / "summary.json"
    if not path.is_file():
        return "no evidence record"
    try:
        summary = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return "evidence record is not valid JSON"
    if not isinstance(summary, dict) or set(summary) != SUMMARY_KEYS or summary["schema"] != SCHEMA:
        return "evidence record has the wrong shape"
    tests = summary["tests"]
    if not isinstance(tests, dict) or set(tests) != TEST_KEYS or not all(
            isinstance(tests[key], int) and not isinstance(tests[key], bool) and tests[key] >= 0
            for key in TEST_KEYS - {"skipped_reasons"}):
        return "evidence record has malformed test counts"
    suite, env = summary["suite"], summary["environment"]
    if not isinstance(suite, dict) or set(suite) != {"path", "digest"} or not isinstance(env, dict):
        return "evidence record has a malformed suite or environment"
    for field, expected in (("name", name), ("version", version), ("identity", identity),
                            ("cases_digest", cases_digest), ("policy_version", policy_version)):
        if summary[field] != expected:
            return f"evidence is for a different {field}"
    if tests["run"] != tests["passed"] + tests["failed"] + tests["errors"] + tests["skipped"]:
        return "evidence test counts do not add up"
    if tests["passed"] < 1 or tests["failed"] or tests["errors"] or tests["skipped"]:
        return "evidence shows failed, errored or skipped tests"
    if summary["fingerprint"] != fingerprint(identity, cases_digest, suite["digest"], policy_version, env):
        return "evidence fingerprint does not recompute"
    if not isinstance(summary["log_digest"], str) or not isinstance(summary["evidence_digest"], str):
        return "evidence record has a malformed log or evidence digest"
    if summary["evidence_digest"] != evidence_digest(summary["fingerprint"], suite, tests, summary["log_digest"]):
        return "evidence digest does not recompute: the counts, suite or log digest were edited"
    log_file = path.parent / "output.txt"
    if not log_file.is_file():
        return "evidence log (output.txt) is missing"
    if file_digest(log_file) != summary["log_digest"]:
        return "evidence log does not match its recorded digest"
    suite_path = Path(suite["path"]) if isinstance(suite["path"], str) else None
    if suite_path is None or suite_path.is_absolute() or ".." in suite_path.parts or not suite["path"]:
        return "evidence names a suite path that is not a safe relative path"
    if suite["path"] not in (suites if suites is not None else APPROVED_SUITES):
        return "evidence names a suite that is not on the approved list"
    problem = _link_free_file(root, suite_path)
    if problem:
        return problem
    suite_file = Path(root) / suite_path
    if file_digest(suite_file) != suite["digest"]:
        return "evidence is for a different suite: the tests changed since the record was written"
    return None


def _tools(root: Path):
    """(name, version, identity, cases_digest) for each tool with an implementation and cases."""
    from .conformance import cases_digest, load_cases
    from .implementations import check_implementations, tool_identity
    from .languages import load_languages
    from .manifest import check_manifest_file

    languages = load_languages(root)
    for path in sorted((Path(root) / "catalog" / "tool").glob("*/manifest.yaml")):
        manifest = check_manifest_file(path)
        cases_path = Path(root) / "library" / manifest["name"] / "conformance.json"
        implementations, errors = check_implementations(manifest, Path(root), languages)
        if errors or not implementations or not cases_path.is_file():
            continue
        yield (manifest["name"], manifest["version"], tool_identity(manifest, implementations),
               cases_digest(load_cases(cases_path)))


def main(argv=None) -> int:
    from .labels import POLICY_VERSION

    parser = argparse.ArgumentParser(prog="capability-evidence")
    parser.add_argument("command", choices=("write", "check"))
    parser.add_argument("root", help="repository root")
    parser.add_argument("--tool", action="append", help="limit write and check to this tool (repeatable)")
    args = parser.parse_args(argv)
    root = Path(args.root).resolve()
    eligible = list(_tools(root))
    unknown = sorted(set(args.tool or []) - {tool[0] for tool in eligible})
    if unknown:
        # A misspelled name must not select nothing and exit 0: a regeneration would silently do nothing.
        print(f"error: no evidence-eligible tool named {', '.join(map(repr, unknown))}. A tool needs an implementation and "
              f"library/<name>/conformance.json. Eligible: {', '.join(sorted(t[0] for t in eligible)) or 'none'}.",
              file=sys.stderr)
        return 2
    tools = [tool for tool in eligible if not args.tool or tool[0] in args.tool]
    if args.command == "write":
        sys.path.insert(0, str(root))
        counts, log = run_suite(root)
        for name, version, identity, digest in tools:
            folder = write_record(root, name, version, identity, digest, POLICY_VERSION, counts, log)
            print(f"wrote {folder.relative_to(root)}")
        print(f"tests: {counts}")
        return 0 if not (counts["failed"] or counts["errors"] or counts["skipped"]) else 1
    status = 0
    for name, version, identity, digest in tools:
        problem = check_record(root, name, version, identity, digest, POLICY_VERSION)
        print(f"{name}@{version}: {problem or 'evidence holds'}")
        status = status or (1 if problem else 0)
    return status


if __name__ == "__main__":
    sys.exit(main())
