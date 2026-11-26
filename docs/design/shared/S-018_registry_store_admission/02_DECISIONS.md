# S-018 decisions

Suite: [01 requirements](01_REQUIREMENTS.md) · 02 decisions (this file) · [03 design](03_DESIGN.md) · [04 implementation](04_IMPLEMENTATION.md) · [05 test plan](05_TEST_PLAN.md) · [06 assumptions](06_ASSUMPTIONS.md) · [07 verification](07_VERIFICATION.md)

Each entry gives the context, the decision, why, the alternatives considered and its status. These are decisions inside the capabilities pillar. Which capabilities Taffee owns (those written by its own agent loop) and which it registers by reference is cross-pillar, and is taffee-core D-013.

## R-D01: Admission goes through an API, and ends in a signed row in a store

- **Context:** the earlier design ended admission in a hand-made git commit (T-003). Taffee's agents will submit tools in bursts, so a pull request for each one cannot keep up.
- **Decision:** `POST /v2/submissions` starts admission. An admitted version becomes a signed row in the store.
- **Why:** an outside author needs a programmatic route, and a store can be queried by key and searched.
- **Alternatives:** a git pull request for each tool, which does not scale and needs repository access for every author. A signed index file rebuilt on each admission, which costs time in proportion to the number of tools and is the current bottleneck.
- **Status:** accepted.

## R-D02: Trust comes from the row's signature, not from the database

- **Context:** the store must be replaceable, and a database can be edited by whoever runs it.
- **Decision:** every row is signed by the admission key, and every row in use is verified (REG-ROW-4). The mechanism: an Ed25519 signature over the row's canonical JSON, checked by the query layer with a public key. A store is never asked whether a row is valid.
- **Why:** a replacement database gains no trust. Editing a stored row breaks its signature, and the edit is refused when the row is read.
- **What this does not cover:** a signature does not stop a store from *leaving out* a row or *serving an older one*. That is covered by R-D03.
- **Alternatives:** trusting the database's access control, which ties trust to one deployment. Signing the whole table, which is the current design's cost.
- **Status:** accepted.

## R-D03: Rows are hash-chained, with a signed checkpoint

- **Context:** a signature on each row does not detect a deleted or reordered row.
- **Decision:** each row carries `seq` and `prev` (REG-ROW-3). The store refuses an append that does not follow its head. `verify_store` walks the chain and compares it with a signed checkpoint `{count, head}`.
- **Why:** removing, reordering or inserting a row changes the chain head, and the checkpoint signature then does not match.
- **What this does not cover:** a request served between checkpoints can still be answered from a store that is hiding a row. Detection happens at the next `verify_store`, not on every read. Checking a proof that a row is in the chain on every read is future work.
- **Alternatives:** a Merkle tree with inclusion proofs, which is stronger and can be added later without changing the row format. No chain, which leaves deletion undetected.
- **Status:** accepted.

## R-D04: SQLite is the default store, behind an interface, with an in-memory second store

- **Context:** the user's requirement that the database be replaceable, the target of 100,000 tools, and no new runtime dependency.
- **Decision:** `CapabilityStore` is an abstract interface. `SQLiteStore` uses the `sqlite3` module that ships with Python, in WAL mode, with FTS5 for search. `MemoryStore` is a second implementation, and both run the same contract tests.
- **Why:** SQLite holds 100,000 rows of a few KB with ease (about 17.6 TB is possible with the default page size; see [07](07_VERIFICATION.md) for the measured figures). It needs no extra service, and our image's build includes FTS5. Writing two implementations from the start is what shows the interface is not shaped around SQLite.
- **Alternatives:** Postgres, which needs a service and a dependency and is not needed at this scale. Neo4j, which is not needed until dependency graphs arrive (A-002, A-009), and which would conflict with D-011's rule on the graph store.
- **Status:** accepted.

## R-D05: Code is stored as files named by their tree digest

- **Context:** the sandbox mounts a folder, and `resolve` recomputes the code digest.
- **Decision:** each implementation tree is written to a temporary folder, digested with `sha256-tree-v1`, and renamed to `code/<hex>/`. If that folder already exists, the copy is dropped. `resolve` recomputes the digest each time, as it does today.
- **Why:** identical code is stored once, a folder's name is its digest, and changing a file afterwards is detected at the next resolve.
- **Alternatives:** blobs in the database, which the sandbox cannot mount directly.
- **Status:** accepted.

## R-D06: Conformance runs in the sandbox, under the tool's own declared policy

- **Context:** the existing conformance runner starts the tool as a plain subprocess. That is acceptable for our own tools, not for code from outside.
- **Decision:** admission runs each case through `Sandbox.run`, with the `SandboxPolicy` from the submitted manifest (`policy_from_spec`). The container sandbox is the one used in deployment. A policy the sandbox cannot enforce refuses the run.
- **Why:** code from outside runs only under the limits it declares, and with no network.
- **Alternatives:** the plain subprocess runner, which is unsafe for outside code. A stricter fixed policy for admission, which would test the tool under limits other than the ones it will run under.
- **Status:** accepted.

