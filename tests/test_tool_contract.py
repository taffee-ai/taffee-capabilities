"""T-007: one contract suite for every implemented tool.

A tool that appears in the catalog with an implementation is found here automatically and must pass every
check below, so a new tool cannot skip one. The checks are the shared contract: a valid declared spec, usable
conformance cases, a process that never crashes and always answers with one JSON line, rejection of inputs
the schema forbids, a bounded output that meets its postconditions, and bounded error text that does not
leak the host path. The evidence suite (tests/test_tools_t001.py) is separate and is not changed here.

Tools that are declared but have no implementation (graph-query) are not part of this contract. They are
listed in DECLARED_ONLY, so a new declared-only tool needs a deliberate edit here.
"""

import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path

import yaml
from jsonschema import Draft202012Validator

from capability.calltime import CallError, validate_input, validate_output
from capability.conformance import ENV_ALLOW_LIST, RUNNERS, build_repository, load_cases, run_cases, run_implementation
from capability.languages import load_languages
from capability.manifest import check_manifest_file

from fixture_support import REPO

DECLARED_ONLY = {"graph-query"}
MAX_RESULT_LINE_BYTES = 64 * 1024
MAX_ERROR_CHARS = 2048
BAD_STDIN = {
    "empty": b"",
    "not utf-8": b"\xff\xfe\x00",
    "not json": b"not json",
    "json array": b"[]",
    "json null": b"null",
    "empty object": b"{}",
}


def catalog_tools():
    found = {}
    for path in sorted((REPO / "catalog" / "tool").glob("*/manifest.yaml")):
        manifest = yaml.safe_load(path.read_text(encoding="utf-8"))
        found[manifest["name"]] = (path, manifest)
    return found


def implemented():
    """{(tool, language): (manifest, folder, entry, runner, toolchain)} for every implementation in the catalog."""
    languages = load_languages(REPO)
    out = {}
    for name, (_path, manifest) in catalog_tools().items():
        for implementation in manifest.get("implementations") or []:
            language = languages[implementation["language"]]
            out[(name, implementation["language"])] = (
                manifest, REPO / implementation["location"], language["entry"], language["runner"], language["toolchain"])
    return out


TOOLS = implemented()


def stress_cases():
    """One case per tool whose output can exceed its bound. A tool with a max_bytes postcondition and no entry
    here fails `test_every_bounded_tool_has_a_stress_case`."""
    return {
        "glob": {"id": "stress", "input": {"pattern": "**/*.ts"},
                 "files": {f"deep/dir{n % 20}/{'x' * 80}{n}.ts": "" for n in range(2500)},
                 "expect": {"ok": True, "contains": "truncated"}},
        "grep": {"id": "stress", "input": {"pattern": "hello", "files": "**/*.txt"},
                 "files": {f"d{n}.txt": "hello there friend\n" * 300 for n in range(60)},
                 "expect": {"ok": True, "contains": "truncated"}},
        "git-log": {"id": "stress", "input": {"n": 100}, "files": {"readme.txt": "x\n"}, "git": {"commits": 150},
                    "expect": {"ok": True, "contains": "commit number 149"}},
        "unawaited-async": {"id": "stress", "input": {"files": "**/*.ts"},
                            "files": {f"f{n}.ts": f"async function load{n}() {{\n  return {n};\n}}\n" * 5 for n in range(1500)},
                            "expect": {"ok": True, "contains": "truncated"}},
    }


def run_tool(tool_key, stdin: bytes):
    """Run one implementation with raw bytes on stdin (so invalid UTF-8 can be sent), in the same minimal environment
    the conformance runner uses."""
    manifest, folder, entry, runner, toolchain = TOOLS[tool_key]
    env = {key: os.environ[key] for key in ENV_ALLOW_LIST if key in os.environ}
    return subprocess.run(RUNNERS[runner]((folder / entry).resolve()), input=stdin, capture_output=True, timeout=60,
                          cwd=str(folder), env=env, check=False)


def _no_constant(name):
    raise ValueError(f"non-finite number {name}")


def _no_duplicates(pairs):
    keys = [key for key, _ in pairs]
    if len(keys) != len(set(keys)):
        raise ValueError("duplicate key")
    return dict(pairs)


