# S-018 Registry store, admission and discovery: requirements

Status: implemented, except REG-SRC-2 (designed only). The results are in [07_VERIFICATION.md](07_VERIFICATION.md).

Written for: engineers building or consuming the capabilities registry, and the owners of the Orchestrator and Governance pillars.

Suite: 01 requirements (this file) · [02 decisions](02_DECISIONS.md) · [03 design](03_DESIGN.md) · [04 implementation](04_IMPLEMENTATION.md) · [05 test plan](05_TEST_PLAN.md) · [06 assumptions](06_ASSUMPTIONS.md) · [07 verification](07_VERIFICATION.md)

## 1. Purpose

Today the registry is a signed index file in git. Every call re-reads the whole file. Tools enter only by being committed, and there is no search. S-018 adds what the platform needs to run a registry as a service:

1. A tool written by **Taffee's own agent loop** is submitted with its code. It is built, tested in a sandbox and checked against Taffee's own rules, then approved and committed to **Taffee's tool library**, after which the code is Taffee's. Capabilities whose code Taffee does not own (third-party tools, external APIs, MCP servers and LLMs) are registered by reference instead (taffee-core D-013).
2. An orchestrator asks the registry what is available, by search or by listing, and gets definitions a model can use.
3. The registry holds its records in a database of its own, behind an interface, so another database can replace the default.
4. Discovery works over REST and over MCP.

Concurrency: 20 agents submitting at once is the development and test setting; 100 is the design goal, load-tested separately. Lookups do not grow with the number of tools (REG-QRY-7).

## 2. Requirements

Every requirement is a MUST unless it says otherwise. IDs are stable: a requirement that is dropped is marked withdrawn and its ID is never reused.

### REG-STORE: the store

| ID | Requirement |
|---|---|
| REG-STORE-1 | The registry service reads and writes records only through a store interface (`CapabilityStore`). No other module may run SQL or know how a store is laid out. |
| REG-STORE-2 | SQLite is the default store. It is opened in WAL mode, so readers are not blocked while an admission writes. |
| REG-STORE-3 | A second store, in memory, implements the same interface. Both pass the same contract test suite. A new database is supported only when it passes that suite. |
| REG-STORE-4 | `(kind, name, version)` is unique. An admitted row is never changed or deleted through the interface. |
| REG-STORE-5 | Tool code is stored as files, named by the tree digest (`sha256-tree-v1`) of their content. A store never hands out a code folder whose digest it has not checked on write. |
| REG-STORE-6 | Submissions, with their state, problems and decisions, are stored through the same interface as rows. |

### REG-ROW: signed rows

| ID | Requirement |
|---|---|
| REG-ROW-1 | Each admitted version is one row (`capability-row/1`). It holds the manifest, `manifest_digest`, the tool identity, each implementation's code digest and entry, the label, the evidence digests, its source, the submitter, the approver and the admission time. |
| REG-ROW-2 | Each row is signed with Ed25519 by the admission key. The signature covers the row's canonical JSON, without the `sig` field. |
| REG-ROW-3 | Rows form a hash chain: each row carries `seq` (1, 2, …) and `prev`, the hash of the previous row (a fixed genesis value for row 1). The store refuses a row whose `seq` or `prev` does not follow the current head. |
| REG-ROW-4 | Every read the registry acts on verifies the signature of each row it uses. A row that fails is an `IntegrityError` and is never served. |
| REG-ROW-5 | The service can issue a signed checkpoint `{count, head, issued_at}`. `verify_store` checks every signature, the whole chain and the checkpoint, and so detects a deleted, reordered or inserted row. |
| REG-ROW-6 | A reader holds only the public key. The private key exists only in the admission service. |

### REG-ADM: admission

