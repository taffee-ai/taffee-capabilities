# Taffee capabilities

The capabilities pillar of Taffee answers one question for the rest of the platform: **what can be called, what is it known to do, and on what evidence?**

It publishes a catalog of tools, agents and models. Every entry has a manifest, a label computed from committed evidence, and a way to resolve it to something executable. It does not decide who may use an entry (that is governance), and it does not run agents (that is the orchestrator).

## Deployment notes

The registry runs inside your own network. Things to know when you install it:

- **Callers are identified by a request header.** The development authorizer takes the caller's identity from `X-Dev-Principal` and does not check it. The port is published on `127.0.0.1` only; reach it from other machines through an SSH tunnel. To open it to your network, put it behind your own authentication first. Authorization is a pluggable interface (`capability/authz.py`), and the governance pillar will supply the real implementation.
- **The registry container uses the host's Docker.** It starts the sandbox containers that build and test submitted tools, so it is given the Docker socket. Give it a host of its own, or one where that access is acceptable.
- **Submitted tools run in containers.** Each build and test runs with no network, limited memory, processes and time, and only its own files. Containers share the host's kernel; if your policy calls for a stronger boundary, run the registry's host as a virtual machine.
- **The revocation feed is a development stand-in.** It is a local file the deployment signs itself.
- **Tested on Linux and on Docker Desktop for macOS.** Not yet on Docker Desktop for Windows. On Docker Desktop for macOS, file locks do not work on the registry's data folder, so only the registry may open its store (see [deploy/README.md](deploy/README.md)).

What was tested, what was found and what is not yet verified: [07_VERIFICATION.md](docs/design/shared/S-018_registry_store_admission/07_VERIFICATION.md).

## What is here

- **Manifests and validators.** JSON schemas for tool, agent and model manifests, content digests for both the declaration and the code, and a validator that rebuilds the signed index.
- **Labels from evidence.** A label (Declared, Validated, then Tested, Trusted or Approved) is a pure function of the manifest, the evidence and a versioned label policy. Labels are never set by hand.
- **Revocation.** A signed, hash-chained feed. If the feed cannot be verified, the registry refuses to answer.
- **The registry service (S-018).** A pluggable store (SQLite with FTS5, or in-memory) with signed rows. An admission pipeline builds and tests agent-written tools in a container sandbox, with automatic or manual approval. It serves a `/v2` REST API and a read-only MCP server.
- **Built-in tools.** `grep`, `glob`, `git-log` and `unawaited-async`, each with conformance cases and committed evidence.
- **`capctl verify`.** One command that re-checks a capability version against the catalog from local files, without running its code.

## Layout

| Path | Contents |
|---|---|
| `capability/` | The library, the registry service and the CLIs |
| `schemas/` | Manifest and index schemas |
| `policy/` | The label policy and supported languages |
| `catalog/` | Manifests of the catalog entries |
| `library/` | Source and conformance cases of the built-in tools |
| `evidence/` | Committed evidence records |
| `drafts/` | Capabilities still being specified |
| `deploy/` | Docker Compose deployment, smoke and stability checks |
| `tests/` | The test suite |
| `docs/` | Design documents and the roadmap |

## Getting started

You need Python 3.12 and git. Node 22 is needed for TypeScript tools.

```sh
python3.12 -m venv .venv && . .venv/bin/activate
pip install -e .
python -m unittest discover -s tests -p 'test_*.py'
```

This runs everything except the container tests, which are skipped and say why. They run tools in the real container sandbox and need Docker and the test images. Run the whole suite, container tests included, like this:

```sh
docker build -f tests/harness/Dockerfile.test --target base    -t taffee-cap-base:dev .
docker build -f tests/harness/Dockerfile.test --target harness -t taffee-cap-harness:dev .
T=$HOME/work/sbxtmp-cap && mkdir -p $T
docker run --rm \
  -e TAFFEE_SANDBOX_IMAGE=taffee-cap-base:dev -e TAFFEE_SANDBOX_TMP=$T \
  -v /var/run/docker.sock:/var/run/docker.sock \
  -v $T:$T -v $PWD:$PWD -w $PWD -e PYTHONPATH=$PWD \
  taffee-cap-harness:dev python -m unittest discover -s tests
```

`$T` must have the same absolute path on the host and in the container. On Docker Desktop for macOS, the socket is `~/.docker/run/docker.sock`.

To run the registry, see [deploy/README.md](deploy/README.md). It covers starting the services, the REST endpoints, connecting an IDE through MCP, and revoking a tool.

## Documentation

- [Design](docs/design/CAPABILITIES-DESIGN.md): how the pillar fits together
- [Requirements](docs/design/CAPABILITY-REQUIREMENTS.md)
- [Usage](docs/design/USAGE-DESIGN.md): how callers discover, resolve and invoke capabilities
- [Operations](docs/design/OPERATIONS.md)
- [Roadmap](docs/roadmap.md): every feature, its exit gate and its status

## Licence

Apache License 2.0. See [LICENSE](LICENSE) and [NOTICE](NOTICE). The Taffee name, logos and the TaffeeAI.com domain are not covered by that licence.
