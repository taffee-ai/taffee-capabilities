"""S-018 R2: the CapabilityStore contract. Every store runs this whole suite (REG-STORE-3).

A new database is supported when a subclass of StoreContract for it passes. Today: SQLiteStore, MemoryStore.
"""

import tempfile
import threading
import unittest
from pathlib import Path

from capability.digest import tree_digest
from capability.rows import GENESIS, row_hash, sign_row
from capability.store import MemoryStore, SQLiteStore, StoreConflict, StoreError
from capability.store.base import words

from registry_support import add_rows, admission_key, synthetic_fields


class StoreContract:
    """Mixed into a TestCase with `make_store(path)`."""

    def make_store(self, path):
        raise NotImplementedError

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.base = Path(self._tmp.name)
        self.store = self.make_store(self.base / "s")
        self.private, self.public = admission_key()

    def tearDown(self):
        self.store.close()
        self._tmp.cleanup()

    def add(self, *fields):
        return add_rows(self.store, self.private, list(fields))

    # -- append and head --
    def test_an_empty_store_has_the_genesis_head(self):
        self.assertEqual(self.store.head(), (0, GENESIS))
        self.assertEqual(self.store.count(), 0)

    def test_append_moves_the_head_to_the_new_row(self):
        self.add(synthetic_fields("alpha"))
        row = self.store.get_row("tool", "alpha", "1.0.0")
        self.assertEqual(self.store.head(), (1, row_hash(row)))

    def test_an_append_not_at_the_head_is_refused(self):
        self.add(synthetic_fields("alpha"))
        stale = sign_row(self.private, synthetic_fields("beta"), seq=1, prev=GENESIS)
        with self.assertRaises(StoreConflict):
            self.store.append_row(stale, row_hash(stale))
        skipped = sign_row(self.private, synthetic_fields("beta"), seq=3, prev=self.store.head()[1])
        with self.assertRaises(StoreConflict):
            self.store.append_row(skipped, row_hash(skipped))
        self.assertEqual(self.store.count(), 1)

    def test_a_duplicate_name_and_version_is_refused(self):
        self.add(synthetic_fields("alpha"))
        seq, head = self.store.head()
        again = sign_row(self.private, synthetic_fields("alpha", summary="Other."), seq=seq + 1, prev=head)
        with self.assertRaises(StoreConflict):
            self.store.append_row(again, row_hash(again))
        self.assertEqual(self.store.count(), 1)

    def test_a_stored_row_comes_back_unchanged(self):
        self.add(synthetic_fields("alpha"))
        seq, head = self.store.head()
        row = self.store.get_row("tool", "alpha", "1.0.0")
        self.assertEqual(row_hash(row), head)
        row["label"] = "Tested"
        self.assertEqual(self.store.get_row("tool", "alpha", "1.0.0")["label"], "Validated")

    def test_concurrent_appends_keep_one_chain(self):
        errors = []

        def worker(n):
            from capability.admission import append_signed
            try:
                append_signed(self.store, self.private, synthetic_fields(f"t{n}"), attempts=50)
            except Exception as exc:  # pragma: no cover - reported below
                errors.append(exc)

        threads = [threading.Thread(target=worker, args=(n,)) for n in range(8)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertEqual(errors, [])
        rows = self.store.rows_from(1, 100)
        self.assertEqual([r["seq"] for r in rows], list(range(1, 9)))
        prev = GENESIS
        for row in rows:
            self.assertEqual(row["prev"], prev)
            prev = row_hash(row)

    # -- reads --
    def test_get_row_misses_cleanly(self):
        self.assertIsNone(self.store.get_row("tool", "nope", "1.0.0"))

    def test_versions_are_in_semantic_order_not_admission_order(self):
        self.add(synthetic_fields("alpha", "1.10.0"), synthetic_fields("alpha", "1.2.0"), synthetic_fields("alpha", "1.9.3"))
        self.assertEqual([r["version"] for r in self.store.versions("tool", "alpha")], ["1.2.0", "1.9.3", "1.10.0"])

    def test_pages_follow_name_then_version_and_resume_after_the_cursor(self):
        self.add(synthetic_fields("bravo"), synthetic_fields("alpha", "2.0.0"), synthetic_fields("alpha", "1.0.0"),
                 synthetic_fields("charlie"))
        first = self.store.rows_page("tool", None, 2)
        self.assertEqual([(r["name"], r["version"]) for r in first], [("alpha", "1.0.0"), ("alpha", "2.0.0")])
        rest = self.store.rows_page("tool", ("alpha", "2.0.0"), 10)
        self.assertEqual([r["name"] for r in rest], ["bravo", "charlie"])

    def test_pages_apply_the_label_floor(self):
        self.add(synthetic_fields("alpha", label="Declared"), synthetic_fields("bravo", label="Tested"))
        self.assertEqual([r["name"] for r in self.store.rows_page("tool", None, 10, min_rank=2)], ["bravo"])

    def test_rows_from_is_chain_order(self):
        self.add(synthetic_fields("bravo"), synthetic_fields("alpha"))
        self.assertEqual([r["name"] for r in self.store.rows_from(1, 10)], ["bravo", "alpha"])
        self.assertEqual([r["name"] for r in self.store.rows_from(2, 10)], ["alpha"])

    # -- search --
    def search(self, text, **kwargs):
        return [r["name"] for r in self.store.search(words(text), "tool", kwargs.pop("limit", 10), **kwargs)]

    def test_search_requires_every_term_and_matches_prefixes(self):
        self.add(synthetic_fields("word-count", summary="Count the words in a text."),
                 synthetic_fields("line-count", summary="Count the lines in a file."),
                 synthetic_fields("grep", summary="Search file contents for a pattern."))
        self.assertEqual(sorted(self.search("count")), ["line-count", "word-count"])
        self.assertEqual(self.search("count word"), ["word-count"])
        self.assertEqual(self.search("cont pat"), ["grep"])  # prefixes of contents, pattern
        self.assertEqual(self.search("nothing-like-this"), [])

    def test_search_covers_parameter_descriptions(self):
        self.add(synthetic_fields("alpha", params={"path": "A repository file to inspect."}))
        self.assertEqual(self.search("repository inspect"), ["alpha"])

    def test_an_exact_name_match_ranks_first(self):
        self.add(synthetic_fields("count-helper", summary="Count count count words count."),
                 synthetic_fields("count", summary="A tool."))
        self.assertEqual(self.search("count")[0], "count")

    def test_search_offers_only_the_newest_version_of_a_name(self):
        self.add(synthetic_fields("alpha", "1.0.0", summary="Old words."), synthetic_fields("alpha", "2.0.0", summary="New words."),
                 synthetic_fields("alpha", "1.5.0", summary="Middle words."))
        found = self.store.search(["words"], "tool", 10)
        self.assertEqual([(r["name"], r["version"]) for r in found], [("alpha", "2.0.0")])
        self.assertEqual(self.search("old"), [])

    def test_search_filters_by_label_and_language(self):
        self.add(synthetic_fields("alpha", label="Declared", summary="Shared word."),
                 synthetic_fields("bravo", label="Validated", summary="Shared word.", languages=("bash",)))
        self.assertEqual(self.search("shared", min_rank=1), ["bravo"])
        self.assertEqual(self.search("shared", language="python"), ["alpha"])
        self.assertEqual(self.search("shared", language="py"), [])  # a language is matched whole, never as a prefix

    def test_search_pages_by_offset(self):
        self.add(*[synthetic_fields(f"tool-{i:02d}", summary="Common word.") for i in range(7)])
        page1, page2 = self.search("common", limit=4), self.search("common", limit=4, offset=4)
        self.assertEqual(len(page1), 4)
        self.assertEqual(len(page2), 3)
        self.assertFalse(set(page1) & set(page2))

    def test_query_syntax_never_reaches_the_store(self):
        self.add(synthetic_fields("alpha", summary="Plain words."))
        for hostile in ('" OR 1=1 --', "name:alpha", "NEAR(plain words)", "*", "alpha*", "'); DROP TABLE rows; --"):
            with self.subTest(query=hostile):
                self.store.search(words(hostile), "tool", 10)  # never raises a syntax error
        self.assertEqual(self.store.search(["plain", '"'], "tool", 10), [])  # a non-word term matches nothing
        self.assertEqual(self.store.count(), 1)

    def test_an_empty_query_matches_nothing(self):
        self.add(synthetic_fields("alpha"))
        self.assertEqual(self.store.search([], "tool", 10), [])

    # -- code --
    def test_code_is_named_by_its_tree_digest_and_stored_once(self):
        files = {"main.py": b"print(1)\n", "lib/util.py": b"X = 1\n"}
        digest = self.store.put_code(files)
        folder = self.store.code_path(digest)
        self.assertEqual(tree_digest(folder), digest)
        self.assertEqual(self.store.put_code(dict(files)), digest)
        self.assertEqual((folder / "lib" / "util.py").read_bytes(), b"X = 1\n")

    def test_unsafe_code_paths_are_refused(self):
        for name in ("../escape.py", "/abs.py", "a/../../b.py", "", "dir/", "a\\b.py"):
            with self.subTest(name=name):
                with self.assertRaises(StoreError):
                    self.store.put_code({name: b"x"})

    def test_an_unknown_or_malformed_digest_has_no_folder(self):
        for digest in ("sha256:" + "0" * 64, "sha256:../../etc", "md5:abc", None):
            with self.subTest(digest=digest):
                self.assertIsNone(self.store.code_path(digest))

    # -- submissions --
    def test_submissions_are_saved_replaced_and_listed_by_state(self):
        self.store.save_submission({"id": "sub-b", "state": "received"})
        self.store.save_submission({"id": "sub-a", "state": "received"})
        self.store.save_submission({"id": "sub-b", "state": "rejected"})
        self.assertEqual(self.store.get_submission("sub-b")["state"], "rejected")
        self.assertEqual([r["id"] for r in self.store.submissions("received")], ["sub-a"])
        self.assertEqual([r["id"] for r in self.store.submissions()], ["sub-a", "sub-b"])
        self.assertIsNone(self.store.get_submission("sub-zzz"))


class SQLiteStoreContractTest(StoreContract, unittest.TestCase):
    def make_store(self, path):
        return SQLiteStore(path)

    def test_the_store_survives_reopening(self):
        self.add(synthetic_fields("alpha"))
        head = self.store.head()
        self.store.close()
        again = SQLiteStore(self.base / "s")
        self.assertEqual(again.head(), head)
        self.assertEqual(again.search(["alpha"], "tool", 5)[0]["name"], "alpha")
        again.close()

    def test_no_lookup_scans_every_row(self):
        """REG-QRY-7: the time a lookup takes must not grow with the number of rows. SQLite's own plan for each
        query must use an index (SEARCH), never a full scan of the rows table (SCAN rows)."""
        import sqlite3
        self.add(synthetic_fields("alpha"), synthetic_fields("bravo"))
        conn = sqlite3.connect(self.store.path)
        queries = {
            "get_row": ("SELECT body FROM rows WHERE kind = ? AND name = ? AND version = ?", ("tool", "alpha", "1.0.0")),
            "versions": ("SELECT body FROM rows WHERE kind = ? AND name = ? ORDER BY major, minor, patch", ("tool", "alpha")),
            "head": ("SELECT seq, hash FROM rows WHERE seq = (SELECT max(seq) FROM rows)", ()),
            "rows_page": ("SELECT body FROM rows WHERE kind = ? AND label_rank >= ? AND (name > ? OR (name = ? AND "
                          "(major, minor, patch) > (?, ?, ?))) ORDER BY name, major, minor, patch LIMIT ?",
                          ("tool", 0, "a", "a", 1, 0, 0, 50)),
            "search": ("SELECT r.body FROM rows_fts f JOIN rows r ON r.seq = f.rowid WHERE rows_fts MATCH ? AND r.kind = ?"
                       " AND r.label_rank >= ? ORDER BY (r.name = ?) DESC, bm25(rows_fts, 10.0, 3.0, 1.0), r.name LIMIT ?",
                       ('"alpha"*', "tool", 0, "alpha", 10)),
            "find_submission": ("SELECT body FROM submissions WHERE submitter = ? AND sub_key = ? AND state != 'rejected'"
                                " ORDER BY submitted_at DESC, id DESC LIMIT 1", ("a", "k")),
        }
        for name, (sql, params) in queries.items():
            with self.subTest(query=name):
                plan = " | ".join(row[3] for row in conn.execute("EXPLAIN QUERY PLAN " + sql, params))
                self.assertNotRegex(plan, r"SCAN (rows|submissions)\b(?! USING)", plan)
        conn.close()

    def test_wal_mode_is_on(self):
        import sqlite3
        conn = sqlite3.connect(self.store.path)
        self.assertEqual(conn.execute("PRAGMA journal_mode").fetchone()[0], "wal")
        conn.close()

    # read-only opens: for every process that is not the store's one writer (verify, audits)
    def test_a_read_only_store_reads_while_the_writer_is_open(self):
        self.add(synthetic_fields("alpha"))
        reader = SQLiteStore(self.base / "s", read_only=True)
        self.assertEqual(reader.head(), self.store.head())
        self.assertEqual(reader.search(["alpha"], "tool", 5)[0]["name"], "alpha")
        reader.close()
        self.add(synthetic_fields("bravo"))  # the writer is unaffected by the reader having come and gone
        self.assertEqual(self.store.search(["bravo"], "tool", 5)[0]["name"], "bravo")

    def test_a_read_only_store_writes_nothing(self):
        import sqlite3
        self.add(synthetic_fields("alpha"))
        reader = SQLiteStore(self.base / "s", read_only=True)
        seq, head = reader.head()
        row = sign_row(self.private, synthetic_fields("bravo"), seq=seq + 1, prev=head)
        with self.assertRaises(sqlite3.OperationalError):  # "attempt to write a readonly database"
            reader.append_row(row, row_hash(row))
        reader.close()
        self.assertEqual(self.store.head(), (seq, head))

    def test_a_read_only_open_of_a_missing_store_creates_nothing(self):
        missing = self.base / "nothing-here"
        with self.assertRaises(StoreError):
            SQLiteStore(missing, read_only=True)
        self.assertFalse(missing.exists())


class MemoryStoreContractTest(StoreContract, unittest.TestCase):
    def make_store(self, path):
        return MemoryStore(path)


if __name__ == "__main__":
    unittest.main()
