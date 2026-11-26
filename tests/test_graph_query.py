"""T-001 graph-query, preparatory slice: the client-independent core, with a double injected only by tests.

Nothing here is T-001 exit. The manifest stays 1.0.0 Declared with no implementation, there is no conformance
file and no evidence, and the contract test against a published schema is skipped with its reason.
"""

import importlib.util
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

import jsonschema
import yaml

from fixture_support import REPO

MAIN = REPO / "drafts" / "graph-query" / "python" / "main.py"
PINNED = json.loads((REPO / "tests" / "fixtures" / "graph_query_schema.json").read_text(encoding="utf-8"))["parameters"]
MANIFEST = REPO / "catalog" / "tool" / "graph-query" / "manifest.yaml"
SECRET = "S3CR3T-credential-value"
CONTEXT_TEXT = "context-internal-stack-trace-do-not-copy"


def load_module():
    spec = importlib.util.spec_from_file_location("graph_query_main", MAIN)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


gq = load_module()


class Double(gq.ContextClient):
    """Records calls and returns a canned reply or raises a canned failure."""

    def __init__(self, reply=None, error=None):
        self.reply, self.error, self.calls = reply, error, []

    def call_tool(self, name, arguments):
        self.calls.append((name, dict(arguments)))
        if self.error is not None:
            raise self.error
        return self.reply


def reply(results=None, truncated=False, **extra):
    return json.dumps({"results": results if results is not None else [], "truncated": truncated, **extra})


def good(**kwargs):
    base = {"repoId": "repo-a", "operation": "find_symbol", "symbol": "parse"}
    base.update(kwargs)
    return base


def request(data, **extra):
    return {"root": "/work", "input": data, **extra}


class SchemaTest(unittest.TestCase):
    def test_the_manifest_declares_the_pinned_context_schema(self):
        declared = yaml.safe_load(MANIFEST.read_text(encoding="utf-8"))["spec"]["inputSchema"]
        self.assertEqual(declared["type"], PINNED["type"])
        self.assertEqual(declared["additionalProperties"], PINNED["additionalProperties"])
        self.assertEqual(declared["required"], PINNED["required"])
        self.assertEqual(set(declared["properties"]), set(PINNED["properties"]))
        for name, schema in PINNED["properties"].items():
            with self.subTest(name=name):
                for key in ("type", "enum", "minimum", "maximum"):
                    self.assertEqual(declared["properties"][name].get(key), schema.get(key))

    def test_the_wrapper_constants_match_the_pinned_schema(self):
        self.assertEqual(set(gq.PROPERTIES), set(PINNED["properties"]))
        self.assertEqual(tuple(PINNED["properties"]["operation"]["enum"]), gq.OPERATIONS)
        self.assertEqual((PINNED["properties"]["limit"]["minimum"], PINNED["properties"]["limit"]["maximum"]),
                         (gq.LIMIT_MIN, gq.LIMIT_MAX))
        self.assertEqual(tuple(PINNED["required"]), gq.REQUIRED)

    def test_the_wrapper_never_accepts_what_the_schema_rejects_and_differs_only_by_the_listed_context_checks(self):
        validator = jsonschema.Draft202012Validator(PINNED)
        context_only = [  # the schema allows these; Context's own validateQuery rejects them (types.ts, neo4j-query.ts)
            good(operation="get_callers", symbol=None) | {"symbol": ""}, {"repoId": "", "operation": "resolve"},
            {"repoId": "  ", "operation": "resolve"}, {"repoId": "r" * 257, "operation": "resolve"},
            good(commitSHA="abc"), good(commitSHA="A" * 40), good(commitSHA="g" * 40), good(commitSHA="a" * 41),
            good(commitSHA=""), {"repoId": "r", "operation": "find_symbol"}, {"repoId": "r", "operation": "get_callers"},
            {"repoId": "r", "operation": "get_callees", "symbol": "  "}, good(symbol="x" * 1025),
            good(symbol="bad\nline"), {"repoId": "bad\tid", "operation": "resolve"}, good(commitSHA="a" * 40 + "\n"),
            {"repoId": "\ufeff", "operation": "resolve"},  # JS trim() removes U+FEFF (review 2a8df5d1, finding 1.2)
            {"repoId": "\u00a0\u2003\u3000", "operation": "resolve"},
        ]
        both_ok = [
            good(), good(limit=1), good(limit=20), good(commitSHA="a" * 40), good(commitSHA="b" * 64), good(symbol="x" * 1024),
            {"repoId": "r" * 256, "operation": "resolve"}, {"repoId": "r", "operation": "resolve"},
            {"repoId": "r", "operation": "resolve", "symbol": "ignored by resolve"},
            {"repoId": "r", "operation": "get_callers", "symbol": "x", "limit": 20}, good(limit=2.0),
            {"repoId": "\u200b", "operation": "resolve"},  # U+200B is not whitespace for JS trim()
            {"repoId": "\x85", "operation": "resolve"},  # nor is U+0085: Context accepts it, so the wrapper must too
            {"repoId": "\ufeffa", "operation": "resolve"},
        ]
        schema_rejects = [
            {}, good(limit=0), good(limit=21), good(limit=None), good(limit=True), good(limit="3"), good(commitSHA=5), good(symbol=None),
            good(operation="raw_cypher"), good(extra=1), {"repoId": "r"}, {"operation": "resolve"},
            {"repoId": 5, "operation": "resolve"}, [], "text", None, 5,
        ]

        def wrapper_accepts(data):
            try:
                gq.validate_input(data)
                return True
            except gq.ToolError as exc:
                self.assertEqual(exc.code, "InvalidInput")
                return False
        for data in both_ok:
            with self.subTest(kind="both accept", data=str(data)[:60]):
                self.assertTrue(validator.is_valid(data))
                self.assertTrue(wrapper_accepts(data))
        for data in schema_rejects:
            with self.subTest(kind="schema rejects", data=str(data)[:60]):
                self.assertFalse(validator.is_valid(data))
                self.assertFalse(wrapper_accepts(data))
        for data in context_only:
            with self.subTest(kind="context only", data=str(data)[:60]):
                self.assertTrue(validator.is_valid(data))
                self.assertFalse(wrapper_accepts(data))

    def test_lengths_are_counted_in_utf16_units_like_context(self):
        astral = "\U0001F600"  # one code point, two UTF-16 units
        self.assertTrue(gq.validate_input({"repoId": astral * 128, "operation": "resolve"}))
        with self.assertRaises(gq.ToolError):
            gq.validate_input({"repoId": astral * 129, "operation": "resolve"})  # 258 units > 256 though 129 code points


