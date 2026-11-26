"""S-011: sandbox interface.

Run capability code under limits that are enforced, or refuse to run it. A `SandboxPolicy` states
time, memory, output size, filesystem scope and network scope. A sandbox states which limits it can
enforce (`enforces`). `Sandbox.run` refuses, before starting anything, a policy that needs a limit
the sandbox cannot enforce. There is no silent fallback to unrestricted execution: running without
confinement is a decision written in the policy (`filesystem="unrestricted"`, `network="allowed"`).

`ProcessSandbox` enforces what needs no privileges. `ContainerSandbox` enforces the full set through
Docker. Neither reads a manifest: where a policy comes from is the caller's business.
"""

import math
import os
import re
import resource
import shutil
import signal
import subprocess
import tempfile
import threading
import time
import uuid
from dataclasses import dataclass, replace
from pathlib import Path

FILESYSTEMS = ("none", "read-root", "write-root", "unrestricted")
NETWORKS = ("none", "allowed")
LIMITS = frozenset({"time", "memory", "output", "stdin", "env", "filesystem", "network", "processes"})
ENV_NAME = re.compile(r"[A-Z_][A-Z0-9_]{0,63}")
IMPLEMENTATION_TARGET = "/impl"
# An image reference starts with a letter or digit. A leading "-" would be parsed by Docker as an option
# (--privileged, --network=host, -v), because the image is placed among the options of `docker run`.
IMAGE_REF = re.compile(r"[A-Za-z0-9][A-Za-z0-9._/:@+-]{0,254}")
# Trees that are never mounted, whatever the caller asks: system state, device and process trees, and the
# places the Docker socket lives. The check is on the resolved path, so a link into them is caught too.
NEVER_MOUNTED = tuple(Path(p) for p in ("/etc", "/proc", "/sys", "/dev", "/run", "/var/run", "/root", "/boot", "/var/lib/docker"))


class SandboxError(Exception):
    """Base class for sandbox failures."""


class PolicyError(SandboxError):
    """A policy or a call is malformed, and nothing was started."""


class SandboxCleanupFailed(SandboxError):
    """The container could not be confirmed gone. No result is returned: it may still be running."""

    code = "SandboxCleanupFailed"

    def __init__(self, container: str):
        self.container = container
        super().__init__(f"container {container} could not be confirmed removed; it may still be running")


class SandboxUnavailable(SandboxError):
    """The sandbox cannot enforce a limit the policy needs, so nothing was started."""

    code = "SandboxUnavailable"

    def __init__(self, sandbox: str, unmet: list):
        self.sandbox = sandbox
        self.unmet = sorted(unmet)
        super().__init__(f"{sandbox} cannot enforce: {', '.join(self.unmet)}")


@dataclass(frozen=True)
class SandboxPolicy:
    """The limits for one run. Out-of-range or unknown values are refused at construction."""

    timeout_s: float = 5.0
    memory_mb: int = 256
    output_bytes: int = 64 * 1024
    stdin_bytes: int = 64 * 1024
    filesystem: str = "none"
    network: str = "none"
    env: tuple = ()

    def __post_init__(self):
        def bad(message):
            raise PolicyError(message)

        if isinstance(self.timeout_s, bool) or not isinstance(self.timeout_s, (int, float)) or not 0.1 <= self.timeout_s <= 600:
            bad("timeout_s must be between 0.1 and 600")
        if isinstance(self.memory_mb, bool) or not isinstance(self.memory_mb, int) or not 16 <= self.memory_mb <= 8192:
            bad("memory_mb must be an integer between 16 and 8192")
        if isinstance(self.output_bytes, bool) or not isinstance(self.output_bytes, int) or not 1 <= self.output_bytes <= 1024 * 1024:
            bad("output_bytes must be an integer between 1 and 1 MiB")
        if isinstance(self.stdin_bytes, bool) or not isinstance(self.stdin_bytes, int) or not 0 <= self.stdin_bytes <= 1024 * 1024:
            bad("stdin_bytes must be an integer between 0 and 1 MiB")
        if self.filesystem not in FILESYSTEMS:
            bad(f"filesystem must be one of {', '.join(FILESYSTEMS)}")
        if self.network not in NETWORKS:
            bad(f"network must be one of {', '.join(NETWORKS)}")
        if not isinstance(self.env, tuple) or not all(isinstance(n, str) and ENV_NAME.fullmatch(n) for n in self.env):
            bad("env must be a tuple of upper-case variable names")