| ID | Requirement |
|---|---|
| REG-ADM-1 | A submission is a bundle: a tool manifest, files for each implementation, and conformance cases. Its size is limited by a setting of the deployment. Only a principal the authorizer recognises as a Taffee agent may submit code. |
| REG-ADM-2 | Every bundle path is relative, contains no `..`, and is under a language folder named in the manifest. Each language has the entry file that `policy/languages.yaml` names. |
| REG-ADM-3 | A submitted manifest passes the same validation as a committed one (`validate_manifest`), plus admission rules: every `inputSchema` property has a description, a `spec.sandbox` block is declared, and each implementation's `location` is `library/<name>/<language>`. |
| REG-ADM-4 | Submitted code is never run outside a sandbox. Conformance cases run under the tool's declared `SandboxPolicy`, through `Sandbox.run`. A sandbox that cannot enforce that policy refuses the run (`SandboxUnavailable`), and there is no fallback. |
| REG-ADM-5 | Every case must pass for every implementation. One failure rejects the submission, with each failing case named. |
| REG-ADM-6 | A submission that passes waits for approval. Approval is given by a principal the authorizer allows to approve, and that principal is not the submitter. Only an approved submission becomes a row. |
| REG-ADM-7 | A rejected or unapproved submission never appears in list, search, describe or resolve. |
| REG-ADM-8 | Versions are immutable. Submitting a `(name, version)` that is already admitted with a different identity is refused with `VersionConflict`. With the same identity, the existing row is returned and nothing new is written. |
| REG-ADM-9 | A submission moves through `received → validating → building → testing → awaiting_approval → admitted`, or ends in `rejected`. Each state change is committed to the store as it happens (`synchronous=FULL`), so a restart or a power loss leaves no submission half-admitted. |
| REG-ADM-10 | Admission awards `Validated`. Its evidence records digests of the build, the cases (including regression cases) and the run. |
| REG-ADM-11 | Admission problems are closed text, naming the field, file and line, or case. Output from the submitted code is never copied into a problem or a log. |
| REG-ADM-12 | **Retries.** A submission that repeats an earlier one byte for byte (the same canonical bundle, from the same submitter) returns the earlier submission, in whatever state it is in. Nothing is stored or run again, and no bound refuses it. |
| REG-ADM-13 | **Built-in names.** A submission may not use the name of a built-in tool (source `file-index`). Built-in tools change only through the reviewed repository. |
| REG-ADM-14 | **Fairness.** Waiting submissions are processed round-robin across submitters, so one agent's burst does not delay another agent's single submission behind it. |
| REG-ADM-15 | **Bounds.** At most `max_queued` submissions wait in total, and at most `max_open_per_submitter` are open per submitter. Past either bound, a new submission is refused with `Busy` (429 with `Retry-After`) before anything is stored. |
| REG-ADM-16 | **Parallelism.** `workers` submissions are processed at once. No more sandbox runs than that are in flight at any time. |
| REG-ADM-17 | **Build.** Before any case runs, every source file is compiled in the sandbox without being run (Python: a syntax compile; Bash: `bash -n`). Code that does not compile is rejected, naming the file and line. |
| REG-ADM-18 | **Taffee's checks.** These are the opinionated rules, and the submitter does not choose them: every case's input must match `inputSchema`; every required parameter must be exercised; at least one case must expect success and one a tool-level error; every run must answer with exactly one result line (the tool contract); and a tool declared `idempotent` must give the same answer to the same case twice. |
| REG-ADM-19 | **Regression.** A new version must also pass every case of every admitted version with the same major version. A new major version starts a new suite. The cases come from the store, so regression works whether or not the library export is on. |
| REG-ADM-20 | **Ownership.** On admission the code becomes Taffee's: the manifest's owner is set to `taffee-library` before the identity is computed, the submitter is kept as provenance (`source.provenance: taffee-agent`, `submitted_by`), and, if the library export is on, the version is committed to the library repository (REG-LIB). |
| REG-ADM-21 | **Approval policy.** A deployment setting. `manual`: a named approver who is not the submitter decides. `auto`: a tool is admitted the moment every check passes, recorded as approved by `policy:auto`, and `awaiting_approval` is never stored or shown, so an agent polling its submission never waits for a person who does not exist. The principal names `policy:*` and `file-index` are reserved and cannot submit or approve. |

