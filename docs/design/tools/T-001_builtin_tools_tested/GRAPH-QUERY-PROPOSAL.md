# T-001 graph-query: Context contract, dependency and test plan (proposal)

Status: proposal accepted in review. **The preparatory slice in section 9 is implemented and accepted in review (2026-10-19), code and record, on 42 focused and 699 full-suite checks with one deliberate skip.** This is not T-001 exit and not a Validated or Tested graph-query. **Sections 3 to 6 are the original proposal; where they differ from sections 8 and 9 (the credential parameter, the transport, the label ceiling, the location in `drafts/`), sections 8 and 9 are current and the corrections are marked inline.** This slice is kept separate from migration parity and S-007.

Written for: reviewers and maintainers.

## 1. What I found in Context

Read-only inspection of `taffee-context` (no edits):

| Fact | Where |
|---|---|
| The `graph_query` tool exists as TypeScript with a JSON-returning handler and a schema `GRAPH_QUERY_SCHEMA`: parameters `repoId`, optional `commitSHA`, `operation` in `resolve`, `find_symbol`, `get_callers`, `get_callees`, `get_dependencies`, `symbol`, `limit` (1 to 20), `additionalProperties: false`, no raw Cypher. It reads Neo4j directly. | `context/retrieval/query/graph-query-tool.ts` |
| It enforces an operator-set repository allow-list (`TAFFEE_CONTEXT_REPOS`) and a 64 KiB output bound that drops results and sets `truncated` rather than slicing JSON. | same file |
| The published MCP server is Python (FastMCP, Streamable HTTP, bearer-token auth with names, per-caller rate limit and call log). Its tools are `list_repos`, `graph_stats`, `get_node`, `explain`, `affected`, `get_neighbors`, `god_nodes`, `get_community`, `shortest_path`, `query_graph`, `semantic_search`, `read_code`, `assemble_context_pack`. **There is no `graph_query` tool.** | `context/serving/mcp/server.py` |
| The published tools return human-readable text, not JSON; `get_neighbors` returns lines like `A -->[calls, EXTRACTED] B`. | `server.py` |
| Context migrates to Python module by module, with the TypeScript tests as the specification (decision D-007). `graph-query-tool.ts` is part of that migration. | taffee-context |

The review finding is therefore right: T-001 SPEC 5.3 assumes a published `graph_query` endpoint that does not exist.

### 1.1 The two contracts side by side (added after review)

The published MCP tool `query_graph` is not an alias for `graph_query`:

| | `graph_query` (TypeScript, unpublished) | `query_graph` (published MCP) |
|---|---|---|
| Arguments | `repoId`, `commitSHA`, `operation` (5 closed values), `symbol`, `limit` 1 to 20 | `repo`, `question` (free text up to 500 characters), `mode` bfs or dfs, `depth` 1 to 6, `token_budget` 200 to 8000, `context_filter`, `commit` |
| Intent | exact structural lookups: resolve a commit, find a symbol, callers, callees, dependencies | keyword search then a walk outward from the best matches |
| Output | JSON object with `results`, `truncated`, metadata, at most 64 KiB | graphify text prefixed with `[repo @ sha]`, bounded by a model-token budget |
| Scope check | operator repository allow-list in the tool | per-caller bearer token and rate limit at the server edge |
| Determinism | closed operations, same input and snapshot give the same rows | search-ranked text, wording can change with the engine |

Consequences: wrapping `query_graph` under the name `graph_query` would change both the arguments and the meaning of the result, so it would not be the same tool and the T-001 5.3 schema comparison could not hold. Option A keeps the declared contract as `graph_query` (the structural one, the one the prototype and the earlier plans mean), and option B asks Context to publish exactly that. If the owner instead wants the capability to expose the existing `query_graph` behaviour, that is a different capability with a different name and schema, to be decided separately; I do not propose it here.

## 2. Options

| Option | What | Verdict |
|---|---|---|
| A | A capabilities-only wrapper over a small client interface, with a test double, declaring the existing `GRAPH_QUERY_SCHEMA`; the contract test against a real published schema is **skipped with a recorded reason** until the endpoint exists | Do this now. It is reversible and stays in capabilities. |
| B | Ask Context to publish `graph_query` on its MCP server with the existing schema and JSON output | The smallest real dependency; **needs the Context owner and a cross-repo edit, so I do not do it without approval**. |
| C | Build the wrapper over existing text tools (`get_node`, `get_neighbors`) and parse the text | Rejected: parsing human-readable output is fragile, its meaning can change with wording, and it fixes no schema. |

## 3. Proposed contract (what A declares and B would publish)

**Tool.** Name `graph_query`, version 1.0.0 of the capability. Read-only and **not idempotent**: an omitted `commitSHA` resolves to the newest ingested commit at call time, so the same input can give different results (the existing manifest and T-001 CAP-T-12 say `idempotent: false`; corrected after review). Requires `context.read`.

