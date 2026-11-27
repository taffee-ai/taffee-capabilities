# S-019: Call service (`POST /v2/call`)

Status: implemented and tested (see [VERIFICATION.md](VERIFICATION.md)). Depends on S-018 (the /v2 service and the store), T-005 (the executor and the sandbox policy) and S-012 (call records). Roadmap: [../../../roadmap.md](../../../roadmap.md).

Written for: engineers building a caller of the registry (an orchestrator, a worker host) and the engineers who maintain the /v2 service.

## 1. Purpose

A caller that cannot run tools itself asks the registry to run one. The registry already holds every admitted tool, verifies it on every resolve, and has the container sandbox that admission uses. This feature puts one HTTP route in front of the existing executor (`execute.run_tool`). It adds no new way to run code.

Where a tool runs is a choice of the caller's architecture:

| The tool runs | Language | Path |
|---|---|---|
| In the registry's container sandbox | any language the sandbox image runs (Python today) | this feature |
| Inside the caller's own process or sandbox | a component the caller can load | not in this feature: it needs a bundle endpoint (planned) |

## 2. Requirements

| ID | Requirement |
|---|---|
| CAP-CALL-SVC-1 | `POST /v2/call` takes `{name, input, call_id}` and optionally `range`, `min_label`, `languages`, `workspace` and `grant`. Any other key, or a value of the wrong shape, is `InvalidRequest` before anything is resolved. |
| CAP-CALL-SVC-2 | The tool is resolved on every call with the S-018 rules: digests recomputed, revocation checked (fail closed), label floor applied. The caller cannot supply a policy, an image, a code path or a version digest. |
| CAP-CALL-SVC-3 | The caller may run only a version `may_call` allows; any other version is answered as absent (`NotFound`). Until govsec provides the decision, `may_call` equals `may_see`. |
| CAP-CALL-SVC-4 | `workspace` names a folder under the service's workspace root by a relative path of up to eight segments. A path that leaves the root, passes through a link, or is not an existing folder is refused. The folder is mounted as the tool's root, read-only unless the tool declares `write-root`. |
| CAP-CALL-SVC-5 | A tool whose sandbox declares `read-root` or `write-root` needs a workspace (`WorkspaceRequired`); a tool declaring `filesystem: none` runs in an empty scratch folder that is removed after the call. |
| CAP-CALL-SVC-6 | Input is checked against the tool's schema before the run (`ValidationFailed`, 422, with problem fields that carry no values); output is checked against its postconditions after it (`PostconditionFailed`, 502). A tool-level failure is a 200 answer with `ok: false`. |
| CAP-CALL-SVC-7 | The answer names exactly what ran: name, version, identity, label, language, code digest and the revocation sequence it was checked against. |
| CAP-CALL-SVC-8 | Every call that resolved writes one S-012 record with the caller's `call_id` and `grant`. A call refused before resolution writes none. Records hold references, never content. |
| CAP-CALL-SVC-9 | At most `--call-workers` calls run at once; another call is `Busy` (429, `Retry-After`). The request body is bounded at 1 MiB plus 8 KiB (the largest `stdinBytes` a tool may declare). |
| CAP-CALL-SVC-10 | The route can be turned off (`--no-calls`); it then answers `CallsDisabled`. |
| CAP-CALL-SVC-11 | `call_id` is idempotent per caller, so a retry never runs a tool twice. The first call whose tool starts takes `(principal, call_id)`. The same request again gets the stored first reply (same status and body, header `X-Call-Replayed: true`), with no second run and no second call record. A different request under that `call_id` (any field but `call_id` differs) is `CallIdConflict` (409). While the first call is still running, a retry of the same request is held until that call finishes and then gets its reply, so every retry gets the same response; only if the hold bound (`--call-hold-seconds`, default 660, longer than the slowest tool a manifest may declare plus the start-up allowance) passes first is it `InProgress` (409, `Retry-After: 5`). A conflict is answered at once, never held. A call refused before its tool started takes nothing, so a retry runs normally. A call whose tool started keeps the `call_id` whatever its outcome (200, 502, 500). A `call_id` is held for `--call-reply-hours` (default 24) from the first call, then freed. Stored replies are kept apart from the call records, in a short-lived store deleted at the end of the window. A call that was running when the service stopped is never run again: its retry answers `Interrupted` (500). |

## 3. Not in this feature

- Running a tool inside the caller (a bundle endpoint and a component build target).
- A real per-caller decision and a real authorizer (governance).
- Credentialed and external tools (T-006).
- Warm container pools. Each call starts one container.

The four built-in tools run through this route since T-005 Stage B, which gave each a declared read-root, no-network policy.

## 4. Tests

`tests/test_tool_call.py`: every requirement and error code above, at unit level with a test-only sandbox and functionally in the real container sandbox. The coverage table is in [VERIFICATION.md](VERIFICATION.md).
