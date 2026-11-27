"""S-019: POST /v2/call runs an admitted tool for a caller and answers with its result.

Every requirement in SPEC.md (CAP-CALL-SVC-1 to 10) and every error code of the route has a test here.

- CallRouteTest: the transport-free service with the test-only unconfined sandbox, which runs only fixture tools
  written for these tests. Request shape, resolution, workspace rules, input and output checks, call records.
- RevocationOnEveryCallTest, AuthorizationTest, BrokenAuthorizationTest, SandboxUnavailableTest,
  SandboxCleanupFailureTest, ConcurrencyTest: one fresh service each, because they revoke, fail or block.
- SwitchAndWiringTest: the route switched off, no workspace root, and the serve options.
- CallOverHttpTest: the body bound on a real socket.
- ContainerCallTest: functional, in the real container sandbox (needs TAFFEE_SANDBOX_IMAGE and
  TAFFEE_SANDBOX_TMP): read-only and read-write workspaces, time limit, postconditions, and the built-in grep
  and git-log.
"""

import contextlib
import copy
import http.client
import io
import json
import os
import re
import shutil
import subprocess
import tempfile
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace

from capability.admission import Admission, Limits
from capability.authz import DevAdmissionAuthorizer
from capability.callrecord import CallRecorder, verify_log
from capability.index import build_index
from capability.registry_api import MAX_CALL_BODY, RegistryService, make_server
from capability.registry_cli import import_index, main as registry_cli_main
from capability.sandbox import ContainerSandbox, ProcessSandbox, SandboxCleanupFailed, docker_available
from capability.store import SQLiteStore
from capability.store_registry import StoreRegistry
from capability.tool_call import CallRefused, ToolCaller

from fixture_support import REPO, TOOL_VERSIONS, copy_suite
from registry_support import (
    CASES, LANGUAGES, UnconfinedTestSandbox, admission_key, bundle_bytes, fresh_revocation, word_count_manifest,
)

AUTHOR, APPROVER, AGENT = "author:acme", "approver:sec", "agent:orchestrator"
IMAGE = os.environ.get("TAFFEE_SANDBOX_IMAGE")
SKIP_REASON = "TAFFEE_SANDBOX_IMAGE is not set" if not IMAGE else docker_available()

# A fixture tool with one mode per behaviour the route must handle: echo its text (postconditions allow at most
# 64 bytes), try to write note.txt in its root, sleep past its 2 s limit, or fail.
PROBE = '''import json, os, sys, time
req = json.loads(sys.stdin.read())
data = req["input"]
mode = data.get("mode")
if mode == "echo":
    out = data.get("text", "")
elif mode == "write":
    try:
        with open(os.path.join(req["root"], "note.txt"), "w", encoding="utf-8") as handle:
            handle.write(data.get("text", ""))
        out = "written"
    except OSError:
        out = "denied"
elif mode == "sleep":
    time.sleep(30)
    out = "woke"
else:
    print(json.dumps({"ok": False, "error": "unknown mode"}))
    sys.exit(0)
print(json.dumps({"ok": True, "output": out}))
'''
PROBE_CASES = [
    {"id": "echoes", "input": {"mode": "echo", "text": "hi"}, "expect": {"ok": True, "contains": '"output": "hi"'}},
    {"id": "unknown-mode-is-an-error", "input": {"mode": "fail"}, "expect": {"ok": False}},
]
TEXT_CASES = [CASES[0], CASES[2]]


def probe_manifest(name, filesystem):
    return {
        "kind": "tool", "name": name, "version": "1.0.0", "owner": "acme-tools",
        "summary": f"A probe tool for the call service ({filesystem}).", "requires": ["repo.read"],
        "implementations": [{"language": "python", "location": f"library/{name}/python"}],
        "spec": {
            "inputSchema": {"type": "object", "additionalProperties": False, "required": ["mode"], "properties": {
                "mode": {"type": "string", "maxLength": 16, "description": "echo, write, sleep, or anything else to fail."},
                "text": {"type": "string", "maxLength": 1000, "description": "Text to echo or to write."}}},
            "readOnly": filesystem != "write-root", "idempotent": True, "timeoutMs": 2000, "boundary": [],
            "postconditions": [{"type": "max_bytes", "value": 64}],
            "sandbox": {"filesystem": filesystem},
        },
    }


def text_manifest(version="1.0.0"):
    """The word-count fixture, declared with no file access: it can run without a workspace."""
    manifest = word_count_manifest(name="text-count", version=version, summary="Count the words in a piece of text.")
    manifest["spec"]["sandbox"] = {"filesystem": "none"}
    return manifest


FIXTURE_TOOLS = [
    dict(),  # word-count, read-root
    dict(manifest=word_count_manifest(name="secret-tool")),  # hidden from every caller
    dict(manifest=text_manifest("1.0.0"), cases=TEXT_CASES),
    dict(manifest=text_manifest("1.1.0"), cases=TEXT_CASES),
    dict(manifest=probe_manifest("probe-none", "none"), source=PROBE, cases=PROBE_CASES),
    dict(manifest=probe_manifest("probe-ro", "read-root"), source=PROBE, cases=PROBE_CASES),
    dict(manifest=probe_manifest("probe-rw", "write-root"), source=PROBE, cases=PROBE_CASES),
]


