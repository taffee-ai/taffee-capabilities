# S-003 verification record (client phase)

Status: client phase (M1 to M5) implemented, tested and **accepted in review**. Acceptance evidence was the measured scratch-copy runs and a full fresh-copy suite (474 tests at acceptance). The full cross-repo S-003 gate is not claimed: the feed service, the dispatcher's running-call record and pre-execute check, key rotation and an outage drill remain open.

Written for: the reviewers who agree each milestone, and the engineers who rely on the record.

## How tests were run

- Tests run on the Linux test host, from a fresh copy of the tree (the previous copy was removed first).
- Image: a test image (Python 3.12, Node 22) with the pins from `pyproject.toml`: PyYAML 6.0.2, jsonschema 4.23.0, cryptography 43.0.3.
- Command: `python -m unittest discover -s tests`, inside the image, with the tree mounted.

## M1: Entries and signatures

Code: `capability/revocation.py`. Tests: `tests/test_revocation.py`.

| Check | Result |
|---|---|
| Genesis head equals the SHA-256 of `taffee-revocation-genesis/1` | Pass |
| A valid entry verifies | Pass |
| One changed byte fails with `BadSignature` | Pass |
| A signature from another key fails with `BadSignature` | Pass |
| A digest target verifies | Pass |
| A name-only target is refused (`TargetInvalid`) | Pass |
| A target without `kind` is refused (`TargetInvalid`) | Pass |
| An unknown reason, `seq` 0, or an extra field is refused (`EntryShape`) | Pass |
| An absent or malformed configured key is `KeyMissing` | Pass |
| The entry hash covers `sig`, and is `sha256:` prefixed | Pass |
| Signed but malformed `prev`: `sha256:`, 63 or 65 hex, uppercase hex, trailing newline, wrong prefix, non-string | Refused (`EntryShape`) |
| Signed but malformed digest target: same cases | Refused (`TargetInvalid`) |
| Name must match `^[a-z][a-z0-9-]{1,63}$` and version canonical `X.Y.Z` (empty, uppercase, underscore, trailing newline, `1.0`, `01.0.0`, `^1.0.0`, non-string) | Refused (`TargetInvalid`) |
| A target with both a digest and a name@version | Refused (`TargetInvalid`) |
| `actor` and `reference`: empty, non-string, over 256 characters, control characters | Refused (`EntryShape`) |
| `time`: empty, date only, no `Z`, offset form, impossible month, non-string | Refused (`EntryShape`) |
| A boolean `seq` | Refused (`EntryShape`) |
| Feed line parser: a valid line parses and then verifies; bytes input; no final newline | Pass |
| Feed line parser: a changed byte in the line fails verification | `BadSignature` |
| Feed line parser: empty, malformed, non-object, two objects, `NaN`, `Infinity`, carriage return, invalid UTF-8, non-text | Refused (`LineInvalid`) |
| Feed line parser: duplicate keys | Refused (`LineInvalid`) |
| Feed line parser: a line over 64 KiB | Refused (`LineTooLong`) |

Result: `tests/test_revocation.py` alone, 27 tests OK (14 first round, 13 added for review). The whole suite, 214 tests OK on the Linux test host from a fresh copy; this includes the M2 draft tests (29), which are not claimed as M2 evidence. Before M1 the suite was 158 tests.

Grammar for targets matches the manifest schemas: `^[a-z][a-z0-9-]{1,63}$` for names and canonical `X.Y.Z` for versions. Patterns use full-string matching, so a trailing newline does not pass.

## M1 correction 2

- `parse_time` now requires the exact lexical form `[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}Z` (ASCII digits, full string) before `strptime`, so non-padded forms, a leading space or trailing newline, a lowercase `z`, full-width digits and impossible dates are refused. It serves entries (`EntryShape`) and checkpoints (`CheckpointShape`).
- `_check_text` refuses text that cannot be encoded as UTF-8 (a lone surrogate) with `EntryShape`. Before, `canonical_bytes` could raise a raw `UnicodeEncodeError` during signature verification.
- Tests: 3 added to `tests/test_revocation.py` (10 timestamp forms; 6 lone-surrogate values in `actor` and `reference`, signed over the surrogate-passed bytes the way an attacker could; and a JSON line with an escaped lone surrogate that parses and is then refused), and the timestamp forms added to the checkpoint shape test. `tests/test_revocation.py` alone: 30 tests OK. Whole suite: 448 tests OK on the Linux test host from a fresh copy.

