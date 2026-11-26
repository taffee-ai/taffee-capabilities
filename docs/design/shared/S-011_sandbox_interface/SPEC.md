# S-011: Sandbox interface

Status: implemented (library layer), tested (see [VERIFICATION.md](VERIFICATION.md)) and accepted in review on 2026-10-19, code and record. T-005 manifest integration and the platform gates stay open. Depends on S-009. Roadmap: [../../../roadmap.md](../../../roadmap.md).

Written for: the dispatcher and admission engineers who run capability code, and the reviewers who check what is actually enforced.

## 1. Purpose

Run capability code under limits that are enforced, or refuse to run it. A policy states time, memory, output size, filesystem scope and network scope. A sandbox states which of those it can enforce. Anything the policy needs that the sandbox cannot enforce is a refusal before anything runs. There is no silent fallback to unrestricted execution.

## 2. Scope of this feature

In scope: the policy, the enforcement contract, a process sandbox (limits that need no privileges), and a container sandbox (the full set, through Docker).

Not in scope, and deliberately not done yet: reading the policy from a tool's manifest, and declaring limits on the four built-in tools (T-005). Both change the tool schema and the tools' contracts, and wait until the current snapshot (T-001, S-009 and the features after it) has been reviewed.

## 3. Requirements

| ID | Requirement |
|---|---|
| CAP-SBX-1 | `SandboxPolicy` fields: `timeout_s` (0.1 to 600), `memory_mb` (16 to 8192), `output_bytes` (1 to 1 MiB), `stdin_bytes` (0 to 1 MiB), `filesystem` (`none`, `read-root`, `write-root`, `unrestricted`), `network` (`none`, `allowed`), `env` (names that are passed through). Anything out of range or unknown is refused at construction. |
| CAP-SBX-2 | A sandbox declares the limits it `enforces` from the set `time`, `memory`, `output`, `stdin`, `env`, `filesystem`, `network`, `processes`. `required(policy)` is the set a policy needs: always `time`, `memory`, `output`, `stdin`, `env`; `filesystem` unless it is `unrestricted`; `network` unless it is `allowed`. |
| CAP-SBX-3 | `Sandbox.run` raises `SandboxUnavailable`, naming the unmet limits, before it starts anything, when `required(policy)` is not a subset of what the sandbox enforces. `unrestricted` and `allowed` are explicit opt-outs, so an unrestricted run is a decision in the policy, never a default or a fallback. |
| CAP-SBX-4 | Time: a wall-clock timeout kills the whole process tree (the whole container for the container sandbox). The result says `timed_out`. Nothing is left running, and for the container sandbox nothing is left behind: when the deadline fires before Docker has finished creating the container, the kill is retried until the container exists, and `run` returns only once the container is confirmed gone. If that cannot be confirmed within `cleanup_wait_s` (the removal keeps being retried and the container listing is the check; a listing that itself fails is not a confirmation), `run` raises `SandboxCleanupFailed` naming the container and returns no result, because a result would say the run was clean while a container remains. (Reviewed: review.) **Container sandbox, amended 2026-11-15 (S-018):** the limit is the program's, counted from when its process starts. The program runs under coreutils `timeout -k 2 <limit>` inside the container (the image must provide `timeout`), and exit status 124 is reported as `timed_out`. Creating and starting the container is the host's work: the outer timer, the backstop that kills the container from outside, fires at the limit plus `startup_allowance_s` (default 60 s, 0–600). Before this, start-up counted against the program's limit, so on a busy Docker host a correct tool could time out before it ran (found by the 20-submitter container burst). A program that ignores SIGTERM is killed 2 s later and its result is exit status 137, not `timed_out`. An optional `owner` labels every container (`org.taffee.sandbox.owner`), and `remove_owned()` removes that owner's containers in any state: a process killed while Docker is creating a container leaves it in `Created` for good, and only its owner's next start can know it is garbage. Not yet re-reviewed. |
| CAP-SBX-5 | Output: stdout and stderr are each bounded by `output_bytes`. An overrun kills the run and the result says `truncated`. Stdin over `stdin_bytes` is refused before the run. |
| CAP-SBX-6 | Memory: enforced by the container's memory limit (swap disabled), and in the process sandbox by an address-space limit, which is approximate and is stated as such. |
| CAP-SBX-7 | Filesystem scope is enforced by the container sandbox only: `none` mounts nothing from the host, `read-root` mounts the root read-only, `write-root` mounts it read-write, and the container's own filesystem is read-only with a small private temporary area. |
| CAP-SBX-8 | Network `none` is enforced by the container sandbox only (no external network: the container has only its loopback interface, and any outbound connection is refused). |
| CAP-SBX-9 | The container runs as an unprivileged user, with every capability dropped, `no-new-privileges`, a process-count limit, no Docker socket, and no host mount other than the root (per the policy) and the read-only implementation folder. This is enforced, not assumed: `run` takes one `implementation` folder and mounts it read-only at the fixed target `/impl`; there is no parameter for another mount or another target. Every mounted host path (the root and the implementation) must be absolute, a real directory with no link on the way, not `/` or one of `/etc`, `/proc`, `/sys`, `/dev`, `/run`, `/var/run`, `/root`, `/boot`, `/var/lib/docker` (checked on the resolved path, so the Docker socket and links into those trees are refused even if a caller configures them as allowed), and under an allowed mount root the sandbox was constructed with. With no allowed roots nothing is mounted, so only `filesystem="none"` policies run. A refused path is a `PolicyError` before Docker is called. (Reviewed: review.) |
| CAP-SBX-10 | Only the environment names in `policy.env` are passed in, with their values from the caller. Nothing else is inherited. In the container sandbox the values travel in a private env file and never enter the Docker client's own environment or command line, so a caller cannot redirect or hijack the Docker control plane. A value with a newline, carriage return or NUL is refused. |
| CAP-SBX-11 | A result is plain data: return code, bytes of stdout and stderr, `timed_out`, `truncated`. It never includes the policy's host paths. |
| CAP-SBX-12 | A test that needs Docker runs only where Docker works, and reports a skip with its reason. A skipped enforcement test is not evidence of enforcement. |

## 4. Test plan

| Test | Assertion |
|---|---|
| Policy | every field and range is validated; unknown values refused |
| Required set | each policy maps to the right required set |
| No silent fallback | the process sandbox refuses a policy that needs filesystem or network enforcement and runs nothing (a marker file is not created); an explicit `unrestricted` / `allowed` policy runs |
| Process sandbox | time kills a process group that has a child; output overrun kills; a stdin overrun is refused; memory limit stops an allocation; the environment is the allow-list; nothing is left running |
| Container argv | every hardening flag is present for every policy, each scope maps to the right mounts and flags, nothing else is mounted, the Docker socket is never mounted |
| Container run (Docker) | the tool runs; a read of the root works under `read-root`; a write to it is refused; `none` hides the root; a write to the container filesystem is refused; network access is refused under `none` and works under `allowed`; the memory limit kills an allocation; a fork bomb is contained; a timeout kills the container and none is left; output is bounded; the process is not root and holds no capabilities |
