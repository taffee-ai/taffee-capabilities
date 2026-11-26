# T-005 verification record: sandboxed execution of tools (Stage A)

Status: Stage A implemented, tested and **accepted in review for the agreed scope (2026-10-20)**, after two independent reviews. Contract and bounds as agreed in `CONTRACT-NOTE.md`. **Stage B (declaring the policy on the four built-in tools) is not started and needs a separate review.** T-005's exit gate is met by the fixture tools; the four built-in tools are not yet run sandboxed by default (the executor refuses them).

Written for: the reviewer who will check this, and whoever declares limits on the next tool.

## What was built

| File | Change |
|---|---|
| `schemas/manifest.tool.schema.json` | `spec.sandbox` (optional, `filesystem` required: `none`, `read-root`, `write-root`; `memoryMb` 16 to 8192; `outputBytes` 64 to 65536 (the S-009 ceiling; the floor is above the smallest valid result line); `stdinBytes` 64 to 1048576; `network` only `none`; `env` up to 16 unique upper-case names). `spec.timeoutMs` is now 100 to 600000, so a valid manifest is never unexecutable. `unrestricted` and `network: allowed` cannot be declared. |
| `capability/execute.py` (new) | `policy_from_spec(spec)` (declared block to `SandboxPolicy`; no block is `SandboxUnavailable`) and `run_tool(registry, name, data, *, project_root, sandbox, ...)`. |
| `capability/registry.py` | A read-only `root` property (the executor mounts implementations from it). |
| `tests/test_execute.py` (new) | 49 tests (see below). |
| `tests/golden/index.json`, `taffee-core/catalog/capabilities.yaml` | Regenerated: only `inputs_digest` and `index_digest` changed (checked path by path); entries are identical. |

`run_tool` order: resolve through the registry (digests and revocation verified; the spec comes from this verified match, never from the caller, and there is no policy parameter), policy from the verified spec, S-009 `validate_input`, the sandboxed run with the implementation at `/impl` and the repository root at `/work` (the host path is never given to the tool), a strict parse of the one result line (the T-007 rules), S-009 `validate_output`. A failing tool is an `{"ok": false}` result with closed text; no tool text or stderr is copied. `SandboxCleanupFailed` is raised and recorded as `error`/`Unexpected`, never `tool_failed` or a clean result. With a recorder, `call_id` is required and is validated with `grant` **before** anything is resolved or run.

## What moved, and what did not

| Item | Result |
|---|---|
| `inputs_digest` | `sha256:4323d822...193439f` to `sha256:3db90887...08df5` (the schema is an indexed input; it moved three times as the limits were corrected: the second reviewer's findings, then the first reviewer's) |
| `index_digest` | `sha256:90db9234...2e24a3` to `sha256:35660ccc...f7e` |
| Catalog entries, the four tools' manifests, versions, identities, code digests, `versions.yaml`, labels | **No change** (golden index compared path by path: only the two digests differ) |
| Evidence records | Valid: `capability.evidence check` passes (a test in the suite runs it) |
| `tests/test_tools_t001.py` | Unchanged |
| S-005 and S-009 records | S-005's currency paragraph is marked superseded by this change (the digests it quotes are history) |

## Result

- `tests/test_execute.py`: 49 tests OK on the Linux test host with the Docker socket (the container tests ran, none skipped); repeated four times without a failure after the fix described below.
- Whole suite on the Linux test host with the Docker socket, run alone with its output redirected to a file: 770 tests OK, 1 skipped (the graph-query contract test, on purpose). 721 before this feature plus these 49.

## Independent second review, checked by me

A second reviewer reviewed the slice as a second opinion. I reproduced each claim on the Linux test host before accepting it.

| Finding | Reproduced | Fix |
|---|---|---|
| A manifest with `stdinBytes` below the request envelope validates but makes every call fail (`PolicyError`, reported as an unexpected fault) | Yes (`stdinBytes: 0` gave `PolicyError: stdin must be bytes of at most 0`) | Schema minimum is now 64. An input too large for the declared limit is now an input rejection (`CallError ValidationFailed`, recorded `input_rejected`) before the sandbox is called, not a fault. |
| `outputBytes` up to 1 MiB is declarable, but S-009's `validate_output` caps results at 64 KiB, so a tool declaring 500000 can never return more than 65536 | Yes (a 100000-byte result gave `PostconditionFailed: exceeds 65536 bytes`) | Schema maximum is now 65536, so a manifest cannot promise more than S-009 allows. |
| `str.splitlines()` splits on U+2028, U+2029 and U+0085, which a valid JSON string may contain raw, so a correct result was rejected as a bad line (fail closed, but wrong) | Yes | The parser splits on a line feed only (a single trailing one is allowed); a stray extra line feed, an empty output and two lines are still refused. |
| `env` names can be declared but Stage A passes no values | Agreed; not a defect | Stated as a limit below. |
| Caller influence on policy, mounts, argv and stdin host path; parser crash resistance; the exit-gate controls; the S-012 mapping and the validate-before-run order | The second reviewer found nothing; I re-read each and agree | none |

