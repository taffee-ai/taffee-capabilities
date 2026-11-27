"""S-013: capctl verify."""

import contextlib
import io
import json
import shutil
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

import yaml

from capability.capctl import FAIL, NOT_RUN, OK, SKIP, main, verify
from capability.conformance import cases_digest, load_cases
from capability.evidence import write_record
from capability.index import build_index
from capability.revocation import GENESIS, RevocationClient, entry_hash, load_public_key

from fixture_support import (FIXTURES, REPO, TOOL_VERSIONS, add_fixture_tool, copy_suite, new_tree,
                             tool_manifest, write_manifest)
from revocation_support import Clocks, FakeFeed, chain, checkpoint, make_key

GOOD = {"run": 3, "passed": 3, "failed": 0, "errors": 0, "skipped": 0, "skipped_reasons": []}


def status(report, name):
    return {check.name: check.status for check in report.checks}[name]


def detail(report, name):
    return {check.name: check.detail for check in report.checks}[name]


class RealCatalogCase(unittest.TestCase):
    """A fresh copy of the real, built catalog (with its committed evidence) for each test."""

    master = None

    @classmethod
    def setUpClass(cls):
        cls._master_tmp = tempfile.TemporaryDirectory()
        cls.master = Path(cls._master_tmp.name) / "master"
        cls.master.mkdir()
        for folder in ("schemas", "policy", "catalog", "library", "evidence"):
            shutil.copytree(REPO / folder, cls.master / folder)
        copy_suite(cls.master)
        build_index(cls.master)

    @classmethod
    def tearDownClass(cls):
        cls._master_tmp.cleanup()

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name) / "tree"
        shutil.copytree(self.master, self.root)

    def tearDown(self):
        self._tmp.cleanup()

    def index_entry(self, name):
        index = json.loads((self.root / "index" / "index.json").read_bytes())
        return next(e for e in index["entries"] if e["name"] == name)


class RealToolTest(RealCatalogCase):
    def test_a_tested_tool_verifies_when_revocation_is_skipped_on_request(self):
        report = verify(self.root, "grep", "1.3.0", skip_revocation=True)
        self.assertTrue(report.verified, report.lines())
        self.assertEqual([c.name for c in report.checks],
                         ["index", "current", "entry", "digest (python)", "evidence", "revocation", "label"])
        self.assertEqual([c.status for c in report.checks], [OK, OK, OK, OK, OK, SKIP, OK])
        self.assertIn("(--skip-revocation)", detail(report, "revocation"))
        self.assertEqual(detail(report, "label"), "Tested")
        self.assertEqual(report.lines()[-1], "VERIFIED")

    def test_no_revocation_feed_and_no_skip_is_a_failure_that_still_reports_the_rest(self):
        report = verify(self.root, "grep", "1.3.0")
        self.assertFalse(report.verified)
        self.assertEqual(status(report, "revocation"), FAIL)
        self.assertIn("no revocation feed configured", detail(report, "revocation"))
        self.assertEqual(status(report, "evidence"), OK)
        self.assertEqual(report.lines()[-1], "NOT VERIFIED")

    def test_all_four_tools_verify(self):
        for tool in ("git-log", "glob", "grep", "unawaited-async"):
            with self.subTest(tool=tool):
                self.assertTrue(verify(self.root, tool, TOOL_VERSIONS[tool], skip_revocation=True).verified)

    def test_graph_query_verifies_with_digest_skipped_and_evidence_skipped(self):
        report = verify(self.root, "graph-query", "1.0.0", skip_revocation=True)
        self.assertTrue(report.verified, report.lines())
        self.assertEqual(status(report, "digest"), SKIP)
        self.assertEqual(status(report, "evidence"), SKIP)
        self.assertEqual(detail(report, "label"), "Declared")

    def test_unknown_name_or_version_is_not_found_and_the_rest_is_not_run(self):
        for name, version in (("nothing", "1.0.0"), ("grep", "9.9.9"), ("grep", "1.0.0")):
            with self.subTest(name=name, version=version):
                report = verify(self.root, name, version, skip_revocation=True)
                self.assertEqual(status(report, "entry"), FAIL)
                self.assertIn("NotFound", detail(report, "entry"))
                self.assertEqual(status(report, "digest"), NOT_RUN)
                self.assertFalse(report.verified)

    def test_the_label_comes_from_the_index_not_the_manifest(self):
        path = self.root / "catalog" / "tool" / "grep" / "manifest.yaml"
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
        data["label"] = "Approved"
        path.write_text(yaml.safe_dump(data, sort_keys=False), encoding="utf-8")
        report = verify(self.root, "grep", "1.3.0", skip_revocation=True)
        self.assertEqual(detail(report, "label"), "Tested")
        self.assertEqual(status(report, "current"), FAIL)  # the edit is still noticed


