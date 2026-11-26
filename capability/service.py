"""S-008: a read-only catalog and resolution service over the registry (USAGE-DESIGN 3.1 and 3.2).

DEVELOPMENT REFERENCE SERVICE, NOT A PRODUCTION DEPLOYMENT. It uses the standard library HTTP server, has no TLS
and authenticates nobody itself: the injected `Authorizer` (authz.py) decides who is asking and what they may see.
There is no default authorizer; a service without one cannot be built.

Rules (CONTRACT-NOTE sections 2 and 6): every response is built from an allow-list of fields, never by filtering a
full record, so nothing executable or path-like is ever returned. Entries the caller may not see are removed
before any other step, so hidden and absent entries answer byte-identically. Each request reads the index
through the registry, which re-verifies it whenever the file changes (S-018 R-D13). Error text is closed; nothing from a record, a path or an exception is copied in.
"""

import hashlib
import json
import logging
import re
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, unquote, urlsplit

from .authz import Authorizer
from .canon import canonical_bytes
from .registry import KINDS, LABEL_ORDER, RegistryError, _version_key
from .resolve import STRICT_SEMVER, in_range

LOG = logging.getLogger("capability.service")
MAX_BODY = 4096
MAX_Q = 200
MAX_QUERY = 1024  # raw query string, checked before it is parsed
NAME = re.compile(r"[a-z][a-z0-9-]{1,63}")
LIST_KEYS = ("name", "kind", "version", "label", "summary", "manifest_digest", "identity", "languages", "requires")
SPEC_KEYS = ("inputSchema", "postconditions", "idempotent", "readOnly", "timeoutMs", "boundary")
# Per kind: the manifest fields a caller may see. `source`, `implementations` and the sandbox block are never in it.
VIEW_FIELDS = {
    "tool": ("name", "kind", "version", "summary", "owner", "requires"),
    "agent": ("name", "kind", "version", "summary", "owner", "requires", "outcomes", "tools", "roles", "limits"),
    "model": ("name", "kind", "version", "summary", "owner", "requires", "provider", "category", "licence", "dataTerms"),
}
STATUS = {"NotFound": 404, "Ambiguous": 409, "BelowLabel": 409, "InvalidRange": 400, "Revoked": 410,
          "RevocationStale": 503, "IntegrityError": 500, "IndexError": 500, "NoImplementation": 404}
MESSAGES = {
    "NotFound": "no matching capability",
    "Ambiguous": "the name is used by more than one kind; pass kind",
    "BelowLabel": "the capability exists but is below the required label",
    "InvalidRange": "the version range is not supported",
    "Revoked": "the capability has been withdrawn; use a newer version or another capability",
    "RevocationStale": "revocation knowledge is not fresh; retry after the feed recovers",
    "IntegrityError": "the registry failed an integrity check; report to the owner, do not retry",
    "IndexError": "the registry index failed verification; report to the owner, do not retry",
    "InvalidRequest": "the request is not valid for this endpoint",
    "Unauthenticated": "the caller is not identified",
    "MethodNotAllowed": "method not allowed",
    "AuthorizerUnavailable": "the visibility decision could not be made; the request is refused",
    "Unexpected": "the service could not complete the request",
}


class _Reply(Exception):
    def __init__(self, status, code, **fields):
        self.status, self.code, self.fields = status, code, fields


def _digest(body: dict) -> str:
    return "sha256:" + hashlib.sha256(canonical_bytes(body)).hexdigest()


def _no_duplicates(pairs):
    keys = [key for key, _ in pairs]
    if len(keys) != len(set(keys)):
        raise ValueError("duplicate key")
    return dict(pairs)


def _no_constant(name):
    raise ValueError("non-finite number")


