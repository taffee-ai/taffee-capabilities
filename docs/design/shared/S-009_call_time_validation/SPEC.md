# S-009: Call-time validation

Status: implemented and tested (see [VERIFICATION.md](VERIFICATION.md)). Depends on S-002. Also closes T-001's postcondition item (CAP-T-10) and the contract-suite intent of T-007. Roadmap: [../../../roadmap.md](../../../roadmap.md).

Written for: the dispatcher engineers who call tools, and the authors who declare them.

## 1. Purpose

A tool's manifest declares what it accepts and what its output must look like. The dispatcher must be able to check both around every call, with errors that name the field or the check and never echo the data. This feature gives the dispatcher those checks as pure functions, and gives authors a language-neutral way to declare the output check.

## 2. Requirements

| ID | Requirement |
|---|---|
| CAP-CALL-1 | `validate_input(spec, value)` checks `value` against `spec.inputSchema` (JSON Schema 2020-12) before execution. Any problem raises `CallError("ValidationFailed")` listing every problem as a field path and a message, in a stable order, at most 20. |
| CAP-CALL-2 | Problem messages never contain an input value. They name the path and the keyword (`type: expected string`, `required property is missing`, `property is not allowed`), so an error cannot leak a secret the caller passed as a value. Property names are reported, because they are structure and the caller needs them to fix a typo. |
| CAP-CALL-3 | An input that is not JSON-compatible, or is over 64 KiB serialised, is `ValidationFailed` before the schema is applied. |
| CAP-CALL-4 | A tool's result is exactly the tool contract object: `{"ok": true, "output": <string>}` or `{"ok": false, "error": <string>}`, and nothing else. A field outside the matching form, including a field of the other form (`ok: true` with `error`, `ok: false` with `output`), is refused and named, never echoed. `validate_output(spec, result)` returns a tool failure (`ok: false`) in the exact form unchanged, because a tool saying "no" is a result, not a violation. |
| CAP-CALL-5 | A result that is not that shape (including any extra or mixed field), or whose output is not a string or is over 64 KiB, raises `CallError("PostconditionFailed")`. |
| CAP-CALL-6 | `spec.postconditions` is an optional list of declarative checks on `output`, applied in order, all failures reported: `max_bytes` (`value`), and `each_line_matches` (`pattern`, optional `allow_empty`, default true). The whole line must match. Patterns are matched with RE2 (`google-re2`, pinned), whose matching time is linear in the line length for any pattern it accepts, so no pattern can hang on a short line. Python's `re` is not used for postconditions. A line over 4096 bytes fails without being matched; that is a size bound for memory and for reporting, **not** the runtime bound (an earlier draft claimed it was; review showed a backtracking pattern can hang well under it). |
| CAP-CALL-7 | A postcondition problem names its index, its type, and a line number. It never contains output text. |
| CAP-CALL-8 | The manifest validator refuses an unknown postcondition type, a pattern RE2 cannot compile (which includes look-around and backreferences, the constructs that have no linear-time guarantee), more than 16 postconditions, and an `inputSchema` that is not a valid JSON Schema. A pattern that nevertheless reaches call time uncompilable fails closed as `PostconditionFailed`. |
| CAP-CALL-9 | The conformance runner applies a tool's postconditions to every case that returns `ok: true`. A tool is not Validated unless its declared postconditions hold on every case output. |
| CAP-CALL-10 | The checks are pure: no I/O, no capability code run. |
| CAP-CALL-11 | The four built-in tools declare postconditions. Declaring them changes each tool's contract, so each moves from 1.1.0 to 1.2.0. |

## 3. Out of scope

Timeouts, memory and output-size enforcement during execution (S-011), caching (S-010), and call records (S-012). Where the orchestrator invokes these checks is the orchestrator's design.

## 4. Test plan

| Test | Assertion |
|---|---|
| Input | valid passes; wrong type, missing, extra and nested paths are named; order is stable; capped at 20; a secret value is never in a message; bool is not an integer; too large and not-JSON are refused |
| Result shape | `ok: false` passes through; five malformed shapes are `PostconditionFailed` |
| Postconditions | `max_bytes` and `each_line_matches` pass and fail; line numbers named and no content; `allow_empty`; a long line; several failures reported together; none declared means pass; an oversize output fails |
| Manifest | unknown type, bad pattern, too many, extra keys, and an invalid `inputSchema` are refused |
| Real tools | each declares postconditions at 1.2.0; sample good outputs pass and sample bad ones fail (an absolute path from `glob`, a non-commit line from `git-log`, a line with no number from `grep`, gibberish from `unawaited-async`) |
| Conformance | a case whose real output violates a declared postcondition fails the case; the four real tools pass all their cases with postconditions applied |
| Match | `resolve` returns the postconditions in the Match's `spec` |
