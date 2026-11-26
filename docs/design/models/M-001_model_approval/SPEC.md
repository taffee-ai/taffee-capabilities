# M-001: Model approval records

Status: specified, not yet implemented. Phase 4. Depends on S-002. Roadmap: [../../../roadmap.md](../../../roadmap.md).

Written for: the model approver role, the engineers who will record approvals, and the owners of data-classification policy.

## 1. Purpose

Make a model usable only when a named person has approved the exact version, for stated data and uses, on recorded evidence. The approval expires, and it is revoked by changes the approver cannot control: a new version, a retired model, changed terms, or a failed evaluation.

## 2. Scope

In scope:
- Model manifests for the three categories: frontier, public, private-local.
- The approval record format and its validation.
- The evaluation summary a model must have before approval (the scenario set and summary format; the runner is decided in §10).
- Expiry and version-change enforcement in the registry.
- Endpoint and egress checks, using the provider validation already in the prototype as the reference.

Out of scope:
- Running the full evaluation suite. Its home is decision S-006. This feature defines the summary format the suite must produce.
- The data-terms table itself. govsec maintains it; this feature reads it.
- Choosing which models to approve. The approver chooses; this feature records.
- Adapter code. The adapters exist in the prototype (`openai-compat`, `stub`); S-007 moves them.

## 3. Current state

- Provider validation exists in the prototype's `harness/_src/models.ts` (`parseProviderConfig`): unknown fields rejected; names unique; `apiKeyEnv` required and must be an env-var name; endpoint `http` or `https`; `modelId` required; `timeoutMs` a positive integer. This is the reference for CAP-MDL-10.
- `harness/config/providers.json` is the prototype's provider list. It has no approval, no data-terms and no version pin.
- The prototype uses model identifiers such as `openai/gpt-4o-mini` in `harness/models/openrouter.ts` (`MODEL_ID`). That is a floating choice that this feature would reject as an approval target unless the provider publishes a version pin.
- The prototype has no evaluation suite for models, only for the harness (`eval/`).

## 4. Requirements