**Input** (exactly the existing schema): `repoId` (string, required), `commitSHA` (string, optional; full SHA; omitted means newest ingested), `operation` (required; one of `resolve`, `find_symbol`, `get_callers`, `get_callees`, `get_dependencies`), `symbol` (string; label fragment for `find_symbol`, exact `localId` for relationship operations), `limit` (integer, 1 to 20). `additionalProperties: false`.

**Output.** A JSON string, parsed by the wrapper: `{"results": [...], "truncated": bool, ...metadata}`, at most 64 KiB, never sliced JSON. Postconditions (S-009): `max_bytes 65536` and a valid-JSON check on the whole output (a new postcondition type `json_object`, or an `each_line_matches` is not enough; see 6, question 3).

**Errors.** The wrapper maps Context's failures to a closed list: `ContextUnavailable`, `ContextUnauthorized`, `RepoNotEnabled`, `InvalidQuery`, `OutputTooLarge`, `ContextError`. No message text from Context is copied into a record or a result (same rule as S-012).

**Authorization and boundary.** The wrapper holds no credential and **no credential path exists in this draft**. Eventually a govsec-brokered, short-lived, single-use credential for `read_context` on the repo (GOVSEC-DESIGN sections 5 and 11) must reach whatever transport talks to Context, but **how it gets there is deliberately undefined** until the orchestrator and govsec transport contract provides a trusted channel (review, finding 1): it must never come from the tool's stdin, which is caller-controlled input. Which repositories a caller may read is **Context's allow-list plus govsec's decision**, not the wrapper's and not a manifest field. The wrapper never shows a credential in a result or a record.

**Client interface (current, as implemented).** `ContextClient.call_tool(name: str, arguments: dict) -> str`: **two arguments, no credential parameter** (a test inspects the signatures). A credential parameter, or a client constructed with a credential, is added only when the trusted transport contract exists; that is part of the activation gate in section 9. Production transport: the MCP Python SDK over Streamable HTTP, which Context itself uses (dependency to be pinned and reviewed, see 6, question 2). Tests: an in-memory double that records calls and returns fixtures. The wrapper depends on the interface only.

## 4. Owner and dependency boundary

| Item | Owner | Needed for |
|---|---|---|
| Wrapper code, manifest, conformance cases, contract test harness | capabilities (me) | all labels |
| Publishing `graph_query` on the MCP server with the schema in 3 and JSON output | Context owner (option B) | the contract test to run for real, hence **Tested** |
| The caller's `read_context` credential | govsec (design only today; G-005 and G-007 not built) | any real call |
| Repository allow-list | Context operator setting | `RepoNotEnabled` behaviour |

## 5. What label this can honestly reach

- With option A alone: **Declared**, then **Validated** (the conformance cases pass against the test double). Not **Tested**: T-001 SPEC 5.3 says the contract-test skip counts against the evidence, and the suite cannot be an approved evidence suite while that test is skipped. I will not weaken that rule to reach Tested.
- Tested needs B (a published endpoint) and a contract test that fetches the real tool list and compares schemas.
- A run against a live Context service needs a credential, which needs govsec to exist. Until then no real call is claimed.

## 6. Test plan

Unit and conformance, all with the test double, run on the Linux test host in Docker:

1. Declared schema equals `GRAPH_QUERY_SCHEMA` field by field (a copy of the TS schema is pinned as a fixture with the commit it came from).
2. Input validation: unknown operation, missing `repoId`, `limit` 0 and 21, extra property, wrong types; each rejected before any client call, with no value echoed (S-009).
3. Output: valid result with `truncated` true and false; invalid JSON; over 64 KiB; wrong shape; each handled by the postcondition.
4. Error mapping: each client failure maps to one code in the closed list; no Context text appears in the result.
5. No credential in the manifest, the conformance cases, the result or any log; no credential field is read from the request (credential-looking request fields are ignored and never forwarded).
6. Cross-check: the declared tool fits the label policy as Declared/Validated, and the evidence step refuses to write a Tested record while the contract test is skipped.
7. Contract test against a published `graph_query`: written, **skipped with a recorded reason** until B exists.

## 7. Open questions for review

1. Do you accept option A now and B as a named dependency on the Context owner, with Tested explicitly out of reach until B?
2. The MCP SDK is the production transport. Accept it as a pinned dependency of this one tool's runtime (not of the capabilities core), or prefer the interface and double only for now, with the real transport added when B exists?
3. The output must be a JSON object. S-009 has `max_bytes` and `each_line_matches` only. Add a `json_object` postcondition type (a new manifest field, so a change to S-009 that you have accepted), or validate the JSON inside the wrapper and fail with `OutputRejected`?
4. May I write the capabilities-only code (wrapper, manifest at 1.0.0 as Declared, conformance cases, tests) once you answer 1 to 3? It is reversible and touches no other repo.

