# S-011 verification record

Status: implemented and tested (library layer: policy, enforcement contract, process sandbox, container sandbox). **Accepted in review (2026-10-19), code and record, after corrections.** T-005 manifest integration and the platform gates stay open. Full suite at acceptance, with the Docker socket mounted: 662 tests OK, none skipped; the sandbox module runs 84 tests. New files: `capability/sandbox.py`, `tests/test_sandbox.py`.

Written for: the reviewer who will check this, and the engineers who will wire the sandbox to a manifest declaration.

## Two ways the tests are run, and what each proves

- **Default suite** (the test image, no Docker): the sandbox module runs 84 tests of which 20 (the container tests) are skipped with the reason `TAFFEE_SANDBOX_IMAGE is not set`. This run does **not** show that the container limits are enforced.
- **With Docker** (the test image, the Docker socket and a host-visible temp directory; the Dockerfile is `tests/harness/Dockerfile.test`): `tests/test_sandbox.py` 74 tests, none skipped, and no `taffee-sbx-` container left on the host afterwards. **This** run is the evidence for enforcement. The harness container has the Docker socket; the sandboxed code never does (a test checks the socket is absent in the sandbox).

## Result by requirement

| Requirement | Tests |
|---|---|
| CAP-SBX-1 policy | every field and range refused or accepted at the edges; immutable |
| CAP-SBX-2 and 3 enforcement contract, no silent fallback | required sets per policy; the process sandbox refuses a policy that needs filesystem or network, naming each unmet limit, and a marker file proves nothing ran; an explicit `unrestricted` / `allowed` policy runs; the guard runs before a subclass is asked to launch; malformed calls are refused before starting |
| CAP-SBX-4 time | process: a process group with a child is killed and nothing is left; container: a timeout kills the container and none is left; a deadline shorter than container start-up (8 attempts) leaves none |
| CAP-SBX-5 output and stdin | stdout and stderr bounded, overrun kills and is flagged, in both sandboxes; stdin over the limit refused before the run; a program that ignores stdin does not hang |
| CAP-SBX-6 memory | container: a 400 MB allocation under a 64 MB limit is killed (return code 137, not a timeout); process: address-space limit stops a 600 MB allocation under 64 MB, and a program inside the limit runs |
| CAP-SBX-7 filesystem | `read-root` reads and refuses a write ("Read-only file system", file not created); `write-root` writes; `none` hides the root; the container's own filesystem is read-only with a writable private `/tmp`; a read-only implementation mount cannot be changed |
| CAP-SBX-8 network | `none` leaves only loopback and an outbound connection is refused; `allowed` has an interface |
| CAP-SBX-9 hardening | uid and gid 65534, no effective capabilities, no-new-privileges set, no Docker socket, a fork bomb stopped by the process limit; the exact `docker run` command is tested for every flag, for the mounts per scope, and that no privileged, host-network, host-pid, extra-capability, device or Docker-socket argument ever appears |
| CAP-SBX-10 environment | only the allow-listed names reach the program (a host secret does not), in both sandboxes; values never appear in the `docker run` command |
| CAP-SBX-11 plain data | the result carries no host path |
| CAP-SBX-12 skip with a reason | the container tests are skipped with the reason when Docker or the image is missing |

## Found while building

1. My forbidden-flag test matched `--pid` as a substring and tripped on the legitimate `--pids-limit`. It now compares whole arguments.
2. A real race. When the deadline fired before Docker had finished creating the container, `docker kill` found nothing to kill, the sandbox killed the Docker client, and the daemon then finished creating a container nobody would start or remove. It showed as an intermittent "container left behind" failure and as containers in state `created`. A regression test written to force the timing passed once when run alone, so that pass proved nothing; run inside the full class it failed in every pass (4 of 4). The fix is that `kill` retries `docker kill` until it works or the client exits, and `run` waits until the container is really gone. After the fix the container tests passed 4 passes in a row with no leftover; the module passed again with Docker (59 tests).
3. My first theory for the race did not reproduce when I tried it alone. I found the cause only after instrumenting every test to report any leftover container.

## Corrections after review

1. **Arbitrary host mounts.** `run` took a free `ro_mounts` map of host path to container path, so a caller could mount a secret path or the Docker socket. `ro_mounts` is gone. `run` now takes one `implementation` folder, mounted read-only at the fixed target `/impl`, and no other mount or target can be named. Every mounted path (the root and the implementation) is checked before Docker is called: absolute, a real directory with no link on the way, not a system tree or the Docker socket (checked on the resolved path, and refused even when configured as allowed), and under `allowed_mount_roots` given to `ContainerSandbox`. With no allowed roots nothing is mounted, so only `filesystem="none"` policies run.
2. **Cleanup that could not confirm.** `_remove` gave up silently after its deadline, so `run` could return a clean result while a container remained. It now returns whether the container is confirmed gone; if not, `run` raises `SandboxCleanupFailed` naming the container and returns no result. A listing command that itself fails is not a confirmation. This holds on the error path too.