class IntegrityTest(RealCatalogCase):
    def test_a_changed_index_byte_fails_the_index_and_marks_the_rest_not_run(self):
        path = self.root / "index" / "index.json"
        path.write_bytes(path.read_bytes().replace(b'"Tested"', b'"Approved"', 1))
        report = verify(self.root, "grep", "1.3.0", skip_revocation=True)
        self.assertEqual(status(report, "index"), FAIL)
        self.assertTrue(all(c.status == NOT_RUN for c in report.checks[1:]))
        self.assertFalse(report.verified)

    def test_a_missing_index_fails_the_index(self):
        (self.root / "index" / "index.json").unlink()
        self.assertEqual(status(verify(self.root, "grep", "1.3.0", skip_revocation=True), "index"), FAIL)

    def test_a_manifest_edited_after_the_build_fails_current_and_the_rest_still_runs(self):
        path = self.root / "catalog" / "tool" / "glob" / "manifest.yaml"
        path.write_text(path.read_text(encoding="utf-8") + "# edited\n", encoding="utf-8")
        report = verify(self.root, "grep", "1.3.0", skip_revocation=True)
        self.assertEqual(status(report, "current"), FAIL)
        self.assertEqual(status(report, "digest (python)"), OK)
        self.assertFalse(report.verified)

    def test_a_changed_implementation_fails_current_and_its_own_digest(self):
        main_py = self.root / "library" / "glob" / "python" / "main.py"
        main_py.write_text(main_py.read_text(encoding="utf-8") + "\n# changed\n", encoding="utf-8")
        report = verify(self.root, "glob", "1.4.0", skip_revocation=True)
        self.assertEqual(status(report, "current"), FAIL)
        self.assertEqual(status(report, "digest (python)"), FAIL)
        self.assertIn("IntegrityError", detail(report, "digest (python)"))
        other = verify(self.root, "grep", "1.3.0", skip_revocation=True)
        self.assertEqual(status(other, "digest (python)"), OK)

    def test_a_missing_implementation_folder_is_an_entry_error(self):
        shutil.rmtree(self.root / "library" / "grep" / "python")
        report = verify(self.root, "grep", "1.3.0", skip_revocation=True)
        self.assertEqual(status(report, "digest (python)"), FAIL)
        self.assertIn("EntryError", detail(report, "digest (python)"))


