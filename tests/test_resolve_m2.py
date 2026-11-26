"""S-002 M2: name, range, kind and label (SPEC §6.2 steps 1 to 4). Internal candidate only."""

import copy
import json
import tempfile
import unittest
from pathlib import Path

from capability.canon import canonical_bytes
from capability.index import index_digest
from capability.manifest import load_manifest
from capability.registry import Registry, RegistryError
from capability.resolve import in_range

from fixture_support import FIXTURES, add_fixture_tool, new_tree, write_manifest
from capability.index import build_index


class RangeTest(unittest.TestCase):
    def test_caret_and_exact_ranges(self):
        self.assertTrue(in_range("1.4.0", "^1.0.0"))
        self.assertFalse(in_range("2.0.0", "^1.0.0"))
        self.assertTrue(in_range("0.2.5", "^0.2.0"))
        self.assertFalse(in_range("0.3.0", "^0.2.0"))
        self.assertTrue(in_range("1.0.0", "1.0.0"))
        self.assertFalse(in_range("1.0.1", "1.0.0"))
        self.assertTrue(in_range("9.9.9", "*"))


class BadRangeTest(unittest.TestCase):
    def test_malformed_range_is_invalid_range_not_a_raw_exception(self):
        for bad in ("not-a-version", "^1.x", "1.0", "^01.0.0", "^-1.0.0", "1.0.-1", "1.0.0.0", "^1.0"):
            with self.subTest(range=bad):
                with self.assertRaises(RegistryError) as ctx:
                    in_range("1.0.0", bad)
                self.assertEqual(ctx.exception.code, "InvalidRange")


class MultiVersionTest(unittest.TestCase):
    """Several versions of one name. The index is built by hand (S-001 keeps one manifest per folder)."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = new_tree(Path(self._tmp.name))
        add_fixture_tool(self.root, "python")
        base = load_manifest(FIXTURES / "valid" / "tool.yaml")
        write_manifest(self.root, "tool", "fixture-tool", base)
        build_index(self.root, fixture_mode=True)
        index = json.loads((self.root / "index" / "index.json").read_bytes())
        template = index["entries"][0]
        entries = []
        for version, label in (("1.2.0", "Declared"), ("1.4.0", "Declared"), ("2.0.0", "Declared")):
            entry = copy.deepcopy(template)
            entry["version"] = version
            entry["label"] = label
            entry["manifest"]["version"] = version
            entries.append(entry)
        index["entries"] = entries
        index["index_digest"] = index_digest(index)
        (self.root / "index" / "index.json").write_bytes(canonical_bytes(index))
        self.registry = Registry(self.root)

    def tearDown(self):
        self._tmp.cleanup()

    def test_range_picks_1_4_0_over_1_2_0_and_rejects_2_0_0(self):
        self.assertEqual(self.registry._candidate("fixture-tool", "^1.0.0")["version"], "1.4.0")

    def test_candidate_is_the_verified_index_row_with_its_fields(self):
        row = self.registry._candidate("fixture-tool", "^1.0.0")
        self.assertEqual(row["version"], "1.4.0")
        self.assertIn("implementations", row)
        self.assertIn("manifest", row)
        self.assertIn("manifest_digest", row)

    def test_star_picks_the_highest(self):
        self.assertEqual(self.registry._candidate("fixture-tool")["version"], "2.0.0")

    def test_no_version_in_range_is_not_found(self):
        with self.assertRaises(RegistryError) as ctx:
            self.registry._candidate("fixture-tool", "^3.0.0")
        self.assertEqual(ctx.exception.code, "NotFound")

    def test_label_floor_is_below_label_when_no_version_meets_it(self):
        with self.assertRaises(RegistryError) as ctx:
            self.registry._candidate("fixture-tool", "^1.0.0", min_label="Validated")
        self.assertEqual(ctx.exception.code, "BelowLabel")

    def test_unknown_name_is_not_found(self):
        with self.assertRaises(RegistryError) as ctx:
            self.registry._candidate("no-such-tool")
        self.assertEqual(ctx.exception.code, "NotFound")


class KindTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = new_tree(Path(self._tmp.name))
        add_fixture_tool(self.root, "python")
        write_manifest(self.root, "tool", "shared", load_manifest(FIXTURES / "valid" / "tool.yaml") | {"name": "shared"})
        agent = load_manifest(FIXTURES / "valid" / "agent.yaml")
        agent["name"] = "shared"
        folder = self.root / "library" / "shared-agent"
        folder.mkdir(parents=True)
        (folder / "agent.txt").write_text("agent\n", encoding="utf-8")
        agent["source"] = {"type": "library", "location": "library/shared-agent"}
        write_manifest(self.root, "agent", "shared", agent)
        build_index(self.root, fixture_mode=True)
        self.registry = Registry(self.root)

    def tearDown(self):
        self._tmp.cleanup()

    def test_a_name_in_two_kinds_is_ambiguous_without_kind(self):
        with self.assertRaises(RegistryError) as ctx:
            self.registry._candidate("shared")
        self.assertEqual(ctx.exception.code, "Ambiguous")

    def test_kind_selects_one(self):
        self.assertEqual(self.registry._candidate("shared", kind="agent")["kind"], "agent")


if __name__ == "__main__":
    unittest.main()
