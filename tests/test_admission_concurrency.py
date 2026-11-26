"""S-018: many agents submitting at once (REG-ADM-12 to 16). Dev and test load: 20 concurrent submitters.
The design goal is 100; that is a separate load test, not part of this suite.

BurstTest: 20 submissions arrive at the same instant over HTTP: good tools, broken ones, retries racing their
originals, two agents racing for one version, and a take-over attempt on a built-in name. Then approvals arrive
concurrently. Everything must end in a clear state, with one chain, one row per version and one library commit each.

QueueBehaviourTest: fairness across submitters, per-submitter and total bounds (429), restart, and parallelism.
ContainerBurstTest: the same burst through the real ContainerSandbox (needs TAFFEE_SANDBOX_IMAGE/_TMP).
"""

import http.client
import json
import os
import tempfile
import threading
import time
import unittest
from pathlib import Path

from capability.admission import Admission
from capability.admission_queue import AdmissionQueue
from capability.authz import DevAdmissionAuthorizer
from capability.library_repo import LibraryRepo
from capability.registry_api import RegistryService, make_server
from capability.rows import verify_store
from capability.sandbox import ContainerSandbox, docker_available
from capability.store import SQLiteStore
from capability.store_registry import StoreRegistry

from registry_support import (
    LANGUAGES, WORD_COUNT, UnconfinedTestSandbox, add_rows, admission_key, bundle_bytes, fresh_revocation,
    synthetic_fields, word_count_manifest,
)

CONCURRENCY = 20
APPROVER = "approver:sec"
IMAGE = os.environ.get("TAFFEE_SANDBOX_IMAGE")
SKIP_REASON = "TAFFEE_SANDBOX_IMAGE is not set" if not IMAGE else docker_available()


class SlowSandbox(UnconfinedTestSandbox):
    """The test sandbox, with a delay per run that stands in for a container's start-up."""

    def __init__(self, delay=0.05):
        super().__init__()
        self.delay = delay
        self.active = 0
        self.peak = 0
        self._lock = threading.Lock()

    def run(self, *args, **kwargs):
        with self._lock:
            self.active += 1
            self.peak = max(self.peak, self.active)
        try:
            time.sleep(self.delay)
            return super().run(*args, **kwargs)
        finally:
            with self._lock:
                self.active -= 1


def tool_bundle(name, version="1.0.0", source=WORD_COUNT):
    return bundle_bytes(manifest=word_count_manifest(name=name, version=version), source=source)


BROKEN = "def broken(:\n"


class Harness:
    """A registry service over a real socket, with its store, library and queue."""

    def __init__(self, base: Path, sandbox, *, workers=4, max_queued=200, max_open=20, approval="manual", start=True):
        self.store = SQLiteStore(base / "data")
        self.private, self.public = admission_key()
        self.client, self.feed, self.clocks = fresh_revocation()
        self.registry = StoreRegistry(self.store, self.public, self.client, LANGUAGES)
        self.library = LibraryRepo(base / "library")
        self.admission = Admission(self.store, self.private, sandbox, LANGUAGES, base / "work",
                                   approval=approval, library=self.library)
        self.queue = AdmissionQueue(self.admission, workers=workers, max_queued=max_queued,
                                    max_open_per_submitter=max_open)
        if start:
            self.queue.start()
        self.service = RegistryService(self.registry, self.admission, DevAdmissionAuthorizer(approvers={APPROVER}),
                                       self.private, queue=self.queue)
        self.server = make_server(self.service)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.port = self.server.server_address[1]

    def call(self, method, path, principal, body=None):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=120)
        conn.request(method, path, body=body, headers={"X-Dev-Principal": principal})
        response = conn.getresponse()
        data = json.loads(response.read())
        conn.close()
        return response.status, dict(response.getheaders()), data

    def close(self):
        self.server.shutdown()
        self.queue.stop()
        self.store.close()


def at_once(jobs):
    """Run every (function, args) at the same instant on its own thread; return the results in order."""
    barrier = threading.Barrier(len(jobs))
    results = [None] * len(jobs)

    def run(index, fn, args):
        barrier.wait()
        results[index] = fn(*args)

    threads = [threading.Thread(target=run, args=(i, fn, args)) for i, (fn, args) in enumerate(jobs)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=300)
    return results


