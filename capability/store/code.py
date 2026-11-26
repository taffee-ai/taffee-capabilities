"""S-018: code folders named by their tree digest (02_DECISIONS R-D05).

A tree is written to a temporary folder, digested with sha256-tree-v1, and renamed to
<root>/<hex>/. An existing folder of that name is kept and the new copy dropped. Folders are
world-readable, so the container sandbox's unprivileged user can read the code it runs.
"""

import os
import shutil
import tempfile
from pathlib import Path, PurePosixPath

from ..digest import ALGORITHM, tree_digest
from .base import StoreError


def check_relative(name: object) -> PurePosixPath:
    """A safe relative path inside a tree, or StoreError."""
    if not isinstance(name, str) or not name or "\\" in name or "\x00" in name:
        raise StoreError(f"bad path {name!r}")
    path = PurePosixPath(name)
    if path.is_absolute() or any(part in ("", ".", "..") for part in path.parts) or name.endswith("/"):
        raise StoreError(f"path {name!r} must be relative and stay inside its folder")
    return path


class CodeArea:
    def __init__(self, root: Path | str, create: bool = True):
        self.root = Path(root)
        if create:  # a read-only store only looks at what is there
            self.root.mkdir(parents=True, exist_ok=True)
            os.chmod(self.root, 0o755)

    def put(self, files: dict[str, bytes]) -> str:
        if not isinstance(files, dict) or not files:
            raise StoreError("a code tree needs at least one file")
        temp = Path(tempfile.mkdtemp(prefix=".incoming-", dir=self.root))
        try:
            for name, data in files.items():
                relative = check_relative(name)
                if not isinstance(data, bytes):
                    raise StoreError(f"{name}: content must be bytes")
                target = temp.joinpath(*relative.parts)
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(data)
            for path in (temp, *temp.rglob("*")):
                os.chmod(path, 0o755 if path.is_dir() else 0o644)
            digest = tree_digest(temp)
            final = self.root / digest.split(":", 1)[1]
            try:
                os.rename(temp, final)
            except OSError:
                if not final.is_dir():
                    raise
                shutil.rmtree(temp)  # the same tree is already stored
            return digest
        except BaseException:
            shutil.rmtree(temp, ignore_errors=True)
            raise

    def remove_incoming(self) -> int:
        """Remove `.incoming-*` folders a killed writer left behind. Call only when no `put` can be running (at
        the start of the one process that writes this store). Returns how many were removed."""
        stale = [child for child in self.root.glob(".incoming-*") if child.is_dir()]
        for child in stale:
            shutil.rmtree(child, ignore_errors=True)
        return len(stale)

    def path(self, code_digest: str) -> Path | None:
        if not isinstance(code_digest, str) or not code_digest.startswith("sha256:"):
            return None
        hexpart = code_digest.split(":", 1)[1]
        if len(hexpart) != 64 or any(c not in "0123456789abcdef" for c in hexpart):
            return None
        folder = self.root / hexpart
        return folder if folder.is_dir() else None


__all__ = ["ALGORITHM", "CodeArea", "check_relative"]
