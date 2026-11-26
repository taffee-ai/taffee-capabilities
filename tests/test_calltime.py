"""S-009: call-time validation (CAP-CALL-1 to CAP-CALL-11)."""

import copy
import json
import shutil
import tempfile
import unittest
from pathlib import Path

import yaml

from capability.calltime import MAX_INPUT_BYTES, MAX_LINE_BYTES, CallError, validate_input, validate_output
from capability.conformance import run_cases
from capability.index import build_index
from capability.manifest import validate_manifest
from capability.registry import Registry

from fixture_support import REPO, TOOL_VERSIONS, add_fixture_tool, new_tree, tool_manifest, write_manifest

SECRET = "s3cr3t-value-that-must-never-appear"
SCHEMA = {
    "inputSchema": {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "name": {"type": "string", "maxLength": 8},
            "n": {"type": "integer", "minimum": 1, "maximum": 100},
            "mode": {"enum": ["a", "b"]},
            "items": {"type": "array", "items": {"type": "integer"}},
        },
        "required": ["name", "n"],
    },
}


def failure(call, *args):
    with_error = None
    try:
        call(*args)
    except CallError as exc:
        with_error = exc
    return with_error


class InputTest(unittest.TestCase):
    def test_a_valid_input_passes(self):
        validate_input(SCHEMA, {"name": "x", "n": 3})
        validate_input(SCHEMA, {"name": "x", "n": 3, "mode": "a", "items": [1, 2]})

    def test_a_wrong_type_names_the_field(self):
        error = failure(validate_input, SCHEMA, {"name": 5, "n": 3})
        self.assertEqual(error.code, "ValidationFailed")
        self.assertEqual([(p.field, p.message) for p in error.problems], [("/name", "type: expected string")])

    def test_missing_required_properties_are_each_named(self):
        error = failure(validate_input, SCHEMA, {})
        self.assertEqual([(p.field, p.message) for p in error.problems],
                         [("/n", "required property is missing"), ("/name", "required property is missing")])

    def test_extra_properties_are_each_named(self):
        error = failure(validate_input, SCHEMA, {"name": "x", "n": 1, "z": 1, "a": 2})
        self.assertEqual([p.field for p in error.problems], ["/a", "/z"])
        self.assertTrue(all(p.message == "property is not allowed" for p in error.problems))

    def test_a_nested_path_is_named(self):
        error = failure(validate_input, SCHEMA, {"name": "x", "n": 1, "items": [1, "two", 3]})
        self.assertEqual([p.field for p in error.problems], ["/items/1"])

    def test_limits_and_enums_are_named_without_the_value(self):
        error = failure(validate_input, SCHEMA, {"name": "x", "n": 0, "mode": "zzz"})
        messages = {p.field: p.message for p in error.problems}
        self.assertEqual(messages["/n"], "minimum: limit is 1")
        self.assertEqual(messages["/mode"], "enum: not an allowed value")

    def test_a_bool_is_not_an_integer(self):
        self.assertEqual(failure(validate_input, SCHEMA, {"name": "x", "n": True}).problems[0].field, "/n")

    def test_no_message_ever_contains_the_input_value(self):
        for value in ({"name": SECRET, "n": 1}, {"name": "x", "n": SECRET}, {"name": "x", "n": 1, "mode": SECRET},
                      {"name": "x", "n": 1, "zzz": SECRET}, {"name": "x", "n": 1, "items": [SECRET]}, SECRET):
            with self.subTest(value=str(value)[:30]):
                error = failure(validate_input, SCHEMA, value)
                self.assertIsNotNone(error)
                self.assertNotIn("s3cr3t", str(error))
                self.assertNotIn("s3cr3t", " ".join(p.field + p.message for p in error.problems))

    def test_an_unknown_property_name_is_reported_but_never_its_value(self):
        error = failure(validate_input, SCHEMA, {"name": "x", "n": 1, "typo": SECRET})
        self.assertEqual([(p.field, p.message) for p in error.problems], [("/typo", "property is not allowed")])
        self.assertNotIn("s3cr3t", str(error))

    def test_the_order_is_stable_and_capped_at_twenty(self):
        value = {"name": "x", "n": 1, "items": ["a"] * 40}
        first = failure(validate_input, SCHEMA, value)
        second = failure(validate_input, SCHEMA, copy.deepcopy(value))
        self.assertEqual(first.problems, second.problems)
        self.assertEqual(len(first.problems), 20)

    def test_an_input_that_is_not_json_or_is_too_large_is_refused(self):
        for value in ({"name": {1, 2}, "n": 1}, {"name": "x", "n": float("nan")}):
            with self.subTest(value=str(value)[:20]):
                self.assertEqual(failure(validate_input, SCHEMA, value).problems[0].message, "input is not JSON-compatible")
        big = {"name": "x", "n": 1, "items": [1] * MAX_INPUT_BYTES}
        self.assertIn("exceeds", failure(validate_input, SCHEMA, big).problems[0].message)

    def test_error_text_names_the_code_and_the_problems(self):
        self.assertEqual(str(failure(validate_input, SCHEMA, {"name": 5, "n": 1})),
                         "ValidationFailed: /name: type: expected string")


