"""S-011: sandbox interface (CAP-SBX-1 to CAP-SBX-12).

The container tests run real Docker. They need a Docker daemon and an image with `python3`:
    TAFFEE_SANDBOX_IMAGE=taffee-cap-base:dev
and, when the tests themselves run inside a container, a temporary directory that has the same
absolute path on the host:
    TAFFEE_SANDBOX_TMP=/path/visible/to/both
Without them each container test is skipped, with the reason.
"""

import os
import subprocess
import sys
import tempfile
import time
import unittest
import uuid
from pathlib import Path

from capability.sandbox import (
    LIMITS,
    NEVER_MOUNTED,
    ContainerSandbox,
    PolicyError,
    ProcessSandbox,
    RunResult,
    Sandbox,
    SandboxCleanupFailed,
    SandboxPolicy,
    SandboxUnavailable,
    docker_available,
    required,
)

OPEN = SandboxPolicy(filesystem="unrestricted", network="allowed")
IMAGE = os.environ.get("TAFFEE_SANDBOX_IMAGE")
SKIP_REASON = None if IMAGE is None and False else None
if not IMAGE:
    SKIP_REASON = "TAFFEE_SANDBOX_IMAGE is not set"
else:
    SKIP_REASON = docker_available()


class PolicyTest(unittest.TestCase):
    def test_defaults_are_the_closed_policy(self):
        policy = SandboxPolicy()
        self.assertEqual((policy.filesystem, policy.network, policy.env), ("none", "none", ()))
        self.assertEqual((policy.timeout_s, policy.memory_mb), (5.0, 256))

    def test_every_field_is_range_checked(self):
        bad = [dict(timeout_s=0), dict(timeout_s=601), dict(timeout_s=True), dict(timeout_s="5"),
               dict(memory_mb=15), dict(memory_mb=8193), dict(memory_mb=1.5), dict(memory_mb=True),
               dict(output_bytes=0), dict(output_bytes=1024 * 1024 + 1), dict(output_bytes=True),
               dict(stdin_bytes=-1), dict(stdin_bytes=1024 * 1024 + 1),
               dict(filesystem="read"), dict(filesystem=None), dict(network="outbound"), dict(network=True),
               dict(env=["A"]), dict(env=("lower",)), dict(env=("A B",)), dict(env=("A=1",)), dict(env=(5,)),
               dict(env=("X" * 65,))]
        for kwargs in bad:
            with self.subTest(kwargs=kwargs):
                with self.assertRaises(PolicyError):
                    SandboxPolicy(**kwargs)

    def test_good_edge_values_are_accepted(self):
        SandboxPolicy(timeout_s=0.1, memory_mb=16, output_bytes=1, stdin_bytes=0)
        SandboxPolicy(timeout_s=600, memory_mb=8192, output_bytes=1024 * 1024, stdin_bytes=1024 * 1024,
                      filesystem="write-root", network="allowed", env=("PATH", "_TOKEN_2"))

    def test_the_policy_is_immutable(self):
        with self.assertRaises(Exception):
            SandboxPolicy().memory_mb = 1


class RequiredTest(unittest.TestCase):
    def test_the_closed_policy_needs_every_limit(self):
        self.assertEqual(required(SandboxPolicy()), frozenset({"time", "memory", "output", "stdin", "env", "filesystem", "network"}))

    def test_unrestricted_and_allowed_are_the_only_ways_to_need_less(self):
        self.assertEqual(required(SandboxPolicy(filesystem="unrestricted")) & {"filesystem", "network"}, {"network"})
        self.assertEqual(required(SandboxPolicy(network="allowed")) & {"filesystem", "network"}, {"filesystem"})
        self.assertEqual(required(OPEN) & {"filesystem", "network"}, set())

    def test_scoped_filesystems_all_need_enforcement(self):
        for scope in ("none", "read-root", "write-root"):
            with self.subTest(scope=scope):
                self.assertIn("filesystem", required(SandboxPolicy(filesystem=scope, network="allowed")))

    def test_what_the_sandboxes_claim(self):
        self.assertEqual(ProcessSandbox.enforces, frozenset({"time", "memory", "output", "stdin", "env"}))
        self.assertEqual(ContainerSandbox.enforces, LIMITS)
        self.assertLess(ProcessSandbox.enforces, ContainerSandbox.enforces)


class NoSilentFallbackTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.marker = self.root / "RAN"
        self.argv = [sys.executable, "-c", f"open({str(self.marker)!r}, 'w').write('ran')"]

    def tearDown(self):
        self._tmp.cleanup()

    def test_the_process_sandbox_refuses_a_policy_that_needs_filesystem_or_network_and_runs_nothing(self):
        with self.assertRaises(SandboxUnavailable) as ctx:
            ProcessSandbox().run(self.argv, root=self.root, policy=SandboxPolicy())
        self.assertEqual(ctx.exception.unmet, ["filesystem", "network"])
        self.assertEqual(ctx.exception.code, "SandboxUnavailable")
        self.assertEqual(ctx.exception.sandbox, "process")
        self.assertFalse(self.marker.exists())

    def test_each_half_is_named_separately(self):
        for policy, unmet in ((SandboxPolicy(filesystem="unrestricted"), ["network"]),
                              (SandboxPolicy(network="allowed"), ["filesystem"]),
                              (SandboxPolicy(filesystem="read-root", network="allowed"), ["filesystem"]),
                              (SandboxPolicy(filesystem="write-root", network="allowed"), ["filesystem"])):
            with self.subTest(policy=policy):
                with self.assertRaises(SandboxUnavailable) as ctx:
                    ProcessSandbox().run(self.argv, root=self.root, policy=policy)
                self.assertEqual(ctx.exception.unmet, unmet)
        self.assertFalse(self.marker.exists())

    def test_an_explicit_unrestricted_policy_does_run(self):
        result = ProcessSandbox().run(self.argv, root=self.root, policy=OPEN)
        self.assertEqual(result.returncode, 0)
        self.assertTrue(self.marker.exists())

    def test_the_guard_runs_before_the_subclass_is_asked_to_start_anything(self):
        calls = []

        class Weak(Sandbox):
            name = "weak"
            enforces = frozenset({"time"})

            def _launch(self, *args):
                calls.append(args)

        with self.assertRaises(SandboxUnavailable) as ctx:
            Weak().run(["x"], root=self.root, policy=OPEN)
        self.assertEqual(ctx.exception.unmet, ["env", "memory", "output", "stdin"])
        self.assertEqual(calls, [])

    def test_a_sandbox_that_enforces_everything_is_asked_to_launch(self):
        calls = []

        class Strong(Sandbox):
            name = "strong"
            enforces = LIMITS

            def _launch(self, argv, root, policy, stdin, env_values, ro_mounts):
                calls.append((argv, env_values))
                return RunResult(0, b"", b"", False, False)

        Strong().run(["x"], root=self.root, policy=SandboxPolicy(env=("A",)), env_values={"A": "1", "B": "2"})
        self.assertEqual(calls, [(["x"], {"A": "1"})])

    def test_malformed_calls_are_refused_before_starting(self):
        for argv, stdin in (([], b""), ("python", b""), ([5], b""), (["x"], "text"), (["x"], b"x" * 70_000)):
            with self.subTest(argv=argv):
                with self.assertRaises(PolicyError):
                    ProcessSandbox().run(argv, root=self.root, policy=OPEN, stdin=stdin)
        with self.assertRaises(PolicyError):
            ProcessSandbox().run(self.argv, root=self.root, policy="not a policy")
        self.assertFalse(self.marker.exists())


class ProcessSandboxTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def run_py(self, code, policy=OPEN, **kwargs):
        return ProcessSandbox().run([sys.executable, "-c", code], root=self.root, policy=policy, **kwargs)

    def child_running(self, marker):
        for entry in Path("/proc").iterdir():
            if entry.name.isdigit():
                try:
                    cmdline = (entry / "cmdline").read_bytes()
                except OSError:
                    continue
                if marker.encode() in cmdline:
                    return True
        return False

    def test_a_program_runs_and_its_output_is_returned_as_plain_data(self):
        result = self.run_py("print('hi')")
        self.assertEqual((result.returncode, result.stdout, result.stderr, result.timed_out, result.truncated),
                         (0, b"hi\n", b"", False, False))
        self.assertNotIn(str(self.root), repr(result))

    def test_stdin_reaches_the_program(self):
        result = self.run_py("import sys; sys.stdout.write(sys.stdin.read().upper())", stdin=b"abc")
        self.assertEqual(result.stdout, b"ABC")

    def test_stdin_over_the_limit_is_refused_before_the_run(self):
        policy = SandboxPolicy(filesystem="unrestricted", network="allowed", stdin_bytes=10)
        with self.assertRaises(PolicyError):
            self.run_py("print(1)", policy=policy, stdin=b"x" * 11)
        self.assertEqual(self.run_py("print(1)", policy=policy, stdin=b"x" * 10).returncode, 0)

    def test_a_program_that_ignores_stdin_does_not_hang_the_run(self):
        result = self.run_py("print('no read')", stdin=b"x" * 60_000)
        self.assertEqual(result.stdout, b"no read\n")

    def test_the_timeout_kills_the_whole_process_group_and_nothing_is_left(self):
        marker = f"31.{os.getpid()}"
        code = ("import subprocess, time\n"
                f"subprocess.Popen(['sleep', {marker!r}])\n"
                "time.sleep(60)\n")
        started = time.monotonic()
        result = self.run_py(code, policy=SandboxPolicy(filesystem="unrestricted", network="allowed", timeout_s=0.7))
        self.assertLess(time.monotonic() - started, 5)
        self.assertTrue(result.timed_out)
        self.assertNotEqual(result.returncode, 0)
        time.sleep(0.3)
        self.assertFalse(self.child_running(marker))

    def test_an_output_overrun_kills_the_run_and_is_flagged(self):
        policy = SandboxPolicy(filesystem="unrestricted", network="allowed", output_bytes=1000, timeout_s=10)
        started = time.monotonic()
        result = self.run_py("import sys\nwhile True: sys.stdout.write('x' * 4096); sys.stdout.flush()", policy=policy)
        self.assertLess(time.monotonic() - started, 5)
        self.assertTrue(result.truncated)
        self.assertLessEqual(len(result.stdout), 1000)
        self.assertFalse(result.timed_out)
        self.assertNotEqual(result.returncode, 0)

    def test_stderr_is_bounded_too(self):
        policy = SandboxPolicy(filesystem="unrestricted", network="allowed", output_bytes=500, timeout_s=10)
        result = self.run_py("import sys\nwhile True: sys.stderr.write('e' * 4096); sys.stderr.flush()", policy=policy)
        self.assertTrue(result.truncated)
        self.assertLessEqual(len(result.stderr), 500)

    def test_the_memory_limit_stops_a_large_allocation(self):
        policy = SandboxPolicy(filesystem="unrestricted", network="allowed", memory_mb=64, timeout_s=10)
        result = self.run_py("x = bytearray(600 * 1024 * 1024)\nprint('allocated')", policy=policy)
        self.assertNotEqual(result.returncode, 0)
        self.assertNotIn(b"allocated", result.stdout)
        self.assertFalse(result.timed_out)

    def test_a_program_inside_the_memory_limit_runs(self):
        policy = SandboxPolicy(filesystem="unrestricted", network="allowed", memory_mb=512)
        self.assertEqual(self.run_py("x = bytearray(20 * 1024 * 1024); print('ok')", policy=policy).stdout, b"ok\n")

    def test_the_environment_is_exactly_the_allow_list(self):
        policy = SandboxPolicy(filesystem="unrestricted", network="allowed", env=("ALLOWED", "ABSENT"))
        os.environ["HOST_SECRET"] = "leak"
        try:
            result = self.run_py("import os; print(sorted(os.environ))", policy=policy,
                                 env_values={"ALLOWED": "1", "OTHER": "2"})
        finally:
            del os.environ["HOST_SECRET"]
        names = eval(result.stdout.decode())
        self.assertEqual(set(names) - {"LC_CTYPE"}, {"ALLOWED"})  # LC_CTYPE is Python's own locale coercion
        self.assertNotIn("HOST_SECRET", names)

    def test_nothing_is_inherited_when_the_allow_list_is_empty(self):
        os.environ["HOST_SECRET"] = "leak"
        try:
            result = self.run_py("import os; print(sorted(os.environ))")
        finally:
            del os.environ["HOST_SECRET"]
        self.assertEqual(set(eval(result.stdout.decode())) - {"LC_CTYPE"}, set())

    def test_the_working_directory_is_the_root(self):
        self.assertEqual(self.run_py("import os; print(os.getcwd())").stdout.decode().strip(), str(self.root.resolve()))

    def test_mounts_are_not_supported_and_are_refused(self):
        with self.assertRaises(PolicyError):
            self.run_py("print(1)", implementation=self.root)

    def test_a_failing_program_reports_its_return_code(self):
        self.assertEqual(self.run_py("import sys; sys.exit(7)").returncode, 7)

    def test_a_program_that_cannot_be_started_is_an_error_not_a_hang(self):
        with self.assertRaises(OSError):
            ProcessSandbox().run(["/no/such/program"], root=self.root, policy=OPEN)


class ContainerArgvTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name).resolve()
        self.impl = self.root / "impl"
        self.impl.mkdir()
        self.sandbox = ContainerSandbox("python:3.12-slim", allowed_mount_roots=(self.root,))

    def tearDown(self):
        self._tmp.cleanup()

    def argv(self, policy=None, implementation=None, env_file=None, command=("python3", "main.py"), sandbox=None):
        return (sandbox or self.sandbox).build_argv(list(command), name="taffee-sbx-test", root=self.root,
                                                    policy=policy or SandboxPolicy(), env_file=env_file,
                                                    implementation=implementation)

    def pairs(self, cmd, flag):
        return [cmd[i + 1] for i, item in enumerate(cmd) if item == flag and i + 1 < len(cmd)]

    def test_the_hardening_flags_are_always_present(self):
        for policy in (SandboxPolicy(), SandboxPolicy(filesystem="read-root"), SandboxPolicy(filesystem="write-root", network="allowed"),
                       OPEN):
            with self.subTest(policy=policy):
                cmd = self.argv(policy)
                self.assertEqual(cmd[:3], ["docker", "run", "--rm"])
                self.assertIn("--read-only", cmd)
                self.assertEqual(self.pairs(cmd, "--cap-drop"), ["ALL"])
                self.assertEqual(self.pairs(cmd, "--security-opt"), ["no-new-privileges"])
                self.assertEqual(self.pairs(cmd, "--user"), ["65534:65534"])
                self.assertEqual(self.pairs(cmd, "--pids-limit"), ["64"])
                self.assertEqual(self.pairs(cmd, "--ulimit"), ["core=0"])
                self.assertEqual(self.pairs(cmd, "--name"), ["taffee-sbx-test"])
                self.assertTrue(self.pairs(cmd, "--tmpfs")[0].startswith("/tmp:rw,noexec,nosuid,nodev,size="))

    def test_the_time_limit_is_counted_inside_the_container_from_the_program_start(self):
        cmd = self.argv(SandboxPolicy(timeout_s=2.5))
        image = cmd.index("python:3.12-slim")
        self.assertEqual(cmd[image + 1:], ["timeout", "-k", "2", "2.5", "python3", "main.py"])

    def test_containers_carry_the_owner_label_only_when_an_owner_is_set(self):
        self.assertNotIn("--label", self.argv())
        owned = ContainerSandbox("python:3.12-slim", allowed_mount_roots=(self.root,), owner="registry-abc")
        self.assertEqual(self.pairs(self.argv(sandbox=owned), "--label"), ["org.taffee.sandbox.owner=registry-abc"])
        for bad in ("", "a b", "x" * 65, "a=b", 7):
            with self.subTest(bad=bad), self.assertRaises(PolicyError):
                ContainerSandbox("python:3.12-slim", owner=bad)

    def test_the_start_up_allowance_is_bounded(self):
        for bad in (-1, 601, True, "60"):
            with self.subTest(bad=bad), self.assertRaises(PolicyError):
                ContainerSandbox("python:3.12-slim", startup_allowance_s=bad)

    def test_memory_has_no_swap(self):
        cmd = self.argv(SandboxPolicy(memory_mb=128))
        self.assertEqual(self.pairs(cmd, "--memory"), ["128m"])
        self.assertEqual(self.pairs(cmd, "--memory-swap"), ["128m"])

    def test_network_none_is_a_flag_and_allowed_is_its_absence(self):
        self.assertEqual(self.pairs(self.argv(SandboxPolicy(network="none")), "--network"), ["none"])
        self.assertNotIn("--network", self.argv(SandboxPolicy(network="allowed")))

    def test_each_filesystem_scope_maps_to_the_right_mount(self):
        none = self.argv(SandboxPolicy(filesystem="none"))
        self.assertEqual(self.pairs(none, "--mount"), [])
        self.assertEqual(self.pairs(none, "-w"), ["/tmp"])
        ro = self.argv(SandboxPolicy(filesystem="read-root"))
        self.assertEqual(self.pairs(ro, "--mount"), [f"type=bind,src={self.root},dst=/work,readonly"])
        self.assertEqual(self.pairs(ro, "-w"), ["/work"])
        rw = self.argv(SandboxPolicy(filesystem="write-root"))
        self.assertEqual(self.pairs(rw, "--mount"), [f"type=bind,src={self.root},dst=/work"])

    def test_the_implementation_is_the_only_other_mount_and_is_read_only_at_a_fixed_target(self):
        cmd = self.argv(SandboxPolicy(filesystem="read-root"), implementation=self.impl)
        self.assertEqual(self.pairs(cmd, "--mount"), [f"type=bind,src={self.root},dst=/work,readonly",
                                                      f"type=bind,src={self.impl},dst=/impl,readonly"])

    def test_the_docker_socket_and_privileged_flags_never_appear(self):
        for policy in (SandboxPolicy(), OPEN, SandboxPolicy(filesystem="write-root", network="allowed")):
            cmd = self.argv(policy, implementation=self.impl)
            self.assertNotIn("docker.sock", " ".join(cmd))
            for forbidden in ("--privileged", "--pid", "--ipc", "--uts", "--cap-add", "--device", "--volumes-from", "-v", "--volume",
                              "--security-opt=seccomp=unconfined", "--userns"):
                with self.subTest(policy=policy, forbidden=forbidden):
                    self.assertNotIn(forbidden, cmd)  # whole arguments, so --pids-limit is not mistaken for --pid
            self.assertNotIn("--net=host", cmd)
            self.assertNotEqual(self.pairs(cmd, "--network"), ["host"])

    def test_environment_reaches_the_container_through_an_env_file_never_through_argv(self):
        cmd = self.argv(env_file="/private/dir/env")
        self.assertEqual(self.pairs(cmd, "--env-file"), ["/private/dir/env"])
        self.assertNotIn("-e", cmd)
        self.assertNotIn("--env", cmd)
        self.assertNotIn("--env-file", self.argv())  # no variables, no file

    def test_the_image_and_the_program_come_last_and_the_program_is_not_shell_parsed(self):
        cmd = self.argv(command=("python3", "-c", "print('a; b && c')"))
        self.assertEqual(cmd[-8:], ["python:3.12-slim", "timeout", "-k", "2", "5", "python3", "-c", "print('a; b && c')"])

    def refused_mount(self, **kwargs):
        with self.assertRaises(PolicyError) as ctx:
            self.argv(SandboxPolicy(filesystem="read-root"), **kwargs)
        return str(ctx.exception)

    def test_an_arbitrary_host_path_is_refused_as_the_implementation(self):
        outside = self.root.parent / (self.root.name + "-outside")
        outside.mkdir()
        try:
            for path in (Path("/"), Path("/etc"), Path("/proc"), Path("/sys"), Path("/dev"), Path("/run"), Path("/var/run"),
                         Path("/root"), Path("/var/run/docker.sock"), Path("/run/docker.sock"), Path.home(), outside,
                         Path("/tmp"), self.root.parent, self.root / ".." / ".."):
                with self.subTest(path=str(path)):
                    self.refused_mount(implementation=path)
        finally:
            outside.rmdir()

    def test_the_docker_socket_is_refused_even_if_it_is_named_as_the_root_or_an_allowed_root(self):
        for sock in ("/var/run/docker.sock", "/run/docker.sock"):
            with self.subTest(sock=sock):
                self.assertIn("docker.sock", " ".join([sock]))
                with self.assertRaises(PolicyError):
                    self.sandbox.build_argv(["x"], name="n", root=Path(sock), policy=SandboxPolicy(filesystem="read-root"),
                                            )
                lax = ContainerSandbox("python:3.12-slim", allowed_mount_roots=(Path("/"), Path("/var/run"), Path("/run")))
                with self.assertRaises(PolicyError):
                    lax.build_argv(["x"], name="n", root=self.root, policy=SandboxPolicy(filesystem="read-root"),
                                   implementation=Path(sock))

    def test_system_trees_are_refused_even_when_they_are_configured_as_allowed(self):
        lax = ContainerSandbox("python:3.12-slim", allowed_mount_roots=(Path("/"),))
        for path in NEVER_MOUNTED + (Path("/"),):
            with self.subTest(path=str(path)):
                if path.exists():
                    with self.assertRaises(PolicyError) as ctx:
                        self.argv(SandboxPolicy(filesystem="read-root"), implementation=path, sandbox=lax)
                    # /var/run is itself a link to /run on most hosts, so it may be refused for that reason first
                    message = str(ctx.exception)
                    self.assertTrue("never mounted" in message or "passes through a link" in message, message)

    def test_a_folder_outside_the_allowed_roots_is_refused_for_the_root_too(self):
        other = ContainerSandbox("python:3.12-slim", allowed_mount_roots=(self.root / "impl",))
        with self.assertRaises(PolicyError) as ctx:
            self.argv(SandboxPolicy(filesystem="read-root"), sandbox=other)
        self.assertIn("is not under an allowed mount root", str(ctx.exception))

    def test_with_no_allowed_roots_nothing_can_be_mounted_but_a_filesystem_none_policy_still_builds(self):
        bare = ContainerSandbox("python:3.12-slim")
        self.assertEqual(self.pairs(self.argv(SandboxPolicy(filesystem="none"), sandbox=bare), "--mount"), [])
        for kwargs in ({"policy": SandboxPolicy(filesystem="read-root")},
                       {"policy": SandboxPolicy(filesystem="none"), "implementation": self.impl}):
            with self.subTest(kwargs=str(kwargs)[:50]):
                with self.assertRaises(PolicyError) as ctx:
                    self.argv(sandbox=bare, **kwargs)
                self.assertIn("no allowed mount roots", str(ctx.exception))

    def test_a_link_to_a_folder_outside_is_refused_whether_it_is_the_last_component_or_a_parent(self):
        outside = Path(self._tmp.name).resolve().parent / (Path(self._tmp.name).name + "-target")
        outside.mkdir()
        try:
            (self.root / "link").symlink_to(outside)
            (outside / "inner").mkdir()
            for path in (self.root / "link", self.root / "link" / "inner"):
                with self.subTest(path=str(path)):
                    self.assertIn("passes through a link", self.refused_mount(implementation=path))
        finally:
            (self.root / "link").unlink()
            (outside / "inner").rmdir()
            outside.rmdir()

    def test_a_relative_missing_file_or_oddly_named_implementation_is_refused(self):
        (self.root / "file.txt").write_text("x", encoding="utf-8")
        weird = self.root / "a,b"
        weird.mkdir()
        for path in (Path("impl"), self.root / "missing", self.root / "file.txt", weird):
            with self.subTest(path=str(path)):
                self.refused_mount(implementation=path)

    def test_the_caller_cannot_choose_a_mount_target_or_name_extra_mounts(self):
        import inspect
        parameters = inspect.signature(Sandbox.run).parameters
        self.assertNotIn("ro_mounts", parameters)
        self.assertNotIn("mounts", parameters)
        with self.assertRaises(TypeError):
            Sandbox.run(ProcessSandbox(), ["x"], root=self.root, policy=SandboxPolicy(), ro_mounts={"/x": "/y"})
        cmd = self.argv(SandboxPolicy(filesystem="none"), implementation=self.impl)
        self.assertEqual(self.pairs(cmd, "--mount"), [f"type=bind,src={self.impl},dst=/impl,readonly"])

    def test_a_bad_image_is_refused(self):
        for image in ("", "has space", None, 5):
            with self.subTest(image=image):
                with self.assertRaises(PolicyError):
                    ContainerSandbox(image)

    def test_an_option_like_image_is_refused_so_it_cannot_change_the_docker_flags(self):
        """review 8c8ae68f: the image sits among the options of `docker run`, so a leading dash would be an option."""
        for image in ("--privileged", "--network=host", "-v/:/host", "-e", "-", "--", "--pid=host", "-it",
                      "img\nmore", "img;rm", "img$(x)", "a" * 300):
            with self.subTest(image=image):
                with self.assertRaises(PolicyError):
                    ContainerSandbox(image)

    def test_ordinary_image_references_are_accepted(self):
        for image in ("python:3.12-slim", "taffee-cap-base:dev", "registry.example:5000/team/img:1.2",
                      "img@sha256:" + "a" * 64, "ubuntu"):
            with self.subTest(image=image):
                self.assertEqual(self.pairs(self.argv(sandbox=ContainerSandbox(image))[:-2], "--user"), ["65534:65534"])

    def test_the_container_sandbox_enforces_every_limit_so_it_never_refuses_a_policy(self):
        self.assertEqual(required(SandboxPolicy()) - ContainerSandbox.enforces, frozenset())


