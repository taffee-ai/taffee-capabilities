"""T-005 Stage A: run a tool under the sandbox policy its manifest declares.

The manifest block, `policy_from_spec`, the executor `run_tool` with a recording double, the S-012 outcomes,
and, with Docker, the exit-gate tests: a disallowed file or network access is refused by the real
ContainerSandbox. The container tests need the same environment as the S-011 ones:
    TAFFEE_SANDBOX_IMAGE=taffee-cap-base:dev   TAFFEE_SANDBOX_TMP=/path/visible/to/both
Without them each is skipped with the reason.

Stage A edits no tool manifest, so the four built-in tools declare no policy and are refused here
(see BuiltInToolsTest). Stage B (declaring the policy on them) is a separate step.
"""

import copy
import inspect
import json
import os
import socket
import subprocess
import sys
import tempfile
import threading
import unittest
from pathlib import Path

import yaml

from capability.calltime import CallError
from capability.callrecord import CallRecorder, verify_log
from capability.execute import DEFAULTS, TOOL_ERRORS, WORK, policy_from_spec, run_tool
from capability.index import build_index
from capability.manifest import validate_manifest
from capability.registry import Registry
from capability.sandbox import (
    ContainerSandbox, ProcessSandbox, RunResult, SandboxCleanupFailed, SandboxPolicy, SandboxUnavailable,
    docker_available, required,
)

from fixture_support import REPO, new_tree, tool_manifest, write_manifest

IMAGE = os.environ.get("TAFFEE_SANDBOX_IMAGE")
SKIP_REASON = "TAFFEE_SANDBOX_IMAGE is not set" if not IMAGE else docker_available()
OPEN = SandboxPolicy(filesystem="unrestricted", network="allowed")

TOOL_SOURCE = '''import json, os, socket, sys, time
req = json.loads(sys.stdin.read())
data = req["input"]
mode = data["mode"]


def out(text):
    print(json.dumps({"ok": True, "output": text}))


try:
    if mode == "echo":
        out("hello")
    elif mode == "root":
        out(req["root"])
    elif mode == "read":
        out("read:" + open(data["path"]).read().strip())
    elif mode == "write":
        open(data["path"], "w").write("x")
        out("wrote")
    elif mode == "connect":
        socket.create_connection((data["host"], data["port"]), timeout=3).close()
        out("connected")
    elif mode == "memory":
        block = bytearray(700 * 1024 * 1024)
        block[::4096] = b"x" * len(block[::4096])
        out("allocated")
    elif mode == "sleep":
        time.sleep(60)
        out("woke")
    elif mode == "flood":
        out("x" * 5000)
    elif mode == "empty":
        print('{"ok":true,"output":""}')
    else:
        out("unknown")
except Exception as exc:
    out("denied:" + type(exc).__name__)
'''

INPUT_SCHEMA = {
    "type": "object", "additionalProperties": False, "required": ["mode"],
    "properties": {"mode": {"type": "string"}, "path": {"type": "string"}, "host": {"type": "string"},
                   "port": {"type": "integer"}},
}


def manifest_for(name, sandbox="default", timeout_ms=5000):
    manifest = tool_manifest(name, [{"language": "python", "location": f"library/{name}/python"}])
    spec = manifest["spec"]
    spec["inputSchema"] = copy.deepcopy(INPUT_SCHEMA)
    spec["timeoutMs"] = timeout_ms
    spec["postconditions"] = [{"type": "max_bytes", "value": 4096}]
    if sandbox == "default":
        sandbox = {"filesystem": "read-root"}
    if sandbox is not None:
        spec["sandbox"] = sandbox
    return manifest


def build_tree(base, tools):
    """A capability tree with the given tools {name: manifest}, every tool using TOOL_SOURCE. Returns its root."""
    root = Path(base) / "caps"
    root.mkdir()
    new_tree(root)
    for name, manifest in tools.items():
        folder = root / "library" / name / "python"
        folder.mkdir(parents=True)
        (folder / "main.py").write_text(TOOL_SOURCE, encoding="utf-8")
        write_manifest(root, "tool", name, manifest)
    build_index(root, fixture_mode=True)
    for path in (root, *root.rglob("*")):
        path.chmod(0o755 if path.is_dir() else 0o644)  # the container user must be able to read the tool
    return root


