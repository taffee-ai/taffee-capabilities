"""The dummy orchestrator (tests/dummy_orchestrator.py) driving the registry end to end.

Three groups:
- OrchestratorFlowTest: fixture tools and a scripted sandbox. Catalog, plan, grant, revocation, input
  checks, call records and run state. No Docker.
- RealCatalogTest: the repository's own four tools as an orchestrator sees them, and the interface gaps.
- RealToolContainerTest: the real grep run through the orchestrator in the real ContainerSandbox, with a
  sandbox block added in a temporary copy (a rehearsal of T-005 Stage B). Needs the same environment as
  the other container tests: TAFFEE_SANDBOX_IMAGE and TAFFEE_SANDBOX_TMP.
"""

import json
import os
import shutil
import tempfile
import unittest
from pathlib import Path

import yaml

from capability.callrecord import CallRecorder, verify_log
from capability.execute import WORK
from capability.index import build_index
from capability.revocation import RevocationClient, load_public_key
from capability.sandbox import ContainerSandbox, RunResult, docker_available

from dummy_orchestrator import (
    DummyOrchestrator, FakeGrants, ScriptedPlanner, ToolCall, interface_gaps,
)
from fixture_support import REPO, new_tree, tool_manifest, write_manifest
from revocation_support import Clocks, FakeFeed, make_key

IMAGE = os.environ.get("TAFFEE_SANDBOX_IMAGE")
SKIP_REASON = "TAFFEE_SANDBOX_IMAGE is not set" if not IMAGE else docker_available()
REAL_TOOLS = ("git-log", "glob", "grep", "unawaited-async")

ECHO_SCHEMA = {
    "type": "object", "additionalProperties": False, "required": ["text"],
    "properties": {"text": {"type": "string", "description": "The text to echo back.", "maxLength": 100}},
}
SECRET_SCHEMA = {
    "type": "object", "additionalProperties": False, "required": ["key"],
    "properties": {"key": {"type": "string"}},
}


def fixture_manifest(name, schema, requires, sandbox=None):
    manifest = tool_manifest(name, [{"language": "python", "location": f"library/{name}/python"}])
    manifest["summary"] = f"The {name} fixture."
    manifest["requires"] = list(requires)
    manifest["spec"]["inputSchema"] = schema
    manifest["spec"]["postconditions"] = [{"type": "max_bytes", "value": 200}]
    manifest["spec"]["sandbox"] = sandbox or {"filesystem": "read-root"}
    return manifest


def ok(output):
    return RunResult(0, (json.dumps({"ok": True, "output": output}) + "\n").encode(), b"", False, False)


class ScriptSandbox:
    """A sandbox double: answers from the request on stdin, records what it was asked. Runs nothing."""

    name = "script"

    def __init__(self, respond):
        self.respond = respond
        self.calls = []

    def run(self, argv, *, root, policy, stdin=b"", env_values=None, implementation=None):
        request = json.loads(stdin)
        self.calls.append({"argv": argv, "root": root, "policy": policy, "request": request})
        return self.respond(request["input"])


def echo_or_fail(data):
    if "text" in data:
        if data["text"] == "boom":
            return RunResult(0, b'{"ok": false, "error": "it broke"}\n', b"", False, False)
        if data["text"] == "huge":
            return ok("x" * 500)  # breaks the 200-byte postcondition
        return ok("echo:" + data["text"])
    return ok("secret-value")


class OrchestratorFlowTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        base = Path(self._tmp.name)
        self.root = new_tree(base / "caps")
        for name in ("echo-tool", "secret-tool"):
            folder = self.root / "library" / name / "python"
            folder.mkdir(parents=True)
            (folder / "main.py").write_text("print('never run here')\n", encoding="utf-8")
        write_manifest(self.root, "tool", "echo-tool", fixture_manifest("echo-tool", ECHO_SCHEMA, ["repo.read"]))
        write_manifest(self.root, "tool", "secret-tool", fixture_manifest("secret-tool", SECRET_SCHEMA, ["secrets.read"]))
        build_index(self.root, fixture_mode=True)
        self.project = base / "project"
        self.project.mkdir()

        self.private, encoded = make_key()
        self.clocks = Clocks()
        self.client = RevocationClient(load_public_key(encoded), wall=self.clocks.wall, monotonic=self.clocks.mono)
        self.feed = FakeFeed(self.clocks, self.private)
        self.client.poll(self.feed)
        self.assertTrue(self.client.is_fresh())

        self.log = base / "calls.jsonl"
        self.recorder = CallRecorder(self.log, clock=self.clocks.wall, fsync=False)
        self.sandbox = ScriptSandbox(echo_or_fail)
        self.grants = FakeGrants({"agent:alice": {"repo.read"}, "agent:root": {"repo.read", "secrets.read"}})
        self.orch = DummyOrchestrator(self.root, revocation=self.client, feed=self.feed, sandbox=self.sandbox,
                                      grants=self.grants, recorder=self.recorder)

    def tearDown(self):
        self._tmp.cleanup()

    def run_calls(self, principal, *calls, stop_on_failure=True):
        planner = ScriptedPlanner(lambda definitions: list(calls))
        return self.orch.run(principal, planner, project_root=self.project, stop_on_failure=stop_on_failure)

    def records(self):
        return verify_log(self.log)

    # Construction

    def test_an_orchestrator_without_a_revocation_client_is_refused(self):
        with self.assertRaises(ValueError):
            DummyOrchestrator(self.root, revocation=None, sandbox=self.sandbox, grants=self.grants,
                              recorder=self.recorder)

    # Catalog

    def test_the_catalog_gives_model_facing_definitions_built_only_from_the_manifest(self):
        definitions, bindings = self.orch.catalog()
        self.assertEqual([d["name"] for d in definitions], ["echo-tool", "secret-tool"])
        echo = definitions[0]
        self.assertEqual(set(echo), {"name", "description", "input_schema"})
        self.assertEqual(echo["description"], "The echo-tool fixture.")
        self.assertEqual(echo["input_schema"], ECHO_SCHEMA)
        binding = bindings["echo-tool"]
        self.assertEqual((binding.version, binding.requires), ("1.0.0", ("repo.read",)))
        self.assertTrue(binding.identity.startswith("sha256:"))

    def test_the_planner_sees_the_definitions_and_nothing_else(self):
        seen = []
        planner = ScriptedPlanner(lambda definitions: seen.append(definitions) or [])
        self.orch.run("agent:alice", planner, project_root=self.project)
        self.assertEqual(seen[0], self.orch.catalog()[0])

    def test_a_planner_editing_its_copy_does_not_change_the_catalog(self):
        def vandal(definitions):
            definitions[0]["input_schema"]["properties"].clear()
            return [ToolCall("echo-tool", {"text": "hi"})]
        report = self.orch.run("agent:alice", ScriptedPlanner(vandal), project_root=self.project)
        self.assertEqual(report.steps[0].status, "ok")
        self.assertEqual(self.orch.catalog()[0][0]["input_schema"], ECHO_SCHEMA)

    # Dispatch

    def test_a_granted_valid_call_runs_and_leaves_one_record_with_the_grant(self):
        report = self.run_calls("agent:alice", ToolCall("echo-tool", {"text": "hi"}))
        self.assertTrue(report.ok)
        step = report.steps[0]
        self.assertEqual((step.status, step.output, step.version), ("ok", "echo:hi", "1.0.0"))
        [record] = self.records()
        self.assertEqual(record["outcome"], {"status": "ok", "code": None})
        self.assertEqual(record["grant"], "grant:agent:alice/echo-tool/1.0.0")
        self.assertEqual(record["call_id"], step.call_id)

    def test_the_policy_comes_from_the_manifest_and_the_tool_sees_the_container_root(self):
        self.run_calls("agent:alice", ToolCall("echo-tool", {"text": "hi"}))
        [call] = self.sandbox.calls
        self.assertEqual(call["policy"].filesystem, "read-root")
        self.assertEqual(call["policy"].timeout_s, 5.0)
        self.assertEqual(call["request"]["root"], WORK)
        self.assertEqual(Path(call["root"]), self.project)

    def test_a_name_the_planner_was_not_offered_is_refused_without_running(self):
        report = self.run_calls("agent:alice", ToolCall("rm-rf", {}))
        self.assertEqual((report.steps[0].status, report.steps[0].code), ("refused", "NotOffered"))
        self.assertEqual(self.sandbox.calls, [])

    def test_a_denied_grant_never_runs_and_leaves_a_refused_record(self):
        report = self.run_calls("agent:alice", ToolCall("secret-tool", {"key": "k"}))
        self.assertEqual((report.steps[0].status, report.steps[0].code), ("refused", "GrantDenied"))
        self.assertEqual(self.sandbox.calls, [])
        [record] = self.records()
        self.assertEqual(record["outcome"], {"status": "refused", "code": "GrantDenied"})
        self.assertIsNone(record["grant"])

    def test_a_principal_holding_the_permission_may_run_the_same_tool(self):
        report = self.run_calls("agent:root", ToolCall("secret-tool", {"key": "k"}))
        self.assertEqual(report.steps[0].status, "ok")

    def test_bad_arguments_are_rejected_before_the_sandbox_with_the_field_named(self):
        report = self.run_calls("agent:alice", ToolCall("echo-tool", {"text": 42}))
        step = report.steps[0]
        self.assertEqual((step.status, step.code), ("input_rejected", "ValidationFailed"))
        self.assertIn("/text", step.error)
        self.assertEqual(self.sandbox.calls, [])
        self.assertEqual(self.records()[0]["outcome"]["status"], "input_rejected")

    def test_a_tool_failure_is_a_result_not_an_exception(self):
        report = self.run_calls("agent:alice", ToolCall("echo-tool", {"text": "boom"}))
        step = report.steps[0]
        self.assertEqual((step.status, step.error), ("tool_failed", "it broke"))

    def test_output_breaking_a_postcondition_is_rejected(self):
        report = self.run_calls("agent:alice", ToolCall("echo-tool", {"text": "huge"}))
        self.assertEqual((report.steps[0].status, report.steps[0].code), ("output_rejected", "PostconditionFailed"))

    # Revocation

    def test_a_revocation_published_after_the_catalog_stops_the_call(self):
        def revoke_then_call(definitions):
            self.feed.publish([{"kind": "tool", "name": "echo-tool", "version": "1.0.0"}])
            return [ToolCall("echo-tool", {"text": "hi"})]
        report = self.orch.run("agent:alice", ScriptedPlanner(revoke_then_call), project_root=self.project)
        self.assertEqual((report.steps[0].status, report.steps[0].code), ("refused", "Revoked"))
        self.assertEqual(self.sandbox.calls, [])

    def test_a_feed_that_goes_down_fails_closed_once_freshness_runs_out(self):
        self.feed.up = False
        self.clocks.advance(31)
        report = self.run_calls("agent:alice", ToolCall("echo-tool", {"text": "hi"}))
        self.assertEqual((report.steps[0].status, report.steps[0].code), ("refused", "RevocationStale"))
        self.assertEqual(self.sandbox.calls, [])

    def test_a_short_outage_inside_the_freshness_bound_does_not_stop_calls(self):
        self.feed.up = False
        self.clocks.advance(10)
        report = self.run_calls("agent:alice", ToolCall("echo-tool", {"text": "hi"}))
        self.assertEqual(report.steps[0].status, "ok")

    # Run state

    def test_a_run_stops_at_the_first_failure_by_default(self):
        report = self.run_calls("agent:alice", ToolCall("echo-tool", {"text": "a"}),
                                ToolCall("echo-tool", {"text": "boom"}), ToolCall("echo-tool", {"text": "c"}))
        self.assertEqual([s.status for s in report.steps], ["ok", "tool_failed"])
        self.assertFalse(report.ok)
        self.assertEqual(len(self.records()), 2)

    def test_a_run_can_continue_past_failures(self):
        report = self.run_calls("agent:alice", ToolCall("echo-tool", {"text": "a"}),
                                ToolCall("secret-tool", {"key": "k"}), ToolCall("echo-tool", {"text": "c"}),
                                stop_on_failure=False)
        self.assertEqual([s.status for s in report.steps], ["ok", "refused", "ok"])
        records = self.records()
        self.assertEqual([r["seq"] for r in records], [1, 2, 3])
        self.assertEqual(len({s.call_id for s in report.steps}), 3)

    # Interface findings

    def test_gaps_name_undescribed_parameters_and_the_missing_output_and_error_contracts(self):
        definitions, _ = self.orch.catalog()
        manifests = {d["name"]: self.orch.registry.describe(d["name"], kind="tool")["manifest"] for d in definitions}
        gaps = interface_gaps(definitions, manifests)
        self.assertNotIn("parameter 'text' has no description", gaps["echo-tool"])
        self.assertIn("parameter 'key' has no description", gaps["secret-tool"])
        for name in ("echo-tool", "secret-tool"):
            self.assertIn("no declared output format (the result is an opaque string)", gaps[name])
            self.assertIn("no declared error cases", gaps[name])