class EvidenceTest(RealCatalogCase):
    def test_changed_cases_fail_evidence(self):
        path = self.root / "library" / "grep" / "conformance.json"
        cases = json.loads(path.read_text(encoding="utf-8"))
        path.write_text(json.dumps(cases + [{"id": "extra", "input": {"pattern": "a", "files": "*.txt"},
                                             "expect": {"ok": False}, "files": {"a.txt": "a"}}]), encoding="utf-8")
        report = verify(self.root, "grep", "1.3.0", skip_revocation=True)
        self.assertEqual(status(report, "evidence"), FAIL)
        self.assertIn("differ", detail(report, "evidence"))

    def test_a_record_for_another_identity_fails(self):
        path = self.root / "evidence" / "tools" / "grep" / "1.3.0" / "summary.json"
        data = json.loads(path.read_text(encoding="utf-8"))
        data["identity"] = "sha256:" + "0" * 64
        path.write_text(json.dumps(data), encoding="utf-8")
        report = verify(self.root, "grep", "1.3.0", skip_revocation=True)
        self.assertEqual(status(report, "evidence"), FAIL)
        self.assertIn("identity", detail(report, "evidence"))

    def test_a_missing_record_at_tested_fails(self):
        shutil.rmtree(self.root / "evidence" / "tools" / "grep")
        report = verify(self.root, "grep", "1.3.0", skip_revocation=True)
        self.assertEqual(status(report, "evidence"), FAIL)
        self.assertIn("no evidence record", detail(report, "evidence"))

    def test_a_valid_record_the_index_does_not_list_fails(self):
        entry = self.index_entry("grep")
        (self.root / "tests").mkdir(exist_ok=True)
        (self.root / "tests" / "other_suite.py").write_text("# a different suite\n", encoding="utf-8")
        cases = cases_digest(load_cases(self.root / "library" / "grep" / "conformance.json"))
        write_record(self.root, "grep", "1.3.0", entry["identity"], cases, 2, GOOD, "log\n", "tests/other_suite.py")
        with mock.patch("capability.evidence.APPROVED_SUITES", ("tests/test_tools_t001.py", "tests/other_suite.py")):
            report = verify(self.root, "grep", "1.3.0", skip_revocation=True)
        self.assertEqual(status(report, "evidence"), FAIL)
        self.assertIn("fingerprint the evidence record does not carry", detail(report, "evidence"))


class ValidatedAndOtherKindsTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        new_tree(self.root)

    def tearDown(self):
        self._tmp.cleanup()

    def add_tool(self, cases=None):
        add_fixture_tool(self.root, "python")
        write_manifest(self.root, "tool", "fixture-tool", tool_manifest(
            "fixture-tool", [{"language": "python", "location": "library/fixture-tool/python"}]))
        if cases is not None:
            (self.root / "library" / "fixture-tool" / "conformance.json").write_text(json.dumps(cases), encoding="utf-8")

    def add_agent(self, name="fixture-agent"):
        manifest = yaml.safe_load((FIXTURES / "valid" / "agent.yaml").read_text(encoding="utf-8"))
        manifest["name"] = name
        write_manifest(self.root, "agent", name, manifest)
        folder = self.root / "library" / "fixture-agent"
        folder.mkdir(parents=True, exist_ok=True)
        (folder / "agent.txt").write_text("agent\n", encoding="utf-8")

    def test_a_validated_tool_with_matching_cases_passes_evidence(self):
        self.add_tool([{"id": "double", "input": {"n": 3}, "expect": {"ok": True, "contains": "\"double\": 6"}}])
        build_index(self.root, fixture_mode=True)
        report = verify(self.root, "fixture-tool", "1.0.0", skip_revocation=True)
        self.assertTrue(report.verified, report.lines())
        self.assertEqual(status(report, "evidence"), OK)
        self.assertIn("no test evidence at this label", detail(report, "evidence"))
        self.assertEqual(detail(report, "label"), "Validated")

    def test_a_declared_tool_skips_evidence(self):
        self.add_tool(cases=None)
        build_index(self.root, fixture_mode=True)
        report = verify(self.root, "fixture-tool", "1.0.0", skip_revocation=True)
        self.assertEqual(status(report, "evidence"), SKIP)
        self.assertTrue(report.verified)

    def test_an_agent_with_a_library_source_verifies_and_skips_evidence(self):
        self.add_agent()
        build_index(self.root, fixture_mode=True)
        report = verify(self.root, "fixture-agent", "1.0.0", skip_revocation=True)
        self.assertTrue(report.verified, report.lines())
        self.assertEqual(status(report, "digest"), OK)
        self.assertIn("code sha256:", detail(report, "digest"))
        self.assertEqual(status(report, "evidence"), SKIP)

    def test_a_name_used_by_two_kinds_needs_a_kind(self):
        self.add_tool(cases=None)
        self.add_agent(name="fixture-tool")
        build_index(self.root, fixture_mode=True)
        ambiguous = verify(self.root, "fixture-tool", "1.0.0", skip_revocation=True)
        self.assertEqual(status(ambiguous, "entry"), FAIL)
        self.assertIn("Ambiguous", detail(ambiguous, "entry"))
        self.assertTrue(verify(self.root, "fixture-tool", "1.0.0", kind="tool", skip_revocation=True).verified)

    def test_verify_runs_no_capability_code(self):
        add_fixture_tool(self.root, "python")
        main_py = self.root / "library" / "fixture-tool" / "python" / "main.py"
        marker = self.root / "RAN"
        main_py.write_text("import pathlib\npathlib.Path(%r).write_text('ran')\n" % str(marker), encoding="utf-8")
        write_manifest(self.root, "tool", "fixture-tool", tool_manifest(
            "fixture-tool", [{"language": "python", "location": "library/fixture-tool/python"}]))
        build_index(self.root, fixture_mode=True)
        self.assertTrue(verify(self.root, "fixture-tool", "1.0.0", skip_revocation=True).verified)
        self.assertFalse(marker.exists())