def required(policy: SandboxPolicy) -> frozenset:
    """The limits a sandbox must enforce to run this policy."""
    needed = {"time", "memory", "output", "stdin", "env"}
    if policy.filesystem != "unrestricted":
        needed.add("filesystem")
    if policy.network != "allowed":
        needed.add("network")
    return frozenset(needed)


@dataclass(frozen=True)
class RunResult:
    """Plain data. It never carries the policy's host paths."""

    returncode: int | None
    stdout: bytes
    stderr: bytes
    timed_out: bool
    truncated: bool


def _supervise(proc: subprocess.Popen, kill, policy: SandboxPolicy, stdin: bytes,
               deadline_s: float | None = None) -> RunResult:
    """Feed stdin, read bounded output, enforce the wall-clock timeout, and always reap the child.
    `deadline_s` (default: the policy's timeout) is when the child is killed from outside."""
    deadline_s = policy.timeout_s if deadline_s is None else deadline_s
    state = {"timed_out": False, "truncated": False}
    chunks = {"out": [], "err": []}

    def expire():
        state["timed_out"] = True
        kill()

    timer = threading.Timer(deadline_s, expire)

    def read(stream, key):
        total = 0
        while True:
            try:
                data = os.read(stream.fileno(), 65536)
            except OSError:
                break
            if not data:
                break
            room = policy.output_bytes - total
            if len(data) > room:
                chunks[key].append(data[:max(room, 0)])
                state["truncated"] = True
                kill()
                break
            chunks[key].append(data)
            total += len(data)

    def feed():
        try:
            proc.stdin.write(stdin)
        except (BrokenPipeError, OSError):
            pass
        finally:
            try:
                proc.stdin.close()
            except (BrokenPipeError, OSError):
                pass

    threads = [threading.Thread(target=read, args=(proc.stdout, "out")),
               threading.Thread(target=read, args=(proc.stderr, "err")),
               threading.Thread(target=feed)]
    timer.start()
    for thread in threads:
        thread.start()
    try:
        try:
            proc.wait(timeout=deadline_s + 10)
        except subprocess.TimeoutExpired:
            state["timed_out"] = True
            kill()
            proc.wait(timeout=10)
    finally:
        timer.cancel()
        for thread in threads:
            thread.join(timeout=10)
        for pipe in (proc.stdout, proc.stderr):
            try:
                pipe.close()
            except OSError:
                pass
    return RunResult(proc.returncode, b"".join(chunks["out"]), b"".join(chunks["err"]),
                     state["timed_out"], state["truncated"])


class Sandbox:
    """Base class. Subclasses set `name` and `enforces` and implement `_launch`."""

    name = "sandbox"
    enforces: frozenset = frozenset()

    def run(self, argv: list, *, root, policy: SandboxPolicy, stdin: bytes = b"",
            env_values: dict | None = None, implementation=None) -> RunResult:
        """Run `argv` under `policy`, or raise before starting anything.

        `argv` is a list (no shell). `env_values` supplies values for the names in `policy.env`; every
        other variable is dropped. `implementation` is the one extra host folder a sandbox may mount: the
        implementation being run, read-only, at the fixed path /impl. There is no way to name another
        mount or another target.
        """
        if not isinstance(policy, SandboxPolicy):
            raise PolicyError("policy must be a SandboxPolicy")
        unmet = required(policy) - self.enforces
        if unmet:
            raise SandboxUnavailable(self.name, list(unmet))
        if not isinstance(argv, list) or not argv or not all(isinstance(a, str) for a in argv):
            raise PolicyError("argv must be a non-empty list of strings")
        if not isinstance(stdin, bytes) or len(stdin) > policy.stdin_bytes:
            raise PolicyError(f"stdin must be bytes of at most {policy.stdin_bytes}")
        values = {name: str(value) for name, value in (env_values or {}).items() if name in policy.env}
        return self._launch(argv, Path(root), policy, stdin, values, None if implementation is None else Path(implementation))

    def _launch(self, argv, root, policy, stdin, env_values, implementation) -> RunResult:
        raise NotImplementedError