### REG-LIB: Taffee's tool library

| ID | Requirement |
|---|---|
| REG-LIB-0 | The library export is a deployment setting, **off by default**. With it off, nothing is written to git and admission, regression, search and resolve work the same. With it on, it may push to a remote the deployment configures, with the deployment's own credentials; a failed push never fails an admission and is caught up by the next push. |
| REG-LIB-1 | When on, Taffee's tool library is a git repository written only by the registry. Each admitted version is one commit, made when the version is admitted. The commit is authored by the registry and carries the submission, identity, approver, submitter and row sequence as trailers. |
| REG-LIB-2 | The library holds, for each tool, the current manifest, the version ledger, every version's signed row, the current code, and the regression suite (every case of every version). The library alone is enough to rebuild the store. |
| REG-LIB-3 | The signed row is the record. If a commit fails after the row is written, `sync` commits every missing version at the next start. It is idempotent. Only a commit counts: a version whose files were written but not committed, because the process was killed, is not in the library. Opening the library recovers from a killed writer (a stale `index.lock`, uncommitted files, a missing first commit), and a failed commit discards its own partial writes, so a retry lists each version once. |
| REG-LIB-4 | Code in the library is never run outside the sandbox. |

### REG-SRC: where a capability comes from

| ID | Requirement |
|---|---|
| REG-SRC-1 | Every row records its source: `submission` (written by Taffee's agent loop and owned in the library), `file-index` (built-in), or, once built, `external` (registered by reference). |
| REG-SRC-2 | *(Designed, not built.)* External capabilities (third-party tools, external APIs, MCP servers, LLMs) are registered by reference. They are pinned by digest or addressed by endpoint, tested against their declared contract, never copied into the library and never owned by Taffee. |

### REG-QRY: queries

| ID | Requirement |
|---|---|
| REG-QRY-1 | `list` returns pages, ordered by `(kind, name, version)`. The cursor is opaque and the page size is bounded. |
| REG-QRY-2 | `describe(name, version?)` returns one version (the newest when none is given), the caller's allowed view of its manifest, and a model-facing `tool_definition` `{name, description, input_schema}`. |
| REG-QRY-3 | `search(query)` returns the newest admitted version of each tool whose name, summary or parameter text matches every query term (prefix matching). An exact name match ranks first. Results can be filtered by kind, minimum label and language, and are bounded and paged. |
| REG-QRY-4 | Search treats the query only as words. No query can reach the store's query language: no SQL or FTS syntax, and no `*`, `"`, `NEAR` or column filters. |
| REG-QRY-5 | `resolve(name, range, min_label, languages)` returns an inert match. It verifies the row, recomputes the chosen implementation's code digest, and checks revocation by identity. It fails closed. |
| REG-QRY-6 | The store-backed registry cannot be built without a revocation client. A revoked version does not resolve, and is left out of search and list when the client knows it is revoked. |
| REG-QRY-7 | The time a query takes does not grow with the number of rows. Lookups by key and search use indexes, never a scan of every row (checked with `EXPLAIN QUERY PLAN`). |

### REG-API: REST

| ID | Requirement |
|---|---|
| REG-API-1 | These endpoints exist under `/v2`: `GET /tools`, `GET /tools/{name}`, `GET /tools/{name}/versions`, `GET /search`, `POST /resolve`, `POST /submissions`, `GET /submissions/{id}`, `POST /submissions/{id}/approve`, `POST /submissions/{id}/reject` and `GET /checkpoint`. |
| REG-API-2 | Every request is identified by the injected authorizer, and there is no default authorizer. Reads apply `may_see` before any other step, so hidden and absent entries get the same answer. Submit and approve are separate permissions. |
| REG-API-3 | Responses are built from allow-lists of fields. No path, sandbox block or code is returned. |
| REG-API-4 | Every error has a code, a fixed message and an HTTP status. No exception text is returned. |
| REG-API-5 | Request bodies, query strings and page sizes have limits set by deployment settings. A request over a limit is refused before it is parsed. |

### REG-MCP: MCP

| ID | Requirement |
|---|---|
| REG-MCP-1 | An MCP server (JSON-RPC 2.0 over stdio) offers three read-only tools: `search_capabilities`, `describe_capability` and `list_capabilities`. |
| REG-MCP-2 | MCP answers come from the same query layer as REST, with the same visibility rules, for a principal set when the server is started. |
| REG-MCP-3 | The MCP server cannot submit, approve, resolve to code, or run anything. |

### REG-COMPAT: compatibility

| ID | Requirement |
|---|---|
| REG-COMPAT-1 | The file index, `Registry`, `capctl verify`, the catalog generator and the S-008 service keep working unchanged, and the existing suite still passes. |
| REG-COMPAT-2 | `run_tool` runs a tool resolved by either registry. |
| REG-COMPAT-3 | `import-index` copies every entry of a verified file index into a store as signed rows (source `file-index`), keeping the labels and evidence that the index carries. |
| REG-COMPAT-4 | A tool has the same identity whether it is committed to the file index or submitted to the store. Both use `tool_identity`. |

## 3. Not required

- **Writing tools inside the capabilities pillar.** Taffee's agent loop writes tools; this pillar checks, owns and serves them (D-013).
- **Agents and models through admission.** The row format allows every kind, but admission accepts only tools in S-018.
- **Awarding `Tested` through admission.** An evidence format for a suite run outside Taffee is not defined. Imported rows keep `Tested` when the file index gave it.
- **Delivering code to a remote orchestrator.** S-018 resolves to a local code folder, and the orchestrator runs code in-process through `run_tool`.
- **Production hardening:** TLS, real authentication, rate limiting and a highly available database. The REST service is a development reference, like S-008.
- **Semantic (embedding) search.** Search matches words only.
- **Load-testing at 100 concurrent submitters.** That is the design goal; it is tested separately, after the rest is accepted.
- **External capabilities** (REG-SRC-2). Designed only.
- **Compiled languages.** The supported languages are interpreted; the build step is a compile check.
- **Deleting or editing an admitted row.** Withdrawing a version is revocation, through the S-003 feed.

## 4. Open questions

| ID | Question | Where it matters |
|---|---|---|
| Q-1 | Where should a production admission key live (HSM, KMS or file), and how is it rotated? S-018 accepts a key the deployment supplies. | REG-ROW-2, REG-ROW-6 |
| Q-2 | How does `Validated` become `Tested` for a tool from outside Taffee? | REG-ADM-10 |
| Q-3 | Should a tool declare its output format and error cases in its manifest? The schema has no field for either today. | REG-QRY-2 |
| Q-4 | How does a remote orchestrator get the code it is allowed to run (signed archive, OCI image, …)? | REG-QRY-5 |
| Q-5 | Which approval policy does production use, and does it depend on the tool's risk class (for example `auto` only for read-only tools with no network)? This is Governance's decision. | REG-ADM-6, REG-ADM-21 |
| Q-6 | How is a Taffee agent principal recognised? That needs Governance's identity contract. Today the development authorizer allows any identified caller. | REG-ADM-1 |
| Q-7 | When is unreferenced code (from rejected submissions) removed from the code area, and after what grace period? | REG-STORE-5 |

## 5. Traceability

Every ID above appears in [03_DESIGN.md §9](03_DESIGN.md#9-traceability) and in [05_TEST_PLAN.md](05_TEST_PLAN.md), mapped to the named test cases.
