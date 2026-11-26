"""S-018: the default CapabilityStore, on SQLite (02_DECISIONS R-D04; 03_DESIGN §6).

WAL mode, so readers are not blocked while an admission writes. One connection per thread. FTS5 for
search; the store refuses to open without it. Rows are stored as their canonical JSON (`body`); the
other columns are filters and are never trusted by the query layer.

Lookups by key and search use indexes (REG-QRY-7). The only full scans are rows_from (verify_store)
and count.
"""

import json
import sqlite3
import threading
from pathlib import Path

from ..canon import canonical_bytes
from ..rows import GENESIS
from .base import LABEL_RANK, CapabilityStore, StoreConflict, StoreError, row_languages, search_fields, version_key
from .code import CodeArea

SCHEMA_VERSION = "1"
DDL = """
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS rows (
  seq INTEGER PRIMARY KEY, kind TEXT NOT NULL, name TEXT NOT NULL, version TEXT NOT NULL,
  major INTEGER NOT NULL, minor INTEGER NOT NULL, patch INTEGER NOT NULL,
  label_rank INTEGER NOT NULL, languages TEXT NOT NULL, hash TEXT NOT NULL UNIQUE, body TEXT NOT NULL,
  UNIQUE (kind, name, version));
CREATE INDEX IF NOT EXISTS rows_by_name ON rows (kind, name, major, minor, patch);
CREATE TABLE IF NOT EXISTS latest (kind TEXT NOT NULL, name TEXT NOT NULL, seq INTEGER NOT NULL,
  PRIMARY KEY (kind, name));
CREATE VIRTUAL TABLE IF NOT EXISTS rows_fts USING fts5(name, summary, params, tokenize = 'unicode61');
CREATE TABLE IF NOT EXISTS code (digest TEXT PRIMARY KEY);
CREATE TABLE IF NOT EXISTS submissions (id TEXT PRIMARY KEY, state TEXT NOT NULL, submitter TEXT, sub_key TEXT,
  submitted_at TEXT, body TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS submissions_by_state ON submissions (state, id);
CREATE INDEX IF NOT EXISTS submissions_by_key ON submissions (submitter, sub_key);
"""


