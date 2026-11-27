"""S-019: run a tool for a caller over the network (`POST /v2/call`).

The transport-free core of the call route. It adds nothing to how a tool runs: `execute.run_tool` resolves the
tool (digests and revocation verified), takes the sandbox policy from the verified manifest, checks the input,
runs the tool in the sandbox, parses its one result line strictly and checks the postconditions. This module
decides only what the caller may name:

- the tool (`name`, `range`, `min_label`, `languages`) and its `input`;
- a `workspace`: a folder under the service's workspace root, mounted as the tool's root. It is named by a
  relative path and must stay inside the root. A tool whose sandbox reads or writes its root needs one; a tool
  that declares `filesystem: none` does not;
- a `call_id` (required) and a `grant` reference (optional), written into the S-012 call record.

`call_id` is idempotent per caller when the caller has a reply store (`call_replies.py`): the route layer in
`registry_api` claims it before the call and stores or releases it after, using `CallTracker.ran` to tell a call
whose tool ran from one refused before anything started.

Whether the caller may run the tool is `AdmissionAuthorizer.may_call`, asked for the exact version resolved. A
version the caller may not call is treated as absent, the same rule as visibility.
"""

from __future__ import annotations

import re
import shutil
import tempfile
import threading
from pathlib import Path

from .callrecord import CALL_ID, GRANT
from .execute import run_tool
from .registry import RegistryError
from .sandbox import PolicyError, SandboxUnavailable

NAME = re.compile(r"[a-z][a-z0-9-]{1,63}")
LABEL = re.compile(r"[A-Z][a-z]{1,15}")
LANGUAGE = re.compile(r"[a-z][a-z0-9-]{0,31}")
SEGMENT = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}")
MAX_RANGE = 64
MAX_WORKSPACE_DEPTH = 8
REQUEST_KEYS = {"name", "range", "min_label", "languages", "input", "workspace", "call_id", "grant"}


class CallRefused(Exception):
    """The request is refused before anything runs. `code` is a service error code."""

    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


class _Resolver:
    """The registry as `run_tool` sees it for one call: resolution limited to what the caller may call, and
    the resolved match kept so the reply can name exactly what ran."""

    def __init__(self, registry, may_call, needs_workspace_check):
        self._registry, self._may_call, self._check = registry, may_call, needs_workspace_check
        self.match = None

    def resolve(self, name, range="*", kind="tool", minLabel="Declared", languages=None):
        match = self._registry.resolve(name, range, kind, minLabel, languages, visible=self._may_call)
        self._check(match)
        self.match = match
        return match

    def languages(self):
        return self._registry.languages()


class CallTracker:
    """Whether a call's tool was started. A sandbox raises SandboxUnavailable or PolicyError only before it starts
    anything (Sandbox.run's contract), so entering the sandbox without one of those means the tool may have run."""

    def __init__(self):
        self.ran = False


class _TrackingSandbox:
    def __init__(self, sandbox, tracker):
        self._sandbox, self._tracker = sandbox, tracker
        self.name = getattr(sandbox, "name", "sandbox")

    def run(self, argv, **kwargs):
        try:
            result = self._sandbox.run(argv, **kwargs)
        except (SandboxUnavailable, PolicyError):
            raise
        except BaseException:
            self._tracker.ran = True
            raise
        self._tracker.ran = True
        return result