class CleanupFailureTest(unittest.TestCase):
    """review 8f1aee5e: run returns only once the container is confirmed gone, or it raises. No Docker needed:
    a fake `docker` script stands in, and can be made to keep listing the container forever."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self._tmp.name).resolve()
        self.log = self.dir / "calls.log"
        self.gone = self.dir / "gone"
        self.listing_fails = self.dir / "listing-fails"
        self.script = self.dir / "fake-docker"
        self.script.write_text(
            "#!/bin/sh\n"
            f'echo "$@" >> {self.log}\n'
            'case "$1" in\n'
            "  run) echo ok; exit 0 ;;\n"
            "  kill) exit 0 ;;\n"
            "  rm) exit 0 ;;\n"
            f"  ps) if [ -f {self.listing_fails} ]; then exit 1; fi; if [ -f {self.gone} ]; then exit 0; fi; echo deadbeef0000; exit 0 ;;\n"
            "esac\n", encoding="utf-8")
        self.script.chmod(0o755)

    def tearDown(self):
        self._tmp.cleanup()

    def sandbox(self, wait=0.6):
        return ContainerSandbox("python:3.12-slim", docker=str(self.script), cleanup_wait_s=wait)

    def run_it(self, wait=0.6):
        return self.sandbox(wait).run(["true"], root=self.dir, policy=SandboxPolicy(timeout_s=10))

    def test_when_the_container_is_confirmed_gone_the_result_is_returned(self):
        self.gone.write_text("", encoding="utf-8")
        result = self.run_it()
        self.assertEqual((result.returncode, result.stdout, result.timed_out), (0, b"ok\n", False))

    def test_a_container_that_stays_listed_raises_and_no_result_is_returned(self):
        import time
        started = time.monotonic()
        with self.assertRaises(SandboxCleanupFailed) as ctx:
            self.run_it(wait=0.6)
        self.assertLess(time.monotonic() - started, 10)
        self.assertEqual(ctx.exception.code, "SandboxCleanupFailed")
        self.assertRegex(ctx.exception.container, r"^taffee-sbx-[0-9a-f]{16}$")
        self.assertIn(ctx.exception.container, str(ctx.exception))
        self.assertIn("may still be running", str(ctx.exception))

    def test_removal_was_attempted_repeatedly_before_giving_up(self):
        with self.assertRaises(SandboxCleanupFailed):
            self.run_it(wait=0.6)
        calls = self.log.read_text().splitlines()
        self.assertGreater(len([c for c in calls if c.startswith("rm -f taffee-sbx-")]), 1)
        self.assertGreater(len([c for c in calls if c.startswith("ps -aq")]), 1)

    def test_a_listing_that_itself_fails_is_not_a_confirmation(self):
        self.listing_fails.write_text("", encoding="utf-8")
        with self.assertRaises(SandboxCleanupFailed):
            self.run_it(wait=0.5)

    def test_a_container_that_disappears_during_the_wait_is_a_success(self):
        import threading
        timer = threading.Timer(0.4, lambda: self.gone.write_text("", encoding="utf-8"))
        timer.start()
        try:
            self.assertEqual(self.run_it(wait=5.0).stdout, b"ok\n")
        finally:
            timer.cancel()

    def test_a_failure_while_running_still_reports_an_unconfirmed_cleanup(self):
        self.script.write_text("#!/bin/sh\ncase \"$1\" in run) exit 3 ;; ps) echo deadbeef0000 ;; esac\nexit 0\n", encoding="utf-8")
        with self.assertRaises(SandboxCleanupFailed):
            self.run_it(wait=0.5)

    def test_the_error_is_a_sandbox_error(self):
        from capability.sandbox import SandboxError
        self.assertTrue(issubclass(SandboxCleanupFailed, SandboxError))


class ControlPlaneIsolationTest(unittest.TestCase):
    """review 36d0e6f6: a caller-chosen variable must never reach the docker client's own environment, so
    DOCKER_HOST, PATH or LD_PRELOAD in policy.env cannot redirect or hijack the Docker control plane. A fake
    `docker` records its own environment and the env file it was given. No Docker needed."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self._tmp.name).resolve()
        self.dump = self.dir / "client-env"
        self.file_copy = self.dir / "env-file-copy"
        self.file_path = self.dir / "env-file-path"
        self.script = self.dir / "fake-docker"
        self.script.write_text(
            "#!/bin/sh\n"
            'case "$1" in\n'
            "  run)\n"
            f"    env > {self.dump}\n"
            '    while [ $# -gt 0 ]; do if [ "$1" = "--env-file" ]; then '
            f'echo "$2" > {self.file_path}; cat "$2" > {self.file_copy}; fi; shift; done\n'
            "    echo ok; exit 0 ;;\n"
            "  kill|rm|ps) exit 0 ;;\n"
            "esac\n", encoding="utf-8")
        self.script.chmod(0o755)

    def tearDown(self):
        self._tmp.cleanup()

    def run_it(self, env, values):
        from unittest import mock
        sandbox = ContainerSandbox("python:3.12-slim", docker=str(self.script), cleanup_wait_s=1.0)
        host = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "DOCKER_HOST": "unix:///host/operator.sock"}
        with mock.patch.dict(os.environ, host):
            return sandbox.run(["true"], root=self.dir, policy=SandboxPolicy(timeout_s=10, env=env), env_values=values), host

    def client_env(self):
        return dict(line.split("=", 1) for line in self.dump.read_text().splitlines() if "=" in line)

    def test_docker_host_and_path_overrides_do_not_reach_the_docker_client(self):
        _, host = self.run_it(("DOCKER_HOST", "PATH", "TOKEN"),
                              {"DOCKER_HOST": "tcp://evil.example:2375", "PATH": "/tmp/evil", "TOKEN": "t"})
        client = self.client_env()
        self.assertEqual(client["DOCKER_HOST"], host["DOCKER_HOST"])  # the operator's value, not the caller's
        self.assertEqual(client["PATH"], host["PATH"])
        self.assertNotIn("TOKEN", client)

    def test_other_client_hijacking_names_do_not_reach_the_docker_client_either(self):
        self.run_it(("LD_PRELOAD", "DOCKER_CONFIG", "HOME"),
                    {"LD_PRELOAD": "/tmp/evil.so", "DOCKER_CONFIG": "/tmp/evil", "HOME": "/tmp/evil"})
        client = self.client_env()
        for name in ("LD_PRELOAD", "DOCKER_CONFIG"):
            self.assertNotIn(name, client)
        self.assertNotEqual(client.get("HOME"), "/tmp/evil")

    def test_the_other_docker_client_variables_do_not_reach_the_docker_client(self):
        """clarification 921c2d92: context, config, TLS and certificate variables also change the endpoint."""
        names = ("DOCKER_CONTEXT", "DOCKER_CONFIG", "DOCKER_TLS_VERIFY", "DOCKER_CERT_PATH", "DOCKER_API_VERSION")
        self.run_it(names, {name: "attacker" for name in names})
        client = self.client_env()
        for name in names:
            with self.subTest(name=name):
                self.assertNotIn(name, client)

    def test_the_program_still_receives_its_variables_through_the_env_file(self):
        self.run_it(("DOCKER_HOST", "TOKEN"), {"DOCKER_HOST": "tcp://evil.example:2375", "TOKEN": "t"})
        self.assertEqual(self.file_copy.read_text().splitlines(), ["DOCKER_HOST=tcp://evil.example:2375", "TOKEN=t"])

    def test_the_env_file_is_private_and_removed_after_the_run(self):
        self.run_it(("TOKEN",), {"TOKEN": "secret"})
        path = Path(self.file_path.read_text().strip())
        self.assertFalse(path.exists())
        self.assertFalse(path.parent.exists())

    def test_the_env_file_is_mode_0600_while_it_exists(self):
        self.script.write_text(self.script.read_text().replace(
            "echo ok; exit 0 ;;", f'stat -c %a "$(cat {self.file_path})" > {self.dir / "mode"} 2>/dev/null || stat -f %Lp "$(cat {self.file_path})" > {self.dir / "mode"}; echo ok; exit 0 ;;'), encoding="utf-8")
        self.run_it(("TOKEN",), {"TOKEN": "x"})
        self.assertEqual((self.dir / "mode").read_text().strip(), "600")

    def test_a_value_with_a_newline_or_nul_is_refused_before_docker_is_called(self):
        for bad in ("a\nINJECTED=1", "a\rb", "a\0b"):
            with self.subTest(value=repr(bad)):
                self.dump.unlink(missing_ok=True)
                with self.assertRaises(PolicyError):
                    self.run_it(("TOKEN",), {"TOKEN": bad})
                self.assertFalse(self.dump.exists())  # docker never ran

    def test_with_no_variables_there_is_no_env_file(self):
        self.run_it((), {})
        self.assertFalse(self.file_path.exists())