def build(base: Path, sandbox, admission_sandbox=None, authz=None, max_concurrent=4, builtins=False, replies=None):
    """A store with the fixture tools admitted (secret-tool hidden from everyone), a service with calls on, and
    a recorder. With `builtins`, the repository's own tools are imported too, as `serve` does."""
    store = SQLiteStore(base / "data")
    private, public = admission_key()
    if builtins:
        repo = base / "repo"
        for folder in ("schemas", "policy", "catalog", "library", "evidence"):
            shutil.copytree(REPO / folder, repo / folder)
        copy_suite(repo)
        build_index(repo)
        import_index(repo, store, private)
    client, feed, clocks = fresh_revocation()
    registry = StoreRegistry(store, public, client, LANGUAGES)
    admission = Admission(store, private, admission_sandbox or UnconfinedTestSandbox(), LANGUAGES, base / "work",
                          limits=Limits(max_bundle_bytes=64 * 1024))
    authz = authz or DevAdmissionAuthorizer(approvers={APPROVER}, hidden={"tool:secret-tool:1.0.0"})
    recorder = CallRecorder(base / "calls.jsonl")
    (base / "workspaces").mkdir(exist_ok=True)
    caller = ToolCaller(registry, sandbox, work_dir=base / "work" / "calls", workspaces=base / "workspaces",
                        recorder=recorder, max_concurrent=max_concurrent, replies=replies)
    service = RegistryService(registry, admission, authz, private, process_inline=True, caller=caller)
    for tool in FIXTURE_TOOLS:
        kwargs = copy.deepcopy(tool)
        status, _, data = service.handle("POST", "/v2/submissions", {"X-Dev-Principal": AUTHOR}, bundle_bytes(**kwargs))
        record = json.loads(data)
        assert status == 202 and record["state"] == "awaiting_approval", record
        status, _, data = service.handle("POST", f"/v2/submissions/{record['id']}/approve",
                                         {"X-Dev-Principal": APPROVER}, b"")
        assert status == 200 and json.loads(data)["state"] == "admitted", data
    return SimpleNamespace(service=service, caller=caller, recorder=recorder, store=store, feed=feed, clocks=clocks,
                           client=client, base=base, log=base / "calls.jsonl")


def post(env, body, principal=AGENT):
    headers = {} if principal is None else {"X-Dev-Principal": principal}
    data = body if isinstance(body, bytes) else json.dumps(body).encode()
    status, out, payload = env.service.handle("POST", "/v2/call", headers, data)
    return status, json.loads(payload), out


def records(env, call_id=None):
    found = verify_log(env.log)
    return found if call_id is None else [r for r in found if r["call_id"] == call_id]


class FreshService(unittest.TestCase):
    sandbox_factory = UnconfinedTestSandbox
    max_concurrent = 4

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.env = build(Path(self._tmp.name).resolve(), self.sandbox_factory(), authz=self.authz(),
                         max_concurrent=self.max_concurrent)

    def authz(self):
        return None

    def tearDown(self):
        self.env.service.stop()
        self.env.store.close()
        self._tmp.cleanup()


class CallRouteTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls._tmp = tempfile.TemporaryDirectory()
        cls.env = build(Path(cls._tmp.name).resolve(), UnconfinedTestSandbox())
        repo = cls.env.base / "workspaces" / "agent-1" / "repo"
        repo.mkdir(parents=True)
        (repo / "doc.txt").write_text("alpha beta\n", encoding="utf-8")
        cls.n = 0

    @classmethod
    def tearDownClass(cls):
        cls.env.service.stop()
        cls.env.store.close()
        cls._tmp.cleanup()

    def call_id(self):
        type(self).n += 1
        return f"run-7:step-{type(self).n}"

    def post(self, body, principal=AGENT):
        return post(self.env, body, principal)

    # -- SVC-7: the answer, and exactly what ran --
    def test_a_text_tool_runs_without_a_workspace_and_names_what_ran(self):
        call_id = self.call_id()
        status, body, _ = self.post({"name": "text-count", "input": {"text": "one two three"}, "call_id": call_id})
        self.assertEqual(status, 200, body)
        self.assertEqual(set(body), {"call_id", "result", "capability"})
        self.assertEqual((body["call_id"], body["result"]), (call_id, {"ok": True, "output": "3"}))
        cap = body["capability"]
        self.assertEqual(set(cap), {"name", "version", "identity", "label", "language", "code_digest", "revocation_seq"})
        self.assertEqual((cap["name"], cap["version"], cap["label"], cap["language"]),
                         ("text-count", "1.1.0", "Validated", "python"))
        self.assertRegex(cap["identity"], r"^sha256:[0-9a-f]{64}$")
        self.assertRegex(cap["code_digest"], r"^sha256:[0-9a-f]{64}$")
        self.assertIsInstance(cap["revocation_seq"], int)

    def test_the_range_pins_the_version_that_runs(self):
        cases = {"*": "1.1.0", "1.0.0": "1.0.0", "1.1.0": "1.1.0", "^1.0.0": "1.1.0"}
        for range_, version in cases.items():
            with self.subTest(range=range_):
                status, body, _ = self.post({"name": "text-count", "range": range_, "input": {"text": "a"},
                                             "call_id": self.call_id()})
                self.assertEqual((status, body.get("capability", {}).get("version")), (200, version), body)
        status, body, _ = self.post({"name": "text-count", "range": "^2.0.0", "input": {"text": "a"},
                                     "call_id": self.call_id()})
        self.assertEqual((status, body["code"]), (404, "NotFound"))

    def test_a_range_the_registry_does_not_support_is_invalid_range(self):
        for range_ in ("~1.0.0", ">=1.0.0", "01.0.0", "1.0", "latest"):
            with self.subTest(range=range_):
                status, body, _ = self.post({"name": "text-count", "range": range_, "input": {"text": "a"},
                                             "call_id": self.call_id()})
                self.assertEqual((status, body["code"]), (400, "InvalidRange"))

    def test_a_language_the_tool_does_not_have_is_no_implementation(self):
        status, body, _ = self.post({"name": "text-count", "languages": ["bash", "typescript"], "input": {"text": "a"},
                                     "call_id": self.call_id()})
        self.assertEqual((status, body["code"]), (404, "NoImplementation"))

    def test_no_reply_carries_a_host_path(self):
        host = str(self.env.base)
        bodies = [
            self.post({"name": "word-count", "input": {"path": "doc.txt"}, "workspace": "agent-1/repo",
                       "call_id": self.call_id()})[1],
            self.post({"name": "word-count", "input": {"path": "absent.txt"}, "workspace": "agent-1/repo",
                       "call_id": self.call_id()})[1],
            self.post({"name": "word-count", "input": {"text": "x"}, "workspace": "nowhere",
                       "call_id": self.call_id()})[1],
            self.post({"name": "word-count", "input": {"text": "x"}, "call_id": self.call_id()})[1],
        ]
        for body in bodies:
            self.assertNotIn(host, json.dumps(body))
            self.assertNotIn("implementation_path", json.dumps(body))

    # -- SVC-4 and 5: workspaces --
    def test_a_tool_reads_the_named_workspace(self):
        status, body, _ = self.post({"name": "word-count", "input": {"path": "doc.txt"}, "workspace": "agent-1/repo",
                                     "call_id": self.call_id()})
        self.assertEqual((status, body["result"]), (200, {"ok": True, "output": "2"}), body)

    def test_a_tool_that_reads_or_writes_its_root_needs_a_workspace_and_nothing_is_recorded(self):
        before = len(records(self.env))
        for tool in ("word-count", "probe-ro", "probe-rw"):
            with self.subTest(tool=tool):
                data = {"mode": "echo"} if tool.startswith("probe") else {"text": "x"}
                status, body, _ = self.post({"name": tool, "input": data, "call_id": self.call_id()})
                self.assertEqual((status, body["code"]), (400, "WorkspaceRequired"))
        self.assertEqual(len(records(self.env)), before)

    def test_a_tool_with_no_file_access_runs_with_or_without_a_workspace(self):
        for workspace in (None, "agent-1/repo"):
            with self.subTest(workspace=workspace):
                body = {"name": "probe-none", "input": {"mode": "echo", "text": "ok"}, "call_id": self.call_id()}
                if workspace:
                    body["workspace"] = workspace
                status, reply, _ = self.post(body)
                self.assertEqual((status, reply["result"]), (200, {"ok": True, "output": "ok"}))

    def test_a_workspace_cannot_leave_the_workspace_root(self):
        root = self.env.base / "workspaces"
        outside = self.env.base / "outside"
        outside.mkdir(exist_ok=True)
        if not (root / "escape").exists():
            (root / "escape").symlink_to(outside, target_is_directory=True)
        if not (root / "alias").exists():
            (root / "alias").symlink_to(root / "agent-1", target_is_directory=True)  # a link that stays inside
        (root / "a-file").write_text("x")
        cases = {"../outside": "InvalidRequest", "/etc": "InvalidRequest", "agent-1/../../outside": "InvalidRequest",
                 "agent-1//repo": "InvalidRequest", "": "InvalidRequest", "agent-1/": "InvalidRequest",
                 ".git": "InvalidRequest", "agent-1/.hidden": "InvalidRequest", "a b": "InvalidRequest",
                 "/".join(["d"] * 9): "InvalidRequest", "x" * 65: "InvalidRequest",
                 "escape": "WorkspaceNotFound", "escape/sub": "WorkspaceNotFound", "alias/repo": "WorkspaceNotFound",
                 "absent": "WorkspaceNotFound", "a-file": "WorkspaceNotFound"}
        for workspace, code in cases.items():
            with self.subTest(workspace=workspace):
                status, body, _ = self.post({"name": "word-count", "input": {"text": "x"}, "workspace": workspace,
                                             "call_id": self.call_id()})
                self.assertEqual(body["code"], code)
                self.assertEqual(status, 400 if code == "InvalidRequest" else 404)

    def test_the_deepest_allowed_workspace_path_works(self):
        deep = self.env.base / "workspaces" / Path(*(["d"] * 8))
        deep.mkdir(parents=True, exist_ok=True)
        (deep / "doc.txt").write_text("one\n", encoding="utf-8")
        status, body, _ = self.post({"name": "word-count", "input": {"path": "doc.txt"}, "workspace": "/".join(["d"] * 8),
                                     "call_id": self.call_id()})
        self.assertEqual((status, body["result"]), (200, {"ok": True, "output": "1"}))

    def test_scratch_folders_of_workspace_free_calls_are_removed(self):
        self.post({"name": "text-count", "input": {"text": "x"}, "call_id": self.call_id()})
        self.post({"name": "probe-none", "input": {"mode": "fail"}, "call_id": self.call_id()})
        self.assertEqual(list((self.env.base / "work" / "calls").iterdir()), [])

    # -- SVC-6: input and output checks, tool failures --
    def test_input_that_breaks_the_schema_is_refused_with_problems_and_no_values(self):
        bad_inputs = [{"text": "x", "secret-value": "hunter2"}, {"text": 7}, ["hunter2"], "hunter2", None,
                      {"text": "x" * 10001}]
        for data in bad_inputs:
            with self.subTest(input=str(data)[:30]):
                status, body, _ = self.post({"name": "text-count", "input": data, "call_id": self.call_id()})
                self.assertEqual((status, body["code"]), (422, "ValidationFailed"))
                self.assertTrue(body["problems"])
                self.assertTrue(all(set(p) == {"field", "message"} for p in body["problems"]))
                self.assertNotIn("hunter2", json.dumps(body))

    def test_output_that_breaks_the_postconditions_is_502_with_problems(self):
        status, body, _ = self.post({"name": "probe-none", "input": {"mode": "echo", "text": "y" * 100},
                                     "call_id": self.call_id()})
        self.assertEqual((status, body["code"]), (502, "PostconditionFailed"))
        self.assertTrue(body["problems"])
        self.assertNotIn("y" * 65, json.dumps(body))

    def test_a_tool_level_failure_is_a_200_result(self):
        status, body, _ = self.post({"name": "probe-none", "input": {"mode": "fail"}, "call_id": self.call_id()})
        self.assertEqual((status, body["result"]), (200, {"ok": False, "error": "unknown mode"}))

    def test_a_tool_past_its_time_limit_is_a_200_result_with_the_closed_timeout_text(self):
        status, body, _ = self.post({"name": "probe-none", "input": {"mode": "sleep"}, "call_id": self.call_id()})
        self.assertEqual((status, body["result"]), (200, {"ok": False, "error": "the tool ran past its time limit"}))

    # -- SVC-8: call records --
    def test_every_call_that_resolved_is_recorded_with_its_outcome(self):
        cases = [
            ({"name": "text-count", "input": {"text": "a b"}, "grant": "grant/team-a/42"}, ("ok", None)),
            ({"name": "probe-none", "input": {"mode": "fail"}}, ("tool_failed", None)),
            ({"name": "text-count", "input": {"text": 5}}, ("input_rejected", "ValidationFailed")),
            ({"name": "probe-none", "input": {"mode": "echo", "text": "z" * 100}}, ("output_rejected", "PostconditionFailed")),
        ]
        for body, (status_, code) in cases:
            with self.subTest(outcome=status_):
                call_id = self.call_id()
                self.post(dict(body, call_id=call_id))
                found = records(self.env, call_id)
                self.assertEqual(len(found), 1)
                record = found[0]
                self.assertEqual((record["outcome"]["status"], record["outcome"].get("code")), (status_, code))
                self.assertEqual(record["capability"]["name"], body["name"])
                self.assertEqual(record["grant"], body.get("grant"))
                self.assertNotIn("a b", json.dumps(record))
                self.assertNotIn("z" * 10, json.dumps(record))

    def test_calls_refused_before_resolution_write_no_record(self):
        before = len(records(self.env))
        for body in ({"name": "no-such-tool", "input": {}},
                     {"name": "secret-tool", "input": {}, "workspace": "agent-1/repo"},
                     {"name": "word-count", "input": {"text": "x"}, "workspace": "absent"},
                     {"name": "text-count", "input": {}, "min_label": "Tested"}):
            self.post(dict(body, call_id=self.call_id()))
        self.assertEqual(len(records(self.env)), before)

    # -- SVC-1 and 3: request shape, visibility, identity --
    def test_malformed_requests_are_refused(self):
        good = {"name": "text-count", "input": {"text": "x"}, "call_id": "c-1"}
        bad = [
            {k: v for k, v in good.items() if k != "call_id"}, {k: v for k, v in good.items() if k != "input"},
            {k: v for k, v in good.items() if k != "name"},
            dict(good, call_id="has space"), dict(good, call_id="x" * 65), dict(good, call_id=""), dict(good, call_id=7),
            dict(good, call_id="-starts-with-dash"), dict(good, grant="free text here"), dict(good, grant="g" * 129),
            dict(good, grant=5), dict(good, extra=1), dict(good, name="Bad_Name"), dict(good, name="x"),
            dict(good, languages=[]), dict(good, languages="python"), dict(good, languages=["python"] * 9),
            dict(good, languages=["Python"]), dict(good, range=""), dict(good, range="1" * 65), dict(good, range=1),
            dict(good, min_label=3), dict(good, min_label="tested"), dict(good, workspace=7), [good], "text", None,
        ]
        for body in bad:
            with self.subTest(body=str(body)[:60]):
                status, reply, _ = self.post(body)
                self.assertEqual((status, reply["code"]), (400, "InvalidRequest"))
        for raw in (b"{not json", b"", b"\xff\xfe", b"[]"):
            with self.subTest(raw=raw):
                self.assertEqual(self.post(raw)[1]["code"], "InvalidRequest")

    def test_the_largest_allowed_values_are_accepted(self):
        status, body, _ = self.post({"name": "text-count", "input": {"text": "a"}, "call_id": "c" * 64,
                                     "grant": "g" * 128, "languages": ["python"] * 8, "range": "*"})
        self.assertEqual(status, 200, body)

    def test_a_tool_the_caller_may_not_see_is_absent_and_unknown_tools_are_not_found(self):
        for name in ("secret-tool", "no-such-tool"):
            with self.subTest(name=name):
                status, body, _ = self.post({"name": name, "input": {"text": "x"}, "workspace": "agent-1/repo",
                                             "call_id": self.call_id()})
                self.assertEqual((status, body["code"]), (404, "NotFound"))

    def test_the_label_floor_applies(self):
        status, body, _ = self.post({"name": "text-count", "input": {"text": "x"}, "min_label": "Tested",
                                     "call_id": self.call_id()})
        self.assertEqual((status, body["code"]), (409, "BelowLabel"))
        status, _, _ = self.post({"name": "text-count", "input": {"text": "x"}, "min_label": "Validated",
                                  "call_id": self.call_id()})
        self.assertEqual(status, 200)

    def test_an_unidentified_caller_is_refused(self):
        for principal in (None, "has space", ""):
            with self.subTest(principal=principal):
                status, body, _ = self.post({"name": "text-count", "input": {"text": "x"}, "call_id": "c-2"},
                                            principal=principal)
                self.assertEqual((status, body["code"]), (401, "Unauthenticated"))

    def test_get_is_not_a_call(self):  # like every POST-only path (/v2/resolve too): GET finds no such route
        status, _, data = self.env.service.handle("GET", "/v2/call", {"X-Dev-Principal": AGENT}, b"")
        self.assertEqual((status, json.loads(data)["code"]), (404, "NotFound"))


