# T-001: Built-in tools reach Tested

Status: slices 1 to 4 done (four Python implementations, Validated by conformance cases, Tested by evidence); `graph-query` and migration parity open. See [IMPLEMENTATION.md](IMPLEMENTATION.md), which also records where this implementation departs from the TypeScript assumptions below. Depends on S-002. Roadmap: [../../../roadmap.md](../../../roadmap.md).

Written for: the engineers moving the tools, and the reviewers who will read their evidence.

## 1. Purpose

Move the four built-in tools and the `graph_query` wrapper into `library/`, give each a tested contract, and record evidence that lets the validator award **Tested**. After this feature, the catalog has its first labels that are backed by something more than a registry flag.

## 2. Scope

In scope:
- Move `git-log`, `glob`, `grep`, `unawaited-async` and `graph-query` into `library/`, each with its spec, entry point, postcondition and tests.
- Add a test suite for `git_log`, which has none today.
- Behavioural tests for each tool, including failure paths and path confinement.
- Evidence records and the fingerprint that ties them to the digest.
- Move the shared helpers the tools need (decision in §10).

Out of scope:
- Changing tool behaviour. This feature preserves behaviour. Any change is a new version.
- The prototype dispatcher and adapter (S-007).
- Agent and model evidence.

## 3. Current state

Verified against the prototype at `cb90f45`.

