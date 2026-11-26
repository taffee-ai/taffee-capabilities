# S-001: Manifests and validators

Status: implemented (M1 to M3 and M5; M4 withdrawn). Phase 0. Results: [VERIFICATION.md](VERIFICATION.md). Roadmap: [../../../roadmap.md](../../../roadmap.md).

Written for: the engineers who will build the validators, and the owners who will write manifests.

## 1. Purpose

Make capabilities data, not code. Every capability gets a manifest that a validator can check, two identities computed over normalised content (a manifest digest for the declaration, and a code identity for the implementation), and a label computed from evidence. This feature delivers the manifest schemas, the digest algorithm, the label policy version 1, and the generator that writes the index. It lists nothing that does not have a manifest.

## 2. Scope

In scope:
- Manifest schemas for `tool`, `agent` and `model` kinds.
- A deterministic digest over a capability's files.
- Label policy v1: Declared for every valid capability. Validated and Tested for tools are T-001's (entry format and export check). **Superseded by policy v2 (T-001):** tools can now be Validated; see T-001 IMPLEMENTATION.md D8 and D9.
- Manifests for the four built-in tools and the `graph_query` wrapper.
- The index generator and its output.
- Validators run in CI.

Out of scope (later features):
- Behavioural evidence and the Tested label (T-001).
- Resolution and handles (S-002).
- Agent and model labels (M-001 to A-002).
- Revocation (S-003).
- Writing the umbrella catalog (S-005).

## 3. Current state

Verified against the prototype at `cb90f45`.

- The four tools are in the prototype's `harness/tools/` (`git-log.ts`, `glob.ts`, `grep.ts`, `unawaited-async.ts`). Their registry entries are in `harness/_src/tools.ts`, with `name`, `real`, `idempotent` and `postcondition`. None has an input schema recorded in the registry.
- The model's schemas are in `harness/models/openai-compat.ts` (`TOOL_SCHEMAS`). They cover the same four tools, and they are not checked against the registry.
- `graph_query` is registered in `harness/_src/tools.ts` and implemented in `harness/context/retrieval/query/graph-query-tool.ts`. Its schema is `GRAPH_QUERY_SCHEMA`.
- No test file exists for `git_log` under `testing/`.

S-001 does not change these files. It writes manifests that describe them.

## 4. Requirements

| ID | Requirement |
|---|---|
| CAP-MAN-1 | Each capability has exactly one manifest at `catalog/<kind>/<name>/manifest.yaml`. |
| CAP-MAN-2 | A manifest validates against `schemas/manifest.<kind>.schema.json`. Unknown fields are rejected. |
| CAP-MAN-3 | Every manifest has `kind`, `name`, `version`, `owner`, `summary`, `requires`, and either `source` (agents, models) or `implementations` (tools, LANGUAGES.md v2; an empty list is allowed). Missing fields are a validation error with the field path. |
| CAP-MAN-4 | `name` matches `^[a-z][a-z0-9-]{1,63}$`. `version` is semantic (MAJOR.MINOR.PATCH). |
| CAP-MAN-5 | A tool manifest declares `spec.inputSchema` (JSON Schema), `spec.readOnly`, `spec.idempotent`, `spec.timeoutMs`, and `spec.boundary` (the input fields that are paths, possibly empty). |
| CAP-MAN-6 | A manifest contains no code and no credentials. An external source is a pinned reference (repository, commit, tree digest), never a filesystem path outside the repository. |
| CAP-MAN-7 | Two digests are computed as §6 specifies: `manifest_digest` (canonical JSON of the parsed manifest) and, for `library` sources, `code_digest` (sha256-tree-v1). Recomputing them on a clean checkout gives the same values. |
| CAP-MAN-8 | The label is computed by the policy in §7 from the manifest and the evidence present. The validator never accepts a label written in the manifest. |
| CAP-MAN-9 | The index lists every valid capability with name, kind, version, `manifest_digest`, its code identity (`code_digest` for `library`; `tree_sha256` and `path` for `external`), label, and evidence references, and embeds each entry's normalised manifest. Invalid capabilities are a build failure, not omitted silently. |
| CAP-MAN-9a | Identity and version rule: a capability version is identified by `manifest_digest` together with its code identity. A change to either is a new version. A label is never carried over to a new identity. |
| CAP-MAN-10 | The index records `inputs_digest` (a digest over `catalog/`, `schemas/`, `policy/` and `library/`, excluding `index/`) and a digest over its own contents. It does not record a commit SHA. The claim is narrow: `inputs_digest` identifies the inputs, not the commit that contains them. S-005 attaches the committed index's Git SHA externally. |
| CAP-MAN-11 | Running the validators twice on the same input gives byte-identical output. |

