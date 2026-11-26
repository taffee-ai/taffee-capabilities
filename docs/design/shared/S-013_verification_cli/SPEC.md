# S-013: Verification CLI (`capctl verify`)

Status: implemented, tested (see [VERIFICATION.md](VERIFICATION.md)) and accepted in review on 2026-10-19, code and record; platform integration stays open. Depends on S-001, S-002, S-003 and T-001. Roadmap: [../../../roadmap.md](../../../roadmap.md). Design source: USAGE-DESIGN §3.8.

Written for: the engineers and consumers who need a yes or no on whether what they hold is what was published.

## 1. Purpose

A consumer runs one command and learns whether a capability they received is the one the catalog says, with the reasons. It re-checks everything it can from local files and runs no capability code.

## 2. Requirements

| ID | Requirement |
|---|---|
| CAP-VER-1 | `capctl verify <name>@<version> [--kind K] [--root R]` verifies one capability version. `--root` defaults to the current directory. The version is exact. |
| CAP-VER-2 | **index:** the index parses, matches the bundled schema, is canonical, and its `index_digest` verifies (the S-002 loader). **current:** `inputs_digest` recomputed from the tree now equals the recorded one, so a catalog or implementation edited after the build fails here. `current` does not gate the other checks, so a changed implementation shows both `current` and the `digest` failure for its language. |
| CAP-VER-3 | **digest:** for a tool, every indexed implementation is resolved with S-002 (entry check, then recomputed `manifest_digest` and `code_digest`). One line per language. For an agent or model with a library or generated source, the same. An external source is `SKIP` with the reason, because its bytes are not held. |
| CAP-VER-4 | **evidence:** for a tool at Tested, the evidence record on disk holds for the tool's current identity and cases (the T-001 check), and its fingerprint and the cases digest equal the ones recorded in the index entry. At Validated, the cases on disk must equal the digest in the entry. At Declared, `SKIP`. Agents and models: `SKIP` (no evidence is defined for them yet). The check never re-runs tests or cases. |
| CAP-VER-5 | **revocation:** with `--revocation-key` and `--feed-file` and `--feed-checkpoint`, the S-003 client polls the feed and answers for this capability. Revoked, not fresh, a bad feed, or a bad key are `FAIL`. With no feed configured the check is `FAIL` ("no revocation feed configured"), unless `--skip-revocation` is given, which makes it `SKIP` and says so on the line. Silence never passes. |
| CAP-VER-6 | **label:** printed from the index, never from the manifest. |
| CAP-VER-7 | Every check is reported even when an earlier one fails, except that a failed `index` check makes the others `NOT RUN`, and an entry that is not found makes the entry-level checks `NOT RUN`. Nothing read from a bad index can be trusted. |
| CAP-VER-8 | Output is one line per check (`name`, status, detail) and a last line `VERIFIED` or `NOT VERIFIED`. Exit 0 when verified (no `FAIL`), 1 when not, 2 for a usage error. `SKIP` does not fail, and the reason is printed. |
| CAP-VER-9 | Verify runs no capability code. |

## 3. Test plan

| Test | Assertion |
|---|---|
| Real tested tool | `grep@1.2.0` on the real catalog verifies with index, digest, evidence and label lines; revocation per the flags |
| Stale tree | Editing a manifest after the build fails `current` and the other checks still run |
| Tampered index | A changed index byte fails `index` and marks the rest NOT RUN |
| Changed code | A changed byte in an implementation fails `current` and the `digest` check for that language only |
| Evidence | A changed tool at Tested fails evidence; changed cases fail; a record for another identity fails; Validated with matching cases passes; Declared skips |
| Index and evidence disagree | A fingerprint in the entry that the record does not carry fails |
| Revocation | No feed fails; `--skip-revocation` skips and says so; a fresh feed with no revocation passes; a revoked target fails; a stale feed fails; a wrong key fails; an unreachable feed fails |
| Unknown or ambiguous | A name or version that is not indexed fails with the registry code; a name used by two kinds needs `--kind` |
| Exit codes | 0, 1 and 2 |
| No execution | A fixture implementation that writes a marker file when run leaves no marker |

## 4. Out of scope

Running conformance cases or tests (that is what produced the evidence), fetching an index over a network, and a signed index (OPS-01).
