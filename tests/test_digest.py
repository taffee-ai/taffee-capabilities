"""M1: manifest_digest and sha256-tree-v1 (S-001 §6, §11)."""

import os
import tempfile
import unittest
from pathlib import Path

from capability.digest import DigestError, manifest_digest, tree_digest


def _write(root: Path, rel: str, data: bytes) -> None:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)


class ManifestDigestTest(unittest.TestCase):
    def test_same_mapping_same_digest(self):
        a = {"kind": "tool", "name": "git-log", "version": "1.0.0"}
        b = {"version": "1.0.0", "name": "git-log", "kind": "tool"}
        self.assertEqual(manifest_digest(a), manifest_digest(b))

    def test_changed_value_changes_digest(self):
        a = {"kind": "tool", "name": "git-log", "version": "1.0.0"}
        b = {"kind": "tool", "name": "git-log", "version": "1.0.1"}
        self.assertNotEqual(manifest_digest(a), manifest_digest(b))

    def test_prefix(self):
        self.assertTrue(manifest_digest({}).startswith("sha256:"))


class TreeDigestTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def test_same_tree_twice_same_digest(self):
        _write(self.root, "a.txt", b"one\n")
        _write(self.root, "sub/b.txt", b"two\n")
        self.assertEqual(tree_digest(self.root), tree_digest(self.root))

    def test_one_byte_change_changes_digest(self):
        _write(self.root, "a.txt", b"one\n")
        before = tree_digest(self.root)
        _write(self.root, "a.txt", b"onE\n")
        self.assertNotEqual(before, tree_digest(self.root))

    def test_crlf_and_lf_text_give_same_digest(self):
        other = Path(tempfile.mkdtemp())
        try:
            _write(self.root, "a.txt", b"line one\r\nline two\r\n")
            _write(other, "a.txt", b"line one\nline two\n")
            self.assertEqual(tree_digest(self.root), tree_digest(other))
        finally:
            for p in sorted(other.rglob("*"), reverse=True):
                p.unlink() if p.is_file() else p.rmdir()
            other.rmdir()

    def test_nul_in_first_8000_bytes_hashes_raw(self):
        other = Path(tempfile.mkdtemp())
        try:
            _write(self.root, "b.bin", b"\x00\r\nx")
            _write(other, "b.bin", b"\x00\nx")
            self.assertNotEqual(tree_digest(self.root), tree_digest(other))
        finally:
            for p in sorted(other.rglob("*"), reverse=True):
                p.unlink() if p.is_file() else p.rmdir()
            other.rmdir()

    def test_excluded_names_do_not_change_digest(self):
        _write(self.root, "a.txt", b"one\n")
        before = tree_digest(self.root)
        _write(self.root, ".DS_Store", b"junk")
        _write(self.root, "__pycache__/x.pyc", b"junk")
        self.assertEqual(before, tree_digest(self.root))

    def test_symlink_named_ds_store_is_refused(self):
        _write(self.root, "a.txt", b"one\n")
        os.symlink(self.root / "a.txt", self.root / ".DS_Store")
        with self.assertRaises(DigestError) as ctx:
            tree_digest(self.root)
        self.assertIn(".DS_Store", str(ctx.exception))

    def test_regular_ds_store_is_still_ignored(self):
        _write(self.root, "a.txt", b"one\n")
        before = tree_digest(self.root)
        _write(self.root, ".DS_Store", b"junk")
        self.assertEqual(before, tree_digest(self.root))

    def test_symlink_is_refused_and_named(self):
        _write(self.root, "a.txt", b"one\n")
        link = self.root / "link.txt"
        os.symlink(self.root / "a.txt", link)
        with self.assertRaises(DigestError) as ctx:
            tree_digest(self.root)
        self.assertIn("link.txt", str(ctx.exception))

    def test_symlink_directory_is_refused(self):
        _write(self.root, "real/a.txt", b"one\n")
        os.symlink(self.root / "real", self.root / "alias")
        with self.assertRaises(DigestError):
            tree_digest(self.root)

    @unittest.skipUnless(hasattr(os, "mkfifo"), "mkfifo not available")
    def test_fifo_is_refused(self):
        os.mkfifo(self.root / "pipe")
        with self.assertRaises(DigestError):
            tree_digest(self.root)

    def test_single_file_is_tree_of_one(self):
        _write(self.root, "only.txt", b"one\n")
        self.assertTrue(tree_digest(self.root / "only.txt").startswith("sha256:"))

    def test_missing_path_is_an_error(self):
        with self.assertRaises(DigestError):
            tree_digest(self.root / "absent")


if __name__ == "__main__":
    unittest.main()