class ResultTest(unittest.TestCase):
    def spec(self, *checks):
        return {"postconditions": list(checks)}

    def post(self, spec, output):
        return failure(validate_output, spec, {"ok": True, "output": output})

    def test_a_tool_failure_passes_through_unchanged(self):
        result = {"ok": False, "error": "git log failed"}
        self.assertIs(validate_output(self.spec({"type": "max_bytes", "value": 1}), result), result)

    def test_extra_and_mixed_fields_are_refused_and_named(self):
        spec = self.spec({"type": "max_bytes", "value": 100})
        for result, fields in (
                ({"ok": True, "output": "x", "error": "unexpected"}, ["/error"]),
                ({"ok": False, "error": "x", "output": "secret"}, ["/output"]),
                ({"ok": True, "output": "x", "debug": 1}, ["/debug"]),
                ({"ok": False, "error": "x", "trace": "t"}, ["/trace"]),
                ({"ok": True, "output": "x", "b": 1, "a": 2}, ["/a", "/b"]),
                ({"ok": True, "output": "x", "error": "e", "output2": "o"}, ["/error", "/output2"])):
            with self.subTest(result=str(result)[:50]):
                error = failure(validate_output, spec, result)
                self.assertEqual(error.code, "PostconditionFailed")
                self.assertEqual([p.field for p in error.problems], fields)
                self.assertTrue(all(p.message == "property is not allowed in this result" for p in error.problems))

    def test_an_extra_field_value_is_never_echoed(self):
        error = failure(validate_output, {}, {"ok": False, "error": "x", "output": SECRET})
        self.assertNotIn("s3cr3t", str(error))
        self.assertEqual([p.field for p in error.problems], ["/output"])

    def test_the_exact_forms_still_pass(self):
        ok = {"ok": True, "output": "fine"}
        failed = {"ok": False, "error": "refused"}
        self.assertIs(validate_output({}, ok), ok)
        self.assertIs(validate_output({}, failed), failed)

    def test_non_string_keys_are_refused_without_a_crash(self):
        error = failure(validate_output, {}, {"ok": True, "output": "x", 5: "y"})
        self.assertEqual(error.code, "PostconditionFailed")

    def test_malformed_results_are_postcondition_failures(self):
        for result in (None, "text", [], {}, {"ok": "yes"}, {"ok": False}, {"ok": False, "error": 5},
                       {"ok": True}, {"ok": True, "output": 5}, {"ok": True, "output": None}):
            with self.subTest(result=str(result)):
                error = failure(validate_output, {}, result)
                self.assertEqual(error.code, "PostconditionFailed")

    def test_no_postconditions_means_pass(self):
        result = {"ok": True, "output": "anything\nat all"}
        self.assertIs(validate_output({}, result), result)
        self.assertIs(validate_output({"postconditions": []}, result), result)

    def test_max_bytes_passes_and_fails(self):
        spec = self.spec({"type": "max_bytes", "value": 5})
        self.assertIsNone(self.post(spec, "12345"))
        error = self.post(spec, "123456")
        self.assertEqual((error.code, error.problems[0].field), ("PostconditionFailed", "postconditions/0"))
        self.assertIn("max_bytes", error.problems[0].message)

    def test_max_bytes_counts_bytes_not_characters(self):
        self.assertIsNotNone(self.post(self.spec({"type": "max_bytes", "value": 3}), "——"))

    def test_each_line_matches_requires_the_whole_line_to_match(self):
        spec = self.spec({"type": "each_line_matches", "pattern": "[0-9]+ .+"})
        self.assertIsNone(self.post(spec, "12 a\n3 b"))
        error = self.post(spec, "12 a\nx 3 b\n4 c")
        self.assertEqual([p.message for p in error.problems], ["each_line_matches: line 2 does not match"])
        self.assertIsNotNone(self.post(spec, "12 a trailing\n"))  # the empty last line does not match

    def test_failures_name_the_line_and_never_the_content(self):
        spec = self.spec({"type": "each_line_matches", "pattern": "ok"})
        error = self.post(spec, "ok\n" + SECRET + "\nok")
        self.assertNotIn("s3cr3t", str(error))
        self.assertEqual(error.problems[0].message, "each_line_matches: line 2 does not match")

    def test_allow_empty_defaults_to_true_and_can_be_false(self):
        self.assertIsNone(self.post(self.spec({"type": "each_line_matches", "pattern": "x"}), ""))
        strict = self.spec({"type": "each_line_matches", "pattern": "x", "allow_empty": False})
        self.assertEqual(self.post(strict, "").problems[0].message, "each_line_matches: output is empty")
        self.assertIsNone(self.post(strict, "x"))

    def test_a_very_long_line_fails_without_being_matched(self):
        spec = self.spec({"type": "each_line_matches", "pattern": "(a+)+b"})  # a catastrophic pattern
        error = self.post(spec, "a" * (MAX_LINE_BYTES + 1))
        self.assertIn("exceeds", error.problems[0].message)

    def test_several_failing_postconditions_are_reported_together(self):
        spec = self.spec({"type": "max_bytes", "value": 3}, {"type": "each_line_matches", "pattern": "x+"})
        error = self.post(spec, "abcdef")
        self.assertEqual([p.field for p in error.problems], ["postconditions/0", "postconditions/1"])

    def test_an_oversize_output_fails(self):
        self.assertIn("exceeds", self.post({}, "x" * (64 * 1024 + 1)).problems[0].message)

    def test_an_unknown_postcondition_type_fails_closed(self):
        self.assertIn("unknown postcondition type", self.post(self.spec({"type": "magic"}), "x").problems[0].message)


