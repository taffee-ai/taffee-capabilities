"""S-019: the short-lived store that makes `call_id` idempotent for `POST /v2/call`.

A caller retries; a tool must never run twice. The first call that runs a tool takes its `(principal, call_id)`
and its reply is kept here for the retention window. A retry of the same request gets that reply back, without
running anything and without a second call record. A different request under a taken `call_id` is a conflict.
A call refused before the tool ran takes nothing, so a retry runs normally. A retry that arrives while the first
call is still running is held until that call finishes, and then gets its reply: the same response either way.
Only if the hold bound passes first does it answer InProgress.

This store holds whole replies, which can contain tool output. That is why it is separate from the S-012 call
records (references only, kept for audit) and has its own, short retention: expired rows are deleted.

States: `running` (claimed, the call is in progress) and `done` (the reply is stored). A `running` row found when
the store is opened was left by a stopped service: the tool may or may not have run, so it is never run again;
the row becomes a stored `Interrupted` reply instead.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
import threading
import time
from pathlib import Path

DEFAULT_RETENTION_S = 24 * 3600
# Long enough for the slowest tool a manifest may declare (timeoutMs up to 600 s) plus the sandbox's 60 s start-up
# allowance, so InProgress is left for a call that is stuck, not a slow one.
DEFAULT_HOLD_S = 660.0
INTERRUPTED = (500, {"code": "Interrupted",
                     "message": "the service stopped while this call was running; it was not run again"})
REQUEST_KEYS = ("name", "range", "min_label", "languages", "input", "workspace", "grant")


def request_digest(request: dict) -> str:
    """What makes two requests under one call_id the same request: every field except the call_id."""
    body = {key: request.get(key) for key in REQUEST_KEYS}
    data = json.dumps(body, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    return "sha256:" + hashlib.sha256(data).hexdigest()


class CallReplyStore:
    """One SQLite file, opened by the service alone. Thread-safe."""

    def __init__(self, path, *, retention_s: float = DEFAULT_RETENTION_S, hold_s: float = DEFAULT_HOLD_S,
                 clock=time.time, monotonic=time.monotonic):
        if not retention_s > 0:
            raise ValueError("retention_s must be positive")
        if not hold_s >= 0:
            raise ValueError("hold_s must not be negative")
        self._path = Path(path)
        self._retention, self._clock, self._monotonic = float(retention_s), clock, monotonic
        self.hold_s = float(hold_s)
        self._lock = threading.Lock()
        self._changed = threading.Condition(self._lock)  # notified when a claim finishes or is given back
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._db = sqlite3.connect(self._path, isolation_level=None, check_same_thread=False)
        self._db.execute("""CREATE TABLE IF NOT EXISTS replies (
            principal TEXT NOT NULL, call_id TEXT NOT NULL, request_digest TEXT NOT NULL, state TEXT NOT NULL,
            status INTEGER, reply TEXT, created REAL NOT NULL, expires REAL NOT NULL,
            PRIMARY KEY (principal, call_id))""")
        status, payload = INTERRUPTED
        self._db.execute("UPDATE replies SET state = 'done', status = ?, reply = ? WHERE state = 'running'",
                         (status, json.dumps(payload)))

    @property
    def retention_s(self) -> float:
        return self._retention

    def close(self):
        with self._lock:
            self._db.close()

    def begin(self, principal: str, call_id: str, digest: str, hold_s: float = 0.0):
        """Claim `(principal, call_id)` for a request, or say why not. Returns one of:
        ("claimed",), ("replay", status, payload), ("conflict",), ("in_progress",).

        When the same request is still running, wait up to `hold_s` seconds for it: its reply is then a replay,
        or, if it was given back (refused before its tool started), this request claims the call_id itself.
        ("in_progress",) only when the hold ends first. A conflict is never held."""
        deadline = self._monotonic() + max(0.0, hold_s)
        with self._changed:
            while True:
                now = self._clock()
                self._db.execute("DELETE FROM replies WHERE expires <= ?", (now,))
                row = self._db.execute("SELECT request_digest, state, status, reply FROM replies WHERE principal = ? "
                                       "AND call_id = ?", (principal, call_id)).fetchone()
                if row is None:
                    self._db.execute("INSERT INTO replies (principal, call_id, request_digest, state, created, expires) "
                                     "VALUES (?, ?, ?, 'running', ?, ?)",
                                     (principal, call_id, digest, now, now + self._retention))
                    return ("claimed",)
                stored_digest, state, status, reply = row
                if stored_digest != digest:
                    return ("conflict",)
                if state != "running":
                    return ("replay", status, json.loads(reply))
                remaining = deadline - self._monotonic()
                if remaining <= 0:
                    return ("in_progress",)
                self._changed.wait(remaining)

    def finish(self, principal: str, call_id: str, status: int, payload: dict):
        """Store the reply of a call whose tool ran."""
        with self._changed:
            self._db.execute("UPDATE replies SET state = 'done', status = ?, reply = ? WHERE principal = ? AND "
                             "call_id = ? AND state = 'running'", (status, json.dumps(payload), principal, call_id))
            self._changed.notify_all()

    def release(self, principal: str, call_id: str):
        """Give the call_id back: the call was refused before the tool ran."""
        with self._changed:
            self._db.execute("DELETE FROM replies WHERE principal = ? AND call_id = ? AND state = 'running'",
                             (principal, call_id))
            self._changed.notify_all()


__all__ = ["CallReplyStore", "DEFAULT_HOLD_S", "DEFAULT_RETENTION_S", "INTERRUPTED", "request_digest"]