def ok_line(output="x"):
    return (json.dumps({"ok": True, "output": output}) + "\n").encode()


ECHO = {"mode": "echo"}


class DoubleSandbox:
    """Records the call it is given and returns a canned RunResult (or raises)."""

    def __init__(self, result=None, error=None):
        self.result = result or RunResult(0, ok_line("hello"), b"", False, False)
        self.error = error
        self.calls = []

    def run(self, argv, *, root, policy, stdin=b"", env_values=None, implementation=None):
        self.calls.append({"argv": argv, "root": root, "policy": policy, "stdin": stdin, "implementation": implementation})
        if self.error is not None:
            raise self.error
        return self.result


class ManifestBlockTest(unittest.TestCase):
    def errors(self, **spec_changes):
        manifest = manifest_for("probe-tool")
        manifest["spec"].update(spec_changes)
        return validate_manifest(manifest)

    def block(self, **changes):
        block = {"filesystem": "read-root"}
        block.update(changes)
        return self.errors(sandbox=block)

    def test_a_full_valid_block_is_accepted(self):
        self.assertEqual(self.block(memoryMb=512, outputBytes=2048, stdinBytes=64, network="none", env=["TOKEN", "HOME_DIR"]), [])

    def test_the_block_is_optional_and_filesystem_is_required_inside_it(self):
        self.assertEqual(validate_manifest(manifest_for("probe-tool", sandbox=None)), [])
        self.assertTrue(self.errors(sandbox={}))
        self.assertTrue(self.errors(sandbox={"memoryMb": 64}))

    def test_every_declarable_filesystem_value_is_accepted_and_the_opt_out_is_not(self):
        for value in ("none", "read-root", "write-root"):
            with self.subTest(filesystem=value):
                self.assertEqual(self.block(filesystem=value), [])
        for value in ("unrestricted", "", "all", None, 5):
            with self.subTest(filesystem=value):
                self.assertTrue(self.block(filesystem=value))

    def test_network_allowed_cannot_be_declared(self):
        for value in ("allowed", "", None, True):
            with self.subTest(network=value):
                self.assertTrue(self.block(network=value))

    def test_out_of_range_and_wrong_type_values_are_refused(self):
        for field, bad in (("memoryMb", 15), ("memoryMb", 8193), ("memoryMb", "256"), ("memoryMb", True), ("memoryMb", 64.5),
                           ("outputBytes", 0), ("outputBytes", 1), ("outputBytes", 23), ("outputBytes", 63), ("outputBytes", 65537), ("outputBytes", 1048576),
                           ("stdinBytes", -1),
                           ("stdinBytes", 0), ("stdinBytes", 32), ("stdinBytes", 63), ("stdinBytes", 1048577)):
            with self.subTest(field=field, value=bad):
                self.assertTrue(self.block(**{field: bad}))
        for field, good in (("memoryMb", 16), ("memoryMb", 8192), ("outputBytes", 64), ("outputBytes", 65536),
                            ("stdinBytes", 64), ("stdinBytes", 1048576)):
            with self.subTest(field=field, value=good):
                self.assertEqual(self.block(**{field: good}), [])

    def test_env_must_be_a_unique_list_of_upper_case_names(self):
        for bad in ("TOKEN", ["token"], ["A B"], ["A", "A"], [5], ["X"] * 17, {"A": 1}, ["1ABC"]):
            with self.subTest(env=bad):
                self.assertTrue(self.block(env=bad))

    def test_unknown_fields_in_the_block_are_refused(self):
        self.assertTrue(self.block(timeoutMs=1000))
        self.assertTrue(self.block(image="evil:latest"))
        self.assertTrue(self.block(mounts=["/"]))

    def test_the_time_limit_is_constrained_to_what_the_sandbox_can_run(self):
        """A valid manifest must not be unexecutable (S-011 allows 100 to 600000 ms)."""
        for good in (100, 5000, 600000):
            with self.subTest(timeoutMs=good):
                self.assertEqual(self.errors(timeoutMs=good), [])
        for bad in (0, 1, 99, 600001, "5000", 1.5, True):
            with self.subTest(timeoutMs=bad):
                self.assertTrue(self.errors(timeoutMs=bad))