## 5. Manifest shape

Common fields, for every kind:

```yaml
kind: tool
name: git-log
version: 1.0.0
owner: domain-owner-tools          # a role or team; required, never blank
summary: Show recent commits on the current branch.
source:
  type: library                    # library | external | generated
  location: library/git-log        # library and generated only
  # external only, instead of location:
  # repository: https://<host>/<org>/<repo>
  # commit: <git sha>
  # path: <file or directory in the repository holding the capability>
  # tree_sha256: sha256:<hex>      # sha256-tree-v1 over `path` at `commit`, per §6; checked by the generator
requires:                          # permissions the capability needs; grants are govsec's
  - repo.read
```

Tool-specific:

```yaml
spec:
  inputSchema: { type: object, properties: { n: { type: integer, minimum: 1 } }, required: [n] }
  readOnly: true
  idempotent: true
  timeoutMs: 5000
  boundary: []                     # fields that are repository paths
```

The `label` field is not part of the manifest. The label is in the index, computed by the validator.

## 6. Digest

There are two digests, and they answer different questions.

- **`manifest_digest`** identifies the declaration. It is the SHA-256 of the canonical JSON form (D-S1-05) of the parsed manifest. The YAML file is the authoring source, so formatting and comments do not change it. The same canonical JSON is embedded in the index, so the registry recomputes `manifest_digest` from the index alone. It can support the Declared label and nothing more. It never implies that the code that runs is the code that was reviewed.
- **`code_digest`** identifies the implementation. It exists for `library` and `generated` sources, because both live under `library/` (a generated source under `library/generated/`). It covers that folder. A `generated` source never gets a null `code_digest` in the index. For an `external` source, the pinned `tree_sha256` in the manifest plays this role, and the digest is computed by the same algorithm over the external tree at the pinned commit. A `code_digest` or `tree_sha256` mismatch is an integrity error.

`manifest_digest` uses the canonical JSON form only. `code_digest` and `tree_sha256` use the following algorithm (version 1, named `sha256-tree-v1` in the index), applied to a file tree. A `path` may name a directory or a single file; a single file is a tree of one.

1. List the files under the tree (for `code_digest`, the `library/<name>/` folder; for `tree_sha256`, the pinned `path` at the pinned commit), recursively.
2. Exclude nothing except files named `.DS_Store` and `__pycache__` directories.
3. Text detection: a file is text if it has no NUL byte in its first 8000 bytes. For text files, normalise line endings to LF (CRLF becomes LF). The digest therefore identifies normalised content, not raw bytes. Binary files are hashed unchanged.
3a. Symlinks and non-regular files (devices, sockets, pipes) are refused. The generator fails with an error naming the path; it never follows a link.
4. Sort the list by path, using byte order of the UTF-8 path with `/` as the separator.
5. For each file, take `path`, a NUL byte, and the SHA-256 of the normalised content (hex), followed by a newline.
6. The digest is `sha256:` followed by the SHA-256 of that concatenated text.

When T-001 moves an external tool into `library/`, its `code_digest` appears and its capability digest changes. The index records that transition explicitly; it is not an in-place edit.