| ID | Requirement |
|---|---|
| CAP-MDL-1 | An approval target is a pinned identifier: provider, model identifier, version. For private-local models, also the weights digest and the serving runtime version. A floating alias is refused at validation. |
| CAP-MDL-2 | Each model manifest records `dataTerms`: retention of inputs (`none` | `days:N` | `unknown`), training use (`yes` | `no` | `unknown`), region, and the data classes the terms allow. `unknown` for any field blocks approval for a data class that depends on it. |
| CAP-MDL-3 | Each manifest records `licence` (SPDX identifier or a reference to a licence file). Approval requires a licence whose permitted use the approver has recorded. |
| CAP-MDL-4 | An approval references an evaluation summary (§5.3) with results for: tool-call conformance, output format adherence, refusal on stop cases, injection resistance, and task outcomes for each capability the model will serve. Missing category is a validation error. |
| CAP-MDL-5 | The summary records cost and latency per 1,000 input and output model tokens, measured on the named inputs, with the input sizes. |
| CAP-MDL-6 | `egress` names the endpoints the model may use. A hosted model's endpoint must be on the allowlist; a local model's endpoint must be on the private network. |
| CAP-MDL-7 | An approval record contains: approver identity, date, expiry (required, at most 365 days after approval), data classes, permitted uses, evidence references, and the evaluation fingerprint. |
| CAP-MDL-8 | The approval is revoked (state recorded; registry returns `BelowLabel` for `minLabel: Approved`) when: the version differs from the approved one; the provider retires the model (a `retired` field set by the owner); the terms change (the manifest's `dataTerms` differs from the approved copy); an evaluation fails on re-run; or the expiry date has passed. |
| CAP-MDL-9 | The approver may not be the proposer of the model's manifest. Enforced by the validator, using the `owner` and `proposed_by` fields. |
| CAP-MDL-10 | Provider configuration is validated: no raw credentials in the manifest; `apiKeyEnv` only; endpoint on `egress`; timeout set. |
| CAP-MDL-11 | A frontier model may be approved for a data class only when its `dataTerms` allow that class. The check is a function of the manifest and the data-class table, and is recorded in the approval. |

## 5. Design

### 5.1 Manifest (model kind)

```yaml
kind: model
name: example-frontier-model        # placeholder; real names are chosen by the approver
version: 1.0.0                      # semantic version of this capability (S-001 CAP-MAN-4)
owner: model-approvers-platform
summary: Hosted frontier model for general reasoning tasks.
source:
  type: library                     # the adapter's code is the code identity (S-001 §6)
  location: library/example-model-adapter
requires: []
category: frontier                  # frontier | public | private-local
licence: <SPDX identifier or licence reference>
provider:
  name: <provider>
  modelId: <pinned model identifier>
  modelVersion: "<pinned provider version>"   # never "latest" (CAP-MDL-1)
dataTerms:
  retention: none                   # none | days:N | unknown
  trainingUse: "no"                 # yes | no | unknown; quote it, YAML reads bare no as false
  allowedDataClasses: [public, internal]
# Not in the manifest, by design:
#  - route fields (endpoint, access kind, apiKeyEnv, timeoutMs, region, route-level data terms): M-002
#  - proposed_by, approver, egress, weights, cost, retired: the approval record (§5.2) and evaluation summary (§5.3)
```

The placeholder values above are not real models or prices. Real entries are created by the approver.

### 5.2 Approval record

`catalog/models/<name>/<version>/approval.yaml` (and the index entry):

```yaml
approver: <identity>                # must not equal proposed_by
approved_on: 2026-10-15
expires_on: 2027-10-15              # at most 365 days after approved_on
data_classes: [public, internal]
permitted_uses: [code-summarisation, repository-search]
evidence: evidence/models/<name>/<version>/summary.json
evaluation_fingerprint: sha256:...
manifest_digest: sha256:...         # the manifest as approved; any change revokes
```

### 5.3 Evaluation summary

`evidence/models/<name>/<version>/summary.json`:

```json
{
  "model": "<name>@<version>",
  "run": { "date": "...", "runner": "...", "runnerVersion": "...", "scenarioSetVersion": "...", "fingerprint": "sha256:..." },
  "categories": {
    "toolCallConformance": { "trials": 100, "passed": 97, "threshold": 0.95, "result": "pass" },
    "formatAdherence":     { "trials": 100, "passed": 99, "threshold": 0.95, "result": "pass" },
    "refusalOnStop":       { "trials": 50,  "passed": 50, "threshold": 1.0,  "result": "pass" },
    "injectionResistance": { "trials": 50,  "passed": 49, "threshold": 1.0,  "result": "fail" },
    "taskOutcomes":        { "trials": 60,  "passed": 54, "threshold": 0.85, "result": "pass" }
  },
  "costLatency": { "inputTokens": 2000, "outputTokens": 500, "p50Ms": 0, "p95Ms": 0, "costPer1kInput": 0.0, "costPer1kOutput": 0.0 }
}
```

The example shows a failed category; a model with any `fail` cannot be approved. The thresholds for injection and stop cases are 1.0, as the requirements state.

### 5.4 Validation rules

`validators/model.*` checks, in order:

1. Manifest schema (CAP-MDL-1 to CAP-MDL-3, CAP-MDL-10).
2. Summary schema, every category present, `result` equal to `pass` iff `passed / trials >= threshold` (recomputed, not trusted).
3. Approval record: `approver != proposed_by`; `expires_on - approved_on <= 365 days`; `manifest_digest` equals the digest of the current manifest; `evaluation_fingerprint` equals the summary's fingerprint.
4. Data classes requested are a subset of `dataTerms.allowedDataClasses` (CAP-MDL-11).
5. The endpoint is on `egress` and `egress` is on the allowlist (CAP-MDL-6).

### 5.5 Label and revocation

Model label: Declared (manifest valid) → Validated (summary valid) → Approved (approval record valid and not expired and manifest digest current).

`Approved` is not computed from the summary alone. It needs the approval record, which only a named approver can create. The validator checks the record, not who wrote it.

The registry's `resolve` with `minLabel: Approved` fails with `BelowLabel` for any revoked condition in CAP-MDL-8. The revocation feed (S-003) carries model revocations when a condition changes between runs, so a running model can be stopped the same way as a tool.

### 5.6 Data-terms table

`policy/data-terms.yaml`, maintained by govsec, maps each provider and category to its permitted data classes. The model manifest's `dataTerms` must agree with the table. A disagreement is a validation error, not a choice made by the approver.

## 6. Test plan

| Test | Assertion |
|---|---|
| Floating alias refused | `version: latest` fails validation |
| Unknown terms blocks class | `retention: unknown` with a class needing `none` fails approval |
| Self-approval refused | `approver == proposed_by` fails |
| Expiry over 365 days refused | 400-day expiry fails |
| Summary recomputes | A summary claiming `result: pass` with 40/100 against 0.95 fails |
| Category missing | Summary without `injectionResistance` fails |
| Manifest change revokes | Change `licence` after approval; resolve gives `BelowLabel` |
| Expiry revokes | Set the clock past `expires_on`; resolve gives `BelowLabel` |
| Retired revokes | `retired: true` gives `BelowLabel` |
| Data class outside terms refused | Approval for `confidential` when `allowedDataClasses` excludes it fails |
| Raw key refused | `apiKey: sk-...` in the manifest fails (CAP-MDL-10) |
| Egress | Endpoint not in `egress` fails; `egress` host not on allowlist fails |
| Fingerprint mismatch | Summary edited after approval fails |

## 7. Exit gate

- All tests in §6 pass on the remote host.
- One model approved per category (frontier, public, private-local), with the approver's record and a summary produced by the evaluation runner. Which models is the owner's decision.
- A drill: retire one approved model; `resolve` fails with `BelowLabel` at the next call.
- Recorded in `VERIFICATION.md`.

## 8. Operations

- The approver role is defined in the design (§10). Approvers do not edit the summary; they cite it.
- Approvals expire. The expiry is checked at every `resolve`, not only at re-evaluation.
- A calendar reminder for expiry is an operational aid, not a control.

## 9. Risks

| Risk | Mitigation |
|---|---|
| Provider changes a model without changing the version string | Not detectable by this feature; scheduled re-evaluation (CAP-MDL-8) is the control |
| Data-terms table is out of date | govsec owns it; the approval copies the terms, so a change revokes |
| Evaluation cost makes re-evaluation rare | Expiry at 365 days at most; the cost is measured and recorded so the owner can decide cadence |
| Approval becomes a rubber stamp | Self-approval refused; the summary must pass mechanically; the approver's identity is recorded |

## 10. Decisions

1. **Evaluation runner.** Blocks the summary's production. Decide with S-006. Until then, the summary can be produced by a script in this repository, marked `runner: manual` and given a lower trust in the report.
2. **Approver role.** Recommended: a named group of two or more people. Approval by one person is refused for the first release.
3. **Maximum expiry.** 365 days (recommended). Shorter for frontier models with unknown change cadence.
4. **Which models.** The owner chooses. Not decided here.

## 11. Access routes

Approval is per `(model version, route, data class)`. A route is how a model is reached. The enterprise chooses its routes; the platform records them, does not forbid them, and requires the approver to confirm each route's terms.

| Access | Auth | Used for | Approval covers |
|---|---|---|---|
| `api` | API key from env var | A provider's own API | One model version |
| `gateway` | API key from env var | OpenRouter, LiteLLM proxy, cloud marketplaces | One model version, per gateway |
| `router` | API key from env var | Services that pick a model per request (for example, NotDiamond) | Every model the router may choose, as a named set |
| `private` | Network reachability; internal key if any | Models inside the enterprise boundary | One model version |
| `cli` | Login held by the CLI on the host | Coding-agent CLIs on a subscription (Claude Code, Codex, Antigravity) | One CLI version and one plan |

Each access kind needs its own adapter. The access kind, not the vendor name, decides which requirements apply. A CLI route inherits CAP-SUB-1 to CAP-SUB-9 from §11.1; the same requirements apply to Codex and Antigravity, once their programmatic interfaces are verified.

### 11.4 Transport for `cli` routes

A `cli` route has a `transport`:

| Transport | How the call travels | Use |
|---|---|---|
| `direct` | The adapter runs the CLI as a child process (§11.1) | Unattended use on a host with the login |
| `mailbox` | The adapter enqueues a task in a maildepot mailbox; a listener running the CLI claims it, answers, and marks it processed | A CLI session that is already signed in on another machine or IDE |

A `mailbox` route is usually a developer's own IDE session, signed in on their machine. It is available only while that developer is running the project, and the model is theirs (their account, their plan). So:

- Its approval is limited to development use and to data classes the developer's plan permits. It is not a production route.
- Its availability is not guaranteed. A request with no listener claiming it times out (CAP-MBX-2), and callers must handle that without a fallback that changes data class.
- Each request names the developer's route id, so a call can be traced to the machine that answered.

### 11.5 Environments

The same agent definition runs in two environments. The environment decides the routes, and the evidence has to match the environment it is used in.

| Environment | Who runs the agent | Routes typically used | Cost falls on |
|---|---|---|---|
| Development | The developer's IDE session, or its mailbox listener | `cli` (mailbox or direct) on the developer's plan | The developer's subscription |
| Deployed | A separate process run by the enterprise | `api`, `gateway`, `router` or `private` | The enterprise's API budget |

Consequences:

- The agent manifest names the outcomes and tools, not the route. Routes are bound per environment in config.
- A trust label (A-002) records the environment it was earned in. Trials run in development do not make an agent trusted for deployment, because the deployed route may be a different model version with different behaviour.
- Approval of a route for deployment needs its own evaluation and its own approval record; the development approval does not carry over.

Requirements for `mailbox`:

| ID | Requirement |
|---|---|
| CAP-MBX-1 | The listener runs with tools disabled (as CAP-SUB-4), in an empty working directory outside any project repository. Maildepot's authority check is not relied on for this; it is honour-based by its own rules. |
| CAP-MBX-2 | The adapter sends each request with a unique id and waits for the result with the route's timeout. A timeout is reported as `timeout`; the claim is released so the task is not left locked. |
| CAP-MBX-3 | The request and the result are the records of the call. The call record references the mailbox id, the claim, and the listener's CLI version and plan. |
| CAP-MBX-4 | A message sent through the mailbox carries no authority: no approval, no grant, and no statement that the user agreed to anything. The mailbox rules of engagement apply to the listener as to every agent. |
| CAP-MBX-5 | The listener is armed with its own prompt that states it is a model-call route for one role, and it refuses any task that asks it to edit files or act outside answering. |
| CAP-MBX-6 | Evidence produced through `mailbox` is marked with that transport and is not compared as a regression against `direct` evidence without a fingerprint that includes the transport. |

A `router` route is different: the platform cannot know in advance which model answers a given request. Its approval therefore names the candidate set, and each call record names the model the router actually chose. A chosen model outside the approved set is an error, not a silent use.

### 11.1 Subscription route requirements

| ID | Requirement |
|---|---|
| CAP-SUB-1 | The route names the host it runs on, the CLI binary and its version. The approval records all three. |
| CAP-SUB-2 | The route names the account owner and the plan. The approver confirms the plan's terms for each data class the role serves. |
| CAP-SUB-3 | Calls run the CLI in non-interactive print mode with JSON output. Each call passes an explicit model, and the call's working directory is an empty temporary directory. |
| CAP-SUB-4 | Tool use is disabled for model calls: the adapter denies every tool the CLI exposes, and it passes no additional directories. The denial list is part of the route's manifest and is tested. |
| CAP-SUB-5 | Session persistence is off for model calls, so no conversation is kept on the host between calls. |
| CAP-SUB-6 | The adapter parses the CLI's JSON result and maps it to the common reply. An unparseable result is an `invalid_response` error. A non-zero exit is a `network` or `http` error with the CLI's stderr truncated. |
| CAP-SUB-7 | The adapter enforces the route's timeout by killing the child process, and reports `timeout`. |
| CAP-SUB-8 | The subscription's usage limit is an error category of its own (`quota`). A quota error never triggers a fallback to another route unless the role lists that fallback. |
| CAP-SUB-9 | Each call record includes the CLI version, the model, the route and the plan. |

### 11.2 What is verified and what is not

Verified on this Mac: the CLI is installed (version 1.0.128) and provides print mode, JSON output and a model flag.

Not yet verified, and must be verified on the test host before the route is approved:
- that the flags which disable tools and session persistence exist on the installed version;
- the exact shape of the JSON result;
- that a login on the test host gives the same plan and limits.

### 11.3 Steps to make the route work

1. Install the CLI on the test host at the same version, with the same Node runtime.
2. Log in there with the plan's account. This is an interactive step the account holder performs.
3. Run the route probe: one prompt, JSON output, the denied tool list in force, no session. Record the output shape in the route manifest's evidence.
4. Add the probe as a test in `library/` for the adapter, and the route becomes approvable under §5.
