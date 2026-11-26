"""S-003 revocation client: entries, signatures, chain and checkpoints.

An entry is one signed statement that a target is revoked. The signature is
Ed25519 over the canonical JSON of every other field. The key comes from
configuration, never from the feed (CAP-REV-11).

`RevocationClient` keeps the verified chain and the freshness deadline. It
trusts nothing it is handed: every entry and checkpoint is verified, a failure
keeps the last verified state, and freshness comes only from an accepted
checkpoint (CAP-REV-6, CAP-REV-8).
"""

import base64
import copy
import hashlib
import json
import re
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

from capability.canon import canonical_bytes

# Wire constant shared with the feed publisher (taffee-governance pkg/feed). Changing it breaks every existing feed.
GENESIS_LABEL = b"aicore-revocation-genesis/1"
GENESIS = "sha256:" + hashlib.sha256(GENESIS_LABEL).hexdigest()

KINDS = ("tool", "agent", "model")
REASONS = (
    "evidence_failure",
    "incident",
    "dependency_changed",
    "expired",
    "owner_withdrawn",
)
ENTRY_FIELDS = frozenset(
    {"seq", "prev", "target", "reason", "actor", "time", "reference", "sig"}
)
CHECKPOINT_FIELDS = frozenset({"serial", "latest_seq", "head_hash", "issued_at", "sig"})

# Same grammar as the manifest schemas, so a revocation can name exactly what the
# catalog can contain.
HASH_PATTERN = re.compile(r"^sha256:[0-9a-f]{64}$")
NAME_PATTERN = re.compile(r"^[a-z][a-z0-9-]{1,63}$")
VERSION_PATTERN = re.compile(r"^(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)$")
MAX_TEXT = 256
TIMESTAMP_PATTERN = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}Z")
MAX_LINE_BYTES = 64 * 1024

FRESHNESS_SECONDS = 30.0
MAX_CHECKPOINT_AGE = 30.0
CLOCK_SKEW = 10.0


class RevocationError(Exception):
    """A typed failure. `code` is one of the stable codes below."""

    def __init__(self, code: str, message: str):
        super().__init__(f"{code}: {message}")
        self.code = code
        self.message = message


def load_public_key(encoded: str) -> Ed25519PublicKey:
    """Read the configured public key: 32 raw bytes, base64 encoded.

    An empty or missing value is KeyMissing. The client never falls back to a
    key found in the feed.
    """
    if not encoded:
        raise RevocationError("KeyMissing", "no revocation public key is configured")
    try:
        raw = base64.b64decode(encoded, validate=True)
        return Ed25519PublicKey.from_public_bytes(raw)
    except (ValueError, TypeError) as exc:
        raise RevocationError("KeyMissing", f"configured key is not a valid Ed25519 key: {exc}")


def signed_bytes(entry: dict) -> bytes:
    """The bytes that the signature covers: every field except `sig`."""
    return canonical_bytes({k: v for k, v in entry.items() if k != "sig"})


def entry_hash(entry: dict) -> str:
    """The chain hash: SHA-256 of the canonical JSON, including `sig`."""
    return "sha256:" + hashlib.sha256(canonical_bytes(entry)).hexdigest()


def _is_hash(value: object) -> bool:
    return isinstance(value, str) and HASH_PATTERN.fullmatch(value) is not None


def _is_utf8(value: str) -> bool:
    """False for text with a lone surrogate, which canonical JSON could not encode."""
    try:
        value.encode("utf-8")
    except UnicodeEncodeError:
        return False
    return True


def _check_text(entry: dict, field: str) -> None:
    value = entry[field]
    if (
        not isinstance(value, str)
        or not value
        or len(value) > MAX_TEXT
        or any(ord(ch) < 32 or ord(ch) == 127 for ch in value)
        or not _is_utf8(value)
    ):
        raise RevocationError(
            "EntryShape",
            f"{field} must be a non-empty UTF-8 string of at most {MAX_TEXT} characters, no control characters",
        )


