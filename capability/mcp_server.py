"""S-018: a read-only MCP server over the registry (03_DESIGN §2.8; REG-MCP-1 to 3).

JSON-RPC 2.0, one message per line on stdio. Three tools, all discovery: search_capabilities,
describe_capability and list_capabilities. Answers come from the running registry's REST API (RestRegistry),
with the same visibility rule, for the principal fixed when the server starts. There is no way to submit,
approve, resolve to code or run anything through this server.

The server never opens the store itself. A second process on the store's SQLite file is not safe where file
locks do not work across processes (Docker Desktop bind mounts on macOS): when it closes, it believes it is the
last user and removes the WAL under the running registry.

Run: python -m capability.registry_cli mcp --url http://127.0.0.1:8090 --principal ID
"""

from __future__ import annotations

import json
import logging
import sys
import urllib.error
import urllib.request
from urllib.parse import quote, urlencode, urlsplit

from .registry import RegistryError

LOG = logging.getLogger("capability.mcp_server")
PROTOCOL_VERSION = "2025-06-18"
SERVER_INFO = {"name": "taffee-capabilities-registry", "version": "0.1.0"}
MAX_LINE = 64 * 1024

TOOLS = [
    {
        "name": "search_capabilities",
        "title": "Search the capability registry",
        "description": "Find tools by what they do. Every word must match a tool's name, summary or parameter "
                       "descriptions (prefix match). Returns model-ready tool definitions.",
        "inputSchema": {"type": "object", "additionalProperties": False, "required": ["query"], "properties": {
            "query": {"type": "string", "description": "Words describing the tool you need.", "maxLength": 200},
            "limit": {"type": "integer", "minimum": 1, "maximum": 50, "description": "How many results (default 10)."},
            "min_label": {"type": "string", "enum": ["Declared", "Validated", "Tested"],
                          "description": "The lowest trust label to include."},
            "cursor": {"type": "string", "description": "The 'next' value from a previous result."}}},
        "annotations": {"readOnlyHint": True, "openWorldHint": False},
    },
    {
        "name": "describe_capability",
        "title": "Describe one capability",
        "description": "The full description of one tool version: label, evidence, input schema and limits.",
        "inputSchema": {"type": "object", "additionalProperties": False, "required": ["name"], "properties": {
            "name": {"type": "string", "description": "The tool's name, as returned by search.", "maxLength": 64},
            "version": {"type": "string", "description": "An exact version; the newest when omitted.", "maxLength": 32}}},
        "annotations": {"readOnlyHint": True, "openWorldHint": False},
    },
    {
        "name": "list_capabilities",
        "title": "List the capability registry",
        "description": "Every tool version, in name order, one page at a time.",
        "inputSchema": {"type": "object", "additionalProperties": False, "properties": {
            "limit": {"type": "integer", "minimum": 1, "maximum": 200, "description": "Page size (default 50)."},
            "min_label": {"type": "string", "enum": ["Declared", "Validated", "Tested"],
                          "description": "The lowest trust label to include."},
            "cursor": {"type": "string", "description": "The 'next' value from a previous page."}}},
        "annotations": {"readOnlyHint": True, "openWorldHint": False},
    },
]
TOOL_KEYS = {tool["name"]: set(tool["inputSchema"]["properties"]) for tool in TOOLS}
REQUIRED = {tool["name"]: set(tool["inputSchema"].get("required", [])) for tool in TOOLS}


MAX_REPLY_BYTES = 4 * 1024 * 1024
DEV_HEADER = "X-Dev-Principal"


class RestRegistry:
    """The query calls MCPServer makes (search, describe, list), answered by the registry's /v2 REST API for one
    principal. The service applies visibility for that principal, so the `visible` argument is not used here.
    A REST error becomes the same RegistryError the store-backed registry raises."""

    def __init__(self, url, principal, timeout=30):
        parts = urlsplit(url)
        if parts.scheme not in ("http", "https") or not parts.netloc or parts.query or parts.fragment:
            raise ValueError("the registry URL must be http(s)://host[:port]")
        self._base, self._principal, self._timeout = url.rstrip("/"), str(principal.id), timeout

    def _get(self, path, query):
        params = urlencode({k: v for k, v in query.items() if v is not None})
        request = urllib.request.Request(self._base + path + ("?" + params if params else ""),
                                         headers={DEV_HEADER: self._principal})
        try:
            with urllib.request.urlopen(request, timeout=self._timeout) as response:
                return json.loads(response.read(MAX_REPLY_BYTES))
        except urllib.error.HTTPError as error:
            try:
                body = json.loads(error.read(MAX_REPLY_BYTES))
            except ValueError:
                body = {}
            raise RegistryError(body.get("code", "Unexpected"), body.get("message", f"HTTP {error.code}")) from None
        except (urllib.error.URLError, OSError, ValueError) as exc:
            raise RegistryError("Unexpected", f"the registry did not answer: {type(exc).__name__}") from None

    def search(self, query, kind, min_label, language, limit, cursor, visible=None):
        return self._get("/v2/search", {"q": query, "min_label": min_label, "language": language,
                                        "limit": limit, "cursor": cursor})

    def describe(self, name, version, kind, visible=None):
        return self._get(f"/v2/tools/{quote(name, safe='')}", {"version": version})

    def list(self, kind, min_label, cursor, limit, visible=None):
        return self._get("/v2/tools", {"min_label": min_label, "cursor": cursor, "limit": limit})


