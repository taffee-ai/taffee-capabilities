# S-008 Catalog and registry service: verification record

Status: implemented and tested, 2026-10-20, as a reference service with an injected authorizer. Not yet reviewed.

Written for: reviewers and maintainers.

Contract: `CONTRACT-NOTE.md` (revision 2 accepted by both reviewers, with the section 6.2 wording correction applied).

## What was built

- `capability/service.py`: `CatalogService` (transport-free `handle(method, target, headers, body)`) and `make_server` (stdlib `ThreadingHTTPServer`, loopback by default). Endpoints: `GET /v1/capabilities`, `/{name}`, `/{name}/{version}`, `GET /v1/index` (redacted `capability-index-view/1` with its own `view_digest`), `POST /v1/resolve`.
- `capability/authz.py`: `Authorizer` interface (`principal`, `may_see`), `DenyAllAuthorizer`, and the explicitly selected, development-only `DevAuthorizer` (trusts `X-Dev-Principal`; every identified caller sees everything; optional hidden set for tests). The service has no default authorizer: constructing one without an `Authorizer` raises.
- `capability/registry.py`: optional `visible` predicate on `list`, `describe`, `resolve` (applied before every other step) and a public `verified_index()`. No other behaviour changed.

## Evidence (Linux test host, Docker)

- Focused: `tests/test_service.py`, 29 tests OK.
- Full suite with the sandbox environment: 799 tests OK, 1 skip (the previously known deliberate skip).
- Golden index unchanged: index_digest `35660ccc…f7e`, inputs_digest `3db90887…08df5`; a test asserts this. No manifest, schema, policy, library, evidence or catalog moved.
- Measured proofs: 13 guards each removed in a scratch copy; the focused suite failed every time (hidden-entry filter in the service, registry `visible` predicate, `manifest_view` leaking implementations, whole-spec instead of allow-list, `view_digest` not recomputable, `Retry-After` on `IntegrityError`, duplicate JSON keys, authorizer failure not fail-closed, hidden entry not 404, `BelowLabel` computed over hidden entries, raw query not bounded before parsing, unknown query parameters, no-default-authorizer guard). Two proofs initially passed (BelowLabel over hidden entries; raw-query bound) and led to new tests.
- Early review finding (no aggregate query bound): fixed with `MAX_QUERY = 1024` checked before `parse_qs`; tested in-process (spy proves refusal before parsing) and through the HTTP handler (5000 characters: 400; 70000: 400 or 414).

## Behaviour pinned by tests

- Allow-listed key sets for list items, detail, `manifest_view`, resolve and index view; no `location`, `entry`, `code_digest`, `implementations`, `sandbox` or path text in any response; unknown spec keys dropped; agent and model views exclude `source`.
- Hidden equals absent, byte for byte (list, detail, versions, resolve), applied before ambiguity, label and range checks; a hidden kind causes no 409; `BelowLabel` names the best visible label.
- Errors: 401 no principal, 404, 405, 400 (bad body, duplicate keys, NaN, unknown fields, bad label/kind/range, oversize), 409 `Ambiguous` and `BelowLabel`, 410 `Revoked` (reason, seq), 503 `RevocationStale` with `Retry-After`, 500 `IntegrityError`/`IndexError` without it, 503 `AuthorizerUnavailable` when the authorizer fails. Error bodies are `{code, message}` plus named fields; messages are closed text.
- A corrupt index and a changed implementation fail closed on every endpoint.

## Latency, reported against the host (owner: functional first, optimise later)

Host: old shared test laptop, Intel Core i5-2520M 2.5 GHz (2011), 4 logical CPUs, 5.9 GB RAM, load average about 1.8 during the run, Docker, Python 3.12. Each request re-reads and verifies the index (no cache, by S-010).

| Call | Median | p95 | Max |
|---|---|---|---|
| list (in-process) | 18.2 ms | 29.9 ms | 35.7 ms |
| detail (in-process) | 18.2 ms | 29.4 ms | 36.4 ms |
| index view (in-process) | 18.3 ms | 27.2 ms | 36.9 ms |
| resolve with digest verification (in-process) | 37.2 ms | 46.7 ms | 53.4 ms |
| list over HTTP loopback | 26.1 ms | 35.5 ms | 40.4 ms |

These are for a five-entry catalog on a busy 2011 laptop; they are a baseline, not a target.

## Limits

- Development reference only: no TLS, no real authentication, stdlib server. A production deployment needs govsec's authorizer and a hardened server (O-OBS-6 and govsec G-005 onward).
- Service-mode invocation (`/v1/tools/.../invoke`), agents' runs and grants are out of scope.
- The signed index is never served; whether any raw-index endpoint exists is the owner's choice.
- No telemetry yet (queued behind this and the graph_query plan).
