# S-003: Revocation client (client phase), with the feed owned by govsec

Status: client phase implemented and accepted in review (474 tests at acceptance). The client phase is exited on its own (§9). The full S-003 gate (feed service, dispatcher record and pre-execute check, key rotation, outage drill) is cross-repo and open.

Written for: the engineers who build the client in capabilities, and govsec's engineers who build the feed.

## 1. Purpose

Capabilities needs a way to know whether a capability version has been revoked, and whether its knowledge of revocations is fresh. The revocation decision is govsec's (D-008). The feed carries it. This pillar holds only the client, which verifies the feed and answers the question at resolve time.

## 2. Scope

In scope (client phase):
- Parse and verify a feed entry, and a signed checkpoint.
- Keep a hash-chained, sequenced view of verified entries.
- Refresh freshness only from a verified checkpoint.
- Answer `check(kind, name, version, digest)` for a kind-qualified digest or name@version, returning `ok`, `Revoked`, or `Stale`.
- Use the check in `resolve`.

Out of scope (govsec, or cross-repo, open):
- Writing revocations, and the feed service that serves them.
- Who runs the feed, where it runs, and the signing key's custody and rotation.
- The dispatcher's running-call record (taffee-orchestrator).
- The live drill across repositories.

## 3. Current state

- The client exists (`capability/revocation.py`) and is wired into `Registry.resolve` (`Registry(root, revocation=client)`). Without a client, `resolve` still returns `revocation: "unchecked"`; with one, it returns `checked` with the sequence, or fails with `Revoked` or `RevocationStale`.
- The feed does not exist. Its operator is open. A file feed (`FileFeed`) exists for development and for `capctl verify`.

## 4. Requirements

| ID | Requirement |
|---|---|
| CAP-REV-1 | A target is kind-qualified: `{kind, digest}` or `{kind, name, version}`. A target without `kind`, or a name-only target, is refused. A tool revocation never matches an agent or model, even with the same name and version. A check takes the composite input `(kind, name, version, digest)` and evaluates both forms in one pass. For a tool, the digest is its identity (manifest digest with the implementation set). For an agent or model, the digest is its declaration digest; it does not cover implementation bytes, so the spec does not call it exact content. |
| CAP-REV-2 | Each entry carries: `seq`, `prev` (the hash of the previous entry), target, reason code, actor, time, reference, and a signature over the other fields in canonical JSON. |
| CAP-REV-3 | The chain is append-only: each entry's `prev` is the hash of the entry before it. A broken chain makes the client stale. |
| CAP-REV-4 | An entry whose signature does not verify is untrusted. It is ignored, and so is its target. The client keeps its last verified `seq` and head hash, and becomes stale, failing every check, until a valid replacement or an authenticated recovery arrives. A target in an unverified entry is never used. |
| CAP-REV-5 | Sequence numbers are contiguous. A gap makes the client stale. |
| CAP-REV-6 | Freshness is refreshed only by a verified checkpoint: a signed record of `(serial, latest_seq, head_hash, issued_at)`. `serial` is strictly increasing. The checkpoint must match the client's own chain: its `latest_seq` and `head_hash` must equal the client's verified head, or the client must have verified every entry up to it. An empty fetch refreshes nothing. A replayed checkpoint (the same or a lower `serial`) refreshes nothing. |
| CAP-REV-7 | A checkpoint whose `serial` or `latest_seq` goes backwards is refused as rollback or replay. The client keeps its state and becomes stale. `issued_at` outside the window in CAP-REV-12 is refused, including on the first fetch after a restart, and faults the client like any other checkpoint it cannot accept. Only the monotonic time of an accepted checkpoint refreshes freshness. |
| CAP-REV-8 | Freshness runs from the checkpoint's `issued_at`, not from the moment it is accepted. At acceptance the client computes `age = max(0, local_now - issued_at)` and sets a monotonic deadline of `accepted_at + (30 s - age)`. A delayed checkpoint therefore gets only the freshness it has left. Past the deadline the client is stale and fails closed. End-to-end bound: on agreeing clocks the client never answers `ok` from a head more than 30 s after that head's checkpoint was issued, so a revocation recorded after a checkpoint is either revoked or fail-closed within 30 s. If the client's clock runs behind the feed's by up to `CLOCK_SKEW`, the worst case is 30 s + `CLOCK_SKEW`. |
| CAP-REV-9 | `check(kind, name, version, digest)` returns `ok`, `Revoked(entry)`, or `Stale`. Both forms are evaluated in the same check, and both are kind-qualified. A digest revocation matches that kind and digest. A name@version revocation matches that kind and version whatever digest is presented. A digest-only check cannot bypass a name@version revocation. |
| CAP-REV-10 | `resolve` uses the check. A stale client gives `RevocationStale`. A revoked target gives `Revoked`. A `Match` carries `revocation: "checked"` and the `seq` it was checked against. |
| CAP-REV-11 | Trust in the signing key comes from configuration (a public key), never from the feed itself. Key rotation is a govsec matter and is out of this phase. |
| CAP-REV-12 | A checkpoint is accepted only when its `issued_at` is inside the window: `local_now - issued_at < MAX_CHECKPOINT_AGE` and `issued_at - local_now <= CLOCK_SKEW`. Recommended values: `MAX_CHECKPOINT_AGE` 30 s, matching the freshness bound, and `CLOCK_SKEW` 10 s. Both are configurable. The window applies on every checkpoint, including the first fetch after a restart, when the client has no serial floor of its own. A checkpoint outside the window is refused and faults the client: a fresh client becomes stale at once, its verified head, deadline and serial do not move, and only a later in-window checkpoint that matches its chain recovers it. (Reviewed: review.) Wall-clock time is used only for this window, never for freshness. Operating requirement for govsec: the feed issues a checkpoint at least every 10 s, so a healthy client stays fresh. |