class PolicyFromSpecTest(unittest.TestCase):
    def spec(self, **block):
        return manifest_for("probe-tool", sandbox=dict({"filesystem": "read-root"}, **block))["spec"]

    def test_defaults_and_the_time_limit_from_timeoutMs(self):
        policy = policy_from_spec(self.spec())
        self.assertEqual(policy, SandboxPolicy(timeout_s=5.0, memory_mb=256, output_bytes=65536, stdin_bytes=65536,
                                               filesystem="read-root", network="none", env=()))
        self.assertEqual((DEFAULTS["memoryMb"], DEFAULTS["outputBytes"], DEFAULTS["stdinBytes"]), (256, 65536, 65536))

    def test_every_field_is_mapped(self):
        spec = self.spec(memoryMb=64, outputBytes=2048, stdinBytes=100, filesystem="write-root", network="none", env=["A", "B_C"])
        spec["timeoutMs"] = 1500
        self.assertEqual(policy_from_spec(spec), SandboxPolicy(timeout_s=1.5, memory_mb=64, output_bytes=2048, stdin_bytes=100,
                                                               filesystem="write-root", network="none", env=("A", "B_C")))

    def test_a_declared_policy_never_asks_for_an_opt_out(self):
        for fs in ("none", "read-root", "write-root"):
            policy = policy_from_spec(self.spec(filesystem=fs))
            self.assertNotEqual(policy.filesystem, "unrestricted")
            self.assertEqual(policy.network, "none")
            self.assertIn("network", required(policy))
            self.assertEqual("filesystem" in required(policy), True)

    def test_no_declaration_is_sandbox_unavailable(self):
        with self.assertRaises(SandboxUnavailable) as ctx:
            policy_from_spec(manifest_for("probe-tool", sandbox=None)["spec"])
        self.assertEqual(ctx.exception.code, "SandboxUnavailable")


class ExecutorTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.base = Path(self._tmp.name).resolve()
        self.root = build_tree(self.base, {"probe-tool": manifest_for("probe-tool"), "bare-tool": manifest_for("bare-tool", sandbox=None)})
        self.registry = Registry(self.root)
        self.project = self.base / "project"
        self.project.mkdir()
        self.log = self.base / "calls.jsonl"

    def tearDown(self):
        self._tmp.cleanup()

    def run_it(self, sandbox, data=ECHO, name="probe-tool", **kwargs):
        return run_tool(self.registry, name, data, project_root=self.project, sandbox=sandbox, **kwargs)

    def test_the_policy_cannot_be_supplied_by_the_caller(self):
        parameters = inspect.signature(run_tool).parameters
        for name in ("policy", "spec", "sandbox_policy", "env_values", "implementation", "mounts"):
            self.assertNotIn(name, parameters)
        with self.assertRaises(TypeError):
            self.run_it(DoubleSandbox(), policy=SandboxPolicy())

    def test_the_call_the_sandbox_receives(self):
        double = DoubleSandbox()
        result = self.run_it(double, {"mode": "echo"})
        self.assertEqual(result, {"ok": True, "output": "hello"})
        call = double.calls[0]
        self.assertEqual(call["argv"], ["python3", "/impl/main.py"])
        self.assertEqual(call["root"], self.project)
        self.assertEqual(call["implementation"], self.root / "library" / "probe-tool" / "python")
        self.assertEqual(call["policy"], SandboxPolicy(timeout_s=5.0, filesystem="read-root"))
        self.assertEqual(json.loads(call["stdin"]), {"root": WORK, "input": {"mode": "echo"}})

    def test_the_host_path_never_reaches_the_tool(self):
        double = DoubleSandbox()
        self.run_it(double, {"mode": "echo"})
        stdin = double.calls[0]["stdin"].decode()
        for secret in (str(self.project), str(self.root), str(self.base)):
            self.assertNotIn(secret, stdin)
        for secret in (str(self.project), str(self.base)):
            self.assertNotIn(secret, " ".join(double.calls[0]["argv"]))

    def test_a_tool_with_no_declaration_is_refused_and_nothing_runs(self):
        double = DoubleSandbox()
        with self.assertRaises(SandboxUnavailable) as ctx:
            self.run_it(double, name="bare-tool")
        self.assertEqual(ctx.exception.code, "SandboxUnavailable")
        self.assertEqual(double.calls, [])

    def test_a_sandbox_that_cannot_enforce_the_declared_limits_refuses_and_starts_nothing(self):
        marker = self.base / "ran"
        source = TOOL_SOURCE + f"\nopen({str(marker)!r}, 'w').write('ran')\n"
        (self.root / "library" / "probe-tool" / "python" / "main.py").write_text(source, encoding="utf-8")
        # the file digest changed, so the index no longer verifies: rebuild it as the author would
        build_index(self.root, fixture_mode=True)
        with self.assertRaises(SandboxUnavailable):
            self.run_it(ProcessSandbox())  # cannot confine files or the network
        self.assertFalse(marker.exists())

    def test_invalid_input_is_refused_before_the_sandbox_is_called(self):
        double = DoubleSandbox()
        for bad in ({}, {"mode": 5}, {"mode": "echo", "extra": 1}, [], "echo", None):
            with self.subTest(data=str(bad)):
                with self.assertRaises(CallError) as ctx:
                    self.run_it(double, bad)
                self.assertEqual(ctx.exception.code, "ValidationFailed")
        self.assertEqual(double.calls, [])

    def test_a_missing_tool_is_a_registry_error_and_nothing_runs(self):
        from capability.registry import RegistryError
        double = DoubleSandbox()
        with self.assertRaises(RegistryError):
            self.run_it(double, name="no-such-tool")
        self.assertEqual(double.calls, [])

    def test_a_tampered_tool_does_not_run(self):
        from capability.registry import RegistryError
        (self.root / "library" / "probe-tool" / "python" / "main.py").write_text("print('evil')\n", encoding="utf-8")
        double = DoubleSandbox()
        with self.assertRaises(RegistryError) as ctx:
            self.run_it(double)
        self.assertEqual(ctx.exception.code, "IntegrityError")
        self.assertEqual(double.calls, [])

    def test_failures_reported_by_the_sandbox_become_closed_error_results(self):
        cases = {
            "timeout": RunResult(None, b"", b"secret stderr", True, False),
            "output": RunResult(0, ok_line() * 3, b"", False, True),
            "exit": RunResult(137, b"", b"secret stderr traceback", False, False),
        }
        for kind, done in cases.items():
            with self.subTest(kind=kind):
                result = self.run_it(DoubleSandbox(done))
                self.assertEqual(result, {"ok": False, "error": TOOL_ERRORS[kind]})
                self.assertNotIn("secret", json.dumps(result))

    def test_a_bad_result_line_is_a_closed_error_and_no_tool_text_is_copied(self):
        for label, stdout in {
            "not json": b"SECRET not json\n", "two lines": ok_line() + ok_line(), "empty": b"", "array": b"[]\n",
            "ok as 1": b'{"ok": 1, "output": "x"}\n', "nan": b'{"ok": true, "output": "x", "n": NaN}\n',
            "duplicate key": b'{"ok": true, "ok": false, "output": "x"}\n',
            "duplicate output key": b'{"output": "first", "output": "second", "ok": true}\n',
            "duplicate error key": b'{"ok": false, "error": "first", "error": "second"}\n', "invalid utf-8": b'{"ok": true, "output": "\xff"}\n',
            "extra key": b'{"ok": true, "output": "x", "extra": 1}\n', "no output": b'{"ok": true}\n',
            "empty error": b'{"ok": false, "error": ""}\n', "long error": b'{"ok": false, "error": "' + b"e" * 2049 + b'"}\n',
            "error not string": b'{"ok": false, "error": 5}\n',
        }.items():
            with self.subTest(case=label):
                result = self.run_it(DoubleSandbox(RunResult(0, stdout, b"", False, False)))
                self.assertEqual(result, {"ok": False, "error": TOOL_ERRORS["result"]})

    def test_a_result_with_raw_unicode_line_separators_inside_a_string_is_accepted(self):
        """review c05d0958: str.splitlines() split on these and rejected a valid result."""
        line = ('{"ok": true, "output": "a\u2028b\u2029c\u0085d"}\n').encode("utf-8")
        self.assertEqual(self.run_it(DoubleSandbox(RunResult(0, line, b"", False, False))),
                         {"ok": True, "output": "a\u2028b\u2029c\u0085d"})

    def test_a_result_with_a_stray_line_feed_or_no_content_is_still_refused(self):
        for raw in (b"\n", b"", b'{"ok": true, "output": "x"}\n\n', b'\n{"ok": true, "output": "x"}\n'):
            with self.subTest(raw=raw):
                self.assertEqual(self.run_it(DoubleSandbox(RunResult(0, raw, b"", False, False))),
                                 {"ok": False, "error": TOOL_ERRORS["result"]})

    def test_an_input_larger_than_the_declared_stdin_limit_is_an_input_rejection_not_a_fault(self):
        """review c05d0958: it used to reach the sandbox and raise PolicyError (error/Unexpected)."""
        (self.base / "small").mkdir()
        tree = build_tree(self.base / "small", {"tiny": manifest_for("tiny", {"filesystem": "read-root", "stdinBytes": 64})})
        registry = Registry(tree)
        double = DoubleSandbox()
        run_tool(registry, "tiny", {"mode": "echo"}, project_root=self.project, sandbox=double)  # fits in 64 bytes
        self.assertEqual(len(double.calls), 1)
        double = DoubleSandbox()
        with self.assertRaises(CallError) as ctx:
            run_tool(registry, "tiny", {"mode": "echo", "path": "p" * 100}, project_root=self.project, sandbox=double)
        self.assertEqual(ctx.exception.code, "ValidationFailed")
        self.assertEqual(double.calls, [])
        recorder = CallRecorder(self.log, fsync=False)
        with self.assertRaises(CallError):
            run_tool(registry, "tiny", {"mode": "echo", "path": "p" * 100}, project_root=self.project, sandbox=double,
                     recorder=recorder, call_id="c-9.1")
        self.assertEqual(self.outcomes(), [("input_rejected", "ValidationFailed")])

    def test_a_tool_that_reports_its_own_failure_passes_through(self):
        line = (json.dumps({"ok": False, "error": "no such file"}) + "\n").encode()
        self.assertEqual(self.run_it(DoubleSandbox(RunResult(0, line, b"", False, False))), {"ok": False, "error": "no such file"})

    def test_output_over_the_declared_postcondition_is_refused(self):
        big = ok_line("x" * 5000)
        with self.assertRaises(CallError) as ctx:
            self.run_it(DoubleSandbox(RunResult(0, big, b"", False, False)))
        self.assertEqual(ctx.exception.code, "PostconditionFailed")

    def test_unconfirmed_cleanup_is_never_a_clean_or_tool_failed_result(self):
        with self.assertRaises(SandboxCleanupFailed):
            self.run_it(DoubleSandbox(error=SandboxCleanupFailed("taffee-sbx-0123456789abcdef")))

    def outcomes(self):
        return [(r["outcome"]["status"], r["outcome"]["code"]) for r in verify_log(self.log)]

    def test_every_call_is_recorded_with_the_closed_outcome_table(self):
        recorder = CallRecorder(self.log, fsync=False)
        n = [0]

        def go(sandbox, data=ECHO, name="probe-tool"):
            n[0] += 1
            try:
                return self.run_it(sandbox, data, name=name, recorder=recorder, call_id=f"c-{n[0]}.1")
            except Exception:
                return None
        go(DoubleSandbox())                                                                  # ok
        go(DoubleSandbox(RunResult(0, ok_line() * 2, b"", False, False)))                    # tool_failed (bad result)
        go(DoubleSandbox(RunResult(None, b"", b"", True, False)))                            # tool_failed (timeout)
        go(DoubleSandbox(), {"mode": 5})                                                     # input_rejected
        go(DoubleSandbox(RunResult(0, ok_line("x" * 5000), b"", False, False)))             # output_rejected
        go(DoubleSandbox(), name="bare-tool")                                                # refused SandboxUnavailable
        go(DoubleSandbox(error=SandboxCleanupFailed("taffee-sbx-0123456789abcdef")))         # error Unexpected
        self.assertEqual(self.outcomes(), [
            ("ok", None), ("tool_failed", None), ("tool_failed", None), ("input_rejected", "ValidationFailed"),
            ("output_rejected", "PostconditionFailed"), ("refused", "SandboxUnavailable"), ("error", "Unexpected")])

    def test_the_record_holds_references_never_content(self):
        recorder = CallRecorder(self.log, fsync=False)
        self.run_it(DoubleSandbox(RunResult(0, ok_line("SECRET OUTPUT"), b"", False, False)), {"mode": "echo"},
                    recorder=recorder, call_id="c-1.1")
        raw = self.log.read_text()
        self.assertNotIn("SECRET OUTPUT", raw)
        self.assertNotIn(str(self.project), raw)
        record = verify_log(self.log)[0]
        self.assertEqual(record["capability"]["name"], "probe-tool")
        self.assertEqual(record["language"], "python")
        self.assertTrue(record["code_digest"].startswith("sha256:"))
        self.assertEqual(record["call_id"], "c-1.1")

    def test_a_recorder_without_a_valid_call_id_fails_before_anything_runs(self):
        """review c3b7a671: the audit join must not be skipped silently, and must not be refused after the tool ran."""
        from capability.callrecord import CallRecordError
        recorder = CallRecorder(self.log, fsync=False)
        for bad in (None, "", " ", "a b", "x" * 65, "-lead", "a/b", "a\n", 5, ["c-1.1"]):
            with self.subTest(call_id=repr(bad)):
                double = DoubleSandbox()
                with self.assertRaises(CallRecordError):
                    self.run_it(double, recorder=recorder, call_id=bad)
                self.assertEqual(double.calls, [])  # no sandbox call, so no tool process and no side effect
        self.assertFalse(self.log.exists())

    def test_a_bad_grant_with_a_recorder_fails_before_anything_runs(self):
        from capability.callrecord import CallRecordError
        recorder = CallRecorder(self.log, fsync=False)
        for bad in ("", "has space", "x" * 129, "line\nbreak", 5):
            with self.subTest(grant=repr(bad)):
                double = DoubleSandbox()
                with self.assertRaises(CallRecordError):
                    self.run_it(double, recorder=recorder, call_id="c-1.1", grant=bad)
                self.assertEqual(double.calls, [])
        self.assertFalse(self.log.exists())

    def test_a_valid_call_id_and_grant_run_and_record(self):
        recorder = CallRecorder(self.log, fsync=False)
        self.run_it(DoubleSandbox(), recorder=recorder, call_id="wf-1.2", grant="grant:tools/probe/v1")
        record = verify_log(self.log)[0]
        self.assertEqual((record["call_id"], record["grant"]), ("wf-1.2", "grant:tools/probe/v1"))

    def test_without_a_recorder_the_call_id_is_not_needed_and_nothing_is_written(self):
        double = DoubleSandbox()
        self.run_it(double, call_id="ignored without a recorder")
        self.assertEqual(len(double.calls), 1)
        self.assertFalse(self.log.exists())


