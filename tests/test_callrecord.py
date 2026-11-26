"""S-012: call records (CAP-REC-1 to CAP-REC-11)."""

import json
import os
import shutil
import stat
import tempfile
import unittest
from pathlib import Path

from capability.calltime import CallError, Problem, validate_input, validate_output
from capability.callrecord import (
    GENESIS,
    CallRecorder,
    CallRecordError,
    calls_for,
    capability_ref,
    outcome_for,
    verify_log,
)
from capability.conformance import run_implementation
from capability.index import build_index
from capability.registry import Registry, RegistryError
from capability.revocation import GENESIS as REVOCATION_GENESIS
from capability.revocation import RevocationClient, entry_hash, load_public_key

from fixture_support import REPO, copy_suite
from revocation_support import Clocks, FakeFeed, make_key

SECRET = "s3cr3t-value-that-must-never-be-logged"
H1, H2, H3 = ("sha256:" + c * 64 for c in "123")
CAP = {"kind": "tool", "name": "grep", "version": "1.2.0", "digest": H1, "label": "Tested"}


class RecorderCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.path = Path(self._tmp.name) / "calls.jsonl"
        self.clock = Clocks()
        self.recorder = CallRecorder(self.path, clock=self.clock.wall, fsync=False)

    def tearDown(self):
        self._tmp.cleanup()

    def ok(self):
        return outcome_for({"ok": True, "output": "x"})

    def write(self, call_id="c-1", capability=None, **kwargs):
        kwargs.setdefault("outcome", self.ok())
        return self.recorder.record(call_id, CAP if capability is None else capability, **kwargs)

    def lines(self):
        return [line for line in self.path.read_bytes().split(b"\n") if line]


class ShapeAndChainTest(RecorderCase):
    def test_the_first_record_links_to_genesis_and_each_links_to_the_last(self):
        one, two, three = self.write("c-1"), self.write("c-2"), self.write("c-3")
        self.assertEqual((one["seq"], one["prev"]), (1, GENESIS))
        self.assertEqual((two["prev"], three["prev"]), (one["hash"], two["hash"]))
        self.assertEqual([r["seq"] for r in verify_log(self.path)], [1, 2, 3])

    def test_a_record_has_exactly_the_specified_fields(self):
        record = self.write(language="python", code_digest=H2, revocation_seq=4, grant="grant:9f2c",
                            input_value={"pattern": "x"}, output="a.txt:1:x", duration_ms=12)
        self.assertEqual(set(record), {"schema", "seq", "prev", "call_id", "time", "capability", "language",
                                       "code_digest", "revocation", "grant", "outcome", "input", "output",
                                       "duration_ms", "hash"})
        self.assertEqual(record["schema"], "capability-call-record/1")
        self.assertEqual(record["time"], "2027-01-15T08:00:00Z")
        self.assertEqual(record["revocation"], {"status": "checked", "seq": 4})
        self.assertEqual((record["grant"], record["language"], record["code_digest"]), ("grant:9f2c", "python", H2))
        self.assertEqual(record["duration_ms"], 12)

    def test_unchecked_revocation_is_recorded_as_such(self):
        self.assertEqual(self.write()["revocation"], {"status": "unchecked", "seq": None})

    def test_a_checked_sequence_of_zero_is_checked_not_unchecked(self):
        self.assertEqual(self.write(revocation_seq=0)["revocation"], {"status": "checked", "seq": 0})

    def test_each_record_is_one_line_and_every_hash_recomputes(self):
        self.write("c-1"), self.write("c-2")
        self.assertEqual(len(self.lines()), 2)
        for line in self.lines():
            self.assertNotIn(b"\n", line)
            self.assertEqual(json.loads(line)["hash"][:7], "sha256:")
        self.assertEqual(len(verify_log(self.path)), 2)

    def test_a_missing_log_verifies_as_empty_and_the_head_starts_at_genesis(self):
        self.assertEqual(verify_log(Path(self._tmp.name) / "none.jsonl"), [])
        self.assertEqual(self.recorder.head(), (0, GENESIS))
        record = self.write()
        self.assertEqual(self.recorder.head(), (1, record["hash"]))

    def test_a_reopened_log_continues_the_chain(self):
        first = self.write("c-1")
        reopened = CallRecorder(self.path, clock=self.clock.wall, fsync=False)
        self.assertEqual(reopened.head(), (1, first["hash"]))
        second = reopened.record("c-2", CAP, self.ok())
        self.assertEqual((second["seq"], second["prev"]), (2, first["hash"]))
        self.assertEqual(len(verify_log(self.path)), 2)

    def test_the_file_is_owner_only(self):
        self.write()
        self.assertEqual(stat.S_IMODE(os.stat(self.path).st_mode), 0o600)


