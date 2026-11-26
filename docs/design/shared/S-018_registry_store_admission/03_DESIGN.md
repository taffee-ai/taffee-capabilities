# S-018 design

Suite: [01 requirements](01_REQUIREMENTS.md) · [02 decisions](02_DECISIONS.md) · 03 design (this file) · [04 implementation](04_IMPLEMENTATION.md) · [05 test plan](05_TEST_PLAN.md) · [06 assumptions](06_ASSUMPTIONS.md) · [07 verification](07_VERIFICATION.md)

## 1. Components

```
 Taffee agent loop (Orchestrator)              taffee-capabilities                                     other pillars
 ────────────────────────────────              ───────────────────                                     ─────────────
 tool-writing agent ── POST /v2/submissions ─┐
 approver (manual policy) ── POST …/approve ─┤
                                             ▼
                            ┌──────────────────────────┐  reserve/enqueue  ┌────────────────────────┐
                            │ registry_api.py  /v2     │──────────────────►│ admission_queue.py      │
                            │ authorizer, allow-lists, │  retry? → answer  │ N workers, round-robin  │
                            │ 429 Busy, bounded bodies │                   │ per submitter, bounded, │
                            └───────────┬──────────────┘                   │ resume after restart    │
                                        │                                  └───────────┬────────────┘
 orchestrator ── GET /v2/search ────────┤                                              ▼
 agent / IDE ─── MCP stdio ──► mcp_server.py                           ┌──────────────────────────┐
                                        │                              │ admission.py             │──► sandbox.py
                                        │                              │ validate → build → test  │    ContainerSandbox:
                                        │                              │ (+ Taffee checks,        │    build step, cases,
                                        │                              │  regression) → approve   │    idempotency re-run
                                        │                              │ (manual | auto)          │
                                        │                              └──────┬─────────┬─────────┘
                                        ▼                                     │ signed  │ commit
                            ┌──────────────────────────┐   rows.py            ▼ row     ▼
                            │ store_registry.py        │◄──────────── CapabilityStore   library_repo.py
                            │ verify sig + digests,    │              ├ SQLiteStore      git: Taffee's tool
                            │ search/list/describe/    │              └ MemoryStore      library (owned code,
                            │ resolve, revocation      │◄── revocation.py ◄── Governance feed   ledger, rows,
                            └───────────┬──────────────┘                                  regression suite)
                                        ▼
 orchestrator (in-process) ── execute.run_tool ── sandbox ── callrecord (S-012)

 unchanged: index.py → index/index.json → registry.py (cached) → capctl, catalog_export, service.py (/v1)
 bridge: serve builds the repository's index and import-index brings the built-in tools into the store
```

### Where a capability comes from (R-D16)

| Source | Written by | Route | Owned by Taffee | In the library |
|---|---|---|---|---|
| `submission` | Taffee's agent loop | Full admission (§3) | Yes | Yes, one commit per version |
| `file-index` | Taffee engineers (built-in) | Reviewed git, then `import-index` | Yes | No; it stays in this repository's `library/` |
| `external` *(designed)* | Third parties: tools, APIs, MCP servers, LLMs | Registration by reference: pinned digest or endpoint, contract tests | No | Never |

### Submission states

```
received ─► validating ─► building ─► testing ─► awaiting_approval ─► admitted
    │            │            │           │              │      ▲ (auto policy: at once)
    └────────────┴────────────┴───────────┴──────────────┴──► rejected (named problems)
```

What each component does:

| Component | Holds | Does not |
|---|---|---|
| `store/*` | Rows, submissions, code folders | Verify signatures, decide labels |
| `rows.py` | The row format, signing, chain hashes, the checkpoint | Talk to a store |
| `admission_queue.py` | Ordering, parallelism, bounds | Check tools |
| `admission.py` | The submission state machine and Taffee's checks | Serve reads |
| `library_repo.py` | Taffee's library: the git repository of owned code | Decide admission |
| `store_registry.py` | Verified queries, and resolve to an inert match | Write |
| `registry_api.py`, `mcp_server.py` | Transport, authorizer, allow-list views | Logic beyond the query layer |

