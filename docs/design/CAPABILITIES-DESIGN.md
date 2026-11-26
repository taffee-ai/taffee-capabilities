# Capabilities pillar: design

Status: design. The reference for how the pillar fits together. Requirement IDs (CAP-TOOL-GEN-*, CAP-AGT-*, CAP-MDL-*) are defined in `CAPABILITY-REQUIREMENTS.md`. What is built is tracked in the [roadmap](../roadmap.md).

Written for: the owners of the four pillars, and engineers who will build the registry, validators and admission gate.

## 1. Purpose

The capabilities pillar answers one question for the rest of the platform: **what can be called, what is it known to do, and on what evidence?**

It publishes a catalog of tools, agents and models. Each entry carries a label that is computed from evidence, and a way to resolve an entry to something executable.

It does not decide who may use an entry (govsec), and it does not run anything (orchestrator).

## 2. Ownership boundary

| Concern | Owner | Pillar's role |
|---|---|---|
| What exists, its version, its declared contract | Capabilities | Owns |
| Evidence and the label it earns | Capabilities (validators) | Owns |
| Test and scenario content | Capabilities for tools and agents; evaluation for model suites | Owns the tests it runs |
| Admission of generated tools | Capabilities (workflow), named owner decides | Owns the workflow |
| Who may use a capability, with what scope | govsec | Consumes the decision |
| Input and output rules, path confinement, output bounds | govsec | Calls the rule at the boundary |
| Dispatch, timeouts, caching, parallel calls, run state | Orchestrator | Provides the declaration the runtime reads |
| Provider routing policy | Orchestrator | Lists providers; does not choose |
| Infrastructure (sandbox worker image, model servers) | Infra | Declares what it needs |

**Rule:** the capabilities pillar produces facts about capabilities. Other pillars produce decisions about them.

## 3. Core concepts

| Concept | Definition |
|---|---|
| **Capability** | A named, versioned thing the platform can invoke. Kinds: `tool`, `agent`, `model`. |
| **Manifest** | The declaration of a capability version. Data only. Validated against a schema. |
| **Identities** | Two per version: `manifest_digest` (the declaration) and a code identity (`code_digest`, or a pinned `tree_sha256` for external source). Both are computed over normalised content. A change to either is a new version. |
| **Version** | Human-readable label (semver) for one identity pair: the `manifest_digest` together with the code identity. A change to either is a new version; a version never changes. |
| **Evidence record** | A summary of one check or evaluation, with the fingerprint of its inputs. Committed as data. |
| **Label** | The tier a capability holds. Computed from manifest and evidence by a versioned policy. Never set by hand. |
| **Revocation** | An event that removes a label or a version from resolution. Effective immediately; see §7. |
| **Grant** | A govsec decision allowing a caller to use a capability at a stated scope. Not stored here. |
| **Handle** | The executable form returned by resolution: spec plus entry point, with both identities attached (`manifest_digest` and `code_digest`, or `tree_sha256` for external). |

## 4. Kinds and labels

| Kind | Labels, lowest to highest | Top label means |
|---|---|---|
| Tool | Declared, Validated, Tested | Tested: contract holds and behavioural tests pass on the pinned version |
| Agent | Declared, Validated, Trusted | Trusted: the outcomes it was built for pass across scenarios, within limits and grants |
| Model | Declared, Validated, Approved | Approved: a named person approved this exact version for stated data classes and uses |

**Declared** is the same for all kinds: the manifest is well formed and complete. **Validated** is the same for all kinds: the contract checks pass. The top label differs because the evidence differs. Tools are deterministic, so tests are enough. Agents are not, so outcome scenarios are required. Models need a human approval record because their terms, licence and provider change outside our control.

The label is a pure function of `(manifest, evidence set, label policy version)`. The validator recomputes it. A label that cannot be recomputed from committed evidence is invalid.

## 5. Lifecycle

```
proposed ──► quarantined ──► declared ──► validated ──► tested | trusted | approved
                 │                                              │
                 └──► rejected                                  ├──► deprecated ──► retired
                                                                └──► revoked
```

