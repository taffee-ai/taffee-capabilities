"""S-018 R4: admission of a tool written outside Taffee (REG-ADM-1 to 11).

AdmissionFlowTest uses a test-only sandbox that runs our own fixture code unconfined, to test the state machine
quickly. ContainerAdmissionTest runs the same flow in the real ContainerSandbox, including a tool that tries to
reach the network and is rejected. It needs TAFFEE_SANDBOX_IMAGE and TAFFEE_SANDBOX_TMP, like the other container
tests.
"""

import base64
import json
import os
import tempfile
import unittest
from pathlib import Path

from capability.admission import Admission, AdmissionError, Limits
from capability.library_repo import LIBRARY_OWNER, LibraryRepo
from capability.sandbox import ContainerSandbox, ProcessSandbox, docker_available
from capability.store import SQLiteStore
from capability.store_registry import StoreRegistry

from registry_support import (
    CASES, LANGUAGES, WORD_COUNT, UnconfinedTestSandbox, add_rows, admission_key, bundle, bundle_bytes,
    fresh_revocation, synthetic_fields, with_error_case, word_count_manifest,
)

IMAGE = os.environ.get("TAFFEE_SANDBOX_IMAGE")
SKIP_REASON = "TAFFEE_SANDBOX_IMAGE is not set" if not IMAGE else docker_available()


def b64(text):
    return base64.b64encode(text.encode()).decode()


class AdmissionFlowTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.base = Path(self._tmp.name)
        self.store = SQLiteStore(self.base / "data")
        self.private, self.public = admission_key()
        self.sandbox = UnconfinedTestSandbox()
        self.library = LibraryRepo(self.base / "library")
        self.admission = Admission(self.store, self.private, self.sandbox, LANGUAGES, self.base / "work",
                                   library=self.library)
        self.client, self.feed, self.clocks = fresh_revocation()
        self.registry = StoreRegistry(self.store, self.public, self.client, LANGUAGES)

    def tearDown(self):
        self.store.close()
        self._tmp.cleanup()

    def run_through(self, raw=None, submitter="author:acme"):
        record = self.admission.submit(raw if raw is not None else bundle_bytes(), submitter)
        return self.admission.process(record["id"])

    def refused(self, raw, code):
        with self.assertRaises(AdmissionError) as ctx:
            self.admission.submit(raw, "author:acme")
        self.assertEqual(ctx.exception.code, code)
        self.assertEqual(self.store.submissions(), [])

    # happy path
    def test_a_passing_tool_waits_for_approval_then_becomes_a_validated_row(self):
        record = self.run_through()
        self.assertEqual(record["state"], "awaiting_approval", record["problems"])
        # one build, every case, and the idempotency re-run (twice): all through the sandbox
        self.assertEqual(len(self.sandbox.calls), 1 + len(CASES) + 2)
        self.assertEqual([e.split(":")[0] for e in record["evidence"]], ["build", "conformance", "run"])
        self.assertEqual(self.registry.search("count words")["items"], [])  # not visible before approval
        admitted = self.admission.approve(record["id"], "approver:sec")
        self.assertEqual((admitted["state"], admitted["row_seq"]), ("admitted", 1))
        described = self.registry.describe("word-count")
        self.assertEqual((described["label"], described["source_type"]), ("Validated", "submission"))
        self.assertEqual(described["evidence"], record["evidence"])
        self.assertEqual(self.registry.resolve("word-count", languages=["python"])["identity"], record["identity"])
        row = self.store.get_row("tool", "word-count", "1.0.0")
        self.assertEqual((row["submitted_by"], row["admitted_by"]), ("author:acme", "approver:sec"))

    def test_every_state_change_is_stored(self):
        seen = []
        original = self.store.save_submission
        self.store.save_submission = lambda record: (seen.append(record["state"]), original(record))[1]
        record = self.run_through()
        self.admission.approve(record["id"], "approver:sec")
        self.assertEqual(seen, ["received", "validating", "building", "testing", "awaiting_approval", "admitted"])

    # bundle shape (REG-ADM-1, 2)
    def test_malformed_bundles_are_refused_before_anything_is_stored(self):
        good = bundle()
        self.refused(b"not json", "InvalidRequest")
        self.refused(json.dumps({**good, "extra": 1}).encode(), "InvalidRequest")
        self.refused(json.dumps({**good, "schema": "other/1"}).encode(), "InvalidRequest")
        self.refused(json.dumps({**good, "cases": []}).encode(), "InvalidRequest")
        self.refused(json.dumps({**good, "files": {"python": {"main.py": "%%%"}}}).encode(), "InvalidRequest")
        self.refused(json.dumps({**good, "files": {"cobol": {"main.cob": b64("x")}}}).encode(), "InvalidRequest")
        for path in ("../main.py", "/main.py", "a/../../main.py"):
            self.refused(json.dumps({**good, "files": {"python": {path: b64("x")}}}).encode(), "InvalidRequest")

    def test_size_limits_are_deployment_settings(self):
        small = Admission(self.store, self.private, self.sandbox, LANGUAGES, self.base / "w2",
                          limits=Limits(max_bundle_bytes=500, max_cases=2))
        with self.assertRaises(AdmissionError) as ctx:
            small.submit(bundle_bytes(), "author:acme")
        self.assertEqual(ctx.exception.code, "TooLarge")
        roomy = Admission(self.store, self.private, self.sandbox, LANGUAGES, self.base / "w3", limits=Limits(max_cases=2))
        with self.assertRaises(AdmissionError) as ctx:
            roomy.submit(bundle_bytes(), "author:acme")
        self.assertEqual(ctx.exception.code, "TooLarge")

    # validation (REG-ADM-3)
    def assert_rejected(self, record, *fragments):
        self.assertEqual(record["state"], "rejected")
        text = " | ".join(record["problems"])
        for fragment in fragments:
            self.assertIn(fragment, text)
        self.assertEqual(self.sandbox.calls, [])  # nothing ran
        self.assertEqual(self.store.count(), 0)

    def test_undescribed_parameters_are_rejected(self):
        record = self.run_through(bundle_bytes(manifest=word_count_manifest(described=False)))
        self.assert_rejected(record, "properties.path: a description is required",
                             "properties.text: a description is required")

    def test_a_tool_without_a_sandbox_policy_is_rejected(self):
        self.assert_rejected(self.run_through(bundle_bytes(manifest=word_count_manifest(sandbox=False))),
                             "spec.sandbox: a sandbox policy must be declared")

    def test_a_location_that_does_not_match_the_name_is_rejected(self):
        manifest = word_count_manifest()
        manifest["implementations"][0]["location"] = "library/other/python"
        self.assert_rejected(self.run_through(bundle_bytes(manifest=manifest)), "location must be library/word-count/python")

    def test_a_missing_entry_file_is_rejected(self):
        files = {"python": {"tool.py": b64(WORD_COUNT)}}
        self.assert_rejected(self.run_through(bundle_bytes(files=files)), "the entry file main.py is missing")

    def test_files_for_an_undeclared_language_are_rejected(self):
        files = {"python": {"main.py": b64(WORD_COUNT)}, "bash": {"main.sh": b64("echo")}}
        self.assert_rejected(self.run_through(bundle_bytes(files=files)), "do not match implementations")

    def test_an_invalid_manifest_is_rejected_with_the_schema_errors(self):
        manifest = word_count_manifest(name="Bad_Name")
        self.assert_rejected(self.run_through(bundle_bytes(manifest=manifest)), "name")

    def test_bad_cases_are_rejected(self):
        cases = [CASES[0], dict(CASES[0])]  # duplicate id
        self.assert_rejected(self.run_through(bundle_bytes(cases=cases)), "unique string id")

    # testing (REG-ADM-4, 5, 11)
    def test_a_failing_case_rejects_with_the_case_named_and_no_tool_output(self):
        broken = 'import json, sys\nsys.stdin.read()\nprint(json.dumps({"ok": False, "error": "SECRET-TOKEN-42"}))\n'
        record = self.run_through(bundle_bytes(source=broken))
        self.assertEqual(record["state"], "rejected")
        text = " | ".join(record["problems"])
        self.assertIn("python/counts-text: ok: wanted True, got False", text)
        self.assertNotIn("SECRET-TOKEN-42", text)
        self.assertEqual(self.store.count(), 0)

    def test_a_tool_that_overruns_its_time_limit_is_rejected(self):
        slow = 'import json, sys, time\nsys.stdin.read()\ntime.sleep(5)\nprint(json.dumps({"ok": True, "output": "1"}))\n'
        manifest = word_count_manifest()
        manifest["spec"]["timeoutMs"] = 300
        record = self.run_through(bundle_bytes(manifest=manifest, source=slow, cases=with_error_case([CASES[0]])))
        self.assertEqual(record["problems"][0], "python/counts-text: ran past its time limit")

    def test_output_breaking_a_postcondition_is_rejected(self):
        chatty = 'import json, sys\nsys.stdin.read()\nprint(json.dumps({"ok": True, "output": "x" * 500}))\n'
        record = self.run_through(bundle_bytes(source=chatty, cases=with_error_case(
            [{"id": "c", "input": {"text": "a"}, "expect": {"ok": True}}])))
        self.assertEqual(record["state"], "rejected")
        self.assertIn("python/c:", record["problems"][0])
        self.assertIn("max_bytes", record["problems"][0])

    def test_a_sandbox_that_cannot_enforce_the_policy_rejects_without_running(self):
        admission = Admission(self.store, self.private, ProcessSandbox(), LANGUAGES, self.base / "w4")
        record = admission.process(admission.submit(bundle_bytes(), "author:acme")["id"])
        self.assertEqual(record["state"], "rejected")
        self.assertIn("sandbox cannot enforce the declared policy: filesystem, network", record["problems"][0])


    # Taffee's own checks: build, contract, idempotency, coverage (the opinionated part)
    def test_code_that_does_not_compile_is_rejected_before_any_case_runs(self):
        record = self.run_through(bundle_bytes(source="def broken(:\n    pass\n"))
        self.assertEqual(record["problems"], ["python/build: syntax: main.py:1"])
        self.assertEqual(len(self.sandbox.calls), 1)  # the build only

    def test_a_tool_that_breaks_the_result_contract_is_rejected(self):
        noisy = 'import json, sys\nsys.stdin.read()\nprint("debug")\nprint(json.dumps({"ok": True, "output": "3"}))\n'
        record = self.run_through(bundle_bytes(source=noisy))
        self.assertIn("python/counts-text: did not answer with exactly one result line (the tool contract)",
                      record["problems"])

    def test_a_tool_declared_idempotent_must_answer_the_same_twice(self):
        flaky = ('import json, sys, random\nreq = json.loads(sys.stdin.read())\nd = req["input"]\n'
                 'if "path" in d and d["path"] == "absent.txt":\n    print(json.dumps({"ok": False, "error": "e"}))\n'
                 'else:\n    print(json.dumps({"ok": True, "output": "3 " + str(random.random())}))\n')
        # A fresh random float on every run: two runs agreeing by chance (1 in 10 with the old padding) made
        # the check pass and this test fail.
        cases = [{"id": "any", "input": {"text": "x"}, "expect": {"ok": True}}, CASES[2]]
        record = self.run_through(bundle_bytes(source=flaky, cases=cases))
        self.assertEqual(record["problems"], ["python/any: declared idempotent, but two runs gave different results"])

    def test_cases_must_include_an_error_case_and_a_success_case(self):
        only_ok = self.run_through(bundle_bytes(cases=[CASES[0]]))
        self.assertIn("cases: at least one case must expect a tool-level error (expect.ok: false)", only_ok["problems"])
        only_err = self.run_through(bundle_bytes(cases=[CASES[2]]))
        self.assertIn("cases: at least one case must expect success (expect.ok: true)", only_err["problems"])

    def test_every_required_parameter_must_be_exercised_and_inputs_must_be_valid(self):
        manifest = word_count_manifest()
        manifest["spec"]["inputSchema"]["required"] = ["text"]
        cases = [{"id": "no-text", "input": {"path": "doc.txt"}, "files": {"doc.txt": "a"}, "expect": {"ok": True}},
                 {"id": "bad-type", "input": {"path": 7}, "expect": {"ok": False}}]
        record = self.run_through(bundle_bytes(manifest=manifest, cases=cases))
        self.assertIn("cases: the required parameter text is never exercised", record["problems"])
        self.assertIn("cases.bad-type: the input does not match spec.inputSchema, so no caller could send it",
                      record["problems"])
        self.assertEqual(self.sandbox.calls, [])

    # approval policy
    def test_auto_approval_admits_the_moment_every_check_passes(self):
        auto = Admission(self.store, self.private, self.sandbox, LANGUAGES, self.base / "w5", approval="auto")
        record = auto.process(auto.submit(bundle_bytes(), "agent:builder")["id"])
        self.assertEqual((record["state"], record["decided_by"]), ("admitted", "policy:auto"))
        self.assertEqual(self.store.get_row("tool", "word-count", "1.0.0")["admitted_by"], "policy:auto")

    def test_under_auto_approval_an_agent_never_sees_awaiting_approval(self):
        """Found by the smoke test against the running service: an agent polling saw awaiting_approval for a moment
        and stopped, as if a person had to act. Under auto there is no one to wait for."""
        auto = Admission(self.store, self.private, self.sandbox, LANGUAGES, self.base / "w7", approval="auto")
        seen = []
        original = self.store.save_submission
        self.store.save_submission = lambda record: (seen.append(record["state"]), original(record))[1]
        auto.process(auto.submit(bundle_bytes(), "agent:builder")["id"])
        self.assertEqual(seen, ["received", "validating", "building", "testing", "admitted"])

    def test_auto_approval_still_rejects_what_fails(self):
        auto = Admission(self.store, self.private, self.sandbox, LANGUAGES, self.base / "w6", approval="auto")
        record = auto.process(auto.submit(bundle_bytes(source="def broken(:\n"), "agent:builder")["id"])
        self.assertEqual(record["state"], "rejected")
        self.assertEqual(self.store.count(), 0)

    def test_reserved_principals_can_neither_submit_nor_approve(self):
        for name in ("policy:auto", "file-index", "policy:anything"):
            with self.subTest(principal=name):
                with self.assertRaises(AdmissionError) as ctx:
                    self.admission.submit(bundle_bytes(), name)
                self.assertEqual(ctx.exception.code, "Forbidden")
        record = self.run_through()
        with self.assertRaises(AdmissionError) as ctx:
            self.admission.approve(record["id"], "policy:auto")  # manual policy: nobody may pose as the policy
        self.assertEqual(ctx.exception.code, "Forbidden")

    # retries and names
    def test_a_retried_submission_returns_the_original_and_runs_nothing_again(self):
        first = self.run_through()
        calls = len(self.sandbox.calls)
        again = self.admission.submit(bundle_bytes(), "author:acme")
        self.assertEqual((again["id"], again["duplicate"]), (first["id"], True))
        self.assertEqual(self.admission.process(again["id"])["state"], "awaiting_approval")
        self.assertEqual(len(self.sandbox.calls), calls)
        other = self.admission.submit(bundle_bytes(), "author:someone-else")
        self.assertNotEqual(other["id"], first["id"])  # a retry is per submitter

    # ownership: admitted code is Taffee's (R-D14, R-D15)
    def test_admitted_code_is_committed_to_the_library_and_owned_by_it(self):
        record = self.admission.approve(self.run_through()["id"], "approver:sec")
        row = self.store.get_row("tool", "word-count", "1.0.0")
        self.assertEqual(row["manifest"]["owner"], LIBRARY_OWNER)
        self.assertEqual(row["source"]["provenance"], "taffee-agent")
        self.assertEqual(record["library_commit"], self.library.head())
        self.assertEqual(self.library.log(1), ["Admit word-count 1.0.0"])
        lib = self.library.path
        self.assertEqual((lib / "library/word-count/python/main.py").read_text(encoding="utf-8"), WORD_COUNT)
        self.assertEqual(json.loads((lib / "catalog/tool/word-count/rows/1.0.0.json").read_bytes()), row)
        suite = [case["id"] for case in self.library.cases_for("word-count")]
        self.assertEqual(suite, [f"1.0.0/{case['id']}" for case in CASES])
        body = self.library._git("log", "-1", "--format=%B")
        for trailer in (f"Submission: {record['id']}", f"Identity: {row['identity']}", "Approved-By: approver:sec",
                        "Submitted-By: author:acme", "Row-Seq: 1"):
            self.assertIn(trailer, body)

    def test_any_taffee_agent_may_propose_a_new_version_but_must_pass_the_regression_suite(self):
        self.admission.approve(self.run_through(submitter="agent:a")["id"], "approver:sec")
        # 1.1.0 from another agent: its own cases pass, but it counts the path's words instead of reading the file,
        # which 1.0.0's suite checks
        regressed = WORD_COUNT.replace('open(os.path.join(req["root"], data["path"]), encoding="utf-8").read()',
                                       'data["path"]')
        own_cases = [CASES[0], {"id": "no-input", "input": {}, "expect": {"ok": False}}]
        record = self.run_through(bundle_bytes(manifest=word_count_manifest(version="1.1.0"), source=regressed,
                                               cases=own_cases), submitter="agent:b")
        self.assertEqual(record["state"], "rejected")
        self.assertTrue(any("regression 1.0.0/counts-a-file" in p for p in record["problems"]), record["problems"])
        self.assertTrue(all("/regression 1.0.0/" in p for p in record["problems"]), record["problems"])

    def test_a_new_major_version_starts_a_new_suite(self):
        self.admission.approve(self.run_through(submitter="agent:a")["id"], "approver:sec")
        changed = WORD_COUNT.replace("str(len(text.split()))", "str(len(text))")  # counts characters now
        cases = [{"id": "chars", "input": {"text": "abc"}, "expect": {"ok": True, "contains": '"output": "3"'}},
                 CASES[2]]
        record = self.run_through(bundle_bytes(manifest=word_count_manifest(version="2.0.0"), source=changed,
                                               cases=cases), submitter="agent:b")
        self.assertEqual(record["state"], "awaiting_approval", record["problems"])

    def test_built_in_tool_names_cannot_be_taken_over(self):
        fields = synthetic_fields("grep", "1.2.0")
        fields.update(source={"type": "file-index", "index_digest": "sha256:" + "1" * 64},
                      submitted_by="file-index", admitted_by="file-index")
        add_rows(self.store, self.private, [fields])
        manifest = word_count_manifest(name="grep", version="9.0.0")
        record = self.run_through(bundle_bytes(manifest=manifest))
        self.assertEqual(record["state"], "rejected")
        self.assertIn("BuiltIn: grep is a built-in tool", record["problems"][0])

    def test_two_agents_racing_for_one_new_version_cannot_both_win(self):
        a = self.run_through(submitter="agent:a")
        b = self.run_through(bundle_bytes(source=WORD_COUNT + "# b\n"), submitter="agent:b")
        self.assertEqual((a["state"], b["state"]), ("awaiting_approval", "awaiting_approval"))
        self.admission.approve(a["id"], "approver:sec")
        lost = self.admission.approve(b["id"], "approver:sec")
        self.assertEqual(lost["state"], "rejected")
        self.assertIn("VersionConflict", lost["problems"][0])

    def test_a_failed_library_commit_is_recovered_by_sync(self):
        original = self.library.admit
        self.library.admit = lambda *a, **k: (_ for _ in ()).throw(RuntimeError("disk full"))
        record = self.admission.approve(self.run_through()["id"], "approver:sec")
        self.assertEqual((record["state"], record["library_commit"]), ("admitted", None))
        self.assertFalse(self.library.has("word-count", "1.0.0"))
        self.library.admit = original
        self.assertEqual(self.admission.sync_library(), 1)
        self.assertTrue(self.library.has("word-count", "1.0.0"))
        self.assertEqual(self.admission.sync_library(), 0)

    # approval (REG-ADM-6, 7)
    def test_a_submitter_cannot_approve_their_own_tool(self):
        record = self.run_through()
        with self.assertRaises(AdmissionError) as ctx:
            self.admission.approve(record["id"], "author:acme")
        self.assertEqual(ctx.exception.code, "Forbidden")
        self.assertEqual(self.admission.get(record["id"])["state"], "awaiting_approval")

    def test_only_a_waiting_submission_can_be_approved(self):
        record = self.admission.submit(bundle_bytes(), "author:acme")
        with self.assertRaises(AdmissionError) as ctx:
            self.admission.approve(record["id"], "approver:sec")
        self.assertEqual(ctx.exception.code, "InvalidState")

    def test_a_rejected_submission_never_becomes_visible(self):
        record = self.run_through()
        rejected = self.admission.reject(record["id"], "approver:sec", "not needed")
        self.assertEqual((rejected["state"], rejected["problems"]), ("rejected", ["rejected by the approver: not needed"]))
        self.assertEqual(self.store.count(), 0)
        self.assertEqual(self.registry.search("count")["items"], [])

    def test_an_unknown_submission_is_not_found(self):
        with self.assertRaises(AdmissionError) as ctx:
            self.admission.get("sub-00000000000000000000")
        self.assertEqual(ctx.exception.code, "NotFound")

    # versions (REG-ADM-8)
    def test_a_version_is_immutable_and_the_same_tool_again_is_a_no_op(self):
        self.admission.approve(self.run_through()["id"], "approver:sec")
        again = self.run_through()
        self.assertEqual((again["state"], again["row_seq"]), ("admitted", 1))
        changed = self.run_through(bundle_bytes(source=WORD_COUNT + "\n# changed\n"))
        self.assertEqual(changed["state"], "rejected")
        self.assertIn("VersionConflict", changed["problems"][0])
        self.assertEqual(self.store.count(), 1)
        newer = self.run_through(bundle_bytes(manifest=word_count_manifest(version="1.0.1"), source=WORD_COUNT + "\n#\n"))
        self.admission.approve(newer["id"], "approver:sec")
        self.assertEqual(self.registry.describe("word-count")["version"], "1.0.1")

    # restart (REG-ADM-9)
    def test_resume_finishes_submissions_left_open_by_a_restart(self):
        record = self.admission.submit(bundle_bytes(), "author:acme")
        record["state"] = "testing"  # as if the process died during testing
        self.store.save_submission(record)
        restarted = Admission(self.store, self.private, self.sandbox, LANGUAGES, self.base / "work")
        self.assertEqual(restarted.resume(), [record["id"]])
        self.assertEqual(restarted.get(record["id"])["state"], "awaiting_approval")


