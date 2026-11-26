# M-001: Model routing design

Status: design, not yet implemented. Companion to [SPEC.md](SPEC.md), which defines the approval records and the access kinds. This document defines how a call reaches an approved model.

Written for: the engineers who build the model router, and the orchestrator and agent authors who call it.

## 1. What the router does

A caller asks for a **role** ("general reasoning", "confidential summary") and passes the **data class** of the content. The router returns a model reply. It decides which approved model and which route serve the role in the current environment, enforces the approval, and records what it did.

The caller never names a provider, a gateway or a model version.

## 2. Vocabulary

| Term | Meaning | Defined in |
|---|---|---|
| **Model** | A pinned version of a model, e.g. `model-a@2025-06-01` | SPEC §5 |
| **Route** | How a model is reached: access kind, account, endpoint, data terms | SPEC §11 |
| **Access kind** | `api`, `gateway`, `router`, `private`, `cli` | SPEC §11 |
| **Transport** | For `cli` only: `direct` (child process) or `mailbox` (maildepot) | SPEC §11.4 |
| **Role** | A named use case, mapped per environment to one route and one model | This document |
| **Environment** | `development` or `deployed`. Set once per process. | SPEC §11.5 |
| **Data class** | `public`, `internal`, `confidential`, or as defined by govsec | CAP-MDL-11 |
| **Approval** | A record binding (model, route, data class, environment) to a named approver | SPEC §5.2 |

## 3. Configuration

Three kinds of config. All are data, validated at load, and none contains a secret.

### 3.1 Routes: `catalog/routes/<name>.yaml`

```yaml
name: team-gateway
access: gateway                  # api | gateway | router | private | cli
protocol: openai-compatible      # openai-compatible | anthropic-native | cli-json
endpoint: https://<gateway>/v1
apiKeyEnv: TEAM_GATEWAY_KEY      # env var name only
timeoutMs: 30000
account:
  owner: <role or person>        # who pays and is accountable
  plan: <plan name or null>
dataTerms:
  retention: unknown             # none | days:N | unknown
  trainingUse: unknown           # yes | no | unknown
  region: <region>
  allowedDataClasses: []         # empty until the approver fills it from the terms
```

A `cli` route has `transport`, `cli` (binary), `cliVersion`, `plan`, and `denyTools` (SPEC CAP-SUB-4).

A `router` route has `candidates`: the named set of models it may choose. Each candidate has an approval.

### 3.2 Models: `catalog/models/<name>/<version>/manifest.yaml`

SPEC §5.1. Model manifests hold identity, category and model profile. The profile is declared data, not code:

```yaml
profile:
  toolCalls: true
  parallelToolCalls: false
  jsonMode: true
  maxContextTokens: 128000
  maxOutputTokens: 4096
```

The adapter reads the profile. It never branches on the model's name.

### 3.3 Roles: `catalog/roles/<environment>.yaml`

```yaml
environment: development
roles:
  general-reasoning:
    dataClass: internal
    primary: { route: team-gateway, model: model-a@2025-06-01 }
    fallback: []                 # same data class only; each listed item needs its own approval
    requires: { toolCalls: true }
  confidential-summary:
    dataClass: confidential
    primary: { route: private-cluster, model: model-b@v3 }
    fallback: []
    requires: { toolCalls: false }
```

There is one file per environment. A role that exists in `deployed` but not `development` is an error in development, not a silent fallback.

Deployment repos may override a role's `primary` in their own config. They may not change `dataClass` without a new approval.

## 4. Resolution

For each call, in order:

1. **Environment.** Read from the process, set at start (`TAFFEE_ENV`). Missing: refuse to start.
2. **Role.** Look up the role in that environment's file. Missing: `UnknownRole`.
3. **Data class.** The caller's `dataClass` must equal the role's `dataClass`. A mismatch is `DataClassMismatch`. The caller cannot choose a looser class to get a different model.
4. **Candidate list.** `primary`, then the `fallback` list in order.
5. **Per candidate**, stop at the first that passes all checks:
   - The model's label is Approved for `(model, route, dataClass, environment)` (SPEC §5.5).
   - The route's `allowedDataClasses` includes `dataClass`.
   - The model's `profile` satisfies the role's `requires`.
   - The route's account and plan are present (for `cli`, the plan).
   - Approval is not expired and not revoked (S-003 feed, when live).
6. **No candidate passes:** `NoApprovedRoute`, with the reason for each candidate. Never a silent downgrade.
7. **Call** the route's adapter with the neutral request.
8. **Record** (§6).

For `router` routes, step 7 returns the model the router chose. That model must be in the route's approved `candidates`; otherwise the call fails with `UnapprovedChoice` and the reply is discarded.

## 5. Caller interface

```ts-like; language follows the roadmap decision
complete({
  role: string,
  dataClass: DataClass,
  transcript: Turn[],          // neutral history, not provider format
  tools?: ToolSpec[],          // declarations from the tools registry (S-002)
  limits?: { timeoutMs?: number, maxOutputTokens?: number },
  callId: string               // caller's id, carried into the record
}) -> { reply: ModelReply, record: CallRecord }
```

Rules:

- `tools` is the set the caller allows this turn. The adapter offers only these. A model that asks for another tool gets an error observation.
- `limits` can only lower the route's limits, never raise them.
- The reply type is the one from the tools design (`tool_call` or `final_answer`). Only one tool-call shape exists.

## 6. Call record

Every call writes one record, whether it succeeds or fails:

```json
{
  "callId": "...",
  "time": "...",
  "environment": "development",
  "role": "general-reasoning",
  "dataClass": "internal",
  "route": "team-gateway",
  "access": "gateway",
  "transport": null,
  "model": "model-a@2025-06-01",
  "chosenBy": "role",          // or "router" when the route is a router
  "approvalFingerprint": "sha256:...",
  "outcome": "ok | error",
  "error": null,
  "usage": { "inputTokens": 0, "outputTokens": 0 },
  "latencyMs": 0,
  "fallbackFrom": []
}
```

The record never contains prompt or reply text. Content is held in controlled storage, referenced by `callId`. This follows the requirement that raw evidence is kept out of committed records.

Cost fields come from `usage` and the route's measured cost, so spend can be attributed by role and by account (G09).

## 7. Adapters

| Adapter | Access | Protocol | Notes |
|---|---|---|---|
| openai-compatible | `api`, `gateway`, `private` | OpenAI chat format | Covers OpenRouter, LiteLLM and most local servers. Reuses `openai-compat` from the prototype. |
| anthropic-native | `api` | Anthropic messages | Only if a route needs it and the gateway can't translate |
| cli-direct | `cli` | Child process, JSON output | Verification pending on the test host (SPEC §11.2) |
| cli-mailbox | `cli` | maildepot task | Asynchronous; timeout handling (SPEC CAP-MBX-2) |
| router | `router` | Calls the router's own API, reads the chosen model | Chosen model checked against the candidate set |

The stub adapter remains for tests and offline runs. It is never in a role config outside tests.

## 8. Failure handling

| Failure | Behaviour |
|---|---|
| Timeout | Error `timeout`. Retry only if the caller marks the call idempotent; the default is no retry. |
| Provider error (5xx, 429) | Error with kind. 429 is `quota` for `cli`, `rateLimited` otherwise. |
| Quota (cli) | `quota`. No fallback unless the role lists it (SPEC CAP-SUB-8). |
| Bad response | `invalid_response`. |
| Route unreachable | `network`. |
| Listener absent (mailbox) | `timeout` after the route's timeout; the claim is released. |
| Approval revoked mid-run | The next call fails with `NoApprovedRoute`. A call already running completes and is marked `revoked_during_run`. |

No failure path switches data class.

## 9. Environments in practice

- **Development:** roles point at `cli` routes (mailbox or direct) on the developer's plan, and at a team gateway for anything that needs an API. The developer's IDE agents do the work; project agents do not spend model tokens on the API.
- **Deployed:** roles point only at `api`, `gateway`, `router` and `private` routes. A `cli` route in the deployed role file is a validation error.

The validator enforces the second rule, so a deployment cannot accidentally run on a subscription.

## 10. Components and repository placement

```
taffee-capabilities/
  catalog/
    routes/            route definitions
    roles/             development.yaml, deployed.yaml
    models/            model manifests and approvals (SPEC §5)
  models/
    router.*           the wrapper and resolution (§4)
    record.*           call record writer (§6)
    config.*           loads and validates routes, roles, models
    adapters/          one per row of §7
  validators/
    route.*  role.*    validation rules (added to S-001's validators)
```

The orchestrator imports `models/router` and nothing else from this area. It never reads the catalog directly.

## 11. Test plan

| Test | Assertion |
|---|---|
| Unknown role | `UnknownRole` |
| Data class mismatch | Caller passes a looser class; `DataClassMismatch` |
| Approval missing | Model with no approval for the environment; `NoApprovedRoute` with reason |
| Fallback same class only | A fallback whose route lacks the class is skipped, with the reason recorded |
| Fallback needs approval | A fallback without its own approval is rejected at config validation |
| Cli in deployed | A `cli` route in `deployed.yaml` fails validation |
| Router choice outside set | Router returns a model not in candidates; `UnapprovedChoice`, reply discarded |
| No silent downgrade | Primary fails with `quota`; no other route is tried unless listed |
| Tools limited | A model requests a tool not in `tools`; error observation, not execution |
| Limits only lower | Caller asks for more output model tokens than the profile allows; the lower value is used |
| Record on failure | Every failure path still writes a record with `outcome: error` |
| No content in record | A record never contains prompt or reply text (scan test) |
| Environment required | Process with no `TAFFEE_ENV` refuses to start |
| Expired approval | Approval past `expires_on`; `NoApprovedRoute` |
| Adapter parity | For a fixed transcript and tool set, the openai-compatible request body equals the prototype's body |

## 12. Phasing

| Step | Build | Exit gate |
|---|---|---|
| 1 | Config loader and validators; resolution with stub adapter; call record | All non-adapter tests pass |
| 2 | openai-compatible adapter; `gateway` and `private` routes | Adapter parity test passes; one approved model per environment resolves |
| 3 | `cli-direct` adapter | Probe passes on the test host (needs the install decision) |
| 4 | `cli-mailbox` adapter | Round trip through a real listener; timeout releases the claim |
| 5 | `router` access | Choice check works with a recorded candidate set |

Steps 1 and 2 need nothing outside this repository. Steps 3 and 4 depend on the CLI decision. Step 5 depends on having a router route to test.

## 13. Decisions

1. **Environment selection.** Recommend a process-level `TAFFEE_ENV`, set by the deployment, not by the caller.
2. **Role files location.** Recommend `catalog/roles/`, one per environment, versioned with the catalog. Deployments override `primary` only.
3. **Fallback default.** Recommend none. Each fallback is explicit, same class, and approved.
4. **Retries.** Recommend none by default. Idempotent retries only when the caller says so.
5. **Library or service.** Recommend a library first. A service is added when a second process needs routing without the library.
