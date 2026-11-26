# Verification: S-001 manifests and validators

Status: M1 to M3 implemented, tested and accepted in review. M5 implemented and tested, with its review rounds recorded below. M4 (prototype pins) was withdrawn and superseded by M5.

Where tests run: the Linux test host, inside the test image (Python 3.12.15). Source is copied to a working directory on the Linux test host for each run; the copy is not a git checkout and is not committed.

## M1: canonical JSON and digests

Files: `capability/canon.py`, `capability/digest.py`, `capability/__init__.py`, `tests/test_canon.py`, `tests/test_digest.py`, `pyproject.toml`.

Command (on the Linux test host, from the test copy):

```
docker run --rm -v "$PWD":/work -w /work -e PYTHONDONTWRITEBYTECODE=1 -e PYTHONPATH=/work the test image \
  python3 -m unittest discover -s tests -p "test_*.py" -v
```

Result: `Ran 19 tests ... OK` on Python 3.12.15 (the count rose from 17 after the fix described below).

| Check (IMPLEMENTATION.md M1) | Covered by | Result |
|---|---|---|
| CRLF and LF text give the same digest | `test_crlf_and_lf_text_give_same_digest` | pass |
| NUL in first 8000 bytes hashed raw | `test_nul_in_first_8000_bytes_hashes_raw` | pass |
| Symlink refused, path named | `test_symlink_is_refused_and_named`, `test_symlink_directory_is_refused` | pass |
| Same input, same output across calls | `test_same_tree_twice_same_digest`, `test_same_value_same_bytes_across_calls` | pass |
| Changing a byte changes the digest | `test_one_byte_change_changes_digest`, `test_changed_value_changes_digest` | pass |
| Excluded names do not change the digest | `test_excluded_names_do_not_change_digest` | pass |
| FIFO refused | `test_fifo_is_refused` | pass |
| Symlink named `.DS_Store` refused; regular `.DS_Store` ignored | `test_symlink_named_ds_store_is_refused`, `test_regular_ds_store_is_still_ignored` | pass |
| Canonical form (sorted keys, indent, final newline, UTF-8) | `test_sorted_keys_two_space_indent_and_final_newline`, `test_utf8_not_escaped` | pass |

Moved to M2 (not failed): "adding a YAML comment does not change `manifest_digest`". It needs PyYAML, which M1 deliberately does not depend on.

## Not yet verified

- Clean-clone reproduction and golden digests (M4).
- The pinned external `tree_sha256` (M4).
- Anything on the Mac (by rule, nothing is run there).

## M2: schemas and manifest validation

Files: `schemas/manifest.{tool,agent,model}.schema.json`, `schemas/index.schema.json`, `capability/manifest.py`, `tests/test_manifest.py`, `tests/fixtures/manifests/{valid,invalid}/`, `pyproject.toml` (pins).

Command (on the Linux test host, from the test copy; the pinned packages are installed into `/tmp/deps` inside the container, not on the host):

```
docker run --rm -v "$PWD":/work -w /work -e PYTHONDONTWRITEBYTECODE=1 the test image sh -c \
  "python3 -m pip install -q --target /tmp/deps pyyaml==6.0.2 jsonschema==4.23.0; \
   PYTHONPATH=/work:/tmp/deps python3 -m unittest discover -s tests -p 'test_*.py' -v"
```

Result (round 3, after the M-001 sample fix): `Ran 44 tests ... OK`, Python 3.12.15, on the Linux test host (the test image). Round 1 had 40 tests. Round 2 added one model-contract test (41). Round 3 added three tests for the M-001 sample (44).

| Check (IMPLEMENTATION.md M2) | Covered by | Result |
|---|---|---|
| Field-path errors (unknown field, missing owner, name rule, version rule) | `test_unknown_field_fails_with_its_path`, `test_missing_owner_fails_with_path_owner`, `test_name_rule`, `test_version_rule` | pass |
| Each fixture validates or fails as its name says | `test_tool_agent_model_fixtures_pass`, `test_agent_without_outcomes_fails`, `test_model_floating_version_and_bad_category_fail`, `test_raw_credential_in_model_is_refused_by_field_name` | pass |
| Unknown field `foo` fails with path `foo` | `test_unknown_field_fails_with_its_path` | pass |
| YAML comment does not change `manifest_digest`; a value change does | `test_comment_does_not_change_digest_and_value_does` | pass |

