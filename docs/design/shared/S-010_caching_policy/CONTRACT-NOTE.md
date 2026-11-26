# S-010 contract note: caching policy

Status: proposal, 2026-10-19. Not yet implemented.

Written for: reviewers and maintainers.

## 1. What the records say

- Roadmap S-010: resolution is cached by digest and index; no cache outlives a revocation check; caching rules are declared per capability and tested. Roadmap T-004: only idempotent tools are cached; per run; concurrent identical calls share one execution; a failed call is never cached.
- S-002 CAP-REG-7 and its design: **no digest cache**; each resolve recomputes; "Revisit only with a sound, tested invalidation scheme"; "the cost of recomputing is measured in M4 before any optimisation is considered". I found no measurement recorded in the S-002 records.
- CAPABILITIES-DESIGN, ownership table: "Dispatch, timeouts, **caching**, parallel calls, run state: **Orchestrator**; the capabilities pillar provides the declaration the runtime reads". Run-scoped caching and single-flight therefore belong to the orchestrator.

## 2. Two conflicts I want your ruling on

1. **T-004 sits in the capabilities roadmap, but the design gives result caching to the orchestrator** (which does not exist). A capabilities-side T-004 would be a cache library with no caller, and per-run state is by definition the runtime's. I propose T-004 is **not built in this pillar**: capabilities supplies the declaration (`idempotent`, `readOnly` exist; a cache-rule declaration is added only when the orchestrator specifies what it reads), and the roadmap row is amended to say the runtime is the orchestrator's. If you want a library anyway (for example a tested single-flight cache the orchestrator can import), say so and I will specify it.
2. **S-010 resolution caching weakens a stated integrity property.** Today every resolve re-reads the index, recomputes the manifest digest, hashes the implementation folder and compares with the verified index, so a changed byte is caught on the next call. Any cache that skips the hashing trades that for speed. A file-metadata key (size, mtime, inode) is **not sound**: a byte can change with the size and a restored mtime. So the question is whether the cost justifies any cache at all.

## 3. Proposal: measure first, then build only what is sound

**Step 0 (no product code).** Measure `Registry.resolve` and `list`/`describe` on the Linux test host in Docker: the real catalog (five tools), and a synthetic catalog of 50 and 500 tools with implementation folders of realistic size. Record the time split between index read and schema validation, manifest digests, folder hashing and the revocation check. Add the numbers to a S-010 record. **If resolve is cheap (a few milliseconds for the real catalog), the recommendation is to leave CAP-REG-7 unchanged and record that no cache is warranted**, which closes S-010 honestly as "measured, not needed, the property is kept".

**If the numbers justify a cache, the only sound pieces I would build:**

| Part | Key | Sound because | Never cached |
|---|---|---|---|
| Parsed and schema-validated index | SHA-256 of the index file's bytes (the file is read and hashed each call: cheaper than parsing and validating, and equal bytes give an equal result) | content-addressed; a changed byte is a different key | the index file read itself |
| Per-entry verified manifest digests | the entry's embedded manifest bytes | content-addressed | |
| Implementation folder digest | not cached from file metadata. Only cacheable for a **content-addressed immutable snapshot** (a deployment that mounts the implementation read-only and names it by its digest); that is a deployment mode, not a default | the snapshot cannot change under the name | any file-metadata-keyed cache |
| Revocation check (S-003) | never cached by the registry: the client's own 30 s freshness is the only staleness allowed and it fails closed | | always re-evaluated at each resolve |

Rules, all tested: a cache hit returns the same `Match` a fresh resolve would (equality test over every catalog entry); changing one byte of the index, a manifest or an implementation file is seen on the next resolve (the S-002 test stays and is repeated with the cache on); a revocation that arrives between two resolves fails the second; a stale or rolled-back cache entry is impossible by construction (content key); the cache holds no secret and no caller input; it is bounded in size; it can be switched off and the results are identical.

## 4. What moves (digests, versions, files)

Step 0 and any cache are in `capability/` and `tests/` (not indexed folders): no manifest, version, identity, ledger, index digest, catalog or evidence changes. If a per-capability cache declaration were added to the manifest schema later (T-004's declaration), that edit would move `inputs_digest` and `index_digest` as T-005 did, and is a separate contract note.

## 5. Open questions for review

1. Agree that Step 0 (measure) comes first and that "measured, not needed" is an acceptable close for S-010?
2. Agree that a file-metadata-keyed digest cache is out, and that only content-addressed pieces (and an immutable-snapshot mode) are admissible?
3. T-004: agree it belongs to the orchestrator and the capabilities roadmap row should be amended (no capabilities code), or do you want a capabilities-side tested library?
4. The roadmap sentence "caching rules are declared per capability": I read it as applying to result caching (T-004 and the orchestrator), not to resolution. Agree?