class ProcessSandbox(Sandbox):
    """Limits that need no privileges: time, memory (address space, approximate), output, stdin, env.

    It cannot confine the filesystem or the network, so it refuses any policy that needs either, unless
    the policy says `unrestricted` / `allowed` in so many words.
    """

    name = "process"
    enforces = frozenset({"time", "memory", "output", "stdin", "env"})

    def _launch(self, argv, root, policy, stdin, env_values, implementation) -> RunResult:
        if implementation is not None:
            raise PolicyError("the process sandbox does not mount anything; pass absolute paths in argv")
        memory = policy.memory_mb * 1024 * 1024
        cpu = math.ceil(policy.timeout_s) + 1

        def limits():
            resource.setrlimit(resource.RLIMIT_AS, (memory, memory))
            resource.setrlimit(resource.RLIMIT_CPU, (cpu, cpu))
            resource.setrlimit(resource.RLIMIT_CORE, (0, 0))

        proc = subprocess.Popen(argv, cwd=str(root), env=env_values, stdin=subprocess.PIPE,
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                start_new_session=True, preexec_fn=limits)

        def kill():
            try:
                os.killpg(proc.pid, signal.SIGKILL)
            except (ProcessLookupError, PermissionError):
                pass

        return _supervise(proc, kill, policy, stdin)


OWNER_LABEL = "org.taffee.sandbox.owner"
OWNER = re.compile(r"[A-Za-z0-9._-]{1,64}")
TIMEOUT_STATUS = 124  # coreutils `timeout`: the program ran past its limit


