"""S-018 R8: end to end, with real containers and a real socket.

1. The registry starts with the repository's own tools imported from the file index.
2. A Taffee agent, working in a folder of its own, submits word-count over HTTP. The registry builds it and runs
   its cases in the container sandbox; an approver approves it over HTTP.
3. A second author submits a tool that fails its cases. It is rejected and never becomes visible.
4. An orchestrator discovers tools over REST search and over MCP, and gets the same answer from both.
5. The dummy orchestrator searches, lets its planner choose, and runs word-count in a container, leaving a call record.
6. word-count is revoked on the feed: it drops out of search, and the orchestrator's next call is refused.

Needs TAFFEE_SANDBOX_IMAGE and TAFFEE_SANDBOX_TMP (the same environment as the other container tests).
"""

import http.client
import json
import os
import shutil
import tempfile
import threading
import time
import unittest
from pathlib import Path
from urllib.parse import quote

from capability.admission import Admission
from capability.authz import DevAdmissionAuthorizer, Principal
from capability.callrecord import CallRecorder, verify_log
from capability.index import build_index
from capability.library_repo import LIBRARY_OWNER, LibraryRepo
from capability.mcp_server import MCPServer
from capability.registry_api import RegistryService, make_server
from capability.registry_cli import import_index
from capability.rows import verify_store
from capability.sandbox import ContainerSandbox, docker_available
from capability.store import SQLiteStore
from capability.store_registry import StoreRegistry

from dummy_orchestrator import DummyOrchestrator, FakeGrants, ScriptedPlanner, ToolCall
from fixture_support import REPO, copy_suite
from registry_support import LANGUAGES, admission_key, bundle_bytes, fresh_revocation

IMAGE = os.environ.get("TAFFEE_SANDBOX_IMAGE")
SKIP_REASON = "TAFFEE_SANDBOX_IMAGE is not set" if not IMAGE else docker_available()
AUTHOR, OTHER_AUTHOR, APPROVER, AGENT = "author:acme", "author:sloppy", "approver:sec", "agent:orchestrator"


