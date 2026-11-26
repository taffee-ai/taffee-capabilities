"""unawaited-async: find async functions whose body never uses await (T-001, from the workbench tool). Heuristic.

Contract (T-001): reads {"root": "<absolute directory>", "input": {...}} as JSON on stdin and
writes exactly one JSON line to stdout: {"ok": true, "output": "..."} or {"ok": false, "error": "..."}.
It exits 0 for every result, including tool-level errors. Python standard library only.
This folder is self-contained: its code_digest covers only these files.
"""

import json
import os
import subprocess
import sys
import threading
import time
import re

MAX_OUTPUT_BYTES = 24 * 1024
MAX_ERROR_BYTES = 2 * 1024
MAX_RESULT_BYTES = 60 * 1024
TIMEOUT_SECONDS = 4.0
TRUNCATED = "[truncated: output exceeded %d bytes]" % MAX_OUTPUT_BYTES


class ToolError(Exception):
    """A tool-level failure. The message is returned to the caller as the error."""


def read_request():
    try:
        request = json.loads(sys.stdin.buffer.read().decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        raise ToolError("stdin is not valid JSON")
    if not isinstance(request, dict):
        raise ToolError("request must be an object")
    root = request.get("root")
    if not isinstance(root, str) or not root:
        raise ToolError("root is required")
    if not os.path.isdir(root):
        raise ToolError("root is not a directory")
    data = request.get("input")
    if not isinstance(data, dict):
        raise ToolError("input must be an object")
    return os.path.realpath(root), data


def _contained(root, target):
    return os.path.commonpath([root, target]) == root


def confine(root, requested):
    """Resolve an existing path inside root. Returns (absolute, relative)."""
    lexical = os.path.normpath(os.path.join(root, requested))
    if not _contained(root, lexical):
        raise ToolError("path escapes repository: " + requested)
    try:
        absolute = os.path.realpath(lexical, strict=True)
    except OSError:
        raise ToolError("path does not exist: " + requested)
    if not _contained(root, absolute):
        raise ToolError("path escapes repository through symlink: " + requested)
    return absolute, os.path.relpath(absolute, root)


def bound_lines(lines):
    """Join lines, keeping whole lines up to MAX_OUTPUT_BYTES, then add a truncation marker."""
    kept, size = [], 0
    for line in lines:
        size += len(line.encode("utf-8", "replace")) + 1
        if size > MAX_OUTPUT_BYTES:
            kept.append(TRUNCATED)
            break
        kept.append(line)
    return "\n".join(kept)


def run_bounded(argv, cwd):
    """Run argv (no shell) in cwd with a deadline and bounded output.

    Returns (returncode, stdout_text, stderr_text). A timeout or an output overrun kills the child.
    """
    proc = subprocess.Popen(
        argv, cwd=cwd, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE
    )
    expired = []

    def _expire():
        expired.append(True)
        proc.kill()

    timer = threading.Timer(TIMEOUT_SECONDS, _expire)
    timer.start()
    errors = []

    def _drain():
        errors.append(proc.stderr.read(MAX_ERROR_BYTES))
        while proc.stderr.read(65536):
            pass

    reader = threading.Thread(target=_drain)
    reader.start()
    try:
        data = proc.stdout.read(MAX_OUTPUT_BYTES + 1)
        if len(data) > MAX_OUTPUT_BYTES:
            proc.kill()
        proc.wait()
    finally:
        timer.cancel()
        reader.join()
        proc.stdout.close()
        proc.stderr.close()
    if expired:
        raise ToolError("timed out after %gs" % TIMEOUT_SECONDS)
    truncated = len(data) > MAX_OUTPUT_BYTES
    data = data[:MAX_OUTPUT_BYTES]
    if truncated and b"\n" in data:
        data = data[: data.rindex(b"\n")]
    text = data.decode("utf-8", "replace")
    if truncated:
        text += "\n" + TRUNCATED
    return (0 if truncated else proc.returncode), text, (errors[0] if errors else b"").decode("utf-8", "replace")


def search_dir(pattern, recursive):
    """The directory part of a file pattern, as the workbench computes it."""
    if recursive:
        head = pattern.split("**")[0]
        head = head[:-1] if head.endswith("/") else head
        return head or "."
    return "/".join(pattern.split("/")[:-1]) or "."


def find_files(root, pattern):
    """(sorted repository-relative paths of regular files matching a name pattern, truncated)."""
    recursive = "**" in pattern
    name = pattern.split("/")[-1]
    _, relative = confine(root, search_dir(pattern, recursive))
    start = "./" + relative if relative.startswith("-") else relative
    argv = ["find", start] + ([] if recursive else ["-maxdepth", "1"]) + ["-type", "f", "-name", name]
    code, out, err = run_bounded(argv, root)
    if code != 0:
        raise ToolError("find failed: " + (err.strip() or "exit %d" % code))
    found, truncated = set(), False
    for line in out.split("\n"):
        if line == TRUNCATED:
            truncated = True
        elif line:
            found.add(confine(root, line)[1])
    return sorted(found), truncated


ASYNC_DECLARATION = re.compile(r"\basync\s+(function\b|\(|\w+\s*\()", re.ASCII)
AWAIT = re.compile(r"\bawait\b", re.ASCII)


def is_comment(line):
    return line.strip().startswith("//")


def matching_brace(text, open_index):
    depth = 0
    for i in range(open_index, len(text)):
        if text[i] == "{":
            depth += 1
        elif text[i] == "}":
            depth -= 1
            if depth == 0:
                return i
    return -1


def analyze(content):
    """(line number, has_await) for each async declaration, as the workbench heuristic finds them."""
    lines = content.split("\n")
    findings = []
    search_from = 0
    for index, line in enumerate(lines):
        if is_comment(line) or not ASYNC_DECLARATION.search(line):
            continue
        line_start = content.find(line, search_from)
        if line_start == -1:
            continue
        open_brace = content.find("{", line_start)
        if open_brace == -1:
            continue
        close_brace = matching_brace(content, open_brace)
        if close_brace == -1:
            continue
        body = content[open_brace : close_brace + 1]
        has_await = any(not is_comment(part) and AWAIT.search(part) for part in body.split("\n"))
        findings.append((index + 1, has_await))
        search_from = close_brace
    return findings


def run(root, data):
    files = data.get("files")
    if not isinstance(files, str) or not files:
        raise ToolError("files must be a non-empty string")
    deadline = time.monotonic() + TIMEOUT_SECONDS
    offenders = []
    listed, truncated = find_files(root, files)
    for relative in listed:
        if time.monotonic() > deadline:
            raise ToolError("timed out after %gs" % TIMEOUT_SECONDS)
        absolute, shown = confine(root, relative)
        with open(absolute, "rb") as handle:
            content = handle.read().decode("utf-8", "replace")
        for number, has_await in analyze(content):
            if not has_await:
                offenders.append("%s:%d \u2014 async function with no await" % (shown, number))
    if not offenders:
        if truncated:
            return "No unawaited async functions found in the files searched.\n" + TRUNCATED
        return "No unawaited async functions found."
    return bound_lines(offenders + ([TRUNCATED] if truncated else []))


def main():
    try:
        root, data = read_request()
        result = {"ok": True, "output": run(root, data)}
    except ToolError as exc:
        result = {"ok": False, "error": str(exc)}
    except Exception as exc:  # a tool never crashes its caller
        result = {"ok": False, "error": "unexpected " + type(exc).__name__}
    encoded = (json.dumps(result, ensure_ascii=False) + "\n").encode("utf-8")
    while len(encoded) > MAX_RESULT_BYTES and result["ok"]:
        result["output"] = result["output"][: len(result["output"]) // 2] + "\n[truncated: output exceeded the result limit]"
        encoded = (json.dumps(result, ensure_ascii=False) + "\n").encode("utf-8")
    sys.stdout.buffer.write(encoded)
    sys.stdout.buffer.flush()


if __name__ == "__main__":
    main()
