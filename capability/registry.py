"""Registry, milestone M1 (S-002 SPEC §4, §6.1; CAP-REG-1, -2, -3, -8, -10, -11).

Reads only the S-001 index for list and describe. resolve also checks the chosen
implementation's entry and recomputes its digests (see match.py). The index is verified
whenever it changes: a tampered or non-canonical index fails list, describe and resolve alike.
Nothing here runs implementation code.

Index cache. Verifying the whole index costs time in proportion to its size, so a Registry
keeps the last verified index in memory and re-verifies only when the file changes. A change
is detected from the file's inode, size, mtime and ctime. ctime is set by the kernel and
cannot be set back with utime, so rewriting the file in place or replacing it (os.replace,
as the index builder does) always causes a full re-verification. A failed verification is
never cached. Callers get copies, so nothing a caller does can alter the cached index.
`Registry(root, cache=False)` verifies on every call, as before.
"""

from __future__ import annotations

import copy
import json
import os
import threading
from pathlib import Path

from .canon import canonical_bytes
from .index import INDEX_DIR, INDEX_FILE, index_digest
from .manifest import SCHEMA_DIR

LABEL_ORDER = ("Declared", "Validated", "Tested", "Trusted", "Approved")
KINDS = ("tool", "agent", "model")


class RegistryError(Exception):
    """A typed registry failure. ``code`` is one of the S-002 error codes."""

    def __init__(self, code: str, message: str):
        self.code = code
        super().__init__(f"{code}: {message}")


def _version_key(version: str) -> tuple[int, int, int]:
    major, minor, patch = (int(part) for part in version.split("."))
    return major, minor, patch


def load_index(root: Path | str) -> dict:
    """Read and verify the index. Any problem is IndexError: the registry fails closed.

    The index is checked in order: it parses, it is a JSON object, it matches the
    S-001 index schema, its index_digest verifies, and it is in canonical form.
    """
    path = Path(root) / INDEX_DIR / INDEX_FILE
    try:
        raw = path.read_bytes()
    except OSError as exc:
        raise RegistryError("IndexError", f"index unreadable: {exc}") from exc
    try:
        data = json.loads(raw)
    except ValueError as exc:
        raise RegistryError("IndexError", f"index is not JSON: {exc}") from exc
    if not isinstance(data, dict):
        raise RegistryError("IndexError", "index must be a JSON object")
    problems = sorted(_index_validator().iter_errors(data), key=lambda e: list(e.absolute_path))
    if problems:
        first = problems[0]
        where = ".".join(str(part) for part in first.absolute_path) or "<root>"
        raise RegistryError("IndexError", f"index does not match the schema at {where}: {first.message}")
    if data["index_digest"] != index_digest(data):
        raise RegistryError("IndexError", "index_digest does not verify")
    if canonical_bytes(data) != raw:
        raise RegistryError("IndexError", "index is not in canonical form")
    return data


def _index_validator():
    """The bundled S-001 index schema. A missing or unreadable schema is SetupError, not IndexError."""
    from jsonschema import Draft202012Validator

    path = SCHEMA_DIR / "index.schema.json"
    try:
        schema = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise RegistryError("SetupError", f"bundled index schema unavailable at {path}: {exc}") from exc
    try:
        Draft202012Validator.check_schema(schema)
    except Exception as exc:  # jsonschema raises SchemaError; any failure is a setup fault
        raise RegistryError("SetupError", f"bundled index schema is invalid: {exc}") from exc
    return Draft202012Validator(schema)


