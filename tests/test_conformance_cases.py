"""T-001 slice 3: conformance cases with repository fixtures, and the Validated label (policy v2)."""

import json
import os
import shutil
import tempfile
import unittest
from pathlib import Path

from capability.conformance import (
    ConformanceError,
    build_repository,
    cases_digest,
    load_cases,
    run_cases,
)
from capability.index import BuildError, build_index
from capability.languages import load_languages
from capability.registry import Registry, RegistryError

from fixture_support import REPO, add_fixture_tool, copy_suite, new_tree, tool_manifest, write_manifest

TOOLS = ("grep", "glob", "git-log", "unawaited-async")
TOOLCHAIN = ["python3", "--version"]


def write_cases(directory: Path, cases) -> Path:
    path = directory / "conformance.json"
    path.write_text(json.dumps(cases), encoding="utf-8")
    return path


class LoadCasesTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def refused(self, cases):
        with self.assertRaises(ConformanceError):
            load_cases(write_cases(self.dir, cases))

    def good(self, **extra):
        return {"id": "one", "input": {}, "expect": {"ok": True}, **extra}

    def test_a_valid_file_loads(self):
        self.assertEqual(len(load_cases(write_cases(self.dir, [self.good()]))), 1)

    def test_missing_unreadable_or_non_list_files_are_refused(self):
        with self.assertRaises(ConformanceError):
            load_cases(self.dir / "nope.json")
        (self.dir / "bad.json").write_text("{", encoding="utf-8")
        with self.assertRaises(ConformanceError):
            load_cases(self.dir / "bad.json")
        self.refused({"id": "x"})
        self.refused([])

    def test_bad_cases_are_refused(self):
        self.refused([self.good(), self.good()])  # duplicate id
        self.refused([{"input": {}, "expect": {"ok": True}}])  # no id
        self.refused([{"id": "a", "expect": {"ok": True}}])  # no input
        self.refused([{"id": "a", "input": {}}])  # no expect
        self.refused([{"id": "a", "input": {}, "expect": {}}])  # empty expect
        self.refused([{"id": "a", "input": {}, "expect": {"magic": 1}}])  # unknown operator
        self.refused([self.good(surprise=1)])  # unknown key
        self.refused([self.good(links={"l": "x"})])  # repository keys without files
        self.refused([self.good(git={"commits": 1})])
        self.refused([self.good(files=[1])])  # files must be an object
        self.refused(["text"])

    def test_the_digest_is_stable_and_changes_with_the_cases(self):
        one = load_cases(write_cases(self.dir, [self.good()]))
        same = load_cases(write_cases(self.dir, [self.good()]))
        other = load_cases(write_cases(self.dir, [self.good(expect={"ok": False})]))
        self.assertEqual(cases_digest(one), cases_digest(same))
        self.assertNotEqual(cases_digest(one), cases_digest(other))
        self.assertTrue(cases_digest(one).startswith("sha256:"))


class BuildRepositoryTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.base = Path(self._tmp.name).resolve()

    def tearDown(self):
        self._tmp.cleanup()

    def test_files_links_and_outside_files_are_built(self):
        case = {"files": {"a/b.txt": "hi"}, "links": {"link": "../outside"}, "outside": {"s.txt": "secret"}}
        root = build_repository(case, self.base)
        self.assertEqual((root / "a" / "b.txt").read_text(), "hi")
        self.assertTrue((root / "link").is_symlink())
        self.assertEqual((root / "link" / "s.txt").read_text(), "secret")
        self.assertEqual(root, self.base / "repo")

    def test_a_git_fixture_has_the_requested_commits(self):
        import subprocess
        root = build_repository({"files": {"r.txt": "x"}, "git": {"commits": 3}}, self.base)
        log = subprocess.run(["git", "log", "--oneline"], cwd=root, capture_output=True, text=True, check=True)
        self.assertEqual(len(log.stdout.splitlines()), 3)

    def test_a_git_fixture_with_no_commits_is_an_empty_repository(self):
        import subprocess
        root = build_repository({"files": {}, "git": {"commits": 0}}, self.base)
        self.assertTrue((root / ".git").is_dir())
        done = subprocess.run(["git", "log"], cwd=root, capture_output=True, text=True)
        self.assertNotEqual(done.returncode, 0)

    def test_a_link_made_earlier_cannot_be_written_through_by_a_later_entry(self):
        with tempfile.TemporaryDirectory() as scratch:
            case = {"files": {"a.txt": "x"}, "links": {"x": scratch, "x/new": "somewhere"}}
            with self.assertRaises(ConformanceError) as ctx:
                build_repository(case, self.base)
            self.assertIn("passes through a link", str(ctx.exception))
            self.assertEqual(os.listdir(scratch), [])  # nothing was written outside the temporary repository

    def test_a_link_through_a_link_to_a_nested_directory_is_refused_too(self):
        with tempfile.TemporaryDirectory() as scratch:
            case = {"files": {}, "links": {"x": scratch, "x/deep/er/new": "t"}}
            with self.assertRaises(ConformanceError):
                build_repository(case, self.base)
            self.assertEqual(os.listdir(scratch), [])

    def test_a_link_whose_name_is_an_existing_directory_or_file_is_refused(self):
        for case in ({"files": {"x/y": "1"}, "links": {"x": "t"}}, {"files": {"x": "1"}, "links": {"x": "t"}},
                     {"files": {}, "links": {"x": "t"}, "outside": {}}):
            with self.subTest(case=str(case)[:40]):
                with tempfile.TemporaryDirectory() as tmp:
                    if case["links"] == {"x": "t"} and case["files"] == {}:
                        continue  # a plain link on its own is fine; covered by the next test
                    with self.assertRaises(ConformanceError) as ctx:
                        build_repository(case, Path(tmp).resolve())
                    self.assertIn("already exists", str(ctx.exception))

    def test_a_path_through_an_existing_file_is_refused(self):
        with self.assertRaises(ConformanceError) as ctx:
            build_repository({"files": {"a": "1", "a/b": "2"}}, self.base)
        self.assertIn("passes through a file", str(ctx.exception))

    def test_a_path_with_no_components_is_a_typed_failure_not_a_crash(self):
        for key in ("files", "links", "outside"):
            for name in (".", "./", "./."):
                with self.subTest(key=key, name=name):
                    case = {"files": {"a.txt": "x"}}
                    case[key] = {name: "x"}
                    with tempfile.TemporaryDirectory() as tmp:
                        with self.assertRaises(ConformanceError) as ctx:
                            build_repository(case, Path(tmp).resolve())
                        self.assertIn("not the repository itself", str(ctx.exception))

    def test_a_dot_in_a_case_fails_the_case_through_run_cases(self):
        case = {"id": "dot", "input": {"pattern": "x", "files": "*.txt"}, "expect": {"ok": True}, "files": {".": "x"}}
        failures = run_cases(REPO / "library" / "grep" / "python", "main.py", "python", TOOLCHAIN, [case])
        self.assertEqual(len(failures), 1)
        self.assertIn("dot: files: '.' must name a path inside the repository", failures[0])

    def test_a_plain_link_and_ordinary_nested_files_still_work(self):
        root = build_repository({"files": {"d/e/f.txt": "x"}, "links": {"d/l": "e/f.txt"}}, self.base)
        self.assertEqual((root / "d" / "l").read_text(), "x")

    def test_a_case_that_tries_it_fails_the_case_and_writes_nothing_outside(self):
        with tempfile.TemporaryDirectory() as scratch:
            case = {"id": "evil", "input": {"pattern": "x", "files": "*.txt"}, "expect": {"ok": True},
                    "files": {"a.txt": "x"}, "links": {"x": scratch, "x/new": "t"}}
            failures = run_cases(REPO / "library" / "grep" / "python", "main.py", "python", TOOLCHAIN, [case])
            self.assertEqual(len(failures), 1)
            self.assertIn("evil: links: x/new passes through a link", failures[0])
            self.assertEqual(os.listdir(scratch), [])

    def test_paths_that_leave_the_repository_are_refused(self):
        for case in ({"files": {"../x": "1"}}, {"files": {"/etc/x": "1"}},
                     {"files": {"a": "1"}, "links": {"../l": "x"}},
                     {"files": {}, "outside": {"../o": "1"}}, {"files": {"": "1"}}):
            with self.subTest(case=case):
                with tempfile.TemporaryDirectory() as tmp:
                    with self.assertRaises(ConformanceError):
                        build_repository(case, Path(tmp).resolve())


