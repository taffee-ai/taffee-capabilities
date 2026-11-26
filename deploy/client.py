"""Client steps for deploy/stability.sh. Runs inside the registry image (profile "smoke"), next to the service.

  submit PREFIX N [--until-busy]   submit N fresh tools; with --until-busy, return as soon as one is being built
                                   or tested (so the caller can kill the registry mid-admission). Prints the ids.
  wait-all FILE [--state S]        wait until every id in FILE is decided; exit 1 unless all are in state S
  burst N                          N submissions at once from N threads; exit 1 unless all are admitted
  manual                           the manual-approval flow: awaiting_approval, self-approval refused, approve, reject
  wait-status NAME CODE [SECONDS]  poll POST /v2/resolve for NAME until it answers CODE
  name-of FILE                     the tool name of the first submission in FILE
  head                             the store head sequence
  audit                            the whole deployment is consistent (see `audit`)

Shares the bundle and request helpers with smoke.py.
"""

import json
import os
import subprocess
import sys
import threading
import time
from pathlib import Path

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))  # /app, for `capability`

from smoke import AGENT, RUN, bundle, call

STATE_FILE = Path(RUN) / "stability"
DECIDED = ("admitted", "rejected", "awaiting_approval")


def fail(message):
    print("FAIL " + message, flush=True)
    sys.exit(1)


def submit_many(prefix, count, until_busy=False):
    tag = f"{prefix}-{int(time.time())}"
    ids = []
    for number in range(count):
        status, record = call("POST", "/v2/submissions", bundle(f"{tag}-{number}"))
        if status != 202:
            fail(f"submit answered {status}: {record}")
        ids.append(record["id"])
    if until_busy:
        deadline = time.monotonic() + 120
        while time.monotonic() < deadline:
            states = [call("GET", f"/v2/submissions/{sid}")[1]["state"] for sid in ids]
            if any(state in ("building", "testing") for state in states):
                print("states at the kill: " + ",".join(states), file=sys.stderr, flush=True)
                break
            time.sleep(0.1)
        else:
            fail("no submission started")
    return ids


def wait_all(ids, timeout=600, principals=None):
    """Wait until every submission is decided. Each is read as its submitter (others' answer 404)."""
    deadline = time.monotonic() + timeout
    records = {}
    who = dict(zip(ids, principals or [AGENT] * len(ids)))
    while time.monotonic() < deadline:
        for sid in ids:
            if sid not in records:
                status, record = call("GET", f"/v2/submissions/{sid}", principal=who[sid])
                if status == 200 and record["state"] in DECIDED:
                    records[sid] = record
        if len(records) == len(ids):
            return [records[sid] for sid in ids]
        time.sleep(1)
    fail(f"only {len(records)} of {len(ids)} submissions were decided in {timeout}s")