class MCPServer:
    def __init__(self, registry, authorizer, principal):
        if principal is None:
            raise ValueError("an MCP server runs for one identified principal")
        self._registry, self._authz, self._principal = registry, authorizer, principal

    def _visible(self, ref):
        return bool(self._authz.may_see(self._principal, ref))

    def handle(self, message) -> dict | None:
        """One JSON-RPC message in, one response out (None for a notification)."""
        if not isinstance(message, dict) or message.get("jsonrpc") != "2.0" or not isinstance(message.get("method"), str):
            return _error(message.get("id") if isinstance(message, dict) else None, -32600, "invalid request")
        mid, method, params = message.get("id"), message["method"], message.get("params") or {}
        if "id" not in message:
            return None  # notifications (notifications/initialized, cancelled) need no answer
        if not isinstance(params, dict):
            return _error(mid, -32602, "params must be an object")
        if method == "initialize":
            return _result(mid, {"protocolVersion": PROTOCOL_VERSION, "serverInfo": SERVER_INFO,
                                 "capabilities": {"tools": {"listChanged": False}},
                                 "instructions": "Read-only discovery over the Taffee capability registry. "
                                                 "Nothing here runs a tool."})
        if method == "ping":
            return _result(mid, {})
        if method == "tools/list":
            return _result(mid, {"tools": TOOLS})
        if method == "tools/call":
            return self._call(mid, params)
        return _error(mid, -32601, "method not found")

    def _call(self, mid, params):
        name, args = params.get("name"), params.get("arguments") or {}
        if name not in TOOL_KEYS:
            return _error(mid, -32602, "unknown tool")
        if not isinstance(args, dict) or set(args) - TOOL_KEYS[name] or REQUIRED[name] - set(args):
            return _error(mid, -32602, "invalid arguments")
        try:
            if name == "search_capabilities":
                data = self._registry.search(args["query"], "tool", args.get("min_label", "Declared"), None,
                                             args.get("limit"), args.get("cursor"), self._visible)
            elif name == "describe_capability":
                data = self._registry.describe(args["name"], args.get("version"), "tool", self._visible)
            else:
                data = self._registry.list("tool", args.get("min_label", "Declared"), args.get("cursor"),
                                           args.get("limit"), self._visible)
        except RegistryError as exc:
            code = exc.code if exc.code in ("NotFound", "BelowLabel", "InvalidRequest", "IntegrityError") else "Unexpected"
            payload = {"code": code}
            return _result(mid, {"content": [{"type": "text", "text": json.dumps(payload)}],
                                 "structuredContent": payload, "isError": True})
        except Exception:
            LOG.exception("mcp tool failed")
            payload = {"code": "Unexpected"}
            return _result(mid, {"content": [{"type": "text", "text": json.dumps(payload)}],
                                 "structuredContent": payload, "isError": True})
        return _result(mid, {"content": [{"type": "text", "text": json.dumps(data, sort_keys=True)}],
                             "structuredContent": data, "isError": False})

    def serve(self, stdin=None, stdout=None):
        stdin, stdout = stdin or sys.stdin, stdout or sys.stdout
        for line in stdin:
            if len(line) > MAX_LINE:
                reply = _error(None, -32600, "message too large")
            else:
                try:
                    message = json.loads(line)
                except ValueError:
                    reply = _error(None, -32700, "parse error")
                else:
                    reply = self.handle(message)
            if reply is not None:
                stdout.write(json.dumps(reply) + "\n")
                stdout.flush()


def _result(mid, result):
    return {"jsonrpc": "2.0", "id": mid, "result": result}


def _error(mid, code, message):
    return {"jsonrpc": "2.0", "id": mid, "error": {"code": code, "message": message}}