@unittest.skipIf(SKIP_REASON is not None, f"container tests need Docker: {SKIP_REASON}")
class ContainerAdmissionTest(unittest.TestCase):
    """The real ContainerSandbox. The submitted code runs as an unprivileged user with no network."""

    @classmethod
    def setUpClass(cls):
        cls._tmp = tempfile.TemporaryDirectory(dir=os.environ.get("TAFFEE_SANDBOX_TMP"))
        cls.base = Path(cls._tmp.name).resolve()
        os.chmod(cls.base, 0o755)
        cls.sandbox = ContainerSandbox(IMAGE, allowed_mount_roots=(cls.base,), cleanup_wait_s=30.0)

    @classmethod
    def tearDownClass(cls):
        cls._tmp.cleanup()

    def setUp(self):
        self.root = Path(tempfile.mkdtemp(dir=self.base))
        os.chmod(self.root, 0o755)
        self.store = SQLiteStore(self.root / "data")
        self.private, self.public = admission_key()
        self.admission = Admission(self.store, self.private, self.sandbox, LANGUAGES, self.root / "work")

    def tearDown(self):
        self.store.close()

    def test_an_agent_written_tool_passes_its_cases_in_a_container(self):
        record = self.admission.process(self.admission.submit(bundle_bytes(), "author:acme")["id"])
        self.assertEqual(record["state"], "awaiting_approval", record["problems"])

    def test_a_tool_that_needs_the_network_fails_its_case_in_the_container(self):
        phone_home = ('import json, socket, sys\nsys.stdin.read()\n'
                      'try:\n    socket.create_connection(("1.1.1.1", 53), timeout=3).close()\n'
                      '    print(json.dumps({"ok": True, "output": "1"}))\n'
                      'except OSError:\n    print(json.dumps({"ok": False, "error": "offline"}))\n')
        cases = with_error_case([{"id": "needs-network", "input": {"text": "a"}, "expect": {"ok": True}}])
        record = self.admission.process(self.admission.submit(bundle_bytes(source=phone_home, cases=cases), "author:x")["id"])
        self.assertEqual(record["state"], "rejected")
        self.assertEqual(record["problems"], ["python/needs-network: ok: wanted True, got False"])

    def test_a_tool_cannot_read_outside_its_repository(self):
        snoop = ('import json, sys\nreq = json.loads(sys.stdin.read())\n'
                 'try:\n    open(req["input"]["path"]).read()\n    print(json.dumps({"ok": True, "output": "read"}))\n'
                 'except OSError:\n    print(json.dumps({"ok": False, "error": "denied"}))\n')
        outside = self.root / "outside-canary.txt"
        outside.write_text("canary", encoding="utf-8")
        cases = with_error_case([{"id": "reads-host-file", "input": {"path": str(outside)}, "expect": {"ok": True}}])
        record = self.admission.process(self.admission.submit(bundle_bytes(source=snoop, cases=cases), "author:x")["id"])
        self.assertEqual(record["problems"], ["python/reads-host-file: ok: wanted True, got False"])


if __name__ == "__main__":
    unittest.main()