class RevocationOnEveryCallTest(FreshService):
    """SVC-2: the tool is resolved and its revocation checked on every call, not once."""

    def test_a_version_revoked_between_two_calls_is_refused_at_the_second(self):
        body = {"name": "text-count", "input": {"text": "a"}}
        self.assertEqual(post(self.env, dict(body, call_id="r-1"))[0], 200)
        self.env.feed.publish([{"kind": "tool", "name": "text-count", "version": "1.1.0"}])
        self.env.client.poll(self.env.feed)
        status, reply, _ = post(self.env, dict(body, call_id="r-2"))
        self.assertEqual((status, reply["code"]), (410, "Revoked"))
        status, reply, _ = post(self.env, dict(body, range="1.0.0", call_id="r-3"))  # the older version still runs
        self.assertEqual((status, reply["capability"]["version"]), (200, "1.0.0"))
        self.assertEqual([r["call_id"] for r in records(self.env)], ["r-1", "r-3"])

    def test_stale_revocation_knowledge_fails_closed_with_retry_after(self):
        self.env.clocks.advance(31)
        status, reply, headers = post(self.env, {"name": "text-count", "input": {"text": "a"}, "call_id": "s-1"})
        self.assertEqual((status, reply["code"], headers.get("Retry-After")), (503, "RevocationStale", "30"))
        self.assertEqual(records(self.env), [])