## 2. Data contracts

### 2.1 Row (`capability-row/1`)

```json
{
  "schema": "capability-row/1",
  "seq": 42,
  "prev": "sha256:<hash of row 41, or GENESIS>",
  "kind": "tool",
  "name": "word-count",
  "version": "1.0.0",
  "manifest": { "...": "the normalised manifest (implementations sorted)" },
  "manifest_digest": "sha256:…",
  "identity": "sha256:… (tool_identity)",
  "implementations": [{"language": "python", "code_digest": "sha256:…", "entry": "main.py"}],
  "label": "Validated",
  "evidence": ["conformance:sha256:…", "run:sha256:…"],
  "source": {"type": "submission", "id": "sub-…", "provenance": "taffee-agent"},
  "key_id": "ed25519:<first 16 hex of sha256(public key)>",
  "submitted_by": "author:acme",
  "admitted_by": "approver:sec-1",
  "admitted_at": "2026-11-15T10:00:00Z",
  "sig": "<base64 Ed25519 signature over the canonical JSON of every field above>"
}
```

- **Imported rows:** `source` is `{"type": "file-index", "index_digest": "sha256:…"}`, and `submitted_by` and `admitted_by` are `"file-index"`.
- **Row hash:** `sha256(canonical_bytes(row))`, *including* `sig`, so the next row's `prev` also commits to this signature.
- **Genesis:** `sha256("taffee-capability-row-genesis/1")`.

### 2.2 Checkpoint (`capability-checkpoint/1`)

`{"schema": "capability-checkpoint/1", "count": N, "head": "<row hash of seq N>", "issued_at": "…", "sig": "…"}`

### 2.3 Submission bundle (`capability-submission/1`)

```json
{
  "schema": "capability-submission/1",
  "manifest": { "kind": "tool", "name": "word-count", "version": "1.0.0", "...": "",
                "implementations": [{"language": "python", "location": "library/word-count/python"}],
                "spec": { "inputSchema": { "...": "every property has a description" },
                          "sandbox": {"filesystem": "read-root"}, "...": "" } },
  "files": { "python": { "main.py": "<base64>" } },
  "cases": [ { "id": "counts", "input": {"...": ""}, "files": {"a.txt": "…"}, "expect": {"ok": true, "contains": "…"} } ]
}
```

Case format and expectation operators are the existing conformance format (`conformance.py`: `ok`, `contains`, `not_contains`, `json_key`, `exit_code`). Every case is given the tool contract `{"root": "/work", "input": …}`. A case without `files` is given an empty repository.

### 2.4 Submission record

`{id, state, key, submitted_by, submitted_at, name, version, manifest, cases, code: {language: digest}, identity?, evidence, problems: [str], decided_by?, decided_at?, row_seq?, library_commit?}`, where `state` is one of `received | validating | building | testing | awaiting_approval | admitted | rejected`, and `key` is the fingerprint used to recognise retries.

### 2.5 Store interface (`store/base.py`)

```python
class CapabilityStore(ABC):
    # rows
    def head(self) -> tuple[int, str]: ...                                  # (seq, row hash); (0, GENESIS) when empty
    def append_row(self, row: dict) -> None: ...                            # refuses StoreConflict: seq/prev not at head, duplicate key
    def get_row(self, kind, name, version) -> dict | None: ...
    def versions(self, kind, name) -> list[dict]: ...                       # all versions, oldest first
    def rows_page(self, kind, after: tuple | None, limit) -> list[dict]: ...# keyset on (kind, name, version)
    def search(self, terms: list[str], kind, limit, offset) -> list[dict]: ...  # newest version per name
    def rows_from(self, seq: int, limit: int) -> list[dict]: ...            # in chain order, for verify_store
    def count(self) -> int: ...
    # code
    def put_code(self, files: dict[str, bytes]) -> str: ...                 # returns the tree digest
    def code_path(self, code_digest) -> Path | None: ...
    # submissions
    def save_submission(self, record: dict) -> None: ...                    # insert or replace by id
    def get_submission(self, sid) -> dict | None: ...
    def submissions(self, state=None) -> list[dict]: ...
```

