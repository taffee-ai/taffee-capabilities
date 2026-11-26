"""Digests for the capabilities pillar (S-001 §6).

Two identities, computed over normalised content:

- ``manifest_digest``: SHA-256 of the canonical JSON of a parsed manifest.
  YAML formatting and comments do not change it.
- ``sha256-tree-v1``: the digest of a file tree, used for ``code_digest`` (a
  library folder) and ``tree_sha256`` (a pinned external path).

The tree algorithm refuses symlinks and non-regular files. It never follows a
link, so a digest cannot read outside the tree it names.
"""

import hashlib
import os
from pathlib import Path

from .canon import canonical_bytes

ALGORITHM = "sha256-tree-v1"
TEXT_PROBE_BYTES = 8000
EXCLUDED_FILE_NAMES = frozenset({".DS_Store"})
EXCLUDED_DIR_NAMES = frozenset({"__pycache__"})


class DigestError(Exception):
    """Raised when a tree cannot be digested as specified."""


def _sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def manifest_digest(manifest: object) -> str:
    """Return ``sha256:<hex>`` over the canonical JSON of a parsed manifest."""
    return "sha256:" + _sha256_hex(canonical_bytes(manifest))


def _is_text(raw: bytes) -> bool:
    return b"\x00" not in raw[:TEXT_PROBE_BYTES]


def _normalised(raw: bytes) -> bytes:
    """Text is hashed with CRLF turned into LF; binary is hashed unchanged."""
    if not _is_text(raw):
        return raw
    return raw.replace(b"\r\n", b"\n")


def _refuse(path: Path) -> DigestError:
    return DigestError(f"refused (symlink or non-regular file): {path}")


def _collect(root: Path) -> list[tuple[str, Path]]:
    """Return (relative posix path, file path) pairs for every hashed file."""
    if root.is_symlink():
        raise _refuse(root)
    if root.is_file():
        return [(root.name, root)]
    if not root.is_dir():
        raise DigestError(f"not a file or directory: {root}")

    collected: list[tuple[str, Path]] = []
    for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
        current = Path(dirpath)
        for name in list(dirnames):
            child = current / name
            if child.is_symlink():
                raise _refuse(child)
            if name in EXCLUDED_DIR_NAMES:
                dirnames.remove(name)
        for name in filenames:
            child = current / name
            # Type and link checks come first, so an excluded name cannot hide a symlink.
            if child.is_symlink() or not child.is_file():
                raise _refuse(child)
            if name in EXCLUDED_FILE_NAMES:
                continue
            collected.append((child.relative_to(root).as_posix(), child))
    return collected


def tree_digest(root: Path | str) -> str:
    """Return the ``sha256-tree-v1`` digest of a file tree or a single file."""
    root = Path(root)
    entries = sorted(_collect(root), key=lambda entry: entry[0].encode("utf-8"))
    listing = bytearray()
    for relative, path in entries:
        content_hash = _sha256_hex(_normalised(path.read_bytes()))
        listing += relative.encode("utf-8") + b"\x00" + content_hash.encode("ascii") + b"\n"
    return "sha256:" + _sha256_hex(bytes(listing))


def tree_digest_roots(root: Path | str, names, exclude=(), optional=()) -> str:
    """Return the sha256-tree-v1 digest of several top-level folders under ``root``.

    Each file is listed as ``<folder>/<relative path>``, so the digest covers which
    folder holds a file as well as its content. ``exclude`` names folders to skip.
    A folder named in ``optional`` may be absent; it then contributes nothing, the same
    as an empty folder. A clean Git clone cannot hold an empty folder, so this keeps the
    digest the same either way.
    """
    root = Path(root)
    collected: list[tuple[str, Path]] = []
    for name in names:
        if name in exclude:
            continue
        base = root / name
        if not base.is_dir():
            if name in optional and not base.exists():
                continue
            raise DigestError(f"input folder missing: {base}")
        for relative, path in _collect(base):
            collected.append((f"{name}/{relative}", path))
    collected.sort(key=lambda entry: entry[0].encode("utf-8"))
    listing = bytearray()
    for relative, path in collected:
        content_hash = _sha256_hex(_normalised(path.read_bytes()))
        listing += relative.encode("utf-8") + b"\x00" + content_hash.encode("ascii") + b"\n"
    return "sha256:" + _sha256_hex(bytes(listing))
