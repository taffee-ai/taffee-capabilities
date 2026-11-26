# S-012: Call records

Status: implemented and tested (see [VERIFICATION.md](VERIFICATION.md)). Depends on S-002 and S-003; uses S-009's error codes. Roadmap: [../../../roadmap.md](../../../roadmap.md).

Written for: the dispatcher engineers who write the records, and the auditors who read them.

## 1. Purpose

Every call to a capability leaves one record that says what ran (digest, label, language), under what knowledge (revocation sequence), by whose authority (grant reference), and what happened (outcome), and holds no content. After a revocation, the records name the affected calls. The records are tamper-evident, so a log can be checked without trusting whoever kept it.

## 2. Requirements

| ID | Requirement |
|---|---|
| CAP-REC-1 | `CallRecorder(path).record(...)` appends exactly one record per call, as one JSON line, in canonical key order. The whole line is written, looping over short writes. If it cannot be written in full, the log is truncated back to its previous size and `record` raises without advancing the head; if even the truncation fails, it raises saying the log may hold a partial record or a whole record the call reported as failed, and the recorder stops (later `record` calls are refused until a new `CallRecorder` is opened over the verified log). It never reports success on a partial line. |
| CAP-REC-2 | A record holds references only: a digest and byte count of the input and of the output, never their text. Error messages and tool error text are never stored; an outcome carries a stable code. |
| CAP-REC-3 | Fields: `schema`, `seq`, `prev`, `call_id`, `time`, `capability` (`kind`, `name`, `version`, `digest`, `label`), `language`, `code_digest`, `revocation` (`status`, `seq`), `grant`, `outcome` (`status`, `code`), `input`, `output`, `duration_ms`, `hash`. |
| CAP-REC-4 | `capability.digest` is the tool identity for a tool when the caller has it, otherwise the manifest digest; for an agent or model it is the manifest digest. `revocation.status` is `checked` with the sequence the call was checked against, or `unchecked` with null. |
| CAP-REC-5 | Outcome statuses are `ok` and `tool_failed` (code null), `input_rejected` (code `ValidationFailed`), `output_rejected` (code `PostconditionFailed`), `refused` (a code from the closed set `IndexError`, `SetupError`, `NotFound`, `BelowLabel`, `Ambiguous`, `InvalidRange`, `NoImplementation`, `IntegrityError`, `EntryError`, `SourceUnavailable`, `Revoked`, `RevocationStale`, `GrantDenied`, `SandboxUnavailable`), and `error` (code `Unexpected`). `record` refuses any other status or code, and a code the caller made up is never copied: `outcome_for` turns an unknown code into `error` / `Unexpected`. (Reviewed: review.) |
| CAP-REC-6 | The log is a hash chain. Each record carries `prev` (the previous record's `hash`, or a fixed genesis for the first) and its own `hash` over the canonical record without `hash`. Opening a log verifies the whole chain; a broken log is refused, never appended to. |
| CAP-REC-7 | `verify_log(path)` returns the records or raises. It detects an edited, removed, inserted or reordered record and a fork from two writers. It cannot detect the removal of the newest records alone; for that, an external party keeps the latest `(seq, hash)`, which `CallRecorder.head()` returns. |
| CAP-REC-8 | `calls_for(path, ...)` finds the records that match a capability (kind, name, version) or a digest, optionally by outcome status and from a sequence number, so the calls affected by a revocation can be found. |
| CAP-REC-9 | Inputs and caller-supplied references are validated: `call_id` and `grant` are bounded identifiers, `duration_ms` a non-negative integer. A bad value raises before anything is written. |
| CAP-REC-10 | An optional `digest_key` makes the input and output digests HMAC-SHA256 under a deployment key, so a log reader cannot confirm a guess at a small input by hashing it. Without it they are plain SHA-256. |
| CAP-REC-11 | The log file is created readable by its owner only, and each record is flushed and synced before `record` returns. One writer per log: two writers fork the chain, which CAP-REC-7 detects. |

## 3. Out of scope

Who calls the recorder and when (the dispatcher, in the orchestrator), shipping records to another system, signing records (the chain is tamper-evident, not a signature), and retention or rotation.

## 4. Test plan

| Test | Assertion |
|---|---|
| Shape and chain | the first record links to genesis; each links to the last; every hash recomputes |
| No content | a secret in the input, the output and an error never appears in the file bytes |
| Outcomes | each of the six statuses is produced from a result, a `CallError`, a registry error and an unexpected exception |
| Revocation and label | the sequence and label from a real resolve are recorded; `unchecked` is recorded as such |
| Validation | bad `call_id`, `grant`, `duration_ms` raise and write nothing |
| Digests | deterministic; with a key they differ from plain and are stable; the output digest is null for a call with no output |
| Tamper | an edited, removed, inserted and reordered record, and a fork from two writers, are all detected; a broken log is not appended to |
| Reopen | a reopened log continues the chain |
| Query | affected calls are found by name and version, by digest, by outcome and from a sequence; kind-qualified |
| Permissions | the file is created `0600` |
| End to end | a real resolve, input check, tool run and output check produce a record whose label, code digest and revocation sequence equal the Match's; a revoked call is recorded as `refused` / `Revoked` |
