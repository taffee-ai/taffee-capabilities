"""S-018: the library export is optional, off by default, and may push to an enterprise's own remote (R-D14).

The remote in these tests is a bare repository in a temporary folder: nothing leaves the machine.
"""

import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

from capability.admission import Admission
from capability.dev_feed import DevFeed
from capability.library_repo import LibraryError, LibraryRepo
from capability.store import SQLiteStore

from registry_support import LANGUAGES, UnconfinedTestSandbox, admission_key, bundle, word_count_manifest

REPO = Path(__file__).resolve().parent.parent


def git(path, *args):
    return subprocess.run(["git", "-C", str(path), *args], capture_output=True, text=True, check=True).stdout


class ExportTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.base = Path(self._tmp.name)
        self.store = SQLiteStore(self.base / "data")
        self.private, _ = admission_key()

    def tearDown(self):
        self.store.close()
        self._tmp.cleanup()

    def admission(self, library=None):
        return Admission(self.store, self.private, UnconfinedTestSandbox(), LANGUAGES, self.base / "work",
                         approval="auto", library=library)

    def submit(self, admission, version="1.0.0", source=None):
        kwargs = {"source": source} if source else {}
        raw = json.dumps(bundle(word_count_manifest(version=version), **kwargs)).encode()
        return admission.process(admission.submit(raw, "author:acme")["id"])

    def test_with_the_export_off_admission_works_and_nothing_is_written_to_git(self):
        admission = self.admission()
        record = self.submit(admission)
        self.assertEqual(record["state"], "admitted")
        self.assertNotIn("library_commit", record)
        self.assertEqual(admission.sync_library(), 0)
        self.assertFalse(any(p.name == ".git" for p in self.base.rglob(".git")))

    def test_with_the_export_off_a_new_version_still_must_pass_the_regression_suite(self):
        admission = self.admission()
        self.assertEqual(self.submit(admission)["state"], "admitted")
        # 1.1.0 counts characters instead of words: its own cases are rewritten to match, the old ones are not.
        broken = "import json, sys\nreq = json.loads(sys.stdin.read())\nprint(json.dumps({'ok': True, 'output': '0'}))\n"
        record = self.submit(admission, "1.1.0", source=broken)
        self.assertEqual(record["state"], "rejected")
        self.assertTrue(any("regression 1.0.0/" in p for p in record["problems"]), record["problems"])

    def test_every_commit_is_pushed_to_the_configured_remote(self):
        remote = self.base / "remote.git"
        subprocess.run(["git", "init", "-q", "--bare", str(remote)], check=True)
        library = LibraryRepo(self.base / "library", remote=str(remote))
        self.submit(self.admission(library))
        self.assertEqual(git(remote, "log", "-1", "--format=%s", "main").strip(), "Admit word-count 1.0.0")
        self.assertEqual(git(remote, "rev-parse", "main").strip(), library.head())

    def test_a_failed_push_never_fails_an_admission_and_the_next_push_sends_what_was_missed(self):
        remote = self.base / "later.git"  # does not exist yet: the push fails
        library = LibraryRepo(self.base / "library", remote=str(remote))
        admission = self.admission(library)
        with self.assertLogs("capability.library_repo", "ERROR"):
            record = self.submit(admission)
        self.assertEqual(record["state"], "admitted")
        self.assertIsNotNone(record["library_commit"], "the local commit is made")
        subprocess.run(["git", "init", "-q", "--bare", str(remote)], check=True)
        self.submit(admission, "1.1.0")
        self.assertEqual(git(remote, "log", "--format=%s", "main").split("\n")[:2],
                         ["Admit word-count 1.1.0", "Admit word-count 1.0.0"])

    def test_an_ssh_remote_uses_only_the_given_key_and_always_checks_the_host_key(self):
        key, hosts = self.base / "id key", self.base / "known_hosts"
        key.write_text("x")
        hosts.write_text("")
        library = LibraryRepo(self.base / "library", remote="git@git.example.invalid:taffee/library.git",
                              ssh_key=key, known_hosts=hosts)
        command = library.ssh_command()
        self.assertIn(f"-i '{key.resolve()}'", command)
        for option in ("IdentitiesOnly=yes", "BatchMode=yes", "StrictHostKeyChecking=yes",
                       f"UserKnownHostsFile={hosts.resolve()}"):
            self.assertIn(option, command)
        self.assertIsNone(LibraryRepo(self.base / "plain").ssh_command())

    def test_an_ssh_key_needs_a_remote_and_known_hosts(self):
        with self.assertRaises(LibraryError):
            LibraryRepo(self.base / "l1", ssh_key=self.base / "k")
        with self.assertRaises(LibraryError):
            LibraryRepo(self.base / "l2", remote="git@h:r.git", ssh_key=self.base / "k")


AS_ROOT = hasattr(os, "geteuid") and os.geteuid() == 0
HOST_USER = 4321  # stands in for the host user who owns the deployment's folder