Additional findings during M2, all fixed and tested:
- YAML reads `2025-06-01` as a date. The loader now keeps it as text (`test_model_version_that_looks_like_a_date_is_text`).
- YAML reads unquoted `no` as `false`. The fixture is quoted, and a validation hint names the cause (`test_unquoted_no_is_reported_with_a_hint`).
- Duplicate YAML keys are refused at load (`test_duplicate_key_is_refused_at_load`), because YAML would otherwise overwrite silently.
- Placeholder owners and summaries are refused (`test_placeholder_owner_fails`).
- A label written in a manifest is refused as an unknown field (`test_label_written_in_manifest_is_refused`).

Model contract, agreed in review:
- Source rule for models: a model adapter normally uses `source.type: library`, so its code identity is a `code_digest`. The schema also permits `generated` and `external`, and those follow their existing digest rules (`code_digest` for generated, `tree_sha256` with a pinned commit for external). The `provider` source type is withdrawn.
- The top-level `version` is semantic for every kind. The provider's version identifier is `provider.modelVersion`, pinned and never `latest`. `provider.modelId` refuses `latest` too.
- M-001 SPEC §5.1 sample is aligned: semantic version, library adapter source, `modelVersion`, quoted `trainingUse`, one `licence` key. Route fields (endpoint, access kind, apiKeyEnv, timeoutMs, region) and approval-side fields (egress, weights, cost, retired, proposed_by) are not manifest fields.
- `tests/test_m001_sample.py` reads the sample from the real M-001 SPEC on the Linux test host and checks that its key structure matches the passing model fixture, and that it declares no route or approval fields. Values are placeholders, so only structure is compared.

## M3: label policy, index and CLI (fixtures only)

Files: `capability/labels.py`, `capability/index.py`, `capability/cli.py`, `capability/digest.py` (added `tree_digest_roots` with optional folders), `policy/labels.yaml`, `tests/test_index.py`, `tests/test_cli.py`.

Command (on the Linux test host, from the test copy):

```
docker run --rm -v "$PWD":/work -w /work -e PYTHONDONTWRITEBYTECODE=1 the test image sh -c \
  "python3 -m pip install -q --target /tmp/deps pyyaml==6.0.2 jsonschema==4.23.0; \
   PYTHONPATH=/work:/tmp/deps python3 -m unittest discover -s tests -p 'test_*.py'"
```

Result: `Ran 66 tests ... OK`, Python 3.12.15, Linux test host, the test image.

| Check (IMPLEMENTATION.md M3) | Covered by | Result |
|---|---|---|
| Two runs byte-identical | `test_two_builds_are_byte_identical` | pass |
| Embedded manifest equals the parsed source | `test_embedded_manifest_is_the_parsed_source` | pass |
| Policy byte change changes inputs and index digests | `test_policy_byte_change_changes_inputs_and_index_digests` | pass |
| Manifest with its own label refused | `test_label_written_in_manifest_is_refused` (M2) | pass |
| Invalid manifest: no index | `test_invalid_manifest_writes_no_index` | pass |
| Stale index removed by a failed build | `test_stale_index_does_not_survive_a_failed_build` | pass |
| Folder without manifest; name vs folder; policy version and presence | `test_folder_without_manifest_fails`, `test_name_must_match_folder`, `test_policy_other_version_fails`, `test_policy_file_missing_fails` | pass |
| S-001 awards Declared only; no manifest field raises it | `test_builds_index_with_declared_library_tool`, `test_valid_library_tool_is_still_declared`, `test_no_manifest_field_can_raise_the_label`, `test_agent_and_model_are_declared` | pass |
| External source: Declared, null code_digest | `test_external_source_is_declared_with_null_code_digest` | pass |
| Generated source: non-null code_digest | `test_generated_source_gets_a_code_digest` | pass |
| Entries sorted by kind then name | `test_entries_are_sorted_by_kind_then_name` | pass |
| External-only catalog with no library/ builds | `test_external_only_catalog_without_library_folder_builds` | pass |
| Absent library/ and empty library/ give the same inputs digest | `test_missing_library_and_empty_library_give_the_same_inputs_digest` | pass |
| CLI exit 0 valid, 1 invalid, no index on failure | `test_valid_tree_exits_zero_and_writes_index`, `test_invalid_tree_exits_one_and_writes_nothing` | pass |