Tests (15 new, 59 to 74): in `ContainerArgvTest`, fourteen arbitrary host paths refused as the implementation, the Docker socket refused whether named as the root, the implementation or an allowed root, system trees refused even when `/` is configured as allowed, a folder outside the allowed roots refused for the root too, no allowed roots meaning nothing mountable while `filesystem="none"` still builds, a link as the last component or a parent refused, relative, missing, file-valued and comma-named paths refused, and the caller unable to choose a target or pass `ro_mounts`; in `ContainerRunTest`, a secret host path or the Docker socket refused before any container starts; and `CleanupFailureTest` (7 tests, no Docker needed, a fake `docker` script): confirmed gone returns the result, a container that stays listed raises with no result and names the container, removal and listing were each retried, a failing listing is not a confirmation, a container that disappears during the wait is a success, and a failure while running still reports an unconfirmed cleanup.

Measured proofs, old behaviour restored in scratch copies on the Linux test host: the allowed-roots check removed, 4 fail; the never-mounted trees check removed, 8 fail; cleanup returning success when unconfirmed, 4 fail. With Docker the module passes all 74 and no container is left. (`/var/run` is a link to `/run` on this host, so it is also refused for passing through a link; the tests accept either reason.)

For the first version of the module, measured proofs (old behaviour restored in scratch copies): the guard removed, 7 fail; no address-space limit, 1 fails; killing only the pid not the group, 1 fails; inheriting the environment, 2 fail; stdin not bounded, 2 fail; and for the command builder, `--network none` dropped, 1 fails, and `--cap-drop ALL` dropped, 4 fail. Removing the output bound (the run never ends within the 200 s limit) did not produce a summary, which is a hang, not a clean failure.

## Corrections after review

1. **Caller-chosen variables reached the Docker client's own environment.** `ContainerSandbox` built the client's environment from the host `PATH` and `DOCKER_HOST` and then added the caller's `env_values` to it, passing only the names with `-e NAME`. A policy that allowed `DOCKER_HOST` could redirect the client to another daemon, where `_checked` means nothing; `PATH` could change which `docker` ran; `DOCKER_CONTEXT`, `DOCKER_CONFIG`, `DOCKER_TLS_VERIFY`, `DOCKER_CERT_PATH` and `LD_PRELOAD` could change the endpoint, configuration or code of the client. A reserved-name list would have been incomplete (`LD_PRELOAD`, `HOME` and others also matter), so the control plane is now separated structurally: the client's environment is only the operator's `PATH` and `DOCKER_HOST` from the host; the program's variables go to the container through a private env file (`--env-file`, directory mode 0700, file mode 0600, removed in a `finally` after the run). Nothing the caller chooses reaches the client's environment or the command line. The documented contract is unchanged: a program may still be given its own `PATH` or `DOCKER_HOST`, which are then variables of the container only. A value containing a newline, carriage return or NUL is refused before Docker is called, because it would corrupt the env file.
2. **An option-like image changed the run flags.** The image sits among the options of `docker run`, so `--privileged`, `--network=host` or `-v/:/host` as an "image" would have been parsed as options. The image must now match an image-reference pattern: it starts with a letter or digit and contains only letters, digits and `. _ / : @ + -`, up to 255 characters.

Tests (10 new, 74 to 84; the old `-e` test replaced by an env-file test): `ControlPlaneIsolationTest` uses a fake `docker` that records its own environment and the env file it received (no Docker needed): `DOCKER_HOST` and `PATH` overrides do not reach the client (the client sees the operator's values); `LD_PRELOAD`, `DOCKER_CONFIG` and `HOME`, and `DOCKER_CONTEXT`, `DOCKER_TLS_VERIFY`, `DOCKER_CERT_PATH` and `DOCKER_API_VERSION`, do not reach the client; the program still receives its variables, in the env file; the env file is mode 0600 while it exists and the directory is gone after the run; newline, carriage return and NUL values are refused before Docker runs; with no variables there is no env file. In `ContainerArgvTest`: eleven option-like or malformed images refused, and ordinary references (tagged, registry with port, digest) accepted.

Measured proofs, old behaviour restored in scratch copies on the Linux test host: caller variables added to the client's environment again, 7 fail (the `DOCKER_HOST` and `PATH` test, the hijacking-names test, and the five other client variables); the image pattern relaxed to any non-space string, the option-like image test fails for eleven images; the newline/NUL check removed, the refusal test fails for all three values.

## Not covered

- Reading the policy from a manifest, declaring limits on the four tools, and running a tool's implementation through the sandbox end to end (T-005). They change the tool schema and the tools' contracts, so they wait until the current snapshot is reviewed.
- Memory limits in the process sandbox are an address-space limit and are approximate.
- Rootless and other container runtimes, user namespaces, seccomp profiles beyond Docker's default, and gVisor-style isolation.
