"""External source pins (S-001 §5, §6; M4).

An external capability is pinned by repository, commit and path, plus a
``tree_sha256`` that identifies the content at that commit. The digest is taken
from the commit itself, with ``git archive``, so the working tree and the
checkout state do not matter. Nothing is checked out, and a link in the archive
is refused.
"""

import argparse
import io
import subprocess
import sys
import tarfile
import tempfile
from pathlib import Path

from .digest import DigestError, tree_digest


class PinError(Exception):
    """A pin could not be read from its repository at its commit."""


def archive_tree_digest(repo: Path | str, commit: str, path: str) -> str:
    """Return the sha256-tree-v1 digest of ``path`` as it is at ``commit`` in the clone ``repo``."""
    repo = Path(repo)
    if not (repo / ".git").exists() and not (repo / "HEAD").exists():
        raise PinError(f"not a git clone: {repo}")
    result = subprocess.run(
        ["git", "-c", f"safe.directory={repo.resolve()}", "-C", str(repo),
         "archive", "--format=tar", commit, "--", path],
        capture_output=True,
        check=False,
    )
    if result.returncode != 0:
        detail = result.stderr.decode("utf-8", "replace").strip()
        raise PinError(f"git archive failed for {commit}:{path}: {detail}")

    with tempfile.TemporaryDirectory() as tmp:
        with tarfile.open(fileobj=io.BytesIO(result.stdout), mode="r:") as archive:
            for member in archive.getmembers():
                if member.issym() or member.islnk():
                    raise PinError(f"link refused in {commit}:{path}: {member.name}")
                if not (member.isfile() or member.isdir()):
                    raise PinError(f"non-regular entry refused in {commit}:{path}: {member.name}")
            archive.extractall(tmp, filter="data")
        target = Path(tmp) / path
        if not target.exists():
            raise PinError(f"{path} is not present at {commit}")
        try:
            return tree_digest(target)
        except DigestError as exc:
            raise PinError(str(exc)) from exc


def main(argv=None) -> int:
    """Print the tree digest for one pin, so a manifest can record it."""
    parser = argparse.ArgumentParser(prog="capability-pin")
    parser.add_argument("repo", help="local clone of the pinned repository")
    parser.add_argument("commit", help="full commit SHA")
    parser.add_argument("path", help="file or directory inside the repository")
    args = parser.parse_args(argv)
    try:
        print(archive_tree_digest(args.repo, args.commit, args.path))
    except PinError as exc:
        print(f"capability-pin: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
