"""S-012: call records.

One record per call: what ran, under what revocation knowledge, by whose grant, and what happened.
Records hold references (digests, byte counts, codes), never content. The log is a hash chain, so it
can be checked without trusting whoever kept it. It is tamper-evident, not a signature.
"""

import hashlib
import hmac
import json
import os
import re
import time
from datetime import datetime, timezone
from pathlib import Path

from .calltime import CallError
from .canon import canonical_bytes
from .registry import RegistryError

SCHEMA = "capability-call-record/1"
GENESIS = "sha256:" + hashlib.sha256(b"taffee-call-record-genesis/1").hexdigest()
OUTCOMES = ("ok", "tool_failed", "input_rejected", "output_rejected", "refused", "error")
# The codes a refusal may carry: the stable error codes of the registry, the revocation check, the grant check
# and the sandbox. A closed set, so a code can never be free text that came from a tool or an exception.
REFUSAL_CODES = frozenset({
    "IndexError", "SetupError", "NotFound", "BelowLabel", "Ambiguous", "InvalidRange", "NoImplementation",
    "IntegrityError", "EntryError", "SourceUnavailable", "Revoked", "RevocationStale", "GrantDenied",
    "SandboxUnavailable",
})
# Which codes each status may carry. Anything else is refused by `record`.
OUTCOME_CODES = {
    "ok": frozenset({None}),
    "tool_failed": frozenset({None}),
    "input_rejected": frozenset({"ValidationFailed"}),
    "output_rejected": frozenset({"PostconditionFailed"}),
    "refused": REFUSAL_CODES,
    "error": frozenset({"Unexpected"}),
}
KINDS = ("tool", "agent", "model")
LABELS = ("Declared", "Validated", "Tested", "Trusted", "Approved")
CALL_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,63}")
GRANT = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:/-]{0,127}")
HASH = re.compile(r"sha256:[0-9a-f]{64}")
NAME = re.compile(r"[a-z][a-z0-9-]{1,63}")
VERSION = re.compile(r"(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)")
RECORD_KEYS = {"schema", "seq", "prev", "call_id", "time", "capability", "language", "code_digest",
               "revocation", "grant", "outcome", "input", "output", "duration_ms", "hash"}
_MISSING = object()


class CallRecordError(Exception):
    """A record could not be written, or a log failed verification."""


def _record_hash(record: dict) -> str:
    body = {key: value for key, value in record.items() if key != "hash"}
    return "sha256:" + hashlib.sha256(canonical_bytes(body)).hexdigest()


def outcome_for(result: object = None, error: BaseException | None = None) -> dict:
    """Classify a call's end as `{"status", "code"}`. Never carries message text or content."""
    unexpected = {"status": "error", "code": "Unexpected"}
    if error is not None:
        if isinstance(error, CallError):
            if error.code == "ValidationFailed":
                return {"status": "input_rejected", "code": "ValidationFailed"}
            if error.code == "PostconditionFailed":
                return {"status": "output_rejected", "code": "PostconditionFailed"}
            return unexpected
        code = error.code if isinstance(error, RegistryError) else getattr(error, "code", None)
        if isinstance(code, str) and code in REFUSAL_CODES:
            return {"status": "refused", "code": code}
        return unexpected  # an unknown code is never copied into a record
    if isinstance(result, dict) and result.get("ok") is True:
        return {"status": "ok", "code": None}
    if isinstance(result, dict) and result.get("ok") is False:
        return {"status": "tool_failed", "code": None}
    return {"status": "error", "code": "Unexpected"}


