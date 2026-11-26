# S-008 Catalog and registry service: contract note

Status: revision 2, 2026-10-20, accepted in review. Implemented; results are in [VERIFICATION.md](VERIFICATION.md).

Revision 2 supersedes sections 1 (the `/v1/index` and `{name}/{version}` rows) and 2 rules 1, 3, 6 where it says otherwise. Section 6 below is the binding text for those points; sections 1 to 5 are kept for the unchanged parts.

Written for: reviewers, including a second reviewer for the endpoint and auth-interface parts.

## 1. Scope

A read-only HTTP service over the existing `Registry` (S-002) implementing USAGE-DESIGN 3.1 and 3.2:

| Endpoint | Answer |
|---|---|
| `GET /v1/capabilities?kind=&minLabel=&q=` | Entries at or above `minLabel`, optionally of one kind; `q` is a case-insensitive plain-text match over name, summary and `when_to_use` |
| `GET /v1/capabilities/{name}` | All versions of the name (all kinds; each row names its kind) |
| `GET /v1/capabilities/{name}/{version}` | The registry's `describe` result: manifest, label, digests, evidence references |
| `GET /v1/index` | The index document with its `index_digest` |
| `POST /v1/resolve` | `Registry.resolve` with `{name, kind, range, minLabel}`; 200 with name, version, digest, label, revocation "checked", requires; 409 `BelowLabel` (have, need); 410 `Revoked` (reason, seq); 404 `NotFound`; 409 `Ambiguous`; 503 `RevocationStale` / `IntegrityError` (fail closed) |

All three kinds (tool, model, agent) answer from the same code path. The service returns the indexed facts only.

## 2. Rules this contract fixes