class NoCallsFor(DevAdmissionAuthorizer):
    """Lets `denied` see every tool but run none: may_call is asked separately from may_see."""

    def __init__(self, denied, **kwargs):
        super().__init__(**kwargs)
        self._denied = denied

    def may_call(self, principal, ref):
        return principal.id != self._denied and super().may_call(principal, ref)


class BrokenCallDecision(DevAdmissionAuthorizer):
    def may_call(self, principal, ref):
        raise RuntimeError("the decision service is down")


class AuthorizationTest(FreshService):
    """SVC-3."""

    def authz(self):
        return NoCallsFor("agent:readonly", approvers={APPROVER})

    def test_may_call_is_asked_separately_from_may_see(self):
        headers = {"X-Dev-Principal": "agent:readonly"}
        self.assertEqual(self.env.service.handle("GET", "/v2/tools/text-count", headers, b"")[0], 200)
        status, reply, _ = post(self.env, {"name": "text-count", "input": {"text": "x"}, "call_id": "c-1"},
                                principal="agent:readonly")
        self.assertEqual((status, reply["code"]), (404, "NotFound"))
        self.assertEqual(post(self.env, {"name": "text-count", "input": {"text": "x"}, "call_id": "c-2"})[0], 200)

    def test_the_default_may_call_is_may_see(self):
        authz = DevAdmissionAuthorizer(hidden={"tool:text-count:1.1.0"})
        self.assertFalse(authz.may_call(None, {"kind": "tool", "name": "text-count", "version": "1.1.0"}))
        self.assertTrue(authz.may_call(None, {"kind": "tool", "name": "text-count", "version": "1.0.0"}))


