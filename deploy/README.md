# Running the capabilities registry (development)

The registry runs as Docker Compose services on one Docker host. This is a development deployment: the authorizer trusts a header, so the service listens on the host's loopback only.

| Service | What it does |
|---|---|
| `registry` (`cap-registry`) | The `/v2` REST service and admission. Submitted code is built and tested in sibling sandbox containers |
| `feed` (`cap-feed`) | A development revocation feed. It signs a checkpoint every 5 s. The real feed is Governance's |
| `sandbox` | Builds `taffee-cap-sandbox:dev`, the image tools run in, then exits |
| `smoke` (profile `smoke`) | A one-shot end-to-end check of the running service |

Everything that must survive a restart lives under `TAFFEE_RUN` on the host:

| Path | What |
|---|---|
| `registry/` | The store: `registry.sqlite3`, `code/<hash>/`, and the admission key (`admission.key`, `admission.pub`) |
| `library/` | Only if the library export is on (`TAFFEE_LIBRARY`): a git repository with one commit per admitted tool version. Its files belong to whoever owns the folder it is in, so your own git on the host can read and push it (the registry runs as root in its container) |
| `feed/` | The dev feed's key, entries and checkpoint |
| `work/` | Scratch space for test cases |

## Start, check and stop (on the Docker host)

```sh
export PATH=/Applications/Docker.app/Contents/Resources/bin:$PATH   # Docker Desktop on macOS, over SSH only
export TAFFEE_RUN=$HOME/taffee-run/capabilities
cd ~/projects/taffee-capabilities
docker compose -f deploy/compose.yaml up -d --build                 # start (the built-in tools are imported)
docker compose -f deploy/compose.yaml --profile smoke run --rm smoke  # 12 end-to-end checks
docker compose -f deploy/compose.yaml logs -f registry
docker compose -f deploy/compose.yaml down                           # stop; the data in TAFFEE_RUN stays
```

Then the stability checks: restart, `docker stop`, a `kill -9` in the middle of 8 admissions, a feed outage, manual approval, a burst of 20 submissions, `down`/`up`, and an audit of the store (and of the library, if the export is on) after each. They kill, restart and recreate the services, so run them when no one else is using the deployment. They leave the services running with auto approval:

```sh
sh deploy/stability.sh          # about 5 minutes; one PASS line per check, stops at the first FAIL
```

Settings, passed as environment variables to `compose`:

| Variable | Values | Default |
|---|---|---|
| `TAFFEE_APPROVAL` | `auto`, `manual` | `auto` |
| `TAFFEE_WORKERS` | number of workers | `4` |
| `TAFFEE_PORT` | port on the host | `8090` |
| `TAFFEE_LIBRARY` | a folder under `TAFFEE_RUN`, e.g. `$TAFFEE_RUN/library`: turns on the git library export | off |
| `TAFFEE_LIBRARY_REMOTE` | a git URL to push every library commit to (your own server) | none: commits stay local |
| `TAFFEE_LIBRARY_SSH_KEY`, `TAFFEE_LIBRARY_KNOWN_HOSTS` | files under `TAFFEE_RUN` for an SSH remote; the host key is always checked | none |

Under `manual` approval, the approver is `approver:dev`.

## From your laptop: REST through an SSH tunnel

```sh
ssh -N -L 8090:localhost:8090 <docker-host> &
curl -H "X-Dev-Principal: agent:me" "http://127.0.0.1:8090/v2/search?q=count+words"
```

| Endpoint | What it does |
|---|---|
| `GET /v2/tools` | List the tools, a page at a time |
| `GET /v2/tools/{name}` | Describe one tool |
| `GET /v2/search?q=` | Search by what a tool does |
| `POST /v2/resolve` | Resolve a tool to verified facts |
| `POST /v2/submissions` | Submit a tool |
| `GET /v2/submissions/{id}` | Check a submission's state |
| `POST /v2/submissions/{id}/approve` and `/reject` | Decide a submission (`manual` approval only) |
| `GET /v2/checkpoint` | The signed checkpoint |

Every request needs an `X-Dev-Principal` header; without one the answer is 401. The bundle format is in [03_DESIGN.md §2.3](../docs/design/shared/S-018_registry_store_admission/03_DESIGN.md), and [smoke.py](smoke.py) has a complete example.

## From your IDE or Claude Code: MCP over SSH

The MCP server speaks stdio and answers through the registry's REST API, so it never opens the store. Register this command as an MCP server:

```sh
ssh <docker-host> docker exec -i cap-registry \
  python -m capability.registry_cli mcp \
  --url=http://127.0.0.1:8090 --principal=agent:ide
```

With Docker Desktop on macOS, `docker` is not on the PATH of an SSH session: use `/Applications/Docker.app/Contents/Resources/bin/docker`.

Only the registry may open its store. On Docker Desktop for macOS, file locks do not work on the bind-mounted `TAFFEE_RUN`, so a second process that opens `registry.sqlite3` read-write removes the running registry's write-ahead log when it closes, and every request fails until a restart. Checks that read the store directly (`registry_cli verify`, `deploy/client.py audit`) open it read-only.

For Claude Code, put `claude mcp add taffee-registry --` in front of it.

The server offers `search_capabilities`, `describe_capability` and `list_capabilities`. It is read-only: nothing can be submitted or run through MCP.

## Revoking a tool (dev feed)

```sh
docker exec cap-feed python -m capability.registry_cli dev-feed revoke \
  --dir=$TAFFEE_RUN/feed --name=<tool> --version=<x.y.z> --reason=incident
```

The registry sees the revocation within about 5 s. From then on, `resolve` answers 410 and search no longer offers the tool.

## If the registry reports `RevocationStale`

The registry fails closed whenever it cannot verify the feed: the feed is down, too old, or holds an entry the client refuses. To start the dev feed over, run `down`, delete `$TAFFEE_RUN/feed`, then run `up`. The registry reads the new feed key at start-up.
