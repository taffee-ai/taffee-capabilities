"""M5: adding a language is data plus, at most, a runner (LANGUAGES.md v2 §9).

A fictional fourth language is registered in a temporary registry, with a runner
that exists only in this test. The manifest, identity, index and language code must
accept it without any change to those modules.
"""

import shutil
import tempfile
import unittest
from pathlib import Path

import yaml

from capability.conformance import RUNNERS
from capability.implementations import check_implementations
from capability.languages import load_languages
from capability.manifest import validate_manifest

from fixture_support import add_fixture_tool, new_tree, tool_manifest


class ExtensionTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = new_tree(Path(self._tmp.name))
        registry = yaml.safe_load((self.root / "policy" / "languages.yaml").read_text(encoding="utf-8"))
        registry["languages"]["fictional"] = {
            "extensions": [".fic"], "entry": "main.fic", "runner": "fictional-runner",
            "toolchain": ["fic-run", "--version"],
        }
        (self.root / "policy" / "languages.yaml").write_text(yaml.safe_dump(registry), encoding="utf-8")
        RUNNERS["fictional-runner"] = lambda entry: ["fic-run", str(entry)]
        self.addCleanup(RUNNERS.pop, "fictional-runner", None)

    def tearDown(self):
        self._tmp.cleanup()

    def test_fictional_language_is_registered_by_data_alone(self):
        self.assertIn("fictional", load_languages(self.root))

    def test_manifest_schema_accepts_the_fictional_language(self):
        manifest = tool_manifest("fixture-fic", [{"language": "fictional", "location": "library/fixture-fic/fictional"}])
        self.assertEqual(validate_manifest(manifest), [])

    def test_identity_and_checks_accept_the_fictional_language(self):
        folder = self.root / "library" / "fixture-fic" / "fictional"
        folder.mkdir(parents=True)
        (folder / "main.fic").write_text("body\n", encoding="utf-8")
        manifest = tool_manifest("fixture-fic", [{"language": "fictional", "location": "library/fixture-fic/fictional"}])
        resolved, errors = check_implementations(manifest, self.root, load_languages(self.root))
        self.assertEqual(errors, [])
        self.assertEqual([item["language"] for item in resolved], ["fictional"])

    def test_bundled_languages_still_work_alongside_it(self):
        add_fixture_tool(self.root, "python")
        manifest = tool_manifest("fixture-tool", [{"language": "python", "location": "library/fixture-tool/python"}])
        resolved, errors = check_implementations(manifest, self.root, load_languages(self.root))
        self.assertEqual(errors, [])
        self.assertEqual(len(resolved), 1)


if __name__ == "__main__":
    unittest.main()