class BrokenAuthorizationTest(FreshService):
    def authz(self):
        return BrokenCallDecision(approvers={APPROVER})

    def test_a_decision_that_cannot_be_made_refuses_the_call(self):
        status, reply, headers = post(self.env, {"name": "text-count", "input": {"text": "x"}, "call_id": "c-1"})
        self.assertEqual((status, reply["code"], headers.get("Retry-After")), (503, "AuthorizerUnavailable", "30"))
        self.assertEqual(records(self.env), [])


class CleanupFailingSandbox:
    name = "cleanup-failing"

    def run(self, argv, *, root, policy, stdin=b"", env_values=None, implementation=None):
        raise SandboxCleanupFailed("taffee-sbx-0123456789abcdef")


class SandboxUnavailableTest(FreshService):
    """A sandbox that cannot enforce what the tool declares refuses it, and says which limits."""

    sandbox_factory = ProcessSandbox

    def test_the_unmet_limits_are_named_and_the_refusal_is_recorded(self):
        status, reply, _ = post(self.env, {"name": "text-count", "input": {"text": "x"}, "call_id": "u-1"})
        self.assertEqual((status, reply["code"]), (409, "SandboxUnavailable"))
        self.assertIn("network", reply["unmet"])
        self.assertEqual([(r["outcome"]["status"], r["outcome"]["code"]) for r in records(self.env)],
                         [("refused", "SandboxUnavailable")])


class SandboxCleanupFailureTest(FreshService):
    sandbox_factory = CleanupFailingSandbox

    def test_an_unconfirmed_cleanup_is_unexpected_recorded_and_the_scratch_folder_still_goes(self):
        status, reply, _ = post(self.env, {"name": "text-count", "input": {"text": "x"}, "call_id": "k-1"})
        self.assertEqual((status, reply["code"]), (500, "Unexpected"))
        self.assertEqual([(r["outcome"]["status"], r["outcome"]["code"]) for r in records(self.env)],
                         [("error", "Unexpected")])
        self.assertEqual(list((self.env.base / "work" / "calls").iterdir()), [])


class GateSandbox(UnconfinedTestSandbox):
    """Holds every run until released, and counts how many run at once."""

    def __init__(self):
        super().__init__()
        self.lock = threading.Lock()
        self.in_flight = self.most = 0
        self.arrived = threading.Semaphore(0)
        self.release = threading.Event()

    def run(self, argv, **kwargs):
        with self.lock:
            self.in_flight += 1
            self.most = max(self.most, self.in_flight)
        self.arrived.release()
        self.release.wait(30)
        try:
            return super().run(argv, **kwargs)
        finally:
            with self.lock:
                self.in_flight -= 1


class ConcurrencyTest(FreshService):
    """SVC-9: at most --call-workers calls run at once; the next is Busy; the record log stays a valid chain."""

    sandbox_factory = GateSandbox
    max_concurrent = 3

    def test_the_bound_holds_under_parallel_calls_and_the_log_stays_valid(self):
        gate = self.env.caller._sandbox
        replies = {}

        def call(i):
            replies[i] = post(self.env, {"name": "text-count", "input": {"text": "a b"}, "call_id": f"p-{i}"})

        threads = [threading.Thread(target=call, args=(i,)) for i in range(3)]
        for thread in threads:
            thread.start()
        for _ in range(3):
            self.assertTrue(gate.arrived.acquire(timeout=30))
        for i in (3, 4):  # every slot is held: these are refused at once, before anything runs
            status, reply, headers = post(self.env, {"name": "text-count", "input": {"text": "a"}, "call_id": f"p-{i}"})
            self.assertEqual((status, reply["code"], headers.get("Retry-After")), (429, "Busy", "10"))
        gate.release.set()
        for thread in threads:
            thread.join(30)
        self.assertEqual(sorted(r[0] for r in replies.values()), [200, 200, 200])
        self.assertEqual(gate.most, 3)
        self.assertEqual(sorted(r["call_id"] for r in records(self.env)), ["p-0", "p-1", "p-2"])
        self.assertEqual(post(self.env, {"name": "text-count", "input": {"text": "a"}, "call_id": "p-5"})[0], 200)


