"""Index generation (S-001 §8; LANGUAGES.md v2; M5).

The index is the only input the registry reads. For each capability it records the
embedded normalised manifest and the digests. Tools carry implementations (one per
language, sorted), an identity, and pass the publication gate in versions.yaml.
Agents and models keep the source rules from M2 and M4.

A build either writes a complete index or writes nothing. An existing index is
removed first, so a failed build cannot leave a stale index that looks fresh.
"""

import hashlib
import os
from pathlib import Path

from .canon import canonical_bytes
from .digest import DigestError, manifest_digest, tree_digest, tree_digest_roots
from .implementations import check_implementations, sorted_manifest, tool_identity
from .labels import POLICY_VERSION, LabelError, PolicyError, check_policy, label_for, tool_label
from .languages import LanguageError, load_languages
from .ledger import check_ledger
from .manifest import KINDS, ManifestError, check_manifest_file
from .pins import PinError, archive_tree_digest

INDEX_DIR = "index"
INDEX_FILE = "index.json"
INPUT_FOLDERS = ("catalog", "schemas", "policy", "library", "evidence")
OPTIONAL_FOLDERS = ("library", "evidence")
SCHEMA_ID = "capability-index/1"


class BuildError(Exception):
    """The index could not be built. ``errors`` lists each problem; no index is written."""

    def __init__(self, errors):
        self.errors = sorted(set(errors))
        super().__init__("; ".join(self.errors))


def discover(root: Path) -> list[Path]:
    """Return catalog/<kind>/<name>/manifest.yaml for every capability folder."""
    manifests: list[Path] = []
    errors: list[str] = []
    catalog = root / "catalog"
    for kind in KINDS:
        kind_dir = catalog / kind
        if not kind_dir.is_dir():
            continue
        for folder in sorted(kind_dir.iterdir()):
            if not folder.is_dir():
                errors.append(f"{folder}: stray file under catalog/{kind}/ (expected a folder)")
                continue
            manifest = folder / "manifest.yaml"
            if manifest.is_file():
                manifests.append(manifest)
            else:
                errors.append(f"{folder}: no manifest.yaml (CAP-MAN-1)")
    if errors:
        raise BuildError(errors)
    return manifests


def _check_pin(manifest: dict, pin_repos: dict) -> str | None:
    """Return a problem with an external source pin (agents and models), or None when it checks out."""
    source = manifest["source"]
    clone = pin_repos.get(source["repository"])
    if clone is None:
        return f"no local clone supplied for pinned repository {source['repository']} (use --pin-repo)"
    try:
        computed = archive_tree_digest(clone, source["commit"], source["path"])
    except PinError as exc:
        return str(exc)
    if computed != source["tree_sha256"]:
        return (f"tree_sha256 mismatch for {source['path']} at {source['commit']}: "
                f"pinned {source['tree_sha256']}, computed {computed}")
    return None


def index_digest(index: dict) -> str:
    """SHA-256 of the canonical form of ``index`` with ``index_digest`` removed."""
    body = {key: value for key, value in index.items() if key != "index_digest"}
    return "sha256:" + hashlib.sha256(canonical_bytes(body)).hexdigest()


def _write_atomic(path: Path, data: bytes) -> None:
    temp = path.with_name(path.name + ".tmp")
    temp.write_bytes(data)
    os.replace(temp, path)


def _tool_entry(root: Path, path: Path, manifest: dict, languages: dict, fixture_mode: bool) -> tuple[dict, list[str]]:
    implementations, errors = check_implementations(manifest, root, languages)
    if errors:
        return {}, [f"{path}: {error}" for error in errors]
    ordered = sorted_manifest(manifest)
    identity = tool_identity(manifest, implementations)
    if not fixture_mode:
        problem = check_ledger(root, manifest["name"], manifest["version"], identity)
        if problem:
            return {}, [f"{path}: {problem}"]
    try:
        label, evidence = tool_label(manifest, root, implementations, languages)
    except LabelError as exc:
        return {}, [f"{path}: {exc}"]
    entry = {
        "name": manifest["name"],
        "kind": "tool",
        "version": manifest["version"],
        "manifest_digest": manifest_digest(ordered),
        "identity": identity,
        "implementations": [{"language": item["language"], "code_digest": item["code_digest"],
                             "entry": languages[item["language"]]["entry"]}
                            for item in implementations],
        "label": label,
        "evidence": evidence,
        "manifest": ordered,
    }
    return entry, []


def _other_entry(root: Path, path: Path, manifest: dict, pin_repos: dict) -> tuple[dict, list[str]]:
    code_digest = None
    source = manifest["source"]
    if source["type"] in ("library", "generated"):
        try:
            code_digest = tree_digest(root / source["location"])
        except DigestError as exc:
            return {}, [f"{path}: {exc}"]
    elif source["type"] == "external":
        problem = _check_pin(manifest, pin_repos)
        if problem:
            return {}, [f"{path}: {problem}"]
    return {
        "name": manifest["name"],
        "kind": manifest["kind"],
        "version": manifest["version"],
        "manifest_digest": manifest_digest(manifest),
        "code_digest": code_digest,
        "label": label_for(manifest, root),
        "source": source,
        "evidence": [],
        "manifest": manifest,
    }, []


def build_index(root: Path | str, pin_repos: dict | None = None, fixture_mode: bool = False) -> dict:
    """Build the index for the capability tree under ``root``. Raises BuildError on any problem.

    ``fixture_mode`` skips the publication gate (versions.yaml). It exists for mechanism
    tests on fixtures and is never used to publish the catalog.
    """
    root = Path(root)
    index_path = root / INDEX_DIR / INDEX_FILE
    if index_path.exists():
        index_path.unlink()

    try:
        check_policy(root)
        languages = load_languages(root)
        manifests = discover(root)
    except (PolicyError, LanguageError) as exc:
        raise BuildError([str(exc)]) from exc
    except BuildError:
        raise

    errors: list[str] = []
    entries: list[dict] = []
    for path in manifests:
        try:
            manifest = check_manifest_file(path)
        except ManifestError as exc:
            errors.extend(exc.errors)
            continue
        if manifest["name"] != path.parent.name or manifest["kind"] != path.parent.parent.name:
            errors.append(f"{path}: name and kind must match the folder path")
            continue
        if manifest["kind"] == "tool":
            entry, problems = _tool_entry(root, path, manifest, languages, fixture_mode)
        else:
            entry, problems = _other_entry(root, path, manifest, pin_repos or {})
        errors.extend(problems)
        if entry:
            entries.append(entry)

    if errors:
        raise BuildError(errors)

    try:
        inputs = tree_digest_roots(root, INPUT_FOLDERS, exclude=(INDEX_DIR,), optional=OPTIONAL_FOLDERS)
    except DigestError as exc:
        raise BuildError([str(exc)]) from exc

    entries.sort(key=lambda entry: (entry["kind"], entry["name"]))
    index = {
        "schema": SCHEMA_ID,
        "policy_version": POLICY_VERSION,
        "inputs_digest": inputs,
        "entries": entries,
    }
    index["index_digest"] = index_digest(index)

    (root / INDEX_DIR).mkdir(exist_ok=True)
    _write_atomic(index_path, canonical_bytes(index))
    return index
