"""S-018: the verified query layer over a CapabilityStore (03_DESIGN §2.6).

Every row this module acts on is verified first: its signature, then that its manifest_digest and
identity recompute from its own content (REG-ROW-4). A row that fails is never served. `resolve`
also recomputes the chosen implementation's code digest and checks revocation by identity, and fails
closed (REG-QRY-5). A revocation client is required (REG-QRY-6).

Search and list are discovery: they leave out versions the client knows are revoked, and still answer
when revocation knowledge is stale. Resolve is the gate (02_DECISIONS R-D11).
"""

from __future__ import annotations

import base64
import copy
import hashlib
import json
import logging
from pathlib import Path

from .canon import canonical_bytes
from .digest import DigestError, manifest_digest, tree_digest
from .implementations import tool_identity
from .manifest import tool_effect
from .registry import KINDS, LABEL_ORDER, RegistryError
from .resolve import in_range
from .rows import RowError, verify_row
from .store.base import LABEL_RANK, StoreError, version_key, words

LOG = logging.getLogger("capability.store_registry")
MAX_TERMS = 16
DEFAULT_PAGE = 50
DEFAULT_SEARCH = 10


def _encode_cursor(value) -> str:
    return base64.urlsafe_b64encode(canonical_bytes(value)).decode("ascii").rstrip("=")


def _decode_cursor(cursor: str):
    try:
        padded = cursor + "=" * (-len(cursor) % 4)
        return json.loads(base64.urlsafe_b64decode(padded.encode("ascii")))
    except (ValueError, UnicodeError) as exc:
        raise RegistryError("InvalidRequest", "the cursor is not one this registry issued") from exc


def tool_definition(row: dict) -> dict:
    """The model-facing definition of a tool: what a model API takes as a tool."""
    manifest = row["manifest"]
    return {"name": row["name"], "description": manifest["summary"],
            "input_schema": copy.deepcopy(manifest["spec"]["inputSchema"])}


