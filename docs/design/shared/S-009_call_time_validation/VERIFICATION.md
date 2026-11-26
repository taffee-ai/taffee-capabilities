# S-009 verification record

Status: implemented and tested, with corrections from review recorded below.

Written for: the reviewer who will check this, and the dispatcher engineers who call these checks.

## How tests were run

Tests on the Linux test host from a fresh copy of the tree, in the test image.

## Result

- `tests/test_calltime.py`: 52 tests OK.
- Whole suite: 636 tests OK, 19 skipped (the S-011 Docker tests).

## Checks per requirement

| Requirement | Tests |
|---|---|
| CAP-CALL-1 input checked against the schema | valid passes; wrong type, missing, extra and nested paths named; bool is not an integer; order stable; capped at 20 |
| CAP-CALL-2 no value in a message | a secret passed as five different values never appears; an unknown property name is reported but never its value (property names are structure) |
| CAP-CALL-3 not JSON or too large | a set, a NaN, and an input over 64 KiB are refused before the schema |
| CAP-CALL-4 tool failures pass through | `ok: false` returns unchanged |
| CAP-CALL-5 malformed results | ten malformed shapes are `PostconditionFailed`; an output over 64 KiB fails |
| CAP-CALL-6 and 7 postconditions | `max_bytes` counts bytes, not characters; `each_line_matches` needs the whole line; `allow_empty`; a 4 KiB+ line fails without being matched (a catastrophic pattern is not run); several failures reported together; line numbers named, content never |
| CAP-CALL-8 manifest validation | ten bad postcondition shapes, an uncompilable pattern (with its index), and an invalid `inputSchema` are refused |
| CAP-CALL-9 conformance applies them | a case whose real output breaks a postcondition fails the case; a tool failure case is not judged; the four real tools pass every case with their postconditions applied; a fixture whose postcondition cannot hold refuses the build |
| CAP-CALL-10 pure | no I/O in the module |
| CAP-CALL-11 the four tools | each declares postconditions at 1.2.0; sample good and bad outputs pass and fail as expected; the Match carries the postconditions |

## Found while building

- `jsonschema` reports one error per missing property; the first version expanded each one and listed missing properties twice. Fixed by de-duplicating.
- The S-001 fixture tool predates the tool contract (`{"ok", "output"}`), so applying postconditions to it was correctly refused. The test uses a contract-shaped tool.

## Measured proofs for the first version (old behaviour restored in scratch copies on the Linux test host)

Each variant removes one behaviour in a scratch copy and runs `tests/test_calltime.py` (37 tests).

| Variant | What was removed | Result |
|---|---|---|
| J | `validate_output` never applies the postconditions | 22 fail (15 failures, 7 errors): every bad real-tool output passes, the conformance and Validated-label tests fail |
| K | a type error echoes the input value into its message | 5 fail: the type message, the error text, and the three no-leak cases |
| L | the conformance runner does not apply postconditions | 2 fail: a case whose output breaks one, and a fixture refusing Validated |
| M | missing required properties not de-duplicated | 1 fails: each missing property is named once |
| N | the 4 KiB line bound removed, so a long line is matched | the test did not finish within 240 s (catastrophic backtracking on the `(a+)+b` pattern). Note this only showed the bound stops a 4,097-character hang; it did not show runtime was bounded, which the reviewer rightly pointed out. RE2 (below) is the actual bound |

These were run after the module was handed over, and the numbers are from that run.

## Corrections after review