def verify_log(path) -> list:
    """Return every record if the whole chain verifies, else raise CallRecordError."""
    path = Path(path)
    if not path.exists():
        return []
    records = []
    prev, seq = GENESIS, 0
    for number, line in enumerate(path.read_bytes().split(b"\n"), start=1):
        if not line:
            continue
        try:
            record = json.loads(line)
        except ValueError:
            raise CallRecordError(f"line {number} is not valid JSON") from None
        if not isinstance(record, dict) or set(record) != RECORD_KEYS or record.get("schema") != SCHEMA:
            raise CallRecordError(f"line {number} is not a call record")
        seq += 1
        if record["seq"] != seq:
            raise CallRecordError(f"line {number}: expected seq {seq}, got {record['seq']}")
        if record["prev"] != prev:
            raise CallRecordError(f"line {number}: the chain is broken (a record was changed, removed, inserted or forked)")
        if record["hash"] != _record_hash(record):
            raise CallRecordError(f"line {number}: the record does not match its hash")
        prev = record["hash"]
        records.append(record)
    return records


def calls_for(path, kind: str, name: str, version: str | None = None, digest: str | None = None,
              outcome: str | None = None, since_seq: int = 1) -> list:
    """The verified records for a capability, or for a digest, so affected calls can be found."""
    if kind not in KINDS:
        raise CallRecordError("kind must be tool, agent or model")
    found = []
    for record in verify_log(path):
        cap = record["capability"]
        if record["seq"] < since_seq or cap["kind"] != kind or cap["name"] != name:
            continue
        if version is not None and cap["version"] != version:
            continue
        if digest is not None and digest not in (cap["digest"], record["code_digest"]):
            continue
        if outcome is not None and record["outcome"]["status"] != outcome:
            continue
        found.append(record)
    return found