## 8. Decisions after review

1. **Option A accepted now; B is a named Context dependency;** no Tested claim until a published `graph_query` with this schema and JSON behaviour exists and a live contract test runs.
2. **No MCP SDK dependency now.** Only the `ContextClient` interface and a test double. The real transport is added when B gives something to exercise.
3. **S-009 stays unchanged.** No `json_object` postcondition. The manifest postcondition is `max_bytes 65536`. The wrapper itself validates that the reply is one JSON object with a `results` array and a boolean `truncated`.
4. **Version 1.1.0**, not 1.0.0: an implementation appears, so identity changes (T-001 section 7, D-S1-03). The ledger `versions.yaml` gets a 1.1.0 entry; the golden index, the catalog and the fixtures that pin versions are regenerated, and the label stays at most Validated.
5. **Error path, exact.** The wrapper never copies Context text. Its in-body error is one of a closed list (`InvalidInput`, `ContextUnavailable`, `ContextUnauthorized`, `RepoNotEnabled`, `ContextBadReply`, `OutputTooLarge`). In the S-012 closed outcome table: a call that fails the input schema is `input_rejected` / `ValidationFailed`; a reply over 64 KiB caught by the dispatcher's `max_bytes` postcondition is `output_rejected` / `PostconditionFailed`; every failure the wrapper reports in its own result (`{"ok": false}`, including a reply that is not valid JSON or has the wrong shape) is `tool_failed` with no code; a missing or denied grant and an unavailable sandbox are `refused` with the existing codes, produced by the dispatcher, not by the tool. No new outcome code is introduced. A bad reply is `tool_failed`, not `output_rejected`, because `output_rejected` is derived from the dispatcher's postcondition check and S-009 has no JSON-shape postcondition.
6. **No spoofable transport.** The tool process has no way to select a fixture transport from its input, its environment or a file in the repository root (that would let a caller plant a fake Context reply). In production the only transport is the unavailable one, so every call returns `ContextUnavailable` until B and govsec exist. The double is injected into the tool's core function by the unit tests, not through the process contract.
7. **What Validated means here, stated plainly.** The conformance cases run the real process, so they can only cover input rejection and the `ContextUnavailable` path. Success paths are covered by unit tests with the double. Validated therefore proves the wrapper's declared validation and failure behaviour, not a successful query. The record says so.

## 9. Preparatory slice: what was built

Scope adjusted in review: a client-independent core and unit tests only. The manifest stays 1.0.0, Declared, `implementations: []`; there is no `conformance.json`; the index and catalog are not regenerated; nothing is Validated or Tested. **This is a preparatory T-001 slice, not T-001 exit.**

**Where.** `drafts/graph-query/python/main.py` and `tests/test_graph_query.py`, `tests/fixtures/graph_query_schema.json`. Not `library/graph-query/`: the index digest covers everything under `library/` (`INPUT_FOLDERS` in `capability/index.py`), so code placed there changes `inputs_digest`, the golden index and the umbrella catalog (the first full run showed exactly that: `test_golden_index_matches` failed with only `inputs_digest` and `index_digest` different). `drafts/` is outside the digested folders, so no accepted artifact changes. At wiring time it moves to `library/graph-query/python` as part of the 1.1.0 identity change.

**What it is.** `run(request, client)` validates the input against the declared schema **and Context's own checks** (names problems, never echoes values or unknown names), calls a `ContextClient` with `graph_query` and the declared arguments, validates the reply (one JSON object, a `results` array, a boolean `truncated`, at most 64 KiB, parsed with non-finite numbers refused), and returns the compact JSON. Failures are one of six closed codes: `InvalidInput`, `ContextUnavailable`, `ContextUnauthorized`, `RepoNotEnabled`, `ContextBadReply`, `OutputTooLarge`. A bad reply is `ok: false` with `ContextBadReply`, which S-012 records as `tool_failed` with no code. The client interface is `call_tool(name, arguments)` with **no credential parameter**: how a brokered credential reaches a client is undefined until the orchestrator and govsec transport contract exists, and it is never read from the tool's stdin (review, finding 1). Fields such as `credential`, `token` or `authorization` on the request are ignored and never forwarded; inside `input` they are an unknown property. The only production client is `UnavailableClient`, so the process always answers `ContextUnavailable`; the double is injected by tests only. The source contains no environment, file, network or subprocess access, so nothing can select another client.

