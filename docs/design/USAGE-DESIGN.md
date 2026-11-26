# Capabilities pillar: how an enterprise uses it

Status: design. The names below are the intended public names; where code differs, the code and its tests are authoritative.

Written for: the enterprise platform team that deploys Taffee, the engineers who build agents on it, and the approvers who decide what is allowed.

## 1. The short answer

An enterprise user does four things with this pillar:

1. **Finds** a capability: a tool, a model or an agent, by purpose and label.
2. **Uses** it: a tool is invoked, an agent is run, and a model is reached through a role.
3. **Governs** it: approves a model for a route and data class, admits a generated tool, or revokes something.
4. **Checks** it: verifies that what they are running is what was evaluated.

Most users only do the first two. The platform team and approvers do the last two.

## 2. Vocabulary

Use these words in documentation, code and conversation. Each has one meaning.

| Word | Meaning | Not |
|---|---|---|
| **Capability** | Anything the platform can invoke or run: a tool, an agent or a model | A feature, or a function in code |
| **Tool** | A callable with a declared input and output, which reads or changes something outside the model | The model's own answer |
| **Agent** | A process that decides which tools to use and in what order, to reach an outcome | A model. An agent uses models. |
| **Model** | A pinned version of a language model, reached through a route | A provider, or a product name |
| **Route** | How a model is reached: API, gateway, private network, or a CLI session | A network address on its own |
| **Role** | A name for a use case, such as `general-reasoning`, that maps to a route and model per environment | A user role or a permission |
| **Environment** | `development` or `deployed`. Chosen when a process starts. | A deployment target |
| **Label** | The evidence level of a capability, from the catalog | A permission |
| **Approval** | A named person's decision that a model may be used on a route, for a data class, in an environment, until a date | A label, which is computed |
| **Revocation** | A decision that stops a capability (or one version) from being used from a given time on | A deletion |
| **Delegation** | A signed statement that an agent acts for a user, with a narrower set of permissions than the user's | A login |
| **Grant** | A govsec decision that a named caller may use a capability at a given scope | A label |

Labels say what evidence exists. Grants say who may use it. Both have to pass.

## 3. What they call

### 3.1 Catalog (read)

Used by anyone who needs to find something. Read-only.

```
GET  /v1/capabilities?kind=tool&minLabel=Tested&q=read%20files
GET  /v1/capabilities/{name}                 # all versions
GET  /v1/capabilities/{name}/{version}       # manifest, label, digest, evidence refs
GET  /v1/index                               # signed index, with its digest
```

The `q` parameter is for search over name, summary and `when_to_use`. For five tools this is a plain text match. A larger catalog can add vector search later, as a derived view.

A catalog response never contains a way to execute the capability. Executing is a separate call.

### 3.2 Resolution (read, but a decision point)

```
POST /v1/resolve
{ "name": "git-log", "kind": "tool", "range": "^1.0.0", "minLabel": "Tested" }
→ 200 { "name": "git-log", "version": "1.0.0", "digest": "sha256:...",
        "label": "Tested", "revocation": "checked", "requires": ["repo.read"] }
→ 409 { "code": "BelowLabel", "have": "Validated", "need": "Tested" }
→ 410 { "code": "Revoked", "reason": "...", "seq": 42 }
```

`resolve` says whether a capability may be considered. It does not grant a caller permission to use it. The response tells the caller what the capability `requires`, and the grant check is a separate call (§3.4).

### 3.3 Invocation

Tools have two modes, decided by the tool's manifest:

| Mode | Call | Used for |
|---|---|---|
| Library | `execute(input, ctx)` in the caller's process | Local tools that read the caller's repository: `git_log`, `glob`, `grep`, `unawaited_async` |
| Service | `POST /v1/tools/{name}/{version}/invoke` | Tools that hold credentials or reach external systems: GitHub, Jira, MCP servers |

Service invocation takes the same input, plus the caller's identity envelope and a grant reference:

```
POST /v1/tools/github-issues/1.2.0/invoke
Authorization: Bearer <caller's bearer token>
{ "input": { "repo": "...", "query": "..." },
  "grant": "grant:9f2c...",
  "callId": "c-0001" }
→ 200 { "ok": true, "output": "...", "digest": "sha256:...", "revocationSeq": 42 }
→ 403 { "code": "GrantDenied" }
→ 410 { "code": "Revoked" }
```

The response always names the digest and the revocation sequence it was checked against.

### 3.4 Grants (govsec, consumed here)

Capabilities does not decide grants. The caller asks the grant service, and passes the result with the invocation. The interface is the one in D-008.

### 3.5 Agents

```
POST /v1/agents/{name}/{version}/runs
{ "goal": "...",
  "delegation": "<signed delegation token>",
  "environment": "deployed",
  "callId": "r-0001" }
→ 202 { "runId": "r-0001", "status": "running" }

GET  /v1/runs/{runId}                # status, outcome, records
GET  /v1/runs/{runId}/records        # every tool and model call in the run
```

A run is accepted only if its delegation verifies and grants at least the agent's `requires`. The agent's trust label is checked at accept time (A-002).

### 3.6 Models

Callers do not usually call a model directly. They call the model router with a role, and the router reaches the route for that role in the current environment. Our part is the approval and the record. The call itself is outside this pillar.

```
POST {router}/v1/complete
{ "role": "general-reasoning", "dataClass": "internal", "transcript": [...], "tools": [...] }
```

