# Verification: S-002 registry

Status: M1 to M6 implemented, tested and reviewed milestone by milestone. Each section below is the record for one milestone or review round.

## M1: load and verify the index; list and describe

Files: `capability/registry.py`, `tests/test_registry.py`.

Command (Linux test host, fresh copy of the tree, Node 22 test image, Python 3.12.15):

```
docker run --rm -v "$PWD":/work -w /work -e PYTHONDONTWRITEBYTECODE=1 the test image sh -c \
  "python3 -m pip install -q --target /tmp/deps pyyaml==6.0.2 jsonschema==4.23.0; \
   PYTHONPATH=/work:/work/tests:/tmp/deps python3 -m unittest discover -s tests -p test_registry.py -v"
```

Result (round 3, after review): `Ran 18 tests ... OK` on the Linux test host, Python 3.12.15, the test image.

| Check (IMPLEMENTATION.md M1) | Covered by | Result |
|---|---|---|
| Tampered index fails list and describe with IndexError | `test_tampered_index_fails_list_describe_and_list_with_filter` | pass |
| Malformed index: non-object JSON, scalar JSON, valid digest with missing entries, valid digest with invalid label, all IndexError | `MalformedIndexTest` | pass |
| Non-canonical index refused even with a matching digest | `test_non_canonical_index_is_refused_even_with_a_valid_digest` | pass |
| Missing index refused | `test_missing_index_is_refused` | pass |
| Default list shows the entry as Declared; minLabel above it lists nothing; kind filter | `test_default_list_shows_the_entry_as_declared`, `test_min_label_above_the_entry_lists_nothing`, `test_kind_filter` | pass |
| describe returns the embedded manifest; unknown name gives NotFound | `test_describe_returns_the_embedded_manifest`, `test_describe_unknown_name_is_not_found` | pass |
| Repeated calls give identical output | `test_repeated_calls_give_identical_output` | pass |
| The five S-001 tools list as Declared with no languages | `RealCatalogTest` | pass |
| describe: a name used by two kinds is Ambiguous without kind, and resolves with kind | `DescribeCollisionTest` | pass |

Not in M1, by design: resolve, language selection, digests, entry checks, Match.
| Corrupt index is IndexError even with invalid options | `OptionOrderTest` | pass |
| Missing or invalid bundled schema is SetupError | `BundledSchemaTest` | pass |

## M2: name, range, kind and label (internal candidate)

Files: `capability/resolve.py` (steps 1 to 4 of SPEC §6.2, internal), `capability/registry.py` (`_candidate`), `tests/test_resolve_m2.py`.

Result (Linux test host, Node 22 test image, Python 3.12.15, full suite on a fresh copy): `Ran 121 tests ... OK`.

| Check (IMPLEMENTATION.md M2) | Covered by | Result |
|---|---|---|
| `^1.0.0` picks 1.4.0 over 1.2.0 and rejects 2.0.0 | `MultiVersionTest.test_range_picks_1_4_0_over_1_2_0_and_rejects_2_0_0` (index built by hand, three versions) | pass |
| Unknown name is NotFound | `test_unknown_name_is_not_found` | pass |
| A name in two kinds is Ambiguous without kind; kind selects one | `KindTest` | pass |
| minLabel above every version is BelowLabel | `test_label_floor_is_below_label_when_no_version_meets_it` | pass |
| No version in range is NotFound | `test_no_version_in_range_is_not_found` | pass |
| Malformed range is InvalidRange | `BadRangeTest` | pass |

Layout note: S-001 keeps one manifest per folder (CAP-MAN-1), so the catalog cannot yet hold several versions of one tool. The multi-version test therefore uses an index built by hand. Choosing a multi-version layout is a decision, not part of M2.

## M3: languages and NoImplementation (internal)

Files: `capability/resolve.py` (`select_implementation`, step 5), `capability/registry.py` (`_select`), `tests/test_languages_m3.py`.

Result (round 3, after review; the Linux test host, Node 22 test image, Python 3.12.15, full suite on a fresh copy): `Ran 138 tests ... OK`.

| Check (IMPLEMENTATION.md M3) | Covered by | Result |
|---|---|---|
| `[typescript, bash]` chooses Bash, with its location from the embedded manifest | `test_first_caller_language_the_tool_implements_is_chosen` | pass |
| Preference order is respected | `test_preference_order_is_respected` | pass |
| `[typescript]` gives NoImplementation listing `[bash, python]` | `test_no_match_is_no_implementation_listing_the_tools_languages` | pass |
| No fallback to an unnamed language | `test_never_falls_back_to_an_unnamed_language` | pass |
| NoImplementation is never IntegrityError | `test_no_implementation_is_never_an_integrity_error` | pass |
| The five S-001 tools give NoImplementation with an empty list | `FiveToolsTest` | pass |
| minLabel Validated gives BelowLabel before the language is checked | `FiveToolsTest.test_minlabel_validated_gives_below_label_before_language_is_checked` | pass |

Implementation note: the index stores each implementation's language and digest, and the location comes from the embedded manifest. Both are verified, so no second lookup of the index is needed.

Round 2: a consistency guard runs before any language is chosen. An index whose implementation list disagrees with its embedded manifest, or a manifest implementation with no location, is `IntegrityError`, not a raw exception. Tests in `IndexManifestMismatchTest` alter only the index (with a recomputed `index_digest`) or only the manifest, and expect `IntegrityError`.