Measured proofs, behaviour restored in scratch copies: `stdinBytes` minimum back to 0, 3 fail; `outputBytes` maximum back to 1 MiB, 2 fail; `splitlines()` back, 1 fails; the executor's stdin size check removed, 1 fails.

## Review findings

| Finding | Reproduced | Fix |
|---|---|---|
| The contract note still read as a proposal with the old bounds | Yes | The note opens with a superseded-as-status notice that points here for the final bounds and keeps the original text as the decision record. |
| `outputBytes` minimum 1 permits a value no valid result can fit in | Yes, through the real container sandbox: the smallest valid result line `{"ok":true,"output":""}` is 24 bytes with its newline; limits 22 and 23 truncate it, 24 and 25 pass | Schema minimum is now 64 (the same floor as `stdinBytes`, above the 24-byte minimum). A test measures the 22, 23, 24, 25 boundary through the sandbox and pins the floor above it. |

Measured proof: the `outputBytes` minimum back to 1, 3 tests fail.

## The exit-gate tests (real ContainerSandbox)

Fixture tools, declared in manifests and run through `run_tool`:

| Behaviour tested | Outcome |
|---|---|
| Control: read a file inside the root | succeeds |
| Control: the request carries `/work`, not the host path | yes |
| Read a host file outside the root | not visible: `FileNotFoundError`, the canary text never appears |
| Write to the declared read-only root | refused (`OSError`); the host file is not created |
| `filesystem: none` | the root is not visible at all |
| `filesystem: write-root` | writes the root, and only the root (`/etc` stays read-only) |
| Connect to a listener on the host's address | refused; the listener sees no connection |
| Control: the same tool code run by an explicit opt-out (`ProcessSandbox` with `unrestricted` and `allowed`) | reads the canary and connects, so the refusals are the policy's doing |
| Memory over the declared 64 MB | ends as `{"ok": false}` with closed text; no container left |
| Time over the declared limit | ends as `{"ok": false}` with closed text; no container left |
| Output over the declared 1024 bytes | ends as `{"ok": false}` with closed text; no container left |

## Measured proofs

Old or bad behaviour restored in scratch copies on the Linux test host; each fails the tests as expected:

| Change | Failures |
|---|---|
| Schema accepts `unrestricted` | 1 |
| No declaration falls back to a default policy instead of `SandboxUnavailable` | 4 |
| `validate_input` skipped | 8 |
| Result parse without the duplicate-key guard | 2 (after I added two cases where only the guard can catch it: the earlier cases were masked by the shape checks, so this was a real gap) |
| Host path put in the request instead of `/work` | 3 |
| Declared filesystem ignored (always `write-root`) | 4 |
| Network forced to `allowed` | 5 |
| `timeoutMs` minimum in the schema reduced to 1 | 2 |
| `validate_output` skipped | 2 |
| Recorder identifier checks removed (old behaviour: validate only inside the recorder) | the call_id and grant tests fail |

Not provable by a code change, stated for honesty: the `SandboxCleanupFailed` override in `_record` is belt and braces, because `outcome_for` already maps an unknown code to `error`/`Unexpected`; the test covers the behaviour, not the override.

## A flake found and fixed

Three early full runs of the container tests each failed one random test with "the tool ran past its time limit". The fixture tools declared the 5000 ms default and a container can take longer than that to start on this busy host. The container fixtures (except the one that tests the time limit) now declare 30000 ms; four consecutive full runs of the module then passed, as did the whole suite.

## Limits

- Stage A makes sandboxed execution possible and tested; it does not make the four built-in tools sandboxed by default. They declare no policy and the executor refuses them (a test pins this); Stage B lifts it and needs four version bumps, regenerated evidence and index, as set out in the contract note.
- A declared `filesystem` other than `unrestricted` needs the container sandbox; `ProcessSandbox` cannot enforce it and the executor then raises `SandboxUnavailable` (tested).
- The container image and the mount roots are deployment configuration of the sandbox, not manifest fields.
- No dispatcher exists yet (S-008): `run_tool` is a library function a dispatcher will call; it authorizes nothing (govsec).
- If a tool does not resolve (unknown name, tampered, revoked), no S-012 record is written, because there is no capability to name; the caller gets the registry error.
- The Docker tests are not part of a default run without the socket (as for S-011).
- `spec.sandbox.env` names can be declared, but Stage A passes no values (`run_tool` has no `env_values`): a declared name is permitted, not supplied. How values reach a tool (from govsec, not the caller) is a later decision.
- A tool that refuses the access reports it in its own result (the fixtures print `denied:...`); a real tool may crash instead, which the executor reports as a closed `ok: false`.
