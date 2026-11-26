# S-018 verification record

Suite: [01 requirements](01_REQUIREMENTS.md) · [02 decisions](02_DECISIONS.md) · [03 design](03_DESIGN.md) · [04 implementation](04_IMPLEMENTATION.md) · [05 test plan](05_TEST_PLAN.md) · [06 assumptions](06_ASSUMPTIONS.md) · 07 verification (this file)

Status: implemented and tested, including the library-export change of 2026-11-16 (§7), the store and checks fixes of 2026-11-22 and the library ownership fix of 2026-11-23 (§8). Not yet reviewed.

**The current runs of record are in §8.** §1 to §7 record earlier code. Where §8 corrects them, it says so: the `stability.sh` results in §6 and §7 came from a version of the script that could not report a failure in nine of its checks.

## 1. The run of record (2026-11-15, stability round)

| | |
|---|---|
| Host | The Docker Desktop test host: Docker Desktop 29.9.2, linux/amd64 VM, 4 CPUs |
| Images | Built from `tests/harness/Dockerfile.test`. `taffee-cap-base:dev` has Python 3.12-slim, SQLite 3.46.1 with FTS5, Node 22.11.0 and git. `taffee-cap-harness:dev` adds the docker 28.1.1 CLI |
| Command | `python -m unittest discover -s tests`, in the harness, with `TAFFEE_SANDBOX_IMAGE=taffee-cap-base:dev`, `TAFFEE_SANDBOX_TMP` set, and the Docker socket mounted |
| Result | **Ran 1008 tests, OK (skipped=1), twice in a row on the Docker Desktop host** (497.7 s and 485.2 s), after the stability fixes in §6. The Linux host: see below. Earlier runs of record: 994 and 988 tests, OK, 1 skip |
| The one skip | `test_graph_query.PreparatoryStateTest.test_the_published_context_schema_matches_the_pinned_one`. This skip is deliberate: Context publishes no `graph_query` tool yet. It was the suite's only skip before S-018 too |
| Container tests | All of them ran, none skipped: S-011, T-005, S-018 admission, the 20-submitter burst and the end-to-end test |