## M2/M3 correction

- **A bad checkpoint faults the client.** `accept_checkpoint` now treats a verification failure (bad signature, malformed shape, a value that is not plain JSON) like a bad entry (CAP-REV-4): the client becomes faulted and stale, its verified head, deadline and last serial do not move, and only a later valid checkpoint recovers it. This also holds through `poll`, which records the error. A replay of an already accepted serial is still the documented non-fault exception, and the age and skew window refusals still only refuse.
- **Verified entries are private.** Every entry and checkpoint is first copied to plain data (`dict`, `list`, `str`, `int`, `float`, `bool`, `None`; anything else is refused with `EntryShape` or `CheckpointShape`) and the copy is what is verified and stored, so a caller who keeps the original cannot change a verified entry. `entries` is now a property returning a deep copy, and `CheckResult.entry` is a deep copy per check.
- **Tests:** in `test_revocation_failure.py`, a forged checkpoint faults a fresh client with head, deadline and serial unchanged; ten malformed checkpoints (including bytes and set values) do the same; recovery only by a later valid checkpoint; the same through `poll`; a replay is still not a fault. In `test_revocation_check.py`, mutating the original entry after ingest, the returned `entries`, or a `CheckResult.entry` cannot change a later check; each result has its own copy; entries holding sets, bytes, objects or non-string keys are refused. All revocation test modules together: 108 tests OK on the Linux test host from a fresh copy.

## M2/M3 correction 2

- `accept_checkpoint` now faults the client when a signed checkpoint is outside the age and skew window (`CheckpointTooOld`, `CheckpointFromFuture`), in the direct and `poll` paths. Before, a fresh client kept answering ok. The verified head, deadline and last serial do not move; a later in-window checkpoint that matches the chain recovers the client. A replay of an accepted serial is still the one refusal that does not fault. SPEC CAP-REV-7 and CAP-REV-12 say so.
- Tests (5 new, all in `AgeBudgetTest`): too old, exactly at the age bound (30 s faults, 29 s does not), from the future, both through `poll`, and the replay exception. With the two `faulted = True` lines removed in a scratch copy on the Linux test host, the 5 new tests fail. Whole suite: 474 tests OK on the Linux test host from a fresh copy.

## M2 to M5: implemented and tested (accepted in review; see the status line)

Whole suite on the Linux test host from a fresh copy: 253 tests OK (158 before S-003 + 27 M1 + 29 M2 + 8 M3 + 23 M4 + 8 M5).

| Milestone | Code | Tests | Count |
|---|---|---|---|
| M2 chain, sequence, checkpoints, age-adjusted deadline | `RevocationClient.ingest`, `accept_checkpoint`, `is_fresh` | `tests/test_revocation_chain.py` | 29 |
| M3 failure keeps the last verified state | same client | `tests/test_revocation_failure.py` | 8 |
| M4 `check(kind, name, version, digest)` | `RevocationClient.check` | `tests/test_revocation_check.py` | 11 |
| M4 `resolve` integration | `Registry(root, revocation=client)` | `tests/test_revocation_resolve.py` | 12 |
| M5 outage and recovery through the transport interface | `RevocationClient.poll` | `tests/test_revocation_outage.py` | 8 |

Choices made in this draft that the reviewer should rule on:

- Recovery from any fault needs an accepted checkpoint that matches the client's chain. A valid replacement entry alone moves the head but does not restore freshness.
- A replayed checkpoint (the serial already accepted) is ignored and does not fault the client. A lower serial, a head behind the client, or a head the client has not caught up to does fault it.
- A verified revocation is reported `revoked` even when the client is stale. Otherwise a client that is not fresh answers `stale`.
- In `resolve`, the revocation check runs after the digests verify. `IntegrityError` and `NoImplementation` come first. A tool presents its identity from the verified index; an agent or model presents its declaration digest.
- `poll` never raises. A transport error leaves the client to go stale at its deadline. A line that does not parse, or a verification failure, faults the client.

## Not covered by M1

Chain, sequence, checkpoints, serial, the age and skew window, the freshness deadline, `check(...)`, the `resolve` integration and outage handling are M2 to M5. They are written but not claimed here.
