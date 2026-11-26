"""S-018: what a killed registry leaves behind, and how the next start recovers (REG-ADM-9, REG-LIB-3).

A container can be killed at any instruction. The store is the source of truth; these tests leave the debris
each crash window leaves, then check that the next start (a new LibraryRepo, Admission, serve) repairs it:

  - files of a version written to the library but not committed
  - a stale .git/index.lock
  - a library whose first commit never happened
  - a commit that fails: the retry must not list the version twice
  - a row appended but the submission not yet saved as admitted
  - partly written code folders and case folders
  - `docker stop` (SIGTERM): the service exits cleanly
  - processing fails for a reason that is not the submission's (Docker down): retried with back-off
"""

import json
import os
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

import yaml

from capability.admission import Admission
from capability.admission_queue import AdmissionQueue
from capability.dev_feed import DevFeed
from capability.library_repo import LibraryError, LibraryRepo
from capability.rows import verify_store
from capability.sandbox import docker_available
from capability.store import SQLiteStore

from registry_support import LANGUAGES, UnconfinedTestSandbox, admission_key, bundle, word_count_manifest

REPO = Path(__file__).resolve().parent.parent
# `serve` lists and removes its stranded sandbox containers at start-up, so it cannot start without Docker.
SERVE_SKIP = docker_available()


def git(path, *args):
    return subprocess.run(["git", "-C", str(path), *args], capture_output=True, text=True, check=True).stdout


class LibraryCrashTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.base = Path(self._tmp.name)
        self.store = SQLiteStore(self.base / "data")
        self.private, self.public = admission_key()
        self.library_path = self.base / "library"
        self.admission = Admission(self.store, self.private, UnconfinedTestSandbox(), LANGUAGES, self.base / "work",
                                   approval="auto", library=LibraryRepo(self.library_path))

    def tearDown(self):
        self.store.close()
        self._tmp.cleanup()

    def admit(self, version="1.0.0"):
        raw = json.dumps(bundle(word_count_manifest(version=version))).encode()
        record = self.admission.process(self.admission.submit(raw, "author:acme")["id"])
        self.assertEqual(record["state"], "admitted", record.get("problems"))
        return record

    def restart(self):
        library = LibraryRepo(self.library_path)
        self.admission = Admission(self.store, self.private, UnconfinedTestSandbox(), LANGUAGES, self.base / "work",
                                   approval="auto", library=library)
        return library

    def ledger(self):
        path = self.library_path / "catalog" / "tool" / "word-count" / "versions.yaml"
        return [v["version"] for v in yaml.safe_load(path.read_text())["versions"]]

    def test_files_written_but_not_committed_are_discarded_and_committed_again(self):
        # The process dies after writing the version's files and before `git commit`.
        library = self.admission.library
        with mock.patch.object(LibraryRepo, "_commit", side_effect=KeyboardInterrupt), \
                mock.patch.object(LibraryRepo, "_discard"):  # a kill runs no clean-up
            with self.assertRaises(KeyboardInterrupt):
                self.admit()
        self.assertTrue((self.library_path / "catalog/tool/word-count/rows/1.0.0.json").exists())
        self.assertFalse(library.has("word-count", "1.0.0"), "files in the working tree are not a commit")
        restarted = self.restart()
        self.assertEqual(git(self.library_path, "status", "--porcelain"), "", "recovery leaves a clean tree")
        self.assertEqual(self.admission.sync_library(), 1)
        self.assertTrue(restarted.has("word-count", "1.0.0"))
        self.assertEqual(self.ledger(), ["1.0.0"])
        self.assertEqual(restarted.log(1), ["Admit word-count 1.0.0"])
        self.assertEqual(self.admission.sync_library(), 0, "sync is idempotent")

    def test_a_stale_index_lock_is_removed_at_start(self):
        self.admit()
        (self.library_path / ".git" / "index.lock").write_text("")
        self.restart()
        self.assertFalse((self.library_path / ".git" / "index.lock").exists())
        self.admit("1.1.0")
        self.assertEqual(self.ledger(), ["1.0.0", "1.1.0"])

    def test_a_library_killed_before_its_first_commit_is_started_again(self):
        path = self.base / "fresh"
        path.mkdir()
        git(path, "init", "-q", "-b", "main")
        library = LibraryRepo(path)
        self.assertEqual(library.log(), ["Start the Taffee tool library"])

    def test_a_failed_commit_discards_its_writes_so_the_retry_lists_the_version_once(self):
        calls = {"n": 0}
        real = LibraryRepo._commit

        def flaky(repo, message):
            calls["n"] += 1
            if calls["n"] == 1:
                raise LibraryError("git commit failed: disk full")
            return real(repo, message)

        with mock.patch.object(LibraryRepo, "_commit", flaky):
            record = self.admit()
        self.assertIsNone(record["library_commit"], "the row stands; the commit is retried by sync")
        self.assertEqual(git(self.library_path, "status", "--porcelain"), "")
        self.assertEqual(self.admission.sync_library(), 1)
        self.assertEqual(self.ledger(), ["1.0.0"])

    def test_a_row_appended_before_the_record_was_saved_finishes_as_admitted(self):
        # Kill between append_signed and save_submission: the store has the row, the record still says testing.
        record = self.admit()
        head = self.store.head()
        record.update(state="testing", decided_by=None, decided_at=None, row_seq=None)
        self.store.save_submission(record)
        self.restart()
        self.assertEqual(self.admission.resume(), [record["id"]])
        done = self.admission.get(record["id"])
        self.assertEqual(done["state"], "admitted")
        self.assertEqual(done["decided_by"], "policy:auto")
        self.assertEqual(done.get("problems"), [], "its own row is not a duplicate")
        self.assertEqual(self.store.head(), head, "nothing new is written")
        self.assertEqual(verify_store(self.store, self.public), head[0])

    def test_partly_written_code_and_case_folders_are_removed_at_start(self):
        self.admit()
        code_root = self.base / "data" / "code"
        (code_root / ".incoming-killed").mkdir()
        (code_root / ".incoming-killed" / "main.py").write_text("half")
        (self.base / "work" / "tmpkilled").mkdir()
        kept = sorted(p.name for p in code_root.iterdir())
        self.assertEqual(self.store.remove_incomplete_code(), 1)
        self.assertEqual(self.admission.clean_work(), 1)
        self.assertEqual(sorted(p.name for p in code_root.iterdir()), [k for k in kept if not k.startswith(".")])
        self.assertEqual(list((self.base / "work").iterdir()), [])
        self.assertEqual(verify_store(self.store, self.public), self.store.head()[0])


