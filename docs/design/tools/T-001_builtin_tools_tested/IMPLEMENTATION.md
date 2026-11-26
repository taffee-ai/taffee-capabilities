# Implementation record: T-001 built-in tools (slices 1 to 4)

Status: slices 1 to 4 done and tested. The D8 to D14 slice (labels, conformance, evidence, with corrections) was **accepted in review** on the 1.2.0 snapshot (599 tests). The four tools are **Tested**, with committed evidence whose fingerprints reproduce on the same host. `graph-query` and migration parity are open (§4); the full T-001 exit is open.

Written for: the engineers who continue T-001, and the reviewer who will check it.

## 1. What changed from the SPEC, and why

The SPEC (written before the language decisions) assumed TypeScript, a `library/shared/` helper folder, and a `spec.ts` per tool. The platform has since decided that tools are language-neutral and that an implementation's `code_digest` covers its own folder only. These decisions follow from that. They are Claude's defaults, for the owner to confirm or redirect.

| # | Decision | Reason |
|---|---|---|
| D1 | Tools are Python 3.12, standard library only, one `main.py` per tool at `library/<name>/python/`. | Python is the language default. A tool with no dependencies needs no install step. |
| D2 | Contract: stdin is `{"root": "<abs dir>", "input": {...}}`; stdout is one JSON line, `{"ok": true, "output": "..."}` or `{"ok": false, "error": "..."}`; exit 0 for every result. | Language-neutral, and matches the S-001 conformance runner (stdin in, JSON out). The root is always passed in (CAP-T-3). |
| D3 | No `library/shared/`. The small shared part (request reading, path confinement, bounded subprocess) is duplicated in each tool, generated from one prelude. | A folder's `code_digest` covers only that folder. Code imported from outside would be outside the digest, which is an integrity gap. |
| D4 | The tools still call `grep`, `find` and `git` as subprocesses with argv lists, never a shell, as the prototype does. | Behaviour parity. |
| D5 | Tools bound their own output (24 KiB, then a truncation marker) and their own deadline (4 s, below the 5 s manifest timeout). | CAP-T-7 and CAP-T-9. The runner kills at 64 KiB / 5 s, so the tool must fail first and say why. |
| D6 | Version 1.1.0 for the four tools, because adding an implementation changes the tool identity and a published version is immutable. The ledger keeps the 1.0.0 entry. | S-001 publication gate. |
| D7 | `graph-query` is deferred. | It needs a running Context service to test its contract. |
| D8 | Label policy v2. **Validated** (tools) = at least one implementation, a valid `library/<name>/conformance.json`, and every implementation passes every case. The digest of the cases is recorded in the entry's `evidence` as `conformance:<digest>`. | The label names the exact cases it was earned on. A change to the cases changes the evidence and the index digest. |
| D9 | A tool that has cases and fails them, or whose cases cannot be run (bad file, missing toolchain, missing git), **refuses the build**. It is never silently downgraded to Declared. | A label that drops quietly is easy to miss, and a host without a toolchain would otherwise produce a different index. |
| D10 | A case with a `files` key describes a throw-away repository (files, links, outside files, git commits) built in a temporary directory; the tool gets `{"root", "input"}`. Without `files`, the input is fed unchanged (the S-001 fixture format). | Tools need a repository, and the cases must not touch anything else. |
| D11 | **Tested** (tools) = Validated, and an evidence record at `evidence/tools/<name>/<version>/summary.json` (+ `output.txt`) that names this tool's current identity, the current cases digest and policy version, shows passed at least 1 with failed, errored and skipped all 0, whose fingerprint recomputes, whose `evidence_digest` recomputes over the counts, suite and log digest, whose `output.txt` matches `log_digest`, and whose named suite file exists and matches its digest now. The entry's `evidence` adds `tests:<fingerprint>` and `record:<evidence_digest>`. Evidence that does not hold leaves the tool Validated. (Corrected after review.) | SPEC §5.4 and §6 ("Evidence tamper"). |
| D12 | The fingerprint covers the identity, cases digest, suite file digest, policy version and environment (Python, OS, architecture, and the versions of git, grep and find). Results are not in it. A separate `evidence_digest` binds the fingerprint, the suite, the result counts and the log digest; it differs on every run, so it makes results and log tamper-evident but is not a reproducibility check. | Output depends on git, grep and find versions (SPEC §9). Two records with the same fingerprint are the same claim. The log carries timings, so binding it into the fingerprint would stop it reproducing. |
| D13 | The label check does not re-run the tests. It checks that the record is consistent and is for the tool as it is now. A record is not a signature. | Running tests inside an index build would be slow and host-dependent. Reviewers read the record and its log. |
| D14 | The evidence suite runs in a fresh child process (`python -c`, PATH-only environment, 30-minute timeout). | An in-process run shared the module cache: a second suite with the same module name was loaded from the first one's directory (found by a test). |

