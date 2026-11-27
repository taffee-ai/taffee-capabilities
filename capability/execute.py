"""T-005: run a tool under its declared sandbox policy.

`run_tool` is the one place a tool is executed through `Sandbox.run`. The policy comes from the verified
registry match (the tool's own manifest, digests recomputed by `Registry.resolve`), never from the caller.
A tool without a declaration is refused: there is no unrestricted default. The caller supplies the sandbox
and the repository root; nothing here chooses an image or opts out of a limit.

It is a library function a dispatcher will call. It does not authorize anything (that is govsec).
"""

import json
import time
from pathlib import Path

from .calltime import CallError, Problem, validate_input, validate_output
from .callrecord import CALL_ID, GRANT, CallRecordError, capability_ref, outcome_for
from .conformance import RUNNERS
from .languages import load_languages
from .sandbox import IMPLEMENTATION_TARGET, SandboxCleanupFailed, SandboxPolicy, SandboxUnavailable

WORK = "/work"  # where the repository root is mounted inside a container; the host path is never given to a tool
MAX_ERROR_CHARS = 2048
DEFAULTS = {"memoryMb": 256, "outputBytes": 64 * 1024, "stdinBytes": 64 * 1024}
# Closed text for a failure the sandbox reports; no tool text is ever copied into an error.
TOOL_ERRORS = {
    "timeout": "the tool ran past its time limit",
    "exit": "the tool exited with an error status",
    "output": "the tool wrote more than its output limit",
    "result": "the tool did not return one valid result line",
}


def policy_from_spec(spec: dict) -> SandboxPolicy:
    """The `SandboxPolicy` a tool's manifest declares. Raises `SandboxUnavailable` when it declares none."""
    block = spec.get("sandbox")
    if not isinstance(block, dict):
        raise SandboxUnavailable("(none declared)", ["a spec.sandbox declaration"])
    return SandboxPolicy(
        timeout_s=spec["timeoutMs"] / 1000,
        memory_mb=block.get("memoryMb", DEFAULTS["memoryMb"]),
        output_bytes=block.get("outputBytes", DEFAULTS["outputBytes"]),
        stdin_bytes=block.get("stdinBytes", DEFAULTS["stdinBytes"]),
        filesystem=block["filesystem"],
        network=block.get("network", "none"),
        env=tuple(block.get("env", [])),
    )


def _no_constant(name):
    raise ValueError(f"non-finite number {name}")


def _no_duplicates(pairs):
    keys = [key for key, _ in pairs]
    if len(keys) != len(set(keys)):
        raise ValueError("duplicate key")
    return dict(pairs)


def _parse_result(done) -> dict:
    """The tool's one result line, strictly. Any failure is a `{"ok": False}` result with closed text."""
    if done.timed_out:
        return {"ok": False, "error": TOOL_ERRORS["timeout"]}
    if done.truncated:
        return {"ok": False, "error": TOOL_ERRORS["output"]}
    if done.returncode != 0:
        return {"ok": False, "error": TOOL_ERRORS["exit"]}
    bad = {"ok": False, "error": TOOL_ERRORS["result"]}
    try:
        text = done.stdout.decode("utf-8")
        # Split on a line feed only. str.splitlines() would also split on U+2028, U+2029 and U+0085, which a valid
        # JSON string may contain raw, and would reject a correct result.
        line = text[:-1] if text.endswith("\n") else text
        if not line or "\n" in line:
            return bad
        result = json.loads(line, parse_constant=_no_constant, object_pairs_hook=_no_duplicates)
    except (UnicodeDecodeError, ValueError, RecursionError):
        return bad
    if not isinstance(result, dict) or type(result.get("ok")) is not bool:
        return bad
    if result["ok"]:
        if not isinstance(result.get("output"), str) or set(result) != {"ok", "output"}:
            return bad
    else:
        error = result.get("error")
        if not isinstance(error, str) or not error or len(error) > MAX_ERROR_CHARS or set(result) != {"ok", "error"}:
            return bad
    return result