1. **Nothing executable.** A response never contains an implementation path, entry point, source or a way to invoke. Responses are built from an allow-list of fields, not by filtering a full record. A test asserts the key set of every response shape and that no host path or `entry_path` appears.
2. **Verify before answering.** Each request goes through the registry's verified read (index digest, and for resolve the manifest and code digests plus the S-003 revocation check). A corrupt index or digest mismatch is an error response, never a partial answer. The service holds no cache of its own (S-010 Step 0: no cache admitted).
3. **Visibility is a decision, not a filter we invent.** An injected `Authorizer` interface answers two questions: `principal(request) -> Principal | None` (no principal is 401) and `may_see(principal, entry) -> bool`. Entries the caller may not see are omitted from lists and answer 404 on direct reads and on resolve, identical to "does not exist" (no existence leak). The interface mirrors GOVSEC-DESIGN's decision shape (AuthZEN-style subject/action/resource, closed reason codes) so govsec replaces the implementation without changing the service.
4. **The development authorizer is explicit.** `DevAuthorizer` is selected only by an explicit constructor argument, its name and a startup log line say it is development-only, and the service refuses to start with no authorizer at all. Default is deny. The service does not authenticate; it trusts the injected interface.
5. **Resolve is not permission.** The 200 response lists `requires`; grants stay a separate call (USAGE-DESIGN 3.4) and out of this pillar.
6. **Closed error bodies.** `{code, message}` plus the named fields above; codes come from USAGE-DESIGN 6. No tool, record or path text is copied into an error. 405 for non-GET on read endpoints; request bodies and query strings are size- and shape-bounded; unknown query parameters are 400.
7. **Standard library only** (`http.server` with a thin WSGI-free handler), no new dependency, no TLS or bearer-token handling here (deployment concern behind govsec's gateway).

## 3. What moves

- New: `capability/service.py`, `capability/authz.py`, `tests/test_service.py`, this feature's VERIFICATION.md.
- Edited: the roadmap S-008 row on completion only (status wording as for T-005).
- **No change** to any manifest, schema, policy, library, evidence, index or catalog. Expected digests: unchanged (inputs `3db90887…08df5`, index `35660ccc…f7e`). A test asserts the golden index is untouched.
- No pillar edits outside capabilities.

## 4. Tests (Linux test host, in Docker)

- Each endpoint for all three kinds against the real index; filters; `q` match; empty result.
- Response key sets (allow-list) and absence of paths/executables.
- 404/409/410/503 bodies; `Revoked` with a real signed feed fixture; `RevocationStale`; corrupt index and digest-tampered scratch copy fail closed.
- Authorizer: no principal 401; hidden entries omitted from list, 404 on read and resolve, byte-identical to a true miss; default-deny; refusal to start without an authorizer.
- Input bounds: oversize body, bad JSON, duplicate keys, unknown fields, bad range, wrong method.
- Measured proofs: restore each guard (omit hidden entries, fail closed, allow-list) in a scratch copy and show the test fails.

## 5. Open questions for review

1. Is 404 (not 403) for hidden entries the right choice, given USAGE-DESIGN lists 403 only for GrantDenied on invocation?
2. Should `RevocationStale` and `IntegrityError` map to 503 (my proposal) or 500/502?
3. Is `GET /v1/capabilities/{name}` across kinds acceptable, or should it require `kind` when a name spans kinds (the registry raises `Ambiguous` on describe)?
4. Is `http.server` acceptable for the reference service, with deployment behind govsec's gateway, or do you want a different surface now?
5. Any field you want added to or removed from the resolve 200 body beyond USAGE-DESIGN 3.2?

## 6. Revision 2: response shapes and the two blockers

### 6.1 Decisions taken from the review answers (1 to 5)

1. Hidden entries are 404. Visibility is applied **first**, before kind-ambiguity, label and range checks, so no error can reveal a hidden record.
2. `RevocationStale` is 503 with `Retry-After`. `IntegrityError` is 500 with `code` and **no** `Retry-After` (USAGE-DESIGN: report, do not retry).
3. A name that spans more than one **visible** kind and no `kind` is given: 409 `Ambiguous`. Hidden kinds never create ambiguity.
4. `http.server` is a development reference service only. The module docstring, the startup log line and the VERIFICATION limits say "not a production deployment".
5. Resolve body is USAGE-DESIGN 3.2 plus `kind`. No path, no invocation handle.

### 6.2 Blocker 1: `/v1/index` (my proposal; owner may overrule)

The signed index is never served by this service, because filtering it breaks `index_digest` and serving it whole leaks hidden names and implementation locations. `GET /v1/index` returns a **separate redacted view** that is not called the signed index and does not claim to hash to it:

```
200 {
  "schema": "capability-index-view/1",
  "source": { "index_digest": "sha256:...", "inputs_digest": "sha256:...", "policy_version": 2 },
  "view_digest": "sha256:...",        // canonical JSON (S-005 rules) of this body without view_digest
  "entries": [ <list item, see 6.3>, ... ]   // only entries the caller may see
}
```

`source.*` identify the verified index the view was cut from (reference only). A holder who needs the signed index obtains it through the operator's own channel, not this service. If the owner ever wants the signed index served, it must be the **whole, unmodified index** to a principal with privileged whole-index access (its own authorizer decision, `catalog.index.raw`); removing any field would change the bytes and invalidate `index_digest`, so it could not then be called the raw or signed index. A reduced version would be a separately named projection with its own digest. Whether any such endpoint exists is an owner choice and none is built here.

Authorizer rule: the owner-directed "all agents have the highest privilege" exists only inside an explicitly selected development authorizer. An absent principal, or an absent authorizer, is denied, and the service never implies production authentication.

### 6.3 Exact shapes (allow-lists; any other field is a test failure)

List item (`GET /v1/capabilities`, also the view entries):
`name, kind, version, label, summary, manifest_digest, identity, languages, requires`.
`GET /v1/capabilities` body: `{"entries": [<list item>...]}`. `GET /v1/capabilities/{name}`: same, all visible versions, newest first, with `kind` on each row (409 `Ambiguous` as in 6.1 when no `kind` query parameter and several visible kinds).

Detail (`GET /v1/capabilities/{name}/{version}?kind=`):
```
{ "name", "kind", "version", "label", "manifest_digest", "identity",
  "languages": ["python"],
  "evidence": ["conformance:sha256:...", ...],        // references only
  "manifest_view": {                                  // a projection, NOT the manifest
      "name","kind","version","summary","owner","requires",
      "spec": { "inputSchema","postconditions","idempotent","readOnly","timeoutMs","boundary" },
      "projection_of": "sha256:<manifest_digest>" } } // reference, not a claim that it hashes to that
```
Removed from the projection: `implementations` (including `location`), `entry`, `code_digest`, the `spec.sandbox` block (an operator concern), and any key not listed. Models and agents use the same envelope with their own `spec` allow-list (listed in the tests by kind); an unknown `spec` key is dropped, never passed through.

Resolve 200: `{name, kind, version, digest, label, revocation:"checked", requires}` where `digest` is the entry's `identity` for a tool and its `manifest_digest` otherwise (the value the revocation check uses). Resolve 404 `NotFound` (also for hidden), 409 `BelowLabel {have, need}` (only evaluated among visible entries), 409 `Ambiguous`, 410 `Revoked {reason, seq}`, 503 `RevocationStale`, 500 `IntegrityError`.

Error body for every non-2xx: `{"code", "message"}` plus the named fields of that code only.

### 6.4 Added tests

Exact key-set tests per shape and kind; a manifest containing `location`, `entry`, `sandbox` and an unknown spec key shows none of them in the response; `view_digest` recomputes from the body; `source.index_digest` equals the golden index; hidden-kind collision does not give 409; hidden vs absent responses are byte-identical including headers; `IntegrityError` has no `Retry-After`; `RevocationStale` has one. Measured proofs as in section 4.

### 6.5 Question for the second pass

Is the redacted `capability-index-view/1` (6.2) acceptable as the only index endpoint, with raw serving left as a later owner decision?