## 2. Deliberate differences from the prototype

| Tool | Difference | Why |
|---|---|---|
| all | Strict input validation: wrong types, empty strings and missing fields are `ok: false`. | The prototype silently defaulted (`n` of the wrong type became 3). |
| all | Output bounded with a marker. | CAP-T-7. |
| `git-log` | `n` must be an integer from 1 to 100. The test-only `timeout: true` hang is not ported. | A back door that makes a production tool hang is not carried over. |
| `git-log` | An empty repository returns empty output, not an error. | CAP-T-6. |
| `glob` | Results are sorted. | Stable output, so it can be cached and compared. |
| `grep` | Output is unchanged, so at the repository root it prints `./a.txt:1:text`. | Parity with the prototype. |

## 3. Evidence so far

- `tests/test_tools_t001.py`: 46 tests, through the real conformance runner. They cover the contract (bad stdin, bad root, wrong types), path confinement (parent, absolute, escaping symlink, missing, symlinked files not followed), each tool's behaviour, bounded output with the marker, deadline kill with the child gone, endless output cut off, and self-containment.
- Catalog: four manifests at 1.1.0 with a `python` implementation; ledger identities added; the real index builds in publication mode; golden index regenerated (`index_digest` `sha256:5cabd611547c2f35bda8020d4225116d84b56687a5268b22d93248a3ef89076c`).
- Earlier tests that described the old state of the real catalog were updated to the new state (`test_pins`, `test_registry`, `test_resolve_m4`, `test_languages_m3`). `graph-query` is still `NoImplementation`. A tampered real tool is an `IntegrityError`.
- Slice 3: `capability/conformance.py` gained `load_cases`, `cases_digest`, `build_repository` and `run_cases`; `capability/labels.py` is policy v2 (`tool_label`); the index records `policy_version` 2 and the cases digest as evidence; `schemas/index.schema.json` accepts any integer policy version of 1 or more. 29 cases across the four tools (`library/<name>/conformance.json`). `tests/test_conformance_cases.py` has 23 tests, including a broken tool that fails its cases, a tool that ignores confinement and fails the escape cases, cases that fail or are invalid refusing the build, and changed cases changing the evidence.
- Slice 4: `capability/evidence.py` (`run_suite`, `write_record`, `check_record`, `fingerprint`, and a `capability-evidence write|check` command); `tool_label` awards Tested; `evidence/` is an index input. Evidence for the four tools was generated on the Linux test host by running `tests/test_tools_t001.py` (46 tests, 0 failed, 0 errored, 0 skipped) and committed. `tests/test_evidence.py` has 34 tests: suite counting, fingerprint, every way a record can fail, label fallback when the tool or cases change, and the committed records.
- Reproduction (exit gate): the evidence was regenerated on the same Linux test host from a copy with no `evidence/` folder, and the four fingerprints were identical to the committed ones.
- Cost: building the real catalog now runs the cases, so a full suite takes about 64 s on the Linux test host (it was about 19 s).
- Whole suite: 356 tests OK on the Linux test host from a fresh copy (the test image); about 75 s.

## 4. Open work

| Item | Notes |
|---|---|
| Postconditions (CAP-T-10) | The manifest has no postcondition field. Belongs to S-009 call-time validation. Not built. |
| Manifest/spec drift test (CAP-T-2) | Not applicable until a tool carries its own spec. Decide with the label work. |
| `graph-query` (CAP-T-11, CAP-T-12) | Needs the Context service. |
| Migration parity against the prototype | Requires running the prototype tools on the same fixtures. Not done. |
| Review | None yet. |

## 5. Exit gate (SPEC §7), item by item

