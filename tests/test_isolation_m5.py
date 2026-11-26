"""S-002 M5: isolation, load errors and determinism (SPEC §6.1, CAP-REG-9, CAP-REG-11)."""

import shutil
import tempfile
import unittest
from pathlib import Path

from capability.index import build_index
from capability.manifest import load_manifest
from capability.registry import Registry, RegistryError

from fixture_support import FIXTURES, add_fixture_tool, new_tree, write_manifest


class IsolationTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = new_tree(Path(self._tmp.name))
        for name in ("fixture-a", "fixture-b"):
            add_fixture_tool(self.root, "python", name=name)
            manifest = load_manifest(FIXTURES / "valid" / "tool.yaml")
            manifest["name"] = name
            manifest["implementations"] = [{"language": "python", "location": f"library/{name}/python"}]
            write_manifest(self.root, "tool", name, manifest)
        build_index(self.root, fixture_mode=True)
        self.registry = Registry(self.root)

    def tearDown(self):
        self._tmp.cleanup()

    def test_a_removed_folder_is_a_row_error_and_the_others_still_resolve(self):
        shutil.rmtree(self.root / "library" / "fixture-a")
        self.assertEqual(sorted(e["name"] for e in self.registry.list()), ["fixture-a", "fixture-b"])
        with self.assertRaises(RegistryError) as ctx:
            self.registry.resolve("fixture-a", languages=["python"])
        self.assertEqual(ctx.exception.code, "EntryError")
        self.assertEqual(ctx.exception.reason, "Missing")
        match = self.registry.resolve("fixture-b", languages=["python"])
        self.assertEqual(match["entry_path"], "library/fixture-b/python/main.py")

    def test_list_and_resolve_are_deterministic_for_the_same_index_and_bytes(self):
        self.assertEqual(self.registry.list(), self.registry.list())
        self.assertEqual(self.registry.resolve("fixture-b", languages=["python"]),
                         self.registry.resolve("fixture-b", languages=["python"]))


if __name__ == "__main__":
    unittest.main()