@unittest.skipUnless(AS_ROOT, "needs root, as the registry has in its container: run in the test harness")
class OwnershipTest(unittest.TestCase):
    """The registry's container runs as root. On a Linux host its files land as root's, and the host's git refuses
    to read a repository someone else owns. So the library belongs to whoever owns the folder it is in."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.base = Path(self._tmp.name)
        os.chmod(self.base, 0o755)  # so the host user's git can reach the library
        self.store = SQLiteStore(self.base / "data")
        self.private, _ = admission_key()
        self.folder = self.base / "run"  # the deployment's folder, the host user's
        self.folder.mkdir()
        os.chown(self.folder, HOST_USER, HOST_USER)

    def tearDown(self):
        self.store.close()
        self._tmp.cleanup()

    def admit(self, library, version="1.0.0"):
        admission = Admission(self.store, self.private, UnconfinedTestSandbox(), LANGUAGES, self.base / "work",
                              approval="auto", library=library)
        raw = json.dumps(bundle(word_count_manifest(version=version))).encode()
        return admission.process(admission.submit(raw, "author:acme")["id"])

    def owners(self, path):
        found = set()
        for top, dirs, files in os.walk(path):
            for entry in [top, *(os.path.join(top, name) for name in dirs + files)]:
                info = os.lstat(entry)
                found.add((info.st_uid, info.st_gid))
        return found

    def git_as_host_user(self, path, *args):
        return subprocess.run(["git", "-C", str(path), *args], capture_output=True, text=True, user=HOST_USER,
                              group=HOST_USER, env={"PATH": os.environ["PATH"], "HOME": str(self.base)})

    def test_every_file_of_the_library_belongs_to_the_owner_of_its_folder(self):
        library = LibraryRepo(self.folder / "library")
        self.assertEqual(self.admit(library)["state"], "admitted")
        self.assertEqual(self.admit(library, "1.1.0")["state"], "admitted")
        self.assertEqual(self.owners(self.folder / "library"), {(HOST_USER, HOST_USER)})
        done = self.git_as_host_user(self.folder / "library", "log", "--format=%s")
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertEqual(done.stdout.split("\n")[:2], ["Admit word-count 1.1.0", "Admit word-count 1.0.0"])

    def test_a_library_left_owned_by_root_is_handed_back_when_it_is_opened(self):
        os.chown(self.folder, 0, 0)
        LibraryRepo(self.folder / "library")  # what an earlier version left: all root's
        self.assertEqual(self.owners(self.folder / "library"), {(0, 0)})
        os.chown(self.folder, HOST_USER, HOST_USER)
        library = LibraryRepo(self.folder / "library")
        self.assertEqual(self.owners(self.folder / "library"), {(HOST_USER, HOST_USER)})
        self.assertEqual(self.admit(library)["state"], "admitted", "the registry's git still works on it")

    def test_a_symbolic_link_is_handed_back_itself_and_never_followed(self):
        outside = self.base / "outside"
        outside.write_text("not the library's")
        library = LibraryRepo(self.folder / "library")
        (self.folder / "library" / "link").symlink_to(outside)
        library._match_owner()
        self.assertEqual(os.lstat(self.folder / "library" / "link").st_uid, HOST_USER)
        self.assertEqual(outside.stat().st_uid, 0)

    def test_a_failed_change_of_owner_never_fails_an_admission(self):
        from unittest import mock

        library = LibraryRepo(self.folder / "library")
        with mock.patch("capability.library_repo.os.lchown", side_effect=PermissionError("not allowed here")):
            with self.assertLogs("capability.library_repo", "WARNING") as logs:
                record = self.admit(library)
        self.assertEqual(record["state"], "admitted")
        self.assertIsNotNone(record["library_commit"])
        self.assertEqual(sum("cannot give its files" in line for line in logs.output), 1, "warned once")

    def test_a_folder_owned_by_root_changes_nothing(self):
        os.chown(self.folder, 0, 0)
        library = LibraryRepo(self.folder / "library")
        self.assertIsNone(library._owner)
        self.assertEqual(self.admit(library)["state"], "admitted")
        self.assertEqual(self.owners(self.folder / "library"), {(0, 0)})


class ServeSettingsTest(unittest.TestCase):
    def serve(self, base, *extra, wait=True):
        DevFeed(base / "feed").issue()
        env = dict(os.environ, PYTHONPATH=str(REPO))
        env.pop("TAFFEE_CAP_LIBRARY", None)
        return subprocess.Popen(
            [sys.executable, "-m", "capability.registry_cli", "serve", "--data", str(base / "data"), "--create-key",
             "--no-import", "--port", "0", "--sandbox-image", "unused:dev", "--work-dir", str(base / "work"),
             "--feed-dir", str(base / "feed"), *extra],
            cwd=REPO, env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)

    def first_lines(self, proc, until="serving /v2"):
        lines, deadline = [], time.monotonic() + 30
        while time.monotonic() < deadline:
            line = proc.stdout.readline()
            if not line:
                break
            lines.append(line)
            if until in line:
                break
        return "".join(lines)

    def test_by_default_the_export_is_off_and_no_repository_is_made(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            proc = self.serve(base, "--library", "")  # what compose passes when TAFFEE_LIBRARY is unset
            try:
                out = self.first_lines(proc)
            finally:
                proc.terminate()
                proc.communicate(timeout=15)
            self.assertIn("library export: off", out)
            self.assertEqual(list(base.rglob(".git")), [])

    def test_remote_settings_without_a_library_are_refused(self):
        with tempfile.TemporaryDirectory() as tmp:
            proc = self.serve(Path(tmp), "--library-remote", "git@h:r.git")
            _, err = proc.communicate(timeout=30)
            self.assertNotEqual(proc.returncode, 0)
            self.assertIn("need --library", err)


if __name__ == "__main__":
    unittest.main()