def check_target(target: object) -> None:
    """A target names a kind and either a digest or a name and version.

    A target without `kind` is refused, and so is a name-only target (CAP-REV-1).
    """
    if not isinstance(target, dict):
        raise RevocationError("TargetInvalid", "target must be an object")
    if target.get("kind") not in KINDS:
        raise RevocationError("TargetInvalid", "target needs a kind: tool, agent or model")
    extra = set(target) - {"kind", "digest", "name", "version"}
    if extra:
        raise RevocationError("TargetInvalid", f"unexpected target fields: {sorted(extra)}")
    if "digest" in target:
        if set(target) - {"kind", "digest"}:
            raise RevocationError("TargetInvalid", "a digest target has no name or version")
        if not _is_hash(target["digest"]):
            raise RevocationError("TargetInvalid", "digest must be sha256: and 64 lowercase hex")
        return
    if set(target) != {"kind", "name", "version"}:
        raise RevocationError("TargetInvalid", "a name@version target needs name and version")
    if not isinstance(target["name"], str) or not NAME_PATTERN.fullmatch(target["name"]):
        raise RevocationError("TargetInvalid", "name must match ^[a-z][a-z0-9-]{1,63}$")
    if not isinstance(target["version"], str) or not VERSION_PATTERN.fullmatch(target["version"]):
        raise RevocationError("TargetInvalid", "version must be canonical X.Y.Z")


def verify_entry(entry: object, public_key: Ed25519PublicKey) -> dict:
    """Return the entry if it is well formed and its signature verifies.

    Raises RevocationError on any failure. Nothing from a failing entry is
    returned, so its target can never be used (CAP-REV-4).
    """
    if not isinstance(entry, dict):
        raise RevocationError("EntryShape", "entry must be an object")
    if set(entry) != ENTRY_FIELDS:
        raise RevocationError("EntryShape", f"entry fields must be exactly {sorted(ENTRY_FIELDS)}")
    if not isinstance(entry["seq"], int) or isinstance(entry["seq"], bool) or entry["seq"] < 1:
        raise RevocationError("EntryShape", "seq must be an integer of at least 1")
    if not _is_hash(entry["prev"]):
        raise RevocationError("EntryShape", "prev must be sha256: and 64 lowercase hex")
    if entry["reason"] not in REASONS:
        raise RevocationError("EntryShape", "reason is not in the declared set")
    for field in ("actor", "reference"):
        _check_text(entry, field)
    parse_time(entry["time"], "EntryShape")
    check_target(entry["target"])
    try:
        signature = base64.b64decode(entry["sig"], validate=True)
    except (ValueError, TypeError):
        raise RevocationError("BadSignature", "sig is not base64")
    try:
        public_key.verify(signature, signed_bytes(entry))
    except InvalidSignature:
        raise RevocationError("BadSignature", "signature does not verify")
    return entry


def _is_int(value: object, minimum: int) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value >= minimum


def verify_checkpoint(checkpoint: object, public_key: Ed25519PublicKey) -> dict:
    """Return the checkpoint if it is well formed and its signature verifies."""
    if not isinstance(checkpoint, dict):
        raise RevocationError("CheckpointShape", "checkpoint must be an object")
    if set(checkpoint) != CHECKPOINT_FIELDS:
        raise RevocationError(
            "CheckpointShape", f"checkpoint fields must be exactly {sorted(CHECKPOINT_FIELDS)}"
        )
    if not _is_int(checkpoint["serial"], 1) or not _is_int(checkpoint["latest_seq"], 0):
        raise RevocationError("CheckpointShape", "serial and latest_seq must be integers")
    if not _is_hash(checkpoint["head_hash"]):
        raise RevocationError("CheckpointShape", "head_hash must be sha256: and 64 lowercase hex")
    parse_time(checkpoint["issued_at"])
    try:
        signature = base64.b64decode(checkpoint["sig"], validate=True)
    except (ValueError, TypeError):
        raise RevocationError("BadSignature", "sig is not base64")
    try:
        public_key.verify(signature, signed_bytes(checkpoint))
    except InvalidSignature:
        raise RevocationError("BadSignature", "signature does not verify")
    return checkpoint