| Transition | Trigger | Required |
|---|---|---|
| proposed → quarantined | Agent or author proposes a tool | Requester identity, purpose, requested scope (CAP-TOOL-GEN-2) |
| quarantined → declared | Schema inferred; manifest complete | Schema validation |
| declared → validated | Contract checks pass | Postcondition and contract tests |
| validated → tested / trusted / approved | Behavioural evidence and, for models and generated tools, a named approver | The evidence in the requirements document for that kind |
| any → deprecated | Owner decision, or a newer version supersedes it | Owner |
| any → revoked | Evidence failure, incident, changed dependency, expiry | Automatic for evidence failure and expiry; a named owner or security role for incidents |
| revoked → (anything) | Re-evaluation | Full evidence again. A revoked version is never reinstated; a new version is created. |

Revocation and retirement are different. A **deprecated** version still resolves for existing runs. A **revoked** version does not resolve at all.

## 6. Architecture

```
            ┌─────────────────────────────────────────────┐
            │ catalog/   manifests (git, reviewed)        │
            │ evidence/  summaries + fingerprints (git)   │
            │ policy/    label policy version (git)       │
            └───────────────┬─────────────────────────────┘
                            │ CI
                            ▼
                    ┌───────────────┐       ┌──────────────────┐
                    │  validators   │──────►│ catalog/index    │ generated, not edited
                    │ (recompute    │       │ (name, digest,   │
                    │  labels)      │       │  label, refs)    │
                    └───────────────┘       └────────┬─────────┘
                                                     │
 ┌──────────────┐   admitted commit          ┌───────▼────────┐        ┌─────────────────┐
 │ admission    │──────────────────────────► │   registry     │◄───────│ revocation feed │
 │ service      │  (generated tools only)    │ (library API)  │ check  │ (separate, live)│
 │ quarantine   │                            └───────┬────────┘        └─────────────────┘
 │ store        │                                    │ handle
 └──────────────┘                                    ▼
                                          ┌────────────────────┐
                                          │ orchestrator       │
                                          │ + govsec decision  │
                                          └────────────────────┘
```

### 6.1 Components

| Component | What it is | Storage | Deployed as |
|---|---|---|---|
| Catalog | Manifests, one per capability version | Git, `catalog/` | Files |
| Evidence | Committed summaries and fingerprints | Git, `evidence/` | Files |
| Label policy | Versioned rules mapping evidence to labels | Git, `policy/` | Files |
| Validators | Recompute labels; check manifests and evidence; generate the index | Run in CI | A command |
| Index | Generated list of everything the catalog publishes | Generated at build | A file |
| Registry | The read and resolve interface used by other pillars | Reads the index | A library, plus an optional read-only service |
| Library | Built-in tool and adapter implementations | Git, `library/` | Code, loaded by the registry through a fixed entry point |
| Admission service | Runs generated tools in the sandbox; holds quarantine | Its own store, not in git | A service with the sandbox worker |
| Revocation feed | Live list of revoked digests and versions | Its own store, signed | A small service or a signed file |

### 6.2 Why revocation is not in git

A git commit is too slow to stop a bad tool from running. Revocation must take effect within a bounded time, so it lives in a separate feed. The index says what the catalog published; the feed says what has since been withdrawn. Resolution checks both.

## 7. Resolution and enforcement

### 7.1 Resolution

The orchestrator never reads `catalog/` or `library/` directly. It asks the registry:

```
resolve(name, { range: "^1.2", minLabel: "Tested", kind: "tool" })
  → handle { name, version, manifest_digest, code_digest, label, spec, entry }
```

Resolution fails closed when: no version satisfies the range; the best label is below `minLabel`; the digest is revoked; or the revocation feed cannot be read within its freshness bound.

### 7.2 Enforcement at call time

A resolved handle is not permission to keep calling. The dispatcher re-checks the revocation feed before each call, with a freshness bound (recommended: 30 seconds, see decision 3). If the feed is stale beyond that bound, calls to that kind fail closed.

