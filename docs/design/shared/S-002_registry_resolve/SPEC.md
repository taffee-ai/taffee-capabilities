# S-002: Registry: list, describe, resolve

Status: M1 to M6 implemented and reviewed milestone by milestone (see [VERIFICATION.md](VERIFICATION.md)). Depends on S-001. Roadmap: [../../../roadmap.md](../../../roadmap.md).

Written for: the engineers who build the registry, and the orchestrator and dispatcher engineers who call it.

## 1. Purpose

Give the platform one way to find capabilities, and to answer, for a caller's languages and label floor, which implementation would be used. The registry reads only the S-001 index, and it returns inert metadata. It does not execute anything, and it does not decide who may use a capability. Authorization is govsec's (D-008). Execution is the caller's runner (S-009, T-001).

## 2. Scope

In scope:
- `list`, `describe` and `resolve` over the S-001 index.
- Language selection by the caller's preference order.
- Recomputing digests on every resolve.
- Typed errors, and fail-closed behaviour on an unverifiable index.

Out of scope:
- Execution, import, or any run of implementation code (S-009, T-001).
- Caller authentication and per-caller filtering (S-008, behind the govsec auth contract).
- Revocation (S-003). Until S-003 lands, metadata says `revocation: "unchecked"`.
- Search ranking (S-004 and S-017).

## 3. Current state

- The S-001 index carries each capability's embedded manifest, identities, implementations and labels. `capability-validate` builds and checks it.
- The five tools are Declared with `implementations: []`.
- No registry code exists.

## 4. Requirements

| ID | Requirement |
|---|---|
| CAP-REG-1 | `list` and `describe` read only the S-001 index, and verify it against the bundled S-001 index schema before any option is checked. `resolve` reads an implementation folder only to check its entry and recompute its digest (CAP-REG-6, §6.2). |
| CAP-REG-2 | `list(kind?, minLabel?)` returns entries at or above `minLabel`. The default `minLabel` is `Declared`, so every entry with a manifest is listed. |
| CAP-REG-3 | `describe(name, version?, kind?)` returns the embedded manifest, the labels, the identities and the implementation list. Without `version`, it returns the highest version. A name used by more than one kind is `Ambiguous` unless `kind` is given. The registry never picks a kind silently. |
| CAP-REG-4 | `resolve(name, range?, kind?, minLabel?, languages?)` returns inert metadata for the highest version in range that meets `minLabel` and has an implementation for the caller's languages. `languages` is in preference order. |
| CAP-REG-5 | `resolve` fails closed with typed errors. A missing or invalid bundled index schema is `SetupError`, not `IndexError`. A range the registry does not support is `InvalidRange`. Typed errors: `NotFound`, `BelowLabel`, `Ambiguous`, `NoImplementation` (tools), `IntegrityError`, `IndexError` (corrupt index only), `EntryError` (`Missing`, `Link`, `Escape`; §6.4), and `SourceUnavailable` (external agents and models only). |
| CAP-REG-6 | On every resolve, the registry recomputes the digests that apply. For a tool: `manifest_digest` from the embedded manifest, and `code_digest` from the chosen implementation's folder only. For an agent or model: `manifest_digest`, and `code_digest` for a `library` or `generated` source. A mismatch is `IntegrityError`, and no metadata is returned. |
| CAP-REG-7 | There is no digest cache. Each resolve recomputes. A `Match` returned earlier can be stale once bytes change; the next resolve recomputes and reports `IntegrityError` if they differ. |
| CAP-REG-8 | A malformed or tampered index fails `list`, `describe` and `resolve` alike, with `IndexError`. The index must parse, be a JSON object, match the S-001 index schema, verify its `index_digest`, and be in canonical form. Any other error is refused, never surfaced as a Python exception. |
| CAP-REG-9 | `list` and `describe` read only the index, so they do not check implementation files. When `resolve` reaches a row, it checks that row's entry. A missing folder, a link, or an entry outside its folder is a per-entry `EntryError` for that row. The other rows still resolve, and `IndexError` is reserved for a corrupt index. |
| CAP-REG-10 | Nothing is imported, executed or linked. The registry never runs implementation code. |
| CAP-REG-11 | Every call is deterministic for the same index and the same bytes on disk. |

## 5. Result shapes

Errors are typed values, never strings. Each carries `code` and the fields that caused it.

Resolution returns **inert metadata**, called a `Match`. It is not a handle, and it has no callable:

```
Match {
  name, kind: "tool", version,
  manifest_digest,
  language,                     // the implementation chosen for the caller
  implementation_location,      // library/<name>/<language>/
  entry_path,                   // the entry file, confined to that folder
  code_digest,                  // recomputed on this resolve
  label,
  spec: { inputSchema, readOnly, idempotent, timeoutMs, boundary },
  revocation: "unchecked"       // until S-003
}
```

A caller that wants to run the tool passes `entry_path` and `language` to its runner (S-009, T-001).

For an agent or model, `resolve` returns the same shape with its own fields. A library-source agent or model returns its `code_digest`. An external source returns `SourceUnavailable`.

## 6. Design

### 6.1 Loading

