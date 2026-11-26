"""M5: conformance runner and language registry (LANGUAGES.md v2 §5–§7, §9)."""

import json
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

from capability.conformance import (ConformanceError, RUNNERS, check_expectations,
                                    run_implementation)
from capability.languages import LanguageError, load_languages


def _declared(language: str):
    """The runner and toolchain declared for a language in the registry."""
    spec = load_languages(REPO)[language]
    return spec["runner"], spec["toolchain"]

from fixture_support import IMPLEMENTATIONS, REPO

CASES = json.loads((IMPLEMENTATIONS / "conformance.json").read_text(encoding="utf-8"))


def _toolchain_available(argv) -> bool:
    try:
        subprocess.run(argv, capture_output=True, check=False)
        return True
    except FileNotFoundError:
        return False


class RegistryTest(unittest.TestCase):
    def test_registry_loads_three_languages(self):
        languages = load_languages(REPO)
        self.assertEqual(sorted(languages), ["bash", "python", "typescript"])

    def test_missing_registry_is_an_error(self):
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(LanguageError):
                load_languages(Path(tmp))

    def test_language_id_must_be_lowercase(self):
        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp) / "policy").mkdir()
            (Path(tmp) / "policy" / "languages.yaml").write_text(
                "policy_version: 1\nlanguages:\n  Python:\n    extensions: [.py]\n    entry: main.py\n"
                "    runner: python\n    toolchain: [python3, --version]\n", encoding="utf-8")
            with self.assertRaises(LanguageError):
                load_languages(Path(tmp))


class OperatorTest(unittest.TestCase):
    """Each expectation operator, checked against a fixed completed-process value."""

    def _result(self, stdout: str, code: int = 0):
        return subprocess.CompletedProcess(args=[], returncode=code, stdout=stdout.encode(), stderr=b"")

    def test_ok_passes_and_fails(self):
        self.assertEqual(check_expectations(self._result('{"ok": true}'), {"ok": True}), [])
        self.assertTrue(check_expectations(self._result('{"ok": false}'), {"ok": True}))

    def test_contains_and_not_contains(self):
        self.assertEqual(check_expectations(self._result("abc"), {"contains": "b"}), [])
        self.assertTrue(check_expectations(self._result("abc"), {"contains": "z"}))
        self.assertEqual(check_expectations(self._result("abc"), {"not_contains": "z"}), [])
        self.assertTrue(check_expectations(self._result("abc"), {"not_contains": "b"}))

    def test_json_key_with_dotted_path(self):
        self.assertEqual(check_expectations(self._result('{"a": {"b": 1}}'), {"json_key": "a.b"}), [])
        self.assertTrue(check_expectations(self._result('{"a": {}}'), {"json_key": "a.b"}))
        self.assertTrue(check_expectations(self._result("not json"), {"json_key": "a"}))

    def test_exit_code(self):
        self.assertEqual(check_expectations(self._result("", 0), {"exit_code": 0}), [])
        self.assertTrue(check_expectations(self._result("", 2), {"exit_code": 0}))

    def test_unknown_operator_is_an_error_not_a_failure(self):
        with self.assertRaises(ConformanceError):
            check_expectations(self._result(""), {"regex": "x"})


class RunSafetyTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.base = Path(self._tmp.name)
        self.folder = self.base / "impl"
        shutil.copytree(IMPLEMENTATIONS / "python", self.folder)

    def tearDown(self):
        self._tmp.cleanup()

    def test_python_fixture_conforms_to_every_case(self):
        for case in CASES:
            with self.subTest(case=case["id"]):
                completed = run_implementation(self.folder, "main.py", "python", json.dumps(case["input"]),
                                               _declared("python")[1])
                self.assertEqual(check_expectations(completed, case["expect"]), [])

    def test_entry_outside_the_folder_is_refused(self):
        outside = self.base / "outside.py"
        outside.write_text("print('x')\n", encoding="utf-8")
        with self.assertRaises(ConformanceError):
            run_implementation(self.folder, "../outside.py", "python", "{}", _declared("python")[1])

    def test_entry_that_is_a_link_is_refused(self):
        (self.folder / "link.py").symlink_to(self.folder / "main.py")
        with self.assertRaises(ConformanceError):
            run_implementation(self.folder, "link.py", "python", "{}", _declared("python")[1])

    def test_unknown_runner_is_refused(self):
        with self.assertRaises(ConformanceError):
            run_implementation(self.folder, "main.py", "no-such-runner", "{}", _declared("python")[1])

    def test_environment_is_an_allow_list(self):
        (self.folder / "main.py").write_text(
            "import os, json\nprint(json.dumps({'ok': True, 'keys': sorted(os.environ)}))\n", encoding="utf-8")
        completed = run_implementation(self.folder, "main.py", "python", "{}", _declared("python")[1])
        keys = json.loads(completed.stdout)["keys"]
        self.assertNotIn("HOME", keys)
        # The interpreter may add LC_CTYPE itself; nothing else may be inherited.
        self.assertLessEqual(set(keys), {"PATH", "LC_CTYPE"})

    def test_output_beyond_the_bound_is_stopped_during_the_run(self):
        (self.folder / "main.py").write_text(
            "import sys\nwhile True:\n    sys.stdout.write('x' * 65536)\n    sys.stdout.flush()\n",
            encoding="utf-8")
        with self.assertRaises(ConformanceError) as ctx:
            run_implementation(self.folder, "main.py", "python", "{}", _declared("python")[1])
        self.assertIn("stdout exceeded", str(ctx.exception))

    def test_stderr_is_bounded_too(self):
        (self.folder / "main.py").write_text(
            "import sys\nwhile True:\n    sys.stderr.write('e' * 65536)\n    sys.stderr.flush()\n",
            encoding="utf-8")
        with self.assertRaises(ConformanceError) as ctx:
            run_implementation(self.folder, "main.py", "python", "{}", _declared("python")[1])
        self.assertIn("stderr exceeded", str(ctx.exception))

    def test_missing_toolchain_is_reported_as_unavailable(self):
        with self.assertRaises(ConformanceError) as ctx:
            run_implementation(self.folder, "main.py", "python", "{}",
                               toolchain=["no-such-toolchain-binary", "--version"])
        self.assertIn("toolchain unavailable", str(ctx.exception))

    def test_failing_toolchain_is_reported_as_unavailable(self):
        with self.assertRaises(ConformanceError) as ctx:
            run_implementation(self.folder, "main.py", "python", "{}",
                               toolchain=["python3", "-c", "import sys; sys.exit(3)"])
        self.assertIn("toolchain unavailable", str(ctx.exception))

    def test_child_that_never_reads_large_input_is_stopped(self):
        import capability.conformance as conformance
        (self.folder / "main.py").write_text("import time\ntime.sleep(60)\n", encoding="utf-8")
        original = conformance.TIMEOUT_SECONDS
        conformance.TIMEOUT_SECONDS = 1
        try:
            with self.assertRaises(ConformanceError) as ctx:
                run_implementation(self.folder, "main.py", "python", "x" * (64 * 1024), _declared("python")[1])
            self.assertIn("exceeded", str(ctx.exception))
        finally:
            conformance.TIMEOUT_SECONDS = original

    def test_oversized_input_is_refused(self):
        with self.assertRaises(ConformanceError) as ctx:
            run_implementation(self.folder, "main.py", "python", "x" * (64 * 1024 + 1), _declared("python")[1])
        self.assertIn("input exceeds", str(ctx.exception))

    def test_a_missing_toolchain_argument_is_refused(self):
        with self.assertRaises(ConformanceError):
            run_implementation(self.folder, "main.py", "python", "{}", [])

    def test_hung_implementation_times_out(self):
        (self.folder / "main.py").write_text("import time\ntime.sleep(60)\n", encoding="utf-8")
        import capability.conformance as conformance
        original = conformance.TIMEOUT_SECONDS
        conformance.TIMEOUT_SECONDS = 1
        try:
            with self.assertRaises(ConformanceError) as ctx:
                run_implementation(self.folder, "main.py", "python", "{}", _declared("python")[1])
            self.assertIn("exceeded", str(ctx.exception))
        finally:
            conformance.TIMEOUT_SECONDS = original


class BashConformanceTest(unittest.TestCase):
    def test_bash_fixture_conforms_to_every_case(self):
        folder = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, folder, True)
        shutil.copytree(IMPLEMENTATIONS / "bash", folder, dirs_exist_ok=True)
        for case in CASES:
            with self.subTest(case=case["id"]):
                completed = run_implementation(folder, "main.sh", "bash", json.dumps(case["input"]),
                                               _declared("bash")[1])
                self.assertEqual(check_expectations(completed, case["expect"]), [])


class TypeScriptConformanceTest(unittest.TestCase):
    def test_typescript_fixture_when_the_toolchain_supports_it(self):
        if not _toolchain_available(["node", "--version"]):
            self.skipTest("node is not installed")
        probe = subprocess.run(["node", "--experimental-strip-types", "-e", "0"], capture_output=True, check=False)
        if probe.returncode != 0:
            self.skipTest("node lacks --experimental-strip-types (needs Node 22.6+); "
                          "TypeScript conformance is unavailable on this host")
        folder = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, folder, True)
        shutil.copytree(IMPLEMENTATIONS / "typescript", folder, dirs_exist_ok=True)
        for case in CASES:
            with self.subTest(case=case["id"]):
                completed = run_implementation(folder, "main.ts", "node-strip-types", json.dumps(case["input"]),
                                               _declared("typescript")[1])
                self.assertEqual(check_expectations(completed, case["expect"]), [])


if __name__ == "__main__":
    unittest.main()
