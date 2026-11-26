"""T-001 slice 4: evidence records and the Tested label (policy v2)."""

import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from capability.canon import canonical_bytes
from capability.conformance import cases_digest, load_cases
from capability.evidence import (
    EVIDENCE_DIR,
    check_record,
    environment,
    evidence_digest,
    file_digest,
    fingerprint,
    run_suite,
    write_record,
)
from capability.implementations import check_implementations, tool_identity
from capability.index import build_index
from capability.labels import POLICY_VERSION
from capability.languages import load_languages
from capability.manifest import check_manifest_file

from fixture_support import TOOL_VERSIONS, add_fixture_tool, copy_suite, new_tree, tool_manifest, write_manifest

GOOD = {"run": 3, "passed": 3, "failed": 0, "errors": 0, "skipped": 0, "skipped_reasons": []}
CASES = [{"id": "double", "input": {"n": 3}, "expect": {"ok": True, "contains": "\"double\": 6"}}]
SUITE = "tests/suite.py"


class SuiteRunTest(unittest.TestCase):
    def run_source(self, source):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "tests").mkdir()
            (root / "tests" / "suite.py").write_text(source, encoding="utf-8")
            return run_suite(root, SUITE)

    def test_counts_for_a_clean_run(self):
        counts, log = self.run_source(
            "import unittest\nclass T(unittest.TestCase):\n    def test_a(self): pass\n    def test_b(self): pass\n")
        self.assertEqual(counts, {"run": 2, "passed": 2, "failed": 0, "errors": 0, "skipped": 0, "skipped_reasons": []})
        self.assertIn("OK", log)

    def test_failures_errors_and_skips_are_counted_with_reasons(self):
        counts, _ = self.run_source(
            "import unittest\nclass T(unittest.TestCase):\n"
            "    def test_ok(self): pass\n"
            "    def test_fail(self): self.fail('no')\n"
            "    def test_error(self): raise RuntimeError('x')\n"
            "    @unittest.skip('no node here')\n    def test_skip(self): pass\n")
        self.assertEqual((counts["run"], counts["passed"], counts["failed"], counts["errors"], counts["skipped"]),
                         (4, 1, 1, 1, 1))
        self.assertEqual(counts["skipped_reasons"], ["no node here"])

    def test_an_unexpected_success_counts_as_a_failure(self):
        counts, _ = self.run_source(
            "import unittest\nclass T(unittest.TestCase):\n    @unittest.expectedFailure\n    def test_x(self): pass\n")
        self.assertEqual(counts["failed"], 1)
        self.assertEqual(counts["passed"], 0)


class FingerprintTest(unittest.TestCase):
    BASE = ("sha256:" + "1" * 64, "sha256:" + "2" * 64, "sha256:" + "3" * 64, 2, {"python": "3.12.0"})

    def test_it_is_stable_and_changes_with_every_input(self):
        base = fingerprint(*self.BASE)
        self.assertEqual(base, fingerprint(*self.BASE))
        for index, changed in enumerate(("sha256:" + "9" * 64, "sha256:" + "9" * 64, "sha256:" + "9" * 64, 3, {"python": "3.13.0"})):
            args = list(self.BASE)
            args[index] = changed
            with self.subTest(field=index):
                self.assertNotEqual(base, fingerprint(*args))

    def test_the_environment_names_the_tools_the_output_depends_on(self):
        env = environment()
        self.assertEqual(set(env), {"python", "os", "arch", "git", "grep", "find"})
        self.assertTrue(env["python"] and env["os"] and env["arch"])


class RecordTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        (self.root / "tests").mkdir()
        (self.root / "tests" / "suite.py").write_text("# suite\n", encoding="utf-8")
        self.identity = "sha256:" + "a" * 64
        self.cases = "sha256:" + "b" * 64
        patcher = mock.patch("capability.evidence.APPROVED_SUITES", (SUITE,))
        patcher.start()
        self.addCleanup(patcher.stop)

    def tearDown(self):
        self._tmp.cleanup()

    def write(self, counts=GOOD, **override):
        args = dict(name="grep", version="1.2.0", identity=self.identity, cases_digest=self.cases,
                    policy_version=2, counts=counts, log="ok\n", suite=SUITE)
        args.update(override)
        return write_record(self.root, **args)

    def check(self, **override):
        args = dict(name="grep", version="1.2.0", identity=self.identity, cases_digest=self.cases, policy_version=2)
        args.update(override)
        return check_record(self.root, **args)

    def summary_path(self):
        return self.root / EVIDENCE_DIR / "grep" / "1.2.0" / "summary.json"

    def edit(self, change):
        data = json.loads(self.summary_path().read_text(encoding="utf-8"))
        change(data)
        self.summary_path().write_bytes(canonical_bytes(data))

    def test_a_good_record_holds_and_is_where_the_spec_says(self):
        folder = self.write()
        self.assertEqual(folder, self.root / "evidence" / "tools" / "grep" / "1.2.0")
        self.assertTrue((folder / "output.txt").is_file())
        self.assertIsNone(self.check())

    def test_the_record_names_the_suite_digest_and_the_environment(self):
        self.write()
        data = json.loads(self.summary_path().read_text(encoding="utf-8"))
        self.assertEqual(data["suite"], {"path": SUITE, "digest": file_digest(self.root / SUITE)})
        self.assertEqual(data["environment"], environment())
        self.assertEqual(data["schema"], "capability-evidence/1")

    def test_writing_twice_gives_the_same_fingerprint(self):
        self.write()
        first = json.loads(self.summary_path().read_text(encoding="utf-8"))["fingerprint"]
        self.write(log="a different log\n")
        self.assertEqual(first, json.loads(self.summary_path().read_text(encoding="utf-8"))["fingerprint"])

    def test_no_record_and_unreadable_records_do_not_hold(self):
        self.assertEqual(self.check(), "no evidence record")
        self.write()
        self.summary_path().write_text("{", encoding="utf-8")
        self.assertIn("not valid JSON", self.check())
        self.summary_path().write_text("[1]", encoding="utf-8")
        self.assertIn("wrong shape", self.check())

    def test_a_record_for_something_else_does_not_hold(self):
        self.write()
        for field, value in (("identity", "sha256:" + "c" * 64), ("cases_digest", "sha256:" + "c" * 64)):
            with self.subTest(field=field):
                self.assertIn(field, self.check(**{field: value}))
        self.assertIn("policy_version", self.check(policy_version=3))

    def test_a_record_whose_own_name_or_version_is_not_the_one_asked_for_does_not_hold(self):
        self.write()
        self.edit(lambda d: d.update(name="glob"))
        self.assertIn("different name", self.check())
        self.write()
        self.edit(lambda d: d.update(version="9.9.9"))
        self.assertIn("different version", self.check())

    def test_failed_errored_skipped_or_empty_runs_do_not_hold(self):
        for counts in ({**GOOD, "failed": 1, "passed": 2}, {**GOOD, "errors": 1, "passed": 2},
                       {**GOOD, "skipped": 1, "passed": 2, "skipped_reasons": ["x"]},
                       {"run": 0, "passed": 0, "failed": 0, "errors": 0, "skipped": 0, "skipped_reasons": []}):
            with self.subTest(counts=counts):
                self.write(counts=counts)
                self.assertIn("failed, errored or skipped", self.check())

    def test_counts_that_do_not_add_up_do_not_hold(self):
        self.write()
        self.edit(lambda d: d["tests"].update(run=99))
        self.assertIn("do not add up", self.check())

    def test_malformed_counts_do_not_hold(self):
        for bad in (True, -1, "3", None, 1.5):
            with self.subTest(value=bad):
                self.write()
                self.edit(lambda d, b=bad: d["tests"].update(passed=b))
                self.assertIn("malformed", self.check())

    def test_an_edited_environment_or_suite_digest_breaks_the_fingerprint(self):
        for change in (lambda d: d["environment"].update(git="git version 0.0"),
                       lambda d: d["suite"].update(digest="sha256:" + "d" * 64),
                       lambda d: d.update(fingerprint="sha256:" + "e" * 64)):
            with self.subTest():
                self.write()
                self.edit(change)
                self.assertIn("fingerprint", self.check())

    def test_edited_counts_with_the_fingerprint_intact_do_not_hold(self):
        self.write()
        self.edit(lambda d: d["tests"].update(run=4, passed=4))  # still adds up, still a clean run
        self.assertIn("evidence digest does not recompute", self.check())

    def test_a_replaced_or_missing_log_does_not_hold(self):
        self.write()
        log = self.summary_path().parent / "output.txt"
        log.write_text("a different log\n", encoding="utf-8")
        self.assertIn("does not match its recorded digest", self.check())
        log.unlink()
        self.assertIn("log (output.txt) is missing", self.check())

    def test_an_edited_log_digest_does_not_hold(self):
        self.write()
        self.edit(lambda d: d.update(log_digest="sha256:" + "0" * 64))
        self.assertIn("evidence digest does not recompute", self.check())

    def test_malformed_digests_do_not_hold(self):
        for field in ("log_digest", "evidence_digest"):
            with self.subTest(field=field):
                self.write()
                self.edit(lambda d, f=field: d.update({f: 5}))
                self.assertIn("malformed", self.check())

    def test_a_changed_suite_does_not_hold(self):
        self.write()
        self.assertIsNone(self.check())
        (self.root / SUITE).write_text("# suite, edited\n", encoding="utf-8")
        self.assertIn("different suite", self.check())

    def test_a_missing_suite_does_not_hold(self):
        self.write()
        (self.root / SUITE).unlink()
        self.assertIn("suite named by the evidence is missing", self.check())

    def test_an_unsafe_suite_path_does_not_hold(self):
        for bad in ("../escape.py", "/etc/passwd", ""):
            with self.subTest(path=bad):
                self.write()

                def change(d, b=bad):
                    d["suite"]["path"] = b
                    d["evidence_digest"] = evidence_digest(d["fingerprint"], d["suite"], d["tests"], d["log_digest"])
                self.edit(change)
                self.assertIn("not a safe relative path", self.check())

    def test_a_different_in_repo_file_cannot_stand_in_for_the_approved_suite(self):
        (self.root / "tests" / "other.py").write_text("# an unrelated file\n", encoding="utf-8")
        write_record(self.root, "grep", "1.2.0", self.identity, self.cases, 2, GOOD, "ok\n", "tests/other.py")
        self.assertIn("not on the approved list", self.check())

    def test_an_allow_list_may_name_more_than_one_suite(self):
        (self.root / "tests" / "second.py").write_text("# the second approved suite\n", encoding="utf-8")
        write_record(self.root, "grep", "1.2.0", self.identity, self.cases, 2, GOOD, "ok\n", "tests/second.py")
        self.assertIn("not on the approved list", self.check())
        with mock.patch("capability.evidence.APPROVED_SUITES", (SUITE, "tests/second.py")):
            self.assertIsNone(self.check())

    def test_a_symlinked_suite_file_is_refused_even_when_its_bytes_match(self):
        self.write()
        real = self.root / "elsewhere.py"
        real.write_bytes((self.root / SUITE).read_bytes())
        (self.root / SUITE).unlink()
        (self.root / SUITE).symlink_to(real)
        self.assertIn("passes through a link", self.check())

    def test_a_symlink_pointing_outside_the_root_is_refused(self):
        self.write()
        with tempfile.TemporaryDirectory() as outside:
            target = Path(outside) / "suite.py"
            target.write_bytes((self.root / SUITE).read_bytes())
            (self.root / SUITE).unlink()
            (self.root / SUITE).symlink_to(target)
            self.assertIn("passes through a link", self.check())

    def test_a_symlinked_parent_directory_is_refused(self):
        self.write()
        moved = self.root / "real_tests"
        (self.root / "tests").rename(moved)
        (self.root / "tests").symlink_to(moved)
        self.assertIn("passes through a link", self.check())

    def test_a_suite_that_is_a_directory_is_refused(self):
        self.write()
        (self.root / SUITE).unlink()
        (self.root / SUITE).mkdir()
        self.assertIn("suite named by the evidence is missing", self.check())

    def test_the_record_is_not_a_signature_so_a_fully_consistent_forgery_holds(self):
        """Documented limit: whoever can edit the repository can write a consistent record. Reviews read it."""
        self.write(counts={**GOOD, "run": 3})
        log = self.summary_path().parent / "output.txt"
        log.write_text("forged but consistent\n", encoding="utf-8")

        def forge(d):
            d["log_digest"] = file_digest(log)
            d["evidence_digest"] = evidence_digest(d["fingerprint"], d["suite"], d["tests"], d["log_digest"])
        self.edit(forge)
        self.assertIsNone(self.check())

    def test_the_evidence_digest_follows_the_log_but_the_fingerprint_does_not(self):
        self.write(log="first log\n")
        one = json.loads(self.summary_path().read_text(encoding="utf-8"))
        self.write(log="second log\n")
        two = json.loads(self.summary_path().read_text(encoding="utf-8"))
        self.assertEqual(one["fingerprint"], two["fingerprint"])
        self.assertNotEqual(one["evidence_digest"], two["evidence_digest"])
        self.assertNotEqual(one["log_digest"], two["log_digest"])

    def test_results_are_not_part_of_the_fingerprint(self):
        self.write(counts=GOOD)
        one = json.loads(self.summary_path().read_text(encoding="utf-8"))["fingerprint"]
        self.write(counts={**GOOD, "run": 9, "passed": 9})
        self.assertEqual(one, json.loads(self.summary_path().read_text(encoding="utf-8"))["fingerprint"])