| Tool | Source | Dependencies | Tests today |
|---|---|---|---|
| `git_log` | `harness/tools/git-log.ts` | `execFileBounded` (`_src/subprocess.ts`), `context.repoRoot` | None under `testing/`. Exercised indirectly by 14 files (for example `tool-dispatcher.test.ts`, `caching.test.ts`, `eval/corpus/git-log.json`). |
| `glob` | `harness/tools/glob.ts` | `resolveExistingRepoPath` (`tools/path-policy.ts`) | `testing/glob.test.ts` |
| `grep` | `harness/tools/grep.ts` | `resolveExistingRepoPath` | `testing/grep.test.ts` |
| `unawaited_async` | `harness/tools/unawaited-async.ts` | `resolveExistingRepoPath` | `testing/unawaited-async.test.ts` |
| `graph_query` | `harness/context/retrieval/query/graph-query-tool.ts` (wrapper in `_src/tools.ts`) | Context code, lazily imported; `GRAPH_QUERY_SCHEMA` | `testing/context-graph.test.ts` (tests the Context query, not the wrapper's contract) |

Postconditions are in `harness/_src/tools.ts` (`git_log`, `glob`, `graph_query`) and are trivial for `grep` and `unawaited_async`.

Observed facts to preserve:
- `dispatchTool` passes `repoRoot: process.cwd()`. The library must take `root` from its caller instead.
- `git_log` uses `cwd: context.repoRoot` in `execFileBounded`. No shell.
- `grep` and `glob` confine paths through `resolveExistingRepoPath`.

## 4. Requirements

| ID | Requirement |
|---|---|
| CAP-T-1 | Each tool lives in `library/<name>/` with `spec.ts` (or equivalent), `execute`, `postcondition` and `tests`. |
| CAP-T-2 | Each tool's manifest matches its spec: the input schema in the manifest and the schema in `spec` are identical after normalisation. A drift test enforces this. |
| CAP-T-3 | `execute` takes `root` from `ctx.root`. There is no `process.cwd()` default in the library. |
| CAP-T-4 | Every tool is tested for: a valid call returns `ok: true` with output; an invalid input returns `ok: false` without throwing; a missing root returns an error, not a crash. |
| CAP-T-5 | Every tool that accepts a path is tested for: a path outside `root` is refused; a symlink that escapes `root` is refused; `..` segments are refused. |
| CAP-T-6 | `git_log` is tested for: a repository with commits (output contains commit lines); an empty repository (empty output, valid); a directory that is not a repository (error result, not a crash); a negative or zero `n` (rejected by schema). |
| CAP-T-7 | `glob` and `grep` are tested for a bounded result: a pattern matching more files than the bound returns the bounded output and a truncation marker. |
| CAP-T-8 | `unawaited_async` is tested on a file with a known unawaited async function and on a file without one. |
| CAP-T-9 | Every tool is tested for timeout: a call past `spec.timeoutMs` returns a timeout error, and the child process is gone afterwards. |
| CAP-T-10 | Every postcondition is tested with output that passes and output that fails (for example `git_log` output without commit lines fails). |
| CAP-T-11 | `graph_query` is tested for the wrapper's contract only: argument validation, the postcondition, and the error shape when the Context service is unreachable. Its query correctness stays with the Context pillar. |
| CAP-T-12 | `graph_query` declares `idempotent: false`, because omitted commit SHA resolves to the latest commit at call time. The test asserts the flag. |
| CAP-T-13 | No test reads or writes outside its own temporary directory. |
| CAP-T-14 | Evidence is written to `evidence/tools/<name>/<version>/` (see §7). |

## 5. Design

### 5.1 Layout

```
library/git-log/
  index.ts          exports execute, spec, postcondition
  spec.ts           inputSchema, readOnly, idempotent, timeoutMs, boundary
  postcondition.ts
  tests/            behavioural tests, one file per tool
library/graph-query/
  index.ts          wrapper; calls the Context MCP service through a client
  spec.ts           the wrapper's own schema, checked against the Context service's published schema (§5.3)
  tests/
```

### 5.2 Shared helpers

`execFileBounded`, `resolveExistingRepoPath` and the output-bounding helper move to `library/shared/`. Decision §10.1 covers where they finally live. Each helper keeps its current behaviour; its tests move with it.

### 5.3 `graph_query` and the Context service

The wrapper does not import Context code. It calls the Context MCP service through a client, and its `spec.ts` holds the schema the wrapper declares. A contract test fetches the service's published tool list and asserts that the declared schema matches. The test is skipped with a recorded reason when the service is not running, and that skip counts against the evidence.

### 5.4 Evidence

After the tests pass, the evidence step writes:

```
evidence/tools/<name>/<version>/
  summary.json      { name, version, digest, policy_version, tests: { passed, failed, skipped, skipped_reasons }, host, runner_version, fingerprint }
  output.txt        the test log, trimmed to 200 KB
```

`fingerprint` is the SHA-256 of: the digest, the test-suite version, the runner version, the host OS and architecture, and the commit of the test code. Two evidence records with the same fingerprint are the same claim.

The validator awards **Tested** (§7) only when:
- the `summary.json` digest equals the current digest of `library/<name>/`;
- `failed` is 0;
- `skipped` is 0, or every skip has a reason on the list in the label policy;
- the fingerprint recomputes from the summary's fields.

### 5.5 Label policy extension

`policy/labels.yaml` version 2 adds for tools:

| Level | Conditions added to Validated |
|---|---|
| Tested | Evidence record present; §5.4 conditions hold |

Policy version bumps from 1 to 2. The index records the policy version it was built with (S-001, CAP-MAN-10).

## 6. Test plan

The tests in §4 are the plan. In addition:

| Test | Assertion |
|---|---|
| Migration parity | For a fixed set of calls (one per tool, fixed repository fixture), the output before the move equals the output after |
| Postcondition parity | The same outputs pass or fail the postcondition as in the prototype |
| Drift | The manifest schema and the spec schema are equal for each tool |
| Evidence tamper | Changing a byte in `library/git-log/` after evidence is written makes the Tested label fall back to Validated |

## 7. Exit gate

- All tests in §4 and §6 pass on the remote host.
- Each of the five tools reaches **Tested** in the index, with evidence present.
- The fingerprint check is recorded in `VERIFICATION.md`, as a same-host fresh-clone reproduction on the Linux test host. Cross-OS proof needs a host the owner permits.
- T-001 records the change of capability digest for each tool, from the S-001 `manifest_digest` to the new `code_digest`, as an explicit version change (D-S1-03).
- The prototype's own test suite still passes unchanged (the prototype is not modified in this feature).

## 8. Fixtures

Every test that needs a repository creates it in a temporary directory with a fixed author, date and commit message, so output is stable. No test depends on the state of the prototype repository.

## 9. Risks

| Risk | Mitigation |
|---|---|
| Tool code changes in the prototype while the library copy is frozen | The library is the source after the move; the prototype is changed only by S-007 |
| `git` version differs across hosts, changing output | Tests assert structure (commit lines present), not exact text; the git version is in the fingerprint |
| `graph_query` contract test needs the Context stack running | Skip with recorded reason; a skip without reason fails the gate |
| Behaviour drift during the move | Migration parity test (§6) must pass before the prototype copy is deleted |

## 10. Decisions

1. **Shared helpers.** Recommended: `library/shared/` inside this repository for now, with the orchestrator importing it when it needs it. Revisit if the orchestrator grows its own copy.
2. **Test runner.** Follows the language decision in the roadmap.
3. **Freezing.** The four prototype tools are frozen from the start of T-001. Changes after that go into the library as a new version.
