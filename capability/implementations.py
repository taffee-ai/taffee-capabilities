"""Tool implementations and identity (S-001 LANGUAGES.md v2; M5).

A tool has one implementation per language, each in its own folder under library/.
Its identity is the manifest digest together with the sorted set of
(language, code_digest) pairs. Sorting makes the order in the manifest file irrelevant.
"""

import copy
import hashlib
from pathlib import Path

from .canon import canonical_bytes
from .digest import DigestError, manifest_digest, tree_digest


class ImplementationError(Exception):
    """A tool's implementations are invalid. ``errors`` lists each problem."""

    def __init__(self, errors):
        self.errors = list(errors)
        super().__init__("; ".join(self.errors))


def sorted_implementations(manifest: dict) -> list[dict]:
    """The manifest's implementations, sorted by language. Unsorted input is not an error."""
    return sorted(manifest.get("implementations", []), key=lambda item: item["language"])


def sorted_manifest(manifest: dict) -> dict:
    """A copy of the manifest with implementations sorted, for digesting and embedding."""
    copied = copy.deepcopy(manifest)
    if "implementations" in copied:
        copied["implementations"] = sorted_implementations(copied)
    return copied


def check_implementations(manifest: dict, root: Path, languages: dict) -> tuple[list[dict], list[str]]:
    """Return (implementations with code digests, sorted by language) and any problems."""
    errors: list[str] = []
    seen: set[str] = set()
    resolved: list[dict] = []
    for item in manifest.get("implementations", []):
        language = item.get("language")
        location = item.get("location", "")
        if language not in languages:
            errors.append(f"implementations: unknown language {language!r}")
            continue
        if language in seen:
            errors.append(f"implementations: duplicate language {language!r}")
            continue
        seen.add(language)
        if not location.startswith("library/") or ".." in Path(location).parts or Path(location).is_absolute():
            errors.append(f"implementations[{language}]: location must be a relative path under library/")
            continue
        folder = root / location
        entry = folder / languages[language]["entry"]
        if not folder.is_dir():
            errors.append(f"implementations[{language}]: folder {location} is missing")
            continue
        if not entry.is_file() or entry.is_symlink():
            errors.append(f"implementations[{language}]: entry {languages[language]['entry']} is missing or a link")
            continue
        try:
            code = tree_digest(folder)
        except DigestError as exc:
            errors.append(f"implementations[{language}]: {exc}")
            continue
        resolved.append({"language": language, "location": location, "code_digest": code})
    resolved.sort(key=lambda item: item["language"])
    return resolved, errors


def tool_identity(manifest: dict, implementations: list[dict]) -> str:
    """The identity of a tool version: manifest digest plus the sorted set of implementation digests."""
    pairs = [{"language": item["language"], "code_digest": item["code_digest"]}
              for item in sorted(implementations, key=lambda item: item["language"])]
    body = {"manifest_digest": manifest_digest(sorted_manifest(manifest)), "implementations": pairs}
    return "sha256:" + hashlib.sha256(canonical_bytes(body)).hexdigest()


def main(argv=None) -> int:
    """Print a tool's identity, so a versions.yaml entry can be written by hand."""
    import argparse
    import sys

    from .languages import load_languages
    from .manifest import check_manifest_file

    parser = argparse.ArgumentParser(prog="capability-identity")
    parser.add_argument("root", help="repository root")
    parser.add_argument("manifest", help="path to a tool manifest.yaml under catalog/tool/")
    args = parser.parse_args(argv)
    root = Path(args.root)
    manifest = check_manifest_file(args.manifest)
    implementations, errors = check_implementations(manifest, root, load_languages(root))
    if errors:
        for error in errors:
            print(error, file=sys.stderr)
        return 1
    print(tool_identity(manifest, implementations))
    return 0


if __name__ == "__main__":
    import sys

    sys.exit(main())
