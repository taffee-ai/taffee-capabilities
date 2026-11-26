# Implementation plan: S-003 revocation client (client phase)

Status: the implementation plan, kept as a record. The client phase (M1 to M5) is implemented and accepted in review; results are in [VERIFICATION.md](VERIFICATION.md). The cross-repo gate (feed service, dispatcher record and pre-execute check, key rotation, outage drill) is open.

## Layout

```
capability/revocation.py   entries, checkpoints, verification, the client state, check(kind, name, version, digest)
capability/resolve.py      (existing) wired to the check in M4
tests/test_revocation.py   M1 to M3, M5
tests/test_revocation_resolve.py   M4
```

The signing library (Ed25519) is pinned in pyproject.toml when M1 starts (`cryptography`), and installed in the test run as the other pins are. Its version is recorded in VERIFICATION.md.

## Milestones

### M1: Entries and signatures

Build: parse one entry; the genesis head `GENESIS` (SPEC §5); canonical JSON of the signed fields; Ed25519 verification against a public key from configuration; the entry hash (SHA-256 of canonical JSON including `sig`); targets without `kind`, and name-only targets, refused.

Checks: a valid entry verifies; one changed byte fails; a name-only target is refused; a key absent from configuration is a typed error; no entry is trusted from the feed alone (the key is configuration, CAP-REV-11).

### M2: Chain, sequence and checkpoints

Build: the client's verified state is `(seq, hash)` plus the verified kind-qualified targets. A checkpoint is accepted only when its `serial` is strictly greater than the last accepted `serial`; an equal or lower serial is a replay and is refused (CAP-REV-6). A checkpoint whose `issued_at` is outside the window (CAP-REV-12) is refused, including on the first fetch after a restart. An entry is accepted only when its `seq` is the next one and its `prev` is the current hash (CAP-REV-3, CAP-REV-5). A checkpoint is accepted only when it is signed, its `latest_seq` is not lower than the client's, and it matches the client's own head `(seq, hash)` or the client has verified every entry up to it (CAP-REV-6, CAP-REV-7). An empty fetch refreshes nothing. Freshness is a monotonic deadline set at acceptance: `accepted_at + (30 s - age)`, with `age = max(0, local_now - issued_at)`, so a delayed checkpoint gets only the freshness it has left (CAP-REV-8).

Checks: a gap is stale until filled; a broken chain is stale; a lower checkpoint is refused as rollback; a replayed checkpoint (same or lower `serial`) refreshes nothing; a checkpoint issued 40 s ago is refused on the first fetch after restart, one issued 20 s ago is accepted with 10 s of freshness left and the client is stale 11 s after acceptance, one issued 29 s ago has 1 s left, one issued 30 s ago is refused, one issued 15 s ahead is refused, and one 5 s ahead is accepted (CAP-REV-12); an empty fetch leaves the client stale after the bound; a wall-clock jump does not change freshness; the first entry links to `GENESIS`.

### M3: Failure keeps the last verified state

Build: a failed verification (bad signature, broken chain, gap, bad checkpoint) keeps the last verified `(seq, hash)` and targets and sets stale. Nothing from the failing input is used (CAP-REV-4). Recovery requires a valid replacement entry or a valid checkpoint that matches the client's chain.

Checks: a tampered entry does not revoke its target and makes the client stale; a valid replacement restores freshness; a valid checkpoint over an unverified head is refused; a replayed checkpoint after recovery is refused.

### M4: The check and registry integration

Build: `check(kind, name, version, digest)` returns `ok`, `Revoked(entry)`, or `Stale` (CAP-REV-9), evaluating both forms in one pass. A digest revocation matches that digest. A name@version revocation matches that version whatever digest is presented. `resolve` uses the check: a stale client gives `RevocationStale`; a revoked target gives `Revoked`; a `Match` carries `revocation: "checked"` and the `seq` (CAP-REV-10).

Checks: a digest revocation matches only that kind and digest; a tool revocation of `name@1.0.0` does not revoke an agent with the same name and version; a name@version revocation is caught when a different digest is presented (the bypass case); a stale client fails `resolve`; the `Match` carries the `seq` it was checked against.

### M5: Outage and recovery

Build: the client becomes stale after the bound without a checkpoint. A restored feed, with a checkpoint that matches the chain, makes it fresh again without a restart.

Checks: a feed stopped past the bound gives `Stale`; a restored feed gives `ok` again; no state is lost across the outage.

## Client-phase exit

The tests in the SPEC §8 table pass on the Linux test host from a fresh copy, the results are recorded in VERIFICATION.md, and the record is agreed in review.

## Not in this plan

- The feed service, its operator, where it runs, and its key custody (govsec, open).
- The dispatcher's running-call record (orchestrator).
- Key rotation and the live drill (cross-repo, open). The full S-003 gate is not claimed.
