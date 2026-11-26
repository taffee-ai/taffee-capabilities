# T-007 verification record: tool contract tests

Status: implemented, tested and **accepted in review (2026-10-19), code and evidence, after corrections.** New files only: `tests/test_tool_contract.py` and this record; the evidence suite `tests/test_tools_t001.py` is untouched.

Written for: the reviewer who will check this, and whoever adds the next tool.

## What it is

One contract suite that finds every tool with an implementation in `catalog/tool/*/manifest.yaml` and runs the same checks on it. A new tool is picked up automatically and cannot skip a check. Tools without an implementation (today only `graph-query`) are not in the contract; they are listed in `DECLARED_ONLY`, so adding a declared-only tool needs a deliberate edit (as the reviewer asked: graph-query stays Declared and gets no label).

| Check | What it enforces |
|---|---|
| Discovery | Every catalog tool is either implemented or in `DECLARED_ONLY`; a declared-only tool has no implementation and no `library/` folder; the four built-in tools are covered |
| Spec | The manifest validates; `inputSchema` is a valid object schema with `additionalProperties: false`; `timeoutMs` is a positive integer; `readOnly` and `idempotent` are booleans; `requires` is a list; at least one postcondition; every tool bounds its output with `max_bytes`; the four tools are read-only |
| Conformance coverage | At least five cases with unique ids, one success and one `ok:false` case, and every case passes with the manifest's postconditions applied |
| Process contract | Ten kinds of bad stdin (empty, invalid UTF-8, not JSON, array, null, empty object, root not a string, root not on disk, input missing, input not an object) each give exactly one JSON object line, exit 0, no traceback, `ok:false`. The shared result check is strict: `ok` must be a real boolean (1 and 0 are refused), an ok result needs a string `output`, an error result needs a non-empty string `error` of at most 2048 characters, stdout is decoded as strict UTF-8, NaN, Infinity and duplicate keys are refused. The check is itself tested (`ContractHelperTest`) |
| Input rejection | For each tool, inputs the schema forbids are generated (each required property missing, each property of the wrong type, one extra property, a non-object), each is checked to be really invalid, S-009's `validate_input` refuses it, and the tool process still answers cleanly if it is ever reached |
| Error text | An error result is at most 2048 characters and does not contain the host root path |
| Output bound | Every tool with a `max_bytes` postcondition must have a stress case in the suite (so a new bounded tool cannot skip it); the stress case's output is within the declared bound and passes `validate_output`; a result over the bound is refused by S-009 |

## Result

- `tests/test_tool_contract.py`: 22 tests OK on the Linux test host (Docker, the test image): 15 contract tests plus 7 tests of the shared result check.
- Whole suite on the Linux test host with the Docker socket mounted, output redirected to a file: 721 tests OK, 1 skipped (the graph-query contract test against a published Context schema, skipped on purpose). 699 before this feature plus these 22. The earlier figure of 714 in the first report predated the strict-result-check tests and is superseded.
- The contract found no defect in the four existing tools: they already answer every bad input cleanly, bound their output, and do not echo the host path.

## Measured proofs

Old or bad behaviour restored in scratch copies on the Linux test host; each fails the suite as expected:

| Change | Failures |
|---|---|
| glob: object check removed and the catch-all handler narrowed, so a JSON array or null crashes the tool | 2 (`json array`, `json null`) |
| grep manifest: `max_bytes` postcondition removed | 3 (bound check, stress, refusal) |
| glob error text includes the root path | 2 (two generated inputs) |
| glob output limit raised from 24 KiB to 240 KiB | 2 (stress, byte bound) |
| grep's stress case removed from the suite | 3 (missing case, two stress tests) |
| grep manifest `additionalProperties: true` | 1 |
| glob manifest `readOnly: false` | 1 |
| result check accepts `ok` as 1 or 0 (the old `assertIn(ok, (True, False))`) | 3 (`ok: 1`, `ok: 0`, `ok: 1.0`) |
| stdout decoded with `errors="replace"` instead of strict | 2 (two invalid UTF-8 lines) |
| `json.loads` without the non-finite and duplicate-key guards | 4 (NaN, Infinity, -Infinity, duplicate key) |

## Limits

- The stress cases are written per tool by hand. The suite fails when one is missing, but it cannot invent one; the next tool's author writes it.
- The suite checks the contract of the process and the manifest. It does not replace each tool's own conformance cases or the evidence suite.
- The invalid-input generator covers missing, wrong type and extra properties; it does not explore values within a type (for example bad patterns), which remain each tool's own cases.
- Harness note: the test harness uses one shared remote directory and log. I twice started a second job while a first was running; the jobs clobbered each other (one poll returned early on a stale `EXIT` line, and two logs were wrong). Both bad runs are discarded and not cited; the recorded runs were made alone, and the full run waited for its container to finish before the log was read.