def burst(count):
    tag = f"burst-{int(time.time())}"
    results, errors = [None] * count, []
    start = threading.Barrier(count)

    def one(number):
        start.wait()
        status, record = call("POST", "/v2/submissions", bundle(f"{tag}-{number}"), principal=f"agent:burst-{number}")
        if status != 202:
            errors.append(f"{number}: {status} {record}")
        else:
            results[number] = record["id"]

    started = time.monotonic()
    threads = [threading.Thread(target=one, args=(n,)) for n in range(count)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    if errors:
        fail("submissions refused: " + "; ".join(errors))
    records = wait_all(results, principals=[f"agent:burst-{n}" for n in range(count)])
    states = [r["state"] for r in records]
    if states != ["admitted"] * count:
        fail(f"not all admitted: {states} {[r.get('problems') for r in records if r['state'] != 'admitted']}")
    print(f"{count} simultaneous submissions admitted in {time.monotonic() - started:.1f}s")


def manual():
    tag = f"manual-{int(time.time())}"
    _, first = call("POST", "/v2/submissions", bundle(f"{tag}-a"))
    _, second = call("POST", "/v2/submissions", bundle(f"{tag}-b"))
    first, second = wait_all([first["id"], second["id"]])
    if first["state"] != "awaiting_approval" or second["state"] != "awaiting_approval":
        fail(f"expected awaiting_approval, got {first['state']}, {second['state']}")
    status, _ = call("POST", f"/v2/submissions/{first['id']}/approve", b"{}", principal=AGENT)
    if status != 403:
        fail(f"a submitter approving its own submission answered {status}, not 403")
    status, _ = call("POST", f"/v2/submissions/{first['id']}/approve", b"{}", principal="agent:other")
    if status != 403:
        fail(f"a principal who is not an approver answered {status}, not 403")
    status, done = call("POST", f"/v2/submissions/{first['id']}/approve", b"{}", principal="approver:dev")
    if status != 200 or done["state"] != "admitted":
        fail(f"approve answered {status}: {done}")
    status, done = call("POST", f"/v2/submissions/{second['id']}/reject", json.dumps({"reason": "not needed"}).encode(),
                        principal="approver:dev")
    if status != 200 or done["state"] != "rejected":
        fail(f"reject answered {status}: {done}")
    status, _ = call("POST", "/v2/resolve", json.dumps({"name": f"{tag}-b"}).encode())
    if status != 404:
        fail(f"a rejected tool resolves with {status}, not 404")
    print("manual approval: waits for a person, refuses self-approval, approves and rejects")


def wait_status(name, code, seconds):
    deadline = time.monotonic() + seconds
    status = body = None
    while time.monotonic() < deadline:
        status, body = call("POST", "/v2/resolve", json.dumps({"name": name}).encode())
        if status == code:
            print(f"resolve {name}: {status}")
            return
        time.sleep(1)
    fail(f"resolve {name} still answers {status} after {seconds}s, not {code}: {body}")


def store():
    """The store, read-only: the running registry is its one writer. A second read-write connection that closes
    removes the registry's WAL where file locks do not work across containers (Docker Desktop on macOS)."""
    from capability.store import SQLiteStore

    return SQLiteStore(f"{RUN}/registry", read_only=True)


LIBRARY = os.environ.get("TAFFEE_LIBRARY") or None  # the library export, if the deployment turned it on


def git(*args):
    """git on the library, without writing to it. These checks run as root and the library belongs to the host
    user, so git is told to trust it (otherwise it refuses: "dubious ownership"), and not to refresh its index
    (`git status` would otherwise rewrite .git/index as root's)."""
    return subprocess.run(["git", "--no-optional-locks", "-c", f"safe.directory={LIBRARY}", "-C", LIBRARY, *args],
                          capture_output=True, text=True, check=True).stdout


def audit():
    """Every signed row verifies; no submission is stuck; every admitted submission is committed to the library
    exactly once; the library is clean and intact; no scratch is left; no sandbox container is orphaned."""
    from capability.revocation import load_public_key
    from capability.rows import verify_store

    data = store()
    public = load_public_key((Path(RUN) / "registry" / "admission.pub").read_text().strip())
    rows = verify_store(data, public)
    problems = []
    open_states = ("received", "validating", "building", "testing")
    stuck = [r["id"] for state in open_states for r in data.submissions(state)]
    if stuck:
        problems.append(f"open submissions: {stuck}")
    leftovers = list(Path(RUN, "registry", "code").glob(".incoming-*")) + list(Path(RUN, "work").glob("tmp*"))
    if leftovers:
        problems.append(f"leftover scratch: {[str(p) for p in leftovers]}")
    admitted = data.submissions("admitted")
    for record in admitted:  # the store alone must be able to serve every admitted version
        row = data.get_row("tool", record["manifest"]["name"], record["manifest"]["version"])
        if row is None or data.code_path(row["implementations"][0]["code_digest"]) is None:
            problems.append(f"{record['id']} is admitted but its row or code is missing from the store")
    if LIBRARY is None:
        if problems:
            fail("audit: " + "; ".join(problems))
        print(f"audit: {rows} rows verify, {len(admitted)} admitted submissions each with a row and code in the store, "
              "no submission open, no scratch left, library export off")
        return
    committed = set(git("ls-tree", "-r", "--name-only", "HEAD", "--", "catalog/tool").splitlines())
    for record in admitted:
        path = f"catalog/tool/{record['manifest']['name']}/rows/{record['manifest']['version']}.json"
        if path not in committed:
            problems.append(f"{record['id']} is admitted but not in the library")
    subjects = git("log", "--format=%s").splitlines()
    repeated = sorted({s for s in subjects if subjects.count(s) > 1})
    if repeated:
        problems.append(f"commits made twice: {repeated}")
    for ledger in Path(LIBRARY, "catalog", "tool").glob("*/versions.yaml"):
        versions = [v["version"] for v in yaml.safe_load(ledger.read_text())["versions"]]
        if len(versions) != len(set(versions)):
            problems.append(f"{ledger}: a version listed twice")
    if git("status", "--porcelain", "--untracked-files=all").strip():
        problems.append("the library has uncommitted changes")
    fsck = subprocess.run(["git", "-c", f"safe.directory={LIBRARY}", "-C", LIBRARY, "fsck", "--strict"],
                          capture_output=True, text=True)
    if fsck.returncode != 0:
        problems.append(f"git fsck: {fsck.stderr.strip()[:300]}")
    if (Path(LIBRARY) / ".git" / "index.lock").exists():
        problems.append("a stale index.lock")
    if problems:
        fail("audit: " + "; ".join(problems))
    print(f"audit: {rows} rows verify, {len(admitted)} admitted submissions all committed once, "
          f"{len(subjects)} library commits, library clean, no scratch left")


def main(argv):
    command, args = argv[0], argv[1:]
    STATE_FILE.mkdir(exist_ok=True)
    if command == "submit":
        ids = submit_many(args[0], int(args[1]), until_busy="--until-busy" in args)
        print("\n".join(ids))
    elif command == "wait-all":
        ids = [line for line in Path(args[0]).read_text().split() if line]
        records = wait_all(ids)
        want = args[args.index("--state") + 1] if "--state" in args else None
        states = [r["state"] for r in records]
        if want and states != [want] * len(ids):
            fail(f"states {states}, problems {[r.get('problems') for r in records]}")
        if any(p.startswith("duplicate") for r in records for p in r.get("problems", [])):
            fail("a resumed submission was reported as a duplicate of its own row")
        print(f"{len(ids)} submissions decided: {sorted(set(states))}")
    elif command == "burst":
        burst(int(args[0]))
    elif command == "manual":
        manual()
    elif command == "wait-status":
        wait_status(args[0], int(args[1]), float(args[2]) if len(args) > 2 else 60)
    elif command == "name-of":
        sid = Path(args[0]).read_text().split()[0]
        print(call("GET", f"/v2/submissions/{sid}")[1]["name"])
    elif command == "head":
        print(store().head()[0])
    elif command == "audit":
        audit()
    else:
        fail(f"unknown command {command}")


if __name__ == "__main__":
    main(sys.argv[1:])