@unittest.skipIf(SKIP_REASON is not None, f"container tests need Docker: {SKIP_REASON}")
class RegistryEndToEndTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls._tmp = tempfile.TemporaryDirectory(dir=os.environ.get("TAFFEE_SANDBOX_TMP"))
        cls.base = Path(cls._tmp.name).resolve()
        os.chmod(cls.base, 0o755)
        # The repository's own catalog, built through the real publication gate, with the suite its evidence names.
        repo = cls.base / "repo"
        repo.mkdir()
        for folder in ("schemas", "policy", "catalog", "library", "evidence"):
            shutil.copytree(REPO / folder, repo / folder)
        copy_suite(repo)
        build_index(repo)
        cls.store = SQLiteStore(cls.base / "data")
        cls.private, cls.public = admission_key()
        cls.imported = import_index(repo, cls.store, cls.private)
        cls.client, cls.feed, cls.clocks = fresh_revocation()
        cls.registry = StoreRegistry(cls.store, cls.public, cls.client, LANGUAGES)
        cls.sandbox = ContainerSandbox(IMAGE, allowed_mount_roots=(cls.base,), cleanup_wait_s=30.0)
        cls.library = LibraryRepo(cls.base / "library")
        admission = Admission(cls.store, cls.private, cls.sandbox, LANGUAGES, cls.base / "work", library=cls.library)
        cls.service = RegistryService(cls.registry, admission, DevAdmissionAuthorizer(approvers={APPROVER}), cls.private)
        cls.server = make_server(cls.service)
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()
        cls.port = cls.server.server_address[1]

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.service.stop()
        cls.store.close()
        cls._tmp.cleanup()

    @classmethod
    def http(cls, method, path, principal, body=None):
        conn = http.client.HTTPConnection("127.0.0.1", cls.port, timeout=120)
        conn.request(method, path, body=body, headers={"X-Dev-Principal": principal})
        response = conn.getresponse()
        data = json.loads(response.read())
        conn.close()
        return response.status, data

    def wait_for(self, sid, principal, states=("awaiting_approval", "rejected", "admitted"), timeout=180):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            status, record = self.http("GET", f"/v2/submissions/{sid}", principal)
            self.assertEqual(status, 200)
            if record["state"] in states:
                return record
            time.sleep(0.5)
        self.fail(f"submission {sid} did not finish")

    def test_agent_written_tool_to_orchestrator_and_back_out(self):
        # 1. the built-in tools are already discoverable
        self.assertEqual(self.imported["imported"], 5)
        status, found = self.http("GET", "/v2/search?q=" + quote("search file contents"), AGENT)
        self.assertEqual(([i["name"] for i in found["items"]], found["items"][0]["label"]), (["grep"], "Tested"))

        # 2. an outside author submits; the registry tests it in containers; an approver approves
        author_dir = self.base / "acme-workspace"  # the author's own folder, outside the repository
        author_dir.mkdir()
        (author_dir / "bundle.json").write_bytes(bundle_bytes())
        status, record = self.http("POST", "/v2/submissions", AUTHOR, (author_dir / "bundle.json").read_bytes())
        self.assertEqual(status, 202)
        record = self.wait_for(record["id"], AUTHOR)
        self.assertEqual(record["state"], "awaiting_approval", record["problems"])
        self.assertEqual(self.http("GET", "/v2/search?q=" + quote("count words"), AGENT)[1]["items"], [])
        status, record = self.http("POST", f"/v2/submissions/{record['id']}/approve", APPROVER)
        self.assertEqual((status, record["state"]), (200, "admitted"))
        # the code is now Taffee's: committed to the library, owned by it
        self.assertEqual(self.library.log(1), ["Admit word-count 1.0.0"])
        self.assertTrue((self.library.path / "library/word-count/python/main.py").is_file())
        self.assertEqual(self.registry.describe("word-count")["owner"], LIBRARY_OWNER)

        # 3. a tool that fails its own cases never becomes visible
        broken = bundle_bytes(source="import json, sys\nsys.stdin.read()\nprint(json.dumps({'ok': True, 'output': 'nine'}))\n")
        broken = broken.replace(b'"name": "word-count"', b'"name": "word-counter"').replace(
            b"library/word-count/python", b"library/word-counter/python")
        status, bad = self.http("POST", "/v2/submissions", OTHER_AUTHOR, broken)
        bad = self.wait_for(bad["id"], OTHER_AUTHOR)
        self.assertEqual(bad["state"], "rejected")
        self.assertTrue(any("counts-text" in problem for problem in bad["problems"]), bad["problems"])

        # 4. REST and MCP discovery agree
        status, rest = self.http("GET", "/v2/search?q=" + quote("count words"), AGENT)
        self.assertEqual([i["name"] for i in rest["items"]], ["word-count"])
        mcp = MCPServer(self.registry, DevAdmissionAuthorizer(), Principal(AGENT))
        reply = mcp.handle({"jsonrpc": "2.0", "id": 7, "method": "tools/call",
                            "params": {"name": "search_capabilities", "arguments": {"query": "count words"}}})
        self.assertEqual(reply["result"]["structuredContent"], rest)

        # 5. the orchestrator searches, its planner picks a tool, and the tool runs in a container
        project = self.base / "project"
        project.mkdir()
        (project / "notes.txt").write_text("the quick brown fox\n", encoding="utf-8")
        for path in (project, *project.rglob("*")):
            path.chmod(0o755 if path.is_dir() else 0o644)
        log = self.base / "calls.jsonl"
        orchestrator = DummyOrchestrator(registry=self.registry, revocation=self.client, feed=self.feed,
                                         sandbox=self.sandbox, grants=FakeGrants({AGENT: {"repo.read"}}),
                                         recorder=CallRecorder(log, fsync=False))
        offered = []

        def planner(definitions):
            offered.extend(d["name"] for d in definitions)
            return [ToolCall("word-count", {"path": "notes.txt"})]

        _, old_bindings = orchestrator.search_catalog("count words")  # kept for step 6
        report = orchestrator.run(AGENT, ScriptedPlanner(planner), project_root=project, query="count words")
        self.assertEqual(offered, ["word-count"])
        self.assertEqual([(s.status, s.output) for s in report.steps], [("ok", "4")], [s.error for s in report.steps])
        [record] = verify_log(log)
        self.assertEqual((record["capability"]["name"], record["outcome"]["status"]), ("word-count", "ok"))

        # 6. revocation takes it out of discovery and out of execution
        self.feed.publish([{"kind": "tool", "name": "word-count", "version": "1.0.0"}])
        self.client.poll(self.feed)
        self.assertEqual(self.http("GET", "/v2/search?q=" + quote("count words"), AGENT)[1]["items"], [])
        report = orchestrator.run(AGENT, ScriptedPlanner(lambda d: [ToolCall("word-count", {"text": "a"})]),
                                  project_root=project, query="count")
        self.assertEqual([s.code for s in report.steps], ["NotOffered"])  # no longer offered at all
        # an orchestrator still holding a binding from before the revocation is refused at execution
        step = orchestrator._dispatch("late", 0, AGENT, ToolCall("word-count", {"text": "a"}), old_bindings, project)
        self.assertEqual((step.status, step.code), ("refused", "Revoked"))
        status, body = self.http("POST", "/v2/resolve", AGENT, json.dumps({"name": "word-count"}).encode())
        self.assertEqual((status, body["code"]), (410, "Revoked"))

        # the store still verifies end to end
        self.assertEqual(verify_store(self.store, self.public), 6)


if __name__ == "__main__":
    unittest.main()