def run_tool(registry, name: str, data: object, *, project_root, sandbox, range: str = "*",
             min_label: str = "Declared", languages=("python",), recorder=None, call_id: str | None = None,
             grant: str | None = None) -> dict:
    """Resolve `name` through `registry`, run it under the policy its manifest declares, return its result.

    Order: resolve (digests and revocation verified), policy from the verified spec (SandboxUnavailable if
    none), S-009 input check, sandboxed run, strict result parse, S-009 output check. Raises `RegistryError`,
    `SandboxUnavailable`, `CallError` or `SandboxCleanupFailed`; a tool that fails is a result with `ok: False`.
    `languages` is the caller's preference list for the implementation (the sandbox image decides what can run;
    the default matches the usual Python image). When `recorder` is given, `call_id` is required and is validated, with `grant`,
    **before** the tool is resolved or run (`CallRecordError`); one S-012 record is then written for every call.
    """
    if recorder is not None:  # a supplied recorder records every call, so its identifiers are checked before anything runs
        if not isinstance(call_id, str) or CALL_ID.fullmatch(call_id) is None:
            raise CallRecordError("a recorder was supplied: call_id is required and must be 1 to 64 characters of letters, digits and . _ : -")
        if grant is not None and (not isinstance(grant, str) or GRANT.fullmatch(grant) is None):
            raise CallRecordError("grant must be a reference of up to 128 characters, not free text")
    started = time.monotonic()
    match = None
    result = None
    error = None
    try:
        match = registry.resolve(name, range, kind="tool", minLabel=min_label, languages=list(languages))
        spec = match["spec"]
        policy = policy_from_spec(spec)
        validate_input(spec, data)
        if "implementation_path" in match:  # S-018 StoreRegistry: code lives in the store's code area
            language = registry.languages()[match["language"]]
            implementation = Path(match["implementation_path"])
            entry = Path(match["entry"])
        else:  # S-002 Registry over the file index
            language = load_languages(registry.root)[match["language"]]
            location = match["implementation_location"]
            implementation = Path(registry.root) / location
            entry = Path(match["entry_path"]).relative_to(location)
        argv = RUNNERS[language["runner"]](Path(IMPLEMENTATION_TARGET) / entry)
        stdin = json.dumps({"root": WORK, "input": data}).encode("utf-8")
        if len(stdin) > policy.stdin_bytes:  # an input problem, not a sandbox fault: refuse it before anything runs
            raise CallError("ValidationFailed", [Problem("input", "is larger than the declared stdinBytes")])
        done = sandbox.run(argv, root=project_root, policy=policy, stdin=stdin, implementation=implementation)
        result = _parse_result(done)
        if result["ok"]:
            validate_output(spec, result)
        return result
    except BaseException as exc:
        error = exc
        raise
    finally:
        if recorder is not None:
            _record(recorder, call_id, match, result, error, data, grant, started)


def _record(recorder, call_id, match, result, error, data, grant, started):
    """One S-012 record, from the closed outcome table. `SandboxCleanupFailed` is `error`/`Unexpected`: unconfirmed
    cleanup is never a clean result and never `tool_failed`."""
    if match is None:  # nothing resolved: there is no capability to name, so no record can be made
        return
    outcome = outcome_for(result=result, error=error)
    if isinstance(error, SandboxCleanupFailed):
        outcome = {"status": "error", "code": "Unexpected"}
    output = result.get("output") if isinstance(result, dict) and result.get("ok") is True else None
    recorder.record(
        call_id, capability_ref(match), outcome, language=match.get("language"), code_digest=match.get("code_digest"),
        revocation_seq=match.get("revocation_seq"), grant=grant, input_value=data, output=output,
        duration_ms=int((time.monotonic() - started) * 1000))
