"""graph-query: read a repository code graph through the Context service (T-001, preparatory slice).

PREPARATORY. It lives in drafts/, outside the folders the index digests (catalog, schemas, policy, library, evidence), so it changes no digest, index or catalog. When Context publishes `graph_query` and the credential boundary exists it moves to library/graph-query/python (an identity change, version 1.1.0). It is not declared by any manifest yet (catalog/tool/graph-query is still 1.0.0, Declared,
implementations: []). Context publishes no `graph_query` tool, and no credential boundary exists, so the only
production client here is `UnavailableClient`: every call returns the closed error ContextUnavailable. The core
`run` takes the client as an argument so tests can inject a double. Nothing in the tool's input, environment or
repository root can select another client.

Contract (T-001): reads {"root": "<absolute directory>", "input": {...}} as JSON on stdin and writes exactly one
JSON line to stdout: {"ok": true, "output": "..."} or {"ok": false, "error": "<closed code>[: property names]"}.
It exits 0 for every result. Python standard library only. The error never carries text from Context and never
carries a caller-supplied value.
"""

import json
import re
import sys

OPERATIONS = ("resolve", "find_symbol", "get_callers", "get_callees", "get_dependencies")
PROPERTIES = ("repoId", "commitSHA", "operation", "symbol", "limit")
REQUIRED = ("repoId", "operation")
LIMIT_MIN, LIMIT_MAX = 1, 20
MAX_REPLY_BYTES = 64 * 1024
# Context's own checks (taffee-context types.ts requiredText / commitSHA, neo4j-query.ts validateQuery), which the
# JSON schema does not state. Lengths are UTF-16 code units, as in Context, so nothing is accepted that Context rejects.
MAX_REPO_ID = 256
MAX_SYMBOL = 1024
MAX_COMMIT = 64
COMMIT_PATTERN = re.compile(r"(?:[a-f0-9]{40}|[a-f0-9]{64})")
# What JavaScript's String.prototype.trim() removes (ECMAScript WhiteSpace and LineTerminator). Python's str.strip()
# differs (it keeps U+FEFF and removes U+0085), so blankness is judged with this exact set to match Context's `!value.trim()`.
JS_WHITESPACE = frozenset("\t\n\v\f\r \u00a0\u1680\u2028\u2029\u202f\u205f\u3000\ufeff") | frozenset(chr(c) for c in range(0x2000, 0x200B))
ERROR_CODES = (
    "InvalidInput",
    "ContextUnavailable",
    "ContextUnauthorized",
    "RepoNotEnabled",
    "ContextBadReply",
    "OutputTooLarge",
)


class ToolError(Exception):
    """A tool-level failure. `code` is one of ERROR_CODES; `detail` is fixed text built here, never from a caller."""

    def __init__(self, code, detail=""):
        super().__init__(code)
        assert code in ERROR_CODES
        self.code = code
        self.detail = detail

    def message(self):
        return self.code + (": " + self.detail if self.detail else "")


class ContextCallError(Exception):
    """Raised by a ContextClient. Only `code` is used; the exception text is never copied anywhere."""

    def __init__(self, code):
        super().__init__(code)
        self.code = code


class ContextClient:
    """The only thing the wrapper knows about Context: call one tool and get its text reply."""

    def call_tool(self, name, arguments):
        # No credential parameter: how a brokered credential reaches a client is undefined until the orchestrator
        # and govsec transport contract exists. It must never come from the tool's stdin.
        raise NotImplementedError


class UnavailableClient(ContextClient):
    """The production client until Context publishes `graph_query` and a credential boundary exists."""

    def call_tool(self, name, arguments):
        raise ContextCallError("ContextUnavailable")


def _text_problem(value, name, maximum):
    """Mirror of Context's requiredText: non-blank text, at most `maximum` UTF-16 units, no control characters."""
    if not isinstance(value, str):
        return name + " must be a string"
    if all(c in JS_WHITESPACE for c in value) or len(value.encode("utf-16-le", "surrogatepass")) // 2 > maximum \
            or any(ord(c) < 0x20 for c in value):
        return name + " must be nonempty text of at most %d characters without control characters" % maximum
    return None