def copy_repo_tree(target: Path, sandbox_for=(), timeout_ms=None) -> Path:
    """A copy of the repository's capability tree. Tools in `sandbox_for` get a read-root sandbox block and,
    when `timeout_ms` is given, that time limit."""
    target.mkdir(parents=True, exist_ok=True)
    for folder in ("schemas", "policy", "catalog", "library", "evidence"):
        shutil.copytree(REPO / folder, target / folder)
    for name in sandbox_for:
        path = target / "catalog" / "tool" / name / "manifest.yaml"
        manifest = yaml.safe_load(path.read_text(encoding="utf-8"))
        manifest["spec"]["sandbox"] = {"filesystem": "read-root"}
        if timeout_ms is not None:
            manifest["spec"]["timeoutMs"] = timeout_ms
        path.write_text(yaml.safe_dump(manifest, sort_keys=False), encoding="utf-8")
    build_index(target, fixture_mode=True)
    return target


class RealCatalogTest(unittest.TestCase):
    """The four real tools, seen by an orchestrator. No tool is run except to show the Stage B refusal."""

    @classmethod
    def setUpClass(cls):
        cls._tmp = tempfile.TemporaryDirectory()
        base = Path(cls._tmp.name)
        cls.root = copy_repo_tree(base / "caps")
        cls.project = base / "project"
        cls.project.mkdir()
        private, encoded = make_key()
        cls.clocks = Clocks()
        cls.client = RevocationClient(load_public_key(encoded), wall=cls.clocks.wall, monotonic=cls.clocks.mono)
        cls.feed = FakeFeed(cls.clocks, private)
        cls.client.poll(cls.feed)
        cls.log = base / "calls.jsonl"
        cls.sandbox = ScriptSandbox(lambda data: ok("unused"))
        cls.orch = DummyOrchestrator(cls.root, revocation=cls.client, feed=cls.feed, sandbox=cls.sandbox,
                                     grants=FakeGrants({"agent:alice": {"repo.read", "context.read"}}),
                                     recorder=CallRecorder(cls.log, clock=cls.clocks.wall, fsync=False))
        cls.definitions, cls.bindings = cls.orch.catalog()

    @classmethod
    def tearDownClass(cls):
        cls._tmp.cleanup()

    def test_the_four_python_tools_are_offered_and_graph_query_is_not(self):
        self.assertEqual(sorted(self.bindings), sorted(REAL_TOOLS))

    def test_every_offered_definition_has_a_summary_and_an_object_schema(self):
        for definition in self.definitions:
            with self.subTest(tool=definition["name"]):
                self.assertTrue(definition["description"])
                self.assertEqual(definition["input_schema"]["type"], "object")

    def test_the_current_interface_gaps_of_the_real_tools(self):
        """Pins today's findings. When a manifest or the schema closes a gap, update this table."""
        manifests = {name: self.orch.registry.describe(name, kind="tool")["manifest"] for name in self.bindings}
        gaps = interface_gaps(self.definitions, manifests)
        contract = ["no declared output format (the result is an opaque string)", "no declared error cases"]
        self.assertEqual(gaps, {
            "git-log": ["parameter 'n' has no description"] + contract,
            "glob": ["parameter 'pattern' has no description"] + contract,
            "grep": ["parameter 'files' has no description", "parameter 'pattern' has no description"] + contract,
            "unawaited-async": ["parameter 'files' has no description"] + contract,
        })

    def test_every_real_tool_is_refused_at_dispatch_until_it_declares_a_sandbox(self):
        arguments = {"git-log": {"n": 1}, "glob": {"pattern": "*.py"},
                     "grep": {"pattern": "x", "files": "*.py"}, "unawaited-async": {"files": "*.ts"}}
        calls = [ToolCall(name, arguments[name]) for name in REAL_TOOLS]
        report = self.orch.run("agent:alice", ScriptedPlanner(lambda d: calls), project_root=self.project,
                               stop_on_failure=False)
        self.assertEqual([(s.status, s.code) for s in report.steps], [("refused", "SandboxUnavailable")] * 4)
        self.assertEqual(self.sandbox.calls, [])