## R-D07: Admission awards Validated

- **Context:** under the label policy, `Tested` needs evidence from a separate test suite. No format exists for a suite run outside Taffee.
- **Decision:** a submission that passes its cases and is approved is `Validated`. Rows imported from the file index keep their label.
- **Why:** this matches what was actually checked. Giving `Tested` to outside code would overstate it.
- **Alternatives:** calling conformance alone `Tested`, which would make the label policy mean something different for these tools.
- **Status:** accepted. The route to `Tested` is open question Q-2.

## R-D08: Every parameter must be described before admission

- **Context:** the dummy orchestrator found that the four built-in tools describe none of their parameters, so a model cannot use them well.
- **Decision:** admission refuses a manifest whose `inputSchema` properties lack a `description`. This is an admission rule, not a schema change, so committed manifests, their digests and the golden index do not change.
- **Why:** the main consumer of a tool definition is a model.
- **Alternatives:** changing the manifest schema, which changes `inputs_digest` and every evidence record.
- **Status:** accepted. Output and error contracts are open question Q-3.

## R-D09: The approval policy is a setting: manual or auto

- **Context:** passing automated checks is not, on its own, a decision to trust code. On the other hand, Taffee's agent loop may produce tools faster than people can review them.
- **Decision:** `approval` is set per deployment.
  - `manual`: a submission that passes waits in `awaiting_approval`. `may_approve` on the injected authorizer decides who may approve, and the submitter can never approve their own submission.
  - `auto`: a submission is admitted the moment every check passes, recorded as approved by the reserved principal `policy:auto`.
  - Both policies run exactly the same checks. The development and test default is `auto`. The `serve` command logs a warning when `auto` is set.
- **Alternatives:** always manual, which turns people into the bottleneck for an agent loop. Always auto, which removes the human check for deployments that need one.
- **Status:** accepted. Which policy production uses, and whether it depends on risk class, is Governance's decision (Q-5).

## R-D10: Search is word matching, with the query never reaching the query language

- **Decision:** the query is split into words of `[A-Za-z0-9]`. Each word becomes a quoted prefix term, and the terms are joined by AND. In SQLite, results are ranked by `bm25`, with an exact name match first. `MemoryStore` uses the same matching rule with a simple score.
- **Why:** this is predictable, cannot be injected into, and needs no new dependency.
- **Alternatives:** embedding search, which is out of scope and could use Context's embedding stack later.
- **Status:** accepted.

## R-D11: Search and list are discovery, and resolve is the gate

- **Context:** what should happen when revocation knowledge is stale.
- **Decision:** `resolve` fails closed: stale means `RevocationStale`. `search` and `list` leave out versions the client knows are revoked, but still answer when knowledge is stale.
- **Why:** finding a tool does not run it. An agent can still discover tools during a short feed outage, and nothing can run until the feed is fresh again.
- **Status:** accepted.

## R-D12: A new `/v2` service; the S-008 service is unchanged

- **Decision:** `capability/registry_api.py` serves `/v2` from the store-backed query layer. The S-008 `/v1` service and its accepted tests are not changed.
- **Why:** REG-COMPAT-1. The S-008 review stays valid.
- **Status:** accepted.

## R-D13: The library Registry caches its verified index

- **Context:** at 1,000 tools the file-index registry spent about 0.5 s re-verifying the whole index on every call.
- **Decision:** `Registry` keeps the last verified index in memory, keyed on the index file's `(dev, inode, size, mtime_ns, ctime_ns)`, and verifies it again whenever any of these changes. A failed verification is never cached, and callers get copies. `cache=False` turns the cache off.
- **Why:** `ctime` cannot be set back from user space, so rewriting the file in place or replacing it is always caught. Copies mean a caller cannot change the cached index.
- **What this does not cover:** a writer with root access who sets the system clock back can forge `ctime`. Such a writer could also replace the code being verified, so the threat model does not change.
- **Status:** accepted and implemented ([registry.py](../../../../capability/registry.py), tests in `test_registry_cache.py`).

## R-D14: Admitted code becomes Taffee's; the git library is an optional export, off by default