class SwitchAndWiringTest(unittest.TestCase):
    """SVC-10, and the serve options that configure the route."""

    def test_without_a_caller_the_route_is_off(self):
        with tempfile.TemporaryDirectory() as tmp:
            env = build(Path(tmp).resolve(), UnconfinedTestSandbox())
            try:
                off = RegistryService(env.service._registry, env.service._admission, env.service._authz, None,
                                      process_inline=True)
                status, _, data = off.handle("POST", "/v2/call", {"X-Dev-Principal": AGENT},
                                             json.dumps({"name": "text-count", "input": {}, "call_id": "c"}).encode())
                self.assertEqual((status, json.loads(data)["code"]), (404, "CallsDisabled"))
            finally:
                env.service.stop()
                env.store.close()

    def test_without_a_workspace_root_a_named_workspace_is_refused_and_workspace_free_tools_still_run(self):
        with tempfile.TemporaryDirectory() as tmp:
            env = build(Path(tmp).resolve(), UnconfinedTestSandbox())
            try:
                env.caller._workspaces = None
                with self.assertRaises(CallRefused) as ctx:
                    env.caller.call({"name": "word-count", "input": {"path": "doc.txt"}, "workspace": "w",
                                     "call_id": "c"}, lambda ref: True)
                self.assertEqual(ctx.exception.code, "WorkspaceUnavailable")
                status, _, data = env.service.handle("POST", "/v2/call", {"X-Dev-Principal": AGENT}, json.dumps(
                    {"name": "word-count", "input": {"text": "x"}, "workspace": "w", "call_id": "c2"}).encode())
                self.assertEqual((status, json.loads(data)["code"]), (409, "WorkspaceUnavailable"))
                reply = env.caller.call({"name": "text-count", "input": {"text": "a"}, "call_id": "c3"}, lambda ref: True)
                self.assertEqual(reply["result"], {"ok": True, "output": "1"})
            finally:
                env.service.stop()
                env.store.close()

    def test_the_caller_needs_at_least_one_slot(self):
        with self.assertRaises(ValueError):
            ToolCaller(None, None, work_dir="/tmp", max_concurrent=0)

    def test_serve_offers_the_call_options(self):
        out = io.StringIO()
        with contextlib.redirect_stdout(out), self.assertRaises(SystemExit) as ctx:
            registry_cli_main(["serve", "--help"])
        self.assertEqual(ctx.exception.code, 0)
        for option in ("--no-calls", "--workspaces", "--call-log", "--call-workers"):
            self.assertIn(option, out.getvalue())


class CallOverHttpTest(unittest.TestCase):
    """SVC-9: the body bound on a real socket."""

    def test_the_call_bound_not_the_small_default_applies_and_is_exact(self):
        with tempfile.TemporaryDirectory() as tmp:
            env = build(Path(tmp).resolve(), UnconfinedTestSandbox())
            server = make_server(env.service)
            threading.Thread(target=server.serve_forever, daemon=True).start()
            try:
                port = server.server_address[1]

                def send(body=None, length=None):
                    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=60)
                    conn.putrequest("POST", "/v2/call")
                    conn.putheader("X-Dev-Principal", AGENT)
                    conn.putheader("Content-Length", str(len(body) if length is None else length))
                    conn.endheaders()
                    if body is not None:
                        conn.send(body)
                    response = conn.getresponse()
                    data = json.loads(response.read())
                    conn.close()
                    return response.status, data

                text = ("word " * 2000)[:9999]  # 10 kB: above the 4 kB default bound
                status, body = send(json.dumps({"name": "text-count", "input": {"text": text}, "call_id": "big-1"}).encode())
                self.assertEqual((status, body["result"]), (200, {"ok": True, "output": "2000"}))
                exact = json.dumps({"name": "text-count", "input": {"text": "a"}, "call_id": "big-2"}).encode()
                exact += b" " * (MAX_CALL_BODY - len(exact))  # trailing whitespace is valid JSON
                self.assertEqual(len(exact), MAX_CALL_BODY)
                self.assertEqual(send(exact)[0], 200)
                status, body = send(length=MAX_CALL_BODY + 1)  # headers only: refused unread
                self.assertEqual((status, body["code"]), (413, "TooLarge"))
            finally:
                server.shutdown()
                env.service.stop()
                env.store.close()