## 5. Genesis, feed entry and checkpoint shape

Genesis: the client starts at `seq` 0 with head hash `GENESIS` = `sha256:` plus the SHA-256 of the UTF-8 bytes of `aicore-revocation-genesis/1`. The first entry is `seq` 1 with `prev` = `GENESIS`. Independent clients therefore agree on the first link.

Entry, one JSON object per line:

```json
{"seq": 42, "prev": "sha256:<hex>",
 "target": {"kind": "tool", "digest": "sha256:<hex>"} | {"kind": "tool", "name": "git-log", "version": "1.0.0"},
 "reason": "evidence_failure | incident | dependency_changed | expired | owner_withdrawn",
 "actor": "<identity>", "time": "2026-10-18T00:00:00Z",
 "reference": "<evidence or incident id>",
 "sig": "<base64 ed25519 over canonical JSON of the other fields>"}
```

Checkpoint:

```json
{"serial": 9001, "latest_seq": 42, "head_hash": "sha256:<hex>", "issued_at": "2026-10-18T00:00:00Z",
 "sig": "<base64 ed25519 over canonical JSON of the other fields>"}
```

The hash of an entry is the SHA-256 of its canonical JSON, including `sig`.

## 6. Transport

The client reads a source through a small interface: `entries_since(seq)` and `checkpoint()`. The interface is the only thing that touches the feed. Its implementation (HTTP, a file, or a test double) is outside this phase. Any transport is acceptable because the client verifies everything it is given.

## 7. Design

- **Verify entry:** canonical JSON, signature, chain link to the previous hash, contiguous `seq`.
- **Verify checkpoint:** signature, strictly increasing `serial`, monotonic `latest_seq`, a match with the client's own chain (CAP-REV-6), and the age and skew window (CAP-REV-12).
- **State:** verified head `(seq, hash)`, verified target set, last verified checkpoint time on the monotonic clock.
- **Failure:** any verification failure keeps the last verified state and sets stale.

## 8. Test plan

| Test | Assertion |
|---|---|
| Valid entry verifies; one changed byte fails | M1 |
| Bad signature: the target is not used, and the client is stale | CAP-REV-4 |
| Name-only target is refused | CAP-REV-1 |
| Broken `prev` chain is stale | CAP-REV-3 |
| Gap in `seq` is stale until filled | CAP-REV-5 |
| Empty fetch does not refresh freshness | CAP-REV-6 |
| Checkpoint with a lower `latest_seq` is refused as rollback | CAP-REV-7 |
| Freshness is monotonic: a wall-clock jump does not change it | CAP-REV-8 |
| Digest revocation matches only that kind and digest | CAP-REV-9 |
| name@version revocation is not bypassed by a digest-only check | CAP-REV-9 |
| Composite check: a name@version revocation is caught against a different digest, in the same check as the digest form | CAP-REV-9 |
| Replayed checkpoint (same `serial`, same content) refreshes nothing | CAP-REV-6 |
| First fetch after restart: a checkpoint issued 40 s ago (outside the age window) is refused; one issued 20 s ago is accepted | CAP-REV-12 |
| A checkpoint issued 15 s ahead (outside the skew window) is refused; one issued 5 s ahead is accepted | CAP-REV-12 |
| Delayed but in window: a checkpoint issued 20 s ago is accepted with 10 s of freshness left; 11 s after acceptance the client is stale and fails closed | CAP-REV-8 |
| A checkpoint issued 29 s ago is accepted with 1 s left; one issued 30 s ago is refused (no freshness left) | CAP-REV-8, CAP-REV-12 |
| End to end: the client never answers `ok` more than 30 s after the issue time of the head it relies on (agreeing clocks) | CAP-REV-8 |
| Kind regression: a tool revocation of `name@1.0.0` does not revoke an agent with the same name and version | CAP-REV-1, CAP-REV-9 |
| First entry links to `GENESIS`; a first entry with any other `prev` is stale | CAP-REV-3 |
| `resolve` returns `revocation: "checked"` with the seq; a stale client fails resolve | CAP-REV-10 |

## 9. Exits

- **Client phase (exit of this spec):** the tests in §8 pass on the Linux test host from a fresh copy, with the results in VERIFICATION.md, and the record is agreed in review.
- **Full S-003 (cross-repo, open):** the feed service, the dispatcher's running-call record, the dispatcher pre-execute check, key rotation, and an outage drill. Not claimed here.

## 10. Decisions and open items

1. Who runs the feed, and where. Open. Govsec owns policy and signing; the operator is not decided.
2. The signing key's custody and rotation. Govsec. Open.
3. The freshness bound: 30 seconds, failing closed. Recommended.
4. The checkpoint window: `MAX_CHECKPOINT_AGE` 30 s and `CLOCK_SKEW` 10 s. Recommended and configurable. They match the freshness bound. A 300 s skew tolerance would let a five-minute-old checkpoint refresh freshness, so it is not used.
5. The end-to-end freshness guarantee is 30 s from the checkpoint's issue time on agreeing clocks, and 30 s + `CLOCK_SKEW` when the client's clock is behind. The skew allowance is a stated limit of trusting wall-clock time for age. It is the price of not relying on the feed's clock being exact.