class BurstMixin:
    """The 20-submitter burst. A subclass supplies `make_sandbox()` and `base`."""

    def run_burst(self):
        h = self.h
        fields = synthetic_fields("grep", "1.2.0")  # a built-in tool already in the registry
        fields.update(source={"type": "file-index", "index_digest": "sha256:" + "1" * 64},
                      submitted_by="file-index", admitted_by="file-index")
        add_rows(h.store, h.private, [fields])

        good = [(f"agent:{i:02d}", tool_bundle(f"tool-{i:02d}")) for i in range(12)]
        broken = [(f"agent:broken-{i}", tool_bundle(f"broken-{i}", source=BROKEN)) for i in range(3)]
        retries = [good[0], good[1]]  # the same agent sends the same bundle again, at the same moment
        racers = [("agent:racer-a", tool_bundle("race-tool")), ("agent:racer-b", tool_bundle("race-tool", source=WORD_COUNT + "# b\n"))]
        squat = [("agent:squatter", tool_bundle("grep", "9.0.0"))]
        posts = good + broken + retries + racers + squat
        self.assertEqual(len(posts), CONCURRENCY)

        replies = at_once([(h.call, ("POST", "/v2/submissions", who, body)) for who, body in posts])
        statuses = [status for status, _, _ in replies]
        self.assertTrue(all(status in (200, 202) for status in statuses), statuses)
        ids = [body["id"] for _, _, body in replies]
        self.assertEqual(ids[0], ids[15])  # good[0] and its retry: one submission
        self.assertEqual(ids[1], ids[16])
        self.assertEqual(len(set(ids)), CONCURRENCY - 2)

        self.assertTrue(h.service.drain(timeout=600), h.queue.stats())
        states = {sid: h.admission.get(sid) for sid in set(ids)}
        by_name = {record["name"]: record for record in states.values()}
        for i in range(12):
            self.assertEqual(by_name[f"tool-{i:02d}"]["state"], "awaiting_approval", by_name[f"tool-{i:02d}"]["problems"])
        for i in range(3):
            self.assertEqual(by_name[f"broken-{i}"]["state"], "rejected")
            self.assertEqual(by_name[f"broken-{i}"]["problems"], ["python/build: syntax: main.py:1"])
        squatter = next(r for r in states.values() if r["submitted_by"] == "agent:squatter")
        self.assertIn("BuiltIn", squatter["problems"][0])
        race = [r for r in states.values() if r["name"] == "race-tool"]
        self.assertEqual([r["state"] for r in race], ["awaiting_approval"] * 2)

        waiting = [sid for sid, r in states.items() if r["state"] == "awaiting_approval"]
        decided = at_once([(h.call, ("POST", f"/v2/submissions/{sid}/approve", APPROVER)) for sid in waiting])
        self.assertTrue(all(status == 200 for status, _, _ in decided), [b for _, _, b in decided])
        final = [body for _, _, body in decided]
        race_final = sorted(r["state"] for r in final if r["name"] == "race-tool")
        self.assertEqual(race_final, ["admitted", "rejected"])
        loser = next(r for r in final if r["name"] == "race-tool" and r["state"] == "rejected")
        self.assertIn("VersionConflict", loser["problems"][0])
        self.assertEqual(sum(r["state"] == "admitted" for r in final), 13)

        # one chain, one row per version, one library commit per admitted version
        self.assertEqual(verify_store(h.store, h.public), 14)
        self.assertEqual(h.store.count(), 14)
        commits = h.library.log(100)
        self.assertEqual(len(commits), 13 + 1)
        self.assertEqual(sorted(c for c in commits if c.startswith("Admit")),
                         sorted([f"Admit tool-{i:02d} 1.0.0" for i in range(12)] + ["Admit race-tool 1.0.0"]))
        self.assertEqual(len(h.registry.search("count words", limit=50)["items"]), 13)