Verification: golden digests for the five manifests are generated inside the test image on the Linux test host and committed as fixtures. A second run from a fresh clone, in a different directory on the Linux test host, must reproduce them exactly. This is same-host clean-clone reproducibility, not cross-host proof. No digest is computed on the Mac (see §11).

## 7. Label policy v1

Policy file: `policy/labels.yaml`, with `policy_version: 1`.

S-001 awards **Declared** to every valid capability, and nothing above it. The reason: Validated for a tool needs the library entry format and the export check (the entry exports `execute` and, if declared, `postcondition`). That format is decided in T-001, so S-001 cannot award Validated honestly. The boundary and readOnly checks belong to T-001's Validated rule and are not run as labels in S-001. The manifest schema is still checked, because it is needed for Declared.

| Kind | Level awarded by S-001 | Conditions |
|---|---|---|
| all | Declared | Manifest validates against its kind's schema; digests compute |

Defined later, not awarded here:
- tool **Validated** and **Tested**: T-001 (entry format, export check, behavioural evidence).
- agent **Trusted**: A-002.
- model **Approved**: M-001.

A label is never read from a manifest. A manifest that sets one is refused as an unknown field (M2).

## 8. Index

`index/index.json` (generated; committed):

```json
{
  "schema": "capability-index/1",
  "policy_version": 1,
  "inputs_digest": "sha256:<hex>",
  "entries": [
    { "name": "git-log", "kind": "tool", "version": "1.0.0",
      "manifest_digest": "sha256:<hex>", "code_digest": null,
      "label": "Declared",
      "source": { "type": "external", "repository": "...", "commit": "...",
                  "path": "harness/tools/git-log", "tree_sha256": "sha256:<hex>" },
      "evidence": [],
      "manifest": { "kind": "tool", "name": "git-log", "version": "1.0.0", "...": "normalised manifest, canonical JSON" } }
  ],
  "index_digest": "sha256:<hex>"
}
```

`inputs_digest` is computed by §6 over `catalog/`, `schemas/`, `policy/` and `library/`, excluding `index/`. It replaces a commit SHA, which cannot be written into a file that is itself committed in that commit.

The index file, every manifest embedded in it, and every digest input use one canonical JSON form (D-S1-05): UTF-8, sorted keys, two-space indentation, LF line endings and a final newline. `index_digest` is the SHA-256 of that form with the `index_digest` field removed.

The index embeds each entry's normalised manifest. The registry therefore reads only the index: `describe` and `resolve` need no read of `catalog/` (CAP-REG-1).

## 9. Components

| Component | Responsibility |
|---|---|
| `schemas/` | JSON Schema for the three kinds and the index |
| `validators/manifest.*` | Schema check, field rules, name and version rules |
| `validators/digest.*` | §6 algorithm |
| `validators/label.*` | §7 policy |
| `validators/index.*` | Builds the index; computes `index_digest` |
| `cli: capability-validate` | Runs all of the above; exit code non-zero on any failure |

Language is an open decision (see the roadmap). The interface is the same in either language: the CLI, the file formats and the digest rules.

## 10. Initial manifests (superseded by LANGUAGES.md v2)

The five tool manifests are `implementations: []`. They have no external source and no pin. The prototype is reference only and is never a manifest source. Their `versions.yaml` ledger entries record the identity computed by `capability/implementations.py`. Real implementations are T-001's.

Earlier text in this section described prototype pins. Those pins are withdrawn and are not part of any index.

### Previous text (withdrawn)

Five tool manifests (`git-log`, `glob`, `grep`, `unawaited-async`, `graph-query`), each with `owner` set to a real role. The code for these tools is moved into `library/` in T-001, so in S-001 their `source.type` is `external`, pinned by `repository`, `commit` and `tree_sha256` of the prototype tree at `cb90f45`, as §5 requires. The prototype path is recorded in `summary` only, for human reference.