- **Context:** tools written by Taffee's agent loop are code Taffee runs on its users' behalf, and must stand behind (taffee-core D-013).
- **Decision:**
  - At validation, the manifest's `owner` is set to `taffee-library`, before the identity is computed. The submitting agent is kept as provenance (`submitted_by`, `source.provenance: taffee-agent`).
  - The record of ownership is the store: the signed row (owner `taffee-library`), the code area and the submission with its cases. The regression suite reads the store, so it does not depend on the library.
  - **The git library is a deployment setting, off by default** (amended 2026-11-16). Not every enterprise wants a service committing to git. When it is on (`--library PATH`), on admission the registry appends the signed row, then commits the version: the manifest, ledger, signed row, code and regression suite, with the submission, identity, approver, submitter and row sequence as commit trailers.
  - An enterprise may push the library to its own remote (`--library-remote`), with its own SSH key and `known_hosts` (`--library-ssh-key`, `--library-known-hosts`); the host key is always checked, and git never prompts. A failed push never fails an admission: it is logged, and the next push sends whatever was missed. Nothing is pushed unless a remote is configured.
  - The row is the record and the commit follows it. `sync` commits anything a failed commit left out, at start-up, and is idempotent.
- **Why:** ownership must not depend on an optional export. Where the export is on, ownership is also auditable in git history, and the library alone can rebuild the store.
- **Alternatives:** committing first and appending the row second, which could leave a commit with no row if the append failed. Committing into this source repository, which would mix agent-written tools into the pillar's own code.
- **Status:** accepted; amended 2026-11-16 (the export is optional and off by default; push to an enterprise remote).

## R-D15: Library names belong to Taffee; regression guards new versions; built-in names are closed

- **Context:** with first-come ownership of names, any agent could take over a name; with no ownership at all, any agent could ship a worse version.
- **Decision:**
  - Any Taffee agent may propose a new version of a library tool.
  - The new version must also pass every case of every admitted version with the same major version, the regression suite kept in the library.
  - A new major version starts a new suite, because a major version may change behaviour.
  - Two different versions racing for one `name@version` cannot both be admitted: the second approval is a `VersionConflict`.
  - Names of built-in tools (source `file-index`) cannot be used by any submission.
- **Why:** quality is guarded by behaviour, not by who arrived first. The built-in tools change only through reviewed commits.
- **Status:** accepted. This replaced a first-publisher rule tried earlier the same day.

## R-D16: Three sources of capability, two routes in

- **Decision:**
  - **`submission`:** written by Taffee's agent loop; full admission; owned; in the library.
  - **`file-index`:** built-in; reviewed git.
  - **`external`** *(designed, not built)*: third-party tools, external APIs, MCP servers and LLMs. Registered by reference (a pinned digest or an endpoint), tested against their contract, never copied and never owned.
- **Why:** only code Taffee wrote is code Taffee can own. Everything else is judged by its contract.
- **Status:** accepted. The `external` route is open.

## R-D17: An admission queue with workers, fairness, bounds and idempotent retries

- **Context:** agents submit in bursts and retry on timeouts. One agent must not starve the others, and the queue must not grow without limit.
- **Decision:** `AdmissionQueue` provides:
  - `workers` parallel workers, which bounds the number of sandbox runs in flight;
  - round-robin across submitters;
  - `max_queued` in total and `max_open_per_submitter` per submitter, with `Busy` (429 with `Retry-After`) past either, before anything is stored;
  - `resume` after a restart.
  
  A retry, meaning the same canonical bundle from the same submitter, is recognised before any bound is checked and returns the original submission.
- **Settings:** the development and test setting is 20 concurrent submitters, with 4 workers. The design goal is 100 concurrent submitters, load-tested separately.
- **Found by its test:** the standard library HTTP server accepts only 5 pending connections, so 20 simultaneous connections were reset. The development server now accepts 128, as a deployment setting.
- **Status:** accepted.

## R-D18: A build step, and Taffee's own checks on top of the submitter's

- **Context:** an agent writes both the tool and its tests, so it could pass its own trivial cases.
- **Decision:** Taffee owns these checks, and the submitter chooses none of them:
  - a build step in the sandbox, before any case runs: a Python syntax compile, or `bash -n`;
  - every case's input must match `inputSchema`;
  - every required parameter must be exercised;
  - at least one case must expect success and one a tool-level error;
  - every run must give exactly one result line (the tool contract);
  - an `idempotent` tool must give the same answer when run twice;
  - the regression suite (R-D15).
- **Why:** this is what makes the library *opinionated*. Every tool in it meets Taffee's floor, not only its author's.
- **Status:** accepted. Further rules, such as probes for confinement and limits, can be added without changing the submission format.

## R-D19: One home for the registry's data

- **Decision:**
  - The store (`registry.sqlite3` and `code/<hash>/`) is in `TAFFEE_CAP_DATA`, by default `var/registry/` in this repository (gitignored).
  - The library repository is in `TAFFEE_CAP_LIBRARY`, by default `var/library/`.
  - Deployed, both are under `/var/lib/taffee-capabilities` on a Docker volume.
  - `serve` builds the repository's own index and imports the built-in tools on start-up, so one registry serves every tool.
  - SQLite runs with `synchronous=FULL`: each admission step is on disk before the next one starts.
- **Status:** accepted.
