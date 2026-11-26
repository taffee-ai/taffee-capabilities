# S-012 verification record

Status: implemented, tested and **accepted in review (2026-10-19), code and record.** Full platform integration stays open. It adds `capability/callrecord.py` and `tests/test_callrecord.py`.

Written for: the reviewer who will check this, and the auditors and dispatcher engineers who will use the records.

## How tests were run

Tests on the Linux test host from a fresh copy of the tree, in the test image.

## Result

- `tests/test_callrecord.py`: 57 tests OK. Full suite on the Linux test host with the Docker socket mounted: 652 tests OK.
- Historical, from before the S-011 and S-012 corrections: whole suite 636 tests OK, 19 skipped (the S-011 Docker tests). The 652-test run above, with the Docker socket mounted and nothing skipped, is the current result.

## Checks per requirement

| Requirement | Tests |
|---|---|
| CAP-REC-1 and 3 one record per call, fixed fields | exactly the specified fields; one line per record; time format; checked sequence 0 is `checked`, not `unchecked` |
| CAP-REC-2 no content | a secret in the input, the output, a tool error, a validation message and a registry message never reaches the file bytes; input and output are a digest and a byte count; a value that is not JSON text is refused before writing |
| CAP-REC-4 digest and revocation | the tool identity is used when given; `unchecked` is recorded as such |
| CAP-REC-5 outcomes | all six statuses from a result, a `CallError`, a registry error, a coded error, and an unexpected exception; a `code` attribute that is not a plain code is not trusted |
| CAP-REC-6 and 7 chain and verification | first record links to genesis; each links to the last; reopening continues the chain; an edited record, an edited record with its hash fixed, a removed middle record, an inserted record, reordered records, non-record lines, and a fork from two writers are all detected; a broken log is never appended to |
| CAP-REC-7 the stated limit | removing only the newest records is not detectable from the log alone (a test shows it), and `head()` exposes the change to an external holder |
| CAP-REC-8 query | by name and kind, version, digest (capability or code digest), outcome, and from a sequence; an unknown kind is refused; the query verifies first |
| CAP-REC-9 validation | bad `call_id`, `grant`, `duration_ms`, revocation sequence, capability, outcome, code digest and language each raise and write nothing; a never-resolved capability may have no digest or label |
| CAP-REC-10 keyed digests | with a deployment key the digests differ from plain, are stable, and differ between keys |
| CAP-REC-11 permissions | the file is created `0600` |
| End to end | against the real catalog: a Tested `grep` call records label, version, code digest, revocation sequence and grant that equal the Match's; a secret in the tool output is not in the log; a rejected input is recorded without running the tool; a revoked call is recorded `refused` / `Revoked` and found by the query; a stale feed is recorded `refused` / `RevocationStale` |

## Corrections after review

1. **Outcome codes were free text.** `record` accepted any string `outcome.code` and wrote it verbatim, so a caller could put a secret tool output or exception message on disk. Statuses now carry only codes from a closed table: `ok` and `tool_failed` carry none, `input_rejected` carries `ValidationFailed`, `output_rejected` carries `PostconditionFailed`, `refused` carries one of fourteen named error codes (`IndexError`, `SetupError`, `NotFound`, `BelowLabel`, `Ambiguous`, `InvalidRange`, `NoImplementation`, `IntegrityError`, `EntryError`, `SourceUnavailable`, `Revoked`, `RevocationStale`, `GrantDenied`, `SandboxUnavailable`), and `error` carries `Unexpected`. `record` refuses any other pair, and `outcome_for` turns a code it does not know into `error` / `Unexpected`, so an unknown code is never copied.
2. **A short or failed write could leave a partial line while `record` reported success.** The append now loops over short writes, treats a write that makes no progress as a failure, and on any failure truncates the log back to its previous size and raises without advancing the head. A failed `fsync` rolls the record back too. If the truncation itself also fails, `record` raises and the recorder **stops**: every later `record` call is refused until a new `CallRecorder` is opened, because its head can no longer be trusted. What the log then holds depends on the failure, and the limit is stated in full in correction 3.
3. **A rollback failure has a verification limit (review).** An earlier version of this record said `verify_log` detects the line left by a failed rollback. That holds only when the leftover is a *partial* line. If a *whole* line was written, `fsync` failed and the truncation failed too, `record` raises but the log holds a complete, valid record, and `verify_log` accepts it. The caller was told the write failed; the log says it happened. This cannot be detected from the log alone and is a stated limit, not a defect that can be closed. What the code does: the recorder stops (no append on a stale head), the error says the log may hold a whole record this call reported as failed, and a new `CallRecorder` over the same log reads that record and continues the chain correctly. A caller that sees this error must treat the call as possibly recorded.

Tests (13 new, 44 to 57): `OutcomeCodeTest` (eight made-up codes including secret text are refused and never reach disk; thirteen code and status mismatches refused; every valid pair accepted; the refusal set is exactly the fourteen codes; `outcome_for` never copies an unknown code; the two always agree) and `WriteIntegrityTest` (a wrapper that writes three bytes at a time still gives a whole, verifiable record after more than ten calls; a write that fails half way leaves the log the same size with the head unmoved and the recorder still usable; a write with no progress fails instead of hanging; a failing truncation says the log may hold a partial record and the partial line is then detected; a failed sync rolls back; a full write is one clean line).

Measured proofs, old behaviour restored in scratch copies on the Linux test host. For the first version of the module: storing output text in the record, 3 fail; not verifying the chain on open, 11 fail; not checking `prev`, 1 fails; file mode 0644, 1 fails; trusting any `code` attribute, 1 fails; not validating the grant, 6 fail. For the two review findings: code not checked against its status, 22 fail; `outcome_for` copying unknown codes, 4 fail; a single unchecked `os.write`, 3 fail; no rollback of a failed write, 3 fail.

## Found while building

The test helper `write` used `capability or CAP`, so an empty dict silently became a valid capability and wrote a record. Fixed in the helper (`CAP if capability is None else capability`); the library rejected the empty capability correctly.

## Notes

The chain is tamper-evident, not a signature: whoever can rewrite the whole file can rebuild a consistent chain, which is why the latest `(seq, hash)` should be held somewhere else. The record names no caller identity, only a grant reference, because identity belongs to govsec.

## Not covered

Who calls the recorder, shipping or signing records, and retention.

Measured proofs for correction 3, old behaviour restored in scratch copies on the Linux test host: the recorder not stopping after a failed rollback, 2 fail (the whole-line regression and the partial-line test); removing the refusal in `record` while still setting the flag, the same 2 fail. The new regression `test_a_whole_line_whose_sync_and_truncation_both_fail_is_a_known_limit` shows the limit itself: after a failed call `verify_log` accepts the complete line, the old recorder's head stays at the earlier value, later appends are refused, and a new `CallRecorder` continues the chain from the on-disk record.
