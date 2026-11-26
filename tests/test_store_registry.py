"""S-018 R3: StoreRegistry, the verified query layer (REG-QRY-1 to 7, REG-ROW-4, REG-COMPAT-2)."""

import sqlite3
import tempfile
import unittest
from pathlib import Path

from capability.execute import run_tool
from capability.registry import RegistryError
from capability.store import SQLiteStore
from capability.store_registry import StoreRegistry

from registry_support import (
    LANGUAGES, WORD_COUNT, UnconfinedTestSandbox, add_rows, admission_key, fresh_revocation, synthetic_fields,
)


class StoreRegistryTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.base = Path(self._tmp.name)
        self.store = SQLiteStore(self.base / "data")
        self.private, self.public = admission_key()
        self.client, self.feed, self.clocks = fresh_revocation()
        self.registry = StoreRegistry(self.store, self.public, self.client, LANGUAGES)
        self.code = self.store.put_code({"main.py": WORD_COUNT.encode()})
        add_rows(self.store, self.private, [
            synthetic_fields("word-count", "1.0.0", summary="Count the words in text.", code_digests={"python": self.code},
                             params={"text": "Text whose words are counted."}),
            synthetic_fields("word-count", "1.1.0", summary="Count the words in text.", code_digests={"python": self.code},
                             params={"text": "Text whose words are counted."}),
            synthetic_fields("word-count", "2.0.0", label="Declared", summary="Count the words in text.",
                             code_digests={"python": self.code}, params={"text": "Text whose words are counted."}),
            synthetic_fields("line-count", summary="Count the lines in a file.", code_digests={"python": self.code}),
        ])

    def tearDown(self):
        self.store.close()
        self._tmp.cleanup()

    def code_of(self, call):
        with self.assertRaises(RegistryError) as ctx:
            call()
        return ctx.exception.code

    def sql(self, statement, *params):
        conn = sqlite3.connect(self.store.path)
        with conn:
            conn.execute(statement, params)
        conn.close()

    def test_a_registry_without_revocation_cannot_be_built(self):
        with self.assertRaises(ValueError):
            StoreRegistry(self.store, self.public, None, LANGUAGES)

    # list
    def test_list_pages_with_an_opaque_cursor(self):
        first = self.registry.list(limit=2)
        self.assertEqual([(i["name"], i["version"]) for i in first["items"]], [("line-count", "1.0.0"), ("word-count", "1.0.0")])
        second = self.registry.list(limit=2, cursor=first["next"])
        self.assertEqual([i["version"] for i in second["items"]], ["1.1.0", "2.0.0"])
        self.assertIsNone(self.registry.list(limit=2, cursor=second["next"])["next"])

    def test_list_refuses_bad_limits_and_cursors(self):
        for limit in (0, 201, True, "5"):
            with self.subTest(limit=limit):
                self.assertEqual(self.code_of(lambda: self.registry.list(limit=limit)), "InvalidRequest")
        for cursor in ("not-base64!", "WyJhIl0", "eyJxIjoxfQ"):
            with self.subTest(cursor=cursor):
                self.assertEqual(self.code_of(lambda: self.registry.list(cursor=cursor)), "InvalidRequest")

    # search
    def test_search_returns_model_ready_definitions(self):
        found = self.registry.search("count words")
        [item] = found["items"]
        self.assertEqual((item["name"], item["version"]), ("word-count", "2.0.0"))
        self.assertEqual(item["tool_definition"]["name"], "word-count")
        self.assertEqual(item["tool_definition"]["input_schema"]["properties"]["text"]["description"],
                         "Text whose words are counted.")

    def test_search_label_floor_uses_the_newest_version_only(self):
        self.assertEqual([i["name"] for i in self.registry.search("count", min_label="Validated")["items"]], ["line-count"])

    def test_a_search_cursor_belongs_to_its_query(self):
        page = self.registry.search("count", limit=1)
        self.assertIsNotNone(page["next"])
        self.assertEqual(len(self.registry.search("count", limit=1, cursor=page["next"])["items"]), 1)
        self.assertEqual(self.code_of(lambda: self.registry.search("lines", limit=1, cursor=page["next"])), "InvalidRequest")

    def test_search_leaves_out_known_revoked_versions_but_answers_when_stale(self):
        self.feed.publish([{"kind": "tool", "name": "line-count", "version": "1.0.0"}])
        self.client.poll(self.feed)
        self.assertEqual([i["name"] for i in self.registry.search("count")["items"]], ["word-count"])
        self.feed.up = False
        self.clocks.advance(31)
        self.assertEqual([i["name"] for i in self.registry.search("count words")["items"]], ["word-count"])

    # describe and versions
    def test_describe_gives_the_newest_or_the_named_version_and_no_paths(self):
        newest = self.registry.describe("word-count")
        self.assertEqual(newest["version"], "2.0.0")
        self.assertEqual(self.registry.describe("word-count", "1.0.0")["version"], "1.0.0")
        text = repr(newest)
        for leak in ("sandbox", "location", str(self.base), "library/"):
            self.assertNotIn(leak, text)
        self.assertEqual(self.code_of(lambda: self.registry.describe("absent")), "NotFound")
        self.assertEqual(self.code_of(lambda: self.registry.describe("word-count", "9.9.9")), "NotFound")

    def test_versions_list_every_version_in_order(self):
        self.assertEqual([v["version"] for v in self.registry.versions("word-count")], ["1.0.0", "1.1.0", "2.0.0"])

    def test_a_hidden_entry_answers_like_an_absent_one(self):
        hide = lambda ref: ref["name"] != "line-count"  # noqa: E731
        self.assertEqual(self.code_of(lambda: self.registry.describe("line-count", visible=hide)), "NotFound")
        self.assertEqual([i["name"] for i in self.registry.search("count", visible=hide)["items"]], ["word-count"])
        self.assertEqual(self.code_of(lambda: self.registry.resolve("line-count", languages=["python"], visible=hide)),
                         "NotFound")

    # tampering
    def test_an_edited_row_is_never_served(self):
        self.sql("UPDATE rows SET body = replace(body, '\"Declared\"', '\"Tested\"') WHERE version = '2.0.0'")
        self.assertEqual(self.code_of(lambda: self.registry.describe("word-count")), "IntegrityError")
        self.assertEqual(self.registry.search("words")["items"], [])
        self.assertEqual(self.code_of(lambda: self.registry.resolve("word-count", languages=["python"])), "IntegrityError")

    def test_a_tampered_filter_column_does_not_raise_a_label(self):
        self.sql("UPDATE rows SET label_rank = 4 WHERE version = '2.0.0'")
        self.assertEqual(self.registry.search("words", min_label="Tested")["items"], [])
        self.assertEqual(self.registry.list(min_label="Tested")["items"], [])

    def test_a_row_signed_by_another_key_is_refused(self):
        other, _ = admission_key()
        add_rows(self.store, other, [synthetic_fields("rogue", code_digests={"python": self.code})])
        self.assertEqual(self.code_of(lambda: self.registry.describe("rogue")), "IntegrityError")

    # resolve
    def test_resolve_picks_the_highest_version_in_range_at_the_floor(self):
        self.assertEqual(self.registry.resolve("word-count", languages=["python"])["version"], "2.0.0")
        self.assertEqual(self.registry.resolve("word-count", "^1.0.0", languages=["python"])["version"], "1.1.0")
        self.assertEqual(self.registry.resolve("word-count", minLabel="Validated", languages=["python"])["version"], "1.1.0")
        match = self.registry.resolve("word-count", "1.0.0", languages=["python"])
        self.assertEqual((match["revocation"], match["entry"]), ("checked", "main.py"))

    def test_resolve_refusals_are_typed(self):
        self.assertEqual(self.code_of(lambda: self.registry.resolve("absent", languages=["python"])), "NotFound")
        self.assertEqual(self.code_of(lambda: self.registry.resolve("word-count", "^3.0.0", languages=["python"])), "NotFound")
        self.assertEqual(self.code_of(lambda: self.registry.resolve("word-count", minLabel="Tested", languages=["python"])),
                         "BelowLabel")
        self.assertEqual(self.code_of(lambda: self.registry.resolve("word-count", languages=["bash"])), "NoImplementation")
        self.assertEqual(self.code_of(lambda: self.registry.resolve("word-count", "1.x", languages=["python"])),
                         "InvalidRange")

    def test_changed_or_missing_code_is_refused_at_resolve(self):
        folder = self.store.code_path(self.code)
        (folder / "main.py").write_text("print('changed')\n", encoding="utf-8")
        self.assertEqual(self.code_of(lambda: self.registry.resolve("word-count", languages=["python"])), "IntegrityError")
        import shutil
        shutil.rmtree(folder)
        self.assertEqual(self.code_of(lambda: self.registry.resolve("word-count", languages=["python"])), "IntegrityError")

    def test_resolve_fails_closed_on_revocation(self):
        self.feed.publish([{"kind": "tool", "name": "word-count", "version": "2.0.0"}])
        self.client.poll(self.feed)
        self.assertEqual(self.code_of(lambda: self.registry.resolve("word-count", languages=["python"])), "Revoked")
        self.feed.up = False
        self.clocks.advance(31)
        self.assertEqual(self.code_of(lambda: self.registry.resolve("word-count", "^1.0.0", languages=["python"])),
                         "RevocationStale")

    def test_a_caller_cannot_change_what_the_registry_holds(self):
        match = self.registry.resolve("word-count", languages=["python"])
        match["spec"]["timeoutMs"] = 1
        self.assertNotEqual(self.registry.resolve("word-count", languages=["python"])["spec"]["timeoutMs"], 1)

    # execution through run_tool (REG-COMPAT-2)
    def test_run_tool_runs_a_store_resolved_tool(self):
        sandbox = UnconfinedTestSandbox()
        project = self.base / "project"
        project.mkdir()
        result = run_tool(self.registry, "word-count", {"text": "a b c d"}, project_root=project, sandbox=sandbox,
                          range="^1.0.0")
        self.assertEqual(result, {"ok": True, "output": "4"})
        self.assertEqual(sandbox.calls[0]["argv"], ["python3", "/impl/main.py"])
        self.assertEqual(sandbox.calls[0]["policy"].filesystem, "read-root")


if __name__ == "__main__":
    unittest.main()