class NoContentTest(RecorderCase):
    def test_a_secret_in_the_input_the_output_and_an_error_never_reaches_the_file(self):
        self.write("c-1", input_value={"token": SECRET}, output="out " + SECRET)
        self.write("c-2", outcome=outcome_for(error=RuntimeError(SECRET)), input_value=SECRET)
        self.write("c-3", outcome=outcome_for({"ok": False, "error": SECRET}))
        self.write("c-4", outcome=outcome_for(error=CallError("ValidationFailed", [Problem("/x", SECRET)])))
        self.write("c-5", outcome=outcome_for(error=RegistryError("Revoked", SECRET)))
        self.assertNotIn(SECRET.encode(), self.path.read_bytes())
        self.assertNotIn(b"s3cr3t", self.path.read_bytes())

    def test_input_and_output_are_a_digest_and_a_byte_count(self):
        record = self.write(input_value={"pattern": "x"}, output="hello")
        self.assertEqual(set(record["input"]), {"digest", "bytes"})
        self.assertEqual(record["output"]["bytes"], 5)
        self.assertRegex(record["input"]["digest"], r"^sha256:[0-9a-f]{64}$")

    def test_no_input_and_no_output_are_null(self):
        record = self.write()
        self.assertEqual((record["input"], record["output"]), (None, None))

    def test_digests_are_deterministic(self):
        a = self.write("c-1", input_value={"b": 1, "a": 2}, output="x")
        b = self.write("c-2", input_value={"a": 2, "b": 1}, output="x")
        self.assertEqual(a["input"], b["input"])
        self.assertEqual(a["output"], b["output"])

    def test_a_deployment_key_changes_the_digests_and_keeps_them_stable(self):
        keyed = CallRecorder(Path(self._tmp.name) / "keyed.jsonl", clock=self.clock.wall, digest_key=b"deployment-key", fsync=False)
        plain = self.write(input_value={"n": 3}, output="x")
        one = keyed.record("c-1", CAP, self.ok(), input_value={"n": 3}, output="x")
        two = keyed.record("c-2", CAP, self.ok(), input_value={"n": 3}, output="x")
        self.assertNotEqual(one["input"]["digest"], plain["input"]["digest"])
        self.assertEqual(one["input"], two["input"])
        other = CallRecorder(Path(self._tmp.name) / "other.jsonl", clock=self.clock.wall, digest_key=b"another-key", fsync=False)
        self.assertNotEqual(other.record("c-1", CAP, self.ok(), input_value={"n": 3})["input"]["digest"], one["input"]["digest"])

    def test_a_value_that_is_not_json_text_is_refused_before_writing(self):
        with self.assertRaises(CallRecordError):
            self.write(input_value={"x": {1, 2}})
        self.assertFalse(self.path.exists())


