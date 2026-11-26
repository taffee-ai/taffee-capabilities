"""S-018: the /v2 registry service: discovery, resolve facts, and admission (03_DESIGN §2.7).

DEVELOPMENT REFERENCE SERVICE, like S-008: standard-library HTTP, no TLS, and no authentication of its own.
The injected `AdmissionAuthorizer` decides who is asking, what they may see, and who may submit or approve;
there is no default. Responses are built from allow-lists (REG-API-3); errors are closed (REG-API-4); bodies and
queries are bounded before they are parsed (REG-API-5). The S-008 /v1 service is not changed (R-D12).

Submissions are processed by one background worker, so `POST /v2/submissions` answers 202 at once and the
submitter polls `GET /v2/submissions/{id}`.
"""

from __future__ import annotations

import json
import logging
import re
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, unquote, urlsplit

from .admission import OPEN_STATES, AdmissionError
from .admission_queue import AdmissionQueue
from .authz import AdmissionAuthorizer
from .canon import canonical_bytes
from .registry import RegistryError
from .rows import sign_checkpoint

LOG = logging.getLogger("capability.registry_api")
MAX_BODY = 4096
MAX_QUERY = 2048
NAME = re.compile(r"[a-z][a-z0-9-]{1,63}")
SID = re.compile(r"sub-[0-9a-f]{20}")
MESSAGES = {
    "NotFound": "no matching capability or submission",
    "Ambiguous": "the name is used by more than one kind; pass kind",
    "BelowLabel": "the capability exists but is below the required label",
    "InvalidRange": "the version range is not supported",
    "InvalidRequest": "the request is not valid for this endpoint",
    "TooLarge": "the request is larger than this service accepts",
    "Unauthenticated": "the caller is not identified",
    "Forbidden": "the caller may not do this",
    "InvalidState": "the submission is not in a state that allows this",
    "Revoked": "the capability has been withdrawn; use a newer version or another capability",
    "RevocationStale": "revocation knowledge is not fresh; retry after the feed recovers",
    "VersionConflict": "that version is admitted with different content; bump the version",
    "IntegrityError": "the registry failed an integrity check; report to the owner, do not retry",
    "NoImplementation": "no implementation in the requested languages",
    "MethodNotAllowed": "method not allowed",
    "AuthorizerUnavailable": "the authorization decision could not be made; the request is refused",
    "Busy": "admission is at capacity; retry after the Retry-After interval",
    "Unexpected": "the service could not complete the request",
}
STATUS = {"NotFound": 404, "Ambiguous": 409, "BelowLabel": 409, "InvalidRange": 400, "InvalidRequest": 400,
          "TooLarge": 413, "Unauthenticated": 401, "Forbidden": 403, "InvalidState": 409, "Revoked": 410,
          "RevocationStale": 503, "VersionConflict": 409, "IntegrityError": 500, "NoImplementation": 404,
          "MethodNotAllowed": 405, "AuthorizerUnavailable": 503, "Busy": 429, "Unexpected": 500}
SUBMISSION_VIEW = ("id", "state", "name", "version", "identity", "submitted_by", "submitted_at", "problems",
                   "decided_by", "decided_at", "row_seq", "evidence", "duplicate")
RESOLVE_VIEW = ("name", "kind", "version", "label", "manifest_digest", "identity", "language", "code_digest",
                "revocation", "revocation_seq")


class _Reply(Exception):
    def __init__(self, code: str, status: int | None = None):
        self.code, self.status = code, status or STATUS[code]


