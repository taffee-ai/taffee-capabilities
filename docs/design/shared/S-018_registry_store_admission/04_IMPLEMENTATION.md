# S-018 implementation plan

Suite: [01 requirements](01_REQUIREMENTS.md) · [02 decisions](02_DECISIONS.md) · [03 design](03_DESIGN.md) · 04 implementation (this file) · [05 test plan](05_TEST_PLAN.md) · [06 assumptions](06_ASSUMPTIONS.md) · [07 verification](07_VERIFICATION.md)

## Milestones and the order they depend on

```
R0 cache ─┐
R1 rows ──┼─► R2 stores ─► R3 query layer ─┬─► R5 REST ─┐
          │                                └─► R6 MCP  ─┼─► R8 end-to-end;  R9 query plans
          └──────────────► R4 admission ───────────────┘
R7 import-index (needs R2, R3)
R4 ─► R10 build + Taffee checks ─► R11 library ownership + regression ─► R12 queue + concurrency ─► R8
```

| Milestone | Delivers | Files | Exit evidence | Requirements |
|---|---|---|---|---|
| R0 | Verified-index cache in `Registry` | `capability/registry.py` | `test_registry_cache.py`; existing registry tests unchanged | REG-COMPAT-1, R-D13 |
| R1 | Row format, signing, chain, checkpoint, `verify_store` | `capability/rows.py` | `test_rows.py` | REG-ROW-1…6 |
| R2 | `CapabilityStore`, `SQLiteStore`, `MemoryStore`, `CodeArea` | `capability/store/` | `test_store_contract.py` runs on both stores | REG-STORE-1…6 |
| R3 | `StoreRegistry`: list, search, describe, versions, resolve; `run_tool` accepts it | `capability/store_registry.py`, `capability/execute.py` | `test_store_registry.py` | REG-QRY-1…7, REG-COMPAT-2 |
| R4 | Admission state machine, sandboxed conformance, approval | `capability/admission.py` | `test_admission.py` (double sandbox), plus container cases | REG-ADM-1…11 |
| R5 | `/v2` REST service and authorizer actions | `capability/registry_api.py`, `capability/authz.py` | `test_registry_api.py` over a real socket | REG-API-1…5 |
| R6 | MCP stdio server | `capability/mcp_server.py` | `test_mcp_server.py` drives a real subprocess | REG-MCP-1…3 |
| R7 | `import-index` command | `capability/registry_cli.py` | `test_registry_cli.py`: the 5 real tools imported with identities unchanged | REG-COMPAT-3, 4 |
| R8 | End to end: an agent submits, the tool is tested in containers, approved and committed to the library, and an orchestrator searches over REST and MCP, runs it in a container, and loses it on revocation | `tests/test_registry_e2e.py`, `tests/dummy_orchestrator.py` | Passes on the test host with Docker | all |
| R9 | Lookups do not grow with the number of rows | `test_store_contract.py::test_no_lookup_scans_every_row`; `tests/bench/bench_store.py` (manual, optional) | The query plan uses indexes for every lookup | REG-QRY-7 |
| R10 | The build stage and Taffee's checks: compile, input validity, coverage, the result contract, idempotency | `capability/admission.py` | `test_admission.py`, the "Taffee's own checks" group | REG-ADM-17, 18 |
| R11 | Library ownership: owner normalised, commit at admission, `sync`, regression by major version, built-in names closed, approval policy | `capability/library_repo.py`, `capability/admission.py` | `test_admission.py`, the ownership and approval-policy groups | REG-ADM-13, 19, 20, 21, REG-LIB-1…4 |
| R12 | The admission queue: workers, round-robin, bounds (429), retries, resume; the HTTP backlog | `capability/admission_queue.py`, `capability/registry_api.py` | `test_admission_concurrency.py`, with 20 concurrent submitters in-process and in containers | REG-ADM-12, 14, 15, 16 |

## Gates

| Gate | Blocks | Resolution in S-018 |
|---|---|---|
| G-1: the admission key's location (Q-1) | Production use | Development uses a file key the deployment supplies. Not a production decision. |
| G-2: code delivery to a remote orchestrator (Q-4) | Orchestrator outside this process | Out of scope. The e2e test runs the orchestrator in-process. |
| G-3: approval policy (Q-5) | Risk-based approval | A setting: `manual` (an approver who is not the submitter) or `auto`. The development default is `auto`. |
| G-4: recognising a Taffee agent principal (Q-6) | Restricting submission to Taffee's agents | Waits for Governance's identity contract. The development authorizer lets any identified caller submit. |
| G-5: the 100-submitter load test | Claiming the design goal | Runs after the rest is accepted. Dev and test run 20. |
| G-6: the `external` route (REG-SRC-2) | Third-party tools, APIs and LLMs | Designed only. |

## Where it runs

Build and test run in Docker. The images are built from [tests/harness/Dockerfile.test](../../../../tests/harness/Dockerfile.test): `taffee-cap-base:dev` for the suite and the sandbox, and `taffee-cap-harness:dev` for the container tests. S-018 is verified on two hosts: a Docker Desktop host (x86_64) and the Linux test host.

## No new dependencies

`sqlite3` ships with Python, and the image's SQLite build includes FTS5. Ed25519 comes from the pinned `cryptography`.