@unittest.skipIf(SKIP_REASON is not None, f"container tests need Docker: {SKIP_REASON}")
class ContainerCallTest(unittest.TestCase):
    """Functional: the route in the real container sandbox, with the fixture tools and the built-in tools."""

    @classmethod
    def setUpClass(cls):
        cls._tmp = tempfile.TemporaryDirectory(dir=os.environ.get("TAFFEE_SANDBOX_TMP"))
        base = Path(cls._tmp.name).resolve()
        os.chmod(base, 0o755)
        sandbox = ContainerSandbox(IMAGE, allowed_mount_roots=(base,), cleanup_wait_s=30.0)
        cls.env = build(base, sandbox, admission_sandbox=sandbox, builtins=True)
        root = base / "workspaces"
        repo = root / "agent-1"
        repo.mkdir(parents=True)
        (repo / "doc.txt").write_text("one two three four\n", encoding="utf-8")
        (repo / "notes.txt").write_text("first line\nthe needle is here\nlast line\n", encoding="utf-8")
        # A real git repository, owned by this process's user, not by the sandbox's: git must still read it.
        env = dict(os.environ, GIT_AUTHOR_NAME="t", GIT_AUTHOR_EMAIL="t@example.invalid", GIT_COMMITTER_NAME="t",
                   GIT_COMMITTER_EMAIL="t@example.invalid", GIT_AUTHOR_DATE="2026-01-01T00:00:00Z",
                   GIT_COMMITTER_DATE="2026-01-01T00:00:00Z")
        for argv in (["git", "init", "-q"], ["git", "add", "-A"], ["git", "commit", "-q", "-m", "first commit"]):
            subprocess.run(argv, cwd=repo, env=env, check=True, capture_output=True)
        for path in (root, repo, *repo.rglob("*")):
            os.chmod(path, 0o755 if path.is_dir() else 0o644)
        cls.rw = root / "agent-rw"  # writable by the sandbox user, for the write-root tool
        cls.rw.mkdir()
        os.chmod(cls.rw, 0o777)

    @classmethod
    def tearDownClass(cls):
        cls.env.service.stop()
        cls.env.store.close()
        cls._tmp.cleanup()

    def post(self, body):
        status, reply, _ = post(self.env, body)
        return status, reply

    def test_a_workspace_tool_runs_in_a_container(self):
        status, body = self.post({"name": "word-count", "input": {"path": "doc.txt"}, "workspace": "agent-1",
                                  "call_id": "ctr-1"})
        self.assertEqual((status, body["result"]), (200, {"ok": True, "output": "4"}), body)

    def test_a_text_tool_runs_in_a_container_without_a_workspace(self):
        status, body = self.post({"name": "text-count", "input": {"text": "a b c"}, "call_id": "ctr-2"})
        self.assertEqual((status, body["result"]), (200, {"ok": True, "output": "3"}), body)

    def test_a_read_root_workspace_is_mounted_read_only(self):
        status, body = self.post({"name": "probe-ro", "input": {"mode": "write", "text": "x"}, "workspace": "agent-rw",
                                  "call_id": "ctr-ro"})
        self.assertEqual((status, body["result"]), (200, {"ok": True, "output": "denied"}), body)
        self.assertFalse((self.rw / "note.txt").exists())

    def test_a_write_root_tool_writes_into_its_workspace(self):
        status, body = self.post({"name": "probe-rw", "input": {"mode": "write", "text": "hello"},
                                  "workspace": "agent-rw", "call_id": "ctr-rw"})
        self.assertEqual((status, body["result"]), (200, {"ok": True, "output": "written"}), body)
        self.assertEqual((self.rw / "note.txt").read_text(encoding="utf-8"), "hello")
        (self.rw / "note.txt").unlink()

    def test_a_tool_with_no_file_access_sees_no_workspace_files(self):
        status, body = self.post({"name": "probe-none", "input": {"mode": "write", "text": "x"}, "workspace": "agent-rw",
                                  "call_id": "ctr-none"})
        self.assertEqual(status, 200, body)
        self.assertNotEqual(body["result"].get("output"), "written")
        self.assertFalse((self.rw / "note.txt").exists())

    def test_a_tool_past_its_time_limit_is_stopped(self):
        status, body = self.post({"name": "probe-none", "input": {"mode": "sleep"}, "call_id": "ctr-sleep"})
        self.assertEqual((status, body["result"]), (200, {"ok": False, "error": "the tool ran past its time limit"}))

    def test_postconditions_apply_to_a_container_run(self):
        status, body = self.post({"name": "probe-none", "input": {"mode": "echo", "text": "q" * 100},
                                  "call_id": "ctr-post"})
        self.assertEqual((status, body["code"]), (502, "PostconditionFailed"))

    def test_the_built_in_grep_runs_in_a_container_on_the_workspace(self):
        status, body = self.post({"name": "grep", "input": {"pattern": "needle", "files": "*.txt"},
                                  "workspace": "agent-1", "call_id": "ctr-grep"})
        self.assertEqual(status, 200, body)
        self.assertTrue(body["result"]["ok"], body)
        self.assertIn("notes.txt:2:the needle is here", body["result"]["output"])
        self.assertEqual((body["capability"]["version"], body["capability"]["label"]), (TOOL_VERSIONS["grep"], "Tested"))

    def test_the_built_in_git_log_reads_a_repository_owned_by_another_user(self):
        status, body = self.post({"name": "git-log", "input": {"n": 5}, "workspace": "agent-1", "call_id": "ctr-git"})
        self.assertEqual(status, 200, body)
        self.assertTrue(body["result"]["ok"], body)
        self.assertRegex(body["result"]["output"], re.compile(r"^[0-9a-f]{7,40} first commit$", re.M))

    def test_every_container_call_was_recorded_and_no_container_is_left(self):
        self.post({"name": "text-count", "input": {"text": "a"}, "call_id": "ctr-last"})
        self.assertTrue(records(self.env, "ctr-last"))
        listed = subprocess.run(["docker", "ps", "-aq", "--filter", "name=taffee-sbx-"], capture_output=True, text=True,
                                timeout=30)
        self.assertEqual(listed.stdout.strip(), "")


if __name__ == "__main__":
    unittest.main()