Read-only view of what a role maps to:

```
GET /v1/roles?environment=deployed
→ [ { "role": "general-reasoning", "dataClass": "internal",
      "route": "team-gateway", "model": "model-a@2025-06-01", "approved": true } ]
```

### 3.7 Governance (approvers and owners)

```
POST /v1/approvals          # model approval: model, route, dataClass, environment, expiry, evidence
POST /v1/admissions         # admit a generated tool: proposal id, scope, reviewer
# Revocation is govsec's control, not a capabilities endpoint (S-003). It names a digest or name@version, a reason and a reference.
```

Each write needs the caller to hold the matching role. The proposer of a capability cannot approve it.

### 3.8 Verification (anyone)

```
capctl verify git-log@1.0.0
→ digest      OK    sha256:...
→ index       OK    commit 3f2a..., index digest sha256:...
→ evidence    OK    fingerprint sha256:..., summary matches digest
→ revocation  OK    checked at seq 42, fresh (8s)
→ label       Tested
```

A failing check prints the reason and exits non-zero. This is the check a consumer runs before trusting what they got.

## 4. How it is deployed

The same contracts run in two ways. The enterprise chooses per capability kind.

| Piece | Library | Service |
|---|---|---|
| Catalog and index | Read from a checkout | Read-only catalog service |
| Resolution | In-process | Catalog service |
| Local tools | Yes | Not needed |
| Credentialed tools | Not recommended | Yes |
| Model router | Not part of this pillar | Yes, outside this pillar |
| Revocation feed | Read from a file, for development only | Service in the stack |
| Agent runs | Development: the IDE session | Deployed: a separate process or service |
| Grants | Local decision, development only | Govsec implementation (ours or theirs) |

The stack that runs the catalog, the revocation feed and the Context service comes up with one compose file. The catalog has no state of its own beyond the index it serves.

## 5. Walkthroughs

### 5.1 A developer builds an agent

1. Searches for tools that fit the job:
   `capctl search "read repository files" --min-label Tested`
2. Reads what one of them requires:
   `capctl describe grep@1.0.0`
3. Writes the agent manifest, listing the tools it may use, the roles it needs, its limits and its outcomes.
4. Runs it in development, in their IDE, where it uses their own plan and no API budget.
5. Adds scenarios that check the outcomes (A-001). Runs them, and reads the pass rate.
6. Hands the manifest and the scenarios to the owner for trust review (A-002).

### 5.2 The platform team deploys the stack

1. Brings up the stack: the catalog and registry service and the Context service. The revocation feed is govsec's, and where it runs is an open decision.
2. Points each environment at its roles file (development and deployed differ).
3. Points roles at routes that exist in their environment, and at the router endpoint.
4. Runs `capctl verify` for every capability the stack will serve.
5. Starts agents only after verification passes.

### 5.3 An approver approves a model

1. Reads the evaluation summary and the route's recorded terms.
2. Confirms the route's data terms for the data class.
3. Records the approval with an expiry date, through `POST /v1/approvals`.
4. Sees the role become usable in the next `GET /v1/roles`.

### 5.4 Something goes wrong

1. A tool misbehaves in production.
2. The security role sets a revocation in govsec, naming the digest or name@version and the incident reference.
3. Within the freshness bound, the next invocation fails with `410 Revoked`.
4. Records for calls made before the revocation keep their `revocationSeq`, so the affected calls can be found.
5. The owner releases a fixed version. The revoked digest stays revoked.

## 6. Errors callers see

| Code | Meaning | What the caller does |
|---|---|---|
| `NotFound` | No version in range | Check the name and range |
| `BelowLabel` | Exists, but not at the required label | Lower `minLabel` if acceptable, or use another capability |
| `IntegrityError` | Bytes don't match the digest | Report to the owner; do not retry |
| `Revoked` | Withdrawn | Use a newer version, or another capability |
| `RevocationStale` | Revocation feed is too old to trust | Retry after the feed recovers. Calls fail closed. |
| `GrantDenied` | Caller lacks the grant | Ask the owner for access |
| `ValidationFailed` | Input does not match the declared schema | Fix the input; the error names the field |
| `PostconditionFailed` | Output does not meet the declared check | Treat the result as failed |
| `Timeout` | Ran past the declared limit | Retry only if the tool is declared idempotent |

Every error has a `code` and a message that names the next step. Errors don't include secrets or the content of records.

## 7. What enterprises get that a library alone would not

- A single place to see what exists, at what label, and who approved it.
- The ability to stop a capability within a bounded time, without a deploy.
- A record of every call that names the digest, label, revocation sequence and grant in force.
- A check any consumer can run on what they received.

## 8. Not in this design

- Model routing. Traffic goes to a router outside this pillar.
- Grant decisions. Those belong to govsec.
- The evaluation runner. Its home is an open decision (S-006).
- Consumer support and licences. Covered when the release is prepared.

## 9. Open decisions

1. **Service language and framework.** Recommend Python, matching the Context service.
2. **Whether the catalog API is REST or MCP-only at first.** Recommend REST for the enterprise, with MCP for discovery by agents.
3. **Whether `capctl` is a separate tool or part of the platform CLI.** Recommend part of the platform CLI.
4. **Whether a deployed agent can be called by another agent directly.** Recommend no. Calls go through the orchestrator, so every call has a record and a grant.