class InputRejectionTest(unittest.TestCase):
    def test_a_rejected_input_never_reaches_the_client(self):
        client = Double(reply())
        for data in ({}, good(operation="x"), good(limit=99), good(extra=1), None, "s"):
            with self.subTest(data=str(data)[:40]):
                with self.assertRaises(gq.ToolError) as ctx:
                    gq.run(request(data), client)
                self.assertEqual(ctx.exception.code, "InvalidInput")
        self.assertEqual(client.calls, [])

    def test_the_error_names_problems_but_never_echoes_a_value_or_an_unknown_name(self):
        for data in (good(operation=SECRET), good(limit=SECRET), {SECRET: 1, "repoId": "r", "operation": "resolve"},
                     {"repoId": SECRET, "operation": 5}):
            with self.subTest(data=str(data)[:30]):
                with self.assertRaises(gq.ToolError) as ctx:
                    gq.run(request(data), Double(reply()))
                self.assertNotIn(SECRET, ctx.exception.message())


class ContextSemanticTest(unittest.TestCase):
    def test_context_semantic_failures_never_reach_the_client(self):
        client = Double(reply())
        for data in ({"repoId": "", "operation": "resolve"}, {"repoId": "r", "operation": "find_symbol"},
                     good(commitSHA="abc"), good(commitSHA="A" * 40), good(symbol="")):
            with self.subTest(data=str(data)[:50]):
                with self.assertRaises(gq.ToolError) as ctx:
                    gq.run(request(data), client)
                self.assertEqual(ctx.exception.code, "InvalidInput")
        self.assertEqual(client.calls, [])

    def test_the_messages_name_the_problem_but_not_the_value(self):
        for data, phrase in (({"repoId": "r", "operation": "get_callers"}, "symbol is required"),
                             (good(commitSHA="abc" + SECRET[:1]), "commitSHA must be a full lowercase"),
                             ({"repoId": " ", "operation": "resolve"}, "repoId must be nonempty")):
            with self.subTest(phrase=phrase):
                with self.assertRaises(gq.ToolError) as ctx:
                    gq.validate_input(data)
                self.assertIn(phrase, ctx.exception.message())

    def test_resolve_does_not_need_a_symbol_and_a_full_sha_of_either_length_is_valid(self):
        for data in ({"repoId": "r", "operation": "resolve"}, good(commitSHA="a" * 40), good(commitSHA="f" * 64)):
            self.assertTrue(gq.validate_input(data))