class ToolCaller:
    """Runs tools for `POST /v2/call`. Thread-safe: the HTTP server calls it from one thread per request."""

    def __init__(self, registry, sandbox, *, work_dir, workspaces=None, recorder=None, max_concurrent=4, replies=None):
        """`replies`, a `call_replies.CallReplyStore`, makes call_id idempotent per caller; without it every call runs."""
        if max_concurrent < 1:
            raise ValueError("max_concurrent must be at least 1")
        self._registry, self._sandbox, self._recorder = registry, sandbox, recorder
        self.replies = replies
        self._work = Path(work_dir)
        self._workspaces = None if workspaces is None else Path(workspaces).resolve()
        self._slots = threading.BoundedSemaphore(max_concurrent)
        self._record_lock = threading.Lock()  # CallRecorder has one writer; calls run in parallel

    # -- request checks --
    @staticmethod
    def parse(request: object) -> dict:
        """The validated request. Raises CallRefused("InvalidRequest") on anything else."""
        bad = CallRefused("InvalidRequest")
        if not isinstance(request, dict) or set(request) - REQUEST_KEYS:
            raise bad
        if not isinstance(request.get("name"), str) or NAME.fullmatch(request["name"]) is None:
            raise bad
        if "input" not in request:
            raise bad
        if not isinstance(request.get("call_id"), str) or CALL_ID.fullmatch(request["call_id"]) is None:
            raise bad
        grant = request.get("grant")
        if grant is not None and (not isinstance(grant, str) or GRANT.fullmatch(grant) is None):
            raise bad
        range_ = request.get("range", "*")
        if not isinstance(range_, str) or not range_ or len(range_) > MAX_RANGE:
            raise bad
        label = request.get("min_label", "Declared")
        if not isinstance(label, str) or LABEL.fullmatch(label) is None:
            raise bad
        languages = request.get("languages", ["python"])
        if (not isinstance(languages, list) or not 1 <= len(languages) <= 8
                or not all(isinstance(x, str) and LANGUAGE.fullmatch(x) for x in languages)):
            raise bad
        workspace = request.get("workspace")
        if workspace is not None:
            if not isinstance(workspace, str):
                raise bad
            parts = workspace.split("/")
            if not 1 <= len(parts) <= MAX_WORKSPACE_DEPTH or not all(SEGMENT.fullmatch(p) for p in parts):
                raise bad
        return {"name": request["name"], "range": range_, "min_label": label, "languages": list(languages),
                "input": request["input"], "workspace": workspace, "call_id": request["call_id"], "grant": grant}

    def _workspace(self, name: str) -> Path:
        """The workspace folder for `name`: an existing directory inside the workspace root, reached without
        leaving it. Every failure is NotFound, so a caller cannot probe the host's folders."""
        if self._workspaces is None:
            raise CallRefused("WorkspaceUnavailable")
        candidate = self._workspaces / name
        try:
            real = candidate.resolve(strict=True)
        except (OSError, RuntimeError):
            raise CallRefused("WorkspaceNotFound")
        if real != candidate or not real.is_relative_to(self._workspaces) or not real.is_dir():
            raise CallRefused("WorkspaceNotFound")  # a link anywhere on the way, or not a folder
        return real

    @staticmethod
    def _needs_root(match: dict) -> bool:
        block = match.get("spec", {}).get("sandbox")
        return isinstance(block, dict) and block.get("filesystem", "none") != "none"

    # -- the call --
    def call(self, request: object, may_call, tracker: CallTracker | None = None) -> dict:
        """Run one call. Returns the reply body. Raises CallRefused, RegistryError, CallError and the sandbox
        errors; the route maps each to a closed error code. `tracker.ran` says whether the tool was started."""
        tracker = tracker or CallTracker()
        req = self.parse(request)
        root = self._workspace(req["workspace"]) if req["workspace"] is not None else None

        def check(match):
            if root is None and self._needs_root(match):
                raise CallRefused("WorkspaceRequired")

        if not self._slots.acquire(blocking=False):
            raise CallRefused("Busy")
        scratch = None
        try:
            if root is None:  # the tool declared no file access; it still needs a folder to start in
                self._work.mkdir(parents=True, exist_ok=True)
                scratch = Path(tempfile.mkdtemp(prefix="call-", dir=self._work))
            resolver = _Resolver(self._registry, may_call, check)
            result = run_tool(resolver, req["name"], req["input"], project_root=root or scratch,
                              sandbox=_TrackingSandbox(self._sandbox, tracker), range=req["range"], min_label=req["min_label"],
                              languages=tuple(req["languages"]), recorder=self._locked_recorder(),
                              call_id=req["call_id"], grant=req["grant"])
            match = resolver.match
            return {"call_id": req["call_id"], "result": result,
                    "capability": {key: match[key] for key in ("name", "version", "identity", "label", "language",
                                                                "code_digest", "revocation_seq")}}
        finally:
            self._slots.release()
            if scratch is not None:
                shutil.rmtree(scratch, ignore_errors=True)

    def _locked_recorder(self):
        if self._recorder is None:
            return None
        return _LockedRecorder(self._recorder, self._record_lock)


class _LockedRecorder:
    def __init__(self, recorder, lock):
        self._recorder, self._lock = recorder, lock

    def record(self, *args, **kwargs):
        with self._lock:
            return self._recorder.record(*args, **kwargs)


__all__ = ["CallRefused", "CallTracker", "ToolCaller", "RegistryError"]
