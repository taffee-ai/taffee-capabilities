"""S-018 R6: the read-only MCP server (REG-MCP-1 to 3): in-process, and as a real stdio subprocess."""

import json
import os
import subprocess
import sys
import tempfile
import threading
import unittest
from pathlib import Path

from capability.admission import Admission
from capability.authz import DevAdmissionAuthorizer, Principal
from capability.mcp_server import PROTOCOL_VERSION, MCPServer, RestRegistry
from capability.registry_api import RegistryService, make_server
from capability.store import SQLiteStore
from capability.store_registry import StoreRegistry

from fixture_support import REPO
from registry_support import LANGUAGES, UnconfinedTestSandbox, add_rows, admission_key, fresh_revocation, synthetic_fields


def rpc(mid, method, params=None):
    message = {"jsonrpc": "2.0", "id": mid, "method": method}
    if params is not None:
        message["params"] = params
    return message


class MCPInProcessTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.store = SQLiteStore(Path(self._tmp.name) / "data")
        private, public = admission_key()
        add_rows(self.store, private, [
            synthetic_fields("word-count", summary="Count the words in text.", params={"text": "The text."}),
            synthetic_fields("secret-tool", summary="Count secret words."),
        ])
        client, _, _ = fresh_revocation()
        registry = StoreRegistry(self.store, public, client, LANGUAGES)
        self.server = MCPServer(registry, DevAdmissionAuthorizer(hidden={"tool:secret-tool:1.0.0"}), Principal("agent:ide"))

    def tearDown(self):
        self.store.close()
        self._tmp.cleanup()

    def call(self, name, arguments):
        return self.server.handle(rpc(1, "tools/call", {"name": name, "arguments": arguments}))["result"]

    def test_initialize_announces_tools_only(self):
        result = self.server.handle(rpc(1, "initialize", {"protocolVersion": PROTOCOL_VERSION, "capabilities": {},
                                                          "clientInfo": {"name": "t", "version": "1"}}))["result"]
        self.assertEqual(result["protocolVersion"], PROTOCOL_VERSION)
        self.assertEqual(result["capabilities"], {"tools": {"listChanged": False}})

    def test_the_three_tools_are_read_only(self):
        tools = self.server.handle(rpc(2, "tools/list"))["result"]["tools"]
        self.assertEqual(sorted(t["name"] for t in tools), ["describe_capability", "list_capabilities", "search_capabilities"])
        self.assertTrue(all(t["annotations"]["readOnlyHint"] for t in tools))

    def test_search_describe_and_list_answer_with_structured_content(self):
        found = self.call("search_capabilities", {"query": "count words"})
        self.assertFalse(found["isError"])
        self.assertEqual([i["name"] for i in found["structuredContent"]["items"]], ["word-count"])
        self.assertEqual(json.loads(found["content"][0]["text"]), found["structuredContent"])
        described = self.call("describe_capability", {"name": "word-count"})["structuredContent"]
        self.assertEqual(described["tool_definition"]["input_schema"]["properties"]["text"]["description"], "The text.")
        listed = self.call("list_capabilities", {})["structuredContent"]
        self.assertEqual([i["name"] for i in listed["items"]], ["word-count"])

    def test_visibility_is_the_same_as_rest(self):
        self.assertEqual(self.call("describe_capability", {"name": "secret-tool"}),
                         {"content": [{"type": "text", "text": '{"code": "NotFound"}'}],
                          "structuredContent": {"code": "NotFound"}, "isError": True})

    def test_nothing_but_discovery_can_be_called(self):
        for name in ("resolve", "run_tool", "submit", "approve"):
            with self.subTest(tool=name):
                self.assertEqual(self.server.handle(rpc(3, "tools/call", {"name": name, "arguments": {}}))["error"]["code"],
                                 -32602)

    def test_bad_messages_get_json_rpc_errors(self):
        self.assertEqual(self.server.handle({"id": 1})["error"]["code"], -32600)
        self.assertEqual(self.server.handle(rpc(1, "resources/list"))["error"]["code"], -32601)
        bad = rpc(1, "tools/call", {"name": "search_capabilities", "arguments": {"query": "x", "extra": 1}})
        self.assertEqual(self.server.handle(bad)["error"]["code"], -32602)
        self.assertIsNone(self.server.handle({"jsonrpc": "2.0", "method": "notifications/initialized"}))


class RestService:
    """A real /v2 service on a socket, over a store with two tools, one of them hidden from every caller."""

    def __init__(self):
        self._tmp = tempfile.TemporaryDirectory()
        base = Path(self._tmp.name)
        self.store = SQLiteStore(base / "data")
        private, public = admission_key()
        add_rows(self.store, private, [
            synthetic_fields("word-count", summary="Count the words in text.", params={"text": "The text."}),
            synthetic_fields("secret-tool", summary="Count secret words."),
        ])
        client, _, _ = fresh_revocation()
        self.registry = StoreRegistry(self.store, public, client, LANGUAGES)
        self.authz = DevAdmissionAuthorizer(hidden={"tool:secret-tool:1.0.0"})
        admission = Admission(self.store, private, UnconfinedTestSandbox(), LANGUAGES, base / "work")
        self.server = make_server(RegistryService(self.registry, admission, self.authz, private, process_inline=True))
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def close(self):
        self.server.shutdown()
        self.server.server_close()
        self.store.close()
        self._tmp.cleanup()