def one_json_line(case, done):
    """The process answered with exactly one JSON object line, exit 0, and no crash trace."""
    case.assertEqual(done.returncode, 0, done.stderr[-300:] if isinstance(done.stderr, (bytes, str)) else "")
    try:
        out = done.stdout.decode("utf-8") if isinstance(done.stdout, bytes) else done.stdout  # strict: invalid UTF-8 is a failure
    except UnicodeDecodeError:
        case.fail("the tool wrote invalid UTF-8")
    err = done.stderr.decode("utf-8", "replace") if isinstance(done.stderr, bytes) else done.stderr
    case.assertNotIn("Traceback", err)
    lines = out.splitlines()
    case.assertEqual(len(lines), 1, out[:200])
    try:
        result = json.loads(lines[0], parse_constant=_no_constant, object_pairs_hook=_no_duplicates)
    except ValueError as exc:
        case.fail(f"the output line is not strict JSON ({exc})")
    case.assertIsInstance(result, dict)
    case.assertIs(type(result.get("ok")), bool, "ok must be a JSON boolean (1 and 0 are not)")
    if result["ok"]:
        case.assertIsInstance(result.get("output"), str, "an ok result carries a string output")
    else:
        case.assertIsInstance(result.get("error"), str, "an error result carries a string error")
        case.assertTrue(result["error"], "the error text must not be empty")
        case.assertLessEqual(len(result["error"]), MAX_ERROR_CHARS)
    case.assertLessEqual(len(lines[0].encode("utf-8")), MAX_RESULT_LINE_BYTES)
    return result


def invalid_inputs(schema):
    """Inputs the declared schema forbids: each required property missing, each property of the wrong type, one
    extra property. Every one is checked to be really invalid, so the generator cannot drift from the schema."""
    validator = Draft202012Validator(schema)
    properties = schema.get("properties", {})
    base = {}
    for name in schema.get("required", []):
        base[name] = {"string": "x", "integer": 1, "boolean": True}.get(properties[name].get("type"), "x")
    found = []
    for name in schema.get("required", []):
        found.append((f"missing {name}", {k: v for k, v in base.items() if k != name}))
    wrong = {"string": 5, "integer": "five", "boolean": "yes", "array": "x", "object": []}
    for name, spec in properties.items():
        if spec.get("type") in wrong:
            found.append((f"wrong type {name}", dict(base, **{name: wrong[spec["type"]]})))
    found.append(("extra property", dict(base, unexpected_property=1)))
    found.append(("not an object", []))
    return [(label, value) for label, value in found if not validator.is_valid(value)]


class FakeDone:
    def __init__(self, stdout, returncode=0, stderr=""):
        self.stdout, self.returncode, self.stderr = stdout, returncode, stderr


class ContractHelperTest(unittest.TestCase):
    """The shared result check must itself reject a malformed tool response."""

    def rejects(self, line, **kwargs):
        with self.assertRaises(AssertionError):
            one_json_line(self, FakeDone(line + "\n", **kwargs))

    def test_the_result_check_accepts_well_formed_results(self):
        one_json_line(self, FakeDone('{"ok": true, "output": "x"}\n'))
        one_json_line(self, FakeDone('{"ok": false, "error": "bad input"}\n'))

    def test_ok_must_be_a_real_boolean(self):
        for line in ('{"ok": 1, "output": "x"}', '{"ok": 0, "error": "e"}', '{"ok": "true", "output": "x"}',
                     '{"ok": null, "error": "e"}', '{"output": "x"}', '{"ok": 1.0, "output": "x"}'):
            with self.subTest(line=line):
                self.rejects(line)

    def test_an_error_result_needs_a_bounded_non_empty_string_error(self):
        for line in ('{"ok": false}', '{"ok": false, "error": 5}', '{"ok": false, "error": ""}', '{"ok": false, "error": null}',
                     '{"ok": false, "error": "' + "e" * (MAX_ERROR_CHARS + 1) + '"}'):
            with self.subTest(line=line[:40]):
                self.rejects(line)

    def test_an_ok_result_needs_a_string_output(self):
        for line in ('{"ok": true}', '{"ok": true, "output": 5}', '{"ok": true, "output": null}', '{"ok": true, "output": ["x"]}'):
            with self.subTest(line=line):
                self.rejects(line)

    def test_strict_json_only(self):
        """NaN, Infinity and duplicate keys are not accepted as a JSON result line."""
        for line in ('{"ok": true, "output": "x", "n": NaN}', '{"ok": true, "output": "x", "n": Infinity}',
                     '{"ok": true, "output": "x", "n": -Infinity}', '{"ok": true, "ok": false, "output": "x"}',
                     '{"ok": false, "error": "e", "error": "f"}'):
            with self.subTest(line=line):
                self.rejects(line)

    def test_invalid_utf8_on_stdout_is_rejected(self):
        for raw in (b'{"ok": true, "output": "\xff"}\n', b'\xff\xfe{"ok": true, "output": "x"}\n',
                    b'{"ok": true, "output": "caf\xe9"}\n'):
            with self.subTest(raw=raw[:30]):
                with self.assertRaises(AssertionError):
                    one_json_line(self, FakeDone(raw))
        one_json_line(self, FakeDone('{"ok": true, "output": "caf\u00e9"}\n'.encode("utf-8")))  # valid UTF-8 is fine

    def test_more_than_one_line_a_crash_trace_or_a_bad_exit_is_rejected(self):
        self.rejects('{"ok": true, "output": "x"}\n{"ok": true, "output": "y"}')
        self.rejects("", returncode=0)
        self.rejects('{"ok": true, "output": "x"}', returncode=1)
        self.rejects('{"ok": true, "output": "x"}', stderr="Traceback (most recent call last):")
        self.rejects("[]")
        self.rejects("not json")


