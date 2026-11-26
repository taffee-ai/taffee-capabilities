"""T-001 slice 1: the four Python tool implementations (CAP-T-3 to CAP-T-9, CAP-T-13).

Every call goes through the S-001 conformance runner, so the tools are exercised the way the
platform runs them: JSON on stdin, PATH-only environment, bounded output, a deadline.
"""

import importlib.util
import json
import os
import subprocess
import tempfile
import time
import unittest
from pathlib import Path

from capability.conformance import run_implementation

from fixture_support import REPO

LIB = REPO / "library"
TOOLS = ("grep", "glob", "git-log", "unawaited-async")
TOOLCHAIN = ["python3", "--version"]
MARKER = "[truncated: output exceeded 24576 bytes]"
GIT_ENV = {
    "GIT_AUTHOR_NAME": "Test", "GIT_AUTHOR_EMAIL": "test@example.invalid",
    "GIT_COMMITTER_NAME": "Test", "GIT_COMMITTER_EMAIL": "test@example.invalid",
    "GIT_AUTHOR_DATE": "2026-01-01T00:00:00Z", "GIT_COMMITTER_DATE": "2026-01-01T00:00:00Z",
}


def call(tool, root, request_input, raw=None):
    """Run one tool through the runner. Returns (exit code, parsed result)."""
    stdin = raw if raw is not None else json.dumps({"root": str(root), "input": request_input})
    done = run_implementation(LIB / tool / "python", "main.py", "python", stdin, TOOLCHAIN)
    return done.returncode, json.loads(done.stdout.decode("utf-8"))


def git(root, *args):
    subprocess.run(["git", *args], cwd=root, env={**os.environ, **GIT_ENV}, check=True, capture_output=True)


