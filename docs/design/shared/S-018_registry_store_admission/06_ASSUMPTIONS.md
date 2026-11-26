# S-018 assumptions

Suite: [01 requirements](01_REQUIREMENTS.md) · [02 decisions](02_DECISIONS.md) · [03 design](03_DESIGN.md) · [04 implementation](04_IMPLEMENTATION.md) · [05 test plan](05_TEST_PLAN.md) · 06 assumptions (this file) · [07 verification](07_VERIFICATION.md)

Each assumption says what breaks if it turns out to be false, and how it is checked.

| ID | Assumption | If false | Checked by |
|---|---|---|---|
| A-1 | One registry service writes to a store at a time. | SQLite's single writer limits admissions per second, and a second writer could race on the chain head. | `append_row` refuses an append not at head (`StoreConflict`), so a race fails safe. Several writers need another store (§8 of the design). |
| A-2 | Reads greatly outnumber admissions. | Write latency matters more, and WAL checkpoints need tuning. | `test_concurrent_searches_all_answer` and the 20-submitter burst run reads and admissions at the same time. Figures come from the 100-submitter load test (G-5). |
| A-3 | The SQLite build includes FTS5. | Search fails at store start-up. | `SQLiteStore` checks for FTS5 when it opens and refuses to start without it. Confirmed for SQLite 3.46.1 in `python:3.12-slim`. |
| A-4 | The data directory is on a local filesystem. | SQLite's locking is unreliable on network filesystems. | Documented. Not checked by the store. |
| A-5 | A manifest is a few KB, and an implementation is up to a few MB. | Bundle limits and the size of the code area need revising. | `max_bundle_bytes` is a setting. The optional `bench_store.py` gives database-size figures when they are wanted. |
| A-6 | The container sandbox is available wherever admission runs. | No submission can be tested, so every one is rejected (`SandboxUnavailable`). It never falls back. | `test_admission` covers this rejection. The e2e test runs with Docker. |
| A-7 | The deployment keeps the admission private key secret. | Anyone holding it can forge rows. | Out of scope (Q-1). Rows record the key's public id, so the key can be rotated later. |
| A-8 | Approvers and Taffee agents are identified by the injected authorizer, which Governance will provide. | The development authorizer trusts a header, so anyone could approve, and any identified caller can submit code. | The development authorizer is marked `DEVELOPMENT_ONLY`, logs a warning, and must be selected by name. The agent claim waits for Q-6. |
| A-9 | Tools follow the existing tool contract (one JSON request on stdin, one JSON line on stdout). | Cases fail, and the tool is rejected with the case named. | Conformance itself. |
| A-10 | Python 3.12 runs both the service and the sandbox image. | Runners and the toolchain checks differ. | The test images pin `python:3.12-slim`. |
| A-11 | Words of `[A-Za-z0-9]` matched by prefix are enough for discovery. | Agents miss tools whose descriptions use other words. | Semantic search is out of scope, and the store interface allows adding it. |
| A-12 | Code submitted for the library is written by Taffee's own agent loop (taffee-core D-013). | Code Taffee did not write would become Taffee's. | This is the scope set for now. Only the Taffee-agent route is built, and the `external` route is separate (REG-SRC-2). |
| A-13 | Tools within one major version are compatible, so older cases still apply to newer versions. | Regression rejects a deliberate change of behaviour. | An author who changes behaviour bumps the major version, which starts a new suite (R-D15). |
| A-14 | One registry process writes to the library repository. | Concurrent commits could clash. | `LibraryRepo` serialises commits under a lock, and approvals are serialised as well. Several writers need another design. |
| A-15 | `git` is present where the service runs. | The library cannot be written. Rows are still admitted, and `sync` commits them once git is back. | The test images include git. `test_a_failed_library_commit_is_recovered_by_sync` covers the failure. |
| A-16 | A host running 20 concurrent submitters with 4 workers is representative for development. | Production figures differ. | The 100-submitter load test (G-5) sets the production figures. |