`StoreConflict` and `StoreError` are the only exceptions the interface raises. The contract tests (`tests/test_store_contract.py`) define what each method must do, including ordering, uniqueness, append-at-head and the search rules.

### 2.6 Query layer (`store_registry.py`)

```python
StoreRegistry(store, public_key, revocation, languages=load_languages(repo), visible=None)
  .list(kind="tool", min_label="Declared", cursor=None, limit=50) -> {"items": [...], "next": cursor|None}
  .search(query, kind="tool", min_label="Declared", language=None, limit=10, cursor=None) -> same shape
  .describe(name, version=None, kind="tool") -> {..., "tool_definition": {...}}
  .versions(name, kind="tool") -> [...]
  .resolve(name, range="*", kind="tool", minLabel="Declared", languages=[...]) -> match
  .languages() -> dict          # used by run_tool
```

The match keeps the S-002 keys `name, kind, version, label, manifest_digest, code_digest, language, spec, revocation, revocation_seq`, and adds `identity`, `implementation_path` (the absolute code folder) and `entry`. It never runs code.

### 2.7 REST (`/v2`)

| Method and path | Permission | 2xx body |
|---|---|---|
| `GET /v2/tools?cursor&limit&min_label` | `may_see` per row | `{items, next}` |
| `GET /v2/tools/{name}?version` | `may_see` | describe view |
| `GET /v2/tools/{name}/versions` | `may_see` | `{items}` |
| `GET /v2/search?q&limit&cursor&min_label&language` | `may_see` | `{items, next}` |
| `POST /v2/resolve` `{name, range, min_label, languages}` | `may_see` | match facts, without `implementation_path` |
| `POST /v2/submissions` (bundle) | `may_submit` | `202 {id, state}` |
| `GET /v2/submissions/{id}` | the submitter, or `may_approve` | the submission record |
| `POST /v2/submissions/{id}/approve` and `/reject` `{reason?}` | `may_approve`, and not the submitter | the submission record |
| `GET /v2/checkpoint` | any identified caller | the checkpoint |

Each error is `{code, message}` with a fixed message:

| Code | Status |
|---|---|
| `NotFound` | 404 |
| `Ambiguous`, `BelowLabel` | 409 |
| `InvalidRange`, `InvalidRequest`, `TooLarge` | 400 or 413 |
| `Unauthenticated` | 401 |
| `Forbidden` | 403 |
| `Revoked` | 410 |
| `RevocationStale` | 503 |
| `VersionConflict` | 409 |
| `IntegrityError` | 500 |
| `NoImplementation` | 404 |
| `Unexpected` | 500 |

### 2.8 MCP (`mcp_server.py`)

- **Protocol:** JSON-RPC 2.0, one message per line on stdio, protocol version `2025-06-18`.
- **Source of answers:** the running service's `/v2` REST API (`RestRegistry`, `registry_cli mcp --url`), for the principal fixed at start. The MCP server never opens the store. A second process on the SQLite file is unsafe where file locks do not work across processes: on Docker Desktop for macOS bind mounts, its closing connection removed the live registry's WAL (found 2026-11-22). Checks that must read the store directly (`verify`, the stability audit) open it read-only.
- **Methods:** `initialize`, `notifications/initialized`, `ping`, `tools/list` and `tools/call`.
- **Tools:**
  - `search_capabilities {query, limit?, min_label?}`
  - `describe_capability {name, version?}`
  - `list_capabilities {cursor?, limit?}`
- **Results:** each result has `content: [{type: "text", text: <json>}]` and `structuredContent`. A failure has `isError: true` and the error code.

## 3. Happy path: admit a tool written by Taffee's agent loop