1. **Exact result shape.** `validate_output` accepts exactly `{"ok": true, "output": <string>}` or `{"ok": false, "error": <string>}`. An extra field, or a field of the other form (`ok: true` with `error`, `ok: false` with `output`), is `PostconditionFailed` with the field named and its value never echoed. Four new test methods; with the check removed in a scratch copy, 8 of them fail.
2. **Regex cost.** My claim that the 4096-byte line limit bounds runtime was unsupported and is retracted. A backtracking pattern can hang on a much shorter line. Postconditions are now matched with RE2 (`google-re2==1.1.20251105`, pinned in `pyproject.toml` and the test images): linear time for any pattern it accepts; look-around and backreferences are refused when the manifest is validated; Python's `re` is not used for postconditions or manifest acceptance. Measured on the Linux test host image before adopting it: `(a+)+b` on 100,000 characters returns in 0.3 ms. The 4096-byte limit remains as a size bound only.
3. **glob 1.2.0 to 1.3.0.** glob's postcondition used a lookahead, so it was rewritten: `(?!/).+` became `[^/].*`, the alternative `\[truncated: output exceeded [0-9]+ bytes\]` unchanged. git-log, grep and unawaited-async compile under RE2 as they were and stay at 1.2.0. glob's identity moved from `sha256:366512f4c4f522446fa1ccf9f115e6d3f651bf0b226d90c6c15bc788a625795b` (1.2.0) to `sha256:f098fb93e2152e5e680ba479a58cdb44a9818c4b141e699e907438bfb292d7e5` (1.3.0), both in `catalog/tool/glob/versions.yaml`. glob's evidence was regenerated for 1.3.0 only (`capability.evidence write --tool glob`; the new `--tool` filter keeps the other three records untouched); the golden index (`index_digest` `sha256:90db923403d41a2f421de8ac73190919df787fb86729677b61e81a376d2e24a3`) and the umbrella catalog were regenerated.
4. **Tests added (11, taking the module from 41 to 52; the four exact-shape tests in item 1 took it from 37 to 41):** `LinearTimeTest` (a catastrophic pattern returns in under a second on lines of 30, 200, 1000 and 3000 characters; five other classic backtracking patterns; matching is done by RE2 and the module does not import `re`; a match needs the whole line; look-around and backreferences refused at validation; an uncompilable pattern fails closed at call time; good patterns accepted) and `GlobRewriteEquivalenceTest` (the rewrite is the documented one; old and new agree on 17 representative lines and on all 3,906 strings of length 0 to 5 over `ab/ .`, with the old pattern run by Python `re` as a test-only oracle; good and bad real outputs). A shared `TOOL_VERSIONS` table in `tests/fixture_support.py` replaces the scattered version literals.
5. **Measured proofs, with the old behaviour restored in scratch copies on the Linux test host.** Python `re` back for matching: the catastrophic-pattern tests did not finish within 150 s (a hang, not a clean failure). glob's lookahead back in its manifest: 9 fail (3 failures, 6 errors). Manifest validation back on Python `re`: 6 fail (look-around and backreferences accepted).

## The evidence command's `--tool` filter

The RE2 and glob change added a `--tool NAME` filter (repeatable) to `capability.evidence write` and `check`, so only glob's record was regenerated. It is a cross-feature edit to T-001's `capability/evidence.py`. Review found two gaps, both accepted:

1. **A misspelling selected nothing and exited 0**, so a regeneration could silently do nothing. An unknown or ineligible name (including one that has no implementation or no `conformance.json`, and an empty name) now exits 2 with `no evidence-eligible tool named 'x'` and the list of eligible tools, for both `write` and `check`. It does so before the test suite is run, and a known name beside an unknown one writes nothing at all.
2. **No test exercised the filter.** `EvidenceToolFilterTest` (10 tests, `tests/test_evidence.py` from 47 to 57) uses a throw-away tree with three fixture tools: two eligible, one with no cases. It checks that `write --tool` regenerates the named tool and leaves the other record identical file by file (a digest of every file before and after), that the filter may be repeated, that no filter covers every eligible tool, that `check --tool` reports only the named tool while an unfiltered check still reports the missing one, that the three kinds of bad name fail and write nothing, and, on a copy of the real catalog, that `check --tool glob` prints exactly `glob@1.3.0: evidence holds` and `--tool globb` is refused with the four eligible names.

Measured proofs, old behaviour restored in scratch copies on the Linux test host: unknown names selecting nothing: 8 fail; the filter ignored: 3 fail.

Facts about the real records, true on disk: the 1.2.0 records for git-log, grep and unawaited-async were not regenerated when glob moved to 1.3.0. Their `record:` evidence digests in the golden index are the same before and after (`08900e08`, `3899a8eb`, `0f8bc251`).

## What it changed outside its own files

Disclosed in review: `capability/conformance.py` (`run_cases(..., spec=)`), `capability/labels.py`, `capability/manifest.py` and `schemas/manifest.tool.schema.json`; the four tool manifests (1.2.0), their ledgers and evidence; the golden index and the umbrella catalog; and the real-tool version in five test files. Review of T-001, S-013 and S-005 was carried out against this 1.2.0 snapshot.

## Not covered

Where the orchestrator calls these checks, and runtime limits (S-011).