class StoreRegistry:
    def __init__(self, store, public_key, revocation, languages: dict, page_limit_max: int = 200,
                 search_limit_max: int = 50):
        if revocation is None:
            raise ValueError("a store-backed registry needs a revocation client; there is no unchecked mode")
        self._store, self._key, self._revocation = store, public_key, revocation
        self._languages = copy.deepcopy(languages)
        self.page_limit_max, self.search_limit_max = page_limit_max, search_limit_max

    @property
    def store(self):
        return self._store

    def languages(self) -> dict:
        return copy.deepcopy(self._languages)

    # -- verification --
    def _verified(self, row: dict) -> dict:
        try:
            verify_row(self._key, row)
        except RowError as exc:
            raise RegistryError("IntegrityError", str(exc)) from exc
        manifest = row["manifest"]
        if manifest.get("name") != row["name"] or manifest.get("version") != row["version"] \
                or manifest.get("kind") != row["kind"]:
            raise RegistryError("IntegrityError", "row fields disagree with its manifest")
        if manifest_digest(manifest) != row["manifest_digest"]:
            raise RegistryError("IntegrityError", "manifest_digest does not recompute")
        if row["kind"] == "tool":
            if tool_identity(manifest, row["implementations"]) != row["identity"]:
                raise RegistryError("IntegrityError", "identity does not recompute")
            declared = sorted(item["language"] for item in manifest.get("implementations", []))
            if declared != sorted(item["language"] for item in row["implementations"]):
                raise RegistryError("IntegrityError", "implementations disagree with the manifest")
        if row["label"] not in LABEL_RANK:
            raise RegistryError("IntegrityError", "unknown label")
        return row

    def _serveable(self, rows, visible, min_rank=0, language=None):
        """Verified rows the caller may see, at the label floor, not known to be revoked. A row that fails
        verification is left out and logged (03_DESIGN §5.3); it is never served."""
        out = []
        for row in rows:
            try:
                self._verified(row)
            except RegistryError:
                LOG.error("row seq %s failed verification and was not served", row.get("seq"))
                continue
            if LABEL_RANK[row["label"]] < min_rank:
                continue  # a tampered filter column let it through
            if language is not None and language not in [i["language"] for i in row["implementations"]]:
                continue
            ref = {"kind": row["kind"], "name": row["name"], "version": row["version"]}
            if visible is not None and not visible(ref):
                continue
            if self._known_revoked(row):
                continue
            out.append(row)
        return out

    def _known_revoked(self, row) -> bool:
        digest = row["identity"] if row["kind"] == "tool" else row["manifest_digest"]
        try:
            return self._revocation.check(row["kind"], row["name"], row["version"], digest).status == "revoked"
        except Exception:  # discovery never fails on a revocation fault; resolve does
            return False

    @staticmethod
    def _check(kind, min_label):
        if kind not in KINDS:
            raise RegistryError("NotFound", f"unknown kind {kind!r}")
        if min_label not in LABEL_ORDER:
            raise RegistryError("BelowLabel", f"unknown label {min_label!r}")
        return LABEL_RANK[min_label]

    def _limit(self, limit, default, maximum):
        if limit is None:
            return default
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= maximum:
            raise RegistryError("InvalidRequest", f"limit must be between 1 and {maximum}")
        return limit

    @staticmethod
    def summary_view(row: dict) -> dict:
        manifest = row["manifest"]
        return {"name": row["name"], "kind": row["kind"], "version": row["version"], "label": row["label"],
                "summary": manifest["summary"], "identity": row["identity"],
                "languages": sorted(i["language"] for i in row["implementations"]),
                "requires": list(manifest.get("requires", [])), "effect": tool_effect(manifest["spec"]),
                "tool_definition": tool_definition(row)}

    # -- queries --
    def list(self, kind="tool", min_label="Declared", cursor=None, limit=None, visible=None) -> dict:
        rank = self._check(kind, min_label)
        limit = self._limit(limit, DEFAULT_PAGE, self.page_limit_max)
        after = None
        if cursor is not None:
            value = _decode_cursor(cursor)
            if not (isinstance(value, list) and len(value) == 2 and all(isinstance(v, str) for v in value)):
                raise RegistryError("InvalidRequest", "the cursor is not one this registry issued")
            try:
                version_key(value[1])
            except ValueError as exc:
                raise RegistryError("InvalidRequest", "the cursor is not one this registry issued") from exc
            after = tuple(value)
        try:
            rows = self._store.rows_page(kind, after, limit, rank)
        except StoreError as exc:
            raise RegistryError("Unexpected", "store failure") from exc
        items = [self.summary_view(row) for row in self._serveable(rows, visible, rank)]
        nxt = _encode_cursor([rows[-1]["name"], rows[-1]["version"]]) if len(rows) == limit else None
        return {"items": items, "next": nxt}

    def search(self, query, kind="tool", min_label="Declared", language=None, limit=None, cursor=None,
               visible=None) -> dict:
        rank = self._check(kind, min_label)
        limit = self._limit(limit, DEFAULT_SEARCH, self.search_limit_max)
        if not isinstance(query, str):
            raise RegistryError("InvalidRequest", "query must be text")
        terms = words(query)[:MAX_TERMS]
        if language is not None and language not in self._languages:
            raise RegistryError("InvalidRequest", "unknown language")
        fingerprint = hashlib.sha256(canonical_bytes([terms, kind, rank, language])).hexdigest()[:16]
        offset = 0
        if cursor is not None:
            value = _decode_cursor(cursor)
            if not (isinstance(value, dict) and value.get("q") == fingerprint
                    and isinstance(value.get("o"), int) and value["o"] >= 0):
                raise RegistryError("InvalidRequest", "the cursor is not one this registry issued for this query")
            offset = value["o"]
        try:
            rows = self._store.search(terms, kind, limit, offset, rank, language)
        except StoreError as exc:
            raise RegistryError("Unexpected", "store failure") from exc
        items = [self.summary_view(row) for row in self._serveable(rows, visible, rank, language)]
        nxt = _encode_cursor({"q": fingerprint, "o": offset + limit}) if len(rows) == limit else None
        return {"items": items, "next": nxt}

    def versions(self, name, kind="tool", visible=None) -> list:
        self._check(kind, "Declared")
        rows = self._serveable(self._store.versions(kind, name), visible)
        if not rows:
            raise RegistryError("NotFound", f"no entry for {name}")
        return [{"name": r["name"], "version": r["version"], "label": r["label"], "identity": r["identity"]}
                for r in rows]

    def _row_for(self, name, version, kind, visible):
        if version is None:
            rows = self._store.versions(kind, name)
            row = rows[-1] if rows else None
        else:
            row = self._store.get_row(kind, name, version)
        if row is None:
            raise RegistryError("NotFound", f"no entry for {name}" + (f"@{version}" if version else ""))
        self._verified(row)
        if visible is not None and not visible({"kind": kind, "name": name, "version": row["version"]}):
            raise RegistryError("NotFound", f"no entry for {name}")
        return row

    def describe(self, name, version=None, kind="tool", visible=None) -> dict:
        self._check(kind, "Declared")
        row = self._row_for(name, version, kind, visible)
        view = self.summary_view(row)
        manifest = row["manifest"]
        spec = manifest["spec"]
        view.update({
            "manifest_digest": row["manifest_digest"], "owner": manifest["owner"], "evidence": list(row["evidence"]),
            "admitted_at": row["admitted_at"], "source_type": row["source"]["type"],
            "spec": {key: copy.deepcopy(spec[key]) for key in
                     ("inputSchema", "postconditions", "idempotent", "readOnly", "effect", "timeoutMs") if key in spec},
        })
        return view

    def resolve(self, name, range="*", kind="tool", minLabel="Declared", languages=None, visible=None) -> dict:
        """Choose the highest version in range at or above the label floor, verify it, check its code digest and
        revocation. Returns an inert match; never runs code."""
        rank = self._check(kind, minLabel)
        if kind != "tool":
            raise RegistryError("NoImplementation", "only tools resolve to code in S-018")
        rows = [row for row in self._store.versions(kind, name)]
        if visible is not None:
            rows = [r for r in rows if visible({"kind": kind, "name": name, "version": r["version"]})]
        if not rows:
            raise RegistryError("NotFound", f"no entry for {name}")
        for row in rows:
            self._verified(row)
        in_expression = [row for row in rows if in_range(row["version"], range)]
        if not in_expression:
            raise RegistryError("NotFound", f"no version of {name} satisfies {range!r}")
        at_floor = [row for row in in_expression if LABEL_RANK[row["label"]] >= rank]
        if not at_floor:
            raise RegistryError("BelowLabel", f"{name} has no version in range at {minLabel}")
        row = max(at_floor, key=lambda r: version_key(r["version"]))
        available = {item["language"]: item for item in row["implementations"]}
        chosen = next((lang for lang in (languages or []) if lang in available), None)
        if chosen is None:
            error = RegistryError("NoImplementation",
                                  f"{name}@{row['version']} has no implementation in {list(languages or [])}")
            error.available = sorted(available)
            raise error
        impl = available[chosen]
        folder = self._store.code_path(impl["code_digest"])
        if folder is None:
            raise RegistryError("IntegrityError", f"code for {name}@{row['version']} ({chosen}) is missing")
        try:
            if tree_digest(folder) != impl["code_digest"]:
                raise RegistryError("IntegrityError", f"code_digest mismatch for {name} ({chosen})")
        except DigestError as exc:
            raise RegistryError("IntegrityError", str(exc)) from exc
        entry = Path(impl["entry"])
        if entry.is_absolute() or ".." in entry.parts or not (folder / entry).is_file() or (folder / entry).is_symlink():
            raise RegistryError("IntegrityError", f"entry {impl['entry']} is missing or unsafe")
        result = self._revocation.check(kind, name, row["version"], row["identity"])
        if result.status == "revoked":
            error = RegistryError("Revoked", f"{name}@{row['version']} is revoked")
            error.entry, error.seq = result.entry, result.seq
            raise error
        if result.status == "stale":
            error = RegistryError("RevocationStale", "revocation knowledge is not fresh; failing closed")
            error.seq = result.seq
            raise error
        return copy.deepcopy({
            "name": name, "kind": kind, "version": row["version"], "label": row["label"],
            "manifest_digest": row["manifest_digest"], "identity": row["identity"], "language": chosen,
            "code_digest": impl["code_digest"], "entry": impl["entry"], "implementation_path": str(folder),
            "spec": row["manifest"]["spec"], "revocation": "checked", "revocation_seq": result.seq,
        })
