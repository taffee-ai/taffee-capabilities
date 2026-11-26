# Languages and implementations (S-001 design change, v2)

Status: implemented (S-001 M5). Results are in [VERIFICATION.md](VERIFICATION.md).

Written for: the owners of capabilities, the reviewer, and the engineers who will add a language later.

## 1. Scope

- **Tools only** carry `implementations`. A tool is a language-neutral contract, and its code lives in `library/` in one or more languages.
- **Agents and models** keep the source rules of the M2 schemas (agents: library, generated or external; models: library adapter, generated or external). Nothing changes for them here.
- **The prototype** is reference only. It is never a manifest source, and it is not named in any manifest. It appears only in migration and provenance notes.
- **Owner role.** Provisionally `capabilities-tools-owner`. To be confirmed by the owner.

## 2. Tool manifest shape

The tool `source` field is replaced by `implementations`:

```yaml
implementations:
  - language: python
    location: library/git-log/python
  - language: bash
    location: library/git-log/bash
```

Rules:
- `language` is a registered id in `policy/languages.yaml`. Unknown ids fail validation.
- Each language appears at most once per tool. Duplicates fail validation.
- `location` must be a relative path inside `library/`, with no `..` and no symlinks.
- An empty list is allowed. A tool with `implementations: []` is Declared, has no code identity, and is **non-resolvable** in S-001 (see §8).

The five prototype tools in S-001 are manifests with `implementations: []`. They carry no external source, no pin and no prototype path. T-001 creates their implementations in `library/`.

## 3. Ordering and identity

- Implementations are sorted by `language` id before the canonical `manifest_digest` is computed and before the index embeds them. The order in the manifest file therefore never changes identity. A reorder test enforces this (review, amendment 1).
- The identity of a tool version is `manifest_digest` together with the **set** of `(language, code_digest)` pairs. The set is order-independent because it is sorted by language before hashing.
- Any change to a `code_digest` changes the identity. **A changed identity at an already-published version is refused.** The version ledger (§4) makes this checkable.
- `policy/languages.yaml` is in `inputs_digest` (it sits in `policy/`, which is an input folder).

## 4. Version ledger

`catalog/tool/<name>/versions.yaml` records each published version and its identity:

```yaml
versions:
  - version: 1.0.0
    identity: sha256:<hex>
```

The validator refuses a build when a version listed in the ledger has a different identity now. A new identity needs a new version, and the ledger gains a new line. 
The ledger is part of the catalog, so it is committed with the manifests once the owner approves a commit.

**Publication gate (review, amendment 2).** A publishable index needs a `versions.yaml` entry for every catalog tool and version, including tools with `implementations: []`. There is no silent exemption for catalog entries. Tests cover three cases: a missing entry (refused), a stable entry (accepted), and a changed identity (refused). Fixture tests stay outside the publication gate, or run under an explicit fixture mode.

## 5. Language registry

`policy/languages.yaml`:

| Field | Meaning |
|---|---|
| `id` | Unique, stable, lowercase. Used in manifests and in the index. |
| `extensions` | Source suffixes the implementation uses (for validation only). |
| `entry` | Entry file relative to the implementation folder. Must resolve inside it. |
| `runner` | Runner name. Each runner is a module that accepts the structured argv (§6). |
| `toolchain` | Argv that checks the toolchain is present. A runtime check, not a digest input. |

Adding a language: one entry here, and a runner if none fits. The manifest schema, identity rules and registry code do not change.

## 6. Runner rules

- **Structured argv only.** The runner builds an argv list. No shell, no string interpolation, no `shell=True`.
- **Entry confinement.** The entry path is resolved inside the declared implementation folder. A path that escapes it, or a symlink, is refused.
- **Bounds.** Every run has an explicit timeout and an output byte limit. Exceeding either is a failure, not a hang.
- **Toolchain check at runtime.** The toolchain command runs when a conformance run starts. It is not part of the index, so an index does not change when a machine changes.
- **Reproducibility.** The environment passed to a runner is fixed (a minimal allow-list), not inherited.

## 7. Conformance and expectation operators

Conformance cases live at `conformance/<tool-name>/cases.json`: a list of `{id, input, expect}`. The operators in `expect` are deliberately few:

| Operator | Passes when |
|---|---|
| `ok` | the result's `ok` field equals the given boolean |
| `contains` | the output contains the given substring |
| `not_contains` | the output does not contain the given substring |
| `json_key` | the output parses as JSON and the given key path exists |
| `exit_code` | the runner's exit code equals the given integer |

No other operator exists in S-001. A new one needs a decision.

S-001 checks conformance only with fixture tools, as a mechanism test. Real tool conformance, and any label above Declared, belong to T-001.

## 8. Resolution (S-002 contract)

The caller passes the languages it can run, in preference order.

Precedence, in order:
1. name, range and kind;
2. `minLabel`;
3. source or implementation availability: if the tool has no implementation in any language the caller named, return `NoImplementation`, listing the languages the tool does have (none, for S-001's five);
4. `manifest_digest`;
5. the code identity of the chosen implementation.

The registry chooses the first caller language that the tool implements. It never falls back to a language the caller did not name. `NoImplementation` is never returned as `IntegrityError`.

For S-001's five tools (`implementations: []`), `resolve` returns `NoImplementation` with an empty language list, after `minLabel`.

## 9. What S-001 proves

- The language registry loads, and an unknown or duplicate language fails validation.
- A fixture tool with implementations in Python, TypeScript and Bash gets one `code_digest` per language, and the identity is order-independent.
- Changing one implementation's bytes changes the identity, and the ledger refuses the same version with a new identity.
- The index lists implementations in language order.
- Fixture conformance runs under each registered runner, using the §7 operators.
- An extension test adds a fictional fourth language to `policy/languages.yaml` with a fake runner, and the manifest, identity, index and resolution code accept it without a code change.

## 10. Decisions

- D-S1-13: languages are registered as data in `policy/languages.yaml`.
- D-S1-14: tools carry implementations per language; identity is the manifest digest plus the sorted set of `(language, code_digest)`.
- D-S1-15: conformance uses language-neutral cases and a fixed, small set of operators.
- D-S1-16: S-001 proves the mechanism with fixtures; the five prototype tools are declared with no implementations and are ported in T-001.
- D-S1-17: no silent fallback. An unknown or unnamed language is refused, and a tool with no implementation in the caller's languages returns `NoImplementation`.
- D-S1-18: a changed identity at a published version is refused by a version ledger (`versions.yaml`).
- D-S1-19: the prototype is never a manifest source.

## 11. Open items

- The capabilities owner role: provisional `capabilities-tools-owner`, to be confirmed by the owner.
- The exact runner environment allow-list (§6), to be fixed in the first implementation milestone.
- Whether `versions.yaml` is generated or hand-written. Proposed: hand-written, and the validator only checks it.

## 12. Change map (to apply after review)

| Document | Change |
|---|---|
| SPEC §4 CAP-MAN-3 | Tools carry `implementations` instead of `source`; agents and models keep `source`. |
| SPEC §4 CAP-MAN-7, 9, 9a | Identity is `manifest_digest` plus the sorted set of `(language, code_digest)`; the index embeds sorted implementations; a changed identity at a published version is refused by the ledger. |
| SPEC §5 | Tool manifest shape uses `implementations` (§2 above). |
| SPEC §8 | Index entry for tools carries `implementations`; `inputs_digest` covers `policy/languages.yaml`. |
| schemas/manifest.tool.schema.json | Replace `source` with `implementations` (array, unique `language`, `location` under `library/`, may be empty). |
| schemas/index.schema.json | Tool entries carry `implementations`. |
| policy/languages.yaml | New file, v1 registry (python, typescript, bash). |
| S-001 §10 and the five tool manifests | `implementations: []`, no source, no pin. Versions ledger entries for each. |
| S-002 §4, §5, §6.2, §8 | `NoImplementation` precedence and caller language preference; `SourceUnavailable` replaced for tools by `NoImplementation`; tests for both. |
| Roadmap S-001, S-002, T-001 | S-001: mechanism with fixtures; five tools declared, no implementations. T-001: real implementations in each language, conformance cases, and labels. |
| IMPLEMENTATION.md | M4 superseded: prototype-pinned plan withdrawn. New milestones follow the agreed order, with tests under fixture mode. |
