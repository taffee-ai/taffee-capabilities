"""S-018: the store interface (03_DESIGN §2.5). A store holds rows, code folders and submissions.

A store is trusted for nothing: the query layer verifies every row it serves (signature, then the
fields it relies on). The indexed columns a store keeps (label rank, languages, version parts) are
only filters; a value a tampered column lets through is caught when the row is verified.
`tests/test_store_contract.py` is the definition of what each method must do.
"""

from __future__ import annotations

import re
from abc import ABC, abstractmethod
from pathlib import Path

LABEL_RANK = {"Declared": 0, "Validated": 1, "Tested": 2, "Trusted": 3, "Approved": 4}
WORD = re.compile(r"[A-Za-z0-9]+")


class StoreError(Exception):
    """The store could not do what was asked (I/O, corruption, missing feature)."""

    code = "StoreError"


class StoreConflict(StoreError):
    """An append that is not at the head, or a duplicate (kind, name, version)."""

    code = "StoreConflict"


def version_key(version: str) -> tuple[int, int, int]:
    major, minor, patch = (int(part) for part in version.split("."))
    return major, minor, patch


def words(text: str) -> list[str]:
    return [word.lower() for word in WORD.findall(text or "")]


def search_fields(row: dict) -> tuple[str, str, str]:
    """The three searchable texts of a row: name, summary, and the input parameters' names and descriptions."""
    manifest = row["manifest"]
    props = (manifest.get("spec", {}).get("inputSchema", {}) or {}).get("properties", {}) or {}
    params = " ".join(f"{name} {body.get('description', '') if isinstance(body, dict) else ''}"
                      for name, body in sorted(props.items()))
    return " ".join(words(row["name"])), " ".join(words(manifest.get("summary", ""))), " ".join(words(params))


def row_languages(row: dict) -> list[str]:
    return sorted(item["language"] for item in row.get("implementations", []))


class CapabilityStore(ABC):
    def remove_incomplete_code(self) -> int:
        """Remove partly written code folders a killed writer left. Call only at the start of the one process
        that writes the store. Stores without a code area on disk have nothing to remove."""
        return 0

    # -- rows --
    @abstractmethod
    def head(self) -> tuple[int, str]:
        """(seq, row hash) of the last row; (0, rows.GENESIS) when empty."""

    @abstractmethod
    def append_row(self, row: dict, row_hash: str) -> None:
        """Append a signed row. StoreConflict unless row.seq == head.seq + 1, row.prev == head hash, and the
        (kind, name, version) is new. The store does not verify the signature; it must not change the row."""

    @abstractmethod
    def get_row(self, kind: str, name: str, version: str) -> dict | None: ...

    @abstractmethod
    def versions(self, kind: str, name: str) -> list[dict]:
        """Every version of one name, oldest first (semantic version order)."""

    @abstractmethod
    def rows_page(self, kind: str, after: tuple[str, str] | None, limit: int, min_rank: int = 0) -> list[dict]:
        """Rows of `kind` with label rank >= min_rank, ordered by (name, version), strictly after `after`."""

    @abstractmethod
    def search(self, terms: list[str], kind: str, limit: int, offset: int = 0, min_rank: int = 0,
               language: str | None = None) -> list[dict]:
        """The newest version of each name whose search fields contain, for every term, a word starting
        with it. An exact name match (terms joined by '-') first, then store-defined relevance, then name.
        `terms` are lower-case [a-z0-9]+ words; an empty list matches nothing."""

    @abstractmethod
    def rows_from(self, seq: int, limit: int) -> list[dict]:
        """Rows with seq >= `seq`, in chain order."""

    @abstractmethod
    def count(self) -> int: ...

    # -- code --
    @abstractmethod
    def put_code(self, files: dict[str, bytes]) -> str:
        """Store one implementation tree; return its sha256-tree-v1 digest. Identical trees are stored once."""

    @abstractmethod
    def code_path(self, code_digest: str) -> Path | None: ...

    # -- submissions --
    @abstractmethod
    def save_submission(self, record: dict) -> None: ...

    @abstractmethod
    def get_submission(self, sid: str) -> dict | None: ...

    @abstractmethod
    def submissions(self, state: str | None = None) -> list[dict]: ...

    @abstractmethod
    def find_submission(self, submitter: str, key: str) -> dict | None:
        """The most recent submission by `submitter` with fingerprint `key` that was not rejected, or None.
        A record's fingerprint is its `key` field; records without one are never found."""

    def close(self) -> None:
        """Release resources. Optional."""
