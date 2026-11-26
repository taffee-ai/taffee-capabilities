"""S-002 M6: a fictional fourth language, registered in data only, resolves without any registry change."""

import tempfile
import unittest
from pathlib import Path

import yaml

from capability.index import build_index
from capability.manifest import load_manifest
from capability.registry import Registry

from fixture_support import FIXTURES, new_tree, write_manifest


class FictionalLanguageTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = new_tree(Path(self._tmp.name))
        registry = yaml.safe_load((self.root / "policy" / "languages.yaml").read_text(encoding="utf-8"))
        registry["languages"]["fictional"] = {
            "extensions": [".fic"], "entry": "main.fic", "runner": "fictional-runner",
            "toolchain": ["fic-run", "--version"],
        }
        (self.root / "policy" / "languages.yaml").write_text(yaml.safe_dump(registry), encoding="utf-8")
        folder = self.root / "library" / "fixture-fic" / "fictional"
        folder.mkdir(parents=True)
        (folder / "main.fic").write_text("body\n", encoding="utf-8")
        manifest = load_manifest(FIXTURES / "valid" / "tool.yaml")
        manifest["name"] = "fixture-fic"
        manifest["implementations"] = [{"language": "fictional", "location": "library/fixture-fic/fictional"}]
        write_manifest(self.root, "tool", "fixture-fic", manifest)
        build_index(self.root, fixture_mode=True)

    def tearDown(self):
        self._tmp.cleanup()

    def test_the_fictional_language_resolves_when_named(self):
        match = Registry(self.root).resolve("fixture-fic", languages=["fictional"])
        self.assertEqual(match["language"], "fictional")
        self.assertEqual(match["entry_path"], "library/fixture-fic/fictional/main.fic")

    def test_it_is_not_chosen_when_not_named(self):
        from capability.registry import RegistryError
        with self.assertRaises(RegistryError) as ctx:
            Registry(self.root).resolve("fixture-fic", languages=["python"])
        self.assertEqual(ctx.exception.code, "NoImplementation")
        self.assertEqual(ctx.exception.available, ["fictional"])


if __name__ == "__main__":
    unittest.main()
