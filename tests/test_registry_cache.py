"""The registry's index cache: verify once, re-verify on any change, never let a caller alter the cache."""

import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from capability import registry as registry_module
from capability.index import build_index
from capability.manifest import load_manifest
from capability.registry import Registry, RegistryError

from fixture_support import FIXTURES, add_fixture_tool, new_tree, write_manifest


class RegistryCacheTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = new_tree(Path(self._tmp.name))
        add_fixture_tool(self.root, "python")
        manifest = load_manifest(FIXTURES / "valid" / "tool.yaml")
        manifest["implementations"] = [{"language": "python", "location": "library/fixture-tool/python"}]
        write_manifest(self.root, "tool", "fixture-tool", manifest)
        build_index(self.root, fixture_mode=True)
        self.path = self.root / "index" / "index.json"
        self.registry = Registry(self.root)

    def tearDown(self):
        self._tmp.cleanup()

    def counting(self):
        """Patch load_index to count full verifications."""
        return mock.patch.object(registry_module, "load_index", wraps=registry_module.load_index)

    def tamper_same_size(self):
        """Change one hex digit of index_digest, keeping the size, so only the content differs."""
        raw = self.path.read_bytes()
        marker = b'"index_digest": "sha256:'
        at = raw.index(marker) + len(marker)
        swapped = b"0" if raw[at:at + 1] != b"0" else b"1"
        self.path.write_bytes(raw[:at] + swapped + raw[at + 1:])
        return raw

    def code(self, call):
        with self.assertRaises(RegistryError) as ctx:
            call()
        return ctx.exception.code

    def test_repeated_calls_verify_the_index_once(self):
        with self.counting() as load:
            for _ in range(5):
                self.registry.list()
                self.registry.describe("fixture-tool")
                self.registry.resolve("fixture-tool", languages=["python"])
        self.assertEqual(load.call_count, 1)

    def test_without_the_cache_every_call_verifies(self):
        registry = Registry(self.root, cache=False)
        with self.counting() as load:
            for _ in range(3):
                registry.list()
        self.assertEqual(load.call_count, 3)

    def test_an_in_place_change_of_the_same_size_is_detected(self):
        self.registry.list()
        self.tamper_same_size()
        self.assertEqual(self.code(self.registry.list), "IndexError")

    def test_a_change_with_its_mtime_set_back_is_still_detected(self):
        self.registry.list()
        before = os.stat(self.path)
        self.tamper_same_size()
        os.utime(self.path, ns=(before.st_atime_ns, before.st_mtime_ns))  # ctime still moves
        self.assertEqual(self.code(self.registry.list), "IndexError")

    def test_a_replaced_index_is_read_again(self):
        self.assertEqual([row["name"] for row in self.registry.list()], ["fixture-tool"])
        add_fixture_tool(self.root, "python", name="second-tool")
        manifest = load_manifest(FIXTURES / "valid" / "tool.yaml")
        manifest["name"] = "second-tool"
        manifest["implementations"] = [{"language": "python", "location": "library/second-tool/python"}]
        write_manifest(self.root, "tool", "second-tool", manifest)
        build_index(self.root, fixture_mode=True)  # writes through os.replace
        self.assertEqual(sorted(row["name"] for row in self.registry.list()), ["fixture-tool", "second-tool"])

    def test_a_failed_verification_is_not_cached(self):
        good = self.tamper_same_size()
        self.assertEqual(self.code(self.registry.list), "IndexError")
        self.assertEqual(self.code(self.registry.list), "IndexError")
        self.path.write_bytes(good)
        self.assertEqual(len(self.registry.list()), 1)

    def test_a_deleted_index_fails_every_time(self):
        self.registry.list()
        self.path.unlink()
        self.assertEqual(self.code(self.registry.list), "IndexError")
        self.assertEqual(self.code(self.registry.list), "IndexError")

    def test_callers_cannot_alter_the_cached_index(self):
        described = self.registry.describe("fixture-tool")
        described["manifest"]["spec"]["inputSchema"] = {"type": "null"}
        described["implementations"].clear()
        match = self.registry.resolve("fixture-tool", languages=["python"])
        match["spec"]["timeoutMs"] = 1
        index = self.registry.verified_index()
        index["entries"].clear()
        listed = self.registry.list()
        listed[0]["languages"].append("cobol")

        again = self.registry.describe("fixture-tool")
        self.assertNotEqual(again["manifest"]["spec"]["inputSchema"], {"type": "null"})
        self.assertTrue(again["implementations"])
        self.assertNotEqual(self.registry.resolve("fixture-tool", languages=["python"])["spec"]["timeoutMs"], 1)
        self.assertEqual(len(self.registry.verified_index()["entries"]), 1)
        self.assertEqual(self.registry.list()[0]["languages"], ["python"])

    def test_a_changed_implementation_is_still_refused_with_the_index_cached(self):
        """The cache covers the index only; resolve still recomputes the chosen code's digest each call."""
        self.registry.resolve("fixture-tool", languages=["python"])
        main = self.root / "library" / "fixture-tool" / "python" / "main.py"
        main.write_text(main.read_text(encoding="utf-8") + "\n# changed\n", encoding="utf-8")
        self.assertEqual(self.code(lambda: self.registry.resolve("fixture-tool", languages=["python"])),
                         "IntegrityError")


if __name__ == "__main__":
    unittest.main()
