"""S-009: call-time validation.

Two pure checks the dispatcher runs around a call: the input against the tool's declared schema,
and the result against its declared postconditions. Nothing here does I/O or runs capability code.

Postcondition patterns are matched with RE2 (google-re2), whose matching time is linear in the line length for
any pattern it accepts. Python's `re` is not used for them: it backtracks, and a pattern such as `(a+)+b` can hang
on a short line. Patterns RE2 cannot compile (look-around, backreferences) are refused when the manifest is
validated.

Errors name a field path or a check and a line number. They never contain a value the caller passed or a
line the tool printed. Property names are reported, because they are structure and the caller needs them
to fix a typo; a value, even one that sits under that name, never is.
"""

import json
from dataclasses import dataclass
from functools import lru_cache

import re2
from jsonschema import Draft202012Validator

MAX_INPUT_BYTES = 64 * 1024
MAX_OUTPUT_BYTES = 64 * 1024
MAX_LINE_BYTES = 4096  # a size bound for memory, not the runtime bound: RE2 is linear in the line length
MAX_PROBLEMS = 20


@dataclass(frozen=True)
class Problem:
    """One thing wrong, as a path or check name and a message that holds no data."""

    field: str
    message: str


class CallError(Exception):
    """A typed call-time failure. `code` is ValidationFailed or PostconditionFailed."""

    def __init__(self, code: str, problems: list):
        self.code = code
        self.problems = problems[:MAX_PROBLEMS]
        super().__init__(f"{code}: " + "; ".join(f"{p.field}: {p.message}" for p in self.problems))


def _pointer(path) -> str:
    return "/" + "/".join(str(part) for part in path) if path else ""


def _input_problems(error) -> list:
    base = _pointer(error.absolute_path)
    keyword = error.validator
    if keyword == "required":
        return [Problem(f"{base}/{name}", "required property is missing")
                for name in error.validator_value if name not in error.instance]
    if keyword == "additionalProperties":
        allowed = set(error.schema.get("properties", {}))
        return [Problem(f"{base}/{name}", "property is not allowed")
                for name in sorted(set(error.instance) - allowed)]
    if keyword == "type":
        expected = error.validator_value
        expected = " or ".join(expected) if isinstance(expected, list) else expected
        return [Problem(base or "<input>", f"type: expected {expected}")]
    if keyword in ("minimum", "maximum", "exclusiveMinimum", "exclusiveMaximum", "minLength", "maxLength",
                   "minItems", "maxItems", "minProperties", "maxProperties"):
        return [Problem(base or "<input>", f"{keyword}: limit is {error.validator_value}")]
    if keyword == "enum":
        return [Problem(base or "<input>", "enum: not an allowed value")]
    return [Problem(base or "<input>", f"does not satisfy '{keyword}'")]


@lru_cache(maxsize=128)
def _validator(schema_text: str) -> Draft202012Validator:
    return Draft202012Validator(json.loads(schema_text))


def validate_input(spec: dict, value: object) -> None:
    """Check ``value`` against the tool's input schema. Raises CallError('ValidationFailed')."""
    try:
        size = len(json.dumps(value, sort_keys=True, ensure_ascii=False, allow_nan=False).encode("utf-8"))
    except (TypeError, ValueError):
        raise CallError("ValidationFailed", [Problem("<input>", "input is not JSON-compatible")]) from None
    if size > MAX_INPUT_BYTES:
        raise CallError("ValidationFailed", [Problem("<input>", f"input exceeds {MAX_INPUT_BYTES} bytes")])
    schema_text = json.dumps(spec["inputSchema"], sort_keys=True)
    problems: list = []
    for error in sorted(_validator(schema_text).iter_errors(value),
                        key=lambda e: (_pointer(e.absolute_path), str(e.validator))):
        problems.extend(_input_problems(error))
    problems = sorted(set(problems), key=lambda p: (p.field, p.message))  # jsonschema reports one error per missing property
    if problems:
        raise CallError("ValidationFailed", problems)


@lru_cache(maxsize=256)
def _pattern(source: str):
    return re2.compile(source)


def _postcondition_problems(index: int, check: dict, output: str) -> list:
    name = f"postconditions/{index}"
    if check["type"] == "max_bytes":
        if len(output.encode("utf-8")) > check["value"]:
            return [Problem(name, f"max_bytes: output exceeds {check['value']} bytes")]
        return []
    if check["type"] == "each_line_matches":
        if output == "":
            return [] if check.get("allow_empty", True) else [Problem(name, "each_line_matches: output is empty")]
        try:
            pattern = _pattern(check["pattern"])
        except Exception:  # RE2 refuses what it cannot match in linear time; fail closed
            return [Problem(name, "each_line_matches: the pattern is not accepted by the linear-time engine")]
        problems = []
        for number, line in enumerate(output.split("\n"), start=1):
            if len(line.encode("utf-8")) > MAX_LINE_BYTES:
                problems.append(Problem(name, f"each_line_matches: line {number} exceeds {MAX_LINE_BYTES} bytes"))
            elif pattern.fullmatch(line) is None:
                problems.append(Problem(name, f"each_line_matches: line {number} does not match"))
        return problems
    return [Problem(name, f"unknown postcondition type {check['type']!r}")]


def validate_output(spec: dict, result: object) -> dict:
    """Check a tool's result against its postconditions. Returns the result.

    The result must be exactly `{"ok": true, "output": <string>}` or `{"ok": false, "error": <string>}`.
    A field that is not part of the matching form, including a field of the other form, is refused and
    named, never echoed. A tool failure (ok false) in the exact form is returned unchanged: a tool saying
    no is a result. A result that is not the tool contract, or whose output breaks a declared
    postcondition, raises CallError('PostconditionFailed').
    """
    shape = "<result>"
    if not isinstance(result, dict) or not isinstance(result.get("ok"), bool):
        raise CallError("PostconditionFailed", [Problem(shape, "result must be an object with a boolean ok")])
    allowed = {"ok", "error"} if result["ok"] is False else {"ok", "output"}
    extra = sorted(str(key) for key in result if key not in allowed)
    if extra:
        raise CallError("PostconditionFailed", [Problem(f"/{key}", "property is not allowed in this result") for key in extra])
    if result["ok"] is False:
        if not isinstance(result.get("error"), str):
            raise CallError("PostconditionFailed", [Problem(shape, "a failed result must carry a string error")])
        return result
    output = result.get("output")
    if not isinstance(output, str):
        raise CallError("PostconditionFailed", [Problem("output", "must be a string")])
    if len(output.encode("utf-8")) > MAX_OUTPUT_BYTES:
        raise CallError("PostconditionFailed", [Problem("output", f"exceeds {MAX_OUTPUT_BYTES} bytes")])
    problems: list = []
    for index, check in enumerate(spec.get("postconditions", [])):
        problems.extend(_postcondition_problems(index, check, output))
    if problems:
        raise CallError("PostconditionFailed", problems)
    return result
