"""Language registry (S-001 LANGUAGES.md v2; M5).

Languages are data in policy/languages.yaml. This module loads and checks that data.
Nothing here names a language. The runner for each language is looked up by name in
capability/conformance.py, so adding a language does not touch the manifest, identity
or registry code.
"""

import re
from pathlib import Path

import yaml

LANGUAGES_FILE = Path("policy") / "languages.yaml"
_ID = re.compile(r"^[a-z][a-z0-9-]{0,31}$")


class LanguageError(Exception):
    """The language registry is missing, malformed, or names an unknown language."""


def load_languages(root: Path | str) -> dict:
    """Return {id: spec} from policy/languages.yaml under ``root``."""
    path = Path(root) / LANGUAGES_FILE
    if not path.is_file():
        raise LanguageError(f"{path}: language registry is missing")
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict) or data.get("policy_version") != 1:
        raise LanguageError(f"{path}: language registry must be policy_version 1")
    languages = data.get("languages")
    if not isinstance(languages, dict) or not languages:
        raise LanguageError(f"{path}: 'languages' must be a non-empty mapping")
    for language_id, spec in languages.items():
        if not _ID.match(str(language_id)):
            raise LanguageError(f"{path}: language id {language_id!r} must be lowercase")
        for field in ("extensions", "entry", "runner", "toolchain"):
            if field not in spec:
                raise LanguageError(f"{path}: language {language_id} is missing '{field}'")
        if "/" in spec["entry"] or ".." in spec["entry"]:
            raise LanguageError(f"{path}: entry for {language_id} must be a plain file name")
    return languages
