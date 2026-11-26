# S-005 verification record

Status: implemented, tested and **accepted in review (2026-10-19), code and record.** The D-002 filename, publishing and OPS owner questions stay open outside this slice.

Written for: the reviewer who will check this, and whoever regenerates the umbrella catalog.

## How tests were run

Tests on the Linux test host from a fresh copy of the tree, in the test image.

## Result

- `tests/test_catalog_export.py`: 19 tests OK.
- Whole suite: 408 tests OK when this record was written. Current (re-run 2026-10-19 on the Linux test host): `test_catalog_export` with `test_capctl` 52 tests OK; whole suite 662 tests OK with the Docker socket mounted, none skipped.
- Currency of the generated output (the umbrella file is an untracked change): on a fresh copy of the current tree the validator builds the index (5 entries, `index_digest` `sha256:90db9234...2e24a3`) and the exporter then reproduces `inputs_digest` `sha256:4323d822...193439f` and the same `index_digest` that `taffee-core/catalog/capabilities.yaml` carries, so the umbrella file matches the current tree (glob 1.3.0 included). **Superseded on 2026-10-19 by T-005 Stage A:** the manifest schema gained an optional `spec.sandbox` block, and `schemas/` is an indexed input, so the digests moved to `inputs_digest` `sha256:3db90887...08df5` and `index_digest` `sha256:35660ccc...f7e` (after the schema's limits were corrected twice in the same feature); the golden index and the umbrella file were regenerated, and no entry changed (checked path by path). The figures above are the S-005 review's, kept as history.

## Checks per requirement

| Requirement | Tests |
|---|---|
| CAP-CAT-1 index only, through the loader | tampered index (error, existing file untouched, no `.tmp` left), missing index (error, no file), only the index is read (catalog, library and evidence folders deleted after the build give identical output) |
| CAP-CAT-2 and 3 entries and digests | five entries sorted; field order; a tool's `digest` equals the index identity and labels equal the index's; a built fixture agent uses its manifest digest |
| CAP-CAT-4 source | tools point at `taffee-capabilities/library/<name>`; `graph-query` has null; a library agent uses its location; an external pin renders as `repository@commit:path` |
| CAP-CAT-5 evidence | Tested tools point at the evidence folder; `graph-query` null; a model gets none |
| CAP-CAT-6 header | policy version and both digests equal the index's; `index_version` null without a commit and the commit with one; five malformed commits refused and nothing written |
| CAP-CAT-7 deterministic | two renders identical; no carriage return; exactly one final newline |
| CAP-CAT-8 atomic | a failed run leaves the previous file untouched and no temporary file |
| CAP-CAT-9 generated notice | the header says GENERATED and "Do not edit by hand" |
| CLI | exit 0 prints the path; exit 1 for an index error and for an unwritable destination; exit 2 for missing or malformed arguments |

## What was generated

`taffee-core/catalog/capabilities.yaml` was regenerated from the real index (built on the Linux test host) and now lists the five tools: four Tested (`git-log`, `grep` and `unawaited-async` at 1.2.0, `glob` at 1.3.0) and `graph-query` Declared. Its `index_digest` equals the golden index's. `index_version` was null because the tree had no commit yet. The previous file was an empty placeholder.

## Notes

- The roadmap names the output `capabilities-index.yaml`. The umbrella already held `capabilities.yaml` with this shape, so that name is kept.
- The umbrella decision D-002 defines `catalog/<pillar>.yaml` as a pillar's service list, but `capabilities.yaml` holds a capability catalog. One filename carries two meanings. This is unchanged and unresolved.
- Running the generator in CI and committing the result are operator steps, not part of this feature.