class RegistryService:
    def __init__(self, registry, admission, authorizer, private_key=None, *, queue: AdmissionQueue | None = None,
                 max_bundle_bytes=None, process_inline=False):
        """`queue` runs admission in the background (a default AdmissionQueue is started when none is given);
        `process_inline` processes each submission inside its request instead (tests only)."""
        if not isinstance(authorizer, AdmissionAuthorizer):
            raise ValueError("an AdmissionAuthorizer is required; there is no default")
        self._registry, self._admission, self._authz, self._key = registry, admission, authorizer, private_key
        self.max_bundle_bytes = max_bundle_bytes or admission.limits.max_bundle_bytes
        self._inline = process_inline
        self.queue = None if process_inline else (queue or AdmissionQueue(admission).start())

    def drain(self, timeout=None):
        """Wait until every queued submission has been processed (tests and orderly shutdown)."""
        return True if self.queue is None else self.queue.drain(timeout)

    def stop(self):
        if self.queue is not None:
            self.queue.stop()

    # -- the transport-free core --
    def handle(self, method: str, target: str, headers: dict, body: bytes = b"") -> tuple[int, dict, bytes]:
        try:
            status, payload = self._dispatch(method, target, {k.lower(): v for k, v in headers.items()}, body)
        except _Reply as reply:
            status, payload = reply.status, {"code": reply.code, "message": MESSAGES[reply.code]}
        except (RegistryError, AdmissionError) as exc:
            code = exc.code if exc.code in STATUS else "Unexpected"
            if code == "Unexpected":
                LOG.error("unexpected %s code %r", type(exc).__name__, exc.code)
            status, payload = STATUS[code], {"code": code, "message": MESSAGES[code]}
        except Exception:
            LOG.exception("unexpected failure")
            status, payload = 500, {"code": "Unexpected", "message": MESSAGES["Unexpected"]}
        out = {"Content-Type": "application/json", "Cache-Control": "no-store", "X-Content-Type-Options": "nosniff"}
        if status == 503:
            out["Retry-After"] = "30"
        elif status == 429:
            out["Retry-After"] = "10"
        return status, out, canonical_bytes(payload)

    def _principal(self, headers):
        try:
            principal = self._authz.principal(headers)
        except Exception:
            raise _Reply("AuthorizerUnavailable")
        if principal is None:
            raise _Reply("Unauthenticated")
        return principal

    def _visible(self, principal):
        def visible(ref):
            try:
                return bool(self._authz.may_see(principal, ref))
            except Exception:
                raise _Reply("AuthorizerUnavailable")
        return visible

    def _allowed(self, check, principal):
        try:
            return bool(check(principal))
        except Exception:
            raise _Reply("AuthorizerUnavailable")

    @staticmethod
    def _query(parts, allowed):
        if len(parts.query) > MAX_QUERY:
            raise _Reply("TooLarge")
        try:
            raw = parse_qs(parts.query, keep_blank_values=True, strict_parsing=bool(parts.query), max_num_fields=10)
        except ValueError:
            raise _Reply("InvalidRequest")
        if set(raw) - set(allowed) or any(len(values) != 1 for values in raw.values()):
            raise _Reply("InvalidRequest")
        return {key: values[0] for key, values in raw.items()}

    @staticmethod
    def _int(value, default=None):
        if value is None:
            return default
        if not value.isdigit() or len(value) > 6:
            raise _Reply("InvalidRequest")
        return int(value)

    @staticmethod
    def _json(body):
        try:
            value = json.loads(body) if body else None
        except (ValueError, UnicodeDecodeError):
            raise _Reply("InvalidRequest")
        if not isinstance(value, dict):
            raise _Reply("InvalidRequest")
        return value

    def _dispatch(self, method, target, headers, body):
        parts = urlsplit(target)
        segments = [unquote(item) for item in parts.path.split("/")[1:]]
        if segments[:1] != ["v2"]:
            raise _Reply("NotFound")
        path = segments[1:]
        principal = self._principal(headers)
        visible = self._visible(principal)
        reg = self._registry

        if path == ["tools"] and method == "GET":
            q = self._query(parts, ("cursor", "limit", "min_label"))
            return 200, reg.list("tool", q.get("min_label", "Declared"), q.get("cursor"), self._int(q.get("limit")), visible)
        if path == ["search"] and method == "GET":
            q = self._query(parts, ("q", "cursor", "limit", "min_label", "language"))
            if "q" not in q:
                raise _Reply("InvalidRequest")
            return 200, reg.search(q["q"], "tool", q.get("min_label", "Declared"), q.get("language"),
                                   self._int(q.get("limit")), q.get("cursor"), visible)
        if len(path) in (2, 3) and path[0] == "tools" and method == "GET":
            if not NAME.fullmatch(path[1]):
                raise _Reply("NotFound")
            if len(path) == 3:
                if path[2] != "versions":
                    raise _Reply("NotFound")
                self._query(parts, ())
                return 200, {"items": reg.versions(path[1], "tool", visible)}
            q = self._query(parts, ("version",))
            return 200, reg.describe(path[1], q.get("version"), "tool", visible)
        if path == ["resolve"] and method == "POST":
            req = self._json(body)
            if set(req) - {"name", "range", "min_label", "languages"} or not isinstance(req.get("name"), str):
                raise _Reply("InvalidRequest")
            languages = req.get("languages", ["python"])
            if not isinstance(languages, list) or not all(isinstance(x, str) for x in languages) or len(languages) > 8:
                raise _Reply("InvalidRequest")
            match = reg.resolve(req["name"], req.get("range", "*"), "tool", req.get("min_label", "Declared"),
                                languages, visible)
            return 200, {key: match[key] for key in RESOLVE_VIEW}
        if path == ["checkpoint"] and method == "GET":
            if self._key is None:
                raise _Reply("NotFound")
            seq, head = reg.store.head()
            return 200, sign_checkpoint(self._key, seq, head)
        if path == ["submissions"] and method == "POST":
            if not self._allowed(self._authz.may_submit, principal):
                raise _Reply("Forbidden")
            if self._inline:
                record = self._admission.submit(body, principal.id)
                record = self._admission.process(record["id"])
                return 202, {key: record.get(key) for key in SUBMISSION_VIEW}
            retry = self._admission.find_retry(body, principal.id)
            if retry is not None:  # a retry is always answered, whatever the bounds (idempotent submit)
                return 200, {key: retry.get(key) for key in SUBMISSION_VIEW}
            self.queue.reserve(principal.id)  # Busy (429) before anything is stored
            try:
                record = self._admission.submit(body, principal.id)
            except BaseException:
                self.queue.release(principal.id)
                raise
            if record.get("duplicate") and record["state"] not in OPEN_STATES:
                self.queue.release(principal.id)  # a retry of finished work: answer it, run nothing
                return 200, {key: record.get(key) for key in SUBMISSION_VIEW}
            self.queue.enqueue(principal.id, record["id"])
            return (200 if record.get("duplicate") else 202), {key: record.get(key) for key in SUBMISSION_VIEW}
        if len(path) in (2, 3) and path[0] == "submissions":
            if not SID.fullmatch(path[1]):
                raise _Reply("NotFound")
            approver = self._allowed(self._authz.may_approve, principal)
            if len(path) == 2 and method == "GET":
                record = self._admission.get(path[1])
                if record["submitted_by"] != principal.id and not approver:
                    raise _Reply("NotFound")  # someone else's submission answers like an absent one
                return 200, {key: record.get(key) for key in SUBMISSION_VIEW}
            if len(path) == 3 and path[2] in ("approve", "reject") and method == "POST":
                if not approver:
                    raise _Reply("Forbidden")
                req = self._json(body) if body else {}
                if set(req) - {"reason"} or not isinstance(req.get("reason", ""), str):
                    raise _Reply("InvalidRequest")
                if path[2] == "approve":
                    record = self._admission.approve(path[1], principal.id)
                else:
                    record = self._admission.reject(path[1], principal.id, req.get("reason"))
                return 200, {key: record.get(key) for key in SUBMISSION_VIEW}
        if method not in ("GET", "POST"):
            raise _Reply("MethodNotAllowed")
        raise _Reply("NotFound")


