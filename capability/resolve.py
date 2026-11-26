"""Resolution, milestone M2 (S-002 SPEC §6.2 steps 1 to 4).

Internal. The candidate is the verified index row for the chosen version. It is a
private object, not a production result, and it is never a Match. A Match exists
only from M4. Steps 5 to 9 are added later.
"""

from __future__ import annotations

import re

from .registry import LABEL_ORDER, KINDS, RegistryError, _version_key


STRICT_SEMVER = re.compile(r"^(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)$")


def parse_range(expression: str) -> tuple[tuple[int, int, int], tuple[int, int, int] | None]:
    """Return (lower inclusive, upper exclusive or None) for '*', 'X.Y.Z', or '^X.Y.Z'.

    The grammar is strict: canonical semver only (no leading zeros, no signs). Anything
    else raises ValueError, which the caller maps to InvalidRange.
    """
    if expression in ("", "*"):
        return (0, 0, 0), None
    if expression.startswith("^"):
        if not STRICT_SEMVER.match(expression[1:]):
            raise ValueError(f"caret range needs canonical X.Y.Z: {expression!r}")
        lower = _version_key(expression[1:])
        major, minor, patch = lower
        if major > 0:
            upper = (major + 1, 0, 0)
        elif minor > 0:
            upper = (0, minor + 1, 0)
        else:
            upper = (0, 0, patch + 1)
        return lower, upper
    if not STRICT_SEMVER.match(expression):
        raise ValueError(f"exact range needs canonical X.Y.Z: {expression!r}")
    exact = _version_key(expression)
    return exact, (exact[0], exact[1], exact[2] + 1)


def in_range(version: str, expression: str) -> bool:
    try:
        lower, upper = parse_range(expression)
    except (ValueError, TypeError) as exc:
        raise RegistryError("InvalidRange", f"not a range this registry supports: {expression!r}") from exc
    key = _version_key(version)
    return key >= lower and (upper is None or key < upper)


def candidate(entries: list[dict], name: str, range_expression: str = "*", kind: str | None = None,
              min_label: str = "Declared") -> dict:
    """Steps 1 to 4 of SPEC §6.2 over already-verified index entries. Returns the verified row."""
    if min_label not in LABEL_ORDER:
        raise RegistryError("BelowLabel", f"unknown label {min_label!r}")
    if kind is not None and kind not in KINDS:
        raise RegistryError("NotFound", f"unknown kind {kind!r}")
    rows = [entry for entry in entries if entry["name"] == name]
    if kind is not None:
        rows = [entry for entry in rows if entry["kind"] == kind]
    if not rows:
        raise RegistryError("NotFound", f"no entry for {name}")
    kinds = {entry["kind"] for entry in rows}
    if len(kinds) > 1:
        raise RegistryError("Ambiguous", f"{name} is used by kinds {sorted(kinds)}; pass kind=")
    in_expression = [entry for entry in rows if in_range(entry["version"], range_expression)]
    if not in_expression:
        raise RegistryError("NotFound", f"no version of {name} satisfies {range_expression!r}")
    floor = LABEL_ORDER.index(min_label)
    at_floor = [entry for entry in in_expression if LABEL_ORDER.index(entry["label"]) >= floor]
    if not at_floor:
        best = max(in_expression, key=lambda entry: _version_key(entry["version"]))
        raise RegistryError("BelowLabel", f"{name}@{best['version']} is {best['label']}, below {min_label}")
    chosen = max(at_floor, key=lambda entry: _version_key(entry["version"]))
    return dict(chosen)


def _consistent(row: dict) -> bool:
    """True when the index's implementation list and the embedded manifest name the same languages, each with a location."""
    indexed = {item["language"] for item in row.get("implementations", [])}
    embedded = {item["language"]: item.get("location") for item in row["manifest"].get("implementations", [])}
    return indexed == set(embedded) and all(embedded[language] for language in indexed)


def select_implementation(row: dict, languages: list[str]) -> dict | None:
    """Step 5 of SPEC §6.2. Tools only: the first caller language the tool implements.

    Order: language availability first (NoImplementation, listing the indexed languages), then, once a
    language is chosen, the consistency of the whole row (IntegrityError on any mismatch). A mismatched row
    with an unsupported caller language is therefore NoImplementation, and never exposes a result.
    Returns None for kinds that have no implementations.
    """
    if row["kind"] != "tool":
        return None
    available = {item["language"]: item for item in row.get("implementations", [])}
    chosen = next((language for language in languages if language in available), None)
    if chosen is None:
        error = RegistryError(
            "NoImplementation",
            f"{row['name']}@{row['version']} has no implementation in {list(languages)}; "
            f"it has {sorted(available)}",
        )
        error.available = sorted(available)
        raise error
    if not _consistent(row):
        raise RegistryError(
            "IntegrityError",
            f"{row['name']}@{row['version']}: the index's implementations do not match its embedded manifest",
        )
    locations = {item["language"]: item["location"] for item in row["manifest"]["implementations"]}
    return {"language": chosen, "location": locations[chosen],
            "code_digest": available[chosen]["code_digest"], "entry": available[chosen]["entry"]}