```
agent        registry_api     queue        admission           sandbox        store        library (git)
  │ POST /v2/submissions │       │             │                   │              │               │
  │─────────────────────►│ find_retry? (none)  │                   │              │               │
  │                      │ reserve ────►│ room?│                   │              │               │
  │                      │ submit ────────────►│ parse, put_code ─────────────────►│ code/<hex>    │
  │                      │                     │ save(received) ──────────────────►│               │
  │◄──── 202 {id} ───────│ enqueue ───►│       │                   │              │               │
  │                      │             │ worker│ (round-robin)     │              │               │
  │                      │             │──────►│ validating: manifest rules, coverage rules,       │
  │                      │             │       │   owner := taffee-library, identity, BuiltIn?    │
  │                      │             │       │ building: compile ─────────────►│ container      │
  │                      │             │       │ testing: own cases + regression (same major) ─► │
  │                      │             │       │   contract line check, idempotent re-run ──────►│
  │                      │             │       │ save(awaiting_approval) ───────────────────────►│
  │                      │             │       │ approve (policy:auto, or a named approver later) │
  │                      │             │       │ sign row at head ─────────────────►│ append_row   │
  │                      │             │       │ commit ──────────────────────────────────────────►│ "Admit name ver"
  │                      │             │       │ save(admitted, row_seq, library_commit) ──────►│  │
  │ GET …/{id} ─────────►│ ◄───────────────────┼── admitted                         │              │
```

## 4. Happy path: an orchestrator finds and runs a tool

```
orchestrator          registry_api / store_registry        store        revocation      sandbox
  │ GET /v2/search?q=word count │                            │               │               │
  │────────────────────────────►│ terms → store.search ─────►│ FTS5 query    │               │
  │                             │ verify sig of each row     │               │               │
  │                             │ may_see; drop known revoked ───────────────►│ check(local)  │
  │◄── items + tool_definition ─│                            │               │               │
  │ model picks word-count       │                            │               │               │
  │ run_tool(StoreRegistry, …)   │                            │               │               │
  │────────────────────────────►│ resolve: get_row, verify sig, choose impl  │               │
  │                             │ code_path → tree_digest == code_digest?    │               │
  │                             │ revocation.check(identity) ───────────────►│ fresh: ok     │
  │◄── match (inert) ───────────│                            │               │               │
  │ validate input, then sandbox.run(policy from the verified spec) ─────────────────────────►│
  │◄──────────────────────────────────────────────────────────────── result, call record ────│
```

## 5. Failure and short-circuit paths

### 5.1 A failing case rejects the submission

```
admission ── run case "counts" ──► sandbox ──► RunResult(ok:false)
admission: check_expectations → ["counts: ok: wanted True, got False"]
admission ── save(rejected, problems=["python/counts: ok: wanted True, got False"]) ──► store
(no row; search, describe and resolve never see it; GET /v2/submissions/{id} shows the named case)
```

### 5.2 A sandbox that cannot enforce the policy: no fallback

```
admission ── policy_from_spec(spec) = read-root ──► ProcessSandbox.run
ProcessSandbox: required(policy) − enforces = {filesystem, network} ─► raise SandboxUnavailable
admission ── save(rejected, problems=["sandbox cannot enforce: filesystem, network"])
(the code never ran)
```

### 5.3 A tampered row: fails closed at read

```
attacker edits rows.label in SQLite: "Validated" → "Tested"
store_registry.describe ─► store.get_row ─► verify_row(sig) fails ─► IntegrityError (500)
store_registry.search: the bad row is left out and the miss is logged; verify_store reports it
```

### 5.4 Revocation: fail-closed and fail-open diverge on purpose

```
                 feed fresh                         feed stale (no checkpoint within the bound)
search/list:     revoked versions left out          answers; revocation not applied     (discovery: open)
resolve:         Revoked (410)                      RevocationStale (503)               (execution: closed)
```

### 5.5 Version conflict

```
POST /v2/submissions word-count@1.0.0 (different code) ─► admission: get_row exists, identity differs
─► rejected, problems=["VersionConflict: word-count@1.0.0 is admitted with another identity; bump the version"]
same code again ─► the existing row is returned, nothing new is written (REG-ADM-8)
```

### 5.6 Self-approval