**Context's own checks, mirrored (review, finding 2).** The JSON schema omits what Context's `validateQuery` enforces (`types.ts` `requiredText` and `commitSHA`, `neo4j-query.ts`), so the wrapper now rejects before any client call: `repoId` blank, over 256 characters or with control characters; `commitSHA` not a full lowercase Git object id (40 or 64 hex characters); a non-`resolve` operation without a non-blank `symbol` (at most 1024 characters, no control characters). Lengths are counted in UTF-16 code units, as in Context, so nothing is accepted that Context rejects. `resolve` ignores `symbol`, as Context does. Schema parity is therefore: the wrapper never accepts what the schema rejects, and it differs from the schema only by these listed Context checks (a test lists both sets).

**Tests (39 at this point, run on the Linux test host in Docker; 1 skipped on purpose).** Declared manifest schema equals the pinned Context schema; the wrapper accepts and rejects exactly what the schema does (24 inputs, compared with `jsonschema`); invalid inputs never reach the client and never echo values; each of the five operations with truncated true and false; unchanged argument pass-through; bad replies (twelve shapes) give `ContextBadReply`; oversize gives `OutputTooLarge` before parsing; the bound itself is accepted; each client failure maps to its closed code and no Context text or credential escapes; the real process answers `ContextUnavailable`, handles bad stdin, and ignores planted fixture files, transport environment variables and extra request fields; the manifest is still 1.0.0 with no implementation and `idempotent: false`; no conformance file, no `library/graph-query`, no evidence; `capability.evidence write --tool graph-query` exits 2 and writes nothing; the ledger still lists only 1.0.0. The skipped test is the contract test against a published schema, with its reason.

**Evidence.** Focused: test_graph_query and test_pins 39 tests, OK with one skip. Full suite on the Linux test host with the Docker socket: the count is in the latest full-suite record; the only skip is that contract test. Measured proofs, behaviour restored in scratch copies: a transport selectable by environment, 2 fail; exception text copied into the error, 3 fail; unknown-property check removed, 4 fail; `truncated` type check removed, 1 fails; the early size check removed, 1 fails (this one passed before the test `an_oversized_reply_is_refused_before_it_is_parsed` was added, so it was a real gap, now closed).

**Remaining dependencies before T-001 exit for graph-query.** (1) Context publishes `graph_query` with this schema and JSON output (option B, Context owner). (2) The credential and transport boundary exists (govsec G-005 and G-007, a pinned MCP transport). Then: move to `library/graph-query/python`, wire the production client, add success and failure conformance cases, version 1.1.0, run the live contract test and the evidence step, regenerate index and catalog, and let the label policy decide.

Measured proofs for the corrections above, behaviour restored in scratch copies: credential read from the request and forwarded, 2 fail; symbol-required check removed, 10 fail; commit-SHA pattern check removed, 8 fail; length counted in code points instead of UTF-16 units, 1 fails; repoId check removed, 8 fail.

### 9.1 Corrections after a second reviewer's independent review (verified by me against the files)

A second reviewer reviewed the slice. I reproduced each finding before accepting it (with a probe on the Linux test host):

| Finding | Verified | What I did |
|---|---|---|
| 1.1 A reply with a lone surrogate (`"\ud800"`) passed `check_reply`, then crashed `run` with `UnicodeEncodeError`, reported as `ContextUnavailable` | Yes, reproduced (CRASH UnicodeEncodeError) | The reply is encoded inside a guard; failure is `ContextBadReply`. The size check measures with `surrogatepass`. |
| 1.2A `repoId` of `U+FEFF` accepted by the wrapper, rejected by Context (JS `trim()` removes U+FEFF; Python `strip()` does not) | Yes, reproduced; also U+0085 went the other way (wrapper stricter than Context) | Blankness now uses the exact ECMAScript whitespace set, so the wrapper accepts exactly what Context's `!value.trim()` accepts. Tests cover U+FEFF, U+00A0, U+2003, U+3000 (rejected) and U+200B, U+0085 (accepted, as in Context). |
| 1.2B `limit: null`: Context's `?? 10` accepts it, the wrapper rejects it | Yes, and intended | The wrapper must never accept what the schema rejects; the schema rejects `null`. Stricter than Context in the safe direction. Pinned by a test (`limit=None` in the schema-rejects list). |
| 1.3 Duplicate keys in the reply accepted | Yes, reproduced | `object_pairs_hook` rejects them: `ContextBadReply`. |
| (mine, found while reproducing 1.1) Excessive nesting made `json.loads` raise `RecursionError`, reported as `ContextUnavailable` | Yes, reproduced (30000 deep) | `RecursionError` is a bad reply. |
| 1(b), 1(d), 1(e) | Checked by a second reviewer: nothing wrong | I re-read them: agreed. |

Tests now: 42 in `test_graph_query` and `test_pins`, one skipped (the contract test). Measured proofs, behaviour restored in scratch copies: surrogate guard removed, 3 error; duplicate-key hook removed, 3 fail; recursion guard removed, 2 error; JavaScript whitespace set replaced by `str.strip()`, 2 fail.
