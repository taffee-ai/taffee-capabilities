# S-013 verification record

Status: implemented, tested and **accepted in review (2026-10-19), code and record.** Platform integration (a service endpoint) stays open.

Written for: the reviewer who will check this, and consumers who rely on `capctl verify`.

## How tests were run

Tests on the Linux test host from a fresh copy of the tree, in the test image (Python 3.12, Node 22, PyYAML 6.0.2, jsonschema 4.23.0, cryptography 43.0.3).

## Result

- `tests/test_capctl.py`: 33 tests OK.
- Whole suite: 389 tests OK when this record was written. Current: the whole suite is 662 tests OK on the Linux test host with the Docker socket mounted, none skipped, and `test_capctl` plus `test_catalog_export` together are 52 tests OK on the current tree (re-run 2026-10-19).

## What each CAP-VER requirement is checked by

| Requirement | Tests |
|---|---|
| CAP-VER-1 target and exact version | real tool, unknown name or version, usage errors for a bad target |
| CAP-VER-2 index and current | changed index byte (rest NOT RUN), missing index, manifest edited after the build (`current` fails, rest still runs), changed implementation (`current` and `digest` both fail) |
| CAP-VER-3 digest per language | all four tools, missing implementation folder (`EntryError`), changed code (`IntegrityError`, other tools unaffected), `graph-query` and external sources skip, agent with a library source |
| CAP-VER-4 evidence | changed cases, record for another identity, missing record, a valid record the index does not list, Validated with matching cases, Declared skips, agents skip |
| CAP-VER-5 revocation | no feed (fails), `--skip-revocation` (skips and says so), fresh feed, revoked identity, revoked name@version, a revocation of another tool or kind, unreachable feed, old checkpoint, wrong key, bad key, missing feed file |
| CAP-VER-6 label from the index | a manifest claiming `Approved` is still reported as `Tested` |
| CAP-VER-7 every check reported | no-feed case still reports evidence; bad key still reports evidence |
| CAP-VER-8 output and exit codes | 0 verified, 1 not verified, 2 for seven usage errors |
| CAP-VER-9 no code run | a fixture implementation that writes a marker file leaves no marker |

## Decisions that differ from, or add to, the design

- `current` is a separate check from `index`. As first written, "the index is current" sat inside `index` and gated everything. Because `library/` is an index input, a changed implementation would then have hidden its own per-language `digest` failure. Splitting them makes both visible. (Found while writing the tests.)
- A verified revocation is reported as revoked even when the feed is stale (the S-003 ruling), so `capctl` inherits that.
- The file feed (`FileFeed`) is a development transport, as USAGE-DESIGN §4 says. Its checkpoints expire in 30 s, so a file feed must be regenerated to verify.
- `capctl` is run as `python -m capability.capctl`; no console script is installed yet.

## Not covered

A network index, a signed index (OPS-01), running conformance or tests during verify, and a console-script install.