def validate_input(data):
    """Check `data` against the declared schema and Context's own checks. Names properties, never values."""
    if not isinstance(data, dict):
        raise ToolError("InvalidInput", "input must be an object")
    problems = []
    if any(key not in PROPERTIES for key in data):
        problems.append("unknown property")
    for name in REQUIRED:
        if name not in data:
            problems.append("missing " + name)
    for name in ("repoId", "commitSHA", "symbol"):
        if name in data and not isinstance(data[name], str):
            problems.append(name + " must be a string")
    if "operation" in data and data["operation"] not in OPERATIONS:
        problems.append("operation must be one of the declared operations")
    if "limit" in data:
        limit = data["limit"]
        # JSON Schema counts 2.0 as an integer, so the wrapper does too; it passes on the integer.
        whole = isinstance(limit, float) and limit.is_integer()
        if isinstance(limit, bool) or not (isinstance(limit, int) or whole) or not LIMIT_MIN <= limit <= LIMIT_MAX:
            problems.append("limit must be an integer from %d to %d" % (LIMIT_MIN, LIMIT_MAX))
    if not problems:  # Context's semantic checks, only once the shape is right
        problem = _text_problem(data["repoId"], "repoId", MAX_REPO_ID)
        if problem:
            problems.append(problem)
        if "commitSHA" in data:
            problem = _text_problem(data["commitSHA"], "commitSHA", MAX_COMMIT)
            if problem:
                problems.append(problem)
            elif COMMIT_PATTERN.fullmatch(data["commitSHA"]) is None:
                problems.append("commitSHA must be a full lowercase Git object id")
        if data["operation"] != "resolve":
            problem = _text_problem(data.get("symbol"), "symbol", MAX_SYMBOL) if "symbol" in data else "symbol is required for this operation"
            if problem:
                problems.append(problem)
    if problems:
        raise ToolError("InvalidInput", "; ".join(dict.fromkeys(problems)))
    clean = {key: data[key] for key in PROPERTIES if key in data}
    if "limit" in clean:
        clean["limit"] = int(clean["limit"])
    return clean


def _reject_constant(_name):
    raise ValueError("non-finite number")


def _reject_duplicates(pairs):
    """A key that appears twice would be read differently by different parsers; refuse the whole reply."""
    keys = [key for key, _ in pairs]
    if len(keys) != len(set(keys)):
        raise ValueError("duplicate key")
    return dict(pairs)


def check_reply(text):
    """Validate Context's reply: one JSON object with a `results` array and a boolean `truncated`, within bounds."""
    if not isinstance(text, str):
        raise ToolError("ContextBadReply")
    if len(text.encode("utf-8", "surrogatepass")) > MAX_REPLY_BYTES:
        raise ToolError("OutputTooLarge")
    try:
        parsed = json.loads(text, parse_constant=_reject_constant, object_pairs_hook=_reject_duplicates)
    except (ValueError, RecursionError):  # RecursionError: nesting deeper than the parser allows
        raise ToolError("ContextBadReply") from None
    if not isinstance(parsed, dict) or not isinstance(parsed.get("results"), list) \
            or not isinstance(parsed.get("truncated", False), bool):
        raise ToolError("ContextBadReply")
    return parsed


def run(request, client):
    """Core of the tool. Returns the output text, or raises ToolError with a closed code."""
    data = request.get("input") if isinstance(request, dict) else None
    arguments = validate_input(data)
    try:
        reply = client.call_tool("graph_query", arguments)
    except ContextCallError as exc:
        raise ToolError(exc.code if exc.code in ERROR_CODES else "ContextBadReply") from None
    except Exception:  # a client fault never leaks its text
        raise ToolError("ContextUnavailable") from None
    parsed = check_reply(reply)
    try:
        output = json.dumps(parsed, separators=(",", ":"), ensure_ascii=False)
        encoded = output.encode("utf-8")  # a lone surrogate in the reply cannot be encoded: it is a bad reply
    except (UnicodeEncodeError, RecursionError):
        raise ToolError("ContextBadReply") from None
    if len(encoded) > MAX_REPLY_BYTES:
        raise ToolError("OutputTooLarge")
    return output


def read_request():
    try:
        request = json.loads(sys.stdin.buffer.read().decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        raise ToolError("InvalidInput", "stdin is not valid JSON") from None
    if not isinstance(request, dict):
        raise ToolError("InvalidInput", "request must be an object")
    return request


def main():
    try:
        result = {"ok": True, "output": run(read_request(), UnavailableClient())}
    except ToolError as exc:
        result = {"ok": False, "error": exc.message()}
    except Exception:  # a tool never crashes its caller
        result = {"ok": False, "error": "ContextUnavailable"}
    sys.stdout.buffer.write((json.dumps(result, ensure_ascii=False) + "\n").encode("utf-8"))
    sys.stdout.buffer.flush()


if __name__ == "__main__":
    main()