class OperationsTest(unittest.TestCase):
    def test_every_operation_goes_through_the_double_and_returns_its_json(self):
        for operation in gq.OPERATIONS:
            for truncated in (False, True):
                with self.subTest(operation=operation, truncated=truncated):
                    client = Double(reply([{"localId": "n1", "label": "parse"}], truncated, repoId="repo-a"))
                    out = gq.run(request(good(operation=operation, limit=5)), client)
                    parsed = json.loads(out)
                    self.assertEqual(parsed["results"], [{"localId": "n1", "label": "parse"}])
                    self.assertIs(parsed["truncated"], truncated)
                    self.assertEqual(client.calls[0][0], "graph_query")
                    self.assertEqual(client.calls[0][1]["operation"], operation)

    def test_only_declared_properties_are_passed_on_and_values_are_unchanged(self):
        client = Double(reply())
        gq.run(request(good(commitSHA="b" * 40, limit=7)), client)
        self.assertEqual(client.calls[0][1], good(commitSHA="b" * 40, limit=7))

    def test_a_reply_without_truncated_is_accepted_as_not_truncated(self):
        out = gq.run(request(good()), Double(json.dumps({"results": []})))
        self.assertEqual(json.loads(out), {"results": []})


class ReplyCheckTest(unittest.TestCase):
    def failure(self, text):
        with self.assertRaises(gq.ToolError) as ctx:
            gq.run(request(good()), Double(text))
        return ctx.exception.code

    def test_a_bad_reply_is_one_closed_code(self):
        for text in ("not json", "", "[]", "5", "null", json.dumps({"results": "x"}), json.dumps({"truncated": False}),
                     json.dumps({"results": [], "truncated": "yes"}), '{"results": [NaN]}', None, 5, b"{}"):
            with self.subTest(reply=str(text)[:30]):
                self.assertEqual(self.failure(text), "ContextBadReply")

    def test_an_oversized_reply_is_refused_not_sliced(self):
        big = reply([{"x": "a" * 1000}] * 100)
        self.assertGreater(len(big.encode()), gq.MAX_REPLY_BYTES)
        self.assertEqual(self.failure(big), "OutputTooLarge")

    def test_a_lone_surrogate_is_a_bad_reply_not_an_outage(self):
        """review 2a8df5d1 finding 1.1: it used to crash with UnicodeEncodeError and be reported as ContextUnavailable."""
        for text in ('{"results": ["\\ud800"], "truncated": false}', '{"results": [], "truncated": false, "\\udc00": 1}',
                     '{"results": ["' + "\ud800" + '"]}'):
            with self.subTest(reply=text[:30]):
                self.assertEqual(self.failure(text), "ContextBadReply")

    def test_a_duplicate_key_is_a_bad_reply(self):
        """finding 1.3: parsers disagree on which duplicate wins."""
        for text in ('{"truncated": true, "results": [], "truncated": false}', '{"results": [], "results": [1]}',
                     '{"results": [{"a": 1, "a": 2}]}'):
            with self.subTest(reply=text[:30]):
                self.assertEqual(self.failure(text), "ContextBadReply")

    def test_excessive_nesting_is_a_bad_reply_not_an_outage(self):
        for text in ("[" * 30000 + "]" * 30000, '{"results": ' + "[" * 20000 + "]" * 20000 + "}"):
            with self.subTest(depth=len(text)):
                self.assertEqual(self.failure(text), "ContextBadReply")

    def test_an_oversized_reply_is_refused_before_it_is_parsed(self):
        # not JSON at all, but too big: the size is checked first, so the answer is OutputTooLarge, not ContextBadReply
        self.assertEqual(self.failure("x" * (gq.MAX_REPLY_BYTES + 1)), "OutputTooLarge")

    def test_a_reply_at_the_bound_is_accepted(self):
        pad = gq.MAX_REPLY_BYTES - len(json.dumps({"results": [""], "truncated": False}, separators=(",", ":")))
        text = json.dumps({"results": ["a" * pad], "truncated": False}, separators=(",", ":"))
        self.assertLessEqual(len(text.encode()), gq.MAX_REPLY_BYTES)
        self.assertEqual(json.loads(gq.run(request(good()), Double(text)))["truncated"], False)