class ApprovedSuiteListTest(unittest.TestCase):
    """The shipped allow-list, read without any patching."""

    def test_the_approved_list_is_exactly_the_one_real_suite(self):
        from capability import evidence
        self.assertEqual(evidence.APPROVED_SUITES, ("tests/test_tools_t001.py",))
        self.assertEqual(evidence.APPROVED_SUITES, (evidence.SUITE,))

    def test_every_committed_record_names_an_approved_suite(self):
        root = Path(__file__).resolve().parent.parent
        from capability import evidence
        for summary in sorted((root / EVIDENCE_DIR).glob("*/*/summary.json")):
            data = json.loads(summary.read_text(encoding="utf-8"))
            if "evidence_digest" in data:  # current-format records; the 1.1.0 history has no such field
                with self.subTest(record=str(summary.relative_to(root))):
                    self.assertIn(data["suite"]["path"], evidence.APPROVED_SUITES)


class EvidenceToolFilterTest(unittest.TestCase):
    """capability.evidence --tool (review 417e8a3e): it selects, it leaves the rest untouched, and it never selects nothing."""

    def setUp(self):
        import contextlib
        import io
        self._contextlib, self._io = contextlib, io
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        new_tree(self.root)
        for name in ("tool-one", "tool-two", "tool-three"):
            add_fixture_tool(self.root, "python", name=name)
            write_manifest(self.root, "tool", name, tool_manifest(
                name, [{"language": "python", "location": f"library/{name}/python"}]))
        for name in ("tool-one", "tool-two"):  # tool-three has an implementation but no cases: not eligible
            (self.root / "library" / name / "conformance.json").write_text(json.dumps(CASES), encoding="utf-8")
        (self.root / "tests").mkdir()
        (self.root / "tests" / "test_tools_t001.py").write_text(
            "import unittest\n\n\nclass T(unittest.TestCase):\n    def test_ok(self):\n        pass\n", encoding="utf-8")
        self.assertEqual(self.run_main("write", str(self.root))[0], 0)

    def tearDown(self):
        self._tmp.cleanup()

    def run_main(self, *args):
        from capability.evidence import main
        out, err = self._io.StringIO(), self._io.StringIO()
        with self._contextlib.redirect_stdout(out), self._contextlib.redirect_stderr(err):
            code = main(list(args))
        return code, out.getvalue(), err.getvalue()

    def snapshot(self, name):
        folder = self.root / EVIDENCE_DIR / name / "1.0.0"
        return {path.name: file_digest(path) for path in sorted(folder.iterdir())} if folder.exists() else None

    def test_the_baseline_wrote_both_eligible_tools_and_not_the_one_without_cases(self):
        self.assertIsNotNone(self.snapshot("tool-one"))
        self.assertIsNotNone(self.snapshot("tool-two"))
        self.assertIsNone(self.snapshot("tool-three"))

    def test_write_with_a_filter_writes_only_the_named_tool_and_leaves_the_others_byte_for_byte(self):
        import shutil
        two = self.snapshot("tool-two")
        shutil.rmtree(self.root / EVIDENCE_DIR / "tool-one")
        code, out, _ = self.run_main("write", str(self.root), "--tool", "tool-one")
        self.assertEqual(code, 0)
        self.assertIn("wrote evidence/tools/tool-one/1.0.0", out)
        self.assertNotIn("tool-two", out)
        self.assertIsNotNone(self.snapshot("tool-one"))           # the named tool was regenerated
        self.assertEqual(self.snapshot("tool-two"), two)          # the other is identical, file by file
        self.assertEqual(set(two), {"output.txt", "summary.json"})

    def test_rewriting_an_existing_named_record_still_leaves_the_others_untouched(self):
        two = self.snapshot("tool-two")
        self.assertEqual(self.run_main("write", str(self.root), "--tool", "tool-one")[0], 0)
        self.assertEqual(self.snapshot("tool-two"), two)

    def test_the_filter_may_be_repeated(self):
        import shutil
        shutil.rmtree(self.root / EVIDENCE_DIR)
        code, out, _ = self.run_main("write", str(self.root), "--tool", "tool-one", "--tool", "tool-two")
        self.assertEqual(code, 0)
        self.assertIsNotNone(self.snapshot("tool-one"))
        self.assertIsNotNone(self.snapshot("tool-two"))

    def test_without_a_filter_write_covers_every_eligible_tool(self):
        import shutil
        shutil.rmtree(self.root / EVIDENCE_DIR)
        self.assertEqual(self.run_main("write", str(self.root))[0], 0)
        self.assertIsNotNone(self.snapshot("tool-one"))
        self.assertIsNotNone(self.snapshot("tool-two"))

    def test_check_with_a_filter_reports_only_the_named_tool(self):
        import shutil
        shutil.rmtree(self.root / EVIDENCE_DIR / "tool-two")
        code, out, _ = self.run_main("check", str(self.root), "--tool", "tool-one")
        self.assertEqual(code, 0, out)
        self.assertIn("tool-one@1.0.0: evidence holds", out)
        self.assertNotIn("tool-two", out)
        code, out, _ = self.run_main("check", str(self.root))
        self.assertEqual(code, 1)
        self.assertIn("tool-two@1.0.0: no evidence record", out)

    def test_an_unknown_tool_fails_clearly_for_write_and_check_and_writes_nothing(self):
        import shutil
        shutil.rmtree(self.root / EVIDENCE_DIR / "tool-one")
        one, two = self.snapshot("tool-one"), self.snapshot("tool-two")
        for command in ("write", "check"):
            with self.subTest(command=command):
                code, out, err = self.run_main(command, str(self.root), "--tool", "tool-onee")
                self.assertEqual(code, 2)
                self.assertEqual(out, "")
                self.assertIn("no evidence-eligible tool named 'tool-onee'", err)
                self.assertIn("Eligible: tool-one, tool-two", err)
        self.assertEqual((self.snapshot("tool-one"), self.snapshot("tool-two")), (one, two))  # nothing was written

    def test_a_known_name_beside_an_unknown_one_writes_nothing_at_all(self):
        import shutil
        shutil.rmtree(self.root / EVIDENCE_DIR / "tool-one")
        code, _, err = self.run_main("write", str(self.root), "--tool", "tool-one", "--tool", "nope")
        self.assertEqual(code, 2)
        self.assertIn("'nope'", err)
        self.assertNotIn("'tool-one'", err)
        self.assertIsNone(self.snapshot("tool-one"))

    def test_a_tool_without_cases_and_an_empty_name_are_not_eligible(self):
        for name in ("tool-three", "", " ", "graph-query"):
            with self.subTest(name=name):
                code, _, err = self.run_main("check", str(self.root), "--tool", name)
                self.assertEqual(code, 2)
                self.assertIn("no evidence-eligible tool named", err)

    def test_the_real_catalog_filter_selects_glob_only_and_rejects_a_misspelling(self):
        import shutil
        root = Path(self._tmp.name) / "real"
        root.mkdir()
        for folder in ("schemas", "policy", "catalog", "library", "evidence"):
            shutil.copytree(Path(__file__).resolve().parent.parent / folder, root / folder)
        copy_suite(root)
        code, out, _ = self.run_main("check", str(root), "--tool", "glob")
        self.assertEqual((code, out.strip()), (0, "glob@1.3.0: evidence holds"))
        code, _, err = self.run_main("check", str(root), "--tool", "globb")
        self.assertEqual(code, 2)
        self.assertIn("Eligible: git-log, glob, grep, unawaited-async", err)