class FlakyAdmission:
    """Fails `failures` times per submission (as Docker would when the daemon is unavailable), then succeeds."""

    def __init__(self, failures):
        self.failures, self.calls, self.done = failures, {}, []

    def process(self, sid):
        self.calls[sid] = self.calls.get(sid, 0) + 1
        if self.calls[sid] <= self.failures:
            raise RuntimeError("docker: Cannot connect to the Docker daemon")
        self.done.append(sid)


class QueueRetryTest(unittest.TestCase):
    def run_queue(self, admission, delays=(0.05, 0.05, 0.05)):
        queue = AdmissionQueue(admission, workers=2, retry_delays=delays).start()
        for number in range(3):
            queue.reserve("agent:a")
            queue.enqueue("agent:a", f"sub-{number}")
        try:
            self.assertTrue(queue.drain(timeout=10), queue.stats())
            return queue
        finally:
            queue.stop(timeout=5)

    def test_an_infrastructure_failure_is_retried_until_it_passes(self):
        admission = FlakyAdmission(failures=2)
        with self.assertLogs("capability.admission_queue", "ERROR"):
            queue = self.run_queue(admission)
        self.assertEqual(sorted(admission.done), ["sub-0", "sub-1", "sub-2"])
        self.assertEqual(admission.calls, {"sub-0": 3, "sub-1": 3, "sub-2": 3})
        self.assertEqual(queue.stats()["retrying"], 0)
        self.assertEqual(dict(queue._open), {}, "the rooms were given back")

    def test_after_the_last_retry_the_submission_is_left_for_resume_and_its_room_is_freed(self):
        admission = FlakyAdmission(failures=99)
        with self.assertLogs("capability.admission_queue", "ERROR") as logs:
            queue = self.run_queue(admission, delays=(0.05,))
        self.assertEqual(admission.calls, {"sub-0": 2, "sub-1": 2, "sub-2": 2})
        self.assertTrue(any("stays open until the next start" in line for line in logs.output))
        self.assertEqual(dict(queue._open), {}, "the rooms were given back")
        self.assertEqual(queue.stats()["retrying"], 0)


@unittest.skipIf(SERVE_SKIP is not None, f"serve needs Docker: {SERVE_SKIP}")
class ServeStopTest(unittest.TestCase):
    def test_sigterm_stops_the_service_cleanly(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            DevFeed(base / "feed").issue()
            env = dict(os.environ, PYTHONPATH=str(REPO))
            proc = subprocess.Popen(
                [sys.executable, "-m", "capability.registry_cli", "serve", "--data", str(base / "data"),
                 "--create-key", "--no-import", "--port", "0", "--sandbox-image", "unused:dev",
                 "--library", str(base / "library"), "--work-dir", str(base / "work"),
                 "--feed-dir", str(base / "feed")],
                cwd=REPO, env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
            try:
                deadline = time.monotonic() + 30
                line = ""
                while time.monotonic() < deadline and "serving /v2" not in line:
                    line = proc.stdout.readline()
                    if not line and proc.poll() is not None:
                        break
                self.assertIn("serving /v2", line, proc.stderr.read() if proc.poll() is not None else "")
                proc.send_signal(signal.SIGTERM)
                out, _ = proc.communicate(timeout=15)
            finally:
                if proc.poll() is None:
                    proc.kill()
            self.assertEqual(proc.returncode, 0)
            self.assertIn("registry stopped", out)


if __name__ == "__main__":
    unittest.main()
