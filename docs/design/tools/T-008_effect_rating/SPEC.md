# T-008: Effect rating (`spec.effect`)

Status: implemented and tested, 2026-11-27. Not yet reviewed. Roadmap: [../../../roadmap.md](../../../roadmap.md).

Written for: tool authors, and engineers building a caller that decides which tools a run may use.

## Purpose

A caller that lets a model pick tools needs to know what a call can change. `spec.readOnly` says only whether a tool changes nothing. It cannot tell a reversible change (a branch, a draft) from an irreversible one (a deletion, a sent message), so a caller had to treat every tool that is not read-only as irreversible.

## Requirements

| ID | Requirement |
|---|---|
| CAP-EFF-1 | A tool manifest may declare `spec.effect`: `read-only`, `reversible` or `irreversible`. Any other value is a schema error. |
| CAP-EFF-2 | A declared effect agrees with `spec.readOnly`: `read-only` exactly when `readOnly` is true. Admission rejects a manifest that contradicts it. |
| CAP-EFF-3 | When `spec.effect` is absent, the effect is `read-only` for a tool declared `readOnly: true` and `irreversible` otherwise. What is not declared undoable is treated as not undoable. |
| CAP-EFF-4 | Search, list and describe report each tool's `effect` (declared, or by CAP-EFF-3). Describe also shows the declaration itself in `spec`, absent when not declared. |
| CAP-EFF-5 | The field is optional, so no existing tool changes version. The four built-in tools declare none and report `read-only`. |

## Tests

`tests/test_effect.py`: the rule for every declared value and for its absence; every consistent and every contradicting combination; six malformed values; admission accepting and rejecting; search, list and describe for five tools covering every case; the built-in tools unchanged.
