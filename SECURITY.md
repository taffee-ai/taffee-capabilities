# Security policy

## Reporting a vulnerability

Please report vulnerabilities privately, through GitHub: on this repository's **Security** tab, choose **Report a vulnerability**. Do not open a public issue, pull request or discussion for a vulnerability.

A useful report says:

- which version or commit you tested (for example `v0.1.0`);
- the component (see the scope below) and the request, manifest or bundle involved;
- the steps to reproduce, and what happened compared with what you expected;
- what an attacker gains: for example code running outside the sandbox, a tool admitted or run that should have been refused, or a forged or replayed answer accepted.

We will confirm that we received the report, keep you informed while we work on it, and agree with you when the details are published. We will credit you in the advisory unless you ask us not to.

## Supported versions

| Version | Supported |
|---|---|
| 0.1.x | Yes |
| Earlier commits | No |

## Scope

In scope: everything in this repository, in particular

- **The sandbox** (`capability/sandbox.py`): a tool reading or writing outside its declared filesystem scope, reaching the network, outliving its time limit, reaching the Docker socket, or leaving a container behind.
- **Admission** (`capability/admission.py`): a submitted tool admitted although it fails its checks, code admitted other than the code that was tested, or a bundle that affects the registry host.
- **Integrity** (`capability/rows.py`, `capability/digest.py`, `capability/store_registry.py`): a changed row, manifest or code area that verification still accepts, or a resolve that returns code whose digest does not match.
- **Revocation** (`capability/revocation.py`): a revoked tool that still resolves or runs, or an unverified, stale or rolled-back feed that is accepted.
- **The call service** (`POST /v2/call`, `capability/tool_call.py`, `capability/call_replies.py`): a workspace outside the workspace root, one caller seeing another caller's reply, or a tool run twice for one `call_id`.
- **The REST and MCP surfaces** (`capability/registry_api.py`, `capability/mcp_server.py`): anything a caller can see or do that the authorizer did not allow, or content from a tool echoed into an error.
- **Call records** (`capability/callrecord.py`): content stored where only references belong, or a record changed without verification noticing.

## Behaviour that is intentional

These are documented properties of the current deployment, not vulnerabilities. Reports that show they are worse than documented are welcome.

- **Caller identity.** The development authorizer takes the caller's identity from the `X-Dev-Principal` header and does not check it, so the service listens on `127.0.0.1` only. Authorization is a pluggable interface (`capability/authz.py`); deployments that open the port put it behind their own authentication.
- **Docker access.** The registry is given the host's Docker socket to start sandbox containers. Sandbox containers never get it.
- **Shared kernel.** Sandbox containers share the host's kernel. A deployment that needs a stronger boundary runs the registry host as a virtual machine.
- **Development revocation feed.** The compose deployment signs its own feed from a local file.

See the [deployment notes](README.md#deployment-notes) and [deploy/README.md](deploy/README.md) for details.