class ClientFailureTest(unittest.TestCase):
    def test_each_client_failure_maps_to_its_closed_code_and_no_context_text_escapes(self):
        for code in ("ContextUnavailable", "ContextUnauthorized", "RepoNotEnabled", "ContextBadReply", "OutputTooLarge"):
            with self.subTest(code=code):
                with self.assertRaises(gq.ToolError) as ctx:
                    gq.run(request(good()), Double(error=gq.ContextCallError(code)))
                self.assertEqual(ctx.exception.code, code)

    def test_an_unknown_code_or_an_arbitrary_exception_never_leaks_its_text(self):
        for error, expected in ((gq.ContextCallError("Weird " + CONTEXT_TEXT), "ContextBadReply"),
                                (RuntimeError(CONTEXT_TEXT), "ContextUnavailable"), (OSError(CONTEXT_TEXT), "ContextUnavailable")):
            with self.subTest(error=type(error).__name__):
                with self.assertRaises(gq.ToolError) as ctx:
                    gq.run(request(good()), Double(error=error))
                self.assertEqual(ctx.exception.code, expected)
                self.assertNotIn(CONTEXT_TEXT, ctx.exception.message())

    def test_the_error_codes_are_exactly_the_closed_list(self):
        self.assertEqual(gq.ERROR_CODES, ("InvalidInput", "ContextUnavailable", "ContextUnauthorized", "RepoNotEnabled",
                                          "ContextBadReply", "OutputTooLarge"))


class CredentialTest(unittest.TestCase):
    """review 49043e3b: a credential is never read from the tool's stdin; how a brokered one arrives is undefined."""

    def test_the_client_interface_has_no_credential_parameter(self):
        import inspect
        self.assertEqual(list(inspect.signature(gq.ContextClient.call_tool).parameters), ["self", "name", "arguments"])
        self.assertEqual(list(inspect.signature(gq.UnavailableClient.call_tool).parameters), ["self", "name", "arguments"])

    def test_credential_looking_fields_on_the_request_are_ignored_and_never_forwarded(self):
        client = Double(reply([{"label": "x"}]))
        out = gq.run(request(good(), credential=SECRET, token=SECRET, authorization="Bearer " + SECRET), client)
        self.assertEqual(client.calls, [("graph_query", good())])
        self.assertNotIn(SECRET, out)

    def test_a_credential_inside_the_input_is_an_unknown_property_and_is_not_echoed(self):
        client = Double(reply())
        with self.assertRaises(gq.ToolError) as ctx:
            gq.run(request(good(credential=SECRET)), client)
        self.assertEqual(ctx.exception.code, "InvalidInput")
        self.assertNotIn(SECRET, ctx.exception.message())
        self.assertEqual(client.calls, [])

    def test_the_source_does_not_read_a_credential_field(self):
        source = MAIN.read_text(encoding="utf-8")
        self.assertNotIn('.get("credential")', source)
        self.assertNotIn("['credential']", source)
        self.assertNotIn('["credential"]', source)

    def test_neither_the_manifest_nor_the_fixtures_hold_a_credential(self):
        for path in (MANIFEST, REPO / "tests" / "fixtures" / "graph_query_schema.json"):
            self.assertNotIn(SECRET, path.read_text(encoding="utf-8"))


