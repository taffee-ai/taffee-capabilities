"""M3: the capability-validate command (S-001 §9)."""

import contextlib
import io
import shutil
import tempfile
import unittest
from pathlib import Path

from capability.cli import main
from fixture_support import FIXTURES as _F  # noqa: F401

REPO = Path(__file__).resolve().parent.parent
FIXTURES = Path(__file__).resolve().parent / "fixtures" / "manifests"


class CliTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        from fixture_support import add_fixture_tool, new_tree
        new_tree(self.root)
        folder = self.root / "catalog" / "tool" / "fixture-tool"
        folder.mkdir(parents=True)
        shutil.copy(FIXTURES / "valid" / "tool.yaml", folder / "manifest.yaml")
        add_fixture_tool(self.root, "python")

    def tearDown(self):
        self._tmp.cleanup()

    def test_valid_tree_exits_zero_and_writes_index(self):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            status = main([str(self.root), "--fixture-mode"])
        self.assertEqual(status, 0)
        self.assertTrue((self.root / "index" / "index.json").is_file())
        self.assertIn("1 entries", out.getvalue())

    def test_invalid_tree_exits_one_and_writes_nothing(self):
        shutil.copy(FIXTURES / "invalid" / "tool-blank-owner.yaml",
                    self.root / "catalog" / "tool" / "fixture-tool" / "manifest.yaml")
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            status = main([str(self.root), "--fixture-mode"])
        self.assertEqual(status, 1)
        self.assertFalse((self.root / "index" / "index.json").exists())
        self.assertIn("owner", err.getvalue())


if __name__ == "__main__":
    unittest.main()
