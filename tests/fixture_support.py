"""Helpers for mechanism tests that build a temporary capability tree from fixtures."""

import shutil
import sys
from pathlib import Path

import yaml

# Tests import the tools' main.py straight from library/ and drafts/. Without this, Python writes a __pycache__
# into those folders, which must hold only main.py (test_tools_t001.SelfContainedTest). Set here, not in
# test_tools_t001.py: that file is the suite the committed evidence pins by its digest.
sys.dont_write_bytecode = True

REPO = Path(__file__).resolve().parent.parent
FIXTURES = Path(__file__).resolve().parent / "fixtures" / "manifests"
IMPLEMENTATIONS = Path(__file__).resolve().parent / "fixtures" / "implementations" / "fixture-tool"


def new_tree(root: Path) -> Path:
    """Create a tree with the schemas and the language registry copied from the repository."""
    shutil.copytree(REPO / "schemas", root / "schemas")
    shutil.copytree(REPO / "policy", root / "policy")
    return root


# The current version of each real tool. glob is 1.3.0: its postcondition pattern was rewritten without a
# lookahead so it matches in linear time under RE2; the other three are still at 1.2.0.
TOOL_VERSIONS = {"git-log": "1.3.0", "glob": "1.4.0", "grep": "1.3.0", "unawaited-async": "1.3.0"}


def copy_suite(root: Path) -> None:
    """Copy the tool suite that the committed evidence names, so a copied tree can still earn Tested."""
    (root / "tests").mkdir(exist_ok=True)
    shutil.copy2(REPO / "tests" / "test_tools_t001.py", root / "tests" / "test_tools_t001.py")


def add_fixture_tool(root: Path, language: str = "python", name: str = "fixture-tool") -> Path:
    """Copy one fixture implementation into library/<name>/<language>/ and return the folder."""
    source = IMPLEMENTATIONS / language
    target = root / "library" / name / language
    shutil.copytree(source, target)
    return target


def write_manifest(root: Path, kind: str, name: str, manifest: dict) -> Path:
    folder = root / "catalog" / kind / name
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / "manifest.yaml"
    path.write_text(yaml.safe_dump(manifest, sort_keys=False), encoding="utf-8")
    return path


def tool_manifest(name: str, implementations: list[dict]) -> dict:
    """A valid tool manifest with the given implementations."""
    return {
        "kind": "tool",
        "name": name,
        "version": "1.0.0",
        "owner": "fixtures-owner",
        "summary": "A test-only tool.",
        "requires": ["repo.read"],
        "implementations": implementations,
        "spec": {
            "inputSchema": {"type": "object", "properties": {"n": {"type": "integer"}}, "required": ["n"]},
            "readOnly": True,
            "idempotent": True,
            "timeoutMs": 5000,
            "boundary": [],
        },
    }
