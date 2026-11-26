# Implementation plan: S-001 manifests and validators

Status: the implementation plan, kept as a record. S-001 is implemented; results are in [VERIFICATION.md](VERIFICATION.md). Tests run in containers on a Linux test host (D-S1-08, D-S1-10).

Written for: the engineer building S-001, and the reviewer, who reviews each milestone before the next one starts.

## Rules for every milestone

1. A milestone is ready when its files are written and its acceptance checks are listed.
2. The milestone's checks run on the Linux test host, in the test image, from a copy of the working tree.
3. The results are reviewed. Review acceptance is needed before the next milestone starts.
4. Nothing is committed until the owner says so.

## Layout (created in M1)

```
taffee-capabilities/
  pyproject.toml              pinned Python 3.12, dependencies: pyyaml, jsonschema
  capability/                 the package
    canon.py                  canonical JSON (D-S1-05)
    digest.py                 sha256-tree-v1, manifest_digest (S-001 §6)
    schema.py                 schema loading and validation
    labels.py                 label policy v1 (§7)
    index.py                  index generation (§8)
    cli.py                    capability-validate (§9)
  schemas/                    manifest.tool/agent/model.schema.json, index.schema.json
  policy/labels.yaml          policy_version: 1
  catalog/tool/<name>/manifest.yaml   five tool manifests
  tests/                      unit and integration tests
  tests/fixtures/             passing and failing manifests (agent, model); a library fixture for S-002
  index/                      generated index, committed (not in M1–M3)
```

## Milestones

### M1: Canonical JSON and digests

Files: `canon.py`, `digest.py`, `tests/test_canon.py`, `tests/test_digest.py`.

Build:
- canonical JSON: UTF-8, sorted keys, two-space indentation, LF, final newline.
- `sha256-tree-v1` over a file tree: text detection (no NUL in the first 8000 bytes), LF normalisation for text, binary unchanged, refusal of symlinks and non-regular files, byte-order path sort.
- `manifest_digest`: SHA-256 of the canonical JSON of a parsed manifest.

Acceptance checks:
- CRLF and LF text give the same `code_digest`.
- A symlink named `.DS_Store` is refused; a regular `.DS_Store` is ignored (added after review).
- A file with a NUL in its first 8000 bytes is hashed raw.
- A symlink in the tree raises an error naming the path; nothing is followed.
- The same input gives the same output across two calls in one process.

### M2: Schemas and manifest validation

Files: `schemas/*.json`, `schema.py`, `tests/test_manifest.py`, `tests/fixtures/`.

Build:
- JSON Schema draft 2020-12 for the three kinds and the index.
- Field rules: name pattern, semver version, non-blank owner (rejects TBD, none, whitespace), unknown fields rejected, source types.
- Passing and failing fixtures for the agent and model kinds. No agent or model manifest is created.

Acceptance checks:
- The field-path errors in the test plan (§11 rows 1 to 4) are produced exactly.
- Each fixture validates or fails as its name says.
- Adding a YAML comment to a manifest does not change `manifest_digest`; changing a value does.
- Unknown field `foo` fails with path `foo`.

### M3: Labels, index and CLI (fixtures only)

Files: `capability/labels.py`, `capability/index.py`, `capability/cli.py`, `policy/labels.yaml`, `tests/test_index.py` (includes the label rules), `tests/test_cli.py`.

Scope, agreed in review: M3 uses fixtures only. The five real external manifests are not written in M3, because their pins need the prototype tree at cb90f45 (M4).

Build:
- Label policy v1: Declared for every valid manifest, and nothing above it (from review). The `library/` folder is optional, so an external-only catalog builds.
- Index: embeds each normalised manifest, records `inputs_digest` and `index_digest`, uses the canonical form, and gives a `generated` source a non-null `code_digest` (library/generated is library-backed).
- A build removes any existing index first, writes to a temp file, and renames only after full success.
- CLI `capability-validate`: exit 0 with an index, exit 1 with the problems and no index.

Acceptance checks (fixtures):
- Two runs give byte-identical `index/index.json`.
- Each embedded manifest equals the canonical JSON of its source.
- Changing one byte under `policy/` changes `inputs_digest` and `index_digest`.
- A manifest that sets its own `label` is refused.
- One invalid manifest fails the build and leaves no index; a stale index is removed first.
- A folder without a manifest, a name that does not match its folder, and a policy file of another version all fail the build.
- The CLI exits 0 on a valid tree and 1 on an invalid one.

### M4 (withdrawn): prototype-pinned manifests

Superseded by LANGUAGES.md v2 (owner correction: prototype is reference only). The prototype pins, the bundle and the golden index built on them are withdrawn. Nothing from this section is committed.

### M5: Languages, implementations, ledger and conformance (current)

Tested on the Linux test host: language registry (`policy/languages.yaml`), implementations and identity (`capability/implementations.py`), publication ledger (`capability/ledger.py`), conformance runner (`capability/conformance.py`), index (`capability/index.py`), five tools declared with `implementations: []` and ledger entries, and a golden index regenerated from them.

### Previous M4 text (withdrawn)

Files: `catalog/tool/{git-log,glob,grep,unawaited-async,graph-query}/manifest.yaml`, `tests/test_pins.py`, `tests/golden/`, `VERIFICATION.md`.

Build:
- Five tool manifests, each with `owner` set to a real role, `source.type: external`, pinned by `repository`, `commit` (`cb90f45`), `path` (the prototype file), and `tree_sha256`. The prototype path goes in `summary` for humans.
- The generator recomputes `tree_sha256` for each pin from the prototype repository at the pinned commit (`git archive`), applies `sha256-tree-v1`, and compares. A mismatch fails the build.
- The index is built from the five manifests. All five are Declared (external source), with `code_digest` null.
- Golden fixtures: the five manifest digests and the index are generated inside the test image on the Linux test host and committed as `tests/golden/` only after the owner approves the commit.

Acceptance checks, all on the Linux test host:
- Golden digests and index bytes match.
- A fresh clone in a different directory reproduces them.
- A deliberately wrong `tree_sha256` fails the build.
- The prototype tree at `cb90f45` is unchanged when checked (A-S1-07).

## Exit

S-001 exits when M1 to M4 pass their checks, the results are in `VERIFICATION.md`, and the record is agreed in review. Cross-OS reproducibility is not part of the exit (D-S1-10).

## Amendments from review

- **M3, stale index:** the generator removes any existing `index/index.json` before it starts. It writes to a temporary file and renames it into place only after every manifest has validated. A failed build leaves no index file. A test runs with a stale index present, and asserts that the failed build leaves no index behind.
- **M4, commits:** the golden fixtures and the index are prepared and tested first. Committing them waits for the owner's approval. Nothing is committed on this basis alone.
