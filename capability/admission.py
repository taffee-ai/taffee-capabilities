"""S-018: admission of a tool written outside Taffee (03_DESIGN §2.3, §3, §5; 02_DECISIONS R-D06 to R-D09).

received → validating → testing → awaiting_approval → admitted, or rejected. Every state change is
stored (REG-ADM-9). Submitted code runs only through `Sandbox.run`, under the policy the submitted
manifest declares (REG-ADM-4); a sandbox that cannot enforce it refuses, and the submission is
rejected. Problems are closed text naming the field or case; nothing the tool printed is copied in
(REG-ADM-11). Taffee does not write tools (taffee-core D-013): this module only checks them.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import json
import logging
import subprocess
import shutil
import tempfile
import threading
import time
import uuid
from pathlib import Path

from .canon import canonical_bytes
from .digest import manifest_digest
from .conformance import (
    RUNNERS, ConformanceError, _postcondition_failures, build_repository, cases_digest, check_expectations, load_cases,
)
from .execute import TOOL_ERRORS, WORK, _parse_result, policy_from_spec
from .implementations import sorted_manifest, tool_identity
from .library_repo import LIBRARY_OWNER
from .manifest import validate_manifest
from .rows import now_iso, row_hash, sign_row
from .sandbox import IMPLEMENTATION_TARGET, SandboxError, SandboxUnavailable
from .store.base import StoreConflict, StoreError
from .store.code import check_relative

LOG = logging.getLogger("capability.admission")
BUNDLE_SCHEMA = "capability-submission/1"
STATES = ("received", "validating", "building", "testing", "awaiting_approval", "admitted", "rejected")
OPEN_STATES = ("received", "validating", "building", "testing")
APPROVALS = ("manual", "auto")
AUTO_APPROVER = "policy:auto"

# The build step, per runner: run inside the sandbox, against the read-only implementation at /impl. It
# compiles every source file without running it, and answers in the tool contract's own result format.
# A language with no entry here has no separate build step; its cases are its only check.
_PY_BUILD = r"""
import json, os, sys
bad = []
for d, _, names in os.walk("/impl"):
    for n in sorted(names):
        if n.endswith(".py"):
            p = os.path.join(d, n)
            try:
                compile(open(p, "rb").read(), p, "exec")
            except SyntaxError as e:
                bad.append("%s:%s" % (os.path.relpath(p, "/impl"), e.lineno))
            except ValueError:
                bad.append(os.path.relpath(p, "/impl"))
print(json.dumps({"ok": not bad, "output": "compiled"} if not bad else {"ok": False, "error": "syntax: " + ", ".join(bad[:10])}))
"""
_BASH_BUILD = r"""
import json, os, subprocess
bad = []
for d, _, names in os.walk("/impl"):
    for n in sorted(names):
        if n.endswith(".sh"):
            p = os.path.join(d, n)
            if subprocess.run(["bash", "-n", p], capture_output=True).returncode != 0:
                bad.append(os.path.relpath(p, "/impl"))