class ProcessTest(unittest.TestCase):
    """The real process: production has only the unavailable client, and nothing selects another."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name).resolve()

    def tearDown(self):
        self._tmp.cleanup()

    def call(self, payload, env=None, stdin=None):
        data = stdin if stdin is not None else json.dumps(payload).encode()
        done = subprocess.run([sys.executable, str(MAIN)], input=data, capture_output=True, timeout=30,
                              env=dict(os.environ, **(env or {})), cwd=str(self.root))
        self.assertEqual(done.returncode, 0)
        lines = done.stdout.decode().splitlines()
        self.assertEqual(len(lines), 1)
        return json.loads(lines[0])

    def test_a_valid_call_is_context_unavailable_in_production(self):
        self.assertEqual(self.call({"root": str(self.root), "input": good()}), {"ok": False, "error": "ContextUnavailable"})

    def test_an_invalid_call_is_invalid_input(self):
        result = self.call({"root": str(self.root), "input": good(limit=99)})
        self.assertFalse(result["ok"])
        self.assertTrue(result["error"].startswith("InvalidInput"))

    def test_bad_stdin_is_an_error_result_not_a_crash(self):
        for raw in (b"", b"not json", b"[]", b"\xff\xfe"):
            with self.subTest(raw=raw):
                self.assertFalse(self.call(None, stdin=raw)["ok"])

    def test_no_environment_or_file_can_select_another_client(self):
        planted = {"results": [{"label": "planted"}], "truncated": False}
        for name in ("fixture.json", "context-fixture.json", ".context-fixture.json", "reply.json"):
            (self.root / name).write_text(json.dumps(planted), encoding="utf-8")
        env = {key: str(self.root / "fixture.json") for key in (
            "TAFFEE_CONTEXT_FIXTURE", "TAFFEE_GRAPH_QUERY_TRANSPORT", "TAFFEE_CONTEXT_TRANSPORT", "GRAPH_QUERY_FIXTURE",
            "TAFFEE_CONTEXT_URL", "MCP_URL")}
        env["TAFFEE_GRAPH_QUERY_TRANSPORT"] = "fixture"
        result = self.call({"root": str(self.root), "input": good(), "client": "fixture", "transport": "fixture"}, env=env)
        self.assertEqual(result, {"ok": False, "error": "ContextUnavailable"})

    def test_the_source_has_no_way_to_read_a_fixture_or_the_network(self):
        source = MAIN.read_text(encoding="utf-8")
        import re
        for pattern in (r"\benviron\b", r"\bgetenv\b", r"\bopen\(", r"\bsocket\b", r"\burllib\b", r"\bhttp\.client\b",
                        r"\bsubprocess\b", r"^\s*import os\b", r"^\s*from os\b"):
            with self.subTest(pattern=pattern):
                self.assertIsNone(re.search(pattern, source, re.M))


class PreparatoryStateTest(unittest.TestCase):
    """The slice is deliberately not wired: nothing resolves it, nothing labels it, no evidence exists."""

    def test_the_manifest_is_still_1_0_0_declared_with_no_implementation_and_idempotent_false(self):
        manifest = yaml.safe_load(MANIFEST.read_text(encoding="utf-8"))
        self.assertEqual(manifest["version"], "1.0.0")
        self.assertEqual(manifest["implementations"], [])
        self.assertIs(manifest["spec"]["idempotent"], False)  # CAP-T-12: an omitted commitSHA resolves latest at call time
        self.assertIs(manifest["spec"]["readOnly"], True)

    def test_there_is_no_conformance_file_and_no_evidence_for_graph_query(self):
        self.assertFalse((REPO / "library" / "graph-query").exists())  # nothing under library/, so no digest, index or catalog changes
        self.assertFalse((REPO / "evidence" / "tools" / "graph-query").exists())

    def test_the_evidence_step_refuses_to_write_a_record_for_graph_query(self):
        with tempfile.TemporaryDirectory() as tmp:
            done = subprocess.run([sys.executable, "-m", "capability.evidence", "write", str(REPO), "--tool", "graph-query"],
                                  capture_output=True, text=True, cwd=str(REPO), timeout=120,
                                  env=dict(os.environ, PYTHONPATH=str(REPO), TMPDIR=tmp))
        self.assertEqual(done.returncode, 2)
        self.assertFalse((REPO / "evidence" / "tools" / "graph-query").exists())

    def test_the_ledger_still_lists_only_1_0_0(self):
        ledger = yaml.safe_load((REPO / "catalog" / "tool" / "graph-query" / "versions.yaml").read_text(encoding="utf-8"))
        self.assertEqual([entry["version"] for entry in ledger["versions"]], ["1.0.0"])

    @unittest.skip("Context publishes no graph_query tool yet (T-001 SPEC 5.3). When B exists this test fetches the "
                   "published tool list and compares its schema to the pinned fixture. The skip counts against evidence.")
    def test_the_published_context_schema_matches_the_pinned_one(self):
        raise AssertionError("not written until Context publishes graph_query")


if __name__ == "__main__":
    unittest.main()
