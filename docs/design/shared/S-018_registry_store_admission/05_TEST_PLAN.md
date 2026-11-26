# S-018 test plan

Suite: [01 requirements](01_REQUIREMENTS.md) · [02 decisions](02_DECISIONS.md) · [03 design](03_DESIGN.md) · [04 implementation](04_IMPLEMENTATION.md) · 05 test plan (this file) · [06 assumptions](06_ASSUMPTIONS.md) · [07 verification](07_VERIFICATION.md)

## 1. Tooling and how to run

- **Framework:** Python `unittest`, the same as the rest of the repository. There are no new dependencies.
- **Where:** in Docker only, never on the editing Mac. Build the images from [tests/harness/Dockerfile.test](../../../../tests/harness/Dockerfile.test):
  - `taffee-cap-base:dev` runs the suite and is the sandbox image;
  - `taffee-cap-harness:dev` adds the docker CLI for the container tests.
- **Run everything, with the container tests on:**

  ```
  docker run --rm -e TAFFEE_SANDBOX_IMAGE=taffee-cap-base:dev -e TAFFEE_SANDBOX_TMP=$T \
    -v <docker socket>:/var/run/docker.sock -v $T:$T -v $PWD:$PWD -w $PWD -e PYTHONPATH=$PWD \
    taffee-cap-harness:dev python -m unittest discover -s tests
  ```

  On Docker Desktop (macOS), the socket is `~/.docker/run/docker.sock`. `$T` must have the same absolute path on the host and in the harness.
- **Without Docker,** the container classes skip and give their reason. A run counts as complete only if they ran.

### Test doubles, and what each one stands for

| Double | Stands for | Why it is safe |
|---|---|---|
| `UnconfinedTestSandbox` | The sandbox. It runs our own fixture code as a subprocess. | It is never given code from outside, and every claim about confinement is tested with the real `ContainerSandbox` |
| `SlowSandbox` | A container's start-up delay | It only adds a delay |
| `FakeFeed` and `Clocks` | The Governance revocation feed and time | The real client verifies every line it is given |
| `DevAdmissionAuthorizer` | Governance's authorizer | It is marked development-only, and tests also cover refusals |

## 2. Test cases, by requirement

Each name is a test method; the class is shown where it isn't obvious.

