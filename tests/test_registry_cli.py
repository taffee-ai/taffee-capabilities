"""S-018 R7: capability-registry init-key, import-index and verify (REG-COMPAT-3, 4; REG-ROW-5)."""

import json
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from capability.index import build_index
from capability.registry import Registry
from capability.registry_cli import import_index
from capability.rows import load_private_key, verify_store
from capability.store import SQLiteStore
from capability.store_registry import StoreRegistry

from fixture_support import REPO, copy_suite
from registry_support import LANGUAGES, fresh_revocation


def cli(*args):
    return subprocess.run([sys.executable, "-m", "capability.registry_cli", *args], capture_output=True, text=True,
                          timeout=120, cwd=str(REPO), env=dict(os.environ, PYTHONPATH=str(REPO)))


class ImportIndexTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls._tmp = tempfile.TemporaryDirectory()
        cls.base = Path(cls._tmp.name)
        cls.repo = cls.base / "repo"
        cls.repo.mkdir()
        for folder in ("schemas", "policy", "catalog", "library", "evidence"):
            shutil.copytree(REPO / folder, cls.repo / folder)
        copy_suite(cls.repo)
        build_index(cls.repo)  # the real publication gate, as for the published catalog
        cls.index = Registry(cls.repo).verified_index()
        done = cli("init-key", "--key", str(cls.base / "admission.key"))
        assert done.returncode == 0, done.stderr
        cls.public_b64 = done.stdout.strip()
        cls.key = load_private_key(cls.base / "admission.key")
        cls.data = cls.base / "data"
        done = cli("import-index", "--repo", str(cls.repo), "--data", str(cls.data), "--key", str(cls.base / "admission.key"))
        assert done.returncode == 0, done.stderr
        cls.first_output = done.stdout

    @classmethod
    def tearDownClass(cls):
        cls._tmp.cleanup()

    def test_the_key_file_is_private(self):
        self.assertEqual(os.stat(self.base / "admission.key").st_mode & 0o777, 0o600)

    def test_every_index_entry_is_imported_with_its_identity_and_label(self):
        self.assertEqual(self.first_output.strip(), f"imported {len(self.index['entries'])}, already present 0")
        store = SQLiteStore(self.data)
        for entry in self.index["entries"]:
            with self.subTest(tool=entry["name"]):
                row = store.get_row(entry["kind"], entry["name"], entry["version"])
                self.assertEqual(row["identity"], entry.get("identity") or entry["manifest_digest"])
                self.assertEqual(row["label"], entry["label"])
                self.assertEqual(row["source"], {"type": "file-index", "index_digest": self.index["index_digest"]})
        store.close()

    def test_the_four_tested_tools_keep_tested_and_resolve_from_the_store(self):
        store = SQLiteStore(self.data)
        client, _, _ = fresh_revocation()
        registry = StoreRegistry(store, self.key.public_key(), client, LANGUAGES)
        for name in ("git-log", "glob", "grep", "unawaited-async"):
            with self.subTest(tool=name):
                match = registry.resolve(name, languages=["python"])
                self.assertEqual(match["label"], "Tested")
                file_entry = next(e for e in self.index["entries"] if e["name"] == name)
                self.assertEqual(match["identity"], file_entry["identity"])
        self.assertEqual([i["name"] for i in registry.search("search file contents")["items"]], ["grep"])
        store.close()

    def test_importing_again_writes_nothing(self):
        store = SQLiteStore(self.data)
        before = store.head()
        self.assertEqual(import_index(self.repo, store, self.key), {"imported": 0, "skipped": len(self.index["entries"])})
        self.assertEqual(store.head(), before)
        store.close()

    def test_verify_passes_then_fails_after_tampering(self):
        done = cli("verify", "--data", str(self.data), "--public-key", self.public_b64)
        self.assertEqual((done.returncode, done.stdout.strip()), (0, f"store verifies: {len(self.index['entries'])} rows"))
        copy = self.base / "tampered"
        shutil.copytree(self.data, copy)
        conn = sqlite3.connect(copy / "registry.sqlite3")
        with conn:
            conn.execute("UPDATE rows SET body = replace(body, '\"Tested\"', '\"Approved\"') WHERE name = 'grep'")
        conn.close()
        done = cli("verify", "--data", str(copy), "--public-key", self.public_b64)
        self.assertEqual(done.returncode, 1)
        self.assertIn("does not verify", done.stderr)

    def test_verify_store_matches_the_cli(self):
        store = SQLiteStore(self.data)
        self.assertEqual(verify_store(store, self.key.public_key()), len(self.index["entries"]))
        store.close()

    def test_serve_refuses_to_start_without_a_revocation_feed(self):
        done = cli("serve", "--data", str(self.base / "d2"), "--key", str(self.base / "admission.key"),
                   "--sandbox-image", "x", "--work-dir", str(self.base / "w"))
        self.assertNotEqual(done.returncode, 0)
        self.assertIn("a revocation feed is required", done.stderr)


if __name__ == "__main__":
    unittest.main()