| Item | State |
|---|---|
| All tests in SPEC §4 and §6 pass on the remote host | Partly. §4 is covered except CAP-T-10 (postconditions, S-009), CAP-T-11 and CAP-T-12 (`graph-query`). §6 migration parity and postcondition parity are not done. |
| Each of the five tools reaches Tested with evidence | Four of five. `graph-query` is Declared. |
| Fingerprint check recorded as a same-host fresh-copy reproduction on the Linux test host | Done (§3). |
| Change of capability digest recorded as an explicit version change | Done: 1.0.0 to 1.1.0, both identities in each `versions.yaml`. |
| The prototype's own test suite still passes unchanged | The prototype was not modified. Its suite was not run. |

T-001 is therefore **not closed**. Closing needs `graph-query`, migration parity, and the postcondition decision.

## 6. Correction after review (Tested evidence binding)

Two gaps in `capability/evidence.py`, both accepted:

1. `check_record` recomputed the fingerprint from the suite digest written in the record, but never compared that digest with the suite as it is now. A changed suite kept Tested on a stale record. It now requires the suite file named by the record to exist (at a safe relative path) and to match its digest.
2. The fingerprint excludes the result counts and the log, and the log was never read. Editing a passing count or replacing `output.txt` left the label Tested. The record now carries `log_digest` and `evidence_digest`; `check_record` recomputes `evidence_digest` over the fingerprint, suite, counts and log digest, and compares `output.txt` with `log_digest`.

Consequences: the index's `evidence` list gains `record:<evidence_digest>` for Tested tools; `capctl verify` compares both the fingerprint and the evidence digest with the record; every test tree that copies the committed evidence also copies `tests/test_tools_t001.py`, because the label now depends on that file; the 1.2.0 evidence records, the golden index and `taffee-core/catalog/capabilities.yaml` were regenerated (the fingerprints are unchanged, as designed). Old records without the new fields (the 1.1.0 records, history only) no longer hold.

Evidence: 11 new tests in `tests/test_evidence.py` (edited counts with the fingerprint intact, a replaced and a missing log, an edited log digest, malformed digests, a changed and a missing suite, three unsafe suite paths, the evidence digest following the log while the fingerprint does not, and two label-level fall-backs to Validated). With the new checks removed in a scratch copy on the Linux test host, 12 of the new tests fail (10 as errors, because the old check returned no problem where one was expected). Whole suite: 469 tests OK on the Linux test host from a fresh copy.

Not changed, and stated so it is not mistaken for a fix: a record is still not a signature. Whoever can edit the repository can write a fully consistent record. `test_the_record_is_not_a_signature_so_a_fully_consistent_forgery_holds` documents that. Reviewer provenance remains a separate gate.

## 7. Correction after review (confine and pin the suite path)

`check_record` called the recorded suite path "safe" after rejecting an absolute path and `..`, but a symlink under `tests/` could still resolve outside the root, and any relative file could stand in for the approved suite. Accepted, and changed in `capability/evidence.py` only (the evidence records, the index and the catalog are unchanged):

1. The recorded suite path must be on an allow-list, `APPROVED_SUITES`, which is the single suite `tests/test_tools_t001.py`. A record that names any other file, even one that exists and matches its own digest, cannot earn Tested. The list lives in code, not in `policy/labels.yaml`, so adding it did not change the index inputs.
2. Every component of the path must be a plain directory or file, not a link, and the resolved file must stay inside the root.

Tests (9 new, in `tests/test_evidence.py`): a different in-repo file; an allow-list of two; a symlinked suite whose bytes match; a symlink pointing outside the root; a symlinked parent directory; a suite that is a directory; the shipped list is exactly the one real suite; every current-format committed record names an approved suite; and the label-level fall-back to Validated for a symlinked suite and a file off the list. Tests that use a fixture suite patch the allow-list explicitly. With the new check removed in a scratch copy on the Linux test host, 6 of them fail (5 errors, 1 failure); the directory case passes either way, as it should.

## 8. Correction after review (D8 to D10)