class BurstTest(BurstMixin, unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.sandbox = SlowSandbox()
        self.h = Harness(Path(self._tmp.name), self.sandbox, workers=4)

    def tearDown(self):
        self.h.close()
        self._tmp.cleanup()

    def test_twenty_agents_submitting_at_once(self):
        self.run_burst()
        self.assertLessEqual(self.sandbox.peak, 4)  # never more runs at once than workers


class QueueBehaviourTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.base = Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def test_one_flooding_agent_does_not_starve_another(self):
        h = Harness(self.base, SlowSandbox(delay=0.02), workers=1, start=False)
        order = []
        original = h.admission.process
        h.admission.process = lambda sid: (order.append(h.admission.get(sid)["submitted_by"]), original(sid))[1]
        try:
            for i in range(8):
                self.assertEqual(h.call("POST", "/v2/submissions", "agent:flood", tool_bundle(f"flood-{i}"))[0], 202)
            self.assertEqual(h.call("POST", "/v2/submissions", "agent:quiet", tool_bundle("quiet-tool"))[0], 202)
            h.queue.start()
            self.assertTrue(h.service.drain(timeout=120))
        finally:
            h.close()
        self.assertEqual(order.index("agent:quiet"), 1)  # served second, not ninth

    def test_per_submitter_and_total_bounds_answer_429_and_store_nothing(self):
        h = Harness(self.base, SlowSandbox(), workers=1, max_queued=5, max_open=3, start=False)
        try:
            for i in range(3):
                self.assertEqual(h.call("POST", "/v2/submissions", "agent:a", tool_bundle(f"a-{i}"))[0], 202)
            status, headers, body = h.call("POST", "/v2/submissions", "agent:a", tool_bundle("a-3"))
            self.assertEqual((status, body["code"], headers.get("Retry-After")), (429, "Busy", "10"))
            for i in range(2):
                self.assertEqual(h.call("POST", "/v2/submissions", f"agent:b{i}", tool_bundle(f"b-{i}"))[0], 202)
            self.assertEqual(h.call("POST", "/v2/submissions", "agent:c", tool_bundle("c-0"))[0], 429)  # queue full
            self.assertEqual(len(h.store.submissions()), 5)
            # a retry of queued work is answered, never refused, even at the bounds
            status, _, body = h.call("POST", "/v2/submissions", "agent:a", tool_bundle("a-0"))
            self.assertEqual((status, body["duplicate"], body["state"]), (200, True, "received"))
            self.assertEqual(len(h.store.submissions()), 5)
        finally:
            h.close()

    def test_a_restart_loses_no_submission(self):
        first = Harness(self.base, SlowSandbox(), workers=2, start=False)
        ids = [first.call("POST", "/v2/submissions", f"agent:{i}", tool_bundle(f"r-{i}"))[2]["id"] for i in range(6)]
        first.close()  # the process dies with everything still queued
        second = Harness(self.base, SlowSandbox(), workers=2, start=False)
        try:
            self.assertEqual(second.queue.resume(), 6)
            second.queue.start()
            self.assertTrue(second.service.drain(timeout=120))
            self.assertEqual({second.admission.get(sid)["state"] for sid in ids}, {"awaiting_approval"})
        finally:
            second.close()

    def test_workers_process_in_parallel(self):
        timings = {}
        for workers in (1, 4):
            base = self.base / f"w{workers}"
            h = Harness(base, SlowSandbox(delay=0.2), workers=workers, start=False)
            try:
                for i in range(8):
                    h.call("POST", "/v2/submissions", f"agent:{i}", tool_bundle(f"p-{i}"))
                start = time.monotonic()
                h.queue.start()
                self.assertTrue(h.service.drain(timeout=300))
                timings[workers] = time.monotonic() - start
            finally:
                h.close()
        self.assertLess(timings[4], timings[1] / 2, timings)

    def test_auto_approval_admits_a_burst_without_anyone_approving(self):
        h = Harness(self.base, SlowSandbox(), workers=4, approval="auto")
        try:
            at_once([(h.call, ("POST", "/v2/submissions", f"agent:{i}", tool_bundle(f"auto-{i}"))) for i in range(10)])
            self.assertTrue(h.service.drain(timeout=120))
            self.assertEqual(h.store.count(), 10)
            self.assertEqual(len([c for c in h.library.log(50) if c.startswith("Admit")]), 10)
        finally:
            h.close()


@unittest.skipIf(SKIP_REASON is not None, f"container tests need Docker: {SKIP_REASON}")
class ContainerBurstTest(BurstMixin, unittest.TestCase):
    """The same 20-submitter burst, every build and case in a real container."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(dir=os.environ.get("TAFFEE_SANDBOX_TMP"))
        base = Path(self._tmp.name).resolve()
        os.chmod(base, 0o755)
        sandbox = ContainerSandbox(IMAGE, allowed_mount_roots=(base,), cleanup_wait_s=30.0)
        self.h = Harness(base, sandbox, workers=4)

    def tearDown(self):
        self.h.close()
        self._tmp.cleanup()

    def test_twenty_agents_submitting_at_once_in_containers(self):
        start = time.monotonic()
        self.run_burst()
        print(f"\ncontainer burst: {CONCURRENCY} submissions in {time.monotonic() - start:.1f}s with 4 workers")


if __name__ == "__main__":
    unittest.main()
