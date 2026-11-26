"""S-018: Taffee's tool library, a git repository the registry owns (02_DECISIONS R-D14, R-D15).

OPTIONAL, AND OFF BY DEFAULT. The record of what was admitted is the store: the signed row, the code area and
the submission (with its cases, which the regression suite reads). The library is an export of that record as
git history, for an enterprise that wants one. A deployment turns it on (`serve --library PATH`) and may push
it to its own remote (`--library-remote`, with its own SSH key and known_hosts). Nothing is pushed unless a
remote is configured.

When a tool is admitted, its code becomes Taffee's. The registry writes it into the library and commits it at
the moment of admission: one commit per admitted version, authored by the registry, with the submission,
identity, approver and row sequence as commit trailers. Layout, the same shape as this repository:

    catalog/tool/<name>/manifest.yaml        the current version's manifest (owner: the library)
    catalog/tool/<name>/versions.yaml        every admitted version and its identity (the ledger)
    catalog/tool/<name>/rows/<version>.json  the signed row of each version: the library alone rebuilds the store
    library/<name>/<language>/               the current version's code
    library/<name>/conformance.json          the regression suite: every case of every admitted version

Agent-written code in this library is never run outside the sandbox; do not point capability-validate (which
runs conformance cases as plain subprocesses) at it.

The store row is written first and the commit second. If a commit fails, `sync` commits every row the library
is missing; it runs at start-up and is idempotent.

Crash safety. The store is the source of truth; the library is rebuilt from it. Only what is committed counts:
`has` asks git, never the working tree, because a process killed between writing a version's files and
committing them leaves those files behind. Opening the library recovers from such a crash: it removes a stale
`.git/index.lock` and discards every uncommitted change, then `sync` commits what is missing. A failed `admit`
discards its own partial writes the same way. So the library must have one writer: the registry service that
opened it (one `serve` per data folder).

Ownership. The library is for people: they read it, and push it, with their own git. So its files belong to
whoever owns the folder it is in, not to the registry. That matters when the registry runs as root, as it does
in the development deployment's container: on a Linux host every file it wrote would be root's, and the host's
git refuses to read a repository someone else owns ("dubious ownership"). As root, the registry hands every
file back after each change (symbolic links are never followed). Its own git is told to trust the folder, since
git refuses root access to a repository another user owns, too.
"""

from __future__ import annotations

import json
import logging
import os
import shlex
import shutil
import subprocess
import threading
from pathlib import Path

import yaml

from .canon import canonical_bytes

LOG = logging.getLogger("capability.library_repo")
AUTHOR_NAME, AUTHOR_EMAIL = "Taffee Registry", "registry@taffee.invalid"
LIBRARY_OWNER = "taffee-library"


class LibraryError(Exception):
    code = "LibraryError"