1. **Present but invalid cases file** (`capability/labels.py`). `tool_label` returned Declared whenever `conformance.json` was not a regular file, so a directory or a broken link downgraded silently, against D9. It now separates absent (`lexists` is false: Declared) from present: anything present that is not a regular, non-linked file refuses the build with `LabelError` ("present but is not a regular file"). A symlink to a valid file is refused as well, so the cases cannot be swapped for a file elsewhere.
2. **Fixture links written through** (`capability/conformance.py`). `build_repository` created parents with `mkdir(parents=True)`, which follows a link made earlier in the same case, so `links["x"]` pointing outside followed by `links["x/new"]` wrote outside the temporary repository. A new `_place` creates parents one component at a time, refuses a path through a link or a file, refuses a final component that already exists, and checks the result resolves inside the base. It serves files, links and the outside area.

Tests, 9 new: in `test_conformance_cases.py`, a directory, a broken link and a link to a valid file each refuse the build, and an absent file is still Declared; a link followed by an entry through it, a link followed by a deeper entry through it (both with a scratch directory outside the temporary base asserted empty afterwards), a link whose name is an existing directory or file, a path through an existing file, a plain link and ordinary nested files still working, and a whole case that tries it failing the case with nothing written outside. With each fix removed in a scratch copy on the Linux test host, the tests fail: 3 failures for the cases-file fix, 3 failures and 3 errors for the fixture fix. Whole suite: 594 tests OK, 19 skipped (the Docker tests, with their reason).

## 9. Follow-up after review (two narrow cases)

1. **Cases present, no implementation** (`capability/labels.py`). `tool_label` returned Declared as soon as `implementations` was empty, so a stray `conformance.json` beside a tool with nothing to run it on was ignored. Now present cases with no implementation refuse the build (`LabelError`, "present but the tool has no implementation to run it"), per D9. Absent cases with no implementation stays Declared, as `graph-query` is.
2. **A path with no components** (`capability/conformance.py`). `Path('.')` has no `..` and is not absolute, so `_safe_relative` accepted it and `_place` raised a raw `IndexError` on `parts[-1]`. `_safe_relative` now refuses a name with no components ("must name a path inside the repository, not the repository itself"), and `_place` guards it as well. `'.'`, `'./'` and `'./.'` are refused for files, links and the outside area.

Tests, 5 new test methods (the dot test runs nine times as subtests) in `test_conformance_cases.py`: present cases with no implementation for a valid file, a directory, a broken link and a link to a valid file; no implementation and no cases is still Declared; the dot forms refused for all three keys; a dot failing the case through `run_cases`; and a dot failing the index build with the typed explanation. Measured proof, with each fix removed in a scratch copy on the Linux test host: the guard fix gives 1 failure (the valid-file case; the directory and link cases are caught by the earlier present-but-not-regular check either way), and the dot fix gives 11 errors (the raw `IndexError`). Focused: `test_conformance_cases.py` 38 tests OK. Whole suite: 599 tests OK, 19 skipped (the S-011 Docker tests, with their reason).

An earlier attempt at this proof produced no result because its patch script had a quoting error; both scratch copies ran unpatched. It is not counted, and the proof above was run again from a script file.

## 10. glob moves from 1.2.0 to 1.3.0 (S-009 regex cost, authorized in review)

glob's postcondition pattern used a lookahead, which RE2 refuses, so it was rewritten (`(?!/).+` to `[^/].*`) and glob became **1.3.0**. git-log, grep and unawaited-async are unchanged at 1.2.0. This touched the accepted T-001 snapshot, with the reviewer's GO.

| | version | identity |
|---|---|---|
| glob before | 1.2.0 | `sha256:366512f4c4f522446fa1ccf9f115e6d3f651bf0b226d90c6c15bc788a625795b` |
| glob now | 1.3.0 | `sha256:f098fb93e2152e5e680ba479a58cdb44a9818c4b141e699e907438bfb292d7e5` |

Both are in `catalog/tool/glob/versions.yaml`; the 1.2.0 entry stays as history. Evidence for 1.3.0 is at `evidence/tools/glob/1.3.0` (46 tests, 0 failed, 0 skipped; fingerprint `sha256:3923c3e66...`, the suite digest unchanged). The 1.2.0 glob record is untouched and is history. The golden index and `taffee-core/catalog/capabilities.yaml` were regenerated. The current versions are kept in `tests/fixture_support.py` as `TOOL_VERSIONS`. New dependency: `google-re2==1.1.20251105`.