class LinearTimeTest(unittest.TestCase):
    """Postcondition patterns run on RE2, so no pattern can hang on a short line (review 1684acd8)."""

    def post(self, pattern, line, **extra):
        spec = {"postconditions": [dict({"type": "each_line_matches", "pattern": pattern}, **extra)]}
        return failure(validate_output, spec, {"ok": True, "output": line})

    def test_a_catastrophic_pattern_on_lines_well_under_the_size_bound_returns_quickly(self):
        import time
        for length in (30, 200, 1000, 3000):
            with self.subTest(length=length):
                started = time.perf_counter()
                error = self.post("(a+)+b", "a" * length)
                self.assertLess(time.perf_counter() - started, 1.0)
                self.assertEqual(error.problems[0].message, "each_line_matches: line 1 does not match")

    def test_other_classic_backtracking_patterns_are_also_linear(self):
        import time
        for pattern in ("(a|aa)+b", "(a*)*b", "(.*a){12}", "([a-z]+)*$x", ".*.*.*.*b"):
            with self.subTest(pattern=pattern):
                started = time.perf_counter()
                self.post(pattern, "a" * 3000)
                self.assertLess(time.perf_counter() - started, 1.0)

    def test_the_matching_is_done_by_re2_not_python_re(self):
        from capability import calltime
        self.assertTrue(type(calltime._pattern("a+")).__module__.startswith("re2"))
        source = Path(calltime.__file__).read_text(encoding="utf-8")
        self.assertNotIn("import re\n", source)
        self.assertNotIn("re.compile(", source)

    def test_a_match_still_needs_the_whole_line(self):
        self.assertIsNone(self.post("[0-9]+", "123"))
        self.assertIsNotNone(self.post("[0-9]+", "123x"))
        self.assertIsNotNone(self.post("[0-9]+", "x123"))

    def test_patterns_re2_cannot_match_in_linear_time_are_refused_at_manifest_validation(self):
        for pattern in ("(?!/).+", "(?=a)a", "(?<=a)b", "(?<!a)b", "(a)\\1", "(?P<x>a)(?P=x)"):
            with self.subTest(pattern=pattern):
                manifest = tool_manifest("fixture-tool", [])
                manifest["spec"]["postconditions"] = [{"type": "each_line_matches", "pattern": pattern}]
                errors = validate_manifest(manifest)
                self.assertEqual(len(errors), 1, errors)
                self.assertIn("not accepted by the linear-time engine (RE2)", errors[0])
                self.assertIn("spec.postconditions[0].pattern", errors[0])

    def test_a_pattern_that_slipped_past_validation_fails_closed_at_call_time(self):
        error = self.post("(?!/).+", "a.ts")
        self.assertEqual(error.code, "PostconditionFailed")
        self.assertIn("not accepted by the linear-time engine", error.problems[0].message)

    def test_good_patterns_are_accepted(self):
        manifest = tool_manifest("fixture-tool", [])
        manifest["spec"]["postconditions"] = [
            {"type": "each_line_matches", "pattern": "[^/].*|\\[truncated: output exceeded [0-9]+ bytes\\]"},
            {"type": "each_line_matches", "pattern": "[0-9a-f]{7,40} .+"},
            {"type": "each_line_matches", "pattern": ".+:[0-9]+:.*"}]
        self.assertEqual(validate_manifest(manifest), [])


