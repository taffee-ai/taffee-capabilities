# Capabilities: operations and lifecycle

Status: proposed. Items are built only when a roadmap feature takes them on. Items marked OPS-nn are gaps not yet on the roadmap.

Written for: the people who run the platform, the tool authors who add to it, and the reviewers who approve changes.

## 1. What the registry service does, and does not do

The registry service is the read path. It is not the write path, and it is not the revocation control.

| The service does | The service does not |
|---|---|
| Serves `list`, `describe` and `resolve` to authenticated callers, filtered to what each caller may see (S-008) | Add, change or delete entries |
| Verifies the index on every call, and fails closed | Publish an index. Publication is an operator step (OPS-01) |
| Exposes health signals: index verification, entry errors, revocation feed freshness | Revoke. Revocation is govsec's control (S-003) |
| Reads its index from the publish pipeline | Decide what is trusted. Labels come from evidence and policy |

Why the split: if the service accepted writes, it would be a mutable store that could change without review. Keeping the service read-only prevents catalog mutation through the service, so every change goes through review. The intended future contract is that the service serves only a signed index (OPS-01). Today the index is verified by digest only, and nothing is signed. A read-only service does not stop a compromised service from serving altered responses. Signing (OPS-01) and the caller's own check (`capctl verify`) cover that.

## 2. Roles

| Role | Does | Does not |
|---|---|---|
| Capability author | Writes a manifest, implementations and conformance cases; proposes a version | Approve their own entry; set labels |
| Maintainer (the `owner` field) | Answers for the entry after publication. A record of who maintains it | Gate use. Ownership is provenance, not permission |
| Reviewer | Reviews a change and its evidence; signs the review decision | Edit an entry they reviewed without a new review |
| Platform operator | Runs CI, runs the publish step, runs the registry service, watches its health | Decide what is trusted |
| Security role (govsec) | Sets revocations and grants; owns the revocation feed's policy and signing | Edit catalog content |
| Generator or agent (for drafts) | Produces a draft manifest, code and scenarios | Publish anything. A draft is a candidate only |

## 3. Review workflow

Review states describe where a change is in the process. They are not labels. Labels are computed by policy from evidence (S-001 §7). A reviewer never sets a label.

```
draft ──► in_review ──► approved_for_publication ──► published
              │                 │
              └─ rejected       └─ (publication is an operator step, OPS-01)
```

- **draft:** an author's branch. Not in the catalog.
- **in_review:** a change set with CI results attached. CI runs `capability-validate`, the conformance cases, and the ledger check (tools).
- **approved_for_publication:** a reviewer has signed the review decision (see §6, decision 3).
- **published:** included in the index that the registry serves.

Deprecation and withdrawal are not in this workflow. They are proposed in §4 under OPS-02.

## 4. Changes to published entries (proposed)

None of these exist in the current schemas or registry. Each needs its own gate.

- **Deprecated (OPS-02, proposed).** A newer version exists. The older version stays resolvable. A `deprecated` notice would appear in `describe`. Whether it also appears in the `Match` is undecided (§6, decision 2). A newer version does not withdraw an older one that is still resolvable.
- **Revoked (S-003, govsec).** Calls to the version fail once the revocation is effective. Effective means within the freshness bound (30 seconds recommended), not at the instant govsec submits it. Revocation is a govsec control, not a catalog edit.
- **Withdrawn (OPS-02, proposed).** Removal from future indexes. Still a reviewed change. For tools, the version identity stays in the ledger, so it cannot return with different content. Only tools have a ledger today. Withdrawal of agents and models is gated on OPS-03.

## 5. Procedures

### Add a tool

1. The author opens a change with the manifest, implementations, conformance cases and the ledger entry.
2. CI runs the validators. A failure stops the change.
3. A reviewer reads the conformance output and records the review decision.
4. The operator runs the publish step. The index is rebuilt, and the registry serves it.

### Add an agent or model

Agents need scenario trials recorded with a pass rate and fingerprint. Models need an approval record for the route, data class and environment. Both are reviewed, then published. The version ledger does not yet cover agents or models (OPS-03).

### Add a generated tool or agent

A draft goes through admission (T-002): quarantine, sandbox run, generated tests, and a named reviewer's decision with a recorded scope. Only then does it enter review like any other entry.

### Stop using a version

1. Govsec sets a revocation. Calls fail once it is effective (within the freshness bound).
2. The maintainer publishes a fixed version. The revoked version is not reinstated; a new version is published instead.

### Remove an entry (proposed, OPS-02)

Removal needs the same review as addition. For tools, the version identity stays in the ledger. Agents and models are gated on OPS-03. Removing a manifest while its version is still resolvable would leave callers pinned to a version that is no longer listed, so revocation comes first.

## 6. Decisions needed

1. Who holds the publish step: one operator, or two people (a publisher and a second reviewer)? Recommend two.
2. Whether a deprecation notice appears in `resolve`'s `Match`, or only in `describe`. Recommend `describe` only. A `Match` field is not part of OPS-02; it needs its own decision.
3. Where the review decision is stored: in the change, or in a separate signed record. Recommend a separate signed record, so it cannot be edited with the change.
4. Who signs the index, and where the key lives (OPS-01). Govsec or the operator. Not decided.

## 7. Gaps

| Gap | Roadmap item | Status |
|---|---|---|
| CI runs the validators, conformance and ledger on every change | OPS-04 (new; S-005 is the umbrella generator and does not cover CI) | Proposed, not built |
| Index signing and the publish step, with an operator approval | OPS-01 (new; S-005 is the umbrella generator and does not cover signing) | Proposed, not built |
| Registry service run by the platform, behind auth | S-008 | Not built |
| Revocation control and feed, owned by govsec | S-003 | Not built; placement open |
| Reviewer decisions stored as signed records | new | Proposed |
| Deprecation and withdrawal, with their fields in the schemas. A `Match` field is undecided (§6, decision 2) | OPS-02 | Proposed |
| Version ledger for agents and models | OPS-03 | Proposed |
| Admission of generated tools | T-002, T-003 | Not built |
| Call records and health signals | S-012 | Not built |
| Health dashboard and alerts for the signals below | new | Not built |

## 8. Monitoring

| Signal | Who watches | What it means |
|---|---|---|
| Index verification failures (`IndexError`) | Operator | The index was altered or corrupted; stop serving it |
| Entry errors per row (`EntryError`) | Operator, maintainer | An implementation folder is missing or a link was placed; the maintainer fixes the entry |
| Integrity errors (`IntegrityError`) | Operator, security | Bytes changed after publication; investigate before anything else |
| `NoImplementation` by tool and language | Maintainer | Callers want a language the tool lacks; a demand signal |
| Revocation feed freshness | Operator, security | A stale feed fails calls closed; restore it first |
| Call records (S-012) | Security, auditors | Who called what, with which digest and label |