class CallRecorder:
    """Appends call records to one log. One writer per log."""

    def __init__(self, path, *, clock=time.time, digest_key: bytes | None = None, fsync: bool = True):
        self._path = Path(path)
        self._clock = clock
        self._key = digest_key
        self._fsync = fsync
        self._stopped = False  # set when a rollback fails: the log and the head can no longer be trusted to agree
        records = verify_log(self._path)
        self._seq = len(records)
        self._prev = records[-1]["hash"] if records else GENESIS

    def head(self) -> tuple:
        """The latest `(seq, hash)`. An external party that keeps it can detect removal of newer records."""
        return self._seq, self._prev

    def _digest(self, data: bytes) -> str:
        if self._key is None:
            return "sha256:" + hashlib.sha256(data).hexdigest()
        return "sha256:" + hmac.new(self._key, data, hashlib.sha256).hexdigest()

    def _reference(self, value: object) -> dict:
        try:
            data = (canonical_bytes(value) if not isinstance(value, str) else value.encode("utf-8"))
        except (TypeError, ValueError, UnicodeEncodeError):
            raise CallRecordError("a value to be recorded is not JSON-compatible text") from None
        return {"digest": self._digest(data), "bytes": len(data)}

    def record(self, call_id: str, capability: dict, outcome: dict, *, language: str | None = None,
               code_digest: str | None = None, revocation_seq: int | None = None, grant: str | None = None,
               input_value: object = _MISSING, output: str | None = None, duration_ms: int = 0) -> dict:
        """Write one record and return it. Raises CallRecordError, writing nothing, on a bad value."""
        if self._stopped:
            raise CallRecordError("this recorder stopped after a failed rollback; verify the log and open a new CallRecorder")
        if not isinstance(call_id, str) or CALL_ID.fullmatch(call_id) is None:
            raise CallRecordError("call_id must be 1 to 64 characters of letters, digits and . _ : -")
        if grant is not None and (not isinstance(grant, str) or GRANT.fullmatch(grant) is None):
            raise CallRecordError("grant must be a reference of up to 128 characters, not free text")
        if not isinstance(duration_ms, int) or isinstance(duration_ms, bool) or duration_ms < 0:
            raise CallRecordError("duration_ms must be a non-negative integer")
        if revocation_seq is not None and (not isinstance(revocation_seq, int) or isinstance(revocation_seq, bool)
                                           or revocation_seq < 0):
            raise CallRecordError("revocation_seq must be a non-negative integer or None")
        if not isinstance(capability, dict) or set(capability) != {"kind", "name", "version", "digest", "label"}:
            raise CallRecordError("capability needs exactly kind, name, version, digest and label")
        if (capability["kind"] not in KINDS or not isinstance(capability["name"], str) or NAME.fullmatch(capability["name"]) is None
                or not isinstance(capability["version"], str) or VERSION.fullmatch(capability["version"]) is None):
            raise CallRecordError("capability kind, name or version is not in the catalog grammar")
        value = capability["digest"]
        if value is not None and (not isinstance(value, str) or HASH.fullmatch(value) is None):
            raise CallRecordError("capability digest must be sha256:<64 hex> or None")
        if capability["label"] is not None and capability["label"] not in LABELS:
            raise CallRecordError("capability label must be a known label or None")
        if code_digest is not None and (not isinstance(code_digest, str) or HASH.fullmatch(code_digest) is None):
            raise CallRecordError("code_digest must be sha256:<64 hex> or None")
        if (not isinstance(outcome, dict) or set(outcome) != {"status", "code"} or outcome["status"] not in OUTCOMES
                or not (outcome["code"] is None or isinstance(outcome["code"], str))
                or outcome["code"] not in OUTCOME_CODES[outcome["status"]]):
            raise CallRecordError("outcome must come from outcome_for: a known status with a code that status may carry")
        if language is not None and (not isinstance(language, str) or re.fullmatch(r"[a-z][a-z0-9-]{0,31}", language) is None):
            raise CallRecordError("language must be a language id")
        record = {
            "schema": SCHEMA,
            "seq": self._seq + 1,
            "prev": self._prev,
            "call_id": call_id,
            "time": datetime.fromtimestamp(self._clock(), tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "capability": dict(capability),
            "language": language,
            "code_digest": code_digest,
            "revocation": ({"status": "checked", "seq": revocation_seq} if revocation_seq is not None
                           else {"status": "unchecked", "seq": None}),
            "grant": grant,
            "outcome": dict(outcome),
            "input": self._reference(input_value) if input_value is not _MISSING else None,
            "output": self._reference(output) if output is not None else None,
            "duration_ms": duration_ms,
        }
        record["hash"] = _record_hash(record)
        line = json.dumps(record, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode("utf-8") + b"\n"
        self._append(line)
        self._seq, self._prev = record["seq"], record["hash"]  # only after the whole line is written
        return record

    def _append(self, line: bytes) -> None:
        """Write the whole line, or leave the log as it was and raise. Never report success on a partial line."""
        descriptor = os.open(self._path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
        try:
            before = os.fstat(descriptor).st_size
            try:
                view = memoryview(line)
                written = 0
                while written < len(line):
                    count = os.write(descriptor, view[written:])
                    if count <= 0:
                        raise OSError("a write made no progress")
                    written += count
                if self._fsync:
                    os.fsync(descriptor)
            except OSError as exc:
                try:
                    os.ftruncate(descriptor, before)
                except OSError:
                    # The log may now hold a partial line (verify_log detects it) or a whole, valid line that the
                    # caller is told failed (verify_log accepts it). Either way the head here is stale: stop.
                    self._stopped = True
                    raise CallRecordError("the record could not be written and the rollback failed; the log may hold "
                                          "a partial record, or a whole record this call reported as failed. This "
                                          "recorder is stopped; verify the log and open a new CallRecorder") from exc
                raise CallRecordError("the record could not be written; the log was left as it was") from exc
        finally:
            os.close(descriptor)


def capability_ref(match: dict, identity: str | None = None) -> dict:
    """The `capability` argument for a resolved match. Pass a tool's identity when you have it."""
    digest = identity if match["kind"] == "tool" and identity else match["manifest_digest"]
    return {"kind": match["kind"], "name": match["name"], "version": match["version"],
            "digest": digest, "label": match["label"]}