class GlobRewriteEquivalenceTest(unittest.TestCase):
    """glob's postcondition lost its lookahead in 1.3.0. The two patterns must agree on every line."""

    OLD = r"(?!/).+|\[truncated: output exceeded [0-9]+ bytes\]"

    def new_pattern(self):
        manifest = yaml.safe_load((REPO / "catalog" / "tool" / "glob" / "manifest.yaml").read_text(encoding="utf-8"))
        patterns = [c["pattern"] for c in manifest["spec"]["postconditions"] if c["type"] == "each_line_matches"]
        self.assertEqual(len(patterns), 1)
        return patterns[0]

    def test_the_new_pattern_is_the_documented_rewrite(self):
        self.assertEqual(self.new_pattern(), r"[^/].*|\[truncated: output exceeded [0-9]+ bytes\]")

    def test_the_two_patterns_agree_on_representative_lines(self):
        import re
        import re2
        old, new = re.compile(self.OLD), re2.compile(self.new_pattern())
        lines = ["a.ts", "sub/b.ts", "/etc/passwd", "/", "", " ", " /x", "./a.ts", "a/", "-odd/a.ts", "\u00e9t\u00e9.ts",
                 "\u4e2d/\u6587.ts", "[truncated: output exceeded 24576 bytes]", "[truncated: output exceeded  bytes]",
                 "[truncated: output exceeded 1 bytes] extra", "x" * 5000, "/" + "x" * 5000]
        for line in lines:
            with self.subTest(line=line[:30]):
                self.assertEqual(old.fullmatch(line) is not None, new.fullmatch(line) is not None)

    def test_the_two_patterns_agree_on_every_short_string_over_a_small_alphabet(self):
        import itertools
        import re
        import re2
        old, new = re.compile(self.OLD), re2.compile(self.new_pattern())
        checked = 0
        for length in range(0, 6):
            for chars in itertools.product("ab/ .", repeat=length):
                line = "".join(chars)
                self.assertEqual(old.fullmatch(line) is not None, new.fullmatch(line) is not None, repr(line))
                checked += 1
        self.assertEqual(checked, sum(5 ** n for n in range(6)))

    def test_good_and_bad_real_outputs(self):
        spec = yaml.safe_load((REPO / "catalog" / "tool" / "glob" / "manifest.yaml").read_text(encoding="utf-8"))["spec"]
        for output in ("", "a.ts\nsub/b.ts", "a.ts\n[truncated: output exceeded 24576 bytes]"):
            validate_output(spec, {"ok": True, "output": output})
        for output in ("/etc/passwd", "a.ts\n/abs/b.ts", "\n", "a.ts\n\nb.ts"):
            self.assertIsNotNone(failure(validate_output, spec, {"ok": True, "output": output}))