class RevocationTest(RealCatalogCase):
    def setUp(self):
        super().setUp()
        self.private, encoded = make_key()
        self.clocks = Clocks()
        self.client = RevocationClient(load_public_key(encoded), wall=self.clocks.wall, monotonic=self.clocks.mono)
        self.feed = FakeFeed(self.clocks, self.private)

    def run_verify(self, tool="grep"):
        return verify(self.root, tool, "1.3.0", client=self.client, feed=self.feed)

    def test_a_fresh_feed_with_no_revocation_passes(self):
        report = self.run_verify()
        self.assertTrue(report.verified, report.lines())
        self.assertEqual(status(report, "revocation"), OK)
        self.assertIn("checked at seq 0, fresh (30s left)", detail(report, "revocation"))

    def test_a_revoked_identity_fails(self):
        identity = self.index_entry("grep")["identity"]
        self.feed.publish([{"kind": "tool", "digest": identity}])
        report = self.run_verify()
        self.assertEqual(status(report, "revocation"), FAIL)
        self.assertIn("revoked at feed seq 1 (incident)", detail(report, "revocation"))
        self.assertFalse(report.verified)

    def test_a_revoked_name_and_version_fails_whatever_the_digest(self):
        self.feed.publish([{"kind": "tool", "name": "grep", "version": "1.3.0"}])
        self.assertEqual(status(self.run_verify(), "revocation"), FAIL)

    def test_a_revocation_of_another_tool_or_kind_does_not_fail_this_one(self):
        self.feed.publish([{"kind": "tool", "name": "glob", "version": "1.4.0"},
                           {"kind": "agent", "name": "grep", "version": "1.3.0"}])
        self.assertEqual(status(self.run_verify(), "revocation"), OK)

    def test_an_unreachable_feed_fails(self):
        self.feed.up = False
        report = self.run_verify()
        self.assertEqual(status(report, "revocation"), FAIL)
        self.assertIn("SourceUnavailable", detail(report, "revocation"))

    def test_an_old_checkpoint_fails(self):
        self.feed.raw_checkpoint = json.dumps(checkpoint(self.private, 1, 0, GENESIS, self.clocks.wall_now - 40))
        report = self.run_verify()
        self.assertEqual(status(report, "revocation"), FAIL)
        self.assertIn("CheckpointTooOld", detail(report, "revocation"))

    def test_a_feed_signed_with_another_key_fails(self):
        other, _ = make_key()
        report = verify(self.root, "grep", "1.3.0", client=self.client, feed=FakeFeed(self.clocks, other))
        self.assertEqual(status(report, "revocation"), FAIL)
        self.assertIn("BadSignature", detail(report, "revocation"))