def parse_time(value: object, code: str = "CheckpointShape") -> float:
    """Parse `YYYY-MM-DDTHH:MM:SSZ` to epoch seconds. Any other form is refused."""
    if not isinstance(value, str) or TIMESTAMP_PATTERN.fullmatch(value) is None:
        raise RevocationError(code, "timestamp must be exactly YYYY-MM-DDTHH:MM:SSZ")
    try:
        moment = datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ")
    except ValueError:
        raise RevocationError(code, "timestamp must be YYYY-MM-DDTHH:MM:SSZ")
    return moment.replace(tzinfo=timezone.utc).timestamp()


def _no_duplicates(pairs: list) -> dict:
    out: dict = {}
    for key, value in pairs:
        if key in out:
            raise RevocationError("LineInvalid", f"duplicate key {key!r}")
        out[key] = value
    return out


def _no_constants(name: str):
    raise RevocationError("LineInvalid", f"{name} is not valid JSON")


def parse_feed_line(line: object) -> dict:
    """Parse one JSON line from the feed into an object, strictly.

    One line is one JSON object. Malformed JSON, a duplicate key, NaN or
    Infinity, an embedded newline, a non-object, or a line over 64 KiB is a
    `LineInvalid` or `LineTooLong`. Parsing does not trust the content: the
    result still goes through `verify_entry` or `verify_checkpoint`.
    """
    if isinstance(line, bytes):
        if len(line) > MAX_LINE_BYTES:
            raise RevocationError("LineTooLong", "line exceeds 64 KiB")
        try:
            line = line.decode("utf-8")
        except UnicodeDecodeError:
            raise RevocationError("LineInvalid", "line is not UTF-8")
    if not isinstance(line, str):
        raise RevocationError("LineInvalid", "line must be text")
    if len(line.encode("utf-8")) > MAX_LINE_BYTES:
        raise RevocationError("LineTooLong", "line exceeds 64 KiB")
    body = line[:-1] if line.endswith("\n") else line
    if not body.strip() or "\n" in body or "\r" in body:
        raise RevocationError("LineInvalid", "a line holds exactly one JSON object")
    try:
        value = json.loads(body, object_pairs_hook=_no_duplicates, parse_constant=_no_constants)
    except json.JSONDecodeError as exc:
        raise RevocationError("LineInvalid", f"not valid JSON: {exc.msg}")
    if not isinstance(value, dict):
        raise RevocationError("LineInvalid", "a line must be a JSON object")
    return value


def _plain(value: object, code: str, depth: int = 0) -> object:
    """A private, plain-data copy of a JSON value: dict, list, str, int, float, bool or None only.

    The copy is what gets verified and stored, so a caller who keeps a reference to the original
    cannot change a verified entry afterwards. Any other type is refused with ``code``.
    """
    if depth > 8:
        raise RevocationError(code, "value is nested too deeply")
    if isinstance(value, dict):
        out = {}
        for key, item in value.items():
            if not isinstance(key, str):
                raise RevocationError(code, "object keys must be strings")
            out[key] = _plain(item, code, depth + 1)
        return out
    if isinstance(value, (list, tuple)):
        return [_plain(item, code, depth + 1) for item in value]
    if value is None or isinstance(value, (str, bool, int, float)):
        return value
    raise RevocationError(code, f"unsupported value type {type(value).__name__}")


@dataclass(frozen=True)
class CheckResult:
    """The answer to one check: `status` is "ok", "revoked" or "stale".

    `entry` is the verified feed entry that revokes the target, for "revoked".
    `seq` is the verified head the answer was computed against.
    """

    status: str
    seq: int
    entry: dict | None = None


