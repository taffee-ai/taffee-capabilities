# Capabilities: remaining design

Status: proposed. Written after S-001; it covers the work that follows it. What is built since is tracked in the [roadmap](../roadmap.md).

Written for: the engineers who build the rest of the pillar, and the reviewers who check it.

## 1. Principles

1. **Capabilities are records.** The catalog describes what exists, its identities, its labels and its evidence. Anyone who can read the catalog can see that a tool exists. The catalog does not decide who may run it.
2. **Enforcement is by auth.** Who may invoke a capability, and with what scope, is decided by govsec through authentication, grants, and delegation (D-008). Provenance fields never gate a call on their own.
2a. **Creator is not owner.** Two provenance fields, both recorded, neither gating use:
   - `created_by`: who produced the artifact: a person, an agent, or an LLM, with its origin (for example, the entity's own LLM or the platform's). It answers "how did this come to exist?"
   - `owner`: who maintains it and answers for it going forward (upkeep, questions, re-approval). It answers "who do I ask?"
   A tool drafted by a platform LLM for an analyst can have the analyst's team as owner. An entity's contractor can be the creator while the entity's platform team is the owner.
   Where it lives: until a schema change is specified, `created_by` is a separate provenance record next to the manifest, not a manifest field. The S-001 manifest schemas do not accept it. Specifying the change is a later decision.
3. **Labels are evidence, not permission.** A label says how far the evidence goes. Use is decided by auth.
4. **The entity decides the draft source.** The orchestrator returns parts and gaps to the entity. The entity drafts with its own LLM or asks for a platform draft. Verification is identical either way.
5. **Parts are filtered per caller.** The entity sees only the parts it may use, not the full catalog.
6. **Language-neutral contracts.** Tools carry implementations per language, registered as data (LANGUAGES.md v2).

## 2. Where the pillar stands

Built and tested on the Linux test host (S-001 M1 to M5): canonical JSON and digests, manifest and index schemas, the language registry, implementations and identity, the publication ledger, the conformance runner, the index builder, and the five tool manifests declared with no implementations.

Not built: the registry service and library resolution (S-002), revocation (S-003 as a govsec control), the read surface (S-004), the catalog generator (S-005), evaluation (S-006), the prototype adoption (S-007), and everything after them.

## 3. Remaining work

### 3.1 Registry (S-002), next

- `list`, `describe`, `resolve(name, range, minLabel, languages)`.
- Precedence, fixed (LANGUAGES.md v2 §8): (a) name, range, kind; (b) `minLabel`; (c) implementation availability for the caller's languages, returning `NoImplementation` with the tool's languages; (d) `manifest_digest`; (e) the chosen implementation's `code_digest`.
- `SourceUnavailable` is retired for tools. Agents and models keep their source rules.
- The five S-001 tools resolve to `NoImplementation` until T-001 gives them implementations.
- A caller's language list is ordered by preference. No fallback to unnamed languages.
- The local library is unfiltered. It has no caller identity, lists every entry that has a manifest, and is for trusted in-process use only. Per-caller filtering is S-008's job, at its authenticated boundary (§3.3). It must not be relied on here.

### 3.2 Revocation (S-003, a govsec control)

- The revocation decision is govsec's. It names a capability version, with a reason, an actor, a time and a reference.
- The transport is the signed, append-only revocation feed specified in S-003. Its client lives in capabilities. The feed is run and signed by govsec, which owns the policy and the keys. Open decision §5.2 confirms this.
- The registry and dispatch check the feed at each call, within a freshness bound of 30 seconds. They fail closed when the feed cannot be read within the bound.
- Labels can drop automatically (an evidence failure, a changed identity). That raises a request to govsec. Only govsec's policy sets a revocation.

### 3.3 Read surface and service (S-004, S-008)

- The catalog and registry service (S-008) is run by the platform, in the stack. Until S-005 publication exists, it serves a validated local index (the S-001 output, checked by the ledger gate). Once S-005 publishes, it serves the published index.
- S-008 has an entry condition: the govsec auth contract (D-008) must exist and be enforced at the service edge. No per-caller filtering is claimed before that contract exists.
- Visibility is decided at that authenticated boundary: the caller's identity, from auth, filters what `list`, `describe` and `resolve` return. A caller-supplied field never controls visibility.
- The local library (S-002) has no caller identity. It lists every entry that has a manifest, and it is for trusted in-process use only. It does not filter by caller.
- MCP (S-004) is a read-only view of the same service: `search` and `describe`, with the same auth. It exposes no execution.
- The revocation feed (S-003) is a separate transport, not part of the S-008 service.

### 3.4 Catalog publication (S-005)

- `taffee-core/catalog/capabilities-index.yaml` is generated from the index. It carries the index's `inputs_digest` and `index_digest`, and the Git SHA of the committed index, attached externally.
- Publication requires the ledger gate (versions.yaml) for every tool version.

### 3.5 Tool labels and conformance (T-001)

