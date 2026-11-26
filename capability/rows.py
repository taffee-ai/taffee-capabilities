"""S-018: signed, hash-chained registry rows and the signed checkpoint (03_DESIGN §2.1, §2.2).

A row is one admitted capability version. It is signed by the admission key (Ed25519, over the
canonical JSON of every field except `sig`) and chained: `prev` is the hash of the previous row,
including that row's signature. A store holds rows; it is never asked whether a row is valid.
"""

import base64
import hashlib
import time

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey, Ed25519PublicKey

from .canon import canonical_bytes

ROW_SCHEMA = "capability-row/1"
CHECKPOINT_SCHEMA = "capability-checkpoint/1"
GENESIS = "sha256:" + hashlib.sha256(b"taffee-capability-row-genesis/1").hexdigest()
ROW_FIELDS = frozenset({
    "schema", "seq", "prev", "key_id", "kind", "name", "version", "manifest", "manifest_digest", "identity",
    "implementations", "label", "evidence", "source", "submitted_by", "admitted_by", "admitted_at", "sig",
})
CHECKPOINT_FIELDS = frozenset({"schema", "key_id", "count", "head", "issued_at", "sig"})


class RowError(Exception):
    """A row or checkpoint is malformed or does not verify. `code` is IntegrityError."""

    code = "IntegrityError"


def _b64(data: bytes) -> str:
    return base64.b64encode(data).decode("ascii")


def public_bytes(public_key: Ed25519PublicKey) -> bytes:
    return public_key.public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)


def key_id(public_key: Ed25519PublicKey) -> str:
    """A short, stable name for a public key, recorded in every row so keys can be rotated later."""
    return "ed25519:" + hashlib.sha256(public_bytes(public_key)).hexdigest()[:16]


def load_private_key(path) -> Ed25519PrivateKey:
    """A raw 32-byte Ed25519 private key, base64 in a file."""
    with open(path, "rb") as handle:
        raw = base64.b64decode(handle.read().strip(), validate=True)
    return Ed25519PrivateKey.from_private_bytes(raw)


def generate_key_file(path) -> Ed25519PublicKey:
    """Write a new private key file (mode 0600) and return its public key."""
    import os

    private = Ed25519PrivateKey.generate()
    raw = private.private_bytes(serialization.Encoding.Raw, serialization.PrivateFormat.Raw,
                                serialization.NoEncryption())
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "wb") as handle:
        handle.write(_b64(raw).encode("ascii") + b"\n")
    return private.public_key()


def unsigned_bytes(record: dict) -> bytes:
    return canonical_bytes({key: value for key, value in record.items() if key != "sig"})


def row_hash(row: dict) -> str:
    """The chain hash of a row. It covers the signature, so the next row commits to it too."""
    return "sha256:" + hashlib.sha256(canonical_bytes(row)).hexdigest()


def now_iso(clock=time.time) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(clock()))


def sign_row(private_key: Ed25519PrivateKey, fields: dict, *, seq: int, prev: str) -> dict:
    """Return the signed row for `fields` (everything except schema, seq, prev, key_id and sig)."""
    row = dict(fields)
    row.update({"schema": ROW_SCHEMA, "seq": seq, "prev": prev, "key_id": key_id(private_key.public_key())})
    missing = ROW_FIELDS - {"sig"} - set(row)
    extra = set(row) - ROW_FIELDS
    if missing or extra:
        raise RowError(f"row fields: missing {sorted(missing)}, unknown {sorted(extra)}")
    row["sig"] = _b64(private_key.sign(unsigned_bytes(row)))
    return row


def _verify_sig(public_key: Ed25519PublicKey, record: dict, what: str) -> None:
    sig = record.get("sig")
    if not isinstance(sig, str):
        raise RowError(f"{what} has no signature")
    try:
        public_key.verify(base64.b64decode(sig, validate=True), unsigned_bytes(record))
    except (InvalidSignature, ValueError) as exc:
        raise RowError(f"{what} signature does not verify") from exc


def verify_row(public_key: Ed25519PublicKey, row: object) -> dict:
    """Return the row if it is well formed and its signature verifies; else raise RowError."""
    if not isinstance(row, dict) or set(row) != ROW_FIELDS or row.get("schema") != ROW_SCHEMA:
        raise RowError("not a capability row")
    if row["key_id"] != key_id(public_key):
        raise RowError("row was signed by another key")
    _verify_sig(public_key, row, f"row {row.get('name')}@{row.get('version')}")
    return row


def sign_checkpoint(private_key: Ed25519PrivateKey, count: int, head: str, clock=time.time) -> dict:
    checkpoint = {"schema": CHECKPOINT_SCHEMA, "key_id": key_id(private_key.public_key()), "count": count,
                  "head": head, "issued_at": now_iso(clock)}
    checkpoint["sig"] = _b64(private_key.sign(unsigned_bytes(checkpoint)))
    return checkpoint


def verify_checkpoint(public_key: Ed25519PublicKey, checkpoint: object) -> dict:
    if (not isinstance(checkpoint, dict) or set(checkpoint) != CHECKPOINT_FIELDS
            or checkpoint.get("schema") != CHECKPOINT_SCHEMA or checkpoint["key_id"] != key_id(public_key)):
        raise RowError("not a capability checkpoint for this key")
    _verify_sig(public_key, checkpoint, "checkpoint")
    return checkpoint


def verify_store(store, public_key: Ed25519PublicKey, checkpoint: dict | None = None, batch: int = 1000) -> int:
    """Walk every row in chain order: signature, seq, prev. Compare with `checkpoint` when given.

    Returns the number of rows. Raises RowError at the first problem. This is what detects a deleted,
    reordered or inserted row, which a per-row signature alone cannot (02_DECISIONS R-D03).
    """
    prev, seq = GENESIS, 0
    at_checkpoint = None
    while True:
        rows = store.rows_from(seq + 1, batch)
        if not rows:
            break
        for row in rows:
            verify_row(public_key, row)
            seq += 1
            if row["seq"] != seq:
                raise RowError(f"chain: expected seq {seq}, found {row['seq']}")
            if row["prev"] != prev:
                raise RowError(f"chain: row {seq} does not follow row {seq - 1}")
            prev = row_hash(row)
            if checkpoint is not None and seq == checkpoint["count"]:
                at_checkpoint = prev
    if store.head() != (seq, prev):
        raise RowError("chain: the store's head does not match its rows")
    if checkpoint is not None:
        verify_checkpoint(public_key, checkpoint)
        if checkpoint["count"] > seq:
            raise RowError(f"chain: the checkpoint covers {checkpoint['count']} rows, the store has {seq}")
        if checkpoint["count"] == 0:
            at_checkpoint = GENESIS
        if at_checkpoint != checkpoint["head"]:
            raise RowError("chain: the rows do not match the checkpoint")
    return seq