class DiscoveryTest(unittest.TestCase):
    def test_every_catalog_tool_is_either_implemented_here_or_declared_only(self):
        names = set(catalog_tools())
        implemented_names = {name for name, _ in TOOLS}
        self.assertEqual(names - implemented_names, DECLARED_ONLY, "a new declared-only tool needs a deliberate edit")
        self.assertTrue(implemented_names, "no implemented tool found")

    def test_a_declared_only_tool_has_no_implementation_and_no_library_folder(self):
        for name in DECLARED_ONLY:
            manifest = catalog_tools()[name][1]
            self.assertEqual(manifest.get("implementations"), [])
            self.assertFalse((REPO / "library" / name).exists())

    def test_the_four_built_in_tools_are_covered(self):
        self.assertEqual({name for name, _ in TOOLS}, {"git-log", "glob", "grep", "unawaited-async"})


class SpecContractTest(unittest.TestCase):
    def test_the_declared_spec_is_complete_and_valid(self):
        for (name, language), (manifest, *_rest) in TOOLS.items():
            with self.subTest(tool=name, language=language):
                check_manifest_file(catalog_tools()[name][0])  # the manifest validator accepts it
                spec = manifest["spec"]
                schema = spec["inputSchema"]
                Draft202012Validator.check_schema(schema)
                self.assertEqual(schema["type"], "object")
                self.assertIs(schema.get("additionalProperties"), False, "an open input schema accepts anything")
                self.assertIsInstance(spec["timeoutMs"], int)
                self.assertGreater(spec["timeoutMs"], 0)
                self.assertIsInstance(spec["readOnly"], bool)
                self.assertIsInstance(spec["idempotent"], bool)
                self.assertIsInstance(manifest["requires"], list)
                self.assertTrue(spec.get("postconditions"), "a tool must declare at least one postcondition")

    def test_every_tool_bounds_its_output_with_a_max_bytes_postcondition(self):
        for (name, language), (manifest, *_rest) in TOOLS.items():
            with self.subTest(tool=name):
                kinds = [check["type"] for check in manifest["spec"]["postconditions"]]
                self.assertIn("max_bytes", kinds)

    def test_the_four_tools_are_read_only(self):
        for (name, language), (manifest, *_rest) in TOOLS.items():
            with self.subTest(tool=name):
                self.assertIs(manifest["spec"]["readOnly"], True)


class ConformanceCoverageTest(unittest.TestCase):
    def cases(self, folder):
        return load_cases(folder.parent / "conformance.json")

    def test_the_cases_cover_success_failure_and_input_rejection(self):
        for (name, language), (_manifest, folder, *_rest) in TOOLS.items():
            with self.subTest(tool=name, language=language):
                cases = self.cases(folder)
                self.assertTrue(any(c["expect"].get("ok") is True for c in cases), "no success case")
                self.assertTrue(any(c["expect"].get("ok") is False for c in cases), "no error case")
                self.assertGreaterEqual(len(cases), 5)
                ids = [c["id"] for c in cases]
                self.assertEqual(len(ids), len(set(ids)), "duplicate case ids")

    def test_the_cases_pass_with_the_postconditions_applied(self):
        for (name, language), (manifest, folder, entry, runner, toolchain) in TOOLS.items():
            with self.subTest(tool=name, language=language):
                self.assertEqual(run_cases(folder, entry, runner, toolchain, self.cases(folder), manifest["spec"]), [])


