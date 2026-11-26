"""Smoke test of the running registry service (deploy/compose.yaml, profile "smoke").

Talks to the service only as a client would: REST for everything, the dev feed for revocation, and the MCP
server as a stdio subprocess. Each run uses fresh tool names, so it can be repeated against the same store.
Exit status 0 when every step passes. Prints one line per step.
"""

import base64
import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.request
import uuid

URL = os.environ.get("REGISTRY_URL", "http://127.0.0.1:8090")
RUN = os.environ["TAFFEE_RUN"]
AGENT = "agent:smoke"
TAG = uuid.uuid4().hex[:8]

WORD_COUNT = '''import json, os, sys
req = json.loads(sys.stdin.read())
data = req["input"]
try:
    if "path" in data:
        text = open(os.path.join(req["root"], data["path"]), encoding="utf-8").read()
    else:
        text = data["text"]
    print(json.dumps({"ok": True, "output": str(len(text.split()))}))
except Exception as exc:
    print(json.dumps({"ok": False, "error": "cannot read: " + type(exc).__name__}))
'''


def bundle(name, source=WORD_COUNT):
    manifest = {
        "kind": "tool", "name": name, "version": "1.0.0", "owner": "smoke", "requires": ["repo.read"],
        "summary": f"Count the words in a text or a repository file (smoke {TAG}).",
        "implementations": [{"language": "python", "location": f"library/{name}/python"}],
        "spec": {"inputSchema": {"type": "object", "additionalProperties": False, "properties": {
            "text": {"type": "string", "description": "Text whose words are counted."},
            "path": {"type": "string", "description": "A repository file whose words are counted."}}},
            "readOnly": True, "idempotent": True, "timeoutMs": 30000, "boundary": [],
            "sandbox": {"filesystem": "read-root"}},
    }
    cases = [
        {"id": "text", "input": {"text": "one two three"}, "expect": {"ok": True, "contains": '"output": "3"'}},
        {"id": "file", "input": {"path": "a.txt"}, "files": {"a.txt": "alpha beta\n"},
         "expect": {"ok": True, "contains": '"output": "2"'}},
        {"id": "missing", "input": {"path": "none.txt"}, "files": {"a.txt": "x"}, "expect": {"ok": False}},
    ]
    return json.dumps({"schema": "capability-submission/1", "manifest": manifest, "cases": cases,
                       "files": {"python": {"main.py": base64.b64encode(source.encode()).decode()}}}).encode()


def call(method, path, body=None, principal=AGENT):
    request = urllib.request.Request(URL + path, data=body, method=method, headers={"X-Dev-Principal": principal})
    try:
        with urllib.request.urlopen(request, timeout=120) as response:
            return response.status, json.loads(response.read())
    except urllib.error.HTTPError as error:
        return error.code, json.loads(error.read())


def wait_for(sid, timeout=300):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        _, record = call("GET", f"/v2/submissions/{sid}")
        if record["state"] in ("admitted", "rejected", "awaiting_approval"):
            return record
        time.sleep(1)
    raise AssertionError(f"submission {sid} did not finish")


def step(text, ok, detail=""):
    print(("PASS " if ok else "FAIL ") + text + (f"  [{detail}]" if detail and not ok else ""), flush=True)
    if not ok:
        sys.exit(1)


def main():
    status, found = call("GET", "/v2/search?q=search+file+contents")
    step("built-in grep is discoverable at Tested", status == 200 and found["items"]
         and found["items"][0]["name"] == "grep" and found["items"][0]["label"] == "Tested", found)

    name = f"smoke-count-{TAG}"
    status, record = call("POST", "/v2/submissions", bundle(name))
    step("an agent's submission is accepted (202)", status == 202, record)
    started = time.monotonic()
    record = wait_for(record["id"])
    step(f"it is built, tested in containers and admitted ({time.monotonic() - started:.1f}s)",
         record["state"] == "admitted", record.get("problems"))

    status, again = call("POST", "/v2/submissions", bundle(name))
    step("a retry returns the original submission", status == 200 and again["id"] == record["id"], again)

    status, described = call("GET", f"/v2/tools/{name}")
    step("it is owned by taffee-library and Validated",
         status == 200 and described["owner"] == "taffee-library" and described["label"] == "Validated", described)

    status, found = call("GET", f"/v2/search?q=count+words+smoke+{TAG}")
    step("search finds it with a model-ready definition",
         status == 200 and [i["name"] for i in found["items"]] == [name]
         and found["items"][0]["tool_definition"]["name"] == name, found)

    status, match = call("POST", "/v2/resolve", json.dumps({"name": name}).encode())
    step("resolve returns verified facts with revocation checked",
         status == 200 and match["revocation"] == "checked", match)

    status, broken = call("POST", "/v2/submissions", bundle(f"smoke-broken-{TAG}", source="def broken(:\n"))
    broken = wait_for(broken["id"])
    step("a tool that does not compile is rejected, naming the file and line",
         broken["state"] == "rejected" and broken["problems"] == ["python/build: syntax: main.py:1"], broken)

    library = os.environ.get("TAFFEE_LIBRARY")
    if library:
        # This check runs as root and the library belongs to the host user: tell git to trust it.
        log = subprocess.run(["git", "-c", f"safe.directory={library}", "-C", library, "log", "-1", "--format=%s"],
                             capture_output=True, text=True)
        step("the library export is on: the admitted tool is committed", log.stdout.strip() == f"Admit {name} 1.0.0",
             log.stdout)
    else:
        status, versions = call("GET", f"/v2/tools/{name}/versions")
        step("the library export is off: the store alone holds the admitted version",
             status == 200 and [v["version"] for v in versions["items"]] == ["1.0.0"], versions)

    messages = [
        {"jsonrpc": "2.0", "id": 1, "method": "initialize",
         "params": {"protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "smoke", "version": "1"}}},
        {"jsonrpc": "2.0", "method": "notifications/initialized"},
        {"jsonrpc": "2.0", "id": 2, "method": "tools/call",
         "params": {"name": "search_capabilities", "arguments": {"query": f"count words smoke {TAG}"}}},
    ]
    done = subprocess.run([sys.executable, "-m", "capability.registry_cli", "mcp", f"--url={URL}",
                           "--principal=agent:ide"], cwd="/app", capture_output=True, text=True,
                          input="".join(json.dumps(m) + "\n" for m in messages), timeout=60)
    replies = [json.loads(line) for line in done.stdout.splitlines() if line.strip()]
    step("MCP over stdio finds it too", len(replies) == 2
         and [i["name"] for i in replies[1]["result"]["structuredContent"]["items"]] == [name], done.stderr[-500:])

    subprocess.run([sys.executable, "-m", "capability.registry_cli", "dev-feed", "revoke", f"--dir={RUN}/feed",
                    f"--name={name}", "--version=1.0.0", "--reason=incident"], cwd="/app", check=True, capture_output=True)
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        status, body = call("POST", "/v2/resolve", json.dumps({"name": name}).encode())
        if status == 410:
            break
        time.sleep(1)
    step("after revocation on the feed, resolve answers 410 Revoked", status == 410, body)
    _, found = call("GET", f"/v2/search?q=count+words+smoke+{TAG}")
    step("and search no longer offers it", found["items"] == [], found)
    print("smoke: all steps passed", flush=True)


if __name__ == "__main__":
    main()