class OutcomeTest(unittest.TestCase):
    def test_every_status_is_produced(self):
        cases = [
            (outcome_for({"ok": True, "output": ""}), ("ok", None)),
            (outcome_for({"ok": False, "error": "x"}), ("tool_failed", None)),
            (outcome_for(error=CallError("ValidationFailed", [Problem("/n", "type")])), ("input_rejected", "ValidationFailed")),
            (outcome_for(error=CallError("PostconditionFailed", [Problem("output", "x")])), ("output_rejected", "PostconditionFailed")),
            (outcome_for(error=RegistryError("Revoked", "x")), ("refused", "Revoked")),
            (outcome_for(error=RegistryError("RevocationStale", "x")), ("refused", "RevocationStale")),
            (outcome_for(error=RegistryError("IntegrityError", "x")), ("refused", "IntegrityError")),
            (outcome_for(error=RuntimeError("boom")), ("error", "Unexpected")),
            (outcome_for(error=KeyError("k")), ("error", "Unexpected")),
            (outcome_for("not a result"), ("error", "Unexpected")),
            (outcome_for(None), ("error", "Unexpected")),
        ]
        for outcome, (status, code) in cases:
            with self.subTest(status=status, code=code):
                self.assertEqual((outcome["status"], outcome["code"]), (status, code))

    def test_an_error_with_a_code_attribute_is_a_refusal_with_that_code(self):
        class GrantDenied(Exception):
            code = "GrantDenied"
        self.assertEqual(outcome_for(error=GrantDenied("nope")), {"status": "refused", "code": "GrantDenied"})

    def test_a_code_attribute_that_is_not_a_plain_code_is_not_trusted(self):
        class Odd(Exception):
            code = "has spaces and " + SECRET
        self.assertEqual(outcome_for(error=Odd()), {"status": "error", "code": "Unexpected"})


class OutcomeCodeTest(RecorderCase):
    """review 9c872f9e: a code is never free text, and must be one the status may carry."""

    def refused(self, outcome):
        with self.assertRaises(CallRecordError):
            self.write(outcome=outcome)
        self.assertFalse(self.path.exists())

    def test_a_made_up_code_is_refused_and_secret_text_never_reaches_disk(self):
        for outcome in ({"status": "error", "code": SECRET}, {"status": "refused", "code": SECRET},
                        {"status": "refused", "code": "Revoked " + SECRET}, {"status": "error", "code": "Exception: " + SECRET},
                        {"status": "ok", "code": SECRET}, {"status": "tool_failed", "code": SECRET},
                        {"status": "input_rejected", "code": SECRET}, {"status": "output_rejected", "code": SECRET}):
            with self.subTest(outcome=str(outcome)[:50]):
                self.refused(outcome)
        self.assertFalse(self.path.exists())

    def test_a_code_that_does_not_belong_to_its_status_is_refused(self):
        for outcome in ({"status": "ok", "code": "Revoked"}, {"status": "ok", "code": "Unexpected"},
                        {"status": "tool_failed", "code": "ValidationFailed"},
                        {"status": "input_rejected", "code": "PostconditionFailed"},
                        {"status": "input_rejected", "code": None}, {"status": "output_rejected", "code": "Revoked"},
                        {"status": "refused", "code": None}, {"status": "refused", "code": "Unexpected"},
                        {"status": "refused", "code": "ValidationFailed"}, {"status": "error", "code": None},
                        {"status": "error", "code": "Revoked"}, {"status": "error", "code": ["x"]},
                        {"status": "refused", "code": {"a": 1}}):
            with self.subTest(outcome=str(outcome)):
                self.refused(outcome)

    def test_every_valid_status_and_code_pair_is_accepted(self):
        from capability.callrecord import OUTCOME_CODES
        for status, codes in OUTCOME_CODES.items():
            for code in sorted(codes, key=str):
                with self.subTest(status=status, code=code):
                    self.write(f"c-{status}-{code}"[:60], outcome={"status": status, "code": code})
        self.assertEqual(len(verify_log(self.path)), sum(len(c) for c in OUTCOME_CODES.values()))

    def test_the_refusal_codes_are_the_stable_error_codes_and_nothing_else(self):
        from capability.callrecord import REFUSAL_CODES
        self.assertEqual(REFUSAL_CODES, {"IndexError", "SetupError", "NotFound", "BelowLabel", "Ambiguous", "InvalidRange",
                                         "NoImplementation", "IntegrityError", "EntryError", "SourceUnavailable",
                                         "Revoked", "RevocationStale", "GrantDenied", "SandboxUnavailable"})

    def test_outcome_for_never_copies_a_code_it_does_not_know(self):
        for error in (RegistryError("MadeUpCode", "x"), RegistryError(SECRET, "x"),
                      CallError("SomethingElse", [Problem("/x", "y")])):
            with self.subTest(error=repr(error)[:40]):
                self.assertEqual(outcome_for(error=error), {"status": "error", "code": "Unexpected"})
        self.write(outcome=outcome_for(error=RegistryError(SECRET, "x")))
        self.assertNotIn(b"s3cr3t", self.path.read_bytes())

    def test_the_outcome_for_and_record_pair_always_agree(self):
        for result in ({"ok": True, "output": ""}, {"ok": False, "error": "x"}, None, "x"):
            self.write(outcome=outcome_for(result))
        for error in (RuntimeError(SECRET), RegistryError("Revoked", "x"), CallError("ValidationFailed", [Problem("/n", "t")]),
                      CallError("PostconditionFailed", [Problem("o", "t")]), KeyError(SECRET)):
            self.write(outcome=outcome_for(error=error))
        self.assertNotIn(b"s3cr3t", self.path.read_bytes())
        self.assertEqual(len(verify_log(self.path)), 9)