class ContainerSandbox(Sandbox):
    """The full set through Docker: an unprivileged, capability-free, read-only container.

    The time limit is the tool's, counted from when its process starts: inside the container the program runs
    under coreutils `timeout` (so the image must provide it), and exit status 124 means it ran out of time.
    Creating and starting a container is the host's work, not the tool's, and on a busy host it alone can take
    longer than a tool's whole limit. So the outer timer, the backstop that kills the container from outside,
    allows `startup_allowance_s` on top of the limit.

    `owner` labels every container this sandbox starts (`org.taffee.sandbox.owner`). A process killed while
    Docker is creating a container leaves it behind in `Created`, with nobody to start or remove it. The one
    process that owns the label removes such leftovers at start-up with `remove_owned`; containers of other
    owners (another service, a test run on the same host) are never touched.
    """

    name = "container"
    enforces = LIMITS

    def __init__(self, image: str, *, docker: str = "docker", user: str = "65534:65534",
                 pids_limit: int = 64, cpus: float = 1.0, tmpfs_mb: int = 16,
                 allowed_mount_roots: tuple = (), cleanup_wait_s: float = 20.0, startup_allowance_s: float = 60.0,
                 owner: str | None = None):
        """`allowed_mount_roots` are the only host trees anything may be mounted from. Empty means nothing
        can be mounted, so only `filesystem="none"` policies run."""
        if not isinstance(image, str) or IMAGE_REF.fullmatch(image) is None:
            raise PolicyError("image must be an image reference: letters, digits and . _ / : @ + -, not starting with '-'")
        self._image, self._docker, self._user = image, docker, user
        self._pids, self._cpus, self._tmpfs = pids_limit, cpus, tmpfs_mb
        self._allowed = tuple(Path(r) for r in allowed_mount_roots)
        self._cleanup_wait = cleanup_wait_s
        if isinstance(startup_allowance_s, bool) or not isinstance(startup_allowance_s, (int, float)) \
                or not 0 <= startup_allowance_s <= 600:
            raise PolicyError("startup_allowance_s must be between 0 and 600")
        self._startup = float(startup_allowance_s)
        if owner is not None and (not isinstance(owner, str) or OWNER.fullmatch(owner) is None):
            raise PolicyError("owner must be 1 to 64 letters, digits, '.', '_' or '-'")
        self._owner = owner

    def remove_owned(self) -> int:
        """Remove every container labelled with this sandbox's owner, in any state. Call only at the start of
        the one process that owns the label, before it runs anything. Returns how many were removed."""
        if self._owner is None:
            return 0
        try:
            listed = subprocess.run([self._docker, "ps", "-aq", "--filter", f"label={OWNER_LABEL}={self._owner}"],
                                    capture_output=True, text=True, timeout=60, check=False)
        except FileNotFoundError as exc:
            raise SandboxError(f"cannot list containers: the docker command {self._docker!r} is not installed") \
                from exc
        if listed.returncode != 0:
            raise SandboxError(f"cannot list containers: {listed.stderr.strip()[:200]}")
        ids = listed.stdout.split()
        if ids:
            subprocess.run([self._docker, "rm", "-f", *ids], capture_output=True, timeout=120, check=False)
        return len(ids)

    def _checked(self, source: Path, what: str) -> Path:
        """A host path that may be mounted: absolute, a real directory with no link on the way, not a system
        tree or socket, and under an allowed mount root. Anything else is refused before Docker is called."""
        text = str(source)
        if not source.is_absolute() or any(c in text for c in ",\n\0"):
            raise PolicyError(f"{what}: a mount source must be an absolute path with no comma or newline")
        if not self._allowed:
            raise PolicyError(f"{what}: no allowed mount roots are configured, so nothing may be mounted")
        walk = Path(source.anchor)
        for part in source.parts[1:]:
            walk = walk / part
            if walk.is_symlink():
                raise PolicyError(f"{what}: {source} passes through a link")
        if not source.is_dir():
            raise PolicyError(f"{what}: {source} must be an existing directory")
        resolved = source.resolve()
        if resolved == Path(resolved.anchor) or any(resolved == bad or bad in resolved.parents for bad in NEVER_MOUNTED):
            raise PolicyError(f"{what}: {source} is a system path that is never mounted")
        if not any(resolved == base.resolve() or base.resolve() in resolved.parents for base in self._allowed):
            raise PolicyError(f"{what}: {source} is not under an allowed mount root")
        return resolved

    @staticmethod
    def _mount(source: Path, target: str, mode: str) -> list:
        return ["--mount", f"type=bind,src={source},dst={target}" + (",readonly" if mode == "ro" else "")]

    def build_argv(self, argv, *, name: str, root, policy: SandboxPolicy, env_file=None, implementation=None) -> list:
        """The exact `docker run` command. Pure, so every flag can be tested without Docker."""
        mb = policy.memory_mb
        cmd = [self._docker, "run", "--rm", "-i", "--name", name,
               "--user", self._user, "--cap-drop", "ALL", "--security-opt", "no-new-privileges",
               "--read-only", "--tmpfs", f"/tmp:rw,noexec,nosuid,nodev,size={self._tmpfs}m",
               "--memory", f"{mb}m", "--memory-swap", f"{mb}m", "--pids-limit", str(self._pids),
               "--cpus", str(self._cpus), "--ulimit", "core=0"]
        if self._owner is not None:
            cmd += ["--label", f"{OWNER_LABEL}={self._owner}"]
        if policy.network == "none":
            cmd += ["--network", "none"]
        workdir = "/tmp"
        if policy.filesystem != "none":
            cmd += self._mount(self._checked(Path(root), "root"), "/work",
                               "rw" if policy.filesystem in ("write-root", "unrestricted") else "ro")
            workdir = "/work"
        if implementation is not None:
            cmd += self._mount(self._checked(Path(implementation), "implementation"), IMPLEMENTATION_TARGET, "ro")
        if env_file is not None:
            # Names and values reach the container through a private file, never through the docker client's own
            # environment (where DOCKER_HOST, PATH or LD_PRELOAD would change what the client does) and never argv.
            cmd += ["--env-file", str(env_file)]
        # The tool's own limit, from its start; -k: SIGKILL 2 s after SIGTERM if it is ignored.
        cmd += ["-w", workdir, self._image, "timeout", "-k", "2", f"{policy.timeout_s:g}", *argv]
        return cmd

    def _launch(self, argv, root, policy, stdin, env_values, implementation) -> RunResult:
        name = "taffee-sbx-" + uuid.uuid4().hex[:16]
        for key, value in env_values.items():
            if any(c in value for c in "\n\r\0"):
                raise PolicyError(f"the value of {key} must not contain a newline or NUL")
        scratch = None
        try:
            env_file = None
            if env_values:
                scratch = tempfile.mkdtemp(prefix="taffee-sbx-env-")  # mode 0700
                env_file = Path(scratch) / "env"
                fd = os.open(env_file, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
                with os.fdopen(fd, "w", encoding="utf-8") as handle:
                    handle.write("".join(f"{key}={env_values[key]}\n" for key in sorted(env_values)))
            return self._run_container(argv, root, policy, stdin, name, env_file, implementation)
        finally:
            if scratch is not None:
                shutil.rmtree(scratch, ignore_errors=True)

    def _run_container(self, argv, root, policy, stdin, name, env_file, implementation) -> RunResult:
        cmd = self.build_argv(argv, name=name, root=root, policy=policy, env_file=env_file,
                              implementation=implementation)
        # The docker client's environment is the operator's, never the caller's: only these two host variables.
        process_env = {key: os.environ[key] for key in ("PATH", "DOCKER_HOST") if key in os.environ}
        proc = subprocess.Popen(cmd, env=process_env, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                stderr=subprocess.PIPE, start_new_session=True)

        def kill():
            # Killing the docker client before the container exists strands a half-created container: the
            # daemon finishes creating it with nobody left to start or remove it. So keep asking the daemon
            # to kill the container until it can (it exists and is running), or the client has exited. Only
            # after a deadline is the client itself killed, and _remove then clears whatever is left.
            deadline = time.monotonic() + 15
            while proc.poll() is None and time.monotonic() < deadline:
                try:
                    done = subprocess.run([self._docker, "kill", name], capture_output=True, timeout=15, check=False)
                    if done.returncode == 0:
                        return
                except (subprocess.TimeoutExpired, OSError):
                    pass
                time.sleep(0.05)
            try:
                proc.kill()
            except ProcessLookupError:
                pass

        try:
            result = _supervise(proc, kill, policy, stdin, deadline_s=policy.timeout_s + self._startup)
            if result.returncode == TIMEOUT_STATUS and not result.timed_out:
                result = replace(result, timed_out=True)  # a tool exiting 124 itself fails either way
        except BaseException as exc:
            if not self._remove(name):
                raise SandboxCleanupFailed(name) from exc
            raise
        # --rm removes a container that exited. One that was killed under the CLI may linger, and a removal
        # already in progress makes `rm -f` return at once, so confirm it is really gone. If that cannot be
        # confirmed, no result is returned: a result would say the run was clean while a container remains.
        if not self._remove(name):
            raise SandboxCleanupFailed(name)
        return result

    def _remove(self, name: str) -> bool:
        """True once the container is confirmed gone; False if that cannot be confirmed in time."""
        deadline = time.monotonic() + self._cleanup_wait
        while True:
            try:
                subprocess.run([self._docker, "rm", "-f", name], capture_output=True, timeout=30, check=False)
                listed = subprocess.run([self._docker, "ps", "-aq", "--filter", f"name=^{name}$"],
                                        capture_output=True, timeout=30, check=False)
                if listed.returncode == 0 and not listed.stdout.strip():
                    return True
            except (subprocess.TimeoutExpired, OSError):
                pass
            if time.monotonic() > deadline:
                return False
            time.sleep(0.2)


def docker_available(docker: str = "docker") -> str | None:
    """None when Docker answers, else the reason it does not. Lets tests skip with a recorded reason."""
    try:
        done = subprocess.run([docker, "info", "--format", "{{.ServerVersion}}"], capture_output=True, timeout=20, check=False)
    except FileNotFoundError:
        return "the docker command is not installed"
    except subprocess.TimeoutExpired:
        return "docker did not answer"
    if done.returncode != 0:
        return "the docker daemon is not reachable"
    return None