Round 3 precedence: step 5 (language availability) runs first. An unsupported caller language on a mismatched row is `NoImplementation`, listing the indexed languages. Once a language is chosen, the whole row is checked for consistency, and a mismatch is `IntegrityError` before any result. Regression: `test_mismatched_row_with_an_unsupported_language_is_no_implementation`.

## M4: entry checks, per-resolve digests and the Match

Files: `capability/match.py` (entry checks, verify_row, manifest digest check), `capability/registry.py` (`resolve`), `tests/test_resolve_m4.py`.

Result (Linux test host, Node 22 test image, Python 3.12.15, full suite on a fresh copy): `Ran 150 tests ... OK`.

| Check (IMPLEMENTATION.md M4) | Covered by | Result |
|---|---|---|
| Match carries language, implementation location, entry path, revocation unchecked; plain data | `MatchTest.test_match_carries_inert_metadata_for_the_chosen_language` | pass |
| Identities equal the index when nothing changed | `test_match_identities_equal_the_index_when_nothing_changed` | pass |
| A changed byte gives IntegrityError on the next call; no cache | `test_a_changed_byte_gives_integrity_error_on_the_next_call_with_no_cache` | pass |
| resolve does not run the entry | `test_resolve_does_not_run_the_entry` | pass |
| No implementation stops before any entry check | `test_no_implementation_stops_before_any_entry_check` | pass |
| Missing entry: EntryError.Missing | `EntryErrorTest.test_missing_entry_is_entry_error_missing` | pass |
| Entry that is a link: EntryError.Link | `test_entry_that_is_a_link_is_entry_error_link` | pass |
| Implementation folder reached through a link: EntryError.Link | `test_implementation_folder_reached_through_a_link_is_entry_error_link` | pass |
| Entry resolving outside its folder: EntryError.Escape | `test_entry_resolving_outside_its_folder_is_escape` (calls the entry check directly) | pass |
| Library agent: Match with code_digest, no language fields | `AgentAndModelTest.test_library_agent_resolves_with_its_code_digest` | pass |
| External model: SourceUnavailable | `test_external_model_is_source_unavailable` (pinned temporary repository) | pass |
| The five S-001 tools: NoImplementation, not a Match | `FiveToolsTest` | pass |

Note on Escape: a link anywhere on the path is caught first as Link, so Escape can only be reached by calling the entry check with a crafted entry name. It is kept as a defensive check, and its test calls `check_entry` directly.

## M4 round 2

Changes: the index's tool implementations now carry their `entry` filename, taken from `policy/languages.yaml` at build time. `resolve` reads the entry from the verified index and never reads the language policy, so a later policy change cannot change a Match. The digest order is manifest first, then code, for agents and models as for tools. `resolve` no longer raises `LanguageError`.

Migration (S-001 index schema, a reversible change): `schemas/index.schema.json` requires `entry` on each indexed implementation. The golden index is regenerated on the Linux test host: `index_digest sha256:7f66898c8a9487cf912e5a990c85818a1c9b4573fef8ae8b2940b511940f2f65` (uncommitted).

Result (Linux test host, Node 22 test image, Python 3.12.15, full suite on a fresh copy): `Ran 153 tests ... OK`.

| Check | Covered by | Result |
|---|---|---|
| A policy change after indexing does not change the Match | `PolicyBindingTest.test_policy_change_after_indexing_does_not_change_the_match` | pass |
| A second candidate file in the folder is refused by the code digest | `test_a_second_candidate_file_in_the_folder_is_refused_by_the_code_digest` | pass |
| Dual fault on an agent: the manifest IntegrityError is reported first | `DigestOrderTest.test_dual_fault_reports_the_manifest_error_first` | pass |
| Golden index matches the regenerated fixture | `test_pins.GoldenIndexTest` | pass |

## M4 round 3

Correction: `check_entry` tests for a link before it tests for existence, so a dangling symlink entry is `EntryError.Link`, not `Missing`. Regression: `EntryErrorTest.test_a_dangling_entry_link_is_entry_error_link_not_missing`.

Result (Linux test host, Node 22 test image, Python 3.12.15, full suite on a fresh copy): `Ran 154 tests ... OK`.

## M5: isolation and load errors, and M6: extension check

Files: `tests/test_isolation_m5.py`, `tests/test_extension_m6.py`.

Result (Linux test host, Node 22 test image, Python 3.12.15, full suite on a fresh copy): `Ran 158 tests ... OK`.

| Check | Covered by | Result |
|---|---|---|
| A removed implementation folder: list shows the row; resolve gives EntryError.Missing; the other row resolves | `IsolationTest.test_a_removed_folder_is_a_row_error_and_the_others_still_resolve` | pass |
| List and resolve are deterministic for the same index and bytes | `test_list_and_resolve_are_deterministic_for_the_same_index_and_bytes` | pass |
| A fictional fourth language, data only: resolves when named | `FictionalLanguageTest.test_the_fictional_language_resolves_when_named` | pass |
| The fictional language is not chosen when not named (NoImplementation, listing it) | `test_it_is_not_chosen_when_not_named` | pass |
