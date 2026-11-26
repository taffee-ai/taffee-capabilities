# taffee-capabilities roadmap

Status: living roadmap. Each row records the feature's state. A feature is Done only when its exit gate has passed and its evidence is recorded.

Written for: the owners of the four pillars and the engineers who will build the capability catalog.

Design: [./CAPABILITIES-DESIGN.md](design/CAPABILITIES-DESIGN.md). Requirements: [./CAPABILITY-REQUIREMENTS.md](design/CAPABILITY-REQUIREMENTS.md).

## How to read this

Features are grouped into four tracks. Each track has its own number series.

| Track | Prefix | What it covers |
|---|---|---|
| Shared | `S-` | Pieces every kind of capability uses: manifests, registry, revocation, discovery, catalog, evaluation |
| Tools | `T-` | Tool capabilities: testing, admission of generated tools |
| Models | `M-` | Model capabilities: approval records for specific models, routes and data classes |
| Agents | `A-` | Agent capabilities: declarations, trials, trust |

- **Depends on:** features that must pass their exit gates first. Features in different tracks can run in parallel once their dependencies allow.
- **Specified** means a detailed spec exists in the feature's folder. Other features are listed with their intent and exit gate, and get a spec when they become next.
- **Out of scope:** model routing. Traffic is sent to model routers, which are outside this pillar.

## Sequence

The shared foundation comes first. The three tracks then run in parallel.

```
Shared foundation
  S-001 manifests + validators ──► S-002 registry ──► S-003 revocation feed
                                       │
                                       ├──► S-004 read-only MCP surface
                                       ├──► S-005 umbrella catalog generator
                                       └──► S-007 prototype adopts registry

Tools                                  Models                    Agents
  T-001 built-in tools Tested            M-001 model approvals     A-001 declarations + trials
  (needs S-002, S-003)                   (needs S-002, S-006)      (needs S-002, T-001, S-006)
        │                                                              │
        ▼                                                              ▼
  T-002 admission gate                                           A-002 agent trust + re-run
  (needs S-003, A-002)                                           (needs A-001, S-003)
        │
        ▼
  T-003 provenance + admitted commit

S-006 evaluation integration: decision on evaluation's home. Blocks M-001 and A-001.
```

## Features

### Shared (S)

