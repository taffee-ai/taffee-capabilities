# S-005: Umbrella catalog generator

Status: implemented, tested (see [VERIFICATION.md](VERIFICATION.md)) and accepted in review on 2026-10-19, code and record. The D-002 filename, publishing and OPS owner questions stay open. Depends on S-001 and S-002. Roadmap: [../../../roadmap.md](../../../roadmap.md).

Written for: the engineers who maintain the umbrella catalog, and anyone who reads `taffee-core/catalog/capabilities.yaml`.

## 1. Purpose

The umbrella repository lists what is reusable and the label each entry has earned in `taffee-core/catalog/capabilities.yaml`. That file is a view, not a source. This feature generates it from the capabilities repository's verified index, so the two cannot disagree and nobody edits the view by hand.

## 2. Requirements

| ID | Requirement |
|---|---|
| CAP-CAT-1 | The generator reads only the index, through the S-002 loader, so a missing, tampered or non-canonical index is an error and nothing is written. |
| CAP-CAT-2 | One catalog entry per index entry, sorted by kind, name and version. Fields: `name`, `kind`, `version`, `digest`, `label`, `owner`, `summary`, `source`, `evidence`. |
| CAP-CAT-3 | `digest` is the tool identity for a tool, and the manifest digest for an agent or model. `owner` and `summary` come from the manifest embedded in the index. `label` is the index's label. |
| CAP-CAT-4 | `source` is the capability's place in the capabilities repository: `taffee-capabilities/library/<name>` for a tool with an implementation, the declared `source.location` for a library or generated agent or model, the pin (`repository@commit:path`) for an external one, and null where there is nothing (a tool with no implementation). |
| CAP-CAT-5 | `evidence` is a path: the evidence folder for Tested, the conformance case file for Validated, null for Declared. The generator does not open those paths. |
| CAP-CAT-6 | The header records `policy_version`, the index's `inputs_digest` and `index_digest`, and `index_version`. `index_version` is the capabilities commit the index was built from. It cannot be inside the index, so the caller passes it (`--commit`, 40 lowercase hex). With none, it is null. |
| CAP-CAT-7 | Output is deterministic: the same index and commit give the same bytes (UTF-8, LF, one final newline). |
| CAP-CAT-8 | The file is written atomically. On any error the existing file is untouched and no partial file remains. |
| CAP-CAT-9 | The file says it is generated and must not be edited by hand. |

## 3. Out of scope

Running the generator in CI, and committing the result (both are operator steps). The roadmap names the file `capabilities-index.yaml`; the umbrella already holds `capabilities.yaml` with this shape, so that name is kept.

## 4. Test plan

| Test | Assertion |
|---|---|
| Real catalog | Five entries sorted; the four tools Tested with an evidence folder; `graph-query` Declared with null source and evidence; tool `digest` equals the index identity |
| Header | Digests equal the index's; `index_version` null without `--commit`, the commit with it; a bad commit is refused |
| Deterministic | Two runs give identical bytes; LF only; final newline |
| Index only | Deleting the catalog, library and evidence folders after the build does not change the output |
| Agent | A library agent's digest is its manifest digest and its source is its location |
| External pin | An external source renders as `repository@commit:path` |
| Tampered or missing index | Error, no output file, and an existing output file is untouched |
| CLI | Exit 0 with the path, exit 1 for an index error, exit 2 for a bad argument |
