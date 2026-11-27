"""Manifest loading and validation (S-001 §4, §5; M2).

A manifest is YAML on disk and a plain mapping in memory. Loading rejects two
things that would make a digest unreliable: duplicate keys, which YAML silently
overwrites, and values JSON cannot represent, such as dates. Validation checks
the kind's JSON Schema, then the placeholder rule for owner and summary.
"""

import json
import re
from pathlib import Path

import re2
import yaml
from jsonschema import Draft202012Validator

SCHEMA_DIR = Path(__file__).resolve().parent.parent / "schemas"
KINDS = ("tool", "agent", "model")
PLACEHOLDERS = frozenset({"tbd", "none", "todo", "n/a", "-"})


class ManifestError(Exception):
    """A manifest could not be loaded or did not validate. ``errors`` lists each problem."""

    def __init__(self, errors):
        self.errors = list(errors)
        super().__init__("; ".join(self.errors))


_TIMESTAMP_TAG = "tag:yaml.org,2002:timestamp"


class _StrictLoader(yaml.SafeLoader):
    """Safe YAML loader that refuses duplicate mapping keys and keeps dates as text.

    Provider model versions such as ``2025-06-01`` are identifiers, not dates, so
    the timestamp resolver is removed. The text is then hashed exactly as written.
    """


_StrictLoader.yaml_implicit_resolvers = {
    first: [(tag, pattern) for tag, pattern in resolvers if tag != _TIMESTAMP_TAG]
    for first, resolvers in yaml.SafeLoader.yaml_implicit_resolvers.items()
}


def _mapping_without_duplicates(loader, node, deep=False):
    mapping = {}
    for key_node, value_node in node.value:
        key = loader.construct_object(key_node, deep=True)
        if key in mapping:
            raise yaml.constructor.ConstructorError(
                "while constructing a mapping",
                node.start_mark,
                f"duplicate key {key!r}",
                key_node.start_mark,
            )
        mapping[key] = loader.construct_object(value_node, deep=True)
    return mapping


_StrictLoader.add_constructor(
    yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG, _mapping_without_duplicates
)


def _json_safe(value, path="<root>"):
    """Return the list of paths holding values JSON cannot represent."""
    if value is None or isinstance(value, (str, bool, int, float)):
        return [] if not isinstance(value, float) or value == value else [path]
    if isinstance(value, dict):
        bad = []
        for key, child in value.items():
            if not isinstance(key, str):
                bad.append(f"{path}.{key!r}")
            bad.extend(_json_safe(child, f"{path}.{key}"))
        return bad
    if isinstance(value, list):
        bad = []
        for index, child in enumerate(value):
            bad.extend(_json_safe(child, f"{path}[{index}]"))
        return bad
    return [path]


def load_manifest(path: Path | str) -> dict:
    """Parse a manifest file. Raises ManifestError for YAML or JSON-compatibility problems."""
    path = Path(path)
    try:
        data = yaml.load(path.read_text(encoding="utf-8"), Loader=_StrictLoader)
    except yaml.YAMLError as exc:
        raise ManifestError([f"{path}: YAML error: {exc}"]) from exc
    if not isinstance(data, dict):
        raise ManifestError([f"{path}: manifest must be a mapping"])
    unsafe = _json_safe(data)
    if unsafe:
        raise ManifestError([f"{path}: value not representable in JSON at {p}" for p in unsafe])
    return data


def _validator(kind: str) -> Draft202012Validator:
    schema = json.loads((SCHEMA_DIR / f"manifest.{kind}.schema.json").read_text(encoding="utf-8"))
    return Draft202012Validator(schema)


_VALIDATORS = {kind: None for kind in KINDS}


def _schema_errors(kind: str, manifest: dict) -> list[str]:
    if _VALIDATORS[kind] is None:
        _VALIDATORS[kind] = _validator(kind)
    messages = []
    for error in sorted(_VALIDATORS[kind].iter_errors(manifest), key=lambda e: list(e.absolute_path)):
        base = ".".join(str(part) for part in error.absolute_path)
        if error.validator == "additionalProperties":
            for name in re.findall(r"'([^']+)' was unexpected", error.message):
                field = f"{base}.{name}" if base else name
                messages.append(f"{field}: unknown field")
        else:
            hint = ""
            if isinstance(error.instance, bool) and error.validator != "type":
                hint = " (YAML reads unquoted yes/no/on/off as booleans; quote the value if it is text)"
            messages.append(f"{base or '<root>'}: {error.message}{hint}")
    return messages


def _placeholder_errors(manifest: dict) -> list[str]:
    errors = []
    for field in ("owner", "summary"):
        value = manifest.get(field)
        if isinstance(value, str) and value.strip().lower() in PLACEHOLDERS:
            errors.append(f"{field}: placeholder value {value.strip()!r} is not allowed")
    return errors


EFFECTS = ("read-only", "reversible", "irreversible")


def tool_effect(spec: dict) -> str:
    """T-008: the effect of a call: the declared `spec.effect`, or, when absent, read-only for a tool declared
    readOnly and irreversible for any other (the safe default: what is not declared undoable is not)."""
    effect = spec.get("effect")
    if effect in EFFECTS:
        return effect
    return "read-only" if spec.get("readOnly") is True else "irreversible"


def _spec_errors(spec: dict) -> list[str]:
    """Checks a JSON Schema cannot express: the input schema is a schema, every pattern compiles (S-009), and a
    declared effect agrees with readOnly (T-008)."""
    errors = []
    if "effect" in spec and (spec["effect"] == "read-only") != (spec.get("readOnly") is True):
        errors.append("spec.effect: read-only exactly when spec.readOnly is true")
    try:
        Draft202012Validator.check_schema(spec["inputSchema"])
    except Exception as exc:  # jsonschema raises SchemaError; any failure means the schema cannot be used
        errors.append(f"spec.inputSchema: not a valid JSON Schema: {getattr(exc, 'message', exc)}")
    for index, check in enumerate(spec.get("postconditions", [])):
        if check["type"] == "each_line_matches":
            try:
                re2.compile(check["pattern"])
            except Exception as exc:  # RE2 refuses look-around and backreferences: no linear-time guarantee
                errors.append(f"spec.postconditions[{index}].pattern: not accepted by the linear-time engine (RE2): {exc}")
    return errors


def validate_manifest(manifest: dict) -> list[str]:
    """Return every validation error for a parsed manifest. An empty list means valid."""
    kind = manifest.get("kind")
    if kind not in KINDS:
        return [f"kind: must be one of {', '.join(KINDS)}"]
    errors = _schema_errors(kind, manifest) + _placeholder_errors(manifest)
    if kind == "tool" and not errors:
        errors += _spec_errors(manifest["spec"])
    return errors


def check_manifest_file(path: Path | str) -> dict:
    """Load and validate a manifest file. Returns the parsed manifest, or raises ManifestError."""
    manifest = load_manifest(path)
    errors = validate_manifest(manifest)
    if errors:
        raise ManifestError([f"{path}: {error}" for error in errors])
    return manifest