@unittest.skipIf(SKIP_REASON is not None, f"container tests need Docker: {SKIP_REASON}")
class RealToolContainerTest(unittest.TestCase):
    """The real grep, its manifest given a read-root sandbox in a temporary copy, run in a real container."""

    @classmethod
    def setUpClass(cls):
        cls._tmp = tempfile.TemporaryDirectory(dir=os.environ.get("TAFFEE_SANDBOX_TMP"))
        base = Path(cls._tmp.name).resolve()
        os.chmod(base, 0o755)
        # The real tools declare timeoutMs 5000, and the sandbox counts container start-up against it. On the
        # test host a cold container start alone can take several seconds, so 5000 timed glob out. Stage B has
        # to decide whether timeoutMs covers start-up; until then this rehearsal declares 30000.
        cls.root = copy_repo_tree(base / "caps", sandbox_for=("grep", "glob"), timeout_ms=30000)
        for path in (cls.root, *cls.root.rglob("*")):
            path.chmod(0o755 if path.is_dir() else 0o644)
        cls.project = base / "project"
        cls.project.mkdir()
        (cls.project / "notes.txt").write_text("first line\nthe needle is here\nlast line\n", encoding="utf-8")
        for path in (cls.project, *cls.project.rglob("*")):
            path.chmod(0o755 if path.is_dir() else 0o644)
        private, encoded = make_key()
        cls.clocks = Clocks()
        cls.client = RevocationClient(load_public_key(encoded), wall=cls.clocks.wall, monotonic=cls.clocks.mono)
        cls.feed = FakeFeed(cls.clocks, private)
        cls.client.poll(cls.feed)
        cls.log = base / "calls.jsonl"
        cls.orch = DummyOrchestrator(
            cls.root, revocation=cls.client, feed=cls.feed,
            sandbox=ContainerSandbox(IMAGE, allowed_mount_roots=(base,), cleanup_wait_s=30.0),
            grants=FakeGrants({"agent:alice": {"repo.read"}}),
            recorder=CallRecorder(cls.log, clock=cls.clocks.wall, fsync=False))

    @classmethod
    def tearDownClass(cls):
        cls._tmp.cleanup()

    def test_grep_and_glob_run_for_real_through_the_orchestrator(self):
        calls = [ToolCall("glob", {"pattern": "*.txt"}), ToolCall("grep", {"pattern": "needle", "files": "*.txt"})]
        report = self.orch.run("agent:alice", ScriptedPlanner(lambda d: calls), project_root=self.project)
        self.assertEqual([s.status for s in report.steps], ["ok", "ok"], [s.error for s in report.steps])
        self.assertIn("notes.txt", report.steps[0].output)
        self.assertIn("notes.txt:2:the needle is here", report.steps[1].output)
        self.assertEqual([r["outcome"]["status"] for r in verify_log(self.log)], ["ok", "ok"])


if __name__ == "__main__":
    unittest.main()
