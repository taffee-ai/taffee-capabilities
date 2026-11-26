"""A development revocation feed: DEVELOPMENT ONLY. The real feed belongs to Governance (S-003).

It keeps, in one folder:
  feed.key          the feed's private key (mode 0600), created on first use
  feed.pub          its public key, base64, for `serve --revocation-key-file`
  entries.jsonl     signed, hash-chained revocation entries, one per line
  checkpoint.json   the current signed checkpoint
  serial            the last checkpoint serial issued

`run` re-signs the checkpoint every `interval` seconds, well inside the client's 30-second freshness bound.
`revoke` appends one entry. Both take a file lock, so a revoke never races the signer. Every write is
atomic (write, then rename), so a reader never sees half a file.
"""

from __future__ import annotations

import base64
import fcntl
import json
import os
import time
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from .revocation import GENESIS, check_target, entry_hash, signed_bytes, verify_entry


def _iso(epoch: float) -> str:
    return datetime.fromtimestamp(epoch, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _write_atomic(path: Path, data: bytes, mode: int = 0o644) -> None:
    temp = path.with_name(path.name + ".tmp")
    temp.write_bytes(data)
    os.chmod(temp, mode)
    os.replace(temp, path)


class DevFeed:
    def __init__(self, folder):
        self.dir = Path(folder)
        self.dir.mkdir(parents=True, exist_ok=True)
        with self._locked():
            key_path = self.dir / "feed.key"
            if not key_path.exists():
                private = Ed25519PrivateKey.generate()
                raw = private.private_bytes(serialization.Encoding.Raw, serialization.PrivateFormat.Raw,
                                            serialization.NoEncryption())
                _write_atomic(key_path, base64.b64encode(raw) + b"\n", 0o600)
                public = private.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
                _write_atomic(self.dir / "feed.pub", base64.b64encode(public) + b"\n")
            if not (self.dir / "entries.jsonl").exists():
                _write_atomic(self.dir / "entries.jsonl", b"")
        raw = base64.b64decode((self.dir / "feed.key").read_bytes().strip(), validate=True)
        self._key = Ed25519PrivateKey.from_private_bytes(raw)

    @contextmanager
    def _locked(self):
        with open(self.dir / ".lock", "a+") as handle:
            fcntl.flock(handle, fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(handle, fcntl.LOCK_UN)

    def public_key_b64(self) -> str:
        return (self.dir / "feed.pub").read_text(encoding="ascii").strip()

    def _sign(self, fields: dict) -> dict:
        signed = dict(fields)
        signed["sig"] = base64.b64encode(self._key.sign(signed_bytes(fields))).decode("ascii")
        return signed

    def _entries(self) -> list:
        text = (self.dir / "entries.jsonl").read_text(encoding="utf-8")
        return [json.loads(line) for line in text.splitlines() if line.strip()]

    def revoke(self, target: dict, reason: str = "incident", actor: str = "dev-feed", reference: str = "DEV-1") -> dict:
        check_target(target)
        with self._locked():
            entries = self._entries()
            prev = entry_hash(entries[-1]) if entries else GENESIS
            entry = self._sign({"seq": len(entries) + 1, "prev": prev, "target": target, "reason": reason,
                                "actor": actor, "time": _iso(time.time()), "reference": reference})
            # Never write an entry the client would refuse: one bad entry makes every client fail closed until
            # the feed is replaced (found by deploy/smoke.py). The client's own check is the check.
            verify_entry(entry, self._key.public_key())
            lines = "".join(json.dumps(e) + "\n" for e in [*entries, entry])
            _write_atomic(self.dir / "entries.jsonl", lines.encode("utf-8"))
            self._issue_locked()
        return entry

    def issue(self) -> dict:
        with self._locked():
            return self._issue_locked()

    def _issue_locked(self) -> dict:
        entries = self._entries()
        serial_path = self.dir / "serial"
        serial = int(serial_path.read_text()) + 1 if serial_path.exists() else 1
        checkpoint = self._sign({"serial": serial, "latest_seq": len(entries),
                                 "head_hash": entry_hash(entries[-1]) if entries else GENESIS,
                                 "issued_at": _iso(time.time())})
        _write_atomic(self.dir / "checkpoint.json", (json.dumps(checkpoint) + "\n").encode("utf-8"))
        _write_atomic(serial_path, str(serial).encode("ascii"))
        return checkpoint

    def run(self, interval: float = 5.0, stop=None):
        while stop is None or not stop.is_set():
            self.issue()
            if stop is not None:
                stop.wait(interval)
            else:
                time.sleep(interval)