| ID | Feature | Depends on | Exit gate | Detail |
|---|---|---|---|---|
| S-001 | Manifests, languages and validators (fixture mechanism; five tools declared, no implementations) | — | Index generates; validators recompute the same labels twice on a clean checkout | [spec](design/shared/S-001_manifests_validators/SPEC.md) |
| S-002 | Registry: list, describe, resolve, digest check | S-001 | The five S-001 tools are listed and described; `resolve` returns `NoImplementation` for them until T-001; a test-only library fixture resolves and fails on a changed file | [spec](design/shared/S-002_registry_resolve/SPEC.md) |
| S-003 | Revocation client (client phase), with the feed owned by govsec | S-002 | Client phase: a verified, signed, chained and checkpointed feed is checked at each resolve; a bad or unverified entry fails closed; freshness is refreshed only by a verified checkpoint; name@version and digest targets both work. **Client phase implemented and accepted in review.** Full S-003 gate (feed service, dispatcher record, key rotation, drill) is cross-repo and open. | [spec](design/shared/S-003_revocation_feed/SPEC.md), [record](design/shared/S-003_revocation_feed/VERIFICATION.md) |
| S-004 | Read-only MCP surface | S-008 | A standard MCP client finds a capability with `search` and reads its label with `describe`; no execute path exists | Not yet specified |
| S-005 | Umbrella catalog generator | S-001, S-002 | `taffee-core/catalog/capabilities.yaml` is generated from the index, with the index's `inputs_digest` and `index_digest`, and the Git SHA of the committed index attached externally. **Implemented, tested and accepted in review (code and record); publishing and OPS owner questions open** | [spec](design/shared/S-005_catalog_generator/SPEC.md), [record](design/shared/S-005_catalog_generator/VERIFICATION.md) |
| S-006 | Evaluation integration | Decision on evaluation's home | Capabilities consumes evaluation results without importing evaluation code | Not yet specified |
| S-007 | Prototype adopts the registry | S-002, T-001 | The prototype's dispatcher and model adapter get tools and schemas from the registry; its test suite passes | Not yet specified |
| S-008 | Catalog and registry service | S-002; govsec auth contract (D-008) as entry condition | The REST endpoints in USAGE-DESIGN §3.1 and §3.2 answer for all three kinds; the service returns the same indexed facts for each entry the caller may see; entries the caller may not see are omitted | [note](design/shared/S-008_catalog_service/CONTRACT-NOTE.md), [record](design/shared/S-008_catalog_service/VERIFICATION.md). **Reference service implemented and tested (29 tests; full suite 799 OK, 1 skip); not yet reviewed.** Scope of this version: stdlib HTTP, injected authorizer (govsec's real one later), no raw signed index endpoint |
| S-009 | Call-time validation | S-002 | Inputs are checked against the declared schema before execution; outputs are checked against the postcondition; a failure is an error with the field or check named. **Implemented and tested; not yet reviewed** | [spec](design/shared/S-009_call_time_validation/SPEC.md), [record](design/shared/S-009_call_time_validation/VERIFICATION.md) |
| S-010 | Caching policy | S-002, S-003 | Resolution is cached by digest and index; no cache outlives a revocation check; caching rules are declared per capability and tested | Not yet specified |
| S-011 | Sandbox interface | S-009 | Execution enforces time, memory, output size, filesystem scope and network scope from the declaration; no silent fallback to unrestricted execution. **Library layer implemented, tested (process and container sandboxes, 84 tests with Docker) and accepted in review (code and record); reading the policy from a manifest declaration is not done, and T-005 and the platform gates stay open** | [spec](design/shared/S-011_sandbox_interface/SPEC.md), [record](design/shared/S-011_sandbox_interface/VERIFICATION.md) |
| S-012 | Call records and audit | S-002, S-003 | Every call writes one record with digest, label, revocation sequence, grant reference and outcome; records hold no content, only references. **Implemented, tested, and accepted in review (code and record); platform integration and owner gates stay open** | [spec](design/shared/S-012_call_records/SPEC.md), [record](design/shared/S-012_call_records/VERIFICATION.md) |
| S-013 | Verification CLI (`capctl verify`) | S-001, S-002, S-003 | A consumer can run one command that checks digest, index, evidence, revocation and label, and gets a yes or no with reasons. **Implemented, tested and accepted in review (code and record); platform integration open** | [spec](design/shared/S-013_verification_cli/SPEC.md), [record](design/shared/S-013_verification_cli/VERIFICATION.md) |
| S-014 | Error contract and consumer documentation | S-008 | Every error in USAGE-DESIGN §6 has a code, a message and a next step; a quickstart works from a clean machine | Not yet specified |
| S-018 | Registry store, admission and discovery: a pluggable store (SQLite by default), signed and chained rows, admission of tools written by Taffee's agent loop (build, tests in the sandbox, Taffee's checks, regression, approval policy) with an optional git library export (off by default), an admission queue (workers, fairness, bounds, retries), search, REST `/v2` and a read-only MCP server | S-002, S-003, S-011 | An agent's tool goes from submission to an admitted, searchable and runnable tool; a failing tool never becomes visible; 20 concurrent submitters are handled correctly. **Implemented and tested; not yet reviewed.** External capabilities and the 100-submitter load test are open | [suite](design/shared/S-018_registry_store_admission/01_REQUIREMENTS.md), [record](design/shared/S-018_registry_store_admission/07_VERIFICATION.md) |

### Tools (T)