### 7.3 Record of use

Every call records the digest and label that were in force. This is what lets a run be reconstructed (G10). A run that used a revoked digest is flagged, not silently accepted.

### 7.4 Grants are not resolution

Resolution answers "is this capability fit for use?". It does not answer "may this caller use it here?". The orchestrator calls govsec with the handle's declared scope requirements (`requires`) and the caller's identity, and gets allow, deny or allow-with-constraints. Both must pass.

## 8. Interfaces

### 8.1 Read interface (library)

| Call | Returns |
|---|---|
| `list(kind, filter)` | Entries with label, version and identities. Default `minLabel`: Declared. |
| `describe(name, version?)` | Manifest, evidence references, label, revocation status. |
| `resolve(name, options)` | Handle (see 7.1). |
| `search(purpose, filter)` | Ranked entries matching a stated purpose. Ranking is by label, then fit. |

### 8.2 Handles

| Kind | Handle contains |
|---|---|
| Tool | Spec (input schema, readOnly, idempotent, timeoutMs, postcondition) and an `execute(input, ctx)` entry. `ctx` carries the root supplied by the caller. |
| Agent | Spec (inputs, outputs, limits, required tools and providers) and an `invoke(input, ctx)` entry. The runner is injected by the orchestrator. |
| Model | Adapter entry and a validated endpoint config. Credentials are referenced by environment variable name only. |

### 8.3 MCP surface (P09)

A read-only MCP tool set exposes `search` and `describe`. It never exposes `execute` or `invoke`. Callers discover capabilities through MCP and run them through the orchestrator. This keeps discovery open to standard clients without making the catalog an execution path.

## 9. Manifests

One schema per kind, in `schemas/`. Common fields:

```yaml
kind: tool | agent | model
name: git-log
version: 1.0.0
digest: sha256:<hex>
owner: <role or team name>
summary: <one line>
source:
  type: library | external | generated
  location: library/git-log            # or repo + commit for external
requires:                              # permissions the capability needs; grants are govsec's
  - repo.read
evidence:
  - evidence/tools/git-log/1.0.0/
```

Kind-specific fields:

- **Tool:** `spec` (inputSchema, readOnly, idempotent, timeoutMs), `boundary` (which inputs are paths).
- **Agent:** `outcomes` (each with scenario references), `tools` and `providers` it may use, `limits` (turns, time, calls), `depth`.
- **Model:** `provider`, `modelId`, `version`, `category` (frontier | public | private-local), `dataTerms`, `licence`, `weightsDigest` and `runtime` (local only), `costPer1k`, `egress`.

Manifests are data. They never contain code, credentials or paths outside their own folder.

## 10. Roles

| Role | Can do | Cannot do |
|---|---|---|
| Author | Propose a manifest; write a library entry | Set a label; approve their own work |
| Validator (CI) | Recompute labels; generate the index | Change evidence it did not produce |
| Owner (per domain) | Admit a tool; approve an agent's outcomes; deprecate | Approve a model without the model approver role |
| Model approver | Approve a model version for data classes and uses | Approve a model they proposed |
| govsec | Grant use at a scope | Change a label |
| Security or incident role | Revoke immediately | Reinstate; reinstatement is re-evaluation |

Separation of duties: the proposer of a capability does not approve it. Enforced by the review tool (CODEOWNERS on `catalog/` and `policy/`), not by convention.

## 11. Repository layout

```
taffee-capabilities/
  README.md
  catalog/                   manifests (source of truth)
    tools/  agents/  models/
  index/                     generated; do not edit
  evidence/                  summaries and fingerprints
    tools/  agents/  models/
  policy/                    label policy versions and CODEOWNERS
  schemas/                   manifest and boundary schemas
  library/                   built-in tool implementations
    git-log/  glob/  grep/  unawaited-async/  graph-query/
    generated/               admitted generated tools only, with provenance
  adapters/                  model adapter code
    openai-compat/  stub/
  registry/                  read and resolve interface; MCP surface
  validators/                label computation and manifest checks
  scenarios/                 agent and model scenario sets
  admission/                 the admission workflow spec (no generated code stored here)
  tests/
```