class ManifestChecksTest(unittest.TestCase):
    def manifest(self, **spec):
        manifest = tool_manifest("fixture-tool", [])
        manifest["spec"].update(spec)
        return manifest

    def test_valid_postconditions_are_accepted(self):
        errors = validate_manifest(self.manifest(postconditions=[
            {"type": "max_bytes", "value": 100}, {"type": "each_line_matches", "pattern": "a+", "allow_empty": False}]))
        self.assertEqual(errors, [])

    def test_bad_postconditions_are_refused(self):
        bad = [
            [{"type": "magic"}],
            [{"type": "max_bytes"}],
            [{"type": "max_bytes", "value": 0}],
            [{"type": "max_bytes", "value": 70000}],
            [{"type": "max_bytes", "value": 5, "extra": 1}],
            [{"type": "each_line_matches", "pattern": ""}],
            [{"type": "each_line_matches", "pattern": "x" * 513}],
            [{"type": "each_line_matches", "pattern": "x", "allow_empty": "yes"}],
            [{"type": "max_bytes", "value": 5}] * 17,
            "not a list",
        ]
        for postconditions in bad:
            with self.subTest(postconditions=str(postconditions)[:40]):
                self.assertTrue(validate_manifest(self.manifest(postconditions=postconditions)))

    def test_a_pattern_that_does_not_compile_is_refused_with_its_index(self):
        errors = validate_manifest(self.manifest(postconditions=[
            {"type": "max_bytes", "value": 5}, {"type": "each_line_matches", "pattern": "(unclosed"}]))
        self.assertEqual(len(errors), 1)
        self.assertIn("spec.postconditions[1].pattern: not accepted by the linear-time engine (RE2)", errors[0])

    def test_an_invalid_input_schema_is_refused(self):
        errors = validate_manifest(self.manifest(inputSchema={"type": "banana"}))
        self.assertTrue(any("spec.inputSchema: not a valid JSON Schema" in e for e in errors), errors)


class RealToolsTest(unittest.TestCase):
    TOOLS = ("git-log", "glob", "grep", "unawaited-async")
    GOOD = {
        "git-log": ["", "abc1234 first commit\n0123456789abcdef0123456789abcdef01234567 second"],
        "glob": ["", "a.ts\nsub/b.ts", "a.ts\n[truncated: output exceeded 24576 bytes]"],
        "grep": ["", "./a.txt:1:hello\nin/b.txt:22:", "a.txt:3:x\n[truncated: output exceeded 24576 bytes]"],
        "unawaited-async": ["No unawaited async functions found.", "x.ts:1 — async function with no await\ny.ts:20 — async function with no await",
                            "No unawaited async functions found in the files searched.\n[truncated: output exceeded 24576 bytes]"],
    }
    BAD = {
        "git-log": ["not a commit line", "abc1234first", "abc123 x"],
        "glob": ["/etc/passwd", "a.ts\n/abs/b.ts", ""[:0] + "\n"],
        "grep": ["a.txt: no number", "plain text", "a.txt:x:y"],
        "unawaited-async": ["", "gibberish", "x.ts:1 - async function with no await"],
    }

    def spec(self, tool):
        manifest = yaml.safe_load((REPO / "catalog" / "tool" / tool / "manifest.yaml").read_text(encoding="utf-8"))
        return manifest["spec"]

    def test_each_tool_declares_postconditions_at_the_new_version(self):
        for tool in self.TOOLS:
            with self.subTest(tool=tool):
                manifest = yaml.safe_load((REPO / "catalog" / "tool" / tool / "manifest.yaml").read_text(encoding="utf-8"))
                self.assertEqual(manifest["version"], TOOL_VERSIONS[tool])
                self.assertEqual(validate_manifest(manifest), [])
                self.assertGreaterEqual(len(manifest["spec"]["postconditions"]), 2)

    def test_sample_good_outputs_pass(self):
        for tool in self.TOOLS:
            for output in self.GOOD[tool]:
                with self.subTest(tool=tool, output=output[:30]):
                    validate_output(self.spec(tool), {"ok": True, "output": output})

    def test_sample_bad_outputs_fail(self):
        for tool in self.TOOLS:
            for output in self.BAD[tool]:
                with self.subTest(tool=tool, output=output[:30]):
                    error = failure(validate_output, self.spec(tool), {"ok": True, "output": output})
                    self.assertIsNotNone(error)
                    self.assertEqual(error.code, "PostconditionFailed")

    def test_the_real_input_schemas_accept_a_valid_call_and_refuse_a_bad_one(self):
        valid = {"git-log": {"n": 3}, "glob": {"pattern": "*.ts"}, "grep": {"pattern": "x", "files": "*.ts"},
                 "unawaited-async": {"files": "*.ts"}}
        for tool, value in valid.items():
            with self.subTest(tool=tool):
                validate_input(self.spec(tool), value)
                self.assertEqual(failure(validate_input, self.spec(tool), {}).code, "ValidationFailed")
        self.assertEqual(failure(validate_input, self.spec("git-log"), {"n": 0}).problems[0].field, "/n")