| Requirement | Tests |
|---|---|
| **REG-ROW-1…6** (rows, chain, checkpoint) | `test_rows.py`: `test_a_signed_row_verifies_and_names_its_key`, `test_any_changed_field_breaks_the_signature`, `test_a_change_inside_the_manifest_breaks_the_signature`, `test_another_key_is_refused`, `test_missing_or_extra_fields_are_refused`, `test_the_row_hash_covers_the_signature`, `test_an_intact_store_verifies_against_its_checkpoint`, `test_an_older_checkpoint_still_verifies_as_a_prefix`, `test_a_deleted_row_is_detected`, `test_a_deleted_last_row_is_detected_by_the_checkpoint`, `test_an_edited_row_body_is_detected`, `test_a_forged_checkpoint_is_refused`, `test_a_checkpoint_claiming_more_rows_than_exist_is_refused`, `test_an_empty_store_verifies_at_genesis` |
| **REG-STORE-1…6** (the contract, run on both `SQLiteStore` and `MemoryStore`) | `test_store_contract.py` `StoreContract`: head and append (`test_an_append_not_at_the_head_is_refused`, `test_a_duplicate_name_and_version_is_refused`, `test_concurrent_appends_keep_one_chain`); reads (`test_versions_are_in_semantic_order_not_admission_order`, `test_pages_follow_name_then_version_and_resume_after_the_cursor`, `test_pages_apply_the_label_floor`); search (`test_search_requires_every_term_and_matches_prefixes`, `test_search_covers_parameter_descriptions`, `test_an_exact_name_match_ranks_first`, `test_search_offers_only_the_newest_version_of_a_name`, `test_search_filters_by_label_and_language`, `test_search_pages_by_offset`); code (`test_code_is_named_by_its_tree_digest_and_stored_once`, `test_unsafe_code_paths_are_refused`, `test_an_unknown_or_malformed_digest_has_no_folder`); submissions. SQLite only: `test_the_store_survives_reopening`, `test_wal_mode_is_on` |
| **REG-QRY-4** (no query syntax reaches the store) | `test_query_syntax_never_reaches_the_store`, `test_an_empty_query_matches_nothing` |
| **REG-QRY-7** (cost does not grow with N) | `test_no_lookup_scans_every_row`: `EXPLAIN QUERY PLAN` for `get_row`, `versions`, `head`, `rows_page`, `search` and `find_submission` uses an index, never `SCAN rows` |
| **REG-QRY-1…6, REG-ROW-4** (the query layer) | `test_store_registry.py`: `test_list_pages_with_an_opaque_cursor`, `test_list_refuses_bad_limits_and_cursors`, `test_search_returns_model_ready_definitions`, `test_a_search_cursor_belongs_to_its_query`, `test_search_leaves_out_known_revoked_versions_but_answers_when_stale`, `test_describe_gives_the_newest_or_the_named_version_and_no_paths`, `test_a_hidden_entry_answers_like_an_absent_one`, `test_an_edited_row_is_never_served`, `test_a_tampered_filter_column_does_not_raise_a_label`, `test_a_row_signed_by_another_key_is_refused`, `test_resolve_picks_the_highest_version_in_range_at_the_floor`, `test_resolve_refusals_are_typed`, `test_changed_or_missing_code_is_refused_at_resolve`, `test_resolve_fails_closed_on_revocation`, `test_a_registry_without_revocation_cannot_be_built` |
| **REG-COMPAT-2** | `test_run_tool_runs_a_store_resolved_tool` |
| **REG-ADM-1…11** (the pipeline) | `test_admission.py` `AdmissionFlowTest`: `test_a_passing_tool_waits_for_approval_then_becomes_a_validated_row`, `test_every_state_change_is_stored`, `test_malformed_bundles_are_refused_before_anything_is_stored`, `test_size_limits_are_deployment_settings`, `test_undescribed_parameters_are_rejected`, `test_a_tool_without_a_sandbox_policy_is_rejected`, `test_a_location_that_does_not_match_the_name_is_rejected`, `test_a_missing_entry_file_is_rejected`, `test_files_for_an_undeclared_language_are_rejected`, `test_an_invalid_manifest_is_rejected_with_the_schema_errors`, `test_bad_cases_are_rejected`, `test_a_failing_case_rejects_with_the_case_named_and_no_tool_output`, `test_a_tool_that_overruns_its_time_limit_is_rejected`, `test_output_breaking_a_postcondition_is_rejected`, `test_a_sandbox_that_cannot_enforce_the_policy_rejects_without_running`, `test_a_submitter_cannot_approve_their_own_tool`, `test_only_a_waiting_submission_can_be_approved`, `test_a_rejected_submission_never_becomes_visible`, `test_a_version_is_immutable_and_the_same_tool_again_is_a_no_op`, `test_resume_finishes_submissions_left_open_by_a_restart` |
| **REG-ADM-4** (real confinement) | `ContainerAdmissionTest`: `test_an_agent_written_tool_passes_its_cases_in_a_container`, `test_a_tool_that_needs_the_network_fails_its_case_in_the_container`, `test_a_tool_cannot_read_outside_its_repository` |
| **REG-ADM-17, 18** (build and Taffee's checks) | `test_code_that_does_not_compile_is_rejected_before_any_case_runs`, `test_a_tool_that_breaks_the_result_contract_is_rejected`, `test_a_tool_declared_idempotent_must_answer_the_same_twice`, `test_cases_must_include_an_error_case_and_a_success_case`, `test_every_required_parameter_must_be_exercised_and_inputs_must_be_valid` |
| **REG-ADM-13, 19, 20, REG-LIB-1…4** (ownership and library) | `test_admitted_code_is_committed_to_the_library_and_owned_by_it`, `test_any_taffee_agent_may_propose_a_new_version_but_must_pass_the_regression_suite`, `test_a_new_major_version_starts_a_new_suite`, `test_built_in_tool_names_cannot_be_taken_over`, `test_two_agents_racing_for_one_new_version_cannot_both_win`, `test_a_failed_library_commit_is_recovered_by_sync` |
| **REG-ADM-9, REG-LIB-3** (a killed registry) | `test_crash_recovery.py`: `test_files_written_but_not_committed_are_discarded_and_committed_again`, `test_a_stale_index_lock_is_removed_at_start`, `test_a_library_killed_before_its_first_commit_is_started_again`, `test_a_failed_commit_discards_its_writes_so_the_retry_lists_the_version_once`, `test_a_row_appended_before_the_record_was_saved_finishes_as_admitted`, `test_partly_written_code_and_case_folders_are_removed_at_start`, `test_sigterm_stops_the_service_cleanly` |
| **REG-LIB-0** (the export is optional) | `test_library_export.py`: `test_with_the_export_off_admission_works_and_nothing_is_written_to_git`, `test_with_the_export_off_a_new_version_still_must_pass_the_regression_suite`, `test_every_commit_is_pushed_to_the_configured_remote` (a bare repository in a temporary folder), `test_a_failed_push_never_fails_an_admission_and_the_next_push_sends_what_was_missed`, `test_an_ssh_remote_uses_only_the_given_key_and_always_checks_the_host_key`, `test_an_ssh_key_needs_a_remote_and_known_hosts`, `test_by_default_the_export_is_off_and_no_repository_is_made`, `test_remote_settings_without_a_library_are_refused` |
| **REG-ADM-21** (approval policy) | `test_auto_approval_admits_the_moment_every_check_passes`, `test_auto_approval_still_rejects_what_fails`, `test_reserved_principals_can_neither_submit_nor_approve`, `test_auto_approval_admits_a_burst_without_anyone_approving` |
| **REG-ADM-12** (retries) | `test_a_retried_submission_returns_the_original_and_runs_nothing_again`; at the bounds, in `test_per_submitter_and_total_bounds_answer_429_and_store_nothing`; racing its original, in `test_twenty_agents_submitting_at_once` |
| **REG-ADM-14, 15, 16** (queue) | `test_admission_concurrency.py`: `test_one_flooding_agent_does_not_starve_another` (the quiet agent is served second, not ninth), `test_per_submitter_and_total_bounds_answer_429_and_store_nothing`, `test_a_restart_loses_no_submission`, `test_workers_process_in_parallel` (4 workers take less than half the time of 1) |
| **Concurrency, all together** (20 concurrent submitters) | `BurstTest.test_twenty_agents_submitting_at_once`: 12 good tools, 3 broken, 2 retries racing their originals, 2 agents racing for one version, and 1 take-over of a built-in name, all at one instant; then concurrent approvals. Asserts: no 5xx; the retries are deduplicated; every submission ends in a clear state; exactly one racer wins; 14 rows; one chain; 13 library commits; peak runs ≤ workers. `ContainerBurstTest.test_twenty_agents_submitting_at_once_in_containers` runs the same in real containers |
| **REG-API-1…5** | `test_registry_api.py`: `test_an_unidentified_caller_is_refused`, `test_unknown_paths_and_methods`, `test_search_returns_the_tool_definition`, `test_list_describe_and_versions`, `test_a_hidden_entry_answers_byte_for_byte_like_an_absent_one`, `test_resolve_returns_facts_and_never_a_path`, `test_a_revoked_version_answers_410_and_stays_revoked_when_knowledge_goes_stale`, `test_stale_revocation_knowledge_answers_503_with_retry_after`, `test_bad_requests_get_closed_errors`, `test_an_oversized_body_is_refused_before_it_is_read`, `test_submission_visibility_and_the_approval_rules`, `test_a_malformed_submission_is_refused_at_once`, `test_the_checkpoint_is_signed_and_matches_the_head`, `test_concurrent_searches_all_answer` |
| **REG-MCP-1…3** | `test_mcp_server.py`: `test_initialize_announces_tools_only`, `test_the_three_tools_are_read_only`, `test_search_describe_and_list_answer_with_structured_content`, `test_visibility_is_the_same_as_rest`, `test_nothing_but_discovery_can_be_called`, `test_bad_messages_get_json_rpc_errors`, `test_an_mcp_client_finds_a_tool_over_stdio` (a real subprocess) |
| **REG-COMPAT-3, 4, REG-ROW-5** (command line) | `test_registry_cli.py`: `test_every_index_entry_is_imported_with_its_identity_and_label`, `test_the_four_tested_tools_keep_tested_and_resolve_from_the_store`, `test_importing_again_writes_nothing`, `test_verify_passes_then_fails_after_tampering`, `test_serve_refuses_to_start_without_a_revocation_feed`, `test_the_key_file_is_private` |
| **R-D13** (the index cache) | `test_registry_cache.py`: all nine tests |
| **Everything, end to end** | `test_registry_e2e.py::test_agent_written_tool_to_orchestrator_and_back_out`: built-in tools imported; an agent submits; the tool is built and tested in containers, approved, committed to the library and owned by `taffee-library`; a broken tool is rejected and stays invisible; REST and MCP give the same answer; the dummy orchestrator searches, plans, runs the tool in a container and writes a call record; revocation removes it from search and refuses a stale binding; the store verifies |
| **REG-COMPAT-1** | The whole pre-existing suite passes unchanged |

### The running deployment

Two scripts run against the live services, on the Docker host, after `up`. They need only docker on the host.

| Script | What it checks |
|---|---|
| `deploy/smoke.py` (profile `smoke`) | The 12-step client journey: discover, submit, admit, retry, search, resolve, reject a broken tool, library commit, MCP, revoke |
| `deploy/stability.sh` | Baseline smoke and audit; `docker restart` writes nothing new; `docker stop` exits 0 after a clean shutdown; `docker kill` while 8 submissions are being built and tested, after which all 8 are admitted, each committed once, with no orphaned sandbox; a feed outage makes resolve answer 503 and it recovers by itself; manual approval (waits, refuses self-approval and non-approvers, approves, rejects); 20 simultaneous submissions from 20 agents; `down`/`up` keeps the data and smoke passes again; a final audit |

The audit (`deploy/client.py audit`) checks: every row verifies; no submission is left open; every admitted submission is committed to the library exactly once; no ledger lists a version twice; the library is clean and passes `git fsck --strict`; no stale lock or scratch folder is left.

## 3. Blocked and not yet run

| What | Blocked by | Plan |
|---|---|---|
| Load test at 100 concurrent submitters (the design goal) | G-5: runs after the rest is accepted | Use `test_admission_concurrency`'s burst with `CONCURRENCY = 100` and a range of worker counts, in containers. Record throughput and peak memory for each worker count, then choose the production `workers` from those figures. No threshold is set in advance |
| Restricting submission to Taffee agents | G-4 (Q-6), Governance's identity contract | Test the contract's agent claim once it exists |
| The external route (REG-SRC-2) | G-6 | A new suite once it is specified |
| A remote orchestrator | G-2 (Q-4) | None yet |
| `tests/bench/bench_store.py` (optional) | Not needed: REG-QRY-7 is proved by the query plans | Run by hand if database size or build time figures are wanted |