class SQLiteStore(CapabilityStore):
    def __init__(self, data_dir, *, read_only=False):
        """`read_only` opens an existing store for checking (verify, audits) and never writes: no folder, table or
        row is created, and closing the connection cannot checkpoint or remove the WAL. Use it for any process
        that is not the store's one writer. Where file locks do not work across processes (Docker Desktop bind
        mounts on macOS), a second read-write connection that closes removes the live writer's WAL."""
        self._dir = Path(data_dir)
        self._read_only = bool(read_only)
        self._path = self._dir / "registry.sqlite3"
        if self._read_only:
            if not self._path.is_file():
                raise StoreError(f"no store at {self._path}")
            self._code = CodeArea(self._dir / "code", create=False)
        else:
            self._dir.mkdir(parents=True, exist_ok=True)
            self._code = CodeArea(self._dir / "code")
        self._local = threading.local()
        self._write_lock = threading.Lock()
        conn = self._conn()
        try:
            conn.execute("CREATE VIRTUAL TABLE temp.fts5_probe USING fts5(x)")
            conn.execute("DROP TABLE temp.fts5_probe")
        except sqlite3.OperationalError as exc:
            raise StoreError("this SQLite build has no FTS5; search cannot work") from exc
        if self._read_only:
            found = conn.execute("SELECT value FROM meta WHERE key = 'schema'").fetchone()
            if found is None or found[0] != SCHEMA_VERSION:
                raise StoreError(f"store schema {found[0] if found else 'missing'} is not {SCHEMA_VERSION}")
            return
        with self._write_lock, conn:
            conn.executescript(DDL)
            found = conn.execute("SELECT value FROM meta WHERE key = 'schema'").fetchone()
            if found is None:
                conn.execute("INSERT INTO meta VALUES ('schema', ?)", (SCHEMA_VERSION,))
            elif found[0] != SCHEMA_VERSION:
                raise StoreError(f"store schema {found[0]} is not {SCHEMA_VERSION}")

    @property
    def path(self) -> Path:
        return self._path

    def _conn(self) -> sqlite3.Connection:
        conn = getattr(self._local, "conn", None)
        if conn is None:
            if self._read_only:  # the writer set WAL mode in the file; a read-only connection must not change it
                conn = sqlite3.connect(self._path.resolve().as_uri() + "?mode=ro", uri=True, timeout=30,
                                       isolation_level=None, check_same_thread=True)
                self._local.conn = conn
                return conn
            conn = sqlite3.connect(self._path, timeout=30, isolation_level=None, check_same_thread=True)
            conn.execute("PRAGMA journal_mode = WAL")
            conn.execute("PRAGMA synchronous = FULL")  # every admission step survives power loss
            conn.execute("PRAGMA foreign_keys = ON")
            self._local.conn = conn
        return conn

    def close(self):
        conn = getattr(self._local, "conn", None)
        if conn is not None:
            conn.close()
            self._local.conn = None

    @staticmethod
    def _load(body: str) -> dict:
        return json.loads(body)

    # -- rows --
    def head(self):
        found = self._conn().execute("SELECT seq, hash FROM rows WHERE seq = (SELECT max(seq) FROM rows)").fetchone()
        return (0, GENESIS) if found is None else (found[0], found[1])

    def append_row(self, row, row_hash):
        major, minor, patch = version_key(row["version"])
        body = canonical_bytes(row).decode("utf-8")
        name_text, summary_text, params_text = search_fields(row)
        conn = self._conn()
        with self._write_lock:
            conn.execute("BEGIN IMMEDIATE")
            try:
                seq, prev = self.head()
                if row.get("seq") != seq + 1 or row.get("prev") != prev:
                    raise StoreConflict("append is not at the head of the chain")
                try:
                    conn.execute(
                        "INSERT INTO rows (seq, kind, name, version, major, minor, patch, label_rank, languages, hash, body)"
                        " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                        (row["seq"], row["kind"], row["name"], row["version"], major, minor, patch,
                         LABEL_RANK.get(row["label"], -1), "," + ",".join(row_languages(row)) + ",", row_hash, body))
                except sqlite3.IntegrityError as exc:
                    raise StoreConflict(f"{row['name']}@{row['version']} already exists") from exc
                current = conn.execute(
                    "SELECT r.seq, r.major, r.minor, r.patch FROM latest l JOIN rows r ON r.seq = l.seq"
                    " WHERE l.kind = ? AND l.name = ?", (row["kind"], row["name"])).fetchone()
                if current is None or (major, minor, patch) > tuple(current[1:]):
                    if current is not None:
                        conn.execute("DELETE FROM rows_fts WHERE rowid = ?", (current[0],))
                    conn.execute("INSERT OR REPLACE INTO latest (kind, name, seq) VALUES (?, ?, ?)",
                                 (row["kind"], row["name"], row["seq"]))
                    conn.execute("INSERT INTO rows_fts (rowid, name, summary, params) VALUES (?, ?, ?, ?)",
                                 (row["seq"], name_text, summary_text, params_text))
                conn.execute("COMMIT")
            except BaseException:
                conn.execute("ROLLBACK")
                raise

    def get_row(self, kind, name, version):
        found = self._conn().execute("SELECT body FROM rows WHERE kind = ? AND name = ? AND version = ?",
                                     (kind, name, version)).fetchone()
        return None if found is None else self._load(found[0])

    def versions(self, kind, name):
        cursor = self._conn().execute(
            "SELECT body FROM rows WHERE kind = ? AND name = ? ORDER BY major, minor, patch", (kind, name))
        return [self._load(body) for (body,) in cursor]

    def rows_page(self, kind, after, limit, min_rank=0):
        if after is None:
            cursor = self._conn().execute(
                "SELECT body FROM rows WHERE kind = ? AND label_rank >= ?"
                " ORDER BY name, major, minor, patch LIMIT ?", (kind, min_rank, limit))
        else:
            name, version = after
            major, minor, patch = version_key(version)
            cursor = self._conn().execute(
                "SELECT body FROM rows WHERE kind = ? AND label_rank >= ?"
                " AND (name > ? OR (name = ? AND (major, minor, patch) > (?, ?, ?)))"
                " ORDER BY name, major, minor, patch LIMIT ?",
                (kind, min_rank, name, name, major, minor, patch, limit))
        return [self._load(body) for (body,) in cursor]

    def search(self, terms, kind, limit, offset=0, min_rank=0, language=None):
        if not terms or not all(term.isascii() and term.isalnum() for term in terms):
            return []
        match = " AND ".join(f'"{term}"*' for term in terms)  # terms are [a-z0-9]+: nothing to escape
        sql = ("SELECT r.body FROM rows_fts f JOIN rows r ON r.seq = f.rowid"
               " WHERE rows_fts MATCH ? AND r.kind = ? AND r.label_rank >= ?")
        params = [match, kind, min_rank]
        if language is not None:
            sql += " AND instr(r.languages, ?) > 0"
            params.append("," + language + ",")
        sql += " ORDER BY (r.name = ?) DESC, bm25(rows_fts, 10.0, 3.0, 1.0), r.name LIMIT ? OFFSET ?"
        params += ["-".join(terms), limit, offset]
        try:
            cursor = self._conn().execute(sql, params)
            return [self._load(body) for (body,) in cursor]
        except sqlite3.OperationalError as exc:
            raise StoreError("search failed") from exc

    def rows_from(self, seq, limit):
        cursor = self._conn().execute("SELECT body FROM rows WHERE seq >= ? ORDER BY seq LIMIT ?", (seq, limit))
        return [self._load(body) for (body,) in cursor]

    def count(self):
        return self._conn().execute("SELECT count(*) FROM rows").fetchone()[0]

    # -- code --
    def put_code(self, files):
        digest = self._code.put(files)
        with self._write_lock:
            self._conn().execute("INSERT OR IGNORE INTO code (digest) VALUES (?)", (digest,))
        return digest

    def remove_incomplete_code(self) -> int:
        return self._code.remove_incoming()

    def code_path(self, code_digest):
        return self._code.path(code_digest)

    # -- submissions --
    def save_submission(self, record):
        with self._write_lock:
            self._conn().execute(
                "INSERT OR REPLACE INTO submissions (id, state, submitter, sub_key, submitted_at, body)"
                " VALUES (?, ?, ?, ?, ?, ?)",
                (record["id"], record["state"], record.get("submitted_by"), record.get("key"),
                 record.get("submitted_at"), canonical_bytes(record).decode("utf-8")))

    def get_submission(self, sid):
        found = self._conn().execute("SELECT body FROM submissions WHERE id = ?", (sid,)).fetchone()
        return None if found is None else self._load(found[0])

    def find_submission(self, submitter, key):
        found = self._conn().execute(
            "SELECT body FROM submissions WHERE submitter = ? AND sub_key = ? AND state != 'rejected'"
            " ORDER BY submitted_at DESC, id DESC LIMIT 1", (submitter, key)).fetchone()
        return None if found is None else self._load(found[0])

    def submissions(self, state=None):
        if state is None:
            cursor = self._conn().execute("SELECT body FROM submissions ORDER BY id")
        else:
            cursor = self._conn().execute("SELECT body FROM submissions WHERE state = ? ORDER BY id", (state,))
        return [self._load(body) for (body,) in cursor]