1. Read `index/index.json` and recompute `index_digest` (S-001 §8). A mismatch makes every call fail with `IndexError`.
2. Build a table keyed by `(kind, name)`, with one row per version.
3. Rows are not checked at load time. A row whose implementation cannot be read is found when `resolve` reaches it, and is reported as that row's `EntryError` (§6.4). `list` and `describe` do not check implementation files.

### 6.2 Resolve (precedence, fixed)

1. Filter rows by `name`, then by `kind` (if given). If `kind` is not given and more than one kind matches, return `Ambiguous`. If no row matches, return `NotFound`.
2. Keep rows whose version satisfies `range` (default `*`).
3. Keep rows whose label is at least `minLabel` (default `Declared`). If none remain, return `BelowLabel`.
4. Pick the highest remaining version.
5. For a tool: choose the first language in the caller's `languages` that the tool implements. If none, return `NoImplementation` with the tool's languages (empty for the five S-001 tools). Never fall back to a language the caller did not name.
6. For an agent or model with an `external` source, return `SourceUnavailable`.
7. Tool-only. Check the entry, before any digest is computed: it must exist, be a regular file, and resolve inside the implementation folder. A missing, linked or escaping entry is an `EntryError` for that row (see §6.4).
8. Recompute `manifest_digest`, then the chosen implementation's `code_digest` (CAP-REG-6). A mismatch is `IntegrityError`.
9. Return the `Match`.

Steps 5 and 7 apply to tools only. Language selection, `implementation_location` and `entry_path` do not apply to agents or models. Agents and models resolve with their own `Match` fields: `manifest_digest`, and `code_digest` for a `library` or `generated` source (CAP-REG-6). An external agent or model returns `SourceUnavailable` at step 6.

`IndexError` is reserved for a corrupt index (step 1 of §6.1). A row-level failure is an `EntryError` for that row, and the other rows are unaffected.

`NoImplementation` is never `IntegrityError`. A tool with no implementation is not a digest failure.

### 6.3 Caching

None (CAP-REG-7). The cost of recomputing is measured in M4 before any optimisation is considered.

### 6.4 Entry errors

| Code | Meaning | Reported by |
|---|---|---|
| `EntryError.Missing` | The implementation folder or entry file is not there | `resolve`, for that row |
| `EntryError.Link` | The entry, or a path to it, is a symbolic link | `resolve`, for that row |
| `EntryError.Escape` | The entry resolves outside its implementation folder | `resolve`, for that row |

`list` and `describe` do not report these, because they read only the index (CAP-REG-9).

## 7. Consumers

| Consumer | Uses | Notes |
|---|---|---|
| Orchestrator | `resolve` for each tool a turn allows | Future (taffee-orchestrator) |
| Prototype | `list` and `resolve` | Reference-consumer validation only (S-007) |
| Service (S-008) | the same functions, behind auth | After the govsec auth contract |
| MCP view (S-004) | `list` and `describe` | Read-only |

## 8. Test plan

| Test | Assertion |
|---|---|
| Tampered index | Changing the index makes `list`, `describe` and `resolve` all fail with `IndexError` |
| Default listing | `list()` shows all five S-001 tools at Declared |
| describe | Returns the embedded manifest, equal to the canonical JSON of the source |
| Range | On a fixture library, `^1.0.0` picks 1.4.0 over 1.2.0 and rejects 2.0.0 |
| NotFound | An unknown name gives `NotFound` |
| Ambiguous | A name used by a tool and an agent gives `Ambiguous` without `kind` |
| BelowLabel | `minLabel: Validated` on a Declared fixture gives `BelowLabel` |
| Language preference | A fixture tool with Python and Bash: `[typescript, bash]` gives Bash; `[typescript]` gives `NoImplementation` listing `[bash, python]` |
| No implementation | Each of the five S-001 tools: default resolve gives `NoImplementation` with an empty list; `minLabel: Validated` gives `BelowLabel` |
| Integrity | Changing one byte of the chosen implementation gives `IntegrityError` on the next call, with no metadata |
| No cache | Two resolves around a byte change: the second gives `IntegrityError` |
| Entry error | An implementation folder removed after indexing: `list` shows the row; `resolve` for that row gives `EntryError.Missing`; the other rows resolve |
| Entry confinement | A link entry gives `EntryError.Link`; an entry resolving outside its folder gives `EntryError.Escape` |
| No execution | A fixture entry that writes a marker file when run leaves no marker after resolve |
| Determinism | Two runs give identical output |

## 9. Exit gate

- All tests in §8 pass on the Linux test host, from a fresh copy.
- The five S-001 tools are listed and described, and `resolve` returns `NoImplementation` for each.
- Recorded in `docs/design/shared/S-002_registry_resolve/VERIFICATION.md`.
- The owner-role and commit gates are not part of this exit.

## 10. Decisions

1. **Inert metadata.** `resolve` never returns a callable. Execution is the caller's runner, under S-009 and T-001.
2. **No cache.** Recompute on every resolve. Measure the cost in M4. Revisit only with a sound, tested invalidation scheme.
3. **Fail-closed index.** A tampered index fails every call.
4. **Versioned catalog layout.** The target is one folder per capability version: `catalog/<kind>/<name>/<version>/manifest.yaml`. S-002 M2 uses a hand-built multi-version index as a mechanism fixture until that migration is planned and reviewed as a separate gate in S-001. The migration touches CAP-MAN-1, index discovery, the ledger and the golden fixtures.