class WriteIntegrityTest(RecorderCase):
    """review 9c872f9e: never report success on a partial line."""

    def patched_write(self, wrapper):
        from unittest import mock
        return mock.patch("capability.callrecord.os.write", side_effect=wrapper)

    def test_short_writes_are_completed_and_the_record_is_whole(self):
        real = os.write
        calls = []

        def three_bytes_at_a_time(descriptor, data):
            calls.append(len(data))
            return real(descriptor, bytes(data[:3]))
        with self.patched_write(three_bytes_at_a_time):
            record = self.write(input_value={"a": 1})
        self.assertGreater(len(calls), 10)  # it really did loop
        self.assertEqual(verify_log(self.path)[0]["hash"], record["hash"])
        self.assertEqual(self.recorder.head(), (1, record["hash"]))

    def test_a_write_that_fails_part_way_leaves_the_log_as_it_was_and_the_head_unmoved(self):
        first = self.write("c-1")
        size = self.path.stat().st_size
        real = os.write
        state = {"calls": 0}

        def half_then_fail(descriptor, data):
            state["calls"] += 1
            if state["calls"] == 1:
                return real(descriptor, bytes(data[: len(data) // 2]))
            raise OSError(28, "No space left on device")
        with self.patched_write(half_then_fail):
            with self.assertRaises(CallRecordError) as ctx:
                self.write("c-2")
        self.assertIn("left as it was", str(ctx.exception))
        self.assertEqual(self.path.stat().st_size, size)
        self.assertEqual(self.recorder.head(), (1, first["hash"]))
        self.assertEqual(len(verify_log(self.path)), 1)
        again = self.write("c-3")  # the recorder is still usable and the chain continues
        self.assertEqual((again["seq"], again["prev"]), (2, first["hash"]))
        self.assertEqual(len(verify_log(self.path)), 2)

    def test_a_write_that_makes_no_progress_is_a_failure_not_a_hang(self):
        with self.patched_write(lambda descriptor, data: 0):
            with self.assertRaises(CallRecordError):
                self.write()
        self.assertEqual(self.path.read_bytes(), b"")
        self.assertEqual(self.recorder.head(), (0, GENESIS))

    def test_if_the_truncation_also_fails_the_error_says_the_log_may_hold_a_partial_record(self):
        from unittest import mock
        real = os.write

        def half_then_fail(descriptor, data):
            real(descriptor, bytes(data[: len(data) // 2]))
            raise OSError(5, "I/O error")
        with self.patched_write(half_then_fail):
            with mock.patch("capability.callrecord.os.ftruncate", side_effect=OSError(5, "I/O error")):
                with self.assertRaises(CallRecordError) as ctx:
                    self.write()
        self.assertIn("may hold a partial record", str(ctx.exception))
        self.assertEqual(self.recorder.head(), (0, GENESIS))  # the head still did not move
        with self.assertRaises(CallRecordError):
            verify_log(self.path)  # and the partial line is detected, not trusted
        with self.assertRaises(CallRecordError) as stopped:
            self.write("c-2")  # the recorder refuses to append with a head it can no longer trust
        self.assertIn("stopped", str(stopped.exception))

    def test_a_whole_line_whose_sync_and_truncation_both_fail_is_a_known_limit(self):
        """review dab9f60c: here the failed call leaves a VALID record on disk, so verify_log cannot flag it."""
        from unittest import mock
        durable = CallRecorder(self.path, clock=self.clock.wall, fsync=True)
        with mock.patch("capability.callrecord.os.fsync", side_effect=OSError(5, "I/O error")):
            with mock.patch("capability.callrecord.os.ftruncate", side_effect=OSError(5, "I/O error")):
                with self.assertRaises(CallRecordError) as ctx:
                    durable.record("c-1", CAP, self.ok())
        self.assertIn("whole record this call reported as failed", str(ctx.exception))
        # The limit, stated: the caller was told the write failed, yet the log holds a complete, valid record.
        on_disk = verify_log(self.path)
        self.assertEqual([r["call_id"] for r in on_disk], ["c-1"])
        # And the recorder's head diverged from the log, so it must not append.
        self.assertEqual(durable.head(), (0, GENESIS))
        with self.assertRaises(CallRecordError) as stopped:
            durable.record("c-2", CAP, self.ok())
        self.assertIn("stopped", str(stopped.exception))
        self.assertEqual(len(verify_log(self.path)), 1)  # nothing was appended on the stale head
        # A new recorder reads the log, sees the record, and continues the chain correctly.
        fresh = CallRecorder(self.path, clock=self.clock.wall, fsync=False)
        self.assertEqual(fresh.head(), (1, on_disk[0]["hash"]))
        self.assertEqual(fresh.record("c-2", CAP, self.ok())["prev"], on_disk[0]["hash"])

    def test_a_failed_sync_rolls_the_record_back(self):
        from unittest import mock
        durable = CallRecorder(self.path, clock=self.clock.wall, fsync=True)
        with mock.patch("capability.callrecord.os.fsync", side_effect=OSError(5, "I/O error")):
            with self.assertRaises(CallRecordError):
                durable.record("c-1", CAP, self.ok())
        self.assertEqual(self.path.read_bytes(), b"")
        self.assertEqual(durable.head(), (0, GENESIS))
        self.assertEqual(durable.record("c-2", CAP, self.ok())["seq"], 1)

    def test_a_full_write_is_one_clean_line_ending_in_a_newline(self):
        self.write("c-1"), self.write("c-2")
        raw = self.path.read_bytes()
        self.assertTrue(raw.endswith(b"\n"))
        self.assertEqual(raw.count(b"\n"), 2)


class ValidationTest(RecorderCase):
    def refused(self, **kwargs):
        with self.assertRaises(CallRecordError):
            self.write(**kwargs)
        self.assertFalse(self.path.exists())

    def test_a_bad_call_id_is_refused(self):
        for bad in ("", " ", "a b", "x" * 65, "-lead", "a/b", "a\n", None, 5):
            with self.subTest(call_id=bad):
                with self.assertRaises(CallRecordError):
                    self.recorder.record(bad, CAP, self.ok())
        self.assertFalse(self.path.exists())

    def test_a_bad_grant_is_refused(self):
        for bad in ("", "has space", "x" * 129, "line\nbreak", "free text with " + SECRET, 5):
            with self.subTest(grant=bad):
                self.refused(grant=bad)

    def test_a_bad_duration_or_revocation_sequence_is_refused(self):
        for bad in (-1, 1.5, "5", True):
            with self.subTest(duration=bad):
                self.refused(duration_ms=bad)
        for bad in (-1, 1.5, "5", True):
            with self.subTest(seq=bad):
                self.refused(revocation_seq=bad)

    def test_a_bad_capability_is_refused(self):
        for bad in ({}, dict(CAP, kind="plugin"), dict(CAP, name="Bad_Name"), dict(CAP, version="1.0"),
                    dict(CAP, digest="sha256:short"), dict(CAP, label="Gold"), dict(CAP, extra=1)):
            with self.subTest(capability=str(bad)[:40]):
                self.refused(capability=bad)

    def test_a_capability_that_was_never_resolved_may_have_no_digest_or_label(self):
        record = self.write(capability=dict(CAP, digest=None, label=None))
        self.assertEqual((record["capability"]["digest"], record["capability"]["label"]), (None, None))

    def test_a_bad_outcome_digest_language_are_refused(self):
        self.refused(outcome={"status": "great", "code": None})
        self.refused(outcome={"status": "ok"})
        self.refused(code_digest="sha256:nope")
        self.refused(language="Has Space")


class TamperTest(RecorderCase):
    def setUp(self):
        super().setUp()
        for i in range(4):
            self.write(f"c-{i}", input_value={"i": i})

    def rewrite(self, change):
        lines = self.lines()
        lines = change(lines)
        self.path.write_bytes(b"\n".join(lines) + b"\n")

    def broken(self):
        with self.assertRaises(CallRecordError):
            verify_log(self.path)
        with self.assertRaises(CallRecordError):
            CallRecorder(self.path, clock=self.clock.wall, fsync=False)

    def test_an_intact_log_verifies(self):
        self.assertEqual(len(verify_log(self.path)), 4)

    def test_an_edited_record_is_detected(self):
        self.rewrite(lambda lines: [lines[0], lines[1].replace(b'"c-1"', b'"c-9"'), *lines[2:]])
        self.broken()

    def test_an_edited_record_with_its_hash_fixed_is_still_detected_by_the_chain(self):
        def change(lines):
            record = json.loads(lines[1])
            record["call_id"] = "c-9"
            from capability.callrecord import _record_hash
            record["hash"] = _record_hash(record)
            return [lines[0], json.dumps(record, sort_keys=True, separators=(",", ":")).encode(), *lines[2:]]
        self.rewrite(change)
        self.broken()

    def test_a_removed_middle_record_is_detected(self):
        self.rewrite(lambda lines: [lines[0], lines[2], lines[3]])
        self.broken()

    def test_an_inserted_record_is_detected(self):
        self.rewrite(lambda lines: [lines[0], lines[1], lines[1], lines[2], lines[3]])
        self.broken()

    def test_reordered_records_are_detected(self):
        self.rewrite(lambda lines: [lines[0], lines[2], lines[1], lines[3]])
        self.broken()

    def test_a_non_record_line_and_garbage_are_detected(self):
        for extra in (b'{"not": "a record"}', b"garbage", b"[]"):
            with self.subTest(extra=extra):
                self.rewrite(lambda lines, e=extra: [*lines, e])
                self.broken()
                self.rewrite(lambda lines: lines[:-1])
                self.assertEqual(len(verify_log(self.path)), 4)

    def test_a_broken_log_is_never_appended_to(self):
        self.rewrite(lambda lines: [lines[0], lines[2]])
        before = self.path.read_bytes()
        with self.assertRaises(CallRecordError):
            CallRecorder(self.path, clock=self.clock.wall, fsync=False)
        self.assertEqual(self.path.read_bytes(), before)

    def test_two_writers_fork_the_chain_and_it_is_detected(self):
        a = CallRecorder(self.path, clock=self.clock.wall, fsync=False)
        b = CallRecorder(self.path, clock=self.clock.wall, fsync=False)
        a.record("a-1", CAP, self.ok())
        b.record("b-1", CAP, self.ok())  # b still believes the old head
        self.broken()

    def test_removing_the_newest_records_alone_is_not_detectable_but_the_head_exposes_it(self):
        head = self.recorder.head()
        self.rewrite(lambda lines: lines[:2])
        self.assertEqual(len(verify_log(self.path)), 2)              # documented limit (CAP-REC-7)
        self.assertNotEqual(CallRecorder(self.path, clock=self.clock.wall, fsync=False).head(), head)


class QueryTest(RecorderCase):
    def setUp(self):
        super().setUp()
        glob = dict(CAP, name="glob", digest=H2)
        grep_old = dict(CAP, version="1.1.0", digest=H3)
        self.write("c-1", capability=CAP, revocation_seq=3, code_digest=H2)
        self.write("c-2", capability=glob, revocation_seq=3)
        self.write("c-3", capability=grep_old, revocation_seq=3)
        self.write("c-4", capability=CAP, revocation_seq=5, outcome=outcome_for(error=RegistryError("Revoked", "x")))
        self.write("c-5", capability=dict(CAP, kind="agent"), revocation_seq=5)

    def ids(self, **kwargs):
        return [r["call_id"] for r in calls_for(self.path, **kwargs)]

    def test_by_name_and_kind(self):
        self.assertEqual(self.ids(kind="tool", name="grep"), ["c-1", "c-3", "c-4"])
        self.assertEqual(self.ids(kind="agent", name="grep"), ["c-5"])

    def test_by_version(self):
        self.assertEqual(self.ids(kind="tool", name="grep", version="1.2.0"), ["c-1", "c-4"])
        self.assertEqual(self.ids(kind="tool", name="grep", version="1.1.0"), ["c-3"])

    def test_by_digest_matches_the_capability_digest_or_the_code_digest(self):
        self.assertEqual(self.ids(kind="tool", name="grep", digest=H1), ["c-1", "c-4"])
        self.assertEqual(self.ids(kind="tool", name="grep", digest=H2), ["c-1"])  # c-1 ran code H2

    def test_by_outcome_and_from_a_sequence(self):
        self.assertEqual(self.ids(kind="tool", name="grep", outcome="refused"), ["c-4"])
        self.assertEqual(self.ids(kind="tool", name="grep", since_seq=3), ["c-3", "c-4"])

    def test_an_unknown_kind_is_refused_and_a_missing_log_is_empty(self):
        with self.assertRaises(CallRecordError):
            calls_for(self.path, "plugin", "grep")
        self.assertEqual(calls_for(Path(self._tmp.name) / "none.jsonl", "tool", "grep"), [])

    def test_the_query_verifies_the_log_first(self):
        self.path.write_bytes(self.path.read_bytes().replace(b'"c-2"', b'"c-9"'))
        with self.assertRaises(CallRecordError):
            calls_for(self.path, "tool", "grep")


class EndToEndTest(unittest.TestCase):
    """A real resolve, input check, tool run and output check, recorded."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name) / "tree"
        self.root.mkdir()
        for folder in ("schemas", "policy", "catalog", "library", "evidence"):
            shutil.copytree(REPO / folder, self.root / folder)
        copy_suite(self.root)
        build_index(self.root)
        self.log = Path(self._tmp.name) / "calls.jsonl"
        self.clock = Clocks()
        private, encoded = make_key()
        self.private = private
        self.client = RevocationClient(load_public_key(encoded), wall=self.clock.wall, monotonic=self.clock.mono)
        self.feed = FakeFeed(self.clock, private)
        self.client.poll(self.feed)
        self.registry = Registry(self.root, revocation=self.client)
        self.recorder = CallRecorder(self.log, clock=self.clock.wall, fsync=False)
        self.repo = Path(self._tmp.name) / "repo"
        self.repo.mkdir()
        (self.repo / "a.txt").write_text("hello\n" + SECRET + "\n", encoding="utf-8")

    def tearDown(self):
        self._tmp.cleanup()

    def call(self, call_id, tool_input):
        try:
            match = self.registry.resolve("grep", languages=["python"])
        except RegistryError as exc:
            return self.recorder.record(call_id, {"kind": "tool", "name": "grep", "version": "1.2.0", "digest": None, "label": None},
                                        outcome_for(error=exc))
        identity = Registry(self.root).describe("grep")["identity"]
        ref = capability_ref(match, identity)
        try:
            validate_input(match["spec"], tool_input)
            done = run_implementation(self.root / match["implementation_location"], "main.py", "python",
                                      json.dumps({"root": str(self.repo), "input": tool_input}), ["python3", "--version"])
            result = validate_output(match["spec"], json.loads(done.stdout))
        except (CallError, ValueError) as exc:
            return self.recorder.record(call_id, ref, outcome_for(error=exc), language=match["language"],
                                        code_digest=match["code_digest"], revocation_seq=match["revocation_seq"],
                                        input_value=tool_input)
        return self.recorder.record(call_id, ref, outcome_for(result), language=match["language"],
                                    code_digest=match["code_digest"], revocation_seq=match["revocation_seq"],
                                    grant="grant:test-1", input_value=tool_input,
                                    output=result.get("output"), duration_ms=7)

    def test_a_successful_call_records_the_matchs_label_digest_and_revocation_sequence(self):
        record = self.call("r-1", {"pattern": "hello", "files": "**/*.txt"})
        match = self.registry.resolve("grep", languages=["python"])
        self.assertEqual(record["outcome"], {"status": "ok", "code": None})
        self.assertEqual(record["capability"]["label"], "Tested")
        self.assertEqual(record["capability"]["version"], "1.2.0")
        self.assertEqual(record["code_digest"], match["code_digest"])
        self.assertEqual(record["revocation"], {"status": "checked", "seq": match["revocation_seq"]})
        self.assertEqual(record["grant"], "grant:test-1")
        self.assertEqual(record["language"], "python")
        self.assertEqual(record["capability"]["digest"], Registry(self.root).describe("grep")["identity"])

    def test_the_tool_output_that_held_a_secret_is_not_in_the_log(self):
        self.call("r-1", {"pattern": "s3cr3t", "files": "**/*.txt"})
        raw = self.log.read_bytes()
        self.assertNotIn(b"s3cr3t", raw)
        self.assertNotIn(b"hello", raw)
        self.assertEqual(verify_log(self.log)[0]["output"]["bytes"] > 0, True)

    def test_a_rejected_input_is_recorded_without_running_the_tool(self):
        record = self.call("r-1", {"pattern": 5, "files": "*.txt"})
        self.assertEqual(record["outcome"], {"status": "input_rejected", "code": "ValidationFailed"})
        self.assertIsNone(record["output"])

    def test_a_revoked_capability_is_recorded_as_refused_and_found_by_the_query(self):
        before = self.call("r-1", {"pattern": "hello", "files": "**/*.txt"})
        self.feed.publish([{"kind": "tool", "name": "grep", "version": "1.2.0"}])
        self.client.poll(self.feed)
        refused = self.call("r-2", {"pattern": "hello", "files": "**/*.txt"})
        self.assertEqual(refused["outcome"], {"status": "refused", "code": "Revoked"})
        self.assertEqual(before["revocation"]["seq"], 0)
        affected = calls_for(self.log, "tool", "grep", version="1.2.0", outcome="refused")
        self.assertEqual([r["call_id"] for r in affected], ["r-2"])
        self.assertEqual([r["call_id"] for r in calls_for(self.log, "tool", "grep", version="1.2.0")], ["r-1", "r-2"])

    def test_a_stale_revocation_feed_is_recorded_as_refused_stale(self):
        self.clock.advance(31)
        record = self.call("r-1", {"pattern": "hello", "files": "**/*.txt"})
        self.assertEqual(record["outcome"], {"status": "refused", "code": "RevocationStale"})


if __name__ == "__main__":
    unittest.main()
