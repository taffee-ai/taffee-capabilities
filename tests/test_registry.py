"""S-002 M1: load and verify the index; list and describe (SPEC §4, §8)."""

import json
import shutil
import tempfile
import unittest
from pathlib import Path

from capability.canon import canonical_bytes
from capability.index import build_index
from capability.manifest import load_manifest
from capability.index import index_digest
from capability.registry import Registry, RegistryError

from fixture_support import FIXTURES, REPO, add_fixture_tool, copy_suite, new_tree, write_manifest


class FixtureTreeTest(unittest.TestCase):
    """A temporary tree with one fixture tool, indexed in fixture mode."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = new_tree(Path(self._tmp.name))
        add_fixture_tool(self.root, "python")
        self.tool = load_manifest(FIXTURES / "valid" / "tool.yaml")
        write_manifest(self.root, "tool", "fixture-tool", self.tool)
        build_index(self.root, fixture_mode=True)
        self.registry = Registry(self.root)

    def tearDown(self):
        self._tmp.cleanup()

    def _index_path(self) -> Path:
        return self.root / "index" / "index.json"


class ListAndDescribeTest(FixtureTreeTest):
    def test_default_list_shows_the_entry_as_declared(self):
        listed = self.registry.list()
        self.assertEqual([e["name"] for e in listed], ["fixture-tool"])
        self.assertEqual(listed[0]["label"], "Declared")
        self.assertEqual(listed[0]["languages"], ["python"])

    def test_min_label_above_the_entry_lists_nothing(self):
        self.assertEqual(self.registry.list(minLabel="Validated"), [])

    def test_kind_filter(self):
        self.assertEqual(len(self.registry.list(kind="tool")), 1)
        self.assertEqual(self.registry.list(kind="agent"), [])

    def test_describe_returns_the_embedded_manifest(self):
        described = self.registry.describe("fixture-tool")
        self.assertEqual(canonical_bytes(described["manifest"]), canonical_bytes(
            json.loads(json.dumps(self.tool, sort_keys=True))))
        self.assertTrue(described["identity"].startswith("sha256:"))

    def test_describe_unknown_name_is_not_found(self):
        with self.assertRaises(RegistryError) as ctx:
            self.registry.describe("no-such-tool")
        self.assertEqual(ctx.exception.code, "NotFound")

    def test_repeated_calls_give_identical_output(self):
        self.assertEqual(self.registry.list(), self.registry.list())
        self.assertEqual(self.registry.describe("fixture-tool"), self.registry.describe("fixture-tool"))


class MalformedIndexTest(FixtureTreeTest):
    """A malformed index is IndexError on every call, never an uncaught Python error."""

    def _write(self, data):
        self._index_path().write_text(json.dumps(data), encoding="utf-8")

    def _assert_index_error(self):
        for call in (lambda: self.registry.list(), lambda: self.registry.describe("fixture-tool")):
            with self.subTest(call=call):
                with self.assertRaises(RegistryError) as ctx:
                    call()
                self.assertEqual(ctx.exception.code, "IndexError")

    def test_non_object_json_is_index_error(self):
        self._index_path().write_text("[1, 2, 3]", encoding="utf-8")
        self._assert_index_error()

    def test_scalar_json_is_index_error(self):
        self._index_path().write_text("42", encoding="utf-8")
        self._assert_index_error()

    def test_valid_digest_but_missing_entries_is_index_error(self):
        data = {"schema": "capability-index/1", "policy_version": 1,
                "inputs_digest": "sha256:" + "0" * 64}
        data["index_digest"] = index_digest(data)
        self._write(data)
        self._assert_index_error()

    def test_valid_digest_but_invalid_label_is_index_error(self):
        data = json.loads(self._index_path().read_bytes())
        data["entries"][0]["label"] = "Gold"
        data["index_digest"] = index_digest(data)
        self._write(data)
        self._assert_index_error()


class OptionOrderTest(FixtureTreeTest):
    def test_corrupt_index_is_index_error_even_with_invalid_options(self):
        self._index_path().write_text("[]", encoding="utf-8")
        with self.assertRaises(RegistryError) as ctx:
            self.registry.list(minLabel="Gold", kind="widget")
        self.assertEqual(ctx.exception.code, "IndexError")


class BundledSchemaTest(FixtureTreeTest):
    def test_missing_bundled_schema_is_setup_error_not_a_raw_exception(self):
        import capability.registry as registry
        original = registry.SCHEMA_DIR
        registry.SCHEMA_DIR = Path(tempfile.mkdtemp())
        self.addCleanup(setattr, registry, "SCHEMA_DIR", original)
        with self.assertRaises(RegistryError) as ctx:
            self.registry.list()
        self.assertEqual(ctx.exception.code, "SetupError")

    def test_invalid_bundled_schema_is_setup_error(self):
        import capability.registry as registry
        original = registry.SCHEMA_DIR
        bad = Path(tempfile.mkdtemp())
        (bad / "index.schema.json").write_text('{"type": 7}', encoding="utf-8")
        registry.SCHEMA_DIR = bad
        self.addCleanup(setattr, registry, "SCHEMA_DIR", original)
        with self.assertRaises(RegistryError) as ctx:
            self.registry.list()
        self.assertEqual(ctx.exception.code, "SetupError")


class DescribeCollisionTest(FixtureTreeTest):
    def test_a_name_used_by_two_kinds_is_ambiguous_without_kind(self):
        agent = load_manifest(FIXTURES / "valid" / "agent.yaml")
        agent["name"] = "fixture-tool"
        folder = self.root / "library" / "fixture-tool-agent"
        folder.mkdir(parents=True)
        (folder / "agent.txt").write_text("agent\n", encoding="utf-8")
        agent["source"] = {"type": "library", "location": "library/fixture-tool-agent"}
        write_manifest(self.root, "agent", "fixture-tool", agent)
        build_index(self.root, fixture_mode=True)
        with self.assertRaises(RegistryError) as ctx:
            self.registry.describe("fixture-tool")
        self.assertEqual(ctx.exception.code, "Ambiguous")
        self.assertEqual(self.registry.describe("fixture-tool", kind="tool")["kind"], "tool")


class IndexFailClosedTest(FixtureTreeTest):
    def _tamper(self):
        data = json.loads(self._index_path().read_bytes())
        data["entries"][0]["label"] = "Tested"
        self._index_path().write_bytes(canonical_bytes(data))

    def test_tampered_index_fails_list_describe_and_list_with_filter(self):
        self._tamper()
        for call in (lambda: self.registry.list(), lambda: self.registry.describe("fixture-tool")):
            with self.subTest(call=call):
                with self.assertRaises(RegistryError) as ctx:
                    call()
                self.assertEqual(ctx.exception.code, "IndexError")

    def test_non_canonical_index_is_refused_even_with_a_valid_digest(self):
        data = json.loads(self._index_path().read_bytes())
        self._index_path().write_text(json.dumps(data, indent=4), encoding="utf-8")
        with self.assertRaises(RegistryError) as ctx:
            self.registry.list()
        self.assertEqual(ctx.exception.code, "IndexError")

    def test_missing_index_is_refused(self):
        self._index_path().unlink()
        with self.assertRaises(RegistryError) as ctx:
            self.registry.list()
        self.assertEqual(ctx.exception.code, "IndexError")


class RealCatalogTest(unittest.TestCase):
    """The five tools, from the publication build. Four have a Python implementation and are Tested (T-001)."""

    def test_the_five_tools_are_listed_with_their_languages(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            shutil.copytree(REPO / "schemas", root / "schemas")
            shutil.copytree(REPO / "policy", root / "policy")
            shutil.copytree(REPO / "catalog", root / "catalog")
            shutil.copytree(REPO / "library", root / "library")
            shutil.copytree(REPO / "evidence", root / "evidence")
            copy_suite(root)
            build_index(root)
            listed = Registry(root).list(kind="tool")
            self.assertEqual(sorted(e["name"] for e in listed),
                             sorted(["git-log", "glob", "grep", "unawaited-async", "graph-query"]))
            self.assertEqual({e["name"]: e["label"] for e in listed},
                             {"git-log": "Tested", "glob": "Tested", "grep": "Tested",
                              "unawaited-async": "Tested", "graph-query": "Declared"})
            self.assertEqual({e["name"]: e["languages"] for e in listed},
                             {"git-log": ["python"], "glob": ["python"], "grep": ["python"],
                              "unawaited-async": ["python"], "graph-query": []})


if __name__ == "__main__":
    unittest.main()