class RealEvidenceTest(unittest.TestCase):
    """The committed records for the four real tools."""

    def copy(self, tmp):
        import shutil
        root = Path(tmp)
        for folder in ("schemas", "policy", "catalog", "library", "evidence"):
            shutil.copytree(Path(__file__).resolve().parent.parent / folder, root / folder)
        copy_suite(root)
        return root

    def run_main(self, *args):
        import contextlib
        import io
        from capability.evidence import main
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = main(list(args))
        return code, out.getvalue()

    def test_every_committed_record_holds(self):
        with tempfile.TemporaryDirectory() as tmp:
            code, text = self.run_main("check", str(self.copy(tmp)))
        self.assertEqual(code, 0, text)
        for tool in ("git-log", "glob", "grep", "unawaited-async"):
            self.assertIn(f"{tool}@{TOOL_VERSIONS[tool]}: evidence holds", text)

    def test_a_changed_tool_makes_the_check_fail_with_a_reason(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = self.copy(tmp)
            main = root / "library" / "glob" / "python" / "main.py"
            main.write_text(main.read_text(encoding="utf-8") + "\n# changed\n", encoding="utf-8")
            code, text = self.run_main("check", str(root))
        self.assertEqual(code, 1)
        self.assertIn("glob@1.3.0: evidence is for a different identity", text)
        self.assertIn("grep@1.2.0: evidence holds", text)

    def test_the_records_show_a_clean_run_of_the_tool_suite(self):
        root = Path(__file__).resolve().parent.parent
        for tool in ("git-log", "glob", "grep", "unawaited-async"):
            with self.subTest(tool=tool):
                data = json.loads((root / EVIDENCE_DIR / tool / TOOL_VERSIONS[tool] / "summary.json").read_text(encoding="utf-8"))
                self.assertEqual((data["tests"]["failed"], data["tests"]["errors"], data["tests"]["skipped"]), (0, 0, 0))
                self.assertGreaterEqual(data["tests"]["passed"], 40)
                self.assertEqual(data["suite"]["path"], "tests/test_tools_t001.py")


class TestedLabelTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        new_tree(self.root)
        add_fixture_tool(self.root, "python")
        write_manifest(self.root, "tool", "fixture-tool", tool_manifest(
            "fixture-tool", [{"language": "python", "location": "library/fixture-tool/python"}]))
        (self.root / "library" / "fixture-tool" / "conformance.json").write_text(json.dumps(CASES), encoding="utf-8")
        (self.root / "tests").mkdir()
        (self.root / "tests" / "suite.py").write_text("# suite\n", encoding="utf-8")
        patcher = mock.patch("capability.evidence.APPROVED_SUITES", (SUITE,))
        patcher.start()
        self.addCleanup(patcher.stop)

    def tearDown(self):
        self._tmp.cleanup()

    def identity(self):
        manifest = check_manifest_file(self.root / "catalog" / "tool" / "fixture-tool" / "manifest.yaml")
        implementations, errors = check_implementations(manifest, self.root, load_languages(self.root))
        self.assertEqual(errors, [])
        return manifest, tool_identity(manifest, implementations)

    def write_evidence(self, counts=GOOD):
        manifest, identity = self.identity()
        digest = cases_digest(load_cases(self.root / "library" / "fixture-tool" / "conformance.json"))
        return write_record(self.root, "fixture-tool", manifest["version"], identity, digest,
                            POLICY_VERSION, counts, "log\n", SUITE)

    def entry(self):
        return build_index(self.root, fixture_mode=True)["entries"][0]

    def test_without_evidence_the_tool_is_validated(self):
        self.assertEqual(self.entry()["label"], "Validated")

    def test_with_good_evidence_the_tool_is_tested_and_the_fingerprint_is_listed(self):
        folder = self.write_evidence()
        entry = self.entry()
        fingerprint_value = json.loads((folder / "summary.json").read_text(encoding="utf-8"))["fingerprint"]
        self.assertEqual(entry["label"], "Tested")
        self.assertEqual(entry["evidence"][1], "tests:" + fingerprint_value)
        record = json.loads((folder / "summary.json").read_text(encoding="utf-8"))["evidence_digest"]
        self.assertEqual(entry["evidence"][2], "record:" + record)
        self.assertTrue(entry["evidence"][0].startswith("conformance:sha256:"))

    def test_a_changed_byte_in_the_tool_falls_back_to_validated(self):
        self.write_evidence()
        self.assertEqual(self.entry()["label"], "Tested")
        main = self.root / "library" / "fixture-tool" / "python" / "main.py"
        main.write_text(main.read_text(encoding="utf-8") + "\n# comment\n", encoding="utf-8")
        self.assertEqual(self.entry()["label"], "Validated")

    def test_changed_cases_fall_back_to_validated(self):
        self.write_evidence()
        path = self.root / "library" / "fixture-tool" / "conformance.json"
        path.write_text(json.dumps(CASES + [{"id": "zero", "input": {"n": 0}, "expect": {"ok": True}}]), encoding="utf-8")
        self.assertEqual(self.entry()["label"], "Validated")

    def test_evidence_with_a_failure_or_skip_leaves_the_tool_validated(self):
        for counts in ({**GOOD, "failed": 1, "passed": 2}, {**GOOD, "skipped": 1, "passed": 2, "skipped_reasons": ["x"]}):
            with self.subTest(counts=counts):
                self.write_evidence(counts)
                self.assertEqual(self.entry()["label"], "Validated")

    def test_evidence_for_another_version_does_not_count(self):
        folder = self.write_evidence()
        moved = folder.parent / "9.9.9"
        folder.rename(moved)
        self.assertEqual(self.entry()["label"], "Validated")

    def test_a_symlinked_suite_or_a_different_file_falls_back_to_validated(self):
        folder = self.write_evidence()
        self.assertEqual(self.entry()["label"], "Tested")
        suite = self.root / "tests" / "suite.py"
        real = self.root / "elsewhere.py"
        real.write_bytes(suite.read_bytes())
        suite.unlink()
        suite.symlink_to(real)
        self.assertEqual(self.entry()["label"], "Validated")
        suite.unlink()
        suite.write_bytes(real.read_bytes())
        self.assertEqual(self.entry()["label"], "Tested")
        with mock.patch("capability.evidence.APPROVED_SUITES", ("tests/another.py",)):
            self.assertEqual(self.entry()["label"], "Validated")

    def test_a_changed_or_missing_suite_falls_back_to_validated(self):
        self.write_evidence()
        self.assertEqual(self.entry()["label"], "Tested")
        (self.root / "tests" / "suite.py").write_text("# suite, edited\n", encoding="utf-8")
        self.assertEqual(self.entry()["label"], "Validated")
        (self.root / "tests" / "suite.py").unlink()
        self.assertEqual(self.entry()["label"], "Validated")

    def test_edited_counts_or_a_replaced_log_fall_back_to_validated(self):
        folder = self.write_evidence()
        self.assertEqual(self.entry()["label"], "Tested")
        path = folder / "summary.json"
        original = path.read_bytes()
        data = json.loads(original)
        data["tests"]["run"], data["tests"]["passed"] = 40, 40
        path.write_bytes(canonical_bytes(data))
        self.assertEqual(self.entry()["label"], "Validated")
        path.write_bytes(original)
        self.assertEqual(self.entry()["label"], "Tested")
        (folder / "output.txt").write_text("a different log\n", encoding="utf-8")
        self.assertEqual(self.entry()["label"], "Validated")

    def test_the_evidence_folder_is_part_of_the_index_inputs(self):
        before = build_index(self.root, fixture_mode=True)["inputs_digest"]
        self.write_evidence()
        self.assertNotEqual(before, build_index(self.root, fixture_mode=True)["inputs_digest"])

    def test_a_tool_without_cases_is_never_tested_even_with_a_record(self):
        self.write_evidence()
        (self.root / "library" / "fixture-tool" / "conformance.json").unlink()
        self.assertEqual(self.entry()["label"], "Declared")


if __name__ == "__main__":
    unittest.main()