def load_tool(tool):
    spec = importlib.util.spec_from_file_location("t001_" + tool.replace("-", "_"), LIB / tool / "python" / "main.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class ToolCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name).resolve() / "repo"
        self.root.mkdir()
        self.outside = Path(self._tmp.name).resolve() / "outside"
        self.outside.mkdir()
        (self.outside / "secret.txt").write_text("secret\n", encoding="utf-8")

    def tearDown(self):
        self._tmp.cleanup()

    def write(self, relative, text):
        path = self.root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")

    def ok(self, tool, request_input):
        code, result = call(tool, self.root, request_input)
        self.assertEqual(code, 0)
        self.assertTrue(result["ok"], result)
        return result["output"]

    def error(self, tool, request_input):
        code, result = call(tool, self.root, request_input)
        self.assertEqual(code, 0)
        self.assertFalse(result["ok"], result)
        return result["error"]


class ContractTest(ToolCase):
    """CAP-T-4: invalid input and a missing root are results, never crashes."""

    VALID = {"grep": {"pattern": "x", "files": "*.txt"}, "glob": {"pattern": "*.txt"},
             "git-log": {"n": 1}, "unawaited-async": {"files": "*.ts"}}

    def test_not_json_is_an_error_result(self):
        for tool in TOOLS:
            with self.subTest(tool=tool):
                code, result = call(tool, self.root, None, raw="not json")
                self.assertEqual((code, result["ok"]), (0, False))

    def test_request_that_is_not_an_object_is_an_error_result(self):
        for tool in TOOLS:
            with self.subTest(tool=tool):
                code, result = call(tool, self.root, None, raw="[1, 2]")
                self.assertEqual((code, result["ok"]), (0, False))

    def test_missing_or_bad_root_is_an_error_result(self):
        for tool in TOOLS:
            for raw in (json.dumps({"input": self.VALID[tool]}),
                        json.dumps({"root": "", "input": self.VALID[tool]}),
                        json.dumps({"root": 5, "input": self.VALID[tool]}),
                        json.dumps({"root": str(self.root / "nope"), "input": self.VALID[tool]}),
                        json.dumps({"root": str(self.outside / "secret.txt"), "input": self.VALID[tool]})):
                with self.subTest(tool=tool, raw=raw):
                    code, result = call(tool, self.root, None, raw=raw)
                    self.assertEqual((code, result["ok"]), (0, False))
                    self.assertIn("root", result["error"])

    def test_input_that_is_not_an_object_is_an_error_result(self):
        for tool in TOOLS:
            with self.subTest(tool=tool):
                code, result = call(tool, self.root, None, raw=json.dumps({"root": str(self.root), "input": "x"}))
                self.assertEqual((code, result["ok"]), (0, False))

    def test_wrong_typed_or_missing_fields_are_error_results(self):
        bad = {"grep": [{}, {"pattern": 1, "files": "*.txt"}, {"pattern": "x"}, {"pattern": "x", "files": ""}],
               "glob": [{}, {"pattern": 1}, {"pattern": ""}],
               "git-log": [{}, {"n": "3"}, {"n": None}],
               "unawaited-async": [{}, {"files": 1}, {"files": ""}]}
        for tool, inputs in bad.items():
            for request_input in inputs:
                with self.subTest(tool=tool, request_input=request_input):
                    self.error(tool, request_input)

    def test_every_tool_leaves_nothing_behind_in_the_repository(self):
        self.write("a.txt", "x\n")
        before = sorted(p.name for p in self.root.rglob("*"))
        for tool in ("grep", "glob", "unawaited-async"):
            call(tool, self.root, self.VALID[tool])
        self.assertEqual(sorted(p.name for p in self.root.rglob("*")), before)


class PathConfinementTest(ToolCase):
    """CAP-T-5: a path outside root, an escaping symlink, and an absolute path are refused."""

    CASES = {"grep": lambda p: {"pattern": "secret", "files": p + "/**/*.txt"},
             "glob": lambda p: {"pattern": p + "/**/*.txt"},
             "unawaited-async": lambda p: {"files": p + "/**/*.ts"}}

    def test_parent_directory_escape_is_refused(self):
        for tool, make in self.CASES.items():
            with self.subTest(tool=tool):
                self.assertIn("escapes", self.error(tool, make("..")))
                self.assertIn("escapes", self.error(tool, make("../outside")))

    def test_absolute_path_is_refused(self):
        for tool, make in self.CASES.items():
            with self.subTest(tool=tool):
                self.assertIn("escapes", self.error(tool, make(str(self.outside))))

    def test_symlink_that_escapes_is_refused(self):
        (self.root / "link").symlink_to(self.outside)
        for tool, make in self.CASES.items():
            with self.subTest(tool=tool):
                self.assertIn("symlink", self.error(tool, make("link")))

    def test_a_missing_directory_is_an_error_not_a_crash(self):
        for tool, make in self.CASES.items():
            with self.subTest(tool=tool):
                self.assertIn("does not exist", self.error(tool, make("nowhere")))

    def test_dot_dot_that_stays_inside_the_repository_is_allowed(self):
        self.write("sub/a.txt", "secret\n")
        self.assertIn("sub/a.txt", self.ok("glob", {"pattern": "sub/../**/*.txt"}))

    def test_symlinked_files_inside_the_tree_are_not_followed_out(self):
        (self.root / "leak.txt").symlink_to(self.outside / "secret.txt")
        self.write("real.txt", "secret\n")
        self.assertEqual(self.ok("glob", {"pattern": "**/*.txt"}), "real.txt")
        found = self.ok("grep", {"pattern": "secret", "files": "**/*.txt"})
        self.assertIn("real.txt:1:secret", found)  # "./" prefix at the root
        self.assertNotIn("leak.txt", found)


class GrepTest(ToolCase):
    def test_finds_matching_lines_with_file_and_line_number(self):
        self.write("a.txt", "hello\nworld\n")
        self.write("b.txt", "nothing\n")
        # At the repository root grep prints "./a.txt", exactly as the workbench tool does (parity).
        self.assertEqual(self.ok("grep", {"pattern": "hello", "files": "**/*.txt"}), "./a.txt:1:hello")

    def test_no_match_is_an_empty_result_not_an_error(self):
        self.write("a.txt", "hello\n")
        self.assertEqual(self.ok("grep", {"pattern": "absent", "files": "**/*.txt"}), "")

    def test_the_directory_prefix_scopes_the_search(self):
        self.write("in/a.txt", "needle\n")
        self.write("out/b.txt", "needle\n")
        self.assertEqual(self.ok("grep", {"pattern": "needle", "files": "in/**/*.txt"}), "in/a.txt:1:needle")

    def test_a_pattern_without_double_star_is_not_recursive_in_scope_but_searches_the_directory(self):
        self.write("in/a.txt", "needle\n")
        self.write("in/deep/c.txt", "needle\n")
        found = self.ok("grep", {"pattern": "needle", "files": "in/*.txt"}).splitlines()
        self.assertIn("in/a.txt:1:needle", found)

    def test_a_pattern_starting_with_a_dash_is_data_not_an_option(self):
        self.write("a.txt", "-v flag\n")
        self.assertEqual(self.ok("grep", {"pattern": "-v", "files": "**/*.txt"}), "./a.txt:1:-v flag")

    def test_an_invalid_regular_expression_is_an_error_result(self):
        self.write("a.txt", "x\n")
        self.assertIn("grep failed", self.error("grep", {"pattern": "[", "files": "**/*.txt"}))

    def test_output_is_bounded_with_a_truncation_marker(self):
        self.write("big.txt", "".join("match line %05d padding padding\n" % i for i in range(3000)))
        out = self.ok("grep", {"pattern": "match", "files": "**/*.txt"})
        self.assertTrue(out.endswith(MARKER), out[-80:])
        self.assertLessEqual(len(out.encode("utf-8")), 24 * 1024 + 100)
        self.assertIn("big.txt:1:match line 00000", out)


class GlobTest(ToolCase):
    def test_lists_matching_files_sorted(self):
        for name in ("b.ts", "a.ts", "sub/c.ts", "sub/d.js"):
            self.write(name, "")
        self.assertEqual(self.ok("glob", {"pattern": "**/*.ts"}), "a.ts\nb.ts\nsub/c.ts")

    def test_the_directory_prefix_scopes_the_search(self):
        self.write("in/a.ts", "")
        self.write("out/b.ts", "")
        self.assertEqual(self.ok("glob", {"pattern": "in/**/*.ts"}), "in/a.ts")

    def test_without_double_star_only_one_level_is_searched(self):
        self.write("sub/a.ts", "")
        self.write("sub/deep/b.ts", "")
        self.assertEqual(self.ok("glob", {"pattern": "sub/*.ts"}), "sub/a.ts")

    def test_no_match_is_empty_output_not_an_error(self):
        self.assertEqual(self.ok("glob", {"pattern": "**/*.nothing"}), "")

    def test_paths_are_relative_never_absolute(self):
        self.write("a.ts", "")
        self.assertFalse(self.ok("glob", {"pattern": "**/*.ts"}).startswith("/"))

    def test_a_directory_beginning_with_a_dash_is_not_taken_as_an_option(self):
        self.write("-odd/a.ts", "")
        self.assertEqual(self.ok("glob", {"pattern": "-odd/**/*.ts"}), "-odd/a.ts")

    def test_a_result_larger_than_the_bound_is_truncated_with_a_marker(self):
        for i in range(2600):
            self.write("many/file%05d.txt" % i, "")
        out = self.ok("glob", {"pattern": "**/*.txt"})
        self.assertTrue(out.endswith(MARKER), out[-80:])
        self.assertLessEqual(len(out.encode("utf-8")), 24 * 1024 + 100)


class GitLogTest(ToolCase):
    def make_repo(self, commits):
        git(self.root, "init", "-q", "-b", "main")
        for index in range(commits):
            self.write("f.txt", "change %d\n" % index)
            git(self.root, "add", "f.txt")
            git(self.root, "commit", "-q", "-m", "commit number %d" % index)

    def test_a_repository_with_commits_returns_the_most_recent_first(self):
        self.make_repo(5)
        lines = self.ok("git-log", {"n": 3}).splitlines()
        self.assertEqual(len(lines), 3)
        self.assertTrue(lines[0].endswith("commit number 4"))
        self.assertTrue(lines[2].endswith("commit number 2"))

    def test_n_larger_than_the_history_returns_what_exists(self):
        self.make_repo(2)
        self.assertEqual(len(self.ok("git-log", {"n": 50}).splitlines()), 2)

    def test_an_empty_repository_is_empty_output_not_an_error(self):
        git(self.root, "init", "-q", "-b", "main")
        self.assertEqual(self.ok("git-log", {"n": 3}), "")

    def test_a_directory_that_is_not_a_repository_is_an_error_result(self):
        self.assertIn("git log failed", self.error("git-log", {"n": 3}))

    def test_n_outside_one_to_one_hundred_or_not_an_integer_is_refused(self):
        self.make_repo(1)
        for bad in (0, -1, 101, 2.5, True, "3", None, [1]):
            with self.subTest(n=bad):
                self.assertIn("whole number", self.error("git-log", {"n": bad}))

    def test_the_boundaries_one_and_one_hundred_are_accepted(self):
        self.make_repo(1)
        self.ok("git-log", {"n": 1})
        self.ok("git-log", {"n": 100})

    def test_the_workbench_test_hang_backdoor_is_not_ported(self):
        self.make_repo(1)
        started = time.monotonic()
        self.ok("git-log", {"n": 1, "timeout": True})
        self.assertLess(time.monotonic() - started, 3)


class UnawaitedAsyncTest(ToolCase):
    def test_flags_an_async_function_with_no_await(self):
        self.write("x.ts", "async function load() {\n  return 1;\n}\n")
        self.assertEqual(self.ok("unawaited-async", {"files": "**/*.ts"}),
                         "x.ts:1 — async function with no await")

    def test_an_async_function_that_awaits_is_not_flagged(self):
        self.write("y.ts", "async function load() {\n  await fetch();\n}\n")
        self.assertEqual(self.ok("unawaited-async", {"files": "**/*.ts"}),
                         "No unawaited async functions found.")

    def test_flags_an_async_arrow_function(self):
        self.write("z.ts", "const f = async () => {\n  return 1;\n};\n")
        self.assertIn("z.ts:1", self.ok("unawaited-async", {"files": "**/*.ts"}))

    def test_flags_an_async_method(self):
        self.write("m.ts", "class A {\n  async run() {\n    return 1;\n  }\n}\n")
        self.assertIn("m.ts:2", self.ok("unawaited-async", {"files": "**/*.ts"}))

    def test_an_await_inside_a_comment_does_not_count(self):
        self.write("c.ts", "async function f() {\n  // await later\n  return 1;\n}\n")
        self.assertIn("c.ts:1", self.ok("unawaited-async", {"files": "**/*.ts"}))

    def test_a_commented_out_declaration_is_ignored(self):
        self.write("d.ts", "// async function f() {\n//   return 1;\n// }\n")
        self.assertEqual(self.ok("unawaited-async", {"files": "**/*.ts"}), "No unawaited async functions found.")

    def test_the_word_async_in_a_string_is_not_a_declaration(self):
        self.write("s.ts", 'import { x } from "./tools/unawaited-async.ts";\nconst y = {\n  a: 1,\n};\n')
        self.assertEqual(self.ok("unawaited-async", {"files": "**/*.ts"}), "No unawaited async functions found.")

    def test_only_files_matching_the_pattern_are_read(self):
        self.write("in/a.ts", "async function f() {\n  return 1;\n}\n")
        self.write("out/b.ts", "async function g() {\n  return 1;\n}\n")
        out = self.ok("unawaited-async", {"files": "in/**/*.ts"})
        self.assertIn("in/a.ts:1", out)
        self.assertNotIn("out/b.ts", out)

    def test_findings_are_bounded_with_a_truncation_marker(self):
        self.write("many.ts", "".join("async function f%d() {\n  return 1;\n}\n" % i for i in range(1500)))
        out = self.ok("unawaited-async", {"files": "**/*.ts"})
        self.assertTrue(out.endswith(MARKER), out[-80:])

    def test_invalid_utf8_in_a_file_does_not_crash_the_tool(self):
        (self.root / "bin.ts").write_bytes(b"async function f() {\n  return \xff\xfe;\n}\n")
        self.assertIn("bin.ts:1", self.ok("unawaited-async", {"files": "**/*.ts"}))


class DeadlineTest(unittest.TestCase):
    """CAP-T-9: a call past the deadline is stopped, and the child process is gone afterwards."""

    def child_running(self, marker):
        for entry in Path("/proc").iterdir():
            if not entry.name.isdigit():
                continue
            try:
                cmdline = (entry / "cmdline").read_bytes()
            except OSError:
                continue
            if marker.encode() in cmdline and b"sleep" in cmdline:
                return True
        return False

    def test_a_run_past_the_deadline_is_killed_in_every_tool(self):
        for tool in TOOLS:
            with self.subTest(tool=tool):
                module = load_tool(tool)
                module.TIMEOUT_SECONDS = 0.4
                marker = "31.%d" % os.getpid()
                started = time.monotonic()
                with self.assertRaises(module.ToolError) as ctx:
                    module.run_bounded(["sleep", marker], "/")
                self.assertIn("timed out", str(ctx.exception))
                self.assertLess(time.monotonic() - started, 3)
                self.assertFalse(self.child_running(marker))

    def test_endless_output_is_cut_off_and_the_child_is_killed(self):
        for tool in TOOLS:
            with self.subTest(tool=tool):
                module = load_tool(tool)
                started = time.monotonic()
                code, text, _ = module.run_bounded(["yes"], "/")
                self.assertLess(time.monotonic() - started, 3)
                self.assertTrue(text.endswith(MARKER))
                self.assertLessEqual(len(text.encode()), 24 * 1024 + 100)


class SelfContainedTest(unittest.TestCase):
    """Each implementation folder holds only its entry, so its code_digest covers all it runs."""

    def test_each_folder_is_one_file_with_no_imports_from_the_repository(self):
        for tool in TOOLS:
            with self.subTest(tool=tool):
                files = sorted(p.name for p in (LIB / tool / "python").iterdir())
                self.assertEqual(files, ["main.py"])
                text = (LIB / tool / "python" / "main.py").read_text(encoding="utf-8")
                self.assertNotIn("capability", text.replace("capability-", ""))
                self.assertNotIn("shell=True", text)
                self.assertNotIn("os.system", text)


if __name__ == "__main__":
    unittest.main()