class MCPOverRestTest(unittest.TestCase):
    """The deployed MCP server answers through REST (RestRegistry), never from the store file."""

    @classmethod
    def setUpClass(cls):
        cls.service = RestService()
        principal = Principal("agent:ide")
        cls.rest = MCPServer(RestRegistry(cls.service.url, principal), DevAdmissionAuthorizer(), principal)
        cls.direct = MCPServer(cls.service.registry, cls.service.authz, principal)

    @classmethod
    def tearDownClass(cls):
        cls.service.close()

    def both(self, name, arguments):
        message = rpc(1, "tools/call", {"name": name, "arguments": arguments})
        return self.rest.handle(message)["result"], self.direct.handle(message)["result"]

    def test_answers_are_the_same_as_from_the_query_layer(self):
        for name, arguments in (("search_capabilities", {"query": "count words"}),
                                ("search_capabilities", {"query": "count", "limit": 1, "min_label": "Declared"}),
                                ("describe_capability", {"name": "word-count"}),
                                ("describe_capability", {"name": "word-count", "version": "1.0.0"}),
                                ("list_capabilities", {}),
                                ("list_capabilities", {"limit": 1})):
            with self.subTest(tool=name, arguments=arguments):
                rest, direct = self.both(name, arguments)
                self.assertFalse(rest["isError"], rest)
                self.assertEqual(rest, direct)

    def test_a_hidden_tool_is_not_found_through_rest_either(self):
        rest, direct = self.both("describe_capability", {"name": "secret-tool"})
        self.assertEqual(rest["structuredContent"], {"code": "NotFound"})
        self.assertEqual(rest, direct)
        found, _ = self.both("search_capabilities", {"query": "secret"})
        self.assertEqual(found["structuredContent"]["items"], [])

    def test_an_unreachable_registry_is_an_error_result_not_a_crash(self):
        principal = Principal("agent:ide")
        server = MCPServer(RestRegistry("http://127.0.0.1:9", principal, timeout=2), DevAdmissionAuthorizer(), principal)
        reply = server.handle(rpc(1, "tools/call", {"name": "list_capabilities", "arguments": {}}))["result"]
        self.assertEqual((reply["isError"], reply["structuredContent"]), (True, {"code": "Unexpected"}))

    def test_only_an_http_base_url_is_accepted(self):
        for url in ("file:///etc/passwd", "127.0.0.1:8090", "http://", "http://h:1/v2?x=1"):
            with self.subTest(url=url), self.assertRaises(ValueError):
                RestRegistry(url, Principal("agent:ide"))


class MCPSubprocessTest(unittest.TestCase):
    """The real command: `capability-registry mcp --url`, spoken to over stdio like an IDE would."""

    def test_an_mcp_client_finds_a_tool_over_stdio(self):
        service = RestService()
        try:
            command = [sys.executable, "-m", "capability.registry_cli", "mcp", "--url", service.url,
                       "--principal", "agent:ide"]
            messages = [rpc(1, "initialize", {"protocolVersion": PROTOCOL_VERSION, "capabilities": {},
                                              "clientInfo": {"name": "test", "version": "1"}}),
                        {"jsonrpc": "2.0", "method": "notifications/initialized"},
                        rpc(2, "tools/list"),
                        rpc(3, "tools/call", {"name": "search_capabilities", "arguments": {"query": "words"}})]
            done = subprocess.run(command, input="".join(json.dumps(m) + "\n" for m in messages), capture_output=True,
                                  text=True, timeout=60, cwd=str(REPO), env=dict(os.environ, PYTHONPATH=str(REPO)))
            self.assertEqual(done.returncode, 0, done.stderr[-2000:])
            replies = [json.loads(line) for line in done.stdout.splitlines()]
            self.assertEqual([r["id"] for r in replies], [1, 2, 3])
            self.assertEqual([i["name"] for i in replies[2]["result"]["structuredContent"]["items"]], ["word-count"])
        finally:
            service.close()

    def test_the_command_takes_no_store_folder(self):
        done = subprocess.run([sys.executable, "-m", "capability.registry_cli", "mcp", "--data", "/tmp/x",
                               "--principal", "agent:ide"], capture_output=True, text=True, timeout=30, cwd=str(REPO),
                              env=dict(os.environ, PYTHONPATH=str(REPO)))
        self.assertEqual(done.returncode, 2)
        self.assertIn("--url", done.stderr)


if __name__ == "__main__":
    unittest.main()