class _Handler(BaseHTTPRequestHandler):
    service: RegistryService

    def _serve(self):
        body = b""
        if self.command == "POST":
            length = self.headers.get("Content-Length", "0")
            limit = self.service.max_bundle_bytes if urlsplit(self.path).path == "/v2/submissions" else MAX_BODY
            if not length.isdigit():
                return self._error("InvalidRequest")
            if int(length) > limit:
                return self._error("TooLarge")
            body = self.rfile.read(int(length))
        status, headers, data = self.service.handle(self.command, self.path, dict(self.headers.items()), body)
        self._send(status, headers, data)

    def _error(self, code):
        headers = {"Content-Type": "application/json", "Cache-Control": "no-store", "X-Content-Type-Options": "nosniff",
                   "Connection": "close"}
        self.close_connection = True
        self._send(STATUS[code], headers, canonical_bytes({"code": code, "message": MESSAGES[code]}))

    def _send(self, status, headers, data):
        self.send_response(status)
        for key, value in headers.items():
            self.send_header(key, value)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    do_GET = do_POST = do_PUT = do_DELETE = do_PATCH = _serve

    def log_message(self, *args):
        pass


def make_server(service: RegistryService, host: str = "127.0.0.1", port: int = 0) -> ThreadingHTTPServer:
    handler = type("Handler", (_Handler,), {"service": service})
    LOG.warning("capability registry service /v2: DEVELOPMENT REFERENCE, not a production deployment")
    server = _Server((host, port), handler)
    server.daemon_threads = True
    return server


class _Server(ThreadingHTTPServer):
    # The standard library default backlog is 5: twenty agents connecting at once had connections reset
    # (found by test_admission_concurrency). The listen backlog is a deployment setting; this is the dev value.
    request_queue_size = 128


__all__ = ["RegistryService", "make_server", "MESSAGES", "STATUS"]