Deferred, as agreed: the five real external manifests and their pins (M4). Validated and Tested for tools are defined in T-001, with the entry format and the export check. S-001 awards Declared only (from review).

## M4: the five real manifests, pins, golden index and reproduction

Files: `catalog/tool/{git-log,glob,grep,unawaited-async,graph-query}/manifest.yaml`, `capability/pins.py`, `capability/index.py` (external pins checked against a local clone), `capability/cli.py` (`--pin-repo URL=PATH`), `tests/test_pins.py`, `tests/golden/index.json` (uncommitted until the owner approves).

Pinned source: the prototype repository, commit `cb90f450c763656d6f6f21ad91443bcb4c06a5bf` (the prototype's HEAD at the time). The commit was bundled, copied to the Linux test host and cloned there.

Pins (sha256-tree-v1 over the single pinned file, computed by `capability/pins.py` from the commit with `git archive`):

| Tool | Path at cb90f45 | tree_sha256 |
|---|---|---|
| git-log | harness/tools/git-log.ts | sha256:f4faae25…a705c |
| glob | harness/tools/glob.ts | sha256:97eae8a3…75aa8 |
| grep | harness/tools/grep.ts | sha256:de867342…1d70a9 |
| unawaited-async | harness/tools/unawaited-async.ts | sha256:720b2473…ac94d5 |
| graph-query | harness/context/retrieval/query/graph-query-tool.ts | sha256:cfe3814d…155a3a |

Command (on the Linux test host, from the test copy, with the clone mounted read-only at `/pins`):

```
docker run --rm -v $PINS:/pins:ro -v "$PWD":/work -w /work \
  -e PYTHONDONTWRITEBYTECODE=1 -e PROTOTYPE_CLONE=/pins the test image sh -c \
  "python3 -m pip install -q --target /tmp/deps pyyaml==6.0.2 jsonschema==4.23.0; \
   PYTHONPATH=/work:/tmp/deps python3 -m unittest discover -s tests -p 'test_*.py'"
```

Result: `Ran 73 tests ... OK`, no skips, Python 3.12.15, the test image, Linux test host.

| Check (IMPLEMENTATION.md M4) | Covered by | Result |
|---|---|---|
| The five manifests build with their pins; all Declared; code_digest null | `test_the_five_real_manifests_build_with_their_pins` | pass |
| Each pin recomputes to the manifest value | `test_pinned_digest_matches_the_manifest_for_each_tool` | pass |
| A wrong tree_sha256 fails the build | `test_one_wrong_tree_sha256_fails_the_build` | pass |
| External pin without a clone fails the build, naming --pin-repo | `test_external_pin_without_a_clone_fails_the_build` | pass |
| A folder that is not a git clone is refused | `test_a_folder_that_is_not_a_git_clone_is_refused` | pass |
| Two copies reproduce the same index bytes | `test_two_copies_reproduce_the_same_index_bytes` | pass |
| Golden index matches | `test_golden_index_matches_the_committed_fixture` | pass |
| Prototype tree at cb90f45 is the pinned content | the pins recompute from the commit, not the working tree; A-S1-07 is recorded | pass |

Golden index: `index_digest sha256:66dc5d1b2ec0e114e5a91af156d1dfbf0bc388b61b6322e8732d1f2ae207f24b`, generated on the Linux test host inside the test image, from the copy that was tested.

Limit, stated plainly: "fresh clone" here is a fresh copy of the working tree into another directory on the Linux test host, not a `git clone`. A true git clone needs the M-track commits, which wait for the owner's approval. The reproduction result is therefore same-host copy reproduction, and it is recorded that way.

## M5: languages, implementations, ledger and conformance (LANGUAGES.md v2)

Tests run on the Linux test host only, in the test image, Python 3.12.15, Node 20.20.2, bash 5.2.

Command (on the Linux test host, from the test copy):

```
docker run --rm -v "$PWD":/work -w /work -e PYTHONDONTWRITEBYTECODE=1 the test image sh -c \
  "python3 -m pip install -q --target /tmp/deps pyyaml==6.0.2 jsonschema==4.23.0; \
   PYTHONPATH=/work:/tmp/deps python3 -m unittest discover -s tests -p 'test_*.py'"
```

Result (superseded, see the final M5 result below): `Ran 87 tests ... OK (skipped=1)`.

| Area | Evidence |
|---|---|
| Language registry: three languages loaded; lowercase ids enforced; missing registry refused | `test_conformance.RegistryTest` |
| Expectation operators (ok, contains, not_contains, json_key, exit_code); unknown operator is an error | `test_conformance.OperatorTest` |
| Runner safety: entry confined to its folder; links refused; unknown runner refused; environment allow-list; timeout | `test_conformance.RunSafetyTest` |
| Python and bash fixtures conform to every case | `test_python_fixture_conforms_to_every_case`, `test_bash_fixture_conforms_to_every_case` |
| TypeScript fixture: skipped, reason recorded (Node 20 lacks `--experimental-strip-types`; Node 22.6+ needed) | `test_conformance.TypeScriptConformanceTest` |
| Identity: order-independent over sorted (language, code_digest); changed bytes change identity | `test_index.ToolIndexTest` |
| Reordered implementations give the same entries | `test_reordered_implementations_do_not_change_the_index` |
| Publication gate: missing ledger entry refused; changed identity refused; matching entry passes | `test_index.PublicationGateTest` |
| Unknown language, duplicate language, location outside library/, missing entry file refused | `test_index.ImplementationRefusalTest` |
| Extension: a fictional fourth language accepted by registry, schema and identity without code change | `test_extension.ExtensionTest` |
| Five tools declared with `implementations: []`, each with a ledger entry; missing ledger entry refuses the catalog | `test_pins.FiveToolsTest` |
| Golden index for the five tools matches | `test_pins.GoldenIndexTest` |

Golden index (regenerated on the Linux test host from the tested tree, copied to `tests/golden/index.json`, uncommitted): `index_digest sha256:7de757c613567d160207e9f5c6b58b208893d45b0014f831a6dd487eb581486a`.

Gaps, stated plainly:
- TypeScript conformance has not run on this host. The test image's Node is 20; the runner needs 22.6+. A later image update is needed to run it.
- The owner role `capabilities-tools-owner` is provisional. It awaits the owner's confirmation.
- No commit. The clean-clone check waits for the owner's approval.
- S-002 spec and roadmap are updated in text only. The S-002 registry code is not built, so `NoImplementation` is specified, not tested.


## M5 final result

Corrections applied: output is bounded while the child runs (stdout and stderr each, with a regression test); the declared toolchain check runs before a conformance run, and an unavailable toolchain is reported as such (tested); the TypeScript fixture prints the same spacing as the others.

Runs on the Linux test host, Python 3.12.15 in both:
- the Node 22 test image (the base test image plus the Node 22.23.3 binary from node:22-slim, built on the Linux test host, not committed): `Ran 91 tests ... OK`, no skips. TypeScript conformance ran and passed.
- the test image (Node 20.20.2): see the note in the run log; TypeScript skips with its recorded reason.

Golden index: `index_digest sha256:7de757c613567d160207e9f5c6b58b208893d45b0014f831a6dd487eb581486a` (unchanged by the conformance corrections, since the index does not depend on the runner).

Open items:
- `capabilities-tools-owner` awaits the owner's confirmation.
- No commit. The clean-clone check waits for the owner's approval.
- Proposed decision (not implemented): value-level expectations. The five operators check keys and substrings only. A check such as "double equals 6" needs a new operator, `json_equals`, which needs review agreement.
- S-002 `NoImplementation` is specified, not built.


## M5 round 2 result

Corrections: the toolchain is a required argument of `run_implementation`, and every fixture call passes the toolchain declared in `policy/languages.yaml`; stdin is bounded (64 KiB) and written from a thread under the same timeout, so a child that never reads cannot hang the parent; regression tests cover the no-read child, the oversized input, and a missing toolchain argument.

- the Node 22 test image (the base test image plus the Node 22.23.3 binary; Python 3.12.15): `Ran 94 tests ... OK`, no skips. TypeScript conformance ran and passed.
- the test image (Node 20.20.2): `Ran 94 tests ... OK (skipped=1)`. The skip is TypeScript, and its reason is recorded.

json_equals is not added. Value-level conformance is a T-001 proposal with concrete cases, agreed in review.