Consequence: S-001 awards Declared only (§7), so all five tools are labelled **Declared** here, with `code_digest` null. This is intended. T-001 moves the code into `library/`, defines the entry format, and awards Validated and then Tested.

Schema fixtures: S-001 also includes one passing and one failing manifest fixture for the agent and model kinds. No agent or model manifests are produced.

## 11. Test plan

| Test | Assertion |
|---|---|
| Schema rejects unknown field | Manifest with `foo: 1` fails with path `foo` |
| Schema rejects missing owner | Fails with path `owner` |
| Name rule | `Git_Log` and `a` fail; `git-log` passes |
| Version rule | `1.0` fails; `1.0.0` passes |
| Digest stable | Same tree twice gives the same digest |
| Digest sensitive | Changing one byte of a library file changes the digest |
| Digest ignores line endings | CRLF and LF text give the same digest |
| Label ignores manifest label | A manifest that says `label: Tested` is labelled Declared |
| Index deterministic | Two runs give identical `index.json` |
| Embedded manifest | Each index entry's embedded manifest equals the canonical JSON of the parsed manifest file; `describe` returns it |
| Manifest digest from index alone | The registry recomputes `manifest_digest` from the embedded JSON and matches the index |
| Comments do not change identity | Adding a comment to a manifest YAML leaves `manifest_digest` unchanged, and changing a field value changes it |
| Index fails on invalid | One invalid manifest fails the build; the index is not written |
| Golden digests | The five manifest digests equal the committed golden fixtures, generated in the test image on the Linux test host |
| Fresh-clone reproduction (same host) | A fresh clone on the Linux test host, in a different directory, reproduces the same digests and index bytes |
| Normalisation | CRLF and LF text give the same digest; a file with a NUL byte in its first 8000 bytes is hashed as binary (unchanged bytes) |
| Links refused | A symlink inside a library or external tree fails the generator with the path named; nothing is followed |
| Generated sources have a code_digest | A `generated` source's index entry carries a non-null `code_digest` computed over `library/generated/<name>/` |
| External tree pin | The generator recomputes `tree_sha256` at the pinned commit and fails on mismatch |
| Schema fixtures, agent and model | Passing and failing fixtures validate or fail as declared; no agent or model manifest is generated |
| Inputs digest changes | Changing one byte under `policy/` changes `inputs_digest` and the index digest |

Tests run on the remote host, per the project rule: tests run in containers on a remote Linux host.

## 12. Exit gate

- All tests in §11 pass on the remote host.
- The index is generated from the five tool manifests, with `index_digest` verified by a second run.
- The golden-fixture and same-host fresh-clone checks (§11) are recorded in `docs/design/shared/S-001_manifests_validators/VERIFICATION.md`.
- Cross-OS reproducibility is not part of this gate. It needs a host the owner permits.
- `taffee-core/catalog/capabilities.yaml` is not written by this feature. That is S-005.

## 13. Decisions

1. **Language.** Python recommended (see roadmap).
2. **Manifests for the prototype tools before the move.** Decided: write them now, labelled Declared, so the pipeline and the honest label are visible from the first commit.
3. **Digest version name.** `sha256-tree-v1`. Changing the algorithm is a new version of the policy, not an edit.

## 14. Risks

| Risk | Mitigation |
|---|---|
| Digest differs by filesystem (case, permissions) | Digest covers content and paths only, not modes or timestamps; §6 excludes them |
| Schema for `inputSchema` is too loose | Validate that it is a JSON Schema with the standard meta-schema |
| Owner field becomes a placeholder | Validator rejects `TBD`, `none`, empty and whitespace-only values |


## Migration: index implementations carry their entry (S-002 M4)

Each indexed tool implementation records its `entry` filename, taken from `policy/languages.yaml` at build time. S-002 resolves the entry from the verified index, so a later change to the language policy cannot change a resolved entry without changing the index and its digest. Schema, builder, golden fixture and tests are migrated together.