@unittest.skipIf(SKIP_REASON is not None, f"container tests need Docker: {SKIP_REASON}")
class ContainerRunTest(unittest.TestCase):
    """Real Docker. These are the tests that show the limits are enforced."""

    def setUp(self):
        base = os.environ.get("TAFFEE_SANDBOX_TMP")
        self._tmp = tempfile.TemporaryDirectory(dir=base)
        self.root = Path(self._tmp.name).resolve()
        os.chmod(self.root, 0o777)  # the container runs as nobody; the mount, not the permissions, must be what refuses
        (self.root / "a.txt").write_text("from the host\n", encoding="utf-8")
        os.chmod(self.root / "a.txt", 0o644)
        self.sandbox = ContainerSandbox(IMAGE, allowed_mount_roots=(self.root,))

    def tearDown(self):
        self._tmp.cleanup()

    def run_py(self, code, policy=None, sandbox=None, **kwargs):
        return (sandbox or self.sandbox).run(["python3", "-c", code], root=self.root,
                                             policy=policy or SandboxPolicy(timeout_s=30), **kwargs)

    def policy(self, **kwargs):
        kwargs.setdefault("timeout_s", 30)
        return SandboxPolicy(**kwargs)

    def leftover(self):
        done = subprocess.run(["docker", "ps", "-aq", "--filter", "name=taffee-sbx-"], capture_output=True, text=True, timeout=30)
        return done.stdout.split()

    def test_a_program_runs_and_its_output_comes_back(self):
        result = self.run_py("print('ok')")
        self.assertEqual((result.returncode, result.stdout, result.timed_out, result.truncated), (0, b"ok\n", False, False))

    def test_read_root_lets_the_program_read_the_root(self):
        result = self.run_py("print(open('/work/a.txt').read().strip())", self.policy(filesystem="read-root"))
        self.assertEqual(result.stdout, b"from the host\n")

    def test_read_root_refuses_a_write_to_the_root(self):
        result = self.run_py("open('/work/new.txt', 'w').write('x')", self.policy(filesystem="read-root"))
        self.assertNotEqual(result.returncode, 0)
        self.assertIn(b"Read-only file system", result.stderr)
        self.assertFalse((self.root / "new.txt").exists())

    def test_write_root_lets_the_program_write_the_root(self):
        result = self.run_py("open('/work/new.txt', 'w').write('written')", self.policy(filesystem="write-root"))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual((self.root / "new.txt").read_text(), "written")

    def test_filesystem_none_hides_the_root(self):
        result = self.run_py("import os; print(os.path.exists('/work'), os.path.exists('/work/a.txt'), os.getcwd())")
        self.assertEqual(result.stdout.decode().strip(), "False False /tmp")

    def test_the_container_filesystem_is_read_only_but_tmp_is_a_private_scratch_area(self):
        result = self.run_py("open('/tmp/scratch', 'w').write('ok'); print('tmp ok')\nopen('/etc/changed', 'w').write('x')")
        self.assertIn(b"tmp ok", result.stdout)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn(b"Read-only file system", result.stderr)

    def test_network_none_leaves_only_loopback_and_allowed_leaves_more(self):
        # The interfaces that are up (IFF_UP, flag 0x1). Some kernels (Docker Desktop's) create down, address-less
        # tunnel placeholders (gre0, sit0, tunl0, ...) in every network namespace, so listing names is not enough.
        code = ("import os\nup = []\nfor n in sorted(os.listdir('/sys/class/net')):\n"
                "    try:\n        flags = int(open(f'/sys/class/net/{n}/flags').read(), 16)\n"
                "    except OSError:\n        continue\n"
                "    if flags & 0x1:\n        up.append(n)\nprint(up)\n")
        self.assertEqual(self.run_py(code, self.policy(network="none")).stdout.decode().strip(), "['lo']")
        allowed = self.run_py(code, self.policy(network="allowed")).stdout.decode()
        self.assertIn("eth0", allowed)

    def test_network_none_refuses_an_outbound_connection(self):
        code = ("import socket\ns = socket.socket()\ns.settimeout(5)\n"
                "try:\n    s.connect(('1.1.1.1', 53)); print('connected')\n"
                "except OSError as e:\n    print('refused', e.errno)\n")
        self.assertIn(b"refused", self.run_py(code, self.policy(network="none")).stdout)

    def test_the_memory_limit_kills_a_large_allocation(self):
        policy = self.policy(memory_mb=64)
        result = self.run_py("x = bytearray(400 * 1024 * 1024)\nfor i in range(0, len(x), 4096): x[i] = 1\nprint('allocated')", policy)
        self.assertEqual(result.returncode, 137)
        self.assertNotIn(b"allocated", result.stdout)
        self.assertFalse(result.timed_out)

    def test_a_fork_bomb_is_contained_by_the_process_limit(self):
        code = ("import os, time\ncount = 0\n"
                "for i in range(300):\n    try:\n        pid = os.fork()\n    except OSError:\n        break\n"
                "    if pid == 0:\n        time.sleep(3)\n        os._exit(0)\n    count += 1\nprint('forked', count)\n")
        result = self.run_py(code)
        forked = int(result.stdout.decode().split()[-1])
        self.assertLess(forked, 100)
        self.assertGreater(forked, 0)

    def test_a_timeout_kills_the_container_and_none_is_left(self):
        started = time.monotonic()
        result = self.run_py("import time; time.sleep(120)", self.policy(timeout_s=1.5))
        self.assertLess(time.monotonic() - started, 30)
        self.assertTrue(result.timed_out)
        self.assertEqual(self.leftover(), [])

    def test_remove_owned_clears_a_stranded_container_and_only_its_own(self):
        """A process killed while Docker creates a container leaves it in Created; its next start removes it."""
        owner = "test-" + uuid.uuid4().hex[:12]
        other = "test-" + uuid.uuid4().hex[:12]
        made = []
        try:
            for label in (owner, owner, other):
                done = subprocess.run(["docker", "create", "--label", f"org.taffee.sandbox.owner={label}", IMAGE, "true"],
                                      capture_output=True, text=True, timeout=60, check=True)
                made.append(done.stdout.strip())
            sandbox = ContainerSandbox(IMAGE, allowed_mount_roots=(self.root,), owner=owner)
            self.assertEqual(sandbox.remove_owned(), 2)
            self.assertEqual(sandbox.remove_owned(), 0)

            def listed(label):
                return subprocess.run(["docker", "ps", "-aq", "--filter", f"label=org.taffee.sandbox.owner={label}"],
                                      capture_output=True, text=True, timeout=30).stdout.split()

            self.assertEqual(listed(owner), [])
            self.assertEqual(len(listed(other)), 1, "another owner's container is never touched")
        finally:
            subprocess.run(["docker", "rm", "-f", *made], capture_output=True, timeout=60)

    def test_a_program_that_ignores_sigterm_is_still_killed_at_its_limit(self):
        started = time.monotonic()
        result = self.run_py("import signal, time\nsignal.signal(signal.SIGTERM, signal.SIG_IGN)\ntime.sleep(120)",
                             self.policy(timeout_s=1))
        self.assertLess(time.monotonic() - started, 30)
        self.assertIn(result.returncode, (124, 137))
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(self.leftover(), [])

    def test_a_deadline_shorter_than_container_start_up_still_leaves_no_container(self):
        """The timeout fires while Docker is still creating the container: kill must not strand it. With no
        start-up allowance, the outer timer fires during creation, which is the race this test is for."""
        policy = self.policy(timeout_s=0.1)
        hurried = ContainerSandbox(self.sandbox._image, allowed_mount_roots=self.sandbox._allowed, startup_allowance_s=0)
        for attempt in range(8):
            with self.subTest(attempt=attempt):
                result = self.run_py("import time; time.sleep(60)", policy, sandbox=hurried)
                self.assertTrue(result.timed_out)
                self.assertEqual(self.leftover(), [])

    def test_output_is_bounded_and_the_container_is_gone(self):
        policy = self.policy(output_bytes=2000, timeout_s=30)
        result = self.run_py("import sys\nwhile True: sys.stdout.write('x' * 8192); sys.stdout.flush()", policy)
        self.assertTrue(result.truncated)
        self.assertLessEqual(len(result.stdout), 2000)
        self.assertEqual(self.leftover(), [])

    def test_the_program_is_not_root_and_holds_no_capabilities(self):
        code = ("import os\nprint(os.getuid(), os.getgid())\n"
                "print([l for l in open('/proc/self/status') if l.startswith('CapEff')][0].split()[1])\n"
                "print([l for l in open('/proc/self/status') if l.startswith('NoNewPrivs')][0].split()[1])\n")
        uid_gid, caps, no_new = self.run_py(code).stdout.decode().split("\n")[:3]
        self.assertEqual(uid_gid, "65534 65534")
        self.assertEqual(int(caps, 16), 0)
        self.assertEqual(no_new, "1")

    def test_the_docker_socket_is_not_there(self):
        self.assertEqual(self.run_py("import os; print(os.path.exists('/var/run/docker.sock'))").stdout.strip(), b"False")

    def test_only_the_allow_listed_environment_reaches_the_program(self):
        os.environ["HOST_SECRET"] = "leak"
        try:
            result = self.run_py("import os; print(sorted(k for k in os.environ if k in ('TOKEN', 'HOST_SECRET', 'OTHER')))",
                                 self.policy(env=("TOKEN",)), env_values={"TOKEN": "t", "OTHER": "o"})
        finally:
            del os.environ["HOST_SECRET"]
        self.assertEqual(result.stdout.decode().strip(), "['TOKEN']")

    def test_a_read_only_mount_exposes_an_implementation_folder_that_cannot_be_changed(self):
        impl = self.root / "impl"
        impl.mkdir()
        os.chmod(impl, 0o777)
        (impl / "main.py").write_text("print('implementation ran')\n", encoding="utf-8")
        os.chmod(impl / "main.py", 0o666)
        result = self.sandbox.run(["python3", "/impl/main.py"], root=self.root, policy=self.policy(),
                                  implementation=impl)
        self.assertEqual(result.stdout, b"implementation ran\n")
        tamper = self.run_py("open('/impl/main.py', 'w').write('changed')", implementation=impl)
        self.assertNotEqual(tamper.returncode, 0)
        self.assertIn(b"Read-only file system", tamper.stderr)
        self.assertEqual((impl / "main.py").read_text(), "print('implementation ran')\n")

    def test_stdin_reaches_the_program(self):
        result = self.run_py("import sys; sys.stdout.write(sys.stdin.read().upper())", stdin=b"abc")
        self.assertEqual(result.stdout, b"ABC")

    def test_a_secret_host_path_or_the_docker_socket_is_refused_before_any_container_starts(self):
        for path in ("/var/run/docker.sock", "/etc", "/", str(Path.home())):
            with self.subTest(path=path):
                with self.assertRaises(PolicyError):
                    self.run_py("print('should not run')", self.policy(filesystem="read-root"), implementation=Path(path))
        self.assertEqual(self.leftover(), [])

    def test_a_failing_program_reports_its_return_code_and_leaves_no_container(self):
        self.assertEqual(self.run_py("import sys; sys.exit(9)").returncode, 9)
        self.assertEqual(self.leftover(), [])


if __name__ == "__main__":
    unittest.main()