class ProcessContractTest(unittest.TestCase):
    def test_bad_stdin_gets_an_error_result_never_a_crash(self):
        with tempfile.TemporaryDirectory() as tmp:
            extra = {"root not a string": json.dumps({"root": 5, "input": {}}).encode(),
                     "root missing on disk": json.dumps({"root": str(Path(tmp) / "nope"), "input": {}}).encode(),
                     "input missing": json.dumps({"root": tmp}).encode(),
                     "input not an object": json.dumps({"root": tmp, "input": []}).encode()}
            for key in TOOLS:
                for label, payload in {**BAD_STDIN, **extra}.items():
                    with self.subTest(tool=key[0], stdin=label):
                        result = one_json_line(self, run_tool(key, payload))
                        self.assertIs(result["ok"], False)

    def test_inputs_the_schema_forbids_are_rejected_by_the_dispatcher_and_never_crash_the_tool(self):
        with tempfile.TemporaryDirectory() as tmp:
            for key, (manifest, *_rest) in TOOLS.items():
                schema = manifest["spec"]["inputSchema"]
                bad = invalid_inputs(schema)
                self.assertTrue(bad, f"{key[0]}: no invalid input could be generated")
                for label, value in bad:
                    with self.subTest(tool=key[0], case=label):
                        with self.assertRaises(CallError):
                            validate_input(manifest["spec"], value)  # S-009 refuses it before execution
                        done = run_tool(key, json.dumps({"root": tmp, "input": value}).encode())
                        one_json_line(self, done)  # and the tool itself still answers cleanly if it is ever reached

    def test_an_error_result_is_bounded_and_does_not_name_the_host_path(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = str(Path(tmp).resolve())
            for key, (manifest, *_rest) in TOOLS.items():
                for label, value in invalid_inputs(manifest["spec"]["inputSchema"]):
                    result = one_json_line(self, run_tool(key, json.dumps({"root": root, "input": value}).encode()))
                    if result["ok"] is False:
                        with self.subTest(tool=key[0], case=label):
                            self.assertLessEqual(len(result["error"]), MAX_ERROR_CHARS)
                            self.assertNotIn(root, result["error"])


class OutputContractTest(unittest.TestCase):
    def test_every_bounded_tool_has_a_stress_case(self):
        cases = stress_cases()
        for (name, language), (manifest, *_rest) in TOOLS.items():
            with self.subTest(tool=name):
                self.assertIn(name, cases, "a tool with a max_bytes postcondition needs a stress case in this suite")

    def test_a_stress_case_reaches_the_bound_and_still_meets_every_postcondition(self):
        cases = stress_cases()
        for (name, language), (manifest, folder, entry, runner, toolchain) in TOOLS.items():
            with self.subTest(tool=name, language=language):
                failures = run_cases(folder, entry, runner, toolchain, [cases[name]], manifest["spec"])
                self.assertEqual(failures, [])

    def test_the_stress_output_fits_the_declared_byte_bound(self):
        cases = stress_cases()
        for (name, language), (manifest, folder, entry, runner, toolchain) in TOOLS.items():
            bound = next(c["value"] for c in manifest["spec"]["postconditions"] if c["type"] == "max_bytes")
            with self.subTest(tool=name, bound=bound):
                case = cases[name]
                with tempfile.TemporaryDirectory() as tmp:
                    root = build_repository(case, Path(tmp).resolve())
                    done = run_implementation(folder, entry, runner, json.dumps({"root": str(root), "input": case["input"]}), toolchain)
                result = one_json_line(self, done)
                self.assertTrue(result["ok"])
                self.assertLessEqual(len(result["output"].encode("utf-8")), bound)
                validate_output(manifest["spec"], result)  # S-009 accepts the result

    def test_a_result_that_breaks_a_postcondition_is_refused(self):
        for (name, language), (manifest, *_rest) in TOOLS.items():
            bound = next(c["value"] for c in manifest["spec"]["postconditions"] if c["type"] == "max_bytes")
            with self.subTest(tool=name):
                with self.assertRaises(CallError):
                    validate_output(manifest["spec"], {"ok": True, "output": "x" * (bound + 1)})


if __name__ == "__main__":
    unittest.main()