class CommandLineTest(RealCatalogCase):
    def run_main(self, *args):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = main(list(args))
        return code, out.getvalue()

    def feed_files(self, targets=(), key=None):
        private, encoded = key or make_key()
        entries = chain(private, len(targets), list(targets)) if targets else []
        head = entry_hash(entries[-1]) if entries else GENESIS
        base = Path(self._tmp.name)
        (base / "feed.jsonl").write_text("".join(json.dumps(e) + "\n" for e in entries), encoding="utf-8")
        (base / "checkpoint.json").write_text(
            json.dumps(checkpoint(private, 1, len(entries), head, time.time())), encoding="utf-8")
        return encoded, str(base / "feed.jsonl"), str(base / "checkpoint.json")

    def test_exit_0_when_verified_and_the_report_is_printed(self):
        code, text = self.run_main("verify", "grep@1.3.0", "--root", str(self.root), "--skip-revocation")
        self.assertEqual(code, 0, text)
        self.assertTrue(text.rstrip().endswith("VERIFIED") and "NOT VERIFIED" not in text)
        self.assertIn("revocation", text)
        self.assertIn("SKIP", text)

    def test_exit_1_when_a_check_fails(self):
        code, text = self.run_main("verify", "grep@1.3.0", "--root", str(self.root))
        self.assertEqual(code, 1)
        self.assertIn("NOT VERIFIED", text)
        self.assertIn("no revocation feed configured", text)

    def test_a_file_feed_passes_and_a_revoked_target_fails(self):
        key, feed, cp = self.feed_files()
        code, text = self.run_main("verify", "grep@1.3.0", "--root", str(self.root),
                                   "--revocation-key", key, "--feed-file", feed, "--feed-checkpoint", cp)
        self.assertEqual(code, 0, text)
        self.assertIn("checked at seq 0", text)
        key, feed, cp = self.feed_files(targets=[{"kind": "tool", "name": "grep", "version": "1.3.0"}])
        code, text = self.run_main("verify", "grep@1.3.0", "--root", str(self.root),
                                   "--revocation-key", key, "--feed-file", feed, "--feed-checkpoint", cp)
        self.assertEqual(code, 1)
        self.assertIn("revoked at feed seq 1", text)

    def test_a_bad_key_is_a_failed_check_and_the_rest_is_still_reported(self):
        _, feed, cp = self.feed_files()
        code, text = self.run_main("verify", "grep@1.3.0", "--root", str(self.root),
                                   "--revocation-key", "not-a-key", "--feed-file", feed, "--feed-checkpoint", cp)
        self.assertEqual(code, 1)
        self.assertIn("KeyMissing", text)
        self.assertIn("evidence", text)

    def test_a_missing_feed_file_is_a_failed_check(self):
        key, _, cp = self.feed_files()
        code, text = self.run_main("verify", "grep@1.3.0", "--root", str(self.root), "--revocation-key", key,
                                   "--feed-file", str(Path(self._tmp.name) / "nope.jsonl"), "--feed-checkpoint", cp)
        self.assertEqual(code, 1)
        self.assertIn("SourceUnavailable", text)

    def test_exit_2_for_usage_errors(self):
        key, feed, cp = self.feed_files()
        for args in (("verify", "grep", "--skip-revocation"), ("verify", "@1.0.0"), ("verify", "a@b@c"),
                     ("verify", "grep@1.3.0", "--feed-file", feed),
                     ("verify", "grep@1.3.0", "--revocation-key", key, "--feed-file", feed,
                      "--feed-checkpoint", cp, "--skip-revocation"),
                     ("verify", "grep@1.3.0", "--kind", "plugin"), ()):
            with self.subTest(args=args):
                with self.assertRaises(SystemExit) as ctx, contextlib.redirect_stderr(io.StringIO()):
                    main(list(args))
                self.assertEqual(ctx.exception.code, 2)


if __name__ == "__main__":
    unittest.main()
