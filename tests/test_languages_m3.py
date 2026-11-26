"""S-002 M3: languages and NoImplementation (SPEC §6.2 step 5, LANGUAGES.md v2 §8)."""

import tempfile
import shutil
import unittest
from pathlib import Path

from capability.index import build_index
from capability.manifest import load_manifest
from capability.registry import Registry, RegistryError

from fixture_support import FIXTURES, REPO, add_fixture_tool, copy_suite, new_tree, write_manifest


class TwoLanguageToolTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = new_tree(Path(self._tmp.name))
        add_fixture_tool(self.root, "python")
        add_fixture_tool(self.root, "bash")
        manifest = load_manifest(FIXTURES / "valid" / "tool.yaml")
        manifest["implementations"] = [
            {"language": "python", "location": "library/fixture-tool/python"},
            {"language": "bash", "location": "library/fixture-tool/bash"},
        ]
        write_manifest(self.root, "tool", "fixture-tool", manifest)
        build_index(self.root, fixture_mode=True)
        self.registry = Registry(self.root)

    def tearDown(self):
        self._tmp.cleanup()

    def test_first_caller_language_the_tool_implements_is_chosen(self):
        chosen = self.registry._select("fixture-tool", languages=["typescript", "bash"])["implementation"]
        self.assertEqual(chosen["language"], "bash")
        self.assertEqual(chosen["location"], "library/fixture-tool/bash")

    def test_preference_order_is_respected(self):
        chosen = self.registry._select("fixture-tool", languages=["python", "bash"])["implementation"]
        self.assertEqual(chosen["language"], "python")

    def test_no_match_is_no_implementation_listing_the_tools_languages(self):
        with self.assertRaises(RegistryError) as ctx:
            self.registry._select("fixture-tool", languages=["typescript"])
        self.assertEqual(ctx.exception.code, "NoImplementation")
        self.assertEqual(ctx.exception.available, ["bash", "python"])

    def test_never_falls_back_to_an_unnamed_language(self):
        with self.assertRaises(RegistryError) as ctx:
            self.registry._select("fixture-tool", languages=[])
        self.assertEqual(ctx.exception.code, "NoImplementation")

    def test_no_implementation_is_never_an_integrity_error(self):
        try:
            self.registry._select("fixture-tool", languages=["typescript"])
        except RegistryError as exc:
            self.assertNotEqual(exc.code, "IntegrityError")


class IndexManifestMismatchTest(TwoLanguageToolTest):
    """A digest-valid index whose implementation list disagrees with its embedded manifest fails closed."""

    def _alter_index_only(self, mutate):
        import json
        from capability.canon import canonical_bytes
        from capability.index import index_digest

        path = self.root / "index" / "index.json"
        data = json.loads(path.read_bytes())
        mutate(data["entries"][0])
        data["index_digest"] = index_digest(data)
        path.write_bytes(canonical_bytes(data))

    def test_mismatched_row_with_an_unsupported_language_is_no_implementation(self):
        self._alter_index_only(lambda entry: entry["implementations"].pop())
        with self.assertRaises(RegistryError) as ctx:
            self.registry._select("fixture-tool", languages=["typescript"])
        self.assertEqual(ctx.exception.code, "NoImplementation")
        self.assertEqual(ctx.exception.available, ["bash"])

    def test_index_implementation_missing_from_embedded_manifest_is_integrity_error(self):
        self._alter_index_only(lambda entry: entry["implementations"].pop())
        with self.assertRaises(RegistryError) as ctx:
            self.registry._select("fixture-tool", languages=["bash"])
        self.assertEqual(ctx.exception.code, "IntegrityError")

    def test_index_implementation_not_in_index_list_is_integrity_error(self):
        self._alter_index_only(lambda entry: entry["manifest"]["implementations"].pop())
        with self.assertRaises(RegistryError) as ctx:
            self.registry._select("fixture-tool", languages=["python"])
        self.assertEqual(ctx.exception.code, "IntegrityError")

    def test_missing_location_is_integrity_error_not_key_error(self):
        self._alter_index_only(lambda entry: entry["manifest"]["implementations"][0].pop("location"))
        with self.assertRaises(RegistryError) as ctx:
            self.registry._select("fixture-tool", languages=["python"])
        self.assertEqual(ctx.exception.code, "IntegrityError")


class FiveToolsTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        shutil.copytree(REPO / "schemas", self.root / "schemas")
        shutil.copytree(REPO / "policy", self.root / "policy")
        shutil.copytree(REPO / "catalog", self.root / "catalog")
        shutil.copytree(REPO / "library", self.root / "library")
        shutil.copytree(REPO / "evidence", self.root / "evidence")
        copy_suite(self.root)
        build_index(self.root)
        self.registry = Registry(self.root)

    def tearDown(self):
        self._tmp.cleanup()

    def test_graph_query_gives_no_implementation_with_an_empty_list(self):
        with self.assertRaises(RegistryError) as ctx:
            self.registry._select("graph-query", languages=["python"])
        self.assertEqual(ctx.exception.code, "NoImplementation")
        self.assertEqual(ctx.exception.available, [])

    def test_the_four_with_python_list_it_as_available_for_another_language(self):
        for name in ["git-log", "glob", "grep", "unawaited-async"]:
            with self.subTest(tool=name):
                with self.assertRaises(RegistryError) as ctx:
                    self.registry._select(name, languages=["typescript"])
                self.assertEqual(ctx.exception.code, "NoImplementation")
                self.assertEqual(ctx.exception.available, ["python"])

    def test_minlabel_validated_gives_below_label_before_language_is_checked(self):
        # graph-query is Declared with no implementation: the label floor comes before NoImplementation.
        with self.assertRaises(RegistryError) as ctx:
            self.registry._select("graph-query", min_label="Validated", languages=["python"])
        self.assertEqual(ctx.exception.code, "BelowLabel")

    def test_a_tested_tool_meets_a_tested_floor_but_not_a_trusted_one(self):
        self.registry._select("git-log", min_label="Tested", languages=["python"])
        with self.assertRaises(RegistryError) as ctx:
            self.registry._select("git-log", min_label="Trusted", languages=["python"])
        self.assertEqual(ctx.exception.code, "BelowLabel")


if __name__ == "__main__":
    unittest.main()