print(json.dumps({"ok": not bad, "output": "checked"} if not bad else {"ok": False, "error": "syntax: " + ", ".join(bad[:10])}))
"""
BUILD_STEPS = {"python": ["python3", "-c", _PY_BUILD], "bash": ["python3", "-c", _BASH_BUILD]}
BUNDLE_KEYS = {"schema", "manifest", "files", "cases"}
MAX_PROBLEMS = 50


class AdmissionError(Exception):
    """A request admission cannot accept. `code` is one of the S-018 API codes."""

    def __init__(self, code: str, message: str):
        self.code = code
        super().__init__(f"{code}: {message}")


class Limits:
    """Deployment settings (03_DESIGN §7). The defaults are development defaults, not measured values."""

    def __init__(self, max_bundle_bytes=1024 * 1024, max_cases=100, max_files=200):
        self.max_bundle_bytes, self.max_cases, self.max_files = max_bundle_bytes, max_cases, max_files


def _problem(text: str) -> str:
    return text if len(text) <= 300 else text[:297] + "..."


class Admission:
    def __init__(self, store, private_key, sandbox, languages: dict, work_dir, *, limits: Limits | None = None,
                 approval: str = "manual", library=None, clock=time.time):
        """`work_dir` holds each case's temporary repository; with a container sandbox it must be a path the
        Docker host can mount (the same absolute path on both sides).

        `approval` is the approval policy (02_DECISIONS R-D09): "manual" waits for a named approver; "auto" admits
        a tool the moment every check passes, recorded as approved by "policy:auto"."""
        if approval not in APPROVALS:
            raise ValueError(f"approval must be one of {APPROVALS}")
        self.approval = approval
        self.library = library  # LibraryRepo: where admitted code becomes Taffee's (R-D14). Required by the service.
        self._store, self._key, self._sandbox = store, private_key, sandbox
        self._languages = languages
        self._work = Path(work_dir)
        self._work.mkdir(parents=True, exist_ok=True)
        self.limits = limits or Limits()
        self._clock = clock
        self._approve_lock = threading.Lock()
        self._submit_lock = threading.Lock()  # a retry racing its original finds it, never doubles it

    @property
    def store(self):
        return self._store

    # -- submit: shape checks only; nothing runs --
    @staticmethod
    def fingerprint(bundle: dict) -> str:
        """What identifies a submission for retries: the canonical form of the whole bundle."""
        return "sha256:" + hashlib.sha256(canonical_bytes(bundle)).hexdigest()

    def find_retry(self, raw, submitter: str) -> dict | None:
        """The existing submission this request repeats, or None. Cheap: nothing is stored or run."""
        try:
            bundle = self._parse(raw)
        except AdmissionError:
            return None
        found = self._store.find_submission(submitter, self.fingerprint(bundle))
        if found is not None:
            found["duplicate"] = True
        return found

    def submit(self, raw: bytes | dict, submitter: str) -> dict:
        if _reserved(submitter):
            raise AdmissionError("Forbidden", "that principal name is reserved by the registry")
        bundle = self._parse(raw)
        manifest, cases = bundle["manifest"], bundle["cases"]
        if not isinstance(manifest, dict) or manifest.get("kind") != "tool":
            raise AdmissionError("InvalidRequest", "manifest must be an object with kind: tool")
        for field in ("name", "version"):
            if not isinstance(manifest.get(field), str):
                raise AdmissionError("InvalidRequest", f"manifest.{field} must be text")
        if not isinstance(cases, list) or not cases:
            raise AdmissionError("InvalidRequest", "cases must be a non-empty list")
        if len(cases) > self.limits.max_cases:
            raise AdmissionError("TooLarge", f"at most {self.limits.max_cases} cases")
        files = bundle["files"]
        if not isinstance(files, dict) or not files:
            raise AdmissionError("InvalidRequest", "files must map each language to its files")
        code = {}
        count = 0
        for language, tree in sorted(files.items()):
            if language not in self._languages:
                raise AdmissionError("InvalidRequest", f"files: unknown language {language!r}")
            if not isinstance(tree, dict) or not tree:
                raise AdmissionError("InvalidRequest", f"files.{language} must map paths to base64 content")
            decoded = {}
            for name, content in tree.items():
                count += 1
                if count > self.limits.max_files:
                    raise AdmissionError("TooLarge", f"at most {self.limits.max_files} files")
                try:
                    check_relative(name)
                except StoreError as exc:
                    raise AdmissionError("InvalidRequest", f"files.{language}: {exc}") from exc
                if not isinstance(content, str):
                    raise AdmissionError("InvalidRequest", f"files.{language}.{name}: content must be base64 text")
                try:
                    decoded[name] = base64.b64decode(content, validate=True)
                except (binascii.Error, ValueError) as exc:
                    raise AdmissionError("InvalidRequest", f"files.{language}.{name}: not valid base64") from exc
            try:
                code[language] = self._store.put_code(decoded)
            except StoreError as exc:
                raise AdmissionError("InvalidRequest", f"files.{language}: {exc}") from exc
        # The fingerprint of what was submitted. An agent that retries (a timeout, a crash) gets its existing
        # submission back, whatever state it is in, and nothing is run again (REG-ADM-12).
        key = self.fingerprint(bundle)
        with self._submit_lock:
            existing = self._store.find_submission(submitter, key)
            if existing is not None:
                existing["duplicate"] = True
                return existing
            record = {
                "id": "sub-" + uuid.uuid4().hex[:20], "state": "received", "submitted_by": submitter,
                "submitted_at": now_iso(self._clock), "name": manifest["name"], "version": manifest["version"],
                "key": key, "manifest": manifest, "cases": cases, "code": code, "identity": None, "evidence": [],
                "problems": [], "decided_by": None, "decided_at": None, "row_seq": None,
            }
            self._store.save_submission(record)
        return record

    def _ownership_problem(self, record) -> str | None:
        """Library names are Taffee's, not the first submitter's: any Taffee agent may propose a new version, and
        the regression suite guards it (R-D15). Built-in tools change only through the reviewed repository."""
        rows = self._store.versions("tool", record["name"])
        if any(row["source"]["type"] == "file-index" for row in rows):
            return f"BuiltIn: {record['name']} is a built-in tool; it changes only through the reviewed repository"
        return None

    def _parse(self, raw) -> dict:
        if isinstance(raw, (bytes, bytearray)):
            if len(raw) > self.limits.max_bundle_bytes:
                raise AdmissionError("TooLarge", f"a bundle is at most {self.limits.max_bundle_bytes} bytes")
            try:
                bundle = json.loads(raw)
            except (ValueError, UnicodeDecodeError) as exc:
                raise AdmissionError("InvalidRequest", "the bundle is not JSON") from exc
        else:
            bundle = raw
            if len(canonical_bytes(bundle)) > self.limits.max_bundle_bytes:
                raise AdmissionError("TooLarge", f"a bundle is at most {self.limits.max_bundle_bytes} bytes")
        if not isinstance(bundle, dict) or set(bundle) != BUNDLE_KEYS or bundle.get("schema") != BUNDLE_SCHEMA:
            raise AdmissionError("InvalidRequest", f"a bundle has exactly {sorted(BUNDLE_KEYS)}, schema {BUNDLE_SCHEMA}")
        return bundle

    # -- process: validate, then test in the sandbox --
    def process(self, sid: str) -> dict:
        record = self._get(sid)
        if record["state"] not in OPEN_STATES:
            return record
        record = self._move(record, "validating")
        problems, manifest = self._validate(record)
        if problems:
            return self._reject(record, problems)
        identity = tool_identity(manifest, [{"language": lang, "code_digest": digest}
                                            for lang, digest in record["code"].items()])
        record["identity"], record["manifest"] = identity, manifest
        owned = self._ownership_problem(record)
        if owned:
            return self._reject(record, [owned])
        existing = self._store.get_row("tool", manifest["name"], manifest["version"])
        if existing is not None:
            if existing["source"].get("id") == record["id"]:
                # This submission's own row: a restart came between appending it and saving the record. Finish
                # the decision that was already signed; the library commit, if missing, was made by sync.
                record.update(state="admitted", row_seq=existing["seq"], decided_by=existing["admitted_by"],
                              decided_at=existing["admitted_at"])
                self._store.save_submission(record)
                return record
            if existing["identity"] == identity:  # REG-ADM-8: the same tool again; nothing new is written
                record.update(state="admitted", row_seq=existing["seq"],
                              problems=["duplicate: this exact version is already admitted"])
                self._store.save_submission(record)
                return record
            return self._reject(record, [f"VersionConflict: {manifest['name']}@{manifest['version']} is admitted with "
                                         "another identity; bump the version"])
        try:
            policy = policy_from_spec(manifest["spec"])
        except (SandboxError, KeyError, TypeError, ValueError) as exc:
            return self._reject(record, [f"spec.sandbox: {exc}"])
        record = self._move(record, "building")
        try:
            problems, built = self._build(record, policy)
        except SandboxUnavailable as exc:
            return self._reject(record, [f"sandbox cannot enforce the declared policy: {', '.join(exc.unmet)}"])
        if problems:
            return self._reject(record, problems)
        record = self._move(record, "testing")
        problems, evidence = self._test(record, manifest, policy)
        if problems:
            return self._reject(record, problems)
        record["evidence"] = built + evidence
        if self.approval == "auto":
            # Under the auto policy there is no one to wait for, so awaiting_approval is never stored or shown:
            # an agent polling its submission sees testing, then admitted (or rejected).
            with self._approve_lock:
                record["state"] = "awaiting_approval"
                return self._decide(record, AUTO_APPROVER)
        return self._move(record, "awaiting_approval")

    def clean_work(self) -> int:
        """Remove case folders a killed run left in the work folder. Call only before any submission is processed
        (at start-up). Returns how many were removed."""
        stale = [child for child in self._work.iterdir() if child.is_dir() and child.name.startswith("tmp")]
        for child in stale:
            shutil.rmtree(child, ignore_errors=True)
        return len(stale)

    def resume(self) -> list[str]:
        """Process every submission left open by a restart (REG-ADM-9). Returns their ids."""
        ids = [r["id"] for state in OPEN_STATES for r in self._store.submissions(state)]
        for sid in ids:
            self.process(sid)
        return ids

    def _validate(self, record) -> tuple[list[str], dict]:
        manifest = record["manifest"]
        problems = list(validate_manifest(manifest))
        if problems:
            return problems, manifest
        name, spec = manifest["name"], manifest["spec"]
        if not isinstance(spec.get("sandbox"), dict):
            problems.append("spec.sandbox: a sandbox policy must be declared")
        props = (spec.get("inputSchema") or {}).get("properties") or {}
        for prop, body in sorted(props.items()):
            if not isinstance(body, dict) or not isinstance(body.get("description"), str) or not body["description"].strip():
                problems.append(f"spec.inputSchema.properties.{prop}: a description is required")
        declared = {}
        for item in manifest["implementations"]:
            if item["location"] != f"library/{name}/{item['language']}":
                problems.append(f"implementations[{item['language']}]: location must be library/{name}/{item['language']}")
            declared[item["language"]] = item
        if not declared:
            problems.append("implementations: at least one is required")
        if set(declared) != set(record["code"]):
            problems.append(f"files: languages {sorted(record['code'])} do not match implementations {sorted(declared)}")
        for language, digest in sorted(record["code"].items()):
            if language not in self._languages:
                problems.append(f"implementations: unknown language {language!r}")
                continue
            folder = self._store.code_path(digest)
            entry = self._languages[language]["entry"]
            if folder is None or not (folder / entry).is_file():
                problems.append(f"files.{language}: the entry file {entry} is missing")
        case_problems = self._case_problems(record["cases"])
        problems.extend(case_problems)
        if not case_problems:
            problems.extend(self._coverage_problems(spec, record["cases"]))
        owned = dict(manifest, owner=LIBRARY_OWNER)  # admitted code is Taffee's; the submitter is provenance only
        return problems, sorted_manifest(owned)

    @staticmethod
    def _coverage_problems(spec, cases) -> list[str]:
        """Taffee's own rules for the cases (the opinionated part; the submitter does not choose them):
        every case's input is one a caller could send; every required parameter is exercised; at least one
        case expects success and at least one expects a tool-level error."""
        from jsonschema import Draft202012Validator

        problems = []
        validator = Draft202012Validator(spec["inputSchema"])
        for case in cases:
            if next(iter(validator.iter_errors(case["input"])), None) is not None:
                problems.append(f"cases.{case['id']}: the input does not match spec.inputSchema, so no caller could send it")
        required = set((spec["inputSchema"] or {}).get("required", []))
        used = set().union(*(set(case["input"]) for case in cases if isinstance(case["input"], dict)))
        for prop in sorted(required - used):
            problems.append(f"cases: the required parameter {prop} is never exercised")
        outcomes = {case["expect"].get("ok") for case in cases}
        if True not in outcomes:
            problems.append("cases: at least one case must expect success (expect.ok: true)")
        if False not in outcomes:
            problems.append("cases: at least one case must expect a tool-level error (expect.ok: false)")
        return problems

    def _case_problems(self, cases) -> list[str]:
        with tempfile.TemporaryDirectory(dir=self._work) as tmp:
            path = Path(tmp) / "cases.json"
            path.write_text(json.dumps(cases), encoding="utf-8")
            try:
                load_cases(path)
            except ConformanceError as exc:
                return [f"cases: {str(exc).split(': ', 1)[-1]}"]
        return []

    def _build(self, record, policy) -> tuple[list[str], list[str]]:
        """Compile every source file of every implementation in the sandbox, without running it."""
        problems, results = [], []
        for language, digest in sorted(record["code"].items()):
            argv = BUILD_STEPS.get(self._languages[language]["runner"])
            if argv is None:
                results.append({"language": language, "build": "none"})
                continue
            with tempfile.TemporaryDirectory(dir=self._work) as tmp:
                repo = Path(tmp).resolve() / "repo"
                repo.mkdir()
                for path in (repo.parent, repo):
                    path.chmod(0o755)
                done = self._sandbox.run(argv, root=repo, policy=policy, stdin=b"{}",
                                         implementation=self._store.code_path(digest))
            result = _parse_result(done)
            if result["ok"]:
                results.append({"language": language, "build": "ok"})
            elif result["error"].startswith("syntax: "):
                problems.append(_problem(f"{language}/build: {result['error']}"))  # file names and lines only
            else:
                problems.append(f"{language}/build: the build step did not complete ({result['error']})")
        digest = "sha256:" + hashlib.sha256(canonical_bytes({"identity": record["identity"], "build": results})).hexdigest()
        return problems, ([] if problems else [f"build:{digest}"])

    def regression_cases(self, name: str, version: str) -> list:
        """Every case of every admitted version with the same major version: what a new version must still pass.
        A new major version may change behaviour, so it starts a new suite."""
        # From the store, not the library: the library is an optional export (R-D14) and may be off.
        major = version.split(".")[0]
        cases = []
        for row in self._store.versions("tool", name):
            if row["version"].split(".")[0] != major or row["source"].get("type") != "submission":
                continue
            record = self._store.get_submission(row["source"].get("id", ""))
            cases += [dict(case, id=f"{row['version']}/{case['id']}") for case in (record or {}).get("cases", [])]
        return cases

    def _test(self, record, manifest, policy) -> tuple[list[str], list[str]]:
        spec = manifest["spec"]
        failures, results = [], []
        regression = self.regression_cases(manifest["name"], manifest["version"])
        for language, digest in sorted(record["code"].items()):
            folder = self._store.code_path(digest)
            runner = self._languages[language]["runner"]
            argv = RUNNERS[runner](Path(IMPLEMENTATION_TARGET) / self._languages[language]["entry"])
            for case in [*record["cases"], *regression]:
                label = f"{language}/{case['id']}" if case in record["cases"] else f"{language}/regression {case['id']}"
                try:
                    found = self._run_case(case, argv, policy, folder, spec)
                except SandboxUnavailable as exc:
                    return [f"sandbox cannot enforce the declared policy: {', '.join(exc.unmet)}"], []
                except (SandboxError, ConformanceError) as exc:
                    found = [type(exc).__name__ + ": the case could not be run"]
                failures.extend(_problem(f"{label}: {item}") for item in found)
                results.append({"language": language, "case": case["id"], "passed": not found})
            if not failures and spec.get("idempotent"):
                case = record["cases"][0]
                first, second = (self._raw_run(case, argv, policy, folder) for _ in range(2))
                if first.stdout != second.stdout:
                    failures.append(f"{language}/{case['id']}: declared idempotent, but two runs gave different results")
        if failures:
            return failures[:MAX_PROBLEMS], []
        digest_cases = cases_digest([*record["cases"], *regression])
        run = "sha256:" + hashlib.sha256(canonical_bytes({"identity": record["identity"], "cases": digest_cases,
                                                          "results": results})).hexdigest()
        return [], [f"conformance:{digest_cases}", f"run:{run}"]

    def _run_case(self, case, argv, policy, folder, spec) -> list[str]:
        done = self._raw_run(case, argv, policy, folder)
        if done.timed_out:
            return ["ran past its time limit"]
        if done.truncated:
            return ["wrote more than its output limit"]
        if done.returncode == 0 and _parse_result(done) == {"ok": False, "error": TOOL_ERRORS["result"]}:
            return ["did not answer with exactly one result line (the tool contract)"]
        completed = subprocess.CompletedProcess(args=[], returncode=done.returncode, stdout=done.stdout, stderr=b"")
        return check_expectations(completed, case["expect"]) + [
            item.split(": ", 1)[-1] for item in _postcondition_failures(case["id"], completed, spec)]

    def _raw_run(self, case, argv, policy, folder):
        with tempfile.TemporaryDirectory(dir=self._work) as tmp:
            base = Path(tmp).resolve()
            base.chmod(0o755)
            if "files" in case:
                repo = build_repository(case, base)
            else:
                repo = base / "repo"
                repo.mkdir()
            for path in (repo, *repo.rglob("*")):
                if not path.is_symlink():
                    path.chmod(0o755 if path.is_dir() else 0o644)
            stdin = json.dumps({"root": WORK, "input": case["input"]}).encode("utf-8")
            return self._sandbox.run(argv, root=repo, policy=policy, stdin=stdin, implementation=folder)

    # -- decisions --
    def approve(self, sid: str, approver: str) -> dict:
        with self._approve_lock:
            return self._decide(self._get(sid), approver)

    def _decide(self, record: dict, approver: str) -> dict:
        """Approve `record`. Called with the approve lock held."""
        if record["state"] != "awaiting_approval":
            raise AdmissionError("InvalidState", f"the submission is {record['state']}, not awaiting_approval")
        if approver == record["submitted_by"]:
            raise AdmissionError("Forbidden", "a submitter cannot approve their own submission")
        if _reserved(approver) and not (approver == AUTO_APPROVER and self.approval == "auto"):
            raise AdmissionError("Forbidden", "that principal name is reserved by the registry")
        manifest = record["manifest"]
        owned = self._ownership_problem(record)  # another publisher may have claimed the name meanwhile
        if owned:
            return self._reject(record, [owned])
        existing = self._store.get_row("tool", manifest["name"], manifest["version"])
        if existing is not None and existing["identity"] != record["identity"]:
            return self._reject(record, [f"VersionConflict: {manifest['name']}@{manifest['version']} was admitted "
                                         "with another identity meanwhile"])
        if existing is None:
            fields = {
                "kind": "tool", "name": manifest["name"], "version": manifest["version"], "manifest": manifest,
                "manifest_digest": manifest_digest(manifest), "identity": record["identity"],
                "implementations": [{"language": lang, "code_digest": digest,
                                     "entry": self._languages[lang]["entry"]}
                                    for lang, digest in sorted(record["code"].items())],
                "label": "Validated", "evidence": record["evidence"],
                "source": {"type": "submission", "id": record["id"], "provenance": "taffee-agent"},
                "submitted_by": record["submitted_by"], "admitted_by": approver,
                "admitted_at": now_iso(self._clock),
            }
            seq = append_signed(self._store, self._key, fields)
        else:
            seq = existing["seq"]
        record.update(state="admitted", decided_by=approver, decided_at=now_iso(self._clock), row_seq=seq)
        if self.library is not None:
            row = self._store.get_row("tool", manifest["name"], manifest["version"])
            folders = {lang: self._store.code_path(digest) for lang, digest in record["code"].items()}
            try:
                record["library_commit"] = self.library.admit(row, folders, record["cases"], record["id"])
            except Exception:  # the signed row stands; LibraryRepo.sync commits it at the next start
                LOG.exception("library commit failed for %s@%s; sync will retry", manifest["name"], manifest["version"])
                record["library_commit"] = None
        self._store.save_submission(record)
        return record

    def sync_library(self) -> int:
        """Commit every admitted row the library is missing (after a failed commit). Idempotent."""
        if self.library is None:
            return 0

        def cases_of(row):
            found = self._store.get_submission(row["source"].get("id", ""))
            return found["cases"] if found else []

        return self.library.sync(self._store, cases_of)

    def reject(self, sid: str, approver: str, reason: str | None = None) -> dict:
        record = self._get(sid)
        if record["state"] in ("admitted", "rejected"):
            raise AdmissionError("InvalidState", f"the submission is already {record['state']}")
        if approver == record["submitted_by"] or _reserved(approver):
            raise AdmissionError("Forbidden", "a submitter cannot decide their own submission")
        text = "rejected by the approver" + (f": {reason[:200]}" if isinstance(reason, str) and reason else "")
        record.update(decided_by=approver, decided_at=now_iso(self._clock))
        return self._reject(record, [text])

    # -- helpers --
    def get(self, sid: str) -> dict:
        return self._get(sid)

    def _get(self, sid):
        record = self._store.get_submission(sid) if isinstance(sid, str) else None
        if record is None:
            raise AdmissionError("NotFound", "no such submission")
        return record

    def _move(self, record, state):
        record["state"] = state
        self._store.save_submission(record)
        return record

    def _reject(self, record, problems):
        record.update(state="rejected", problems=[_problem(p) for p in problems][:MAX_PROBLEMS])
        self._store.save_submission(record)
        return record


RESERVED_PRINCIPALS = ("policy:", "file-index")


def _reserved(principal) -> bool:
    return not isinstance(principal, str) or principal.startswith(RESERVED_PRINCIPALS)


def append_signed(store, private_key, fields: dict, attempts: int = 3) -> int:
    """Sign `fields` at the store's head and append it; retry if another writer moved the head."""
    for _ in range(attempts):
        seq, prev = store.head()
        row = sign_row(private_key, fields, seq=seq + 1, prev=prev)
        try:
            store.append_row(row, row_hash(row))
            return row["seq"]
        except StoreConflict:
            if store.get_row(fields["kind"], fields["name"], fields["version"]) is not None:
                raise
    raise StoreConflict("could not append at the head")


__all__ = ["Admission", "AdmissionError", "Limits", "append_signed", "BUNDLE_SCHEMA", "STATES"]
