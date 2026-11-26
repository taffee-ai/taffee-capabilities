"""S-018: an in-memory CapabilityStore. The second implementation that proves the interface is not
shaped around SQLite (02_DECISIONS R-D04). Code folders still live on disk, in a CodeArea, because the
sandbox mounts a folder. Rows are kept as canonical JSON, so a caller can never alter a stored row.
"""

import copy
import json
import threading

from ..canon import canonical_bytes
from ..rows import GENESIS
from .base import LABEL_RANK, CapabilityStore, StoreConflict, row_languages, search_fields, version_key
from .code import CodeArea


class MemoryStore(CapabilityStore):
    def __init__(self, code_dir):
        self._code = CodeArea(code_dir)
        self._lock = threading.RLock()
        self._rows: list[bytes] = []          # chain order; index = seq - 1
        self._hashes: list[str] = []
        self._by_key: dict[tuple, int] = {}   # (kind, name, version) -> seq
        self._submissions: dict[str, bytes] = {}

    @staticmethod
    def _load(raw: bytes) -> dict:
        return json.loads(raw)

    def head(self):
        with self._lock:
            return (len(self._rows), self._hashes[-1] if self._hashes else GENESIS)

    def append_row(self, row, row_hash):
        with self._lock:
            seq, prev = self.head()
            if row.get("seq") != seq + 1 or row.get("prev") != prev:
                raise StoreConflict("append is not at the head of the chain")
            key = (row["kind"], row["name"], row["version"])
            if key in self._by_key:
                raise StoreConflict(f"{row['name']}@{row['version']} already exists")
            self._rows.append(canonical_bytes(row))
            self._hashes.append(row_hash)
            self._by_key[key] = seq + 1

    def _row(self, seq: int) -> dict:
        return self._load(self._rows[seq - 1])

    def get_row(self, kind, name, version):
        with self._lock:
            seq = self._by_key.get((kind, name, version))
            return None if seq is None else self._row(seq)

    def versions(self, kind, name):
        with self._lock:
            rows = [self._row(seq) for (k, n, _), seq in self._by_key.items() if k == kind and n == name]
        return sorted(rows, key=lambda row: version_key(row["version"]))

    def _all(self, kind):
        with self._lock:
            return [self._row(seq) for (k, _, _), seq in self._by_key.items() if k == kind]

    def rows_page(self, kind, after, limit, min_rank=0):
        rows = [row for row in self._all(kind) if LABEL_RANK.get(row["label"], -1) >= min_rank]
        rows.sort(key=lambda row: (row["name"], version_key(row["version"])))
        if after is not None:
            mark = (after[0], version_key(after[1]))
            rows = [row for row in rows if (row["name"], version_key(row["version"])) > mark]
        return rows[:limit]

    def _latest(self, kind):
        newest = {}
        for row in self._all(kind):
            current = newest.get(row["name"])
            if current is None or version_key(row["version"]) > version_key(current["version"]):
                newest[row["name"]] = row
        return newest.values()

    def search(self, terms, kind, limit, offset=0, min_rank=0, language=None):
        if not terms:
            return []
        scored = []
        for row in self._latest(kind):
            if LABEL_RANK.get(row["label"], -1) < min_rank:
                continue
            if language is not None and language not in row_languages(row):
                continue
            name, summary, params = (text.split() for text in search_fields(row))
            fields = (name, summary, params)
            if not all(any(word.startswith(term) for words in fields for word in words) for term in terms):
                continue
            # Score: name hits weigh most, then summary, then parameters (a simple stand-in for bm25).
            score = sum(weight * sum(word.startswith(term) for word in words)
                        for term in terms for weight, words in zip((10, 3, 1), fields))
            exact = row["name"] == "-".join(terms)
            scored.append((not exact, -score, row["name"], row))
        scored.sort(key=lambda item: item[:3])
        return [copy.deepcopy(item[3]) for item in scored[offset:offset + limit]]

    def rows_from(self, seq, limit):
        with self._lock:
            start = max(seq, 1) - 1
            return [self._load(raw) for raw in self._rows[start:start + limit]]

    def count(self):
        with self._lock:
            return len(self._rows)

    def put_code(self, files):
        return self._code.put(files)

    def remove_incomplete_code(self) -> int:
        return self._code.remove_incoming()

    def code_path(self, code_digest):
        return self._code.path(code_digest)

    def save_submission(self, record):
        with self._lock:
            self._submissions[record["id"]] = canonical_bytes(record)

    def get_submission(self, sid):
        with self._lock:
            raw = self._submissions.get(sid)
        return None if raw is None else self._load(raw)

    def find_submission(self, submitter, key):
        with self._lock:
            records = [self._load(raw) for raw in self._submissions.values()]
        found = [r for r in records if r.get("submitted_by") == submitter and r.get("key") == key
                 and r.get("state") != "rejected"]
        return max(found, key=lambda r: (r.get("submitted_at", ""), r["id"])) if found else None

    def submissions(self, state=None):
        with self._lock:
            records = [self._load(raw) for raw in self._submissions.values()]
        return sorted((r for r in records if state is None or r["state"] == state), key=lambda r: r["id"])
