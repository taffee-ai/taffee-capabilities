"""S-019: call_id is idempotent per caller (CAP-CALL-SVC-11). A retried call never runs a tool twice.

- ReplyStoreTest: the store alone (claim, replay, conflict, in progress, release, scope, expiry, restart, races).
- RequestDigestTest: what counts as the same request.
- IdempotentRouteTest and the classes after it: POST /v2/call through the service, with the test-only unconfined
  sandbox counting how many times a tool really ran.
- ContainerIdempotencyTest: functional, a write-root tool in the real container sandbox, retried.
"""

import contextlib
import http.client
import io
import json
import os
import tempfile
import threading
import time
import unittest
from pathlib import Path

from capability.call_replies import DEFAULT_HOLD_S, CallReplyStore, INTERRUPTED, REQUEST_KEYS, request_digest
from capability.registry_api import make_server
from capability.registry_cli import main as registry_cli_main
from capability.sandbox import ContainerSandbox, ProcessSandbox, SandboxCleanupFailed

import test_tool_call as tc
from registry_support import UnconfinedTestSandbox

DAY = 24 * 3600
A, B = "agent:orchestrator", "agent:other"


class FakeClock:
    def __init__(self, now=1_800_000_000.0):
        self.now = now

    def __call__(self):
        return self.now


class ReplyStoreTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.path = Path(self._tmp.name) / "replies.sqlite3"
        self.clock = FakeClock()
        self.store = CallReplyStore(self.path, retention_s=DAY, clock=self.clock)

    def tearDown(self):
        self.store.close()
        self._tmp.cleanup()

    def test_claim_then_replay_of_the_stored_reply(self):
        self.assertEqual(self.store.begin(A, "t:1", "d1"), ("claimed",))
        self.store.finish(A, "t:1", 200, {"result": {"ok": True, "output": "3"}})
        self.assertEqual(self.store.begin(A, "t:1", "d1"), ("replay", 200, {"result": {"ok": True, "output": "3"}}))
        self.assertEqual(self.store.begin(A, "t:1", "d1")[0], "replay")  # as often as asked

    def test_a_different_request_under_a_taken_call_id_is_a_conflict(self):
        self.store.begin(A, "t:1", "d1")
        self.assertEqual(self.store.begin(A, "t:1", "d2"), ("conflict",))  # while running
        self.store.finish(A, "t:1", 200, {})
        self.assertEqual(self.store.begin(A, "t:1", "d2"), ("conflict",))  # and once done

    def test_a_running_call_is_in_progress_for_the_same_request_when_not_held(self):
        self.store.begin(A, "t:1", "d1")
        self.assertEqual(self.store.begin(A, "t:1", "d1"), ("in_progress",))

    def held(self, digest="d1", hold_s=10.0):
        """Start a held begin in a thread; returns (thread, answers)."""
        answers = []
        thread = threading.Thread(target=lambda: answers.append(self.store.begin(A, "t:1", digest, hold_s=hold_s)))
        thread.start()
        time.sleep(0.2)  # let it reach the wait
        return thread, answers

    def test_a_held_retry_gets_the_reply_when_the_first_call_finishes(self):
        self.store.begin(A, "t:1", "d1")
        thread, answers = self.held()
        self.assertEqual(answers, [])  # still waiting
        self.store.finish(A, "t:1", 502, {"code": "PostconditionFailed"})
        thread.join(5)
        self.assertEqual(answers, [("replay", 502, {"code": "PostconditionFailed"})])

    def test_a_held_retry_claims_the_call_id_when_the_first_call_gives_it_back(self):
        self.store.begin(A, "t:1", "d1")
        thread, answers = self.held()
        self.store.release(A, "t:1")
        thread.join(5)
        self.assertEqual(answers, [("claimed",)])

    def test_a_hold_that_runs_out_is_in_progress(self):
        self.store.begin(A, "t:1", "d1")
        started = time.monotonic()
        self.assertEqual(self.store.begin(A, "t:1", "d1", hold_s=0.3), ("in_progress",))
        self.assertGreaterEqual(time.monotonic() - started, 0.3)

    def test_a_conflict_is_answered_at_once_never_held(self):
        self.store.begin(A, "t:1", "d1")
        started = time.monotonic()
        self.assertEqual(self.store.begin(A, "t:1", "d2", hold_s=10), ("conflict",))
        self.assertLess(time.monotonic() - started, 1)

    def test_many_held_retries_all_get_the_one_reply(self):
        self.store.begin(A, "t:1", "d1")
        answers, threads = [], []
        for _ in range(10):
            thread = threading.Thread(target=lambda: answers.append(self.store.begin(A, "t:1", "d1", hold_s=10)))
            thread.start()
            threads.append(thread)
        time.sleep(0.2)
        self.store.finish(A, "t:1", 200, {"n": 1})
        for thread in threads:
            thread.join(5)
        self.assertEqual(answers, [("replay", 200, {"n": 1})] * 10)

    def test_the_hold_bound_has_a_default_and_must_not_be_negative(self):
        self.assertEqual(self.store.hold_s, DEFAULT_HOLD_S)
        self.assertGreaterEqual(DEFAULT_HOLD_S, 660)
        with self.assertRaises(ValueError):
            CallReplyStore(self.path, hold_s=-1)

    def test_release_gives_the_call_id_back(self):
        self.store.begin(A, "t:1", "d1")
        self.store.release(A, "t:1")
        self.assertEqual(self.store.begin(A, "t:1", "d2"), ("claimed",))  # even for another request

    def test_release_and_finish_touch_only_a_running_claim(self):
        self.store.begin(A, "t:1", "d1")
        self.store.finish(A, "t:1", 200, {"first": True})
        self.store.release(A, "t:1")  # a done reply is never given back
        self.store.finish(A, "t:1", 500, {"second": True})  # nor overwritten
        self.assertEqual(self.store.begin(A, "t:1", "d1"), ("replay", 200, {"first": True}))

    def test_a_call_id_is_scoped_to_its_principal(self):
        self.assertEqual(self.store.begin(A, "t:1", "d1"), ("claimed",))
        self.assertEqual(self.store.begin(B, "t:1", "d1"), ("claimed",))
        self.store.finish(A, "t:1", 200, {"who": "A"})
        self.assertEqual(self.store.begin(B, "t:1", "d1"), ("in_progress",))
        self.assertEqual(self.store.begin(B, "t:1", "d9"), ("conflict",))  # B's own conflict, not A's
        self.assertEqual(self.store.begin(A, "t:1", "d1"), ("replay", 200, {"who": "A"}))

    def test_a_call_id_is_held_for_the_retention_window_then_freed(self):
        self.store.begin(A, "t:1", "d1")
        self.store.finish(A, "t:1", 200, {"n": 1})
        self.clock.now += DAY - 1
        self.assertEqual(self.store.begin(A, "t:1", "d1")[0], "replay")
        self.clock.now += 1  # the window is measured from the first call, and ends at exactly DAY
        self.assertEqual(self.store.begin(A, "t:1", "d1"), ("claimed",))

    def test_expired_rows_are_deleted(self):
        for i in range(5):
            self.store.begin(A, f"t:{i}", "d")
            self.store.finish(A, f"t:{i}", 200, {"secret-output": "x" * 100})
        self.clock.now += DAY
        self.store.begin(A, "fresh", "d")
        rows = self.store._db.execute("SELECT call_id FROM replies").fetchall()
        self.assertEqual(rows, [("fresh",)])

    def test_done_replies_survive_a_restart_and_running_claims_become_interrupted(self):
        self.store.begin(A, "done", "d")
        self.store.finish(A, "done", 502, {"code": "PostconditionFailed"})
        self.store.begin(A, "running", "d")
        self.store.close()
        self.store = CallReplyStore(self.path, retention_s=DAY, clock=self.clock)
        self.assertEqual(self.store.begin(A, "done", "d"), ("replay", 502, {"code": "PostconditionFailed"}))
        self.assertEqual(self.store.begin(A, "running", "d"), ("replay",) + INTERRUPTED)
        self.assertEqual(self.store.begin(A, "running", "other"), ("conflict",))

    def test_the_retention_must_be_positive(self):
        for bad in (0, -1):
            with self.assertRaises(ValueError):
                CallReplyStore(self.path, retention_s=bad)

    def test_simultaneous_first_calls_claim_exactly_once(self):
        answers, start = [], threading.Barrier(20)

        def go():
            start.wait()
            answers.append(self.store.begin(A, "race", "d")[0])

        threads = [threading.Thread(target=go) for _ in range(20)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(10)
        self.assertEqual(sorted(answers), ["claimed"] + ["in_progress"] * 19)


class RequestDigestTest(unittest.TestCase):
    BASE = {"name": "grep", "range": "1.3.0", "min_label": "Tested", "languages": ["python"],
            "input": {"pattern": "x", "files": "*.py"}, "workspace": "agent-1", "grant": "run-9", "call_id": "t:1"}

    def test_every_request_field_but_the_call_id_counts(self):
        base = request_digest(self.BASE)
        self.assertEqual(request_digest(dict(self.BASE, call_id="t:2")), base)
        self.assertEqual(set(REQUEST_KEYS), set(self.BASE) - {"call_id"})
        changes = {"name": "glob", "range": "*", "min_label": "Declared", "languages": ["python", "bash"],
                   "input": {"pattern": "y", "files": "*.py"}, "workspace": "agent-2", "grant": "run-10"}
        for key, value in changes.items():
            with self.subTest(key=key):
                self.assertNotEqual(request_digest(dict(self.BASE, **{key: value})), base)
        self.assertNotEqual(request_digest(dict(self.BASE, grant=None)), base)

    def test_key_order_inside_the_input_does_not_count(self):
        swapped = dict(self.BASE, input={"files": "*.py", "pattern": "x"})
        self.assertEqual(request_digest(swapped), request_digest(self.BASE))


class Env:
    """A fresh service with a reply store on a controllable clock."""

    def __init__(self, sandbox=None, authz=None, max_concurrent=4, hold_s=30.0):
        self._tmp = tempfile.TemporaryDirectory()
        base = Path(self._tmp.name).resolve()
        self.clock = FakeClock()
        self.replies = CallReplyStore(base / "replies.sqlite3", retention_s=DAY, hold_s=hold_s, clock=self.clock)
        self.sandbox = sandbox or UnconfinedTestSandbox()
        self.env = tc.build(base, self.sandbox, authz=authz, max_concurrent=max_concurrent, replies=self.replies)
        repo = base / "workspaces" / "agent-1"
        repo.mkdir(parents=True)
        (repo / "doc.txt").write_text("alpha beta\n", encoding="utf-8")

    def post(self, body, principal=A):
        return tc.post(self.env, body, principal)

    def runs(self):
        return len(self.sandbox.calls)

    def close(self):
        self.env.service.stop()
        self.env.store.close()
        self.replies.close()
        self._tmp.cleanup()


class IdempotentRouteTest(unittest.TestCase):
    def setUp(self):
        self.e = Env()

    def tearDown(self):
        self.e.close()

    def test_a_retry_gets_the_first_reply_without_running_again_or_recording_again(self):
        body = {"name": "text-count", "input": {"text": "one two"}, "call_id": "task-1:1", "grant": "run-9"}
        first = self.e.post(body)
        self.assertEqual(first[0], 200)
        self.assertNotIn("X-Call-Replayed", first[2])
        for _ in range(3):
            again = self.e.post(body)
            self.assertEqual((again[0], again[1]), (first[0], first[1]))
            self.assertEqual(again[2].get("X-Call-Replayed"), "true")
        self.assertEqual(self.e.runs(), 1)
        self.assertEqual(len(tc.records(self.e.env, "task-1:1")), 1)

    def test_outcomes_after_the_tool_ran_are_replayed_too(self):
        cases = [({"name": "probe-none", "input": {"mode": "fail"}}, 200),
                 ({"name": "probe-none", "input": {"mode": "echo", "text": "p" * 100}}, 502),
                 ({"name": "probe-none", "input": {"mode": "sleep"}}, 200)]
        for i, (body, status) in enumerate(cases):
            with self.subTest(status=status, input=body["input"]["mode"]):
                body = dict(body, call_id=f"after-{i}")
                runs = self.e.runs()
                first = self.e.post(body)
                self.assertEqual(first[0], status)
                again = self.e.post(body)
                self.assertEqual((again[0], again[1], again[2].get("X-Call-Replayed")), (first[0], first[1], "true"))
                self.assertEqual(self.e.runs(), runs + 1)

    def test_a_different_request_under_a_taken_call_id_is_refused_and_nothing_runs(self):
        body = {"name": "word-count", "input": {"path": "doc.txt"}, "workspace": "agent-1", "call_id": "c:1",
                "range": "1.0.0", "min_label": "Declared", "languages": ["python"], "grant": "g-1"}
        self.assertEqual(self.e.post(body)[0], 200)
        changes = {"name": "text-count", "input": {"path": "other.txt"}, "workspace": "agent-1x", "range": "*",
                   "min_label": "Validated", "languages": ["python", "bash"], "grant": "g-2"}
        for key, value in changes.items():
            with self.subTest(key=key):
                status, reply, _ = self.e.post(dict(body, **{key: value}))
                self.assertEqual((status, reply["code"]), (409, "CallIdConflict"))
        status, reply, _ = self.e.post({k: v for k, v in body.items() if k != "grant"})  # dropping a field counts
        self.assertEqual(reply["code"], "CallIdConflict")
        self.assertEqual(self.e.runs(), 1)
        self.assertEqual(self.e.post(body)[2].get("X-Call-Replayed"), "true")  # the first reply is still there

    def test_a_call_refused_before_the_tool_ran_does_not_take_the_call_id(self):
        runs = self.e.runs()
        # ValidationFailed, then the corrected input under the same call_id runs
        self.assertEqual(self.e.post({"name": "text-count", "input": {"text": 7}, "call_id": "v:1"})[0], 422)
        self.assertEqual(self.e.post({"name": "text-count", "input": {"text": "a"}, "call_id": "v:1"})[0], 200)
        # WorkspaceRequired, then with the workspace
        self.assertEqual(self.e.post({"name": "word-count", "input": {"path": "doc.txt"}, "call_id": "w:1"})[0], 400)
        self.assertEqual(self.e.post({"name": "word-count", "input": {"path": "doc.txt"}, "workspace": "agent-1",
                                      "call_id": "w:1"})[0], 200)
        # WorkspaceNotFound, then the same request once the workspace exists
        body = {"name": "word-count", "input": {"path": "doc.txt"}, "workspace": "later", "call_id": "w:2"}
        self.assertEqual(self.e.post(body)[1]["code"], "WorkspaceNotFound")
        later = self.e.env.base / "workspaces" / "later"
        later.mkdir()
        (later / "doc.txt").write_text("x y z\n", encoding="utf-8")
        self.assertEqual(self.e.post(body)[1]["result"], {"ok": True, "output": "3"})
        # NotFound, BelowLabel and InvalidRange, then a valid request under each call_id
        for refused, code in (({"name": "no-such-tool"}, "NotFound"), ({"min_label": "Tested"}, "BelowLabel"),
                              ({"range": "~1.0.0"}, "InvalidRange")):
            call_id = "r:" + code
            body = dict({"name": "text-count", "input": {"text": "a"}, "call_id": call_id}, **refused)
            self.assertEqual(self.e.post(body)[1]["code"], code)
            self.assertEqual(self.e.post({"name": "text-count", "input": {"text": "a"}, "call_id": call_id})[0], 200)
        # InvalidRequest never reaches the store
        self.assertEqual(self.e.post({"name": "text-count", "input": {"text": "a"}, "call_id": "bad id"})[0], 400)
        self.assertEqual(self.e.runs(), runs + 6)

    def test_revoked_and_stale_then_recovered(self):
        self.e.env.feed.publish([{"kind": "tool", "name": "text-count", "version": "1.1.0"}])
        self.e.env.client.poll(self.e.env.feed)
        self.assertEqual(self.e.post({"name": "text-count", "input": {"text": "a"}, "call_id": "rv:1"})[1]["code"],
                         "Revoked")
        status, reply, _ = self.e.post({"name": "text-count", "range": "1.0.0", "input": {"text": "a"}, "call_id": "rv:1"})
        self.assertEqual((status, reply["capability"]["version"]), (200, "1.0.0"))
        self.e.env.clocks.advance(31)
        body = {"name": "text-count", "range": "1.0.0", "input": {"text": "a"}, "call_id": "st:1"}
        self.assertEqual(self.e.post(body)[1]["code"], "RevocationStale")
        self.e.env.client.poll(self.e.env.feed)  # the feed recovers
        self.assertEqual(self.e.post(body)[0], 200)
        self.assertEqual(self.e.post(body)[2].get("X-Call-Replayed"), "true")

    def test_a_call_id_is_scoped_per_principal(self):
        body = {"name": "text-count", "input": {"text": "a b c"}, "call_id": "shared:1"}
        first_a = self.e.post(body, principal=A)
        first_b = self.e.post(dict(body, input={"text": "d"}), principal=B)  # B's own request, no conflict with A's
        self.assertEqual((first_a[1]["result"]["output"], first_b[1]["result"]["output"]), ("3", "1"))
        self.assertEqual(self.e.runs(), 2)
        self.assertEqual(self.e.post(body, principal=A)[1]["result"]["output"], "3")  # A never sees B's reply
        self.assertEqual(self.e.post(dict(body, input={"text": "d"}), principal=B)[1]["result"]["output"], "1")
        self.assertEqual(self.e.runs(), 2)

    def test_after_the_window_the_same_call_id_runs_again(self):
        body = {"name": "text-count", "input": {"text": "a"}, "call_id": "old:1"}
        self.e.post(body)
        self.e.clock.now += DAY
        status, _, headers = self.e.post(body)
        self.assertEqual((status, headers.get("X-Call-Replayed")), (200, None))
        self.assertEqual(self.e.runs(), 2)

    def test_after_a_restart_a_call_that_was_running_is_interrupted_not_run_again(self):
        body = {"name": "text-count", "input": {"text": "a"}, "call_id": "crash:1"}
        self.e.replies.begin(A, "crash:1", request_digest(dict(body, range="*", min_label="Declared",
                                                               languages=["python"], workspace=None, grant=None)))
        path = self.e.env.base / "replies.sqlite3"
        self.e.replies.close()
        self.e.replies = CallReplyStore(path, retention_s=DAY, clock=self.e.clock)
        self.e.env.caller.replies = self.e.replies
        for _ in range(2):  # the same response every time
            status, reply, headers = self.e.post(body)
            self.assertEqual((status, reply, headers.get("X-Call-Replayed")), (INTERRUPTED[0], INTERRUPTED[1], "true"))
        self.assertEqual(self.e.runs(), 0)


class BusyThenRetryTest(unittest.TestCase):
    def test_busy_does_not_take_the_call_id(self):
        e = Env(max_concurrent=1)
        try:
            body = {"name": "text-count", "input": {"text": "a"}, "call_id": "busy:1"}
            self.assertTrue(e.env.caller._slots.acquire(blocking=False))
            try:
                status, reply, headers = e.post(body)
                self.assertEqual((status, reply["code"], headers.get("Retry-After")), (429, "Busy", "10"))
            finally:
                e.env.caller._slots.release()
            self.assertEqual(e.post(body)[0], 200)
            self.assertEqual(e.runs(), 1)
        finally:
            e.close()


class HeldRetryTest(unittest.TestCase):
    """A retry while the first call runs is held and gets the same response; never a second run."""

    def start_first(self, e, body):
        first = {}
        thread = threading.Thread(target=lambda: first.update(reply=e.post(body)))
        thread.start()
        self.assertTrue(e.sandbox.arrived.acquire(timeout=30))
        return thread, first

    def test_held_retries_get_the_first_response_including_an_error_result(self):
        for body, status in (({"name": "text-count", "input": {"text": "a b"}}, 200),
                             ({"name": "probe-none", "input": {"mode": "echo", "text": "e" * 100}}, 502)):
            with self.subTest(status=status):
                e = Env(sandbox=tc.GateSandbox())
                try:
                    body = dict(body, call_id="slow:1")
                    thread, first = self.start_first(e, body)
                    retries = []
                    waiters = [threading.Thread(target=lambda: retries.append(e.post(body))) for _ in range(3)]
                    for waiter in waiters:
                        waiter.start()
                    time.sleep(0.3)
                    self.assertEqual(retries, [])  # held, not answered InProgress
                    status_, reply, _ = e.post(dict(body, input={"text": "other"}))  # a conflict is not held
                    self.assertEqual((status_, reply["code"]), (409, "CallIdConflict"))
                    e.sandbox.release.set()
                    thread.join(30)
                    for waiter in waiters:
                        waiter.join(30)
                    self.assertEqual(first["reply"][0], status)
                    for retry in retries:
                        self.assertEqual((retry[0], retry[1], retry[2].get("X-Call-Replayed")),
                                         (first["reply"][0], first["reply"][1], "true"))
                    self.assertEqual(len(retries), 3)
                    self.assertEqual(e.runs(), 1)
                    self.assertEqual(e.sandbox.most, 1)
                finally:
                    e.sandbox.release.set()
                    e.close()

    def test_past_the_hold_bound_a_retry_is_in_progress_with_retry_after(self):
        e = Env(sandbox=tc.GateSandbox(), hold_s=0.3)
        try:
            body = {"name": "text-count", "input": {"text": "a"}, "call_id": "stuck:1"}
            thread, first = self.start_first(e, body)
            status, reply, headers = e.post(body)
            self.assertEqual((status, reply["code"], headers.get("Retry-After")), (409, "InProgress", "5"))
            e.sandbox.release.set()
            thread.join(30)
            again = e.post(body)  # once finished, the same response
            self.assertEqual((again[1], again[2].get("X-Call-Replayed")), (first["reply"][1], "true"))
            self.assertEqual(e.runs(), 1)
        finally:
            e.sandbox.release.set()
            e.close()


class CountingCleanupFailure(UnconfinedTestSandbox):
    def run(self, argv, **kwargs):
        self.calls.append(argv)
        raise SandboxCleanupFailed("taffee-sbx-0123456789abcdef")


class AfterStartFailureTest(unittest.TestCase):
    def test_a_failure_after_the_tool_started_keeps_the_call_id(self):
        e = Env(sandbox=CountingCleanupFailure())
        try:
            body = {"name": "text-count", "input": {"text": "a"}, "call_id": "k:1"}
            first = e.post(body)
            self.assertEqual((first[0], first[1]["code"]), (500, "Unexpected"))
            again = e.post(body)
            self.assertEqual((again[0], again[1], again[2].get("X-Call-Replayed")), (500, first[1], "true"))
            self.assertEqual(e.runs(), 1)
        finally:
            e.close()


class BeforeStartRefusalsTest(unittest.TestCase):
    def test_sandbox_unavailable_does_not_take_the_call_id(self):
        e = Env(sandbox=ProcessSandbox())
        e.sandbox.calls = []  # ProcessSandbox records nothing; nothing may reach a launch anyway
        try:
            body = {"name": "text-count", "input": {"text": "a"}, "call_id": "sb:1"}
            self.assertEqual(e.post(body)[1]["code"], "SandboxUnavailable")
            self.assertEqual(e.replies.begin(A, "sb:1", "anything"), ("claimed",))  # it was given back
        finally:
            e.close()

    def test_an_authorizer_that_cannot_answer_does_not_take_the_call_id(self):
        e = Env(authz=tc.BrokenCallDecision(approvers={tc.APPROVER}))
        try:
            body = {"name": "text-count", "input": {"text": "a"}, "call_id": "az:1"}
            self.assertEqual(e.post(body)[1]["code"], "AuthorizerUnavailable")
            self.assertEqual(e.replies.begin(A, "az:1", "anything"), ("claimed",))
            self.assertEqual(e.runs(), 0)
        finally:
            e.close()


class WithoutAReplyStoreTest(unittest.TestCase):
    def test_without_a_store_every_call_runs(self):
        with tempfile.TemporaryDirectory() as tmp:
            sandbox = UnconfinedTestSandbox()
            env = tc.build(Path(tmp).resolve(), sandbox)
            try:
                body = {"name": "text-count", "input": {"text": "a"}, "call_id": "same"}
                tc.post(env, body)
                status, _, headers = tc.post(env, body)
                self.assertEqual((status, headers.get("X-Call-Replayed")), (200, None))
                self.assertEqual(len(sandbox.calls), 2)
            finally:
                env.service.stop()
                env.store.close()


class OverHttpAndWiringTest(unittest.TestCase):
    def test_a_replay_over_a_socket_has_the_same_bytes_and_the_replayed_header(self):
        e = Env()
        server = make_server(e.env.service)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        try:
            def send():
                conn = http.client.HTTPConnection("127.0.0.1", server.server_address[1], timeout=30)
                conn.request("POST", "/v2/call", body=json.dumps({"name": "text-count", "input": {"text": "a"},
                                                                   "call_id": "h:1"}),
                             headers={"X-Dev-Principal": A})
                response = conn.getresponse()
                data = response.read()
                conn.close()
                return response.status, response.getheader("X-Call-Replayed"), data

            first, again = send(), send()
            self.assertEqual((first[0], first[1]), (200, None))
            self.assertEqual(again, (200, "true", first[2]))
        finally:
            server.shutdown()
            e.close()

    def test_serve_offers_the_retention_option(self):
        out = io.StringIO()
        with contextlib.redirect_stdout(out), self.assertRaises(SystemExit):
            registry_cli_main(["serve", "--help"])
        self.assertIn("--call-reply-hours", out.getvalue())
        self.assertIn("--call-hold-seconds", out.getvalue())


@unittest.skipIf(tc.SKIP_REASON is not None, f"container tests need Docker: {tc.SKIP_REASON}")
class ContainerIdempotencyTest(unittest.TestCase):
    """Functional: a write-root tool in the real container sandbox, retried, runs once."""

    def test_a_retried_write_runs_once(self):
        with tempfile.TemporaryDirectory(dir=os.environ.get("TAFFEE_SANDBOX_TMP")) as tmp:
            base = Path(tmp).resolve()
            os.chmod(base, 0o755)
            sandbox = ContainerSandbox(tc.IMAGE, allowed_mount_roots=(base,), cleanup_wait_s=30.0)
            replies = CallReplyStore(base / "replies.sqlite3")
            env = tc.build(base, sandbox, admission_sandbox=sandbox, replies=replies)
            try:
                rw = base / "workspaces" / "agent-rw"
                rw.mkdir()
                os.chmod(rw, 0o777)
                body = {"name": "probe-rw", "input": {"mode": "write", "text": "first"}, "workspace": "agent-rw",
                        "call_id": "ctr-idem:1"}
                first = tc.post(env, body)
                self.assertEqual(first[1]["result"], {"ok": True, "output": "written"})
                (rw / "note.txt").write_text("changed by someone else", encoding="utf-8")
                again = tc.post(env, body)
                self.assertEqual((again[1], again[2].get("X-Call-Replayed")), (first[1], "true"))
                self.assertEqual((rw / "note.txt").read_text(encoding="utf-8"), "changed by someone else")  # not rerun
                self.assertEqual(len(tc.records(env, "ctr-idem:1")), 1)
            finally:
                env.service.stop()
                env.store.close()
                replies.close()


if __name__ == "__main__":
    unittest.main()