class LibraryRepo:
    def __init__(self, path, *, remote: str | None = None, ssh_key=None, known_hosts=None, branch: str = "main"):
        """`remote`: a git URL every commit is pushed to (none: commits stay local). `ssh_key` and `known_hosts`:
        files for an SSH remote; the host key is always checked. A failed push never fails an admission: it is
        logged, and the next push (after the next commit, or `push` at start-up) sends everything pending."""
        self.path = Path(path)
        self._lock = threading.Lock()
        if ssh_key is not None and (remote is None or known_hosts is None):
            raise LibraryError("an SSH key needs a remote and a known_hosts file")
        self.remote, self.branch = remote, branch
        self._ssh_key = None if ssh_key is None else Path(ssh_key).resolve()
        self._known_hosts = None if known_hosts is None else Path(known_hosts).resolve()
        self._owner, self._owner_warned = self._folder_owner(), False
        if not (self.path / ".git").is_dir():
            self.path.mkdir(parents=True, exist_ok=True)
            self._git("init", "-q", "-b", "main")
        self._recover()
        self._match_owner()  # also hands back a library an earlier version left owned by root

    def _folder_owner(self):
        """(uid, gid) the library's files must have: those of the folder it is in (the nearest that exists). None
        when nothing needs changing: the registry is not root, or that folder is root's too."""
        if not hasattr(os, "geteuid") or os.geteuid() != 0:
            return None
        folder = self.path.absolute().parent
        while not folder.exists():
            folder = folder.parent
        info = folder.stat()
        return None if (info.st_uid, info.st_gid) == (0, 0) else (info.st_uid, info.st_gid)

    def _match_owner(self):
        """Give every file of the library the folder's owner. A failure is logged once and never fails an
        admission: the library is an export, and the next change tries again."""
        if self._owner is None:
            return
        uid, gid = self._owner
        try:
            for top, dirs, files in os.walk(self.path, followlinks=False):
                for entry in [top, *(os.path.join(top, name) for name in dirs + files)]:
                    info = os.lstat(entry)
                    if (info.st_uid, info.st_gid) != (uid, gid):
                        os.lchown(entry, uid, gid)
        except OSError as exc:
            if not self._owner_warned:
                self._owner_warned = True
                LOG.warning("library: cannot give its files to the owner of %s (%s:%s), so the host's git may "
                            "refuse to read it: %s", self.path.parent, uid, gid, exc)

    def _recover(self):
        """Undo whatever a killed writer left: a stale index lock, uncommitted files, a missing first commit."""
        lock = self.path / ".git" / "index.lock"
        if lock.exists():
            LOG.warning("library: removing a stale %s left by an interrupted commit", lock)
            lock.unlink()
        if not self._has_head():
            (self.path / "README.md").write_text(
                "# Taffee tool library\n\nWritten by the capabilities registry: one commit per admitted tool "
                "version. Do not edit by hand.\n", encoding="utf-8")
            self._git("add", "README.md")
            self._commit("Start the Taffee tool library")
        self._discard()

    def _has_head(self) -> bool:
        try:
            self._git("rev-parse", "-q", "--verify", "HEAD")
            return True
        except LibraryError:
            return False

    def _discard(self):
        """Drop every uncommitted change, tracked or not."""
        if self._git("status", "--porcelain", "--untracked-files=all").strip():
            LOG.warning("library: discarding uncommitted changes left by an interrupted admission")
            self._git("reset", "-q", "--hard", "HEAD")
            self._git("clean", "-q", "-f", "-d")

    def _git(self, *args) -> str:
        env = {"PATH": "/usr/local/bin:/usr/bin:/bin", "HOME": str(self.path), "GIT_AUTHOR_NAME": AUTHOR_NAME,
               "GIT_AUTHOR_EMAIL": AUTHOR_EMAIL, "GIT_COMMITTER_NAME": AUTHOR_NAME, "GIT_COMMITTER_EMAIL": AUTHOR_EMAIL,
               "GIT_TERMINAL_PROMPT": "0"}  # never wait for a password
        ssh = self.ssh_command()
        if ssh is not None:
            env["GIT_SSH_COMMAND"] = ssh
        try:  # safe.directory: the library may belong to another user (see Ownership above)
            done = subprocess.run(["git", "-c", f"safe.directory={self.path.resolve()}", *args], cwd=self.path, env=env, capture_output=True, text=True, timeout=60,
                                  check=True)
        except FileNotFoundError as exc:
            raise LibraryError("git is not installed") from exc
        except subprocess.CalledProcessError as exc:
            raise LibraryError(f"git {args[0]} failed: {exc.stderr.strip()[:300]}") from exc
        return done.stdout

    def ssh_command(self) -> str | None:
        """The ssh command git uses for the remote: only the configured key, and the host key always checked."""
        if self._ssh_key is None:
            return None
        return (f"ssh -i {shlex.quote(str(self._ssh_key))} -o IdentitiesOnly=yes -o BatchMode=yes "
                f"-o StrictHostKeyChecking=yes -o UserKnownHostsFile={shlex.quote(str(self._known_hosts))}")

    def push(self) -> bool:
        """Push the branch to the remote, if there is one. False (logged) if it fails; nothing is lost, because the
        next push sends every commit not yet pushed."""
        if self.remote is None:
            return True
        try:
            self._git("push", "-q", self.remote, f"HEAD:refs/heads/{self.branch}")
            return True
        except LibraryError as exc:
            LOG.error("library: push to the configured remote failed; it is retried after the next commit: %s", exc)
            return False

    def _commit(self, message: str) -> str:
        self._git("commit", "-q", "--no-gpg-sign", "-m", message)
        return self._git("rev-parse", "HEAD").strip()

    @staticmethod
    def _row_path(name: str, version: str) -> str:
        return f"catalog/tool/{name}/rows/{version}.json"

    def has(self, name: str, version: str) -> bool:
        """Whether this version is committed (files in the working tree alone do not count)."""
        try:
            self._git("cat-file", "-e", f"HEAD:{self._row_path(name, version)}")
            return True
        except LibraryError:
            return False

    def _committed_rows(self) -> set:
        """Every committed rows/<version>.json path, in one git call."""
        listing = self._git("ls-tree", "-r", "--name-only", "HEAD", "--", "catalog/tool")
        return {line for line in listing.splitlines() if "/rows/" in line}

    def admit(self, row: dict, code_folders: dict, cases: list, submission_id: str | None, *, push: bool = True) -> str:
        """Write one admitted version, commit it and (unless `push` is False) push it. Returns the commit id."""
        name, version = row["name"], row["version"]
        with self._lock:
            if self.has(name, version):
                return self._git("log", "-1", "--format=%H", "--", self._row_path(name, version)).strip()
            try:
                commit = self._write_and_commit(row, code_folders, cases, submission_id)
            except BaseException:
                self._discard()  # a retry must start from the last commit, or the ledger would list the version twice
                raise
            finally:
                self._match_owner()
            if push:
                self.push()
            return commit

    def _write_and_commit(self, row, code_folders, cases, submission_id) -> str:
        name, version = row["name"], row["version"]
        meta = self.path / "catalog" / "tool" / name
        (meta / "rows").mkdir(parents=True, exist_ok=True)
        (meta / "rows" / f"{version}.json").write_bytes(canonical_bytes(row))
        ledger_path = meta / "versions.yaml"
        ledger = yaml.safe_load(ledger_path.read_text(encoding="utf-8")) if ledger_path.exists() else {"versions": []}
        ledger["versions"].append({"version": version, "identity": row["identity"]})
        ledger_path.write_text(yaml.safe_dump(ledger, sort_keys=False), encoding="utf-8")
        if self._is_newest(name, version):
            (meta / "manifest.yaml").write_text(yaml.safe_dump(row["manifest"], sort_keys=False), encoding="utf-8")
            for language, folder in sorted(code_folders.items()):
                target = self.path / "library" / name / language
                if target.exists():
                    shutil.rmtree(target)
                shutil.copytree(folder, target)
        suite_path = self.path / "library" / name / "conformance.json"
        suite_path.parent.mkdir(parents=True, exist_ok=True)
        suite = json.loads(suite_path.read_text(encoding="utf-8")) if suite_path.exists() else []
        known = {case["id"] for case in suite}
        suite += [dict(case, id=f"{version}/{case['id']}") for case in cases if f"{version}/{case['id']}" not in known]
        suite_path.write_text(json.dumps(suite, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        self._git("add", "-A", f"catalog/tool/{name}", f"library/{name}")
        trailers = [f"Identity: {row['identity']}", f"Row-Seq: {row['seq']}", f"Approved-By: {row['admitted_by']}",
                    f"Submitted-By: {row['submitted_by']}"]
        if submission_id:
            trailers.insert(0, f"Submission: {submission_id}")
        return self._commit(f"Admit {name} {version}\n\n" + "\n".join(trailers))

    def _is_newest(self, name, version) -> bool:
        from .store.base import version_key

        ledger = yaml.safe_load((self.path / "catalog" / "tool" / name / "versions.yaml").read_text(encoding="utf-8"))
        return version_key(version) >= max(version_key(v["version"]) for v in ledger["versions"])

    def cases_for(self, name: str) -> list:
        """The regression suite: every case of every admitted version, ids prefixed with their version."""
        path = self.path / "library" / name / "conformance.json"
        return json.loads(path.read_text(encoding="utf-8")) if path.exists() else []

    def sync(self, store, cases_of) -> int:
        """Commit every library-sourced row the library does not hold yet (after a failed commit). `cases_of(row)`
        returns that version's cases. Returns how many were committed. Then pushes once: whatever an earlier
        push failed to send goes too."""
        count, seq = 0, 1
        committed = self._committed_rows()
        while True:
            rows = store.rows_from(seq, 500)
            if not rows:
                self.push()
                return count
            for row in rows:
                seq = row["seq"] + 1
                if row["source"]["type"] != "submission" or self._row_path(row["name"], row["version"]) in committed:
                    continue
                folders = {item["language"]: store.code_path(item["code_digest"]) for item in row["implementations"]}
                self.admit(row, folders, cases_of(row), row["source"].get("id"), push=False)
                count += 1

    def head(self) -> str:
        return self._git("rev-parse", "HEAD").strip()

    def log(self, limit: int = 20) -> list[str]:
        return self._git("log", f"-{limit}", "--format=%s").splitlines()
