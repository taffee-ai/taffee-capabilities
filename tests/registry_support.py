"""Helpers for the S-018 tests: keys, a fresh revocation client, signed rows, and an outside tool's bundle."""

import base64
import copy
import json
import subprocess
from pathlib import Path

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from capability.admission import BUNDLE_SCHEMA, append_signed
from capability.digest import manifest_digest
from capability.implementations import sorted_manifest, tool_identity
from capability.languages import load_languages
from capability.revocation import RevocationClient, load_public_key
from capability.sandbox import RunResult

from fixture_support import REPO
from revocation_support import Clocks, FakeFeed, make_key

LANGUAGES = load_languages(REPO)

# A tool written outside Taffee: counts the words in a text, or in a file inside the repository.
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

CASES = [
    {"id": "counts-text", "input": {"text": "one two three"}, "expect": {"ok": True, "contains": '"output": "3"'}},
    {"id": "counts-a-file", "input": {"path": "doc.txt"}, "files": {"doc.txt": "alpha beta\n"},
     "expect": {"ok": True, "contains": '"output": "2"'}},
    {"id": "missing-file-is-an-error", "input": {"path": "absent.txt"}, "files": {"doc.txt": "x"},
     "expect": {"ok": False}},
]


ERROR_CASE = CASES[2]


def with_error_case(cases):
    """Taffee requires at least one case that expects a tool-level error; add the standard one."""
    return list(cases) + [ERROR_CASE]


def word_count_manifest(version="1.0.0", name="word-count", described=True, sandbox=True, summary=None):
    props = {"text": {"type": "string", "maxLength": 10000}, "path": {"type": "string", "maxLength": 200}}
    if described:
        props["text"]["description"] = "Text whose words are counted."
        props["path"]["description"] = "A file inside the repository whose words are counted."
    manifest = {
        "kind": "tool", "name": name, "version": version, "owner": "acme-tools",
        "summary": summary or "Count the words in a piece of text or in a repository file.",
        "requires": ["repo.read"],
        "implementations": [{"language": "python", "location": f"library/{name}/python"}],
        "spec": {"inputSchema": {"type": "object", "additionalProperties": False, "properties": props},
                 "readOnly": True, "idempotent": True, "timeoutMs": 30000, "boundary": [],
                 "postconditions": [{"type": "max_bytes", "value": 64}]},
    }
    if sandbox:
        manifest["spec"]["sandbox"] = {"filesystem": "read-root"}
    return manifest


def bundle(manifest=None, source=WORD_COUNT, cases=None, files=None) -> dict:
    files = files if files is not None else {"python": {"main.py": base64.b64encode(source.encode()).decode()}}
    return {"schema": BUNDLE_SCHEMA, "manifest": manifest or word_count_manifest(), "files": files,
            "cases": copy.deepcopy(CASES if cases is None else cases)}


def bundle_bytes(**kwargs) -> bytes:
    return json.dumps(bundle(**kwargs)).encode()


def admission_key():
    private = Ed25519PrivateKey.generate()
    return private, private.public_key()


def fresh_revocation():
    """A revocation client kept fresh by a FakeFeed; returns (client, feed, clocks)."""
    private, encoded = make_key()
    clocks = Clocks()
    client = RevocationClient(load_public_key(encoded), wall=clocks.wall, monotonic=clocks.mono)
    feed = FakeFeed(clocks, private)
    client.poll(feed)
    return client, feed, clocks


def synthetic_fields(name, version="1.0.0", label="Validated", summary=None, params=None, languages=("python",),
                     code_digests=None):
    """Row fields for a tool whose code is not stored (store contract tests, scale benchmarks)."""
    props = {key: {"type": "string", "description": text} for key, text in (params or {"q": "a query"}).items()}
    manifest = {
        "kind": "tool", "name": name, "version": version, "owner": "bench", "summary": summary or f"Tool {name}.",
        "requires": ["repo.read"],
        "implementations": [{"language": lang, "location": f"library/{name}/{lang}"} for lang in languages],
        "spec": {"inputSchema": {"type": "object", "properties": props}, "readOnly": True, "idempotent": True,
                 "timeoutMs": 5000, "boundary": [], "sandbox": {"filesystem": "read-root"}},
    }
    manifest = sorted_manifest(manifest)
    impls = [{"language": lang, "code_digest": (code_digests or {}).get(lang, "sha256:" + "0" * 64),
              "entry": LANGUAGES[lang]["entry"]} for lang in sorted(languages)]
    return {"kind": "tool", "name": name, "version": version, "manifest": manifest,
            "manifest_digest": manifest_digest(manifest), "identity": tool_identity(manifest, impls),
            "implementations": impls, "label": label, "evidence": [], "source": {"type": "submission", "id": "sub-x"},
            "submitted_by": "author:test", "admitted_by": "approver:test", "admitted_at": "2026-11-15T00:00:00Z"}


def add_rows(store, private_key, fields_list):
    return [append_signed(store, private_key, fields) for fields in fields_list]


class UnconfinedTestSandbox:
    """TEST ONLY. Runs the tool as a plain subprocess, mapping /impl and /work to the real folders. It claims to
    enforce every limit and enforces none, so it is used only with test code we wrote, never with outside code."""

    name = "unconfined-test"

    def __init__(self):
        self.calls = []

    def run(self, argv, *, root, policy, stdin=b"", env_values=None, implementation=None):
        self.calls.append({"argv": list(argv), "policy": policy})
        argv = [arg.replace("/impl", str(implementation)) for arg in argv]  # also inside a `python3 -c` script
        request = json.loads(stdin)
        request["root"] = str(root)
        try:
            done = subprocess.run(argv, input=json.dumps(request).encode(), capture_output=True,
                                  timeout=policy.timeout_s, cwd=str(root))
        except subprocess.TimeoutExpired:
            return RunResult(None, b"", b"", True, False)
        truncated = len(done.stdout) > policy.output_bytes
        return RunResult(done.returncode, done.stdout[:policy.output_bytes], b"", False, truncated)


__all__ = ["Path"]
