"""S-018: the /v2 registry service: discovery, resolve facts, and admission (03_DESIGN §2.7).

DEVELOPMENT REFERENCE SERVICE, like S-008: standard-library HTTP, no TLS, and no authentication of its own.
The injected `AdmissionAuthorizer` decides who is asking, what they may see, and who may submit or approve;
there is no default. Responses are built from allow-lists (REG-API-3); errors are closed (REG-API-4); bodies and
queries are bounded before they are parsed (REG-API-5). The S-008 /v1 service is not changed (R-D12).

Submissions are processed by one background worker, so `POST /v2/submissions` answers 202 at once and the
submitter polls `GET /v2/submissions/{id}`.

S-019: `POST /v2/call` runs a tool for the caller in the container sandbox and answers with its result. It is on
only when the service is given a `ToolCaller` (see `tool_call.py`).
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
from .calltime import CallError
from .canon import canonical_bytes
from .registry import RegistryError
from .rows import sign_checkpoint
from .sandbox import SandboxUnavailable
from .call_replies import request_digest
from .tool_call import CallRefused, CallTracker

LOG = logging.getLogger("capability.registry_api")
MAX_BODY = 4096
MAX_CALL_BODY = 1024 * 1024 + 8192  # the largest stdinBytes a tool may declare, plus the request around the input
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
    "Busy": "the service is at capacity; retry after the Retry-After interval",
    "ValidationFailed": "the input does not match the tool's declared schema",
    "PostconditionFailed": "the tool's output failed its declared postconditions",
    "SandboxUnavailable": "the tool declares limits this service cannot enforce, so it is not run",
    "WorkspaceRequired": "the tool reads or writes its root; name a workspace",
    "WorkspaceNotFound": "no such workspace",
    "WorkspaceUnavailable": "this service has no workspace root configured",
    "CallsDisabled": "this service does not run tools",
    "CallIdConflict": "this call_id was already used for a different request",
    "InProgress": "a call with this call_id is still running; retry after the Retry-After interval",
    "Unexpected": "the service could not complete the request",
}
STATUS = {"NotFound": 404, "Ambiguous": 409, "BelowLabel": 409, "InvalidRange": 400, "InvalidRequest": 400,
          "TooLarge": 413, "Unauthenticated": 401, "Forbidden": 403, "InvalidState": 409, "Revoked": 410,
          "RevocationStale": 503, "VersionConflict": 409, "IntegrityError": 500, "NoImplementation": 404,
          "MethodNotAllowed": 405, "AuthorizerUnavailable": 503, "Busy": 429, "ValidationFailed": 422,
          "PostconditionFailed": 502, "SandboxUnavailable": 409, "WorkspaceRequired": 400,
          "WorkspaceNotFound": 404, "WorkspaceUnavailable": 409, "CallsDisabled": 404, "CallIdConflict": 409,
          "InProgress": 409, "Unexpected": 500}
SUBMISSION_VIEW = ("id", "state", "name", "version", "identity", "submitted_by", "submitted_at", "problems",
                   "decided_by", "decided_at", "row_seq", "evidence", "duplicate")
RESOLVE_VIEW = ("name", "kind", "version", "label", "manifest_digest", "identity", "language", "code_digest",
                "revocation", "revocation_seq")


class _Reply(Exception):
    def __init__(self, code: str, status: int | None = None, extra: dict | None = None):
        self.code, self.status, self.extra = code, status or STATUS[code], extra or {}


def error_reply(exc: BaseException) -> tuple[int, dict]:
    """The closed (status, body) for an exception raised while answering a request."""
    if isinstance(exc, _Reply):
        return exc.status, {"code": exc.code, "message": MESSAGES[exc.code], **exc.extra}
    if isinstance(exc, CallRefused):
        return STATUS[exc.code], {"code": exc.code, "message": MESSAGES[exc.code]}
    if isinstance(exc, CallError):  # problems name fields and checks only; they never carry a value
        return STATUS[exc.code], {"code": exc.code, "message": MESSAGES[exc.code],
                                  "problems": [{"field": p.field, "message": p.message} for p in exc.problems]}
    if isinstance(exc, SandboxUnavailable):
        return STATUS[exc.code], {"code": exc.code, "message": MESSAGES[exc.code], "unmet": list(exc.unmet)}
    if isinstance(exc, (RegistryError, AdmissionError)):
        code = exc.code if exc.code in STATUS else "Unexpected"
        if code == "Unexpected":
            LOG.error("unexpected %s code %r", type(exc).__name__, exc.code)
        return STATUS[code], {"code": code, "message": MESSAGES[code]}
    LOG.error("unexpected failure", exc_info=exc)
    return 500, {"code": "Unexpected", "message": MESSAGES["Unexpected"]}


class RegistryService:
    def __init__(self, registry, admission, authorizer, private_key=None, *, queue: AdmissionQueue | None = None,
                 max_bundle_bytes=None, process_inline=False, caller=None):
        """`queue` runs admission in the background (a default AdmissionQueue is started when none is given);
        `process_inline` processes each submission inside its request instead (tests only). `caller`, a
        `tool_call.ToolCaller`, turns on `POST /v2/call`; without it the route answers CallsDisabled."""
        if not isinstance(authorizer, AdmissionAuthorizer):
            raise ValueError("an AdmissionAuthorizer is required; there is no default")
        self._registry, self._admission, self._authz, self._key = registry, admission, authorizer, private_key
        self.max_bundle_bytes = max_bundle_bytes or admission.limits.max_bundle_bytes
        self._inline = process_inline
        self._caller = caller
        self.queue = None if process_inline else (queue or AdmissionQueue(admission).start())

    def drain(self, timeout=None):
        """Wait until every queued submission has been processed (tests and orderly shutdown)."""
        return True if self.queue is None else self.queue.drain(timeout)

    def stop(self):
        if self.queue is not None:
            self.queue.stop()

    # -- the transport-free core --
    def handle(self, method: str, target: str, headers: dict, body: bytes = b"") -> tuple[int, dict, bytes]:
        extra_headers = {}
        try:
            result = self._dispatch(method, target, {k.lower(): v for k, v in headers.items()}, body)
            status, payload = result[0], result[1]
            if len(result) == 3:
                extra_headers = result[2]
        except Exception as exc:
            status, payload = error_reply(exc)
        out = {"Content-Type": "application/json", "Cache-Control": "no-store", "X-Content-Type-Options": "nosniff"}
        if status == 503:
            out["Retry-After"] = "30"
        elif status == 429:
            out["Retry-After"] = "10"
        elif payload.get("code") == "InProgress":
            out["Retry-After"] = "5"
        out.update(extra_headers)
        return status, out, canonical_bytes(payload)

    def _call(self, body, principal):
        """POST /v2/call. With a reply store, call_id is idempotent per caller: the first call that runs the tool
        takes it, a retry of the same request gets the stored reply (held until the first call finishes, if it is
        still running), a different request is CallIdConflict, and a call refused before the tool ran gives the
        call_id back."""
        caller = self._caller
        request = self._json(body)
        if caller.replies is None:
            return 200, caller.call(request, self._callable(principal))
        req = caller.parse(request)
        claim = caller.replies.begin(principal.id, req["call_id"], request_digest(req), hold_s=caller.replies.hold_s)
        if claim[0] == "replay":
            return claim[1], claim[2], {"X-Call-Replayed": "true"}
        if claim[0] == "conflict":
            raise _Reply("CallIdConflict")
        if claim[0] == "in_progress":
            raise _Reply("InProgress")
        tracker = CallTracker()
        try:
            status, payload = 200, caller.call(request, self._callable(principal), tracker)
        except Exception as exc:
            status, payload = error_reply(exc)
        if tracker.ran:
            caller.replies.finish(principal.id, req["call_id"], status, payload)
        else:
            caller.replies.release(principal.id, req["call_id"])
        return status, payload

    def _principal(self, headers):
        try:
            principal = self._authz.principal(headers)
        except Exception:
            raise _Reply("AuthorizerUnavailable")
        if principal is None:
            raise _Reply("Unauthenticated")
        return principal

    def _callable(self, principal):
        def may_call(ref):
            try:
                return bool(self._authz.may_call(principal, ref))
            except Exception:
                raise _Reply("AuthorizerUnavailable")
        return may_call

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
        if path == ["call"] and method == "POST":
            if self._caller is None:
                raise _Reply("CallsDisabled")
            return self._call(body, principal)
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
            route = urlsplit(self.path).path
            limit = (self.service.max_bundle_bytes if route == "/v2/submissions"
                     else MAX_CALL_BODY if route == "/v2/call" else MAX_BODY)
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