class BuiltInToolsTest(unittest.TestCase):
    """Stage A edits no tool manifest: the four built-in tools declare no policy, so the executor refuses them."""

    def test_the_four_manifests_declare_no_policy(self):
        for tool in ("git-log", "glob", "grep", "unawaited-async"):
            with self.subTest(tool=tool):
                manifest = yaml.safe_load((REPO / "catalog" / "tool" / tool / "manifest.yaml").read_text(encoding="utf-8"))
                self.assertNotIn("sandbox", manifest["spec"])

    def test_the_executor_refuses_them_until_stage_b(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "caps"
            root.mkdir()
            import shutil
            for folder in ("schemas", "policy", "catalog", "library"):
                shutil.copytree(REPO / folder, root / folder)
            build_index(root, fixture_mode=True)
            double = DoubleSandbox()
            with self.assertRaises(SandboxUnavailable):
                run_tool(Registry(root), "glob", {"pattern": "*.py"}, project_root=Path(tmp), sandbox=double)
            self.assertEqual(double.calls, [])


class UntouchedArtifactsTest(unittest.TestCase):
    def test_the_committed_evidence_still_checks(self):
        done = subprocess.run([sys.executable, "-m", "capability.evidence", "check", str(REPO)], capture_output=True, text=True,
                              cwd=str(REPO), timeout=300, env=dict(os.environ, PYTHONPATH=str(REPO)))
        self.assertEqual(done.returncode, 0, done.stdout[-500:] + done.stderr[-500:])


def host_ip():
    probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        probe.connect(("10.255.255.255", 1))
        return probe.getsockname()[0]
    finally:
        probe.close()


@unittest.skipIf(SKIP_REASON is not None, f"container tests need Docker: {SKIP_REASON}")
class ContainerExitGateTest(unittest.TestCase):
    """Real ContainerSandbox. The declared policy is read from each tool's manifest by run_tool."""

    @classmethod
    def setUpClass(cls):
        cls._tmp = tempfile.TemporaryDirectory(dir=os.environ.get("TAFFEE_SANDBOX_TMP"))
        cls.base = Path(cls._tmp.name).resolve()
        os.chmod(cls.base, 0o755)
        # A container can take several seconds to start on a busy host. Every tool except the one that tests the
        # time limit declares a generous limit, so a slow start is not mistaken for the behaviour under test
        # (three full runs showed a random test timing out at 5 s before this).
        slow = 30000
        cls.root = build_tree(cls.base, {
            "probe-fs": manifest_for("probe-fs", {"filesystem": "read-root"}, timeout_ms=slow),
            "probe-none": manifest_for("probe-none", {"filesystem": "none"}, timeout_ms=slow),
            "probe-write": manifest_for("probe-write", {"filesystem": "write-root"}, timeout_ms=slow),
            "probe-mem": manifest_for("probe-mem", {"filesystem": "read-root", "memoryMb": 64}, timeout_ms=slow),
            "probe-time": manifest_for("probe-time", {"filesystem": "read-root"}, timeout_ms=1500),
            "probe-flood": manifest_for("probe-flood", {"filesystem": "read-root", "outputBytes": 1024}, timeout_ms=slow),
        })
        cls.registry = Registry(cls.root)
        cls.project = cls.base / "project"
        cls.project.mkdir()
        os.chmod(cls.project, 0o777)
        (cls.project / "inside.txt").write_text("inside the root\n", encoding="utf-8")
        os.chmod(cls.project / "inside.txt", 0o644)
        cls.outside = cls.base / "outside"
        cls.outside.mkdir()
        os.chmod(cls.outside, 0o755)
        cls.canary = cls.outside / "canary.txt"
        cls.canary.write_text("HOST CANARY\n", encoding="utf-8")
        os.chmod(cls.canary, 0o644)
        cls.sandbox = ContainerSandbox(IMAGE, allowed_mount_roots=(cls.base,), cleanup_wait_s=30.0)

    @classmethod
    def tearDownClass(cls):
        cls._tmp.cleanup()

    def go(self, name, data):
        return run_tool(self.registry, name, data, project_root=self.project, sandbox=self.sandbox)

    def leftover(self):
        done = subprocess.run(["docker", "ps", "-aq", "--filter", "name=taffee-sbx-"], capture_output=True, text=True, timeout=30)
        return done.stdout.split()

    def test_control_a_permitted_read_inside_the_root_succeeds(self):
        self.assertEqual(self.go("probe-fs", {"mode": "read", "path": f"{WORK}/inside.txt"}),
                         {"ok": True, "output": "read:inside the root"})

    def test_control_the_request_carries_the_container_root_not_the_host_path(self):
        self.assertEqual(self.go("probe-fs", {"mode": "root"}), {"ok": True, "output": WORK})

    def test_a_host_file_outside_the_root_is_not_visible(self):
        result = self.go("probe-fs", {"mode": "read", "path": str(self.canary)})
        self.assertEqual(result, {"ok": True, "output": "denied:FileNotFoundError"})
        self.assertNotIn("CANARY", json.dumps(result))

    def test_the_declared_read_only_root_refuses_a_write(self):
        result = self.go("probe-fs", {"mode": "write", "path": f"{WORK}/new.txt"})
        self.assertEqual(result, {"ok": True, "output": "denied:OSError"})
        self.assertFalse((self.project / "new.txt").exists())

    def test_filesystem_none_hides_the_root_entirely(self):
        result = self.go("probe-none", {"mode": "read", "path": f"{WORK}/inside.txt"})
        self.assertEqual(result, {"ok": True, "output": "denied:FileNotFoundError"})

    def test_write_root_when_declared_lets_the_tool_write_the_root_and_only_the_root(self):
        self.assertEqual(self.go("probe-write", {"mode": "write", "path": f"{WORK}/made.txt"}), {"ok": True, "output": "wrote"})
        self.assertEqual((self.project / "made.txt").read_text(), "x")
        self.assertEqual(self.go("probe-write", {"mode": "write", "path": "/etc/changed"}), {"ok": True, "output": "denied:OSError"})

    def test_a_network_connection_to_a_host_listener_is_refused(self):
        listener = socket.socket()
        listener.bind(("0.0.0.0", 0))
        listener.listen(5)
        port = listener.getsockname()[1]
        accepted = []
        threading.Thread(target=lambda: accepted.append(listener.accept()), daemon=True).start()
        try:
            result = self.go("probe-fs", {"mode": "connect", "host": host_ip(), "port": port})
        finally:
            listener.close()
        self.assertEqual(result["ok"], True)
        self.assertTrue(result["output"].startswith("denied:"), result)
        self.assertEqual(accepted, [], "the listener must have seen no connection")

    def test_control_the_same_tool_code_without_a_sandbox_reads_the_canary_and_connects(self):
        """The refusals above are the policy's doing: the same fixture, run by an explicit opt-out, succeeds."""
        listener = socket.socket()
        listener.bind(("0.0.0.0", 0))
        listener.listen(5)
        port = listener.getsockname()[1]
        threading.Thread(target=lambda: listener.accept(), daemon=True).start()
        entry = self.root / "library" / "probe-fs" / "python" / "main.py"
        try:
            for data, expected in (({"mode": "read", "path": str(self.canary)}, "read:HOST CANARY"),
                                   ({"mode": "connect", "host": host_ip(), "port": port}, "connected")):
                done = ProcessSandbox().run([sys.executable, str(entry)], root=self.project, policy=OPEN,
                                            stdin=json.dumps({"root": str(self.project), "input": data}).encode())
                self.assertEqual(json.loads(done.stdout)["output"], expected)
        finally:
            listener.close()

    def test_memory_over_the_declared_limit_ends_the_tool_with_a_closed_error(self):
        result = self.go("probe-mem", {"mode": "memory"})
        self.assertEqual(result, {"ok": False, "error": TOOL_ERRORS["exit"]})
        self.assertEqual(self.leftover(), [])

    def test_time_over_the_declared_limit_ends_the_tool_with_a_closed_error(self):
        result = self.go("probe-time", {"mode": "sleep"})
        self.assertEqual(result, {"ok": False, "error": TOOL_ERRORS["timeout"]})
        self.assertEqual(self.leftover(), [])

    def test_the_smallest_valid_result_line_is_24_bytes_and_the_schema_floor_is_above_it(self):
        """review a1edb12b: an outputBytes below the smallest valid result could never succeed. Measured through the sandbox
        directly (a manifest cannot declare these values any more)."""
        from capability.execute import _parse_result
        entry = self.root / "library" / "probe-fs" / "python" / "main.py"
        argv = ["python3", "/impl/main.py"]
        stdin = json.dumps({"root": WORK, "input": {"mode": "empty"}}).encode()
        outcomes = {}
        for limit in (22, 23, 24, 25):
            done = self.sandbox.run(argv, root=self.project, policy=SandboxPolicy(timeout_s=30, filesystem="read-root", output_bytes=limit),
                                    stdin=stdin, implementation=entry.parent)
            outcomes[limit] = _parse_result(done)
        self.assertEqual(outcomes[22], {"ok": False, "error": TOOL_ERRORS["output"]})
        self.assertEqual(outcomes[23], {"ok": False, "error": TOOL_ERRORS["output"]})  # the 23 characters without the newline
        self.assertEqual(outcomes[24], {"ok": True, "output": ""})
        self.assertEqual(outcomes[25], {"ok": True, "output": ""})
        self.assertLess(24, 64, "the schema floor (64) must be above the smallest valid result line (24 bytes)")

    def test_output_over_the_declared_limit_ends_the_tool_with_a_closed_error(self):
        result = self.go("probe-flood", {"mode": "flood"})
        self.assertEqual(result, {"ok": False, "error": TOOL_ERRORS["output"]})
        self.assertEqual(self.leftover(), [])


if __name__ == "__main__":
    unittest.main()