**The Linux test host** (4 threads, the designated test machine), same images and command: **Ran 1006 tests in 1060.0 s, OK (skipped=1)**, on the code just before the last fix of §6 (the owner label, which adds 2 tests). On the final code: **Ran 1008 tests in 1104.7 s, OK (skipped=1)**, no container left. Its 20-submitter container burst took 369.5 s (about 6 times the Docker Desktop host's) with every submission in its expected state: the load at which, before the sandbox timing fix (§6), a correct tool could time out before it ran.

## 2. What the S-018 tests showed

| Area | Evidence |
|---|---|
| Rows, chain, checkpoint | `test_rows.py`: 14 tests. Deleting, editing, re-signing with another key, inserting past the checkpoint or forging the checkpoint are each detected |
| Store contract | `test_store_contract.py`: 49 tests, the same contract passing on `SQLiteStore` and `MemoryStore` |
| No full scans (REG-QRY-7) | `test_no_lookup_scans_every_row`: `get_row`, `versions`, `head`, `rows_page`, `search` and `find_submission` all use an index. The first run found that `head` used `ORDER BY seq DESC LIMIT 1`, which reads one row but appears in the plan as a scan; it was rewritten as an indexed lookup of `max(seq)` |
| Query layer | `test_store_registry.py`: 19 tests, including tampered rows never being served and tampered filter columns never raising a label |
| Admission | `test_admission.py`: 39 tests. Three of them run in real containers: a tool that tries to reach the network fails its case, and so does a tool that tries to read a host file outside its repository |
| Concurrency (20 submitters) | `test_admission_concurrency.py`: 7 tests. The burst of 20 submissions at one instant ends with 13 admitted, 3 build failures, 2 retries deduplicated, 1 take-over attempt on a built-in name refused, and 1 of 2 racers admitted. That leaves 14 rows (13 plus the seeded built-in), one chain and 13 library commits. In real containers, 20 submissions take **54.6–56.6 s with 4 workers** (across two runs). Elsewhere: a flooding agent does not starve a quiet one (the quiet one is served second), bounds answer 429 with nothing stored, a restart loses nothing, and 4 workers take less than half the time of 1 |
| REST, MCP, CLI, end to end | 14 + 7 + 7 + 1 tests. The MCP server is driven as a real stdio subprocess. The end-to-end test runs from submission, through containers, approval and the library commit, to search over REST and MCP, a run in a container, and revocation |
| Index cache (R-D13) | `test_registry_cache.py`: 9 tests |

## 3. Defects the tests found, and what was done

| Found by | Defect | Fix |
|---|---|---|
| The 20-submitter burst | The standard library HTTP server's listen backlog is 5, so 20 simultaneous connections were reset | The development server's backlog is 128, recorded as a deployment setting (R-D17) |
| The bounds test | A retry from an agent at its per-submitter bound got 429 | Retries are recognised before any bound is checked (REG-ADM-12) |
| `test_no_lookup_scans_every_row` | `head` showed as a full scan | Rewritten as an indexed lookup |
| The full suite on Docker Desktop | `test_sandbox.test_network_none_leaves_only_loopback_and_allowed_leaves_more` listed interface names, and Docker Desktop's kernel has down, address-less tunnel placeholders in every namespace | The test now checks that no interface other than loopback is *up*, which is the property that matters. Outbound connections were refused throughout |
| The full suite | The TypeScript conformance test was skipped, because the first test image used Node 20 | The image now uses Node 22.11.0 and the test passes |
| The library tests | The test-only sandbox mapped `/impl` only at the start of an argument, so the build script inside `python3 -c` searched the wrong folder | It now maps every occurrence (a test double only) |

## 4. The running deployment (the Docker Desktop host, `deploy/compose.yaml`)

The registry, the dev feed and the sandbox image run as Docker Compose services. Data lives in `$TAFFEE_RUN` on that host.

- **The smoke test** (`deploy/smoke.py`), run against the live service, passes all 12 steps:
  - built-in `grep` is found at Tested;
  - an agent's tool is submitted, then built, tested in containers and admitted in about 6 s;
  - a retry returns the original submission;
  - the tool is owned by `taffee-library`;
  - search returns a model-ready definition;
  - resolve checks revocation;
  - a tool that doesn't compile is rejected, naming the file and line;
  - the tool is committed to the library;
  - MCP over stdio finds it;
  - a revocation on the feed gives 410 within seconds, and search drops the tool.
- **The data survived a full `down`/`up` cycle:** the tools admitted earlier were still served afterwards.
- **From the laptop:** REST through an SSH tunnel answered search, and gave 401 to a caller with no identity. MCP over `ssh <docker-host> docker exec -i cap-registry …` initialised, listed its three tools and found `grep`.

Defects the smoke test found, both fixed:

| Defect | Fix |
|---|---|
| Under `auto` approval, a polling agent could see `awaiting_approval` for a moment and stop, as if a person had to act | Under `auto`, that state is never stored or shown. Test: `test_under_auto_approval_an_agent_never_sees_awaiting_approval` |
| `dev-feed revoke` accepted a reason outside the feed's declared set. The client refused the entry and failed closed, so every resolve answered `RevocationStale`. That is the designed behaviour, and it shows that one bad entry stops resolution until the feed is replaced | The dev feed runs the client's own `verify_entry` before writing, and the CLI offers only the declared reasons. Test: `test_dev_feed.py` (5 tests) |

## 5. Not verified

- **The 100-submitter design goal** (G-5). Only 20 has been run.
- **Restricting submission to Taffee agents** (Q-6). The development authorizer lets any identified caller submit.
- **The `external` route** (REG-SRC-2), which is designed only.
- **A production key, TLS, real authentication, or several writers.**
- **Review.** No reviewer has read S-018.

## 6. Stability round (2026-11-15): what a crash, a restart and load do

Before the first commit, the deployment and the code were tested for what goes wrong in operation rather than in a
single request: being killed at any point, restarted, stopped, losing the feed, and loaded with bursts. Two new
test files carry it: `tests/test_crash_recovery.py` (9 tests) and `deploy/stability.sh` against the live service.

### The live deployment: `deploy/stability.sh` on the Docker Desktop host, two passing runs

| Check | Result |
|---|---|
| Baseline | smoke passes; the audit is clean |
| `docker restart` | the store head is unchanged (the start-up import writes nothing); every row verifies |
| `docker stop` | exit code 0, "registry stopped" in the log; starts again healthy |
| `docker kill` (SIGKILL) during 8 admissions (at the kill: 4 building, 4 received) | after the restart all 8 are admitted, each committed to the library once, none reported as a duplicate; no sandbox container left in any state |
| The feed stopped | resolve answers 503 (fails closed) within the 30 s bound; when the feed returns, resolve answers 200 without a registry restart |
| Manual approval | waits in `awaiting_approval`; refuses the submitter (403) and a non-approver (403); `approver:dev` approves and rejects; a rejected tool does not resolve (404) |
| 20 simultaneous submissions from 20 agents | all admitted: 89.6 s and 106.0 s on the live service (Docker Desktop also running the deployment's and other projects' containers) |
| `down` then `up` | the store head is unchanged; smoke passes again |
| Final audit | 39, then 70 rows verify; every admitted submission committed exactly once; no version listed twice; library clean and `git fsck --strict` passes; no lock, scratch or orphaned container |

### Defects found, and what was done

| Found by | Defect | Fix |
|---|---|---|
| Review for crash windows | The library's `has` looked at the working tree. A registry killed after writing a version's files but before `git commit` left files that made `sync` skip that version for good | `has` asks git (`cat-file -e HEAD:…`); `sync` lists committed rows once (`ls-tree`) |
| Review for crash windows | A stale `.git/index.lock` after a kill would fail every later commit | Opening the library removes it, discards uncommitted changes and makes the first commit if it is missing |
| Review for crash windows | A failed commit left its writes, so the retry would list the version twice in `versions.yaml` | A failed `admit` discards its writes |
| Review for crash windows | Killed between appending the row and saving the record, a resumed submission was admitted but reported as "duplicate: this exact version is already admitted" | A submission that finds its own row (same source id) is admitted with the row's decision, and no problem |
| Review for crash windows | Partly written `code/.incoming-*` folders and `work/tmp*` case folders stayed after a kill | Removed at `serve` start-up, before anything runs |
| Review | `docker stop` killed the service without shutdown, and `queue.stop` waited its timeout per worker (20 s for 4, past Docker's 10 s grace) | SIGTERM shuts the server down and stops the queue within one 5 s deadline; exit 0 |
| Review | Processing that failed for the host's reason (Docker unavailable, a container not removed in time) left the submission in `testing` until the next restart | The queue retries after 10, 30 and 90 s; then the submission stays open for `resume`. Tests: `QueueRetryTest` |
| The 20-submitter container burst (1 run in 4) | A correct tool was **rejected**: "the build step did not complete (the tool ran past its time limit)". The container sandbox started the tool's clock at `docker run`, so container creation on a busy host counted against the tool's 30 s | The limit is counted inside the container from the program's start (`timeout -k 2 <limit>`, exit 124 = timed out); the outer timer allows 60 s for start-up (CAP-SBX-4, amended). After the fix: the burst passed 3 runs in a row alone (48.9, 59.6, 64.3 s) and in both full runs (62.1, 58.9 s) |
| `stability.sh` then the full suite | `docker kill` came while the registry's docker CLI was creating a sandbox container. Docker finished creating it and nobody ever started or removed it (state `Created`); every later "nothing left behind" test on that host failed on it | Every container is labelled with its owner (`org.taffee.sandbox.owner=registry-<hash of the data folder>`), and `serve` removes its own label's containers at start-up, never another owner's. `stability.sh` now counts containers in any state. Test: `test_remove_owned_clears_a_stranded_container_and_only_its_own` |

Defects in the new scripts themselves (a quote inside `${…:?}`, the client's import path, polling as the wrong
principal) were found on their first runs and fixed; they were never in the service.

## 7. The library export made optional (2026-11-16)

The git library is now off by default and pushes to an enterprise remote when configured (R-D14 amended,
REG-LIB-0). Regression reads its cases from the store. Tests: `tests/test_library_export.py` (8).

| Run | Result |
|---|---|
| Full suite, Docker Desktop host | **Ran 1016 tests in 493.7 s, OK (skipped=1)** |
| Full suite, Linux test host | **Ran 1016 tests in 1041.7 s, OK (skipped=1)**; its container burst took 354.6 s, all correct |
| `stability.sh`, library export **off** (the default) | all checks pass; the audit confirms every admitted submission has its row and code in the store (96 at the end) and nothing was written to git; burst of 20 in 98.9 s |
| `stability.sh`, library export **on** (`TAFFEE_LIBRARY=$TAFFEE_RUN/library`), on the same data | all checks pass. Turning it on after 30 tools were admitted with it off, `sync` committed them at start-up: 127 admitted submissions, each committed exactly once, library clean; burst of 20 in 119.0 s |

The deployment was left running with the export off.

## 8. Revalidation (2026-11-22 and 2026-11-23)

The repository was rechecked against its own claims before release: the README steps in a stock Python image, the
full suite on both hosts, and the deployment checks with the library export off and on. This found the defects
below. The results in this section are for the code with every fix applied.

### Defects found, and what was done

| Found by | Defect | Fix |
|---|---|---|
| Running smoke three times in a row on the Docker Desktop host | **The store broke when a second process opened it.** On Docker Desktop for macOS, file locks do not work on the bind-mounted store. A second process that opened `registry.sqlite3` read-write removed the live registry's write-ahead log when it closed. Every later request failed (`no such table`, `disk I/O error`) until a restart: the second smoke run got 500, the third could not search. The documented IDE command (`docker exec … mcp --data …`), smoke's MCP step and the stability audit all opened the store this way. A two-container test confirmed it: a bind mount fails both across containers and within one; a named volume and a read-only connection do not | MCP answers through the running registry's REST API (`registry_cli mcp --url … --principal …`) and never opens the store. `registry_cli verify` and the deployment's audit open it read-only (`SQLiteStore(read_only=True)`: SQLite `mode=ro`, nothing created or written). Tests: `MCPOverRestTest`, `test_the_command_takes_no_store_folder`, and three read-only store tests in `test_store_contract.py` |
| Reading `stability.sh` after it reported "all checks passed" while its baseline smoke run had failed | Nine checks were written `check && pass "…"`. Under `set -e` a failure on the left of `&&` does not stop the script, so those checks could never report a failure. **The `stability.sh` results in §6 and §7 are therefore not evidence for those nine checks** | Each is now `check \|\| fail "…"` then `pass "…"`. The runs below are the first that could fail |
| The full suite, then 60 runs under CPU load | A SIGTERM that arrived while `serve` was printing its ready line ended the process without its shutdown steps (exit −2). It failed 34 runs in 60 under load | The ready line is printed inside the `try`. 60 in 60 after the fix |
| The README steps in a stock `python:3.12` image (no Docker) | `serve` without the docker command failed with a traceback, and the SIGTERM test needed Docker without saying so | One error line; the test skips and gives the reason |
| The same | The tests wrote `__pycache__` into the tools' folders | Bytecode is off for the tests (`tests/fixture_support.py`; `test_tools_t001.py` is pinned by the evidence and was not changed) |
| 50 runs of `test_admission.py` | The idempotency test passed by chance: 4 failures in 50 | Its flaky tool's output always differs between runs. 50 in 50 after the fix |
| The library export on the Linux host | **The library belonged to root.** The registry runs as root in its container, so on a Linux host every library file was root's, and the host user's git refused the repository ("dubious ownership"): it could not be read or pushed without sudo. Docker Desktop hides this, because its shared folders show the host user as owner | The library's files are given to the owner of the folder it is in, after every change; symbolic links are never followed. The registry's own git trusts the folder (`safe.directory`), as do the deployment's checks, which also run as root. A library an earlier version left root-owned is handed back when the registry opens it. A failed change of owner is logged once and never fails an admission. Tests: `OwnershipTest` (5) in `test_library_export.py` |
| The first deployment run with the ownership fix, on Linux | The deployment's checks (`deploy/smoke.py`, `deploy/client.py audit`) also run as root, and git now refused *them* the user-owned library | They pass `safe.directory` too |
| The next run, counting the library's owners at the end | One file was root's again: `.git/index`. The audit's `git status`, run as root, refreshed and rewrote it | The audit's git runs with `--no-optional-locks` and writes nothing. Checked by starting the registry on that folder (which handed the file back) and auditing three times: every file stayed the host user's |

### The runs of record (final code)

| Run | Docker Desktop host (macOS, Docker Desktop 29.9.2, linux/amd64 VM) | Linux host (Ubuntu 20.04, kernel 5.15, Docker 28.1.1, Compose 2.35.1) |
|---|---|---|
| README steps in stock `python:3.12`, no Docker | **1024 OK, 43 skipped**, each with its reason (40 container tests, 1 needing the docker command, 1 needing Node, the deliberate Context skip). Before the ownership tests were added | not run |
| Full suite with containers (README command) | **Ran 1029 tests in 648.2 s, OK (skipped=1)** | **Ran 1029 tests in 1033.0 s, OK (skipped=1)** |
| After the suite | no container left, no `__pycache__` | no container left, no `__pycache__` |
| Smoke, library export off | 12/12, three times in a row (before the fix: the second run failed) | 12/12 |
| `stability.sh`, library export off | all checks pass | all checks pass |
| Smoke and `stability.sh`, library export on | 12/12; all checks pass. Every library file is the host user's, no ownership warning, the library readable by the host user. Run before the checks' own `safe.directory` change, which Docker Desktop does not need | 12/12; all checks pass (65 admitted submissions, each committed once). Every library file is the host user's and the host user's git reads all 66 commits. No ownership warning |
| Upgrade: a library an earlier version left root-owned | — | Before: 833 of 833 files root's and the host user's git refused the repository. After the registry started on it: every file the host user's, and git reads its 33 commits. The smoke and stability runs above were on this same folder |
| Store errors in the registry's log (`no such table`, `disk I/O`) | none | none |

The one skip in the full suite is `test_graph_query`'s deliberate one (§1). The README and export-off rows ran on the code before the ownership fix (commit `cd517ef`). That fix changes only what happens when the export is on, and the export-on rows ran with it.

### The live deployment on the Docker Desktop host

Moved to the new code on 2026-11-22, its data kept (a backup was taken first). Afterwards, with only read-only
checks:
- the store head was unchanged (132 rows);
- the audit verified all 132 rows and 127 admitted submissions;
- MCP through the new command initialised, listed its tools and found them by search;
- the old command (`mcp --data …`) was refused with exit 2, so an old IDE setting can no longer break the store.

The library export stays off there.

### Still not verified

Everything in §5, and:
- **Docker Desktop for Windows.** Its shared folders are likely to have the same lock problem. Not run.
- **The registry running directly on macOS, without Docker.** The filesystem's locks work there. Not run.
- **A read-only check against a live store on Docker Desktop for macOS.** Without working locks it could catch the store mid-write and report a false error. It cannot damage the store. Stopping the registry first, or running the check again, settles it.
