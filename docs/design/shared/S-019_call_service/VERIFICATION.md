# S-019 verification: Call service (`POST /v2/call`)

Status: implemented and tested with the feature's own and affected tests, 2026-11-26. Not yet reviewed.

Written for: reviewers of S-019 and engineers building a caller of `/v2/call`.

Spec: [SPEC.md](SPEC.md). The four built-in tools run through this route since T-005 Stage B, recorded in [T-005 VERIFICATION.md](../../tools/T-005_sandboxed_execution/VERIFICATION.md).

## What was built

| File | Change |
|---|---|
| `capability/tool_call.py` | New. `ToolCaller`: request checks, workspace resolution under the workspace root, the `may_call` filter on resolution, the concurrency bound, a lock around the single-writer call recorder, and the call itself through `execute.run_tool`. |
| `capability/registry_api.py` | The `POST /v2/call` route, its body bound (1 MiB + 8 KiB), and closed error codes: `ValidationFailed` (422, with problem fields), `PostconditionFailed` (502), `SandboxUnavailable` (409), `WorkspaceRequired` (400), `WorkspaceNotFound` (404), `WorkspaceUnavailable` (409), `CallsDisabled` (404). |
| `capability/authz.py` | `AdmissionAuthorizer.may_call`, defaulting to `may_see`. |
| `capability/call_replies.py` | New (SVC-11). The reply store: one SQLite file with `(principal, call_id)` claims, the request digest, stored replies, the retention window, and recovery of claims a stopped service left running. |
| `capability/registry_cli.py` | `serve --workspaces DIR --call-log FILE --call-workers N --no-calls`. The workspace root is added to the sandbox's allowed mount roots. |
| `deploy/compose.yaml` | `--workspaces=${TAFFEE_RUN}/workspaces`, `--call-workers=${TAFFEE_CALL_WORKERS:-4}`. |

## Results

All runs on the Linux test host, in Docker.

| Run | Scope | Result |
|---|---|---|
| Feature tests, no Docker | `tests/test_tool_call.py` | 20 tests: 17 OK, 1 failed, 2 container tests skipped. The failure was a wrong expectation, corrected: `GET /v2/call` answers 404, as every POST-only path does. The corrected test passed in the affected run below. |
| Affected, with the Docker socket | `test_tool_call`, `test_registry_api`, `test_registry_cli`, `test_execute`, `test_mcp_server`, `test_registry_e2e` | 103 tests OK, none skipped. |
| After T-005 Stage B, with the Docker socket | `test_tool_call`, `test_dummy_orchestrator`, `test_execute`, `test_registry_e2e` | 95 tests OK, none skipped. Includes the built-in grep and git-log through `/v2/call` in real containers, git-log on a repository owned by another user. |
| Idempotent `call_id` (SVC-11), with the Docker socket | `tests/test_call_replies.py` (new) and `tests/test_tool_call.py` (its fixture builder changed) | 76 tests OK, none skipped; no sandbox container left. |
| Complete feature tests, with the Docker socket | `tests/test_tool_call.py`, rewritten so every requirement and error code has a test | 46 tests OK, none skipped; no sandbox container left. A first run had 2 failures, both wrong test assumptions: the registry supports only `*`, exact and `^` ranges. The tests now use those, and the unsupported forms are tested as `InvalidRange`. |

The full suite was not run for this change.

## Coverage by requirement

| Requirement | Unit (unconfined test sandbox) | Functional (real container sandbox) |
|---|---|---|
| SVC-1 request shape | 27 malformed requests, 4 malformed bodies, the largest allowed values accepted | |
| SVC-2 resolved on every call | range pinning (exact, `^`, `*`), `InvalidRange`, `NotFound` out of range, `NoImplementation`, `BelowLabel`, a version revoked between two calls (`Revoked`, the older version still runs), `RevocationStale` with `Retry-After` | the built-in tools resolve from the imported catalog |
| SVC-3 may_call | asked separately from may_see, defaults to may_see, hidden equals absent, an authorizer that cannot answer is `AuthorizerUnavailable` | |
| SVC-4 workspace | 11 malformed and 5 escaping workspace names (links out of and inside the root, a file, an absent folder), the deepest allowed path, no host path in any reply | read-only mount for read-root, writes land for write-root, no workspace files for `filesystem: none` |
| SVC-5 workspace required | `WorkspaceRequired` for read-root and write-root, nothing recorded; no-file-access tools run with or without one; scratch folders removed | a workspace-free tool in a container |
| SVC-6 input and output | 6 schema violations with problems and no values, `PostconditionFailed` (502), a tool-level failure, a tool past its time limit | postconditions and the time limit in a container; grep and git-log (git-log on a repository owned by another user) |
| SVC-7 what ran | the exact reply keys and capability fields | grep reports its current version and `Tested` |
| SVC-8 records | one record each for ok, tool_failed, input_rejected and output_rejected, with call_id and grant and no content; none for calls refused before resolution; `SandboxUnavailable` and an unconfirmed cleanup are recorded | every container call recorded |
| SVC-9 limits | `Busy` with `Retry-After` while every slot is held; the bound holds under parallel calls and the record chain verifies; the body bound exact at 1 MiB + 8 KiB and 413 above it, on a real socket | no container left after the calls |
| SVC-11 idempotent call_id | store: claim, replay, conflict while running and when done, in progress, release, a done reply never released or overwritten, scope per principal, the window ending at exactly 24 h, expired rows deleted, done replies surviving a restart, running claims becoming `Interrupted`, 20 simultaneous first calls claiming once; digest: every field but `call_id` counts, key order does not. Route: a retry replays status and body with `X-Call-Replayed` and runs once with one record; replays of a tool failure, a timeout and a 502; `CallIdConflict` for each of 7 changed fields and a dropped field; refusals before the start give the call_id back (ValidationFailed, WorkspaceRequired, WorkspaceNotFound, NotFound, BelowLabel, InvalidRange, Revoked, RevocationStale, Busy, SandboxUnavailable, AuthorizerUnavailable); a retry while the first call runs is held and gets the first response, a 200 and a 502 alike, for three simultaneous retries, with one run; `InProgress` with `Retry-After` only past the hold bound; store-level: a held retry gets the reply on finish, claims on give-back, times out, a conflict is never held, ten held retries get one reply; per-principal scope; runs again after the window; `Interrupted` after a restart; a failure after the start keeps the call_id; without a store every call runs; identical bytes over a socket; the serve option | a retried write-root tool in a container runs once: the workspace file changed after the first call is not overwritten, one record |
| SVC-10 switch | `CallsDisabled` without a caller, `WorkspaceUnavailable` without a workspace root, the four serve options exist, a caller needs at least one slot | |

## Limits

- One container per call. Start-up adds roughly a second or more per call on the test host; it has not been measured separately.
- The workspace must be on the registry's host, under the workspace root. A caller on another machine has to place its files there first.
- `may_call` equals `may_see` until govsec provides the decision; the authorizer is the development one (`X-Dev-Principal`, unchecked).
- A call refused before resolution (bad request, workspace refused, unknown or hidden tool) writes no call record, because there is no capability to name.