- Real implementations for the five tools, in each language they will run in, each with conformance cases in `conformance/<tool>/cases.json`.
- Validated and Tested are awarded here, from the entry format, export checks and conformance results.
- Value-level expectations (a `json_equals` operator) are proposed here with concrete cases, not added silently.
- The TypeScript runner needs Node 22.6+ on the test host, in the test image.

### 3.6 Admission of generated tools (T-002, T-003)

- A generated tool enters quarantine, is run in the sandbox (S-011), gets generated tests that must pass, and is admitted by a named reviewer with a recorded scope.
- Admission is a record of the review, not a gate on use. Use is decided by auth.
- The admitted tool is committed with provenance (T-003).

### 3.7 Composition and gap detection (S-016, S-017, A-009)

- S-016 validates a draft before any trial: its manifest, its code through quarantine, and its composition. A draft that references an unknown or unadmitted part is refused, with the part named.
- S-017 searches the registry for parts matching an intent's objective, and returns matches and unmet objectives as structured data. Results are filtered to what the caller may see.
- A-009 builds a new agent manifest from existing agents, tools and model roles. A composition inherits the weakest label of its parts. Each part must be usable for the intent's data class.

### 3.8 Agents (A-001 to A-008)

- A-001: a declaration and scenario trials, with pass rate and fingerprint.
- A-002: trust, re-run on change. A change to a tool, a model version or the scenario set drops the label.
- A-003: manifest types for capability agents and framework agents.
- A-004: the delegation check on accept. A run is accepted only when its delegation verifies and covers the agent's `requires`.
- A-005 to A-008: limits and isolation, long-running registration with heartbeat, the short-lived lifecycle, and outcome records.

### 3.9 Models (M-001 to M-006)

- An approval (M-001) is an evidence record. It earns the Approved label for a model version, route, data class and environment, with expiry. It is not a runtime permission.
- Runtime permission comes from govsec: auth, and govsec's data-class policy.
- A caller may ask for `minLabel: Approved` as a qualification filter. That filter can refuse a below-label result. It is not a grant.
- M-002 route validation, M-003 model profiles, M-004 CLI transports, M-005 call records with cost, M-006 the role view.
- Routing is out of scope. Traffic goes to a model router.

### 3.10 Evaluation (S-006, E-001, E-002)

- Scenario generation from an intent, with requester review (E-001), and a trial runner for drafts with the same checkers as A-001 (E-002).
- Proposed home: an isolated evaluation component inside `taffee-orchestrator`, with versioned contracts and results consumed by capabilities. This is pending the owner's decision (§5.1). Capabilities consumes results without importing evaluation code.

### 3.11 Prototype adoption (S-007)

- The prototype's dispatcher and adapter consume the registry, once S-002 exists. The prototype is reference only, so this is a consumer change, not a code move.

## 4. Order

Each step respects the dependencies in the roadmap.

1. S-002 registry, with `NoImplementation`, caller preferences and the local library, tested against a fixture library.
2. S-008 registry service, after the govsec auth contract is in place (its entry condition). Serves a validated local index until S-005 publication.
3. S-017 gap detection, which depends on S-008.
4. S-003 revocation client and the feed, with govsec as the policy owner.
5. T-001 implementations and conformance for the five tools, which depend on S-002 and S-003. This turns `NoImplementation` into real resolution for them.
6. S-004 read surface (MCP view of S-008).
7. S-005 catalog publication, after the ledger and a commit is approved.
8. S-006 evaluation placement, then E-001 and E-002, then A-001 trials, then A-002 trust.
9. S-009 call-time validation, then S-011 sandbox interface, then T-002 admission for generated tools (depends on S-003, S-011 and A-002), then T-003 provenance.
10. S-016 draft validation: its code path goes through T-002 quarantine, so it comes after T-002.
11. A-009 composition (depends on S-016 and A-001).
12. S-007 reference-consumer validation: the prototype's dispatcher and adapter read the registry, to prove the interface. It is not a platform repository deliverable.

## 5. Decisions needed

1. **Evaluation's home.** Proposed: an isolated evaluation component inside `taffee-orchestrator`, with versioned contracts and results that capabilities consumes. No sixth repository. **Owner decision pending.**
2. **Revocation.** The feed transport and client stay in capabilities (S-003). Policy and signing belong to govsec, which must confirm it runs and signs the feed. Freshness is 30 seconds, failing closed. **Cross-repo decision pending: govsec signing ownership.**
3. **Model approval.** An evidence record that earns Approved, with runtime permission from govsec. Confirm, and update the M-001 wording that currently makes approval a universal usability gate.
4. **Read filtering.** Per-caller filtering at the authenticated S-008 boundary, from auth. Confirm.
5. **Registry service.** Platform-run in the capabilities stack, with auth at the edge. Until S-005 publication, it serves a validated local index; after, the published index. **Confirm.**

## 6. Non-goals

- Owner-based gating of any kind.
- Model routing inside this pillar.
- Generated agents that use platform tools before they are verified and promoted.
- Silent language fallback.