Not in this repository: quarantined generated tools, raw transcripts, raw model outputs, the revocation feed, the admission store.

## 12. Public and private

The project is open source. The line is drawn by what is safe to publish.

| Public | Private (enterprise or controlled storage) |
|---|---|
| Manifests, schemas, validators, label policy | Raw evaluation transcripts |
| Built-in library code | Quarantined generated tools |
| Evidence summaries and fingerprints | Enterprise data-term decisions for frontier providers |
| Public-model approvals we have made for our own use | Approver identities for enterprise approvals |
| Scenario sets that contain no private data | Scenarios that reveal internal systems |

Open-source consumers can run the validators on their own evidence, and can see what we claim and on what evidence. They cannot see private evidence, and they do not inherit our approvals for their own data.

## 13. Failure modes

| Failure | Behaviour |
|---|---|
| Evidence missing or fails to recompute | Label drops to the highest one still supported; logged |
| Index out of date with catalog | CI fails; the index is generated, so this is a build error |
| Revocation feed unreachable | Resolution fails closed; running calls fail closed after the freshness bound |
| Sandbox worker unavailable for admission | Proposal stays in quarantine; nothing is admitted |
| Digest mismatch on load | Load fails; the handle is never created |
| Model provider changes behaviour under the same name | Not detectable from the name; detected only by scheduled re-evaluation (CAP-MDL-8) |
| Agent trusted, then a tool it uses is revoked | Agent's label drops (CAP-AGT-8); calls to the revoked tool fail at dispatch |

## 13.1 Design rules that follow

- No label is set by hand. It is always derived from evidence and policy.
- No version is mutated. A changed digest is a new version.
- No capability runs without a handle, and no handle exists without a digest check.
- No resolution path bypasses the revocation feed.
- No decision about who may use a capability is made in this pillar.

## 14. Phases

Each phase has an exit gate. Later phases do not start until the earlier gate passes.

| Phase | Build | Exit gate |
|---|---|---|
| 0. Manifests and validators | Schemas; manifests for the four built-in tools and `graph_query`; label computation for Declared and Validated | Index generates; validators recompute the same labels twice on a clean checkout |
| 1. Registry read | `list`, `describe`, `resolve` over the index; digest verification on load | Each reference tool resolves; a changed file fails to load |
| 2. Tested tools | Behavioural evidence for the built-in tools, including `git_log`'s missing test | Tools reach Tested; evidence fingerprints reproduce |
| 3. Revocation | Feed, freshness bound, call-time check in the dispatcher | A revoked digest fails at the next call, within the bound |
| 4. Models | Manifests for the three categories; evaluation suite; approval records | One approved model per category, with expiry enforced |
| 5. Agents | Scenario sets; trials; outcome checkers; trust revocation on dependency change | One trusted agent, with a re-run triggered by a tool change |
| 6. Admission | Quarantine, sandbox validation, provenance, admission commit, revocation | A generated tool goes from proposal to Tested through the gate; a failing one stays quarantined |
| 7. MCP surface | Read-only `search` and `describe` | A standard MCP client discovers a tool and gets its label |

Phase 6 is last on purpose. Admission depends on every earlier piece: the schema, the label policy, the revocation feed and the sandbox.

## 15. Decisions needed

1. **Evaluation as its own home.** This design assumes evaluation is separate from capabilities and produces evidence that capabilities consumes. Confirm.
2. **Admission service location.** Inside this pillar as a deployed service (recommended), or in the orchestrator, since it runs work?
3. **Revocation freshness bound.** Recommend 30 seconds. Longer is cheaper and weaker.
4. **Index publication.** Commit the generated index (reviewable, recommended) or build it at deploy only?
5. **Read service.** Start as a library only (recommended), and add a service when a second pillar needs remote reads?
