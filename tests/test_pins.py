"""M4 (revised): the five prototype-named tools, and external pins refused where they are not
allowed (S-001 LANGUAGES.md v2 §2).

Four of the five (git-log, glob, grep, unawaited-async) have a Python implementation under
library/ since T-001 (1.0.0 was declared with none; now 1.2.0, and 1.3.0 for glob), pass their conformance cases
and have evidence records, so they are Tested. graph-query is still
declared with none. The prototype is reference only and is never a manifest source.
"""

import shutil
import tempfile
import unittest
from pathlib import Path

from capability.index import BuildError, build_index
from capability.pins import PinError, archive_tree_digest

from fixture_support import TOOL_VERSIONS, copy_suite

REPO = Path(__file__).resolve().parent.parent
TOOLS = ["git-log", "glob", "grep", "unawaited-async", "graph-query"]


def _copy_catalog(into: Path) -> Path:
    shutil.copytree(REPO / "schemas", into / "schemas")
    shutil.copytree(REPO / "policy", into / "policy")
    shutil.copytree(REPO / "catalog", into / "catalog")
    shutil.copytree(REPO / "library", into / "library")
    shutil.copytree(REPO / "evidence", into / "evidence")
    copy_suite(into)
    return into


class PinRefusalTest(unittest.TestCase):
    def test_a_folder_that_is_not_a_git_clone_is_refused(self):
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(PinError):
                archive_tree_digest(Path(tmp), "a" * 40, "harness/tools/git-log.ts")


class FiveToolsTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = _copy_catalog(Path(self._tmp.name))

    def tearDown(self):
        self._tmp.cleanup()

    def test_four_declare_a_python_implementation_and_graph_query_declares_none(self):
        import yaml
        for name in TOOLS:
            with self.subTest(tool=name):
                data = yaml.safe_load((self.root / "catalog" / "tool" / name / "manifest.yaml").read_text(encoding="utf-8"))
                if name == "graph-query":
                    self.assertEqual(data["implementations"], [])
                else:
                    self.assertEqual(data["implementations"],
                                     [{"language": "python", "location": "library/%s/python" % name}])
                    self.assertEqual(data["version"], TOOL_VERSIONS[name])
                self.assertNotIn("source", data)

    def test_the_five_build_in_publication_mode_with_their_ledger_entries(self):
        index = build_index(self.root)
        self.assertEqual(sorted(e["name"] for e in index["entries"]), sorted(TOOLS))
        for entry in index["entries"]:
            with self.subTest(tool=entry["name"]):
                if entry["name"] == "graph-query":
                    self.assertEqual(entry["label"], "Declared")
                    self.assertEqual(entry["implementations"], [])
                else:
                    self.assertEqual(entry["label"], "Tested")
                    self.assertEqual([i["language"] for i in entry["implementations"]], ["python"])
                    self.assertEqual(entry["implementations"][0]["entry"], "main.py")

    def test_a_missing_ledger_entry_for_one_of_them_refuses_the_catalog(self):
        (self.root / "catalog" / "tool" / "glob" / "versions.yaml").unlink()
        with self.assertRaises(BuildError) as ctx:
            build_index(self.root)
        self.assertTrue(any("glob@1.3.0" in e for e in ctx.exception.errors))


if __name__ == "__main__":
    unittest.main()


class GoldenIndexTest(unittest.TestCase):
    """The publication index for the five tools matches the committed golden fixture, when one exists."""

    def test_golden_index_matches(self):
        golden = REPO / "tests" / "golden" / "index.json"
        if not golden.exists():
            self.skipTest("golden index not generated yet")
        with tempfile.TemporaryDirectory() as tmp:
            root = _copy_catalog(Path(tmp))
            build_index(root)
            self.assertEqual((root / "index" / "index.json").read_bytes(), golden.read_bytes())
