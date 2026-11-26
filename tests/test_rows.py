"""S-018 R1: signed rows, the hash chain, the checkpoint and verify_store (REG-ROW-1 to 6)."""

import copy
import sqlite3
import tempfile
import unittest
from pathlib import Path

from capability.rows import (
    GENESIS, RowError, key_id, row_hash, sign_checkpoint, sign_row, verify_checkpoint, verify_row, verify_store,
)
from capability.store import MemoryStore, SQLiteStore

from registry_support import add_rows, admission_key, synthetic_fields


class RowSignatureTest(unittest.TestCase):
    def setUp(self):
        self.private, self.public = admission_key()
        self.row = sign_row(self.private, synthetic_fields("alpha"), seq=1, prev=GENESIS)

    def test_a_signed_row_verifies_and_names_its_key(self):
        self.assertIs(verify_row(self.public, self.row), self.row)
        self.assertEqual(self.row["key_id"], key_id(self.public))

    def test_any_changed_field_breaks_the_signature(self):
        for field, value in (("label", "Tested"), ("version", "9.9.9"), ("seq", 2), ("prev", "sha256:" + "1" * 64)):
            with self.subTest(field=field):
                tampered = dict(self.row, **{field: value})
                with self.assertRaises(RowError):
                    verify_row(self.public, tampered)

    def test_a_change_inside_the_manifest_breaks_the_signature(self):
        tampered = copy.deepcopy(self.row)
        tampered["manifest"]["spec"]["sandbox"]["network"] = "allowed"
        with self.assertRaises(RowError):
            verify_row(self.public, tampered)

    def test_another_key_is_refused(self):
        _, other = admission_key()
        with self.assertRaises(RowError):
            verify_row(other, self.row)

    def test_missing_or_extra_fields_are_refused(self):
        with self.assertRaises(RowError):
            verify_row(self.public, {k: v for k, v in self.row.items() if k != "evidence"})
        with self.assertRaises(RowError):
            verify_row(self.public, dict(self.row, extra=1))
        with self.assertRaises(RowError):
            sign_row(self.private, dict(synthetic_fields("beta"), extra=1), seq=1, prev=GENESIS)

    def test_the_row_hash_covers_the_signature(self):
        other = dict(self.row, sig=sign_row(self.private, synthetic_fields("alpha"), seq=1, prev=GENESIS)["sig"])
        # Ed25519 is deterministic, so re-signing the same fields gives the same signature and hash
        self.assertEqual(row_hash(other), row_hash(self.row))
        self.assertNotEqual(row_hash(dict(self.row, sig="AAAA")), row_hash(self.row))


class ChainTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.private, self.public = admission_key()
        self.store = SQLiteStore(Path(self._tmp.name) / "data")
        add_rows(self.store, self.private, [synthetic_fields(f"tool-{i}") for i in range(5)])

    def tearDown(self):
        self.store.close()
        self._tmp.cleanup()

    def sql(self, statement, *params):
        conn = sqlite3.connect(self.store.path)
        with conn:
            conn.execute(statement, params)
        conn.close()

    def test_an_intact_store_verifies_against_its_checkpoint(self):
        seq, head = self.store.head()
        checkpoint = sign_checkpoint(self.private, seq, head)
        self.assertEqual(verify_store(self.store, self.public, checkpoint), 5)

    def test_an_older_checkpoint_still_verifies_as_a_prefix(self):
        rows = self.store.rows_from(1, 3)
        checkpoint = sign_checkpoint(self.private, 3, row_hash(rows[-1]))
        self.assertEqual(verify_store(self.store, self.public, checkpoint), 5)

    def test_a_deleted_row_is_detected(self):
        self.sql("DELETE FROM rows WHERE seq = 3")
        with self.assertRaises(RowError):
            verify_store(self.store, self.public)

    def test_a_deleted_last_row_is_detected_by_the_checkpoint(self):
        seq, head = self.store.head()
        checkpoint = sign_checkpoint(self.private, seq, head)
        self.sql("DELETE FROM rows WHERE seq = 5")
        with self.assertRaises(RowError):
            verify_store(self.store, self.public, checkpoint)

    def test_an_edited_row_body_is_detected(self):
        self.sql("UPDATE rows SET body = replace(body, '\"Validated\"', '\"Tested\"') WHERE seq = 2")
        with self.assertRaises(RowError):
            verify_store(self.store, self.public)

    def test_a_forged_checkpoint_is_refused(self):
        other, _ = admission_key()
        seq, head = self.store.head()
        with self.assertRaises(RowError):
            verify_checkpoint(self.public, sign_checkpoint(other, seq, head))

    def test_a_checkpoint_claiming_more_rows_than_exist_is_refused(self):
        _, head = self.store.head()
        with self.assertRaises(RowError):
            verify_store(self.store, self.public, sign_checkpoint(self.private, 6, head))

    def test_an_empty_store_verifies_at_genesis(self):
        empty = MemoryStore(Path(self._tmp.name) / "code2")
        self.assertEqual(verify_store(empty, self.public, sign_checkpoint(self.private, 0, GENESIS)), 0)


if __name__ == "__main__":
    unittest.main()
