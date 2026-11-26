# Implementation plan: S-002 registry

Status: the implementation plan, kept as a record. M1 to M6 are implemented; results are in [VERIFICATION.md](VERIFICATION.md). Tests run in containers on a Linux test host.

## Rules for every milestone

1. A milestone is ready when its files are written and its acceptance checks are listed.
2. Its checks run on the Linux test host, from a fresh copy of the tree.
3. The results are reviewed. Review acceptance is needed before the next milestone starts.
4. Nothing is committed until the owner approves it.

## Layout

```
capability/
  registry.py     load, verify the index, list, describe, resolve  (M1 to M4)
  resolve.py      candidate and language selection (internal)    (M2, M3)
  match.py        entry checks, per-resolve digests, Match  (M4)
tests/
  test_registry.py       M1 (list, describe, index verification, collisions)
  test_resolve_m2.py     M2 (name, range, kind, label)
  test_languages_m3.py   M3 (languages, NoImplementation, consistency)
  test_resolve_m4.py     M4 (entry checks, digests, Match, no execution)
  test_isolation_m5.py   M5 (isolation, load errors, determinism)
  test_extension_m6.py   M6 (fictional language, data only)
tests/fixtures/registry/   a small library built by the test: one tool in python, one in bash, one with none
```

## Milestones

### M1: Load the index, describe and list

Files: `capability/registry.py`, `tests/test_registry.py`.

Build:
- Load `index/index.json` and recompute `index_digest` (S-001 §8). A mismatch makes `list`, `describe` and `resolve` fail with `IndexError` (CAP-REG-8).
- `list(kind?, minLabel?)`: default `minLabel` is `Declared`. Lists every entry with a manifest, including the five tools with `implementations: []`.
- `describe(name, version?)`: the embedded manifest, labels, identities, implementation list. Unfiltered.

Acceptance checks:
- A tampered index fails `list`, `describe` and `resolve` with `IndexError`.
- `list()` with the default shows all five S-001 tools.
- `describe` returns the embedded manifest equal to the canonical JSON of the source.
- Two calls on the same index give the same output.

### M2: Name, range, kind and label (internal, returns no implementation)

Files: `capability/resolve.py`, `tests/test_resolve_m2.py`.

Build:
- Steps 1 to 4 of SPEC §6.2: name, kind, range, `minLabel`, then the highest version.
- Returns an internal candidate row, never a `Match`, and it is not exposed as a production result. A `Match` exists only from M4.
- Typed errors: `NotFound`, `BelowLabel`, `Ambiguous`.

Acceptance checks:
- Range `^1.0.0` picks 1.4.0 over 1.2.0 and rejects 2.0.0 (fixture library).
- An unknown name gives `NotFound`.
- A name used by a tool and an agent gives `Ambiguous` without `kind`.
- `minLabel: Validated` on a Declared fixture gives `BelowLabel`.

### M3: Languages and NoImplementation

Files: `capability/resolve.py` (step 5), `tests/test_languages_m3.py`.

Build:
- Step 5 of SPEC §6.2: choose the first caller language the tool implements.
- No match: `NoImplementation` with the tool's languages.
- Never a fallback to a language the caller did not name.

Acceptance checks:
- A fixture tool with Python and Bash: `[typescript, bash]` chooses Bash.
- `[typescript]` gives `NoImplementation` listing `[bash, python]`.
- The five S-001 tools give `NoImplementation` with an empty list, at the default label.
- `NoImplementation` is never returned as `IntegrityError`.

### M4: Per-resolve digests and the Match

Files: `capability/digests.py`, `capability/resolve.py` (steps 6 to 9), `tests/test_resolve.py`.

Build:
- Step 7 first: check the entry (exists, regular file, inside its folder). A failure is an `EntryError` for that row.
- Step 8: recompute `manifest_digest` from the embedded manifest, then `code_digest` from the chosen implementation's folder only (CAP-REG-6). A mismatch is `IntegrityError`, and no metadata is returned.
- No cache (CAP-REG-7). Measure the cost of recomputing on the fixture library and record it.
- Return the `Match` of SPEC §5. It has no callable and carries no code.
- A `Match` never runs anything. Nothing is imported.
- One agent fixture (a library source) and one model fixture (an external source) resolve through the same path: the agent gives its `code_digest`, and the external model gives `SourceUnavailable`. Kind handling is tested, not inferred from the tool-only tests.

Acceptance checks:
- Changing one byte of the chosen implementation gives `IntegrityError` on the next call, with no metadata.
- Two resolves around a byte change: the second is `IntegrityError`.
- The `Match`'s identities equal the index's when nothing has changed.
- A link entry, or an entry that resolves outside its folder, is refused.
- A fixture entry that writes a marker when run leaves no marker after resolve.

### M5: Isolation and load errors

Files: `tests/test_isolation_m5.py`.

Build:
- An implementation folder that is removed after the index is built is a realistic per-entry failure. The index is valid. `list` still shows the row, because it reads only the index. `resolve` for that row gives `EntryError.Missing`, and the other rows still resolve (CAP-REG-9).
- No broken manifest is put in an index, because S-001 refuses to publish one.

Acceptance checks:
- Removing one fixture implementation folder after indexing: `list` shows the row; `resolve` for that row gives `EntryError.Missing`; the others resolve.
- Two runs give identical `list` and `resolve` output.

### M6: Extension check

Files: `tests/test_extension_m6.py`.

Build:
- A fictional language registered in data only (as in S-001 M5) resolves when a caller names it, with no change to the registry code.

Acceptance checks:
- The fictional language resolves when named.
- No registry module changes for it.

## Exit

S-002 exits when M1 to M6 pass their checks on the Linux test host from a fresh copy, the results are recorded in `VERIFICATION.md`, and the record is agreed in review. The owner-role and commit gates are not part of this exit.
