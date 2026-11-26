"""M3/M5: index generation for agents, models and tools (S-001 §8, LANGUAGES.md v2)."""

import copy
import json
import shutil
import tempfile
import unittest
from pathlib import Path

from capability.canon import canonical_bytes
from capability.digest import manifest_digest, tree_digest
from capability.index import BuildError, build_index, index_digest
from capability.manifest import load_manifest
from capability.implementations import tool_identity

from fixture_support import FIXTURES, add_fixture_tool, new_tree, tool_manifest, write_manifest


class TreeTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = new_tree(Path(self._tmp.name))
        add_fixture_tool(self.root, "python")
        self.tool = load_manifest(FIXTURES / "valid" / "tool.yaml")
        write_manifest(self.root, "tool", "fixture-tool", self.tool)

    def tearDown(self):
        self._tmp.cleanup()

    def _index_path(self) -> Path:
        return self.root / "index" / "index.json"


class ToolIndexTest(TreeTest):
    def test_fixture_mode_builds_a_declared_tool_with_identity_and_implementations(self):
        index = build_index(self.root, fixture_mode=True)
        (entry,) = index["entries"]
        self.assertEqual(entry["label"], "Declared")
        self.assertEqual([i["language"] for i in entry["implementations"]], ["python"])
        self.assertEqual(entry["implementations"][0]["code_digest"],
                         tree_digest(self.root / "library" / "fixture-tool" / "python"))
        self.assertTrue(entry["identity"].startswith("sha256:"))

    def test_identity_is_manifest_digest_plus_implementation_digests(self):
        index = build_index(self.root, fixture_mode=True)
        entry = index["entries"][0]
        expected = tool_identity(self.tool, [
            {"language": "python", "code_digest": entry["implementations"][0]["code_digest"]}])
        self.assertEqual(entry["identity"], expected)

    def test_embedded_manifest_is_canonical_with_sorted_implementations(self):
        index = build_index(self.root, fixture_mode=True)
        self.assertEqual(index["entries"][0]["manifest"]["implementations"],
                         [{"language": "python", "location": "library/fixture-tool/python"}])

    def test_reordered_implementations_do_not_change_the_index(self):
        add_fixture_tool(self.root, "bash")
        manifest = copy.deepcopy(self.tool)
        manifest["implementations"] = [
            {"language": "python", "location": "library/fixture-tool/python"},
            {"language": "bash", "location": "library/fixture-tool/bash"},
        ]
        write_manifest(self.root, "tool", "fixture-tool", manifest)
        first = build_index(self.root, fixture_mode=True)
        manifest["implementations"].reverse()
        write_manifest(self.root, "tool", "fixture-tool", manifest)
        second = build_index(self.root, fixture_mode=True)
        # The manifest file's bytes are an input, so inputs_digest may differ. Identity must not.
        self.assertEqual(first["entries"], second["entries"])

    def test_changed_implementation_bytes_change_the_identity(self):
        before = build_index(self.root, fixture_mode=True)["entries"][0]["identity"]
        (self.root / "library" / "fixture-tool" / "python" / "main.py").write_text(
            "print('changed')\n", encoding="utf-8")
        after = build_index(self.root, fixture_mode=True)["entries"][0]["identity"]
        self.assertNotEqual(before, after)

    def test_empty_implementations_is_declared_with_no_code_identity(self):
        manifest = tool_manifest("fixture-empty", [])
        write_manifest(self.root, "tool", "fixture-empty", manifest)
        index = build_index(self.root, fixture_mode=True)
        empty = next(e for e in index["entries"] if e["name"] == "fixture-empty")
        self.assertEqual(empty["implementations"], [])
        self.assertEqual(empty["label"], "Declared")

    def test_index_digest_verifies_and_bytes_are_canonical(self):
        index = build_index(self.root, fixture_mode=True)
        on_disk = json.loads(self._index_path().read_bytes())
        self.assertEqual(index_digest(on_disk), on_disk["index_digest"])
        self.assertEqual(self._index_path().read_bytes(), canonical_bytes(on_disk))
        self.assertEqual(on_disk["index_digest"], index["index_digest"])

    def test_policy_byte_change_changes_inputs_and_index_digests(self):
        before = build_index(self.root, fixture_mode=True)
        policy = self.root / "policy" / "languages.yaml"
        policy.write_text(policy.read_text(encoding="utf-8") + "\n# touched\n", encoding="utf-8")
        after = build_index(self.root, fixture_mode=True)
        self.assertNotEqual(before["inputs_digest"], after["inputs_digest"])
        self.assertNotEqual(before["index_digest"], after["index_digest"])