class RevocationClient:
    """The verified view of a revocation feed.

    State: the verified head `(seq, hash)`, the verified entries, the last
    accepted checkpoint serial, and a monotonic freshness deadline. `faulted`
    is set by any verification failure that leaves the client unable to vouch
    for what it knows. A faulted client is stale until an accepted checkpoint
    matches its chain. Both clocks are injectable for tests.
    """

    def __init__(
        self,
        public_key: Ed25519PublicKey,
        *,
        wall=time.time,
        monotonic=time.monotonic,
        freshness: float = FRESHNESS_SECONDS,
        max_checkpoint_age: float = MAX_CHECKPOINT_AGE,
        clock_skew: float = CLOCK_SKEW,
    ):
        self._key = public_key
        self._wall = wall
        self._mono = monotonic
        self._freshness = freshness
        self._max_age = max_checkpoint_age
        self._skew = clock_skew
        self.head_seq = 0
        self.head_hash = GENESIS
        self._entries: list[dict] = []
        self.last_serial = 0
        self.deadline: float | None = None
        self.faulted = False
        self.last_error: RevocationError | None = None

    @property
    def entries(self) -> list:
        """A copy of the verified entries. Changing it cannot change what the client revokes."""
        return copy.deepcopy(self._entries)

    def ingest(self, entries: list) -> int:
        """Verify and append entries in order. Returns how many were appended.

        The first failure sets `faulted`, keeps everything verified so far, and
        raises. Nothing from the failing entry is used (CAP-REV-4). An entry
        already held, byte for byte, is skipped; a different entry at a held
        `seq` is a fork. Each entry is copied to plain data first, and that copy
        is what is verified and stored.
        """
        appended = 0
        for original in entries:
            try:
                entry = _plain(original, "EntryShape")
                verify_entry(entry, self._key)
                self._place(entry)
            except RevocationError:
                self.faulted = True
                raise
            else:
                appended += self._append(entry)
        return appended

    def _place(self, entry: dict) -> None:
        seq = entry["seq"]
        if seq <= self.head_seq:
            held = self._entries[seq - 1]
            if entry_hash(held) != entry_hash(entry):
                raise RevocationError("ChainBroken", f"seq {seq} differs from the verified entry")
            return
        if seq != self.head_seq + 1:
            raise RevocationError("Gap", f"expected seq {self.head_seq + 1}, got {seq}")
        if entry["prev"] != self.head_hash:
            raise RevocationError("ChainBroken", f"seq {seq} does not link to the verified head")

    def _append(self, entry: dict) -> int:
        if entry["seq"] <= self.head_seq:
            return 0
        self._entries.append(entry)
        self.head_seq = entry["seq"]
        self.head_hash = entry_hash(entry)
        return 1

    def accept_checkpoint(self, checkpoint: object) -> None:
        """Accept a signed checkpoint, or raise. Only acceptance refreshes freshness.

        A replay (the serial already accepted) refreshes nothing and changes no
        state. A lower serial, a head behind the client, a head the client
        has not caught up to, or an `issued_at` outside the age and skew window
        (CAP-REV-12) faults the client: a signed checkpoint the client cannot
        accept means its fresh view can no longer be vouched for.
        The freshness left is the bound minus the checkpoint's age (CAP-REV-8).

        A checkpoint that fails verification (a bad signature or a malformed shape)
        faults the client, like a bad entry (CAP-REV-4): a fresh client becomes stale,
        its head and deadline do not move, and only a later valid checkpoint recovers it.
        """
        try:
            checkpoint = _plain(checkpoint, "CheckpointShape")
            verify_checkpoint(checkpoint, self._key)
        except RevocationError:
            self.faulted = True
            raise
        serial = checkpoint["serial"]
        if serial == self.last_serial:
            raise RevocationError("Replay", f"serial {serial} was already accepted")
        if serial < self.last_serial:
            self.faulted = True
            raise RevocationError("Rollback", f"serial {serial} is below {self.last_serial}")
        issued = parse_time(checkpoint["issued_at"])
        age = self._wall() - issued
        if age >= self._max_age:
            self.faulted = True
            raise RevocationError("CheckpointTooOld", f"checkpoint is {age:.0f}s old")
        if -age > self._skew:
            self.faulted = True
            raise RevocationError("CheckpointFromFuture", f"checkpoint is {-age:.0f}s ahead")
        latest = checkpoint["latest_seq"]
        if latest < self.head_seq:
            self.faulted = True
            raise RevocationError("Rollback", f"latest_seq {latest} is below the verified head")
        if latest > self.head_seq:
            self.faulted = True
            raise RevocationError("Behind", f"checkpoint attests seq {latest}; client holds {self.head_seq}")
        if checkpoint["head_hash"] != self.head_hash:
            self.faulted = True
            raise RevocationError("ChainBroken", "checkpoint head does not match the verified chain")
        self.last_serial = serial
        self.deadline = self._mono() + (self._freshness - max(0.0, age))
        self.faulted = False

    def seconds_left(self) -> float:
        """Freshness left on the monotonic clock. 0 when faulted, never refreshed, or expired."""
        if self.faulted or self.deadline is None:
            return 0.0
        return max(0.0, self.deadline - self._mono())

    def is_fresh(self) -> bool:
        """True only when not faulted and the monotonic deadline has not passed."""
        return (not self.faulted) and self.deadline is not None and self._mono() < self.deadline

    def poll(self, source) -> None:
        """One fetch through the transport interface (SPEC §6). Never raises.

        `source.entries_since(seq)` returns feed lines after `seq`; `source.checkpoint()`
        returns the checkpoint line. The client verifies everything it is given. An
        unreachable source changes nothing, so the client simply goes stale at its
        deadline. A line that does not parse, or a verification failure, faults the client
        and is kept in `last_error`. A replayed checkpoint is the normal result of polling
        faster than the feed issues them: it is ignored and does not fault.
        """
        try:
            for line in source.entries_since(self.head_seq):
                try:
                    entry = parse_feed_line(line)
                except RevocationError:
                    self.faulted = True
                    raise
                self.ingest([entry])
            try:
                checkpoint = parse_feed_line(source.checkpoint())
            except RevocationError:
                self.faulted = True
                raise
            self.accept_checkpoint(checkpoint)
        except RevocationError as exc:
            self.last_error = None if exc.code == "Replay" else exc
        except OSError as exc:
            self.last_error = RevocationError("SourceUnavailable", str(exc))
        else:
            self.last_error = None

    def check(self, kind: str, name: str, version: str, digest: str) -> CheckResult:
        """Is this capability revoked? One check evaluates both target forms.

        A digest revocation matches the kind and digest. A name@version
        revocation matches the kind, name and version whatever digest is
        presented, so a digest-only caller cannot bypass it (CAP-REV-9). A
        revocation never crosses kinds. Only verified entries are consulted.

        A verified match is reported as "revoked" even when the client is
        stale, because the entry is authoritative and the answer is already a
        refusal. Otherwise a client that is not fresh answers "stale", and
        callers fail closed. Malformed input is a `CheckInput` error, never "ok".
        """
        if kind not in KINDS:
            raise RevocationError("CheckInput", "kind must be tool, agent or model")
        if not isinstance(name, str) or not NAME_PATTERN.fullmatch(name):
            raise RevocationError("CheckInput", "name does not follow the manifest grammar")
        if not isinstance(version, str) or not VERSION_PATTERN.fullmatch(version):
            raise RevocationError("CheckInput", "version must be canonical X.Y.Z")
        if not _is_hash(digest):
            raise RevocationError("CheckInput", "digest must be sha256: and 64 lowercase hex")
        for entry in self._entries:
            target = entry["target"]
            if target["kind"] != kind:
                continue
            if target.get("digest") == digest or (
                target.get("name") == name and target.get("version") == version
            ):
                return CheckResult("revoked", self.head_seq, copy.deepcopy(entry))
        if not self.is_fresh():
            return CheckResult("stale", self.head_seq)
        return CheckResult("ok", self.head_seq)


class FileFeed:
    """A transport for development: the entries and the checkpoint are files (SPEC §6).

    `entries_path` holds one entry per line, in order, so the entries after `seq` are the lines
    after the first `seq` lines. `checkpoint_path` holds the current checkpoint. A missing or
    unreadable file raises OSError, which `RevocationClient.poll` treats as an unreachable feed.
    """

    def __init__(self, entries_path, checkpoint_path):
        self._entries = Path(entries_path)
        self._checkpoint = Path(checkpoint_path)

    def entries_since(self, seq: int) -> list:
        lines = [line for line in self._entries.read_text(encoding="utf-8").splitlines(keepends=True) if line.strip()]
        return lines[seq:]

    def checkpoint(self) -> str:
        return self._checkpoint.read_text(encoding="utf-8")