| ID | Feature | Depends on | Exit gate | Detail |
|---|---|---|---|---|
| T-001 | Built-in tools reach Tested | S-002, S-003 | Four tools and `graph_query` reach Tested; `git_log` has its own test suite; fingerprints reproduce. **In progress:** four tools are Tested (Python implementations, conformance cases, reproducible evidence); `graph_query` (a client-independent core is built and accepted in `drafts/graph-query`, but it cannot run a real query until Context publishes `graph_query` and govsec's credential transport exists), migration parity with the prototype and postconditions (S-009) are open | [spec](design/tools/T-001_builtin_tools_tested/SPEC.md), [record](design/tools/T-001_builtin_tools_tested/IMPLEMENTATION.md) |
| T-002 | Admission gate: quarantine and sandbox validation | S-003, S-011, A-002 | A generated tool goes from proposal to validation in the sandbox; a failing tool stays in quarantine | Not yet specified |
| T-003 | Provenance and admitted commit | T-002 | An admitted tool appears in `library/generated/` with its manifest and provenance; nothing else does | Not yet specified |
| T-004 | Tool result caching | S-010, S-012 | Only tools declared idempotent are cached; cache is per run; concurrent identical calls share one execution; a failed call is never cached | Not yet specified |
| T-005 | Sandboxed execution of tools | S-011 | A tool declared as sandboxed runs with the limits in its declaration; a test shows a disallowed file or network access is refused | [note](design/tools/T-005_sandboxed_execution/CONTRACT-NOTE.md), [record](design/tools/T-005_sandboxed_execution/VERIFICATION.md). **Stage A implemented, tested (49 tests; full suite 770 OK; exit-gate tests on the real container sandbox) and accepted in review for the agreed scope. Stage B (declaring the policy on the four built-in tools) not started** |
| T-006 | Service-mode tools: credentialed and external | S-008, S-012, govsec contract | GitHub, Jira and MCP-server tools run behind the service; the caller's grant is checked per call; no access token (API credential) is stored in the catalog or the caller | Not yet specified |
| T-007 | Tool contract tests | S-009 | Every tool's declared schema, postcondition and error cases are tested against the same contract suite, so a new tool cannot skip a check | [record](design/tools/T-007_tool_contract_tests/VERIFICATION.md). **Implemented, tested (22 tests; full suite 721 OK) and accepted in review (code and evidence)** |

### Models (M)

| ID | Feature | Depends on | Exit gate | Detail |
|---|---|---|---|---|
| M-001 | Model approval records | S-002, S-006 | One approved model per category; expiry and version change revoke approval | [spec](design/models/M-001_model_approval/SPEC.md) |
| M-002 | Route definitions and validation | S-001, M-001 | Every access kind in SPEC §11 validates; a deployed role cannot reference a `cli` route; raw credentials are refused | Not yet specified |
| M-003 | Model profiles | M-001 | Each approved model declares its tool-call support, parallel calls, context and output limits; the adapter reads the profile and never branches on the model name | Not yet specified |
| M-004 | CLI routes: direct and mailbox transports | M-002 | A `cli` route answers one request in each transport, under the tool-denial rules, with the record written | Not yet specified |
| M-005 | Model call records and cost attribution | S-012, M-002 | Every model call writes a record with route, model, approval fingerprint and usage; cost can be totalled by role and by account; no prompt or reply text in the record | Not yet specified |
| M-006 | Role view | M-002 | `GET /v1/roles` returns each role's route, model and approval state for an environment | Not yet specified |

Response caching of model output is not in this track. It is off by default, and would need its own decision, because a cached answer can outlive the approval that allowed it.

M-001 covers approval records, the access routes they bind to, and the evaluation summary format. Routing is out of scope (see above). Its design document still contains routing material that must be removed to match this scope.

### Agents (A)

| ID | Feature | Depends on | Exit gate | Detail |
|---|---|---|---|---|
| A-001 | Agent declarations and scenario trials | S-002, T-001, S-006 | One agent declared, its scenarios run N times, pass rate recorded with fingerprint | Not yet specified |
| A-002 | Agent trust and re-run on change | A-001, S-003 | A change to a tool or model version drops the agent's label until re-evaluation | Not yet specified |
| A-003 | Agent manifest and two agent types | S-001, S-002 | Capability agents (catalog members, run under a delegation from the caller) and framework agents (principals that start delegations) are separate manifest types, and each validates | Not yet specified |
| A-004 | Delegation check on accept | A-003, govsec contract | A run is accepted only when its delegation verifies and every action it needs is within the delegation; a child delegation that widens the parent's is refused | Not yet specified |
| A-005 | Agent limits and isolation | S-011, A-003 | Turn, time, tool and provider limits from the manifest are enforced; the run stops with a named reason when a limit is hit | Not yet specified |
| A-006 | Long-running agent registration | A-003, S-012 | An existing long-running agent registers with an owner-approved entry, a heartbeat, and a version; a missed heartbeat or a version change drops its label | Not yet specified |
| A-007 | Short-lived agent lifecycle | A-005 | A spin-up agent is created from its manifest for one request, runs within its limits, and leaves a complete record; nothing persists after the run | Not yet specified |
| A-008 | Outcome records and verification | A-001, S-012 | Each run records the outcome checked (the final state, not the answer text) and the checker results | Not yet specified |

## Milestones

| Milestone | Features | What it means |
|---|---|---|
| M1: Catalog exists | S-001, S-002, S-005 | Capabilities are listed with real digests, and nothing is listed without a manifest |
| M2: Tools are Tested | T-001 | The four built-in tools and the Context wrapper carry a label backed by evidence |
| M3: Revocable | S-003 | A bad capability can be stopped at the next call, without a git commit |
| M4: Models approved | M-001 | Model approval records are in use, one per category |
| M5: Agents trusted | A-001, A-002 | One agent's trust is backed by outcome scenarios and is revoked by dependency changes |
| M6: Generated tools admitted | T-002, T-003 | Agent-written tools can enter the catalog only through the gate |
| M7: Discoverable | S-004 | External MCP clients can discover capabilities without executing them |
| M8: Enterprise-usable | S-008, S-012, S-013, S-014 | A consumer can reach the catalog over REST, verify what they receive, and read a record for every call |

M1 to M3 are the first public release of this pillar. M6 depends on everything before it and is last on purpose.

## Risks

| Risk | Effect | Mitigation |
|---|---|---|
| Digest of a library folder is not reproducible across clean clones | Labels cannot be recomputed | S-001 fixes the digest algorithm and tests same-host clean-clone reproducibility on the Linux test host. Cross-OS proof needs a host the owner permits. |
| Evaluation's home is undecided | S-006, M-001 and A-001 evidence may be built twice | Decide before M-001 starts; M-001 only needs a summary file format |
| Prototype code changes under the registry | Digests go stale, labels drop | Freeze the four tools' code at T-001 start; changes go through a new version |
| Revocation feed becomes a single point of failure | Tool calls stop | Freshness bound and fail-closed are a conscious choice; feed is run with the stack |
| Scope creep into govsec | Grants implemented here | Requirements state grants are out of scope; reviews check it |

## Language

Open decision. The Context pillar is Python (D-007). Recommend Python for this pillar too, so the toolchain is one. Decide before S-001 starts.

## Enterprise interaction additions

These serve the enterprise interaction flow (EI-nn items). They are planned, not specified.

| ID | Feature | Depends on | Exit gate |
|---|---|---|---|
| S-016 | Draft validation: manifest, code and composition checks for a draft, before any trial | S-002, T-002 | A draft that references an unknown or unadmitted part is refused with the part named |
| S-017 | Gap detection: registry search for parts matching an intent's objective | S-008 | Given an intent, returns the matching parts and the unmet objectives, as structured data |
| A-009 | Composition: a new agent manifest built from existing agents, tools and model roles | S-016, A-001 | A composition inherits the weakest label of its parts, and is refused when any part is not usable for the data class |