class PublicationGateTest(TreeTest):
    def test_catalog_build_without_versions_yaml_is_refused(self):
        with self.assertRaises(BuildError) as ctx:
            build_index(self.root)
        self.assertTrue(any("publication gate" in e for e in ctx.exception.errors))
        self.assertFalse(self._index_path().exists())

    def test_catalog_build_with_a_matching_ledger_entry_passes(self):
        identity = build_index(self.root, fixture_mode=True)["entries"][0]["identity"]
        (self.root / "catalog" / "tool" / "fixture-tool" / "versions.yaml").write_text(
            f"versions:\n  - version: 1.0.0\n    identity: {identity}\n", encoding="utf-8")
        index = build_index(self.root)
        self.assertEqual(index["entries"][0]["identity"], identity)

    def test_changed_identity_at_a_recorded_version_is_refused(self):
        identity = build_index(self.root, fixture_mode=True)["entries"][0]["identity"]
        (self.root / "catalog" / "tool" / "fixture-tool" / "versions.yaml").write_text(
            f"versions:\n  - version: 1.0.0\n    identity: {identity}\n", encoding="utf-8")
        (self.root / "library" / "fixture-tool" / "python" / "main.py").write_text(
            "print('changed')\n", encoding="utf-8")
        with self.assertRaises(BuildError) as ctx:
            build_index(self.root)
        self.assertTrue(any("identity changed" in e for e in ctx.exception.errors))


class ImplementationRefusalTest(TreeTest):
    def test_unknown_language_is_refused(self):
        manifest = tool_manifest("fixture-bad", [{"language": "cobol", "location": "library/fixture-bad/cobol"}])
        write_manifest(self.root, "tool", "fixture-bad", manifest)
        with self.assertRaises(BuildError) as ctx:
            build_index(self.root, fixture_mode=True)
        self.assertTrue(any("unknown language" in e for e in ctx.exception.errors))

    def test_duplicate_language_is_refused(self):
        manifest = tool_manifest("fixture-dup", [
            {"language": "python", "location": "library/fixture-tool/python"},
            {"language": "python", "location": "library/fixture-tool/python"},
        ])
        write_manifest(self.root, "tool", "fixture-dup", manifest)
        with self.assertRaises(BuildError) as ctx:
            build_index(self.root, fixture_mode=True)
        self.assertTrue(any("duplicate language" in e for e in ctx.exception.errors))

    def test_location_outside_library_is_refused(self):
        manifest = tool_manifest("fixture-out", [{"language": "python", "location": "../secrets/python"}])
        write_manifest(self.root, "tool", "fixture-out", manifest)
        with self.assertRaises(BuildError):
            build_index(self.root, fixture_mode=True)

    def test_missing_entry_file_is_refused(self):
        (self.root / "library" / "fixture-tool" / "python" / "main.py").unlink()
        with self.assertRaises(BuildError) as ctx:
            build_index(self.root, fixture_mode=True)
        self.assertTrue(any("entry main.py is missing" in e for e in ctx.exception.errors))


class OtherKindsTest(TreeTest):
    def test_library_agent_keeps_code_digest(self):
        agent = load_manifest(FIXTURES / "valid" / "agent.yaml")
        _ = agent  # the agent fixture names library/fixture-agent; create it
        folder = self.root / "library" / "fixture-agent"
        folder.mkdir(parents=True)
        (folder / "agent.txt").write_text("agent body\n", encoding="utf-8")
        write_manifest(self.root, "agent", "fixture-agent", agent)
        index = build_index(self.root, fixture_mode=True)
        entry = next(e for e in index["entries"] if e["kind"] == "agent")
        self.assertEqual(entry["code_digest"], tree_digest(folder))
        self.assertEqual(entry["manifest_digest"], manifest_digest(agent))


if __name__ == "__main__":
    unittest.main()