class RealToolCasesTest(unittest.TestCase):
    """Every real tool passes its own cases, and a changed tool does not."""

    def test_each_real_tool_passes_all_its_cases(self):
        for tool in TOOLS:
            with self.subTest(tool=tool):
                cases = load_cases(REPO / "library" / tool / "conformance.json")
                self.assertGreaterEqual(len(cases), 5)
                self.assertEqual(run_cases(REPO / "library" / tool / "python", "main.py", "python", TOOLCHAIN, cases), [])

    def test_a_broken_tool_fails_its_cases_and_names_them(self):
        cases = load_cases(REPO / "library" / "grep" / "conformance.json")
        with tempfile.TemporaryDirectory() as tmp:
            folder = Path(tmp) / "python"
            shutil.copytree(REPO / "library" / "grep" / "python", folder)
            main = folder / "main.py"
            main.write_text(main.read_text(encoding="utf-8").replace('"--",', '"--include=*.never",'), encoding="utf-8")
            failures = run_cases(folder, "main.py", "python", TOOLCHAIN, cases)
        self.assertTrue(failures)
        self.assertTrue(all(":" in item for item in failures))

    def test_a_tool_that_ignores_confinement_fails_the_escape_cases(self):
        cases = load_cases(REPO / "library" / "glob" / "conformance.json")
        with tempfile.TemporaryDirectory() as tmp:
            folder = Path(tmp) / "python"
            shutil.copytree(REPO / "library" / "glob" / "python", folder)
            main = folder / "main.py"
            text = main.read_text(encoding="utf-8")
            self.assertIn("def confine(root, requested):\n", text)
            main.write_text(text.replace("def confine(root, requested):\n",
                                         "def confine(root, requested):\n    return os.path.join(root, requested), requested\n", 1),
                            encoding="utf-8")
            failures = run_cases(folder, "main.py", "python", TOOLCHAIN, cases)
        self.assertTrue(any(item.startswith("parent-directory-is-refused") for item in failures), failures)

    def test_a_missing_toolchain_is_an_error_not_a_failure(self):
        cases = load_cases(REPO / "library" / "grep" / "conformance.json")
        with self.assertRaises(ConformanceError):
            run_cases(REPO / "library" / "grep" / "python", "main.py", "python", ["no-such-toolchain-xyz", "--version"], cases)


class ValidatedLabelTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def real_catalog(self, evidence=False):
        """The real catalog. Without ``evidence`` the tools can reach Validated and no further."""
        for folder in ("schemas", "policy", "catalog", "library") + (("evidence",) if evidence else ()):
            shutil.copytree(REPO / folder, self.root / folder)
        if evidence:
            copy_suite(self.root)
        return self.root

    def fixture_tool(self, with_cases=None):
        new_tree(self.root)
        add_fixture_tool(self.root, "python")
        write_manifest(self.root, "tool", "fixture-tool", tool_manifest(
            "fixture-tool", [{"language": "python", "location": "library/fixture-tool/python"}]))
        if with_cases is not None:
            write_cases(self.root / "library" / "fixture-tool", with_cases)

    def entries(self, **kwargs):
        return {e["name"]: e for e in build_index(self.root, **kwargs)["entries"]}

    def test_the_four_real_tools_are_validated_with_the_cases_digest_as_evidence(self):
        self.real_catalog()
        entries = self.entries()
        for tool in TOOLS:
            with self.subTest(tool=tool):
                cases = load_cases(self.root / "library" / tool / "conformance.json")
                self.assertEqual(entries[tool]["label"], "Validated")
                self.assertEqual(entries[tool]["evidence"], ["conformance:" + cases_digest(cases)])

    def test_with_the_committed_evidence_the_four_real_tools_are_tested(self):
        self.real_catalog(evidence=True)
        entries = self.entries()
        for tool in TOOLS:
            with self.subTest(tool=tool):
                self.assertEqual(entries[tool]["label"], "Tested")
                self.assertEqual(len(entries[tool]["evidence"]), 3)
                self.assertTrue(entries[tool]["evidence"][1].startswith("tests:sha256:"))
                self.assertTrue(entries[tool]["evidence"][2].startswith("record:sha256:"))
        self.assertEqual(entries["graph-query"]["label"], "Declared")

    def test_a_changed_real_tool_loses_tested_but_keeps_validated(self):
        self.real_catalog(evidence=True)
        main = self.root / "library" / "grep" / "python" / "main.py"
        main.write_text(main.read_text(encoding="utf-8") + "\n# a comment changes the identity\n", encoding="utf-8")
        entry = self.entries(fixture_mode=True)["grep"]
        self.assertEqual(entry["label"], "Validated")

    def test_resolve_with_evidence_meets_a_tested_floor(self):
        self.real_catalog(evidence=True)
        build_index(self.root)
        self.assertEqual(Registry(self.root).resolve("grep", languages=["python"], minLabel="Tested")["label"], "Tested")

    def test_graph_query_has_no_implementation_so_it_stays_declared(self):
        self.real_catalog()
        entry = self.entries()["graph-query"]
        self.assertEqual((entry["label"], entry["evidence"]), ("Declared", []))

    def test_the_index_records_policy_version_two(self):
        self.real_catalog()
        self.assertEqual(build_index(self.root)["policy_version"], 2)

    def test_a_tool_without_a_case_file_stays_declared(self):
        self.fixture_tool(with_cases=None)
        entry = self.entries(fixture_mode=True)["fixture-tool"]
        self.assertEqual((entry["label"], entry["evidence"]), ("Declared", []))

    def test_a_tool_whose_cases_pass_is_validated(self):
        self.fixture_tool(with_cases=[{"id": "double", "input": {"n": 3}, "expect": {"ok": True, "contains": "\"double\": 6"}}])
        self.assertEqual(self.entries(fixture_mode=True)["fixture-tool"]["label"], "Validated")

    def test_a_present_but_invalid_case_file_refuses_the_build_unlike_an_absent_one(self):
        for number, kind in enumerate(("directory", "broken link", "link to a valid file")):
            with self.subTest(kind=kind):
                self.root = Path(self._tmp.name) / f"case-{number}"  # a fresh tree for each kind
                self.root.mkdir()
                self.fixture_tool(with_cases=None)
                path = self.root / "library" / "fixture-tool" / "conformance.json"
                if kind == "directory":
                    path.mkdir()
                elif kind == "broken link":
                    path.symlink_to(self.root / "nowhere.json")
                else:
                    real = self.root / "real-cases.json"
                    real.write_text(json.dumps([{"id": "double", "input": {"n": 3}, "expect": {"ok": True}}]), encoding="utf-8")
                    path.symlink_to(real)
                with self.assertRaises(BuildError) as ctx:
                    build_index(self.root, fixture_mode=True)
                self.assertTrue(any("present but is not a regular file" in e for e in ctx.exception.errors), ctx.exception.errors)

    def test_present_cases_with_no_implementation_refuse_the_build(self):
        for number, kind in enumerate(("valid file", "directory", "broken link", "link to a valid file")):
            with self.subTest(kind=kind):
                self.root = Path(self._tmp.name) / f"noimpl-{number}"
                self.root.mkdir()
                new_tree(self.root)
                write_manifest(self.root, "tool", "fixture-tool", tool_manifest("fixture-tool", []))
                folder = self.root / "library" / "fixture-tool"
                folder.mkdir(parents=True)
                path = folder / "conformance.json"
                cases = json.dumps([{"id": "double", "input": {"n": 3}, "expect": {"ok": True}}])
                if kind == "valid file":
                    path.write_text(cases, encoding="utf-8")
                elif kind == "directory":
                    path.mkdir()
                elif kind == "broken link":
                    path.symlink_to(self.root / "nowhere.json")
                else:
                    real = self.root / "real.json"
                    real.write_text(cases, encoding="utf-8")
                    path.symlink_to(real)
                with self.assertRaises(BuildError) as ctx:
                    build_index(self.root, fixture_mode=True)
                self.assertTrue(any("present but" in e for e in ctx.exception.errors), ctx.exception.errors)

    def test_no_implementation_and_no_cases_is_still_declared(self):
        new_tree(self.root)
        write_manifest(self.root, "tool", "fixture-tool", tool_manifest("fixture-tool", []))
        self.assertEqual(self.entries(fixture_mode=True)["fixture-tool"]["label"], "Declared")

    def test_a_case_that_names_the_repository_itself_fails_with_a_typed_explanation(self):
        self.fixture_tool(with_cases=[{"id": "dot", "input": {"n": 3}, "expect": {"ok": True}, "files": {".": "x"}}])
        with self.assertRaises(BuildError) as ctx:
            build_index(self.root, fixture_mode=True)
        self.assertTrue(any("not the repository itself" in e for e in ctx.exception.errors), ctx.exception.errors)

    def test_an_absent_case_file_is_declared_not_an_error(self):
        self.fixture_tool(with_cases=None)
        self.assertEqual(self.entries(fixture_mode=True)["fixture-tool"]["label"], "Declared")

    def test_a_tool_whose_cases_fail_refuses_the_build(self):
        self.fixture_tool(with_cases=[{"id": "wrong", "input": {"n": 3}, "expect": {"ok": True, "contains": "\"double\": 7"}}])
        with self.assertRaises(BuildError) as ctx:
            build_index(self.root, fixture_mode=True)
        self.assertTrue(any("fails its conformance cases" in e and "wrong" in e for e in ctx.exception.errors))

    def test_an_invalid_case_file_refuses_the_build(self):
        self.fixture_tool(with_cases=[{"id": "x"}])
        with self.assertRaises(BuildError) as ctx:
            build_index(self.root, fixture_mode=True)
        self.assertTrue(any("conformance could not be run" in e for e in ctx.exception.errors))

    def test_changing_the_cases_changes_the_evidence_and_the_index_digest(self):
        cases = [{"id": "double", "input": {"n": 3}, "expect": {"ok": True}}]
        self.fixture_tool(with_cases=cases)
        first = build_index(self.root, fixture_mode=True)
        write_cases(self.root / "library" / "fixture-tool", cases + [{"id": "zero", "input": {"n": 0}, "expect": {"ok": True}}])
        second = build_index(self.root, fixture_mode=True)
        self.assertNotEqual(first["entries"][0]["evidence"], second["entries"][0]["evidence"])
        self.assertNotEqual(first["index_digest"], second["index_digest"])

    def test_resolve_returns_the_validated_label_and_min_label_works(self):
        self.real_catalog()
        build_index(self.root)
        registry = Registry(self.root)
        self.assertEqual(registry.resolve("grep", languages=["python"], minLabel="Validated")["label"], "Validated")
        with self.assertRaises(RegistryError) as ctx:
            registry.resolve("graph-query", minLabel="Validated", languages=["python"])
        self.assertEqual(ctx.exception.code, "BelowLabel")
        with self.assertRaises(RegistryError) as ctx:
            registry.resolve("grep", minLabel="Tested", languages=["python"])
        self.assertEqual(ctx.exception.code, "BelowLabel")

    def test_a_changed_byte_in_a_validated_tool_is_an_integrity_error_at_resolve(self):
        self.real_catalog()
        build_index(self.root)
        main = self.root / "library" / "glob" / "python" / "main.py"
        main.write_text(main.read_text(encoding="utf-8") + "\n# changed\n", encoding="utf-8")
        with self.assertRaises(RegistryError) as ctx:
            Registry(self.root).resolve("glob", languages=["python"])
        self.assertEqual(ctx.exception.code, "IntegrityError")


if __name__ == "__main__":
    unittest.main()
