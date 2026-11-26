"""S-002 M4: entry checks, per-resolve digests, and the Match (SPEC §5, §6.2 steps 6 to 9).

Nothing here runs implementation code. Entry checks look at the filesystem; digests are
recomputed on every call (no cache, CAP-REG-7).
"""

from __future__ import annotations

from pathlib import Path

from .canon import canonical_bytes
from .digest import DigestError, manifest_digest, tree_digest
from .implementations import sorted_manifest
from .registry import RegistryError


def _entry_error(reason: str, message: str) -> RegistryError:
    error = RegistryError("EntryError", message)
    error.reason = reason
    return error


def check_entry(root: Path, folder: Path, entry_name: str) -> Path:
    """Return the entry path if it exists, is a regular file, and sits inside its folder.

    Checked before any digest: a missing entry is Missing; a link in the folder path or on the
    entry is Link; a resolved entry outside the resolved folder is Escape.
    """
    folder_rel = folder.relative_to(root) if folder.is_relative_to(root) else None
    walk = root
    for part in (folder_rel.parts if folder_rel else ()):
        walk = walk / part
        if walk.is_symlink():
            raise _entry_error("Link", f"{walk} is a link")
    entry = folder / entry_name
    if entry.is_symlink():
        raise _entry_error("Link", f"entry {entry} is a link (checked before existence, so a dangling link is Link)")
    if not folder.is_dir():
        raise _entry_error("Missing", f"implementation folder {folder} is missing")
    if not entry.exists():
        raise _entry_error("Missing", f"entry {entry_name} is missing from {folder}")
    if not entry.is_file():
        raise _entry_error("Missing", f"entry {entry} is not a regular file")
    if folder.resolve() not in entry.resolve().parents:
        raise _entry_error("Escape", f"entry {entry} resolves outside {folder}")
    return entry


def verify_row(root: Path, row: dict) -> dict:
    """Entry checks, then digests, then the Match for one resolved row (steps 6 to 9).

    Order (SPEC §6.2): source and entry first, then manifest_digest, then code_digest.
    The entry filename comes from the verified index, not from the mutable language policy.
    """
    if row["kind"] != "tool":
        if row["source"]["type"] == "external":
            raise RegistryError("SourceUnavailable", f"{row['name']}@{row['version']} has an external source")
        manifest = _check_manifest(row, None)
        code_digest = None
        if row["source"]["type"] in ("library", "generated"):
            folder = root / row["source"]["location"]
            try:
                code_digest = tree_digest(folder)
            except DigestError as exc:
                raise RegistryError("IntegrityError", str(exc)) from exc
            if code_digest != row["code_digest"]:
                raise RegistryError("IntegrityError", f"code_digest mismatch for {row['name']}")
        return {
            "name": row["name"], "kind": row["kind"], "version": row["version"], "label": row["label"],
            "manifest_digest": manifest["manifest_digest"], "code_digest": code_digest,
            "revocation": "unchecked",
        }

    chosen = row["_chosen"]
    folder = root / chosen["location"]
    entry = check_entry(root, folder, chosen["entry"])
    manifest = _check_manifest(row, None)
    try:
        code_digest = tree_digest(folder)
    except DigestError as exc:
        raise RegistryError("IntegrityError", str(exc)) from exc
    if code_digest != chosen["code_digest"]:
        raise RegistryError("IntegrityError",
                            f"code_digest mismatch for {row['name']} ({chosen['language']})")
    return {
        "name": row["name"], "kind": "tool", "version": row["version"],
        "manifest_digest": manifest["manifest_digest"],
        "language": chosen["language"],
        "implementation_location": chosen["location"],
        "entry_path": str(entry.relative_to(root)),
        "code_digest": code_digest,
        "label": row["label"],
        "spec": row["manifest"]["spec"],
        "revocation": "unchecked",
    }


def _check_manifest(row: dict, match: dict | None) -> dict:
    """Recompute manifest_digest from the embedded manifest (CAP-REG-6). Returns the match when given."""
    recomputed = manifest_digest(sorted_manifest(row["manifest"]))
    if recomputed != row["manifest_digest"]:
        raise RegistryError("IntegrityError",
                            f"manifest_digest mismatch for {row['name']}@{row['version']} (checked before code)")
    return match or {"manifest_digest": recomputed}