class ConformanceAppliesPostconditionsTest(unittest.TestCase):
    def test_a_case_whose_output_breaks_a_postcondition_fails_the_case(self):
        with tempfile.TemporaryDirectory() as tmp:
            folder = Path(tmp) / "python"
            shutil.copytree(REPO / "library" / "glob" / "python", folder)
            cases = [{"id": "lists", "input": {"pattern": "**/*.ts"}, "expect": {"ok": True}, "files": {"a.ts": ""}}]
            spec = {"postconditions": [{"type": "each_line_matches", "pattern": "NEVER"}]}
            failures = run_cases(folder, "main.py", "python", ["python3", "--version"], cases, spec=spec)
            self.assertEqual(failures, ["lists: postconditions/0: each_line_matches: line 1 does not match"])
            self.assertEqual(run_cases(folder, "main.py", "python", ["python3", "--version"], cases, spec={}), [])

    def test_a_tool_failure_case_is_not_judged_by_the_postconditions(self):
        with tempfile.TemporaryDirectory() as tmp:
            folder = Path(tmp) / "python"
            shutil.copytree(REPO / "library" / "glob" / "python", folder)
            cases = [{"id": "refused", "input": {}, "expect": {"ok": False}, "files": {"a.ts": ""}}]
            spec = {"postconditions": [{"type": "each_line_matches", "pattern": "NEVER", "allow_empty": False}]}
            self.assertEqual(run_cases(folder, "main.py", "python", ["python3", "--version"], cases, spec=spec), [])

    def test_the_real_tools_pass_every_case_with_their_postconditions_applied(self):
        from capability.conformance import load_cases
        for tool in ("git-log", "glob", "grep", "unawaited-async"):
            with self.subTest(tool=tool):
                spec = yaml.safe_load((REPO / "catalog" / "tool" / tool / "manifest.yaml").read_text(encoding="utf-8"))["spec"]
                cases = load_cases(REPO / "library" / tool / "conformance.json")
                self.assertEqual(run_cases(REPO / "library" / tool / "python", "main.py", "python",
                                           ["python3", "--version"], cases, spec=spec), [])

    def test_a_validated_label_requires_the_postconditions_to_hold(self):
        from capability.index import BuildError
        with tempfile.TemporaryDirectory() as tmp:
            root = new_tree(Path(tmp))
            folder = add_fixture_tool(root, "python")
            # the fixture predates the tool contract, so give it a contract-shaped result
            (folder / "main.py").write_text('import json\nprint(json.dumps({"ok": True, "output": "x"}))\n', encoding="utf-8")
            manifest = tool_manifest("fixture-tool", [{"language": "python", "location": "library/fixture-tool/python"}])
            manifest["spec"]["postconditions"] = [{"type": "each_line_matches", "pattern": "NEVER"}]
            write_manifest(root, "tool", "fixture-tool", manifest)
            (root / "library" / "fixture-tool" / "conformance.json").write_text(json.dumps(
                [{"id": "double", "input": {"n": 3}, "expect": {"ok": True}}]), encoding="utf-8")
            with self.assertRaises(BuildError) as ctx:
                build_index(root, fixture_mode=True)
            self.assertTrue(any("postconditions/0" in e for e in ctx.exception.errors), ctx.exception.errors)


class MatchCarriesThePostconditionsTest(unittest.TestCase):
    def test_resolve_returns_them_in_the_match_spec(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            for folder in ("schemas", "policy", "catalog", "library", "evidence"):
                shutil.copytree(REPO / folder, root / folder)
            build_index(root)
            match = Registry(root).resolve("glob", languages=["python"])
        self.assertEqual(match["version"], "1.3.0")
        self.assertEqual(match["spec"]["postconditions"][0], {"type": "max_bytes", "value": 25000})
        json.dumps(match)


if __name__ == "__main__":
    unittest.main()