class CatalogService:
    def __init__(self, registry, authorizer):
        if not isinstance(authorizer, Authorizer):
            raise ValueError("an Authorizer is required; there is no default")
        self._registry, self._authorizer = registry, authorizer

    # -- the transport-free core: (method, target, headers, body) -> (status, headers, bytes) --
    def handle(self, method: str, target: str, headers: dict, body: bytes = b"") -> tuple[int, dict, bytes]:
        try:
            status, payload = 200, self._dispatch(method, target, {k.lower(): v for k, v in headers.items()}, body)
        except _Reply as reply:
            status, payload = reply.status, {"code": reply.code, "message": MESSAGES[reply.code], **reply.fields}
        except RegistryError as exc:
            status, payload = self._registry_error(exc)
        except Exception:  # never let a fault print text from a record or path
            LOG.exception("unexpected failure")
            status, payload = 500, {"code": "Unexpected", "message": MESSAGES["Unexpected"]}
        out = {"Content-Type": "application/json", "Cache-Control": "no-store", "X-Content-Type-Options": "nosniff"}
        if status == 503:
            out["Retry-After"] = "30"
        return status, out, canonical_bytes(payload)

    def _registry_error(self, exc):
        code = exc.code if exc.code in STATUS else "Unexpected"
        payload = {"code": code, "message": MESSAGES[code]}
        if code == "Revoked":
            entry = getattr(exc, "entry", None) or {}
            payload.update({"reason": entry.get("reason"), "seq": getattr(exc, "seq", None)})
        return STATUS.get(code, 500), payload

    # -- routing --
    def _dispatch(self, method, target, headers, body):
        parts = urlsplit(target)
        segments = [unquote(item) for item in parts.path.split("/")[1:]]
        if segments and segments[-1] == "" and len(segments) > 1:
            segments.pop()
        if segments[:1] != ["v1"]:
            raise _Reply(404, "NotFound")
        route = segments[1:]
        allowed = None
        if route == ["capabilities"] or route == ["index"] or (route[:1] == ["capabilities"] and len(route) in (2, 3)):
            allowed = "GET"
        elif route == ["resolve"]:
            allowed = "POST"
        if allowed is None:
            raise _Reply(404, "NotFound")
        principal = self._authorizer.principal(headers)
        if principal is None:
            raise _Reply(401, "Unauthenticated")
        if method != allowed:
            raise _Reply(405, "MethodNotAllowed")
        query = self._query(parts.query)

        def visible(entry):
            try:
                return bool(self._authorizer.may_see(principal, {k: entry[k] for k in ("name", "kind", "version")}))
            except Exception:
                raise _Reply(503, "AuthorizerUnavailable")

        if route == ["capabilities"]:
            return self._list(query, visible)
        if route == ["index"]:
            self._only(query, ())
            return self._index_view(visible)
        if route == ["resolve"]:
            self._only(query, ())
            return self._resolve(body, visible)
        if len(route) == 2:
            return self._versions(route[1], query, visible)
        return self._detail(route[1], route[2], query, visible)

    @staticmethod
    def _query(raw):
        if len(raw) > MAX_QUERY:
            raise _Reply(400, "InvalidRequest")
        try:
            parsed = parse_qs(raw, keep_blank_values=True, strict_parsing=bool(raw), max_num_fields=8)
        except ValueError:
            raise _Reply(400, "InvalidRequest")
        if any(len(values) != 1 for values in parsed.values()):
            raise _Reply(400, "InvalidRequest")
        return {key: values[0] for key, values in parsed.items()}

    @staticmethod
    def _only(query, allowed):
        if set(query) - set(allowed):
            raise _Reply(400, "InvalidRequest")

    def _entries(self, visible):
        index = self._registry.verified_index()
        return index, [entry for entry in index["entries"] if visible(entry)]

    @staticmethod
    def _kind(query):
        kind = query.get("kind")
        if kind is not None and kind not in KINDS:
            raise _Reply(400, "InvalidRequest")
        return kind

    @staticmethod
    def _item(entry):
        manifest = entry["manifest"]
        return {
            "name": entry["name"], "kind": entry["kind"], "version": entry["version"], "label": entry["label"],
            "summary": manifest["summary"], "manifest_digest": entry["manifest_digest"],
            "identity": entry.get("identity"),
            "languages": [item["language"] for item in entry.get("implementations", [])],
            "requires": list(manifest.get("requires", [])),
        }

    def _list(self, query, visible):
        self._only(query, ("kind", "minLabel", "q"))
        kind = self._kind(query)
        label = query.get("minLabel", "Declared")
        text = query.get("q")
        if label not in LABEL_ORDER or (text is not None and len(text) > MAX_Q):
            raise _Reply(400, "InvalidRequest")
        floor = LABEL_ORDER.index(label)
        _, entries = self._entries(visible)
        rows = []
        for entry in entries:
            if (kind is not None and entry["kind"] != kind) or LABEL_ORDER.index(entry["label"]) < floor:
                continue
            if text is not None:
                hay = " ".join(str(value) for value in (entry["name"], entry["manifest"]["summary"],
                                                        entry["manifest"].get("when_to_use", ""))).lower()
                if text.lower() not in hay:
                    continue
            rows.append(self._item(entry))
        return {"entries": rows}

    def _versions(self, name, query, visible):
        self._only(query, ("kind",))
        kind = self._kind(query)
        if NAME.fullmatch(name) is None:
            raise _Reply(404, "NotFound")
        _, entries = self._entries(visible)
        rows = [entry for entry in entries if entry["name"] == name and (kind is None or entry["kind"] == kind)]
        if not rows:
            raise _Reply(404, "NotFound")
        if len({entry["kind"] for entry in rows}) > 1:
            raise _Reply(409, "Ambiguous")
        rows.sort(key=lambda entry: _version_key(entry["version"]), reverse=True)
        return {"entries": [self._item(entry) for entry in rows]}

    def _detail(self, name, version, query, visible):
        self._only(query, ("kind",))
        kind = self._kind(query)
        if NAME.fullmatch(name) is None or STRICT_SEMVER.fullmatch(version) is None:
            raise _Reply(404, "NotFound")
        _, entries = self._entries(visible)
        rows = [entry for entry in entries if entry["name"] == name and entry["version"] == version
                and (kind is None or entry["kind"] == kind)]
        if not rows:
            raise _Reply(404, "NotFound")
        if len({entry["kind"] for entry in rows}) > 1:
            raise _Reply(409, "Ambiguous")
        entry = rows[0]
        item = self._item(entry)
        item["evidence"] = list(entry.get("evidence", []))
        item["manifest_view"] = self._manifest_view(entry)
        return item

    @staticmethod
    def _manifest_view(entry):
        manifest = entry["manifest"]
        view = {key: manifest[key] for key in VIEW_FIELDS[entry["kind"]] if key in manifest}
        if entry["kind"] == "tool":
            view["spec"] = {key: manifest["spec"][key] for key in SPEC_KEYS if key in manifest["spec"]}
        view["projection_of"] = entry["manifest_digest"]  # a reference; the projection does not hash to it
        return view

    def _index_view(self, visible):
        index, entries = self._entries(visible)
        view = {
            "schema": "capability-index-view/1",
            "source": {"index_digest": index["index_digest"], "inputs_digest": index["inputs_digest"],
                       "policy_version": index["policy_version"]},
            "entries": [self._item(entry) for entry in entries],
        }
        view["view_digest"] = _digest(view)
        return view

    def _resolve(self, body, visible):
        if len(body) > MAX_BODY:
            raise _Reply(400, "InvalidRequest")
        try:
            request = json.loads(body.decode("utf-8"), object_pairs_hook=_no_duplicates, parse_constant=_no_constant)
        except (ValueError, RecursionError):
            raise _Reply(400, "InvalidRequest")
        if not isinstance(request, dict) or set(request) - {"name", "kind", "range", "minLabel"} or "name" not in request:
            raise _Reply(400, "InvalidRequest")
        name, kind = request["name"], request.get("kind")
        rng, label = request.get("range", "*"), request.get("minLabel", "Declared")
        if not (isinstance(name, str) and NAME.fullmatch(name) and isinstance(rng, str) and len(rng) <= 32
                and label in LABEL_ORDER and (kind is None or kind in KINDS)):
            raise _Reply(400, "InvalidRequest")
        _, entries = self._entries(visible)
        named = [entry for entry in entries if entry["name"] == name and (kind is None or entry["kind"] == kind)]
        languages = sorted({item["language"] for entry in named for item in entry.get("implementations", [])})
        try:
            match = self._registry.resolve(name, rng, kind=kind, minLabel=label, languages=languages, visible=visible)
        except RegistryError as exc:
            if exc.code == "BelowLabel" and "unknown label" not in str(exc):
                raise _Reply(409, "BelowLabel", have=self._have(named, rng), need=label)
            raise
        row = next(entry for entry in named if entry["version"] == match["version"] and entry["kind"] == match["kind"])
        return {"name": match["name"], "kind": match["kind"], "version": match["version"],
                "digest": row["identity"] if match["kind"] == "tool" else row["manifest_digest"],
                "label": match["label"], "revocation": match["revocation"],
                "requires": list(row["manifest"].get("requires", []))}

    @staticmethod
    def _have(named, rng):
        in_range_rows = [entry for entry in named if in_range(entry["version"], rng)]
        best = max(in_range_rows, key=lambda entry: _version_key(entry["version"]))
        return best["label"]


