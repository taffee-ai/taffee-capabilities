# T-005 contract note: sandboxed execution of tools

> **Superseded as a status document (2026-10-20, after review).** This note is kept as the original proposal and the record of the decision sequence. It was agreed in review and Stage A is **implemented**; the final bounds differ from this note's example: `spec.sandbox.outputBytes` is **64 to 65536** (not 1 to 1048576), `stdinBytes` is **64 to 1048576** (not 0 to 1048576), and `timeoutMs` is 100 to 600000. The reasons are in the 'Independent second review' and 'review' sections of [VERIFICATION.md](VERIFICATION.md), which is the current record. Read the sections below as the proposal, and VERIFICATION.md for what exists.

Status: the original proposal, 2026-10-19, kept as a record. Stage A is implemented; see the note above.

Written for: the reviewer and the engineers who will declare limits on a tool.

## 1. Goal and exit gate

Roadmap T-005: a tool declared as sandboxed runs with the limits in its declaration, and a test shows that a disallowed file or network access is refused. Depends on S-011 (the sandbox interface, accepted).

Today there is no code path that runs a tool under `Sandbox.run`. The conformance runner starts a tool as a plain subprocess (`RUNNERS` in `capability/conformance.py`), and `SandboxPolicy` is never read from a manifest (S-011 SPEC: "reading the policy from a manifest declaration is not done").

## 2. The declaration

An optional block in the tool manifest, `spec.sandbox`:

```yaml
spec:
  timeoutMs: 5000          # existing field: becomes SandboxPolicy.timeout_s (timeoutMs / 1000)
  sandbox:
    memoryMb: 256          # 16 to 8192, default 256
    outputBytes: 65536     # 1 to 1048576, default 65536
    stdinBytes: 65536      # 0 to 1048576, default 65536
    filesystem: read-root  # none | read-root | write-root
    network: none          # none only, in this stage
    env: []                # names the tool may be given (default none)
```

Rules, all fail closed:

- `filesystem: unrestricted` and `network: allowed` (the explicit opt-outs of `SandboxPolicy`) **cannot be declared in a manifest** in this stage. An opt-out is a deployment and admission decision (govsec), not something a tool grants itself.
- A tool with **no** `spec.sandbox` block is **not run** by the new executor: it raises the existing `SandboxUnavailable` refusal (reusing the closed S-012 code `SandboxUnavailable`, so the accepted S-012 table does not change). There is no unrestricted default.
- `required(policy)` must be a subset of what the chosen sandbox enforces, as S-011 already checks; otherwise `SandboxUnavailable`.
- The container image is deployment configuration of the `ContainerSandbox`, not a manifest field.

## 3. The executor

New module `capability/execute.py`:

- `policy_from_spec(spec) -> SandboxPolicy` (pure; the mapping above).
- `run_tool(registry_match, root, input, *, sandbox, call_recorder=None)`: S-009 `validate_input` first; builds the argv from the language registry (the runner's argv with the entry at `/impl/<entry>`, using the existing `RUNNERS`); calls `sandbox.run(argv, root=root, policy=policy, stdin=json({root:"/work", input}), implementation=<implementation folder>)`; parses the one JSON result line strictly (the T-007 rules: one line, boolean `ok`, bounded string `error`); applies S-009 `validate_output`; returns the result. Records an S-012 call record when a recorder is passed, using only the closed outcome table: input failure `input_rejected`/`ValidationFailed`, postcondition failure `output_rejected`/`PostconditionFailed`, a result the tool reports as `ok:false` `tool_failed`, `SandboxUnavailable` `refused`/`SandboxUnavailable`, and anything unexpected `error`/`Unexpected`.
- The tool's `root` inside the container is `/work`; the host root path is never put in the request.
- A non-zero exit, a timeout or output over the limit (S-011 reports these) is `tool_failed` with no tool text copied.

## 4. What moves, exactly (digests, versions, files)

Per the review's correction, I do not assume a code digest moves unless its bytes change.

**Stage A, this feature (proposed to run first):**

| What | Changes? |
|---|---|
| `schemas/manifest.tool.schema.json` (adds the optional block) | **Yes.** `schemas/` is an indexed input folder, so `inputs_digest` changes, hence `index_digest`, hence `tests/golden/index.json` and the umbrella catalog's `inputs_digest` and `index_digest` are regenerated. The catalog's entries do not change. |
| `capability/manifest.py` (validates the block) | No digest effect (code outside the indexed folders). |
| `capability/execute.py`, `tests/test_execute.py`, fixture tools under `tests/fixtures/` | New files; none indexed. |
| The four built-in tools' manifests, versions, identities, code digests, `versions.yaml` entries | **No change.** |
| Evidence records of the four tools | **No change**; they stay valid because the evidence fingerprint binds the tool identity, the cases digest, the suite digest, the policy and the environment, none of which move. I will run `capability.evidence check` to prove it. |
| Labels | No change (Tested stays Tested). |
| `tests/test_tools_t001.py` | Unchanged. |

**Stage B, a separate step needing review and maintainer approval (not proposed to start now):** declaring `spec.sandbox` on the four built-in tools so they run sandboxed by default.

| What | Changes? |
|---|---|
| The four manifests | Yes: each adds a `spec.sandbox` block, so each **manifest digest** changes, hence each **tool identity** changes. |
| Versions | Four bumps, because an identity change needs a new version (D-S1-03): git-log 1.3.0, grep 1.3.0, unawaited-async 1.3.0, glob 1.4.0. |
| Code digests | **Unchanged** (the `main.py` bytes do not change). |
| `versions.yaml` | Four new entries, old entries stay. |
| Evidence | Regenerated for the four new identities with `capability.evidence write`; Tested returns when the check passes. |
| Index, golden index, catalog | Regenerated again. |
| `TOOL_VERSIONS` in `tests/fixture_support.py` and any test pinning the versions or the index digest | Updated. |

## 5. Tests

On the Linux test host, in Docker. Docker-dependent tests run with the socket mounted (as the S-011 container tests) and skip without it, with the reason recorded.

1. Schema and validator: the block is accepted with every field, rejected with unknown fields, out-of-range numbers, `unrestricted`, `allowed`, a non-list `env`; a manifest without the block is still valid (Declared, as today).
2. `policy_from_spec`: every default, every field mapped, `timeoutMs` to seconds, `required()` of the result.
3. No silent fallback: a tool with no declaration raises `SandboxUnavailable`; a sandbox that cannot enforce a declared limit raises `SandboxUnavailable`; neither starts any process (a recording double proves it).
4. Executor with a recording double sandbox: argv, `/impl` and `/work`, stdin shape (no host path), `validate_input` before any call, `validate_output` after, S-012 outcome mapping for each outcome (using a real recorder on a temp log).
5. **The exit-gate tests, real `ContainerSandbox`:** fixture tools that (a) read a host canary file outside the declared root, (b) write to a read-only root, (c) open a network connection to a listener on the host, (d) exceed memory, (e) exceed the timeout, (f) print more than `outputBytes`. Under the declared policy (a), (b) and (c) are refused (the canary is not visible, the write fails, the connection fails), (d) to (f) end as `tool_failed` with the container confirmed gone; a control tool that does the permitted thing (reads a file inside the root) succeeds. A second control run of the same fixture under an `unrestricted`/`allowed` policy, done only inside the test with the opt-outs given explicitly to `Sandbox.run`, shows the canary read and the connection succeed, so the refusals are not artefacts of the fixture.
6. The built-in tools are not touched in Stage A, so a test asserts that their manifests have no `spec.sandbox` and that `run_tool` refuses them with `SandboxUnavailable` (documenting that Stage B is what lifts that).
7. Measured proofs for each behaviour, as before.

## 6. What I will write once the questions are answered

`capability/execute.py`, the manifest schema block and its validation in `capability/manifest.py`, the tests above, regenerated golden index and catalog digests (inputs and index only), a T-005 verification record, and the roadmap row. Nothing in Context, govsec or the prototype.

## 7. Limits stated up front

- Stage A makes sandboxed execution possible and tested; it does not make the four built-in tools sandboxed by default (that is Stage B).
- A tool's declared policy is only as strong as the sandbox: `ProcessSandbox` cannot confine files or the network, so a manifest declaring `filesystem: read-root` needs the container sandbox, otherwise `SandboxUnavailable`.
- The Docker tests are not part of a default run without the socket (as for S-011).
- No dispatcher exists yet (S-008); `run_tool` is a library function a dispatcher will call.

## 8. Open questions for review

1. Stage A first (schema, executor, fixtures; no tool manifest, version or evidence moves) and Stage B (declaring the policy on the four tools, four version bumps and regenerated evidence) as a separate step after you and the owner approve it?
2. A tool without a declaration is refused with the existing `SandboxUnavailable` code (no new S-012 code): acceptable, or do you want a distinct code (which would change the accepted S-012 closed table)?
3. The manifest cannot declare `unrestricted` or `allowed`: acceptable for this stage?
4. `timeoutMs` stays the single source of the time limit (no second timeout field): acceptable?
5. The default for an omitted field (256 MB, 64 KiB output, 64 KiB stdin, no env) and `filesystem` having no default (it must be stated): acceptable?