class Registry:
    """list and describe over a verified index. Every call re-reads and re-verifies the index."""

    def __init__(self, root: Path | str, revocation=None, cache: bool = True):
        """``revocation`` is an optional S-003 RevocationClient.

        Without one, resolve reports ``revocation: "unchecked"`` as in S-002. With one,
        every resolve is checked and fails closed (SPEC S-003 CAP-REV-10).
        ``cache`` keeps the last verified index until the file changes (see the module docstring).
        """
        self._root = Path(root)
        self._revocation = revocation
        self._cache_enabled = cache
        self._cache_key = None
        self._cache_index = None
        self._cache_lock = threading.Lock()

    @property
    def root(self) -> Path:
        """The repository root this registry reads (read-only; the executor mounts implementations from it)."""
        return self._root

    def _index_key(self):
        """What identifies the index file's current content, or None when it cannot be read (then nothing is cached)."""
        try:
            st = os.stat(self._root / INDEX_DIR / INDEX_FILE)
        except OSError:
            return None
        return (st.st_dev, st.st_ino, st.st_size, st.st_mtime_ns, st.st_ctime_ns)

    def _shared_index(self) -> dict:
        """The verified index, shared with the cache. Internal: never hand this object to a caller."""
        if not self._cache_enabled:
            return load_index(self._root)
        with self._cache_lock:
            key = self._index_key()
            if key is not None and key == self._cache_key:
                return self._cache_index
            index = load_index(self._root)  # raises on any problem, leaving nothing cached
            # Cache only if the file did not change while it was being read and verified.
            if key is not None and self._index_key() == key:
                self._cache_key, self._cache_index = key, index
            else:
                self._cache_key, self._cache_index = None, None
            return index

    def verified_index(self) -> dict:
        """The index, read and verified once (S-008 builds every response from one such read). A copy."""
        return copy.deepcopy(self._shared_index())

    def _entries(self, visible=None) -> list[dict]:
        """The verified entries. ``visible`` (S-008) is an optional predicate; entries it rejects take no part in
        any later step, so a hidden entry cannot change an Ambiguous, BelowLabel or version choice.
        Internal: the entries are shared with the cache, so callers here copy what they return."""
        entries = self._shared_index()["entries"]
        return entries if visible is None else [entry for entry in entries if visible(entry)]

    def list(self, kind: str | None = None, minLabel: str = "Declared", visible=None) -> list[dict]:
        """Entries at or above minLabel, optionally of one kind, in index order.

        The index is verified first, so a corrupt index is IndexError whatever the options.
        """
        entries = self._entries(visible)
        if minLabel not in LABEL_ORDER:
            raise RegistryError("BelowLabel", f"unknown label {minLabel!r}")
        if kind is not None and kind not in KINDS:
            raise RegistryError("NotFound", f"unknown kind {kind!r}")
        floor = LABEL_ORDER.index(minLabel)
        selected = []
        for entry in entries:
            if kind is not None and entry["kind"] != kind:
                continue
            if LABEL_ORDER.index(entry["label"]) < floor:
                continue
            selected.append({
                "name": entry["name"],
                "kind": entry["kind"],
                "version": entry["version"],
                "label": entry["label"],
                "manifest_digest": entry["manifest_digest"],
                "identity": entry.get("identity"),
                "languages": [item["language"] for item in entry.get("implementations", [])],
            })
        return selected

    def _select(self, name: str, range_expression: str = "*", kind: str | None = None,
                min_label: str = "Declared", languages: list[str] | None = None) -> dict:
        """Internal (M3): steps 1 to 5 of SPEC §6.2. Returns the candidate row and the chosen implementation."""
        from .resolve import candidate, select_implementation

        row = candidate(self._entries(), name, range_expression, kind, min_label)
        return copy.deepcopy({"row": row, "implementation": select_implementation(row, list(languages or []))})

    def resolve(self, name: str, range: str = "*", kind: str | None = None,
                minLabel: str = "Declared", languages: list[str] | None = None, visible=None) -> dict:
        """Steps 1 to 9 of SPEC §6.2. Returns the inert Match; never runs code (CAP-REG-10)."""
        from .match import verify_row
        from .resolve import select_implementation

        row = dict(self._candidate(name, range, kind, minLabel, visible))
        chosen = select_implementation(row, list(languages or []))
        if chosen is not None:
            row["_chosen"] = chosen
        match = copy.deepcopy(verify_row(self._root, row))  # the match carries the cached spec; give out a copy
        if self._revocation is None:
            return match
        return self._check_revocation(row, match)

    def _check_revocation(self, row: dict, match: dict) -> dict:
        """The last step of resolve: the revocation check, after the digests verify.

        A tool presents its identity (manifest digest with its implementation set, from the
        verified index). An agent or model presents its declaration digest. A revoked
        target is ``Revoked``; a client that is not fresh is ``RevocationStale``.
        """
        from .revocation import RevocationError

        digest = row["identity"] if row["kind"] == "tool" else row["manifest_digest"]
        try:
            result = self._revocation.check(row["kind"], row["name"], row["version"], digest)
        except RevocationError as exc:
            raise RegistryError("IntegrityError", f"index row is not a valid revocation check: {exc.message}") from exc
        if result.status == "revoked":
            error = RegistryError("Revoked", f"{row['name']}@{row['version']} is revoked (feed seq {result.entry['seq']})")
            error.entry = result.entry
            error.seq = result.seq
            raise error
        if result.status == "stale":
            error = RegistryError("RevocationStale", "revocation knowledge is not fresh; failing closed")
            error.seq = result.seq
            raise error
        return {**match, "revocation": "checked", "revocation_seq": result.seq}

    def _candidate(self, name: str, range_expression: str = "*", kind: str | None = None,
                   min_label: str = "Declared", visible=None) -> dict:
        """Internal (M2): steps 1 to 4 of SPEC §6.2. Not a production result; no Match yet."""
        from .resolve import candidate

        return candidate(self._entries(visible), name, range_expression, kind, min_label)

    def describe(self, name: str, version: str | None = None, kind: str | None = None, visible=None) -> dict:
        """The embedded manifest and identities for one version (the highest when version is omitted).

        A name used by more than one kind is Ambiguous unless ``kind`` is given. The registry
        never picks a kind silently.
        """
        rows = [entry for entry in self._entries(visible) if entry["name"] == name]
        if kind is not None:
            rows = [entry for entry in rows if entry["kind"] == kind]
        if version is not None:
            rows = [entry for entry in rows if entry["version"] == version]
        if not rows:
            raise RegistryError("NotFound", f"no entry for {name}" + (f"@{version}" if version else ""))
        kinds = {entry["kind"] for entry in rows}
        if len(kinds) > 1:
            raise RegistryError("Ambiguous", f"{name} is used by kinds {sorted(kinds)}; pass kind=")
        chosen = copy.deepcopy(max(rows, key=lambda entry: _version_key(entry["version"])))
        return {
            "name": chosen["name"],
            "kind": chosen["kind"],
            "version": chosen["version"],
            "label": chosen["label"],
            "manifest_digest": chosen["manifest_digest"],
            "code_digest": chosen.get("code_digest"),
            "identity": chosen.get("identity"),
            "implementations": chosen.get("implementations", []),
            "manifest": chosen["manifest"],
        }