```
POST …/approve by the submitter ─► 403 Forbidden; the state stays awaiting_approval
POST …/approve as "policy:auto" under the manual policy ─► 403 Forbidden (reserved principal)
```

### 5.7 A burst: retries, bounds and fairness

```
20 agents POST at the same instant
  ├─ a retry of an earlier bundle ─► find_retry ─► 200 {the original id, duplicate: true} (no bound applies)
  ├─ the submitter is at max_open_per_submitter ─► 429 Busy, Retry-After: 10 (nothing stored)
  ├─ the queue holds max_queued ─► 429 Busy (nothing stored)
  └─ otherwise ─► 202; queued per submitter; workers take one from each submitter in turn
```

### 5.8 A new version fails the regression suite

```
agent:b submits word-count 1.1.0 (its own cases pass)
admission: regression = the library's cases for word-count with major 1 ─► run each in the sandbox
           "python/regression 1.0.0/counts-a-file: ok: wanted True, got …" ─► rejected; 1.0.0 stays newest
```

### 5.9 The library commit fails after the row is written

```
approve: append_row ok ─► library.admit raises ─► submission admitted, library_commit: null (logged)
next start: sync_library ─► the row is missing from the library ─► commit it ─► library_commit known
```

### 5.10 The registry is killed (`docker kill`, power loss)

Any instruction can be the last. The store commits each step (`synchronous=FULL`); everything else is rebuilt
from it at the next `serve`, in this order:

```
serve start
  ├─ LibraryRepo(): stale .git/index.lock? ─► remove; uncommitted files? ─► reset --hard, clean
  │                 (a version is in the library only if it is committed: `has` asks git)
  ├─ store.remove_incomplete_code(): .incoming-* folders ─► remove
  ├─ admission.clean_work(): case folders left in work/ ─► remove
  ├─ sandbox.remove_owned(): containers labelled org.taffee.sandbox.owner=registry-<hash of the data folder>
  │                          ─► remove (one left in Created because the kill came while Docker was creating it)
  ├─ sync_library(): rows not committed ─► commit each once
  └─ queue.resume(): submissions in received…testing ─► process again
        └─ the row already exists and its source is this submission (killed between append and save)
              ─► admitted, decided_by from the row; nothing new is written; not reported as a duplicate
```

A sandbox container that was already running at the kill runs to its own time limit and is removed (`--rm`);
its result is discarded and the case runs again. One that Docker was still creating is never started and never
removed by Docker: that is what the owner label is for. Containers of any other owner on the same host are not
touched.

Processing that fails for a reason that is not the submission's (Docker down, a container that cannot be
removed in time) is retried by the queue after 10, 30 and 90 s; after that the submission stays open in the
store and the next start resumes it. The submission is never rejected for the host's failure. `docker stop` (SIGTERM) stops answering, gives workers 5 s, and exits 0;
whatever did not finish is resumed the same way.

## 6. Storage layout

Where the data lives (R-D19):

| What | Development default | Deployed |
|---|---|---|
| Store (`registry.sqlite3`, `code/<hex>/`) | `var/registry/` in this repository (gitignored) | `/var/lib/taffee-capabilities/registry` (`TAFFEE_CAP_DATA`) |
| Taffee's library (git) | `var/library/` | `/var/lib/taffee-capabilities/library` (`TAFFEE_CAP_LIBRARY`) |

The library's layout:

```
catalog/tool/<name>/manifest.yaml        current version (owner: taffee-library)
catalog/tool/<name>/versions.yaml        ledger: version → identity
catalog/tool/<name>/rows/<version>.json  every version's signed row
library/<name>/<language>/               current code
library/<name>/conformance.json          regression suite: "<version>/<case id>" for every version
```

The SQLite store's schema:

```sql
rows(seq INTEGER PRIMARY KEY, kind TEXT, name TEXT, version TEXT, major INT, minor INT, patch INT,
     label_rank INT, languages TEXT, hash TEXT UNIQUE, body TEXT,       -- body = canonical JSON of the signed row
     UNIQUE(kind, name, version));
CREATE INDEX rows_by_name ON rows(kind, name, major, minor, patch);   -- every lookup is indexed: test_no_lookup_scans_every_row
latest(kind TEXT, name TEXT, seq INT, PRIMARY KEY(kind, name));          -- newest version per name, for search
rows_fts USING fts5(name, summary, params, content='', tokenize='unicode61');  -- rowid = seq of the latest version
code(digest TEXT PRIMARY KEY, created_at TEXT);                           -- folders under <data>/code/<hex>/
submissions(id TEXT PRIMARY KEY, state TEXT, submitter TEXT, sub_key TEXT, submitted_at TEXT, body TEXT);  -- index (submitter, sub_key) for retries
meta(key TEXT PRIMARY KEY, value TEXT);                                   -- schema version
```

The indexed columns (`label_rank`, `languages`, version parts) are only filters. The registry never trusts them: every row it serves is re-read from `body` and verified, and a filter that a tampered column let through is caught at that point.

## 7. Settings

Each setting is chosen by the deployment. The defaults are for development, and none comes from workload measurement:

- `approval` (`auto` in development, `manual` available)
- `workers` (4)
- `max_queued` (200)
- `max_open_per_submitter` (20)
- the HTTP listen backlog (128)
- `max_bundle_bytes`
- `max_cases`
- `max_files`
- `page_limit_max`
- `search_limit_max`
- the sandbox image
- `TAFFEE_CAP_DATA`
- the library export (`--library`, `TAFFEE_CAP_LIBRARY`; **off by default**), its remote (`--library-remote`) and SSH credentials (`--library-ssh-key`, `--library-known-hosts`)
- the admission key file
- the revocation public key

The dev and test concurrency is 20 submitters; the design goal is 100.

## 8. Not resolved here

- **Code delivery to a remote orchestrator** (Q-4). S-018 resolves to a local folder.
- **Where the admission key lives and how it is rotated** (Q-1). S-018 loads one key from a file.
- **`Tested` for outside tools** (Q-2).
- **Output and error contracts** (Q-3).
- **Approval policy by risk class** (Q-5).
- **Per-read proof that a row is in the chain.** Today deletion is caught by `verify_store`, not by each read (R-D03).
- **Several writers or replicas.** SQLite allows one writer, and Postgres behind the same interface is future work.
- **Agents and models through admission.**
- **The `external` route** (R-D16): registration by reference, and its contract tests.
- **How a Taffee agent principal is recognised** (Q-6): this needs Governance's identity contract.
- **Clean-up of unreferenced code** (Q-7).
- **Load-testing at 100 concurrent submitters.**

## 9. Traceability

| Requirement | Design section | Decision |
|---|---|---|
| REG-STORE-1…6 | §2.5, §6 | R-D04, R-D05 |
| REG-ROW-1…6 | §2.1, §2.2, §5.3 | R-D02, R-D03 |
| REG-ADM-1…3 | §2.3, §3 | R-D08 |
| REG-ADM-4, 5 | §3, §5.1, §5.2 | R-D06 |
| REG-ADM-6, 7 | §3, §5.6 | R-D09 |
| REG-ADM-8 | §5.5 | — |
| REG-ADM-9…11 | §2.4, §3, §5.10 | R-D07 |
| REG-QRY-1…4, 7 | §2.6, §4, §6 | R-D10 |
| REG-QRY-5, 6 | §4, §5.4 | R-D11 |
| REG-API-1…5 | §2.7 | R-D12 |
| REG-MCP-1…3 | §2.8 | — |
| REG-COMPAT-1…4 | §1 | R-D12, R-D13 |
| REG-ADM-12, 14, 15, 16 | §5.7 | R-D17 |
| REG-ADM-13, 19 | §5.8 | R-D15 |
| REG-ADM-17, 18 | §3 | R-D18 |
| REG-ADM-20, REG-LIB-1…4 | §3, §5.9, §5.10, §6 | R-D14, R-D19 |
| REG-ADM-21 | §3, §5.6 | R-D09 |
| REG-SRC-1, 2 | §1 | R-D16 |