class _Handler(BaseHTTPRequestHandler):
    service: CatalogService

    def _serve(self):
        length = self.headers.get("Content-Length")
        body = b""
        if self.command == "POST":
            if length is None or not length.isdigit() or int(length) > MAX_BODY:
                headers = {"Content-Type": "application/json", "Cache-Control": "no-store",
                           "X-Content-Type-Options": "nosniff"}
                return self._send(400, headers, canonical_bytes(
                    {"code": "InvalidRequest", "message": MESSAGES["InvalidRequest"]}))
            body = self.rfile.read(int(length))
        status, headers, data = self.service.handle(self.command, self.path, dict(self.headers.items()), body)
        self._send(status, headers, data)

    def _send(self, status, headers, data):
        self.send_response(status)
        for key, value in headers.items():
            self.send_header(key, value)
        if status == 405:
            self.send_header("Allow", "GET, POST")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    do_GET = do_POST = do_PUT = do_DELETE = do_PATCH = do_HEAD = do_OPTIONS = _serve

    def log_message(self, *args):  # request lines can hold query text; the service logs nothing per request
        pass


def make_server(registry, authorizer, host: str = "127.0.0.1", port: int = 0) -> ThreadingHTTPServer:
    """A server bound to loopback by default. Development reference only; see the module docstring."""
    service = CatalogService(registry, authorizer)
    handler = type("Handler", (_Handler,), {"service": service})
    LOG.warning("capability catalog service: DEVELOPMENT REFERENCE, not a production deployment")
    if getattr(authorizer, "DEVELOPMENT_ONLY", False):
        LOG.warning("authorizer %s is development-only and performs no authentication", type(authorizer).__name__)
    return ThreadingHTTPServer((host, port), handler)
