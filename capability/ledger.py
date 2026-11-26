"""Publication ledger (S-001 LANGUAGES.md v2 §4; M5).

catalog/tool/<name>/versions.yaml records each published version and its identity.
It is hand-written. The validator checks it. A publishable index needs an entry for
every tool version: a missing entry is refused, and so is a changed identity at a
version that is already recorded.
"""

from pathlib import Path

import yaml


class LedgerError(Exception):
    """The publication ledger does not allow this tool version to be published."""


def load_ledger(root: Path, name: str) -> dict[str, str] | None:
    """Return {version: identity} from the tool's versions.yaml, or None when there is no file."""
    path = Path(root) / "catalog" / "tool" / name / "versions.yaml"
    if not path.is_file():
        return None
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    entries = data.get("versions", [])
    return {str(entry["version"]): entry["identity"] for entry in entries}


def check_ledger(root: Path, name: str, version: str, identity: str) -> str | None:
    """Return a problem with publishing name@version with this identity, or None when it may be published."""
    ledger = load_ledger(root, name)
    if ledger is None or version not in ledger:
        return (f"no versions.yaml entry for {name}@{version} (publication gate); "
                f"add identity {identity}")
    if ledger[version] != identity:
        return (f"identity changed at published version {name}@{version}: "
                f"recorded {ledger[version]}, now {identity}; bump the version")
    return None
