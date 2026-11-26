"""S-018 command line: capability-registry.

  init-key     --key FILE                         create an admission key; print the public key (base64)
  import-index --repo DIR --data DIR --key FILE   copy a verified file index into a store (REG-COMPAT-3)
  verify       --data DIR --public-key B64        check every row, the chain and the head (REG-ROW-5); read-only
  serve        --data DIR --key FILE ...          the /v2 REST service (development reference)
  mcp          --url URL --principal ID           the read-only MCP server on stdio, answered by `serve` over REST

`serve` needs a revocation feed (--revocation-key, --feed-file, --feed-checkpoint): there is no unchecked mode
(REG-QRY-6). `mcp` asks the running service, which checks revocation on every answer.

Only `serve` writes the store. A second process must never open it for writing: where file locks do not work
across processes (Docker Desktop bind mounts on macOS), closing that connection removes the live WAL.
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import logging
import os
import signal
import sys
import threading
from pathlib import Path

from .digest import _collect, tree_digest
from .languages import load_languages
from .registry import Registry, RegistryError
from .revocation import FileFeed, RevocationClient, load_public_key
from .rows import RowError, generate_key_file, load_private_key, now_iso, public_bytes, verify_store
from .sandbox import SandboxError
from .store import SQLiteStore, StoreConflict

REPO = Path(__file__).resolve().parent.parent
# The registry's one home: rows, submissions and every admitted tool's code (03_DESIGN §6). Deployed, it is a
# Docker volume at /var/lib/taffee-capabilities, chosen with TAFFEE_CAP_DATA. In development it is var/registry/
# in this repository (gitignored). Built-in tools' source stays in library/ and is imported on `serve`.
DEFAULT_DATA = Path(os.environ.get("TAFFEE_CAP_DATA") or (REPO / "var" / "registry"))
# Taffee's tool library, a git export of what was admitted (R-D14). Off unless a deployment names a folder
# (--library, or TAFFEE_CAP_LIBRARY): not every enterprise wants the registry committing to git.
DEFAULT_LIBRARY = os.environ.get("TAFFEE_CAP_LIBRARY") or None


def import_index(repo: Path, store, private_key) -> dict:
    """Copy every entry of `repo`'s verified file index into `store`. Idempotent: an entry already imported
    with the same identity is skipped; one with another identity is a conflict."""
    from .admission import append_signed

    index = Registry(repo).verified_index()
    counts = {"imported": 0, "skipped": 0}
    for entry in index["entries"]:
        kind, manifest = entry["kind"], entry["manifest"]
        identity = entry.get("identity") or entry["manifest_digest"]
        existing = store.get_row(kind, entry["name"], entry["version"])
        if existing is not None:
            if existing["identity"] != identity:
                raise StoreConflict(f"{entry['name']}@{entry['version']} is in the store with another identity")
            counts["skipped"] += 1
            continue
        implementations = []
        locations = {item["language"]: item["location"] for item in manifest.get("implementations", [])}
        for item in entry.get("implementations", []):
            folder = repo / locations[item["language"]]
            files = {relative: path.read_bytes() for relative, path in _collect(folder)}
            digest = store.put_code(files)
            if digest != item["code_digest"] or tree_digest(store.code_path(digest)) != digest:
                raise RowError(f"{entry['name']}: code digest changed on import")
            implementations.append({"language": item["language"], "code_digest": digest, "entry": item["entry"]})
        fields = {
            "kind": kind, "name": entry["name"], "version": entry["version"], "manifest": manifest,
            "manifest_digest": entry["manifest_digest"], "identity": identity, "implementations": implementations,
            "label": entry["label"], "evidence": list(entry.get("evidence", [])),
            "source": {"type": "file-index", "index_digest": index["index_digest"]},
            "submitted_by": "file-index", "admitted_by": "file-index", "admitted_at": now_iso(),
        }
        append_signed(store, private_key, fields)
        counts["imported"] += 1
    return counts


def _revocation(args):
    if args.feed_dir:  # a dev-feed folder: its public key, entries and checkpoint
        folder = Path(args.feed_dir)
        args.revocation_key = args.revocation_key or (folder / "feed.pub").read_text(encoding="ascii").strip()
        args.feed_file = args.feed_file or str(folder / "entries.jsonl")
        args.feed_checkpoint = args.feed_checkpoint or str(folder / "checkpoint.json")
    if not (args.revocation_key and args.feed_file and args.feed_checkpoint):
        raise SystemExit("a revocation feed is required: --feed-dir, or --revocation-key, --feed-file and --feed-checkpoint")
    client = RevocationClient(load_public_key(args.revocation_key))
    feed = FileFeed(args.feed_file, args.feed_checkpoint)
    client.poll(feed)

    def keep_fresh(stop=threading.Event()):
        while not stop.wait(args.poll_seconds):
            client.poll(feed)

    threading.Thread(target=keep_fresh, daemon=True).start()
    return client


def _feed_args(parser):
    parser.add_argument("--feed-dir", help="a dev-feed folder (feed.pub, entries.jsonl, checkpoint.json)")
    parser.add_argument("--revocation-key", help="base64 Ed25519 public key of the revocation feed")
    parser.add_argument("--feed-file")
    parser.add_argument("--feed-checkpoint")
    parser.add_argument("--poll-seconds", type=float, default=10.0)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="capability-registry")
    sub = parser.add_subparsers(dest="command", required=True)
    p = sub.add_parser("init-key")
    p.add_argument("--key", required=True)
    p = sub.add_parser("import-index")
    p.add_argument("--repo", default=str(REPO))
    p.add_argument("--data", default=str(DEFAULT_DATA))
    p.add_argument("--key", required=True)
    p = sub.add_parser("verify")
    p.add_argument("--data", default=str(DEFAULT_DATA))
    p.add_argument("--public-key", required=True)
    p = sub.add_parser("serve")
    p.add_argument("--data", default=str(DEFAULT_DATA))
    p.add_argument("--key", help="the admission private key (default: <data>/admission.key)")
    p.add_argument("--create-key", action="store_true", help="create the admission key if it does not exist")
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, default=8090)
    p.add_argument("--approver", action="append", default=[])
    p.add_argument("--sandbox-image", required=True)
    p.add_argument("--work-dir", default=str(DEFAULT_DATA / "work"))
    p.add_argument("--no-import", action="store_true", help="do not import the built-in tools from library/")
    p.add_argument("--library", default=DEFAULT_LIBRARY,
                   help="turn on the library export: commit every admitted version to a git repository in this folder "
                        "(default: off; an empty value is off)")
    p.add_argument("--library-remote", help="push every library commit to this git URL (default: commits stay local)")
    p.add_argument("--library-ssh-key", help="the private key file for an SSH remote")
    p.add_argument("--library-known-hosts", help="the known_hosts file for an SSH remote (the host key is always checked)")
    p.add_argument("--approval", choices=("auto", "manual"), default="auto",
                   help="auto: admit when every check passes (development default); manual: a named approver decides")
    p.add_argument("--workers", type=int, default=4, help="parallel admission workers (containers at once)")
    p.add_argument("--max-queued", type=int, default=200)
    p.add_argument("--max-open-per-submitter", type=int, default=20)
    _feed_args(p)
    p = sub.add_parser("mcp", help="read-only MCP over stdio, answered by a running registry's REST API")
    p.add_argument("--url", required=True, help="the registry's base URL, e.g. http://127.0.0.1:8090")
    p.add_argument("--principal", required=True)
    p = sub.add_parser("dev-feed", help="DEVELOPMENT ONLY: a local revocation feed")
    p.add_argument("action", choices=("run", "revoke", "public-key"))
    p.add_argument("--dir", required=True)
    p.add_argument("--interval", type=float, default=5.0, help="seconds between checkpoints (run)")
    p.add_argument("--kind", default="tool")
    p.add_argument("--name")
    p.add_argument("--version")
    from .revocation import REASONS

    p.add_argument("--reason", default="incident", choices=REASONS)
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.WARNING, stream=sys.stderr)

    if args.command == "dev-feed":
        from .dev_feed import DevFeed

        feed = DevFeed(args.dir)
        if args.action == "public-key":
            print(feed.public_key_b64())
        elif args.action == "revoke":
            if not (args.name and args.version):
                parser.error("revoke needs --name and --version")
            entry = feed.revoke({"kind": args.kind, "name": args.name, "version": args.version}, reason=args.reason)
            print(f"revoked {args.kind} {args.name}@{args.version} (feed seq {entry['seq']})")
        else:
            print(f"dev feed: signing a checkpoint every {args.interval:g}s in {args.dir}", flush=True)
            feed.run(args.interval)
        return 0

    if args.command == "init-key":
        public = generate_key_file(args.key)
        print(base64.b64encode(public_bytes(public)).decode("ascii"))
        return 0
    if args.command == "import-index":
        counts = import_index(Path(args.repo), SQLiteStore(args.data), load_private_key(args.key))
        print(f"imported {counts['imported']}, already present {counts['skipped']}")
        return 0
    if args.command == "mcp":  # never opens the store: a second process on its SQLite file is not safe everywhere
        from .authz import DevAdmissionAuthorizer, Principal
        from .mcp_server import MCPServer, RestRegistry

        principal = Principal(args.principal)
        MCPServer(RestRegistry(args.url, principal), DevAdmissionAuthorizer(), principal).serve()
        return 0
    if args.command == "verify":
        try:
            count = verify_store(SQLiteStore(args.data, read_only=True), load_public_key(args.public_key))
        except RowError as exc:
            print(f"store does not verify: {exc}", file=sys.stderr)
            return 1
        print(f"store verifies: {count} rows")
        return 0

    from .authz import DevAdmissionAuthorizer, Principal
    from .store_registry import StoreRegistry

    store = SQLiteStore(args.data)
    revocation = _revocation(args)
    languages = load_languages(REPO)
    from .admission import Admission
    from .registry_api import RegistryService, make_server
    from .sandbox import ContainerSandbox

    key_path = Path(args.key) if args.key else Path(args.data) / "admission.key"
    if not key_path.exists():
        if not args.create_key:
            raise SystemExit(f"no admission key at {key_path}; pass --create-key to create one")
        public = generate_key_file(key_path)
        (key_path.parent / "admission.pub").write_text(base64.b64encode(public_bytes(public)).decode("ascii") + "\n",
                                                       encoding="ascii")
        print(f"created the admission key {key_path}", flush=True)
    key = load_private_key(key_path)
    pub_path = key_path.parent / "admission.pub"
    if not pub_path.exists():  # readers (mcp, verify) need only the public key
        pub_path.write_text(base64.b64encode(public_bytes(key.public_key())).decode("ascii") + "\n", encoding="ascii")
    registry = StoreRegistry(store, key.public_key(), revocation, languages)
    work = Path(args.work_dir)
    # One owner per data folder: at start-up, every container with this label was left by a previous run.
    owner = "registry-" + hashlib.sha256(str(Path(args.data).resolve()).encode()).hexdigest()[:16]
    sandbox = ContainerSandbox(args.sandbox_image, allowed_mount_roots=(work.resolve(), Path(args.data).resolve()),
                               owner=owner)
    from .library_repo import LibraryError, LibraryRepo

    for name in ("library", "library_remote", "library_ssh_key", "library_known_hosts"):
        if getattr(args, name) == "":  # compose passes unset settings as empty values
            setattr(args, name, None)
    library = None
    if args.library is None:
        if args.library_remote or args.library_ssh_key or args.library_known_hosts:
            raise SystemExit("--library-remote, --library-ssh-key and --library-known-hosts need --library")
        print("library export: off (admitted tools are recorded in the store only)", flush=True)
    else:
        try:
            library = LibraryRepo(args.library, remote=args.library_remote, ssh_key=args.library_ssh_key,
                                  known_hosts=args.library_known_hosts)
        except LibraryError as exc:
            raise SystemExit(f"library: {exc}") from exc
        print(f"library export: on, {args.library}" + (", pushing to the configured remote" if args.library_remote
                                                       else ", local commits only"), flush=True)
    admission = Admission(store, key, sandbox, languages, work, approval=args.approval, library=library)
    if args.approval == "auto":
        logging.getLogger("capability.registry_cli").warning("approval policy is auto: no person reviews admissions")
    if not args.no_import:
        from .index import build_index

        build_index(REPO)  # the published catalog of library/, through the real publication gate
        counts = import_index(REPO, store, key)  # built-in tools from library/, so one registry holds every tool
        print(f"built-in tools: imported {counts['imported']}, already present {counts['skipped']}", flush=True)
    from .admission_queue import AdmissionQueue

    queue = AdmissionQueue(admission, workers=args.workers, max_queued=args.max_queued,
                           max_open_per_submitter=args.max_open_per_submitter)
    # Leftovers of a killed run. Safe here: this process is the store's one writer and nothing runs yet.
    removed = store.remove_incomplete_code() + admission.clean_work()
    if removed:
        print(f"removed {removed} partly written folder(s) left by an interrupted run", flush=True)
    stranded = sandbox.remove_owned()  # containers a killed run was creating, which nobody would ever remove
    if stranded:
        print(f"removed {stranded} sandbox container(s) left by an interrupted run", flush=True)
    synced = admission.sync_library()  # rows a failed commit left out of the library
    if synced:
        print(f"library: committed {synced} admitted version(s) a previous run left out", flush=True)
    queue.resume()  # submissions a restart left open are queued again, ahead of new work
    queue.start()
    service = RegistryService(registry, admission, DevAdmissionAuthorizer(approvers=args.approver), key, queue=queue)
    server = make_server(service, args.host, args.port)

    def on_term(signum, frame):  # `docker stop`: stop answering, let workers finish briefly, exit 0
        raise KeyboardInterrupt

    signal.signal(signal.SIGTERM, on_term)
    try:
        # The ready line is inside the try: a SIGTERM that lands the moment it is written still shuts down cleanly.
        print(f"serving /v2 on {server.server_address[0]}:{server.server_address[1]}", flush=True)
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
        # Work in progress that does not finish in time is picked up again by `resume` at the next start.
        queue.stop(timeout=5)
        print("registry stopped", flush=True)
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except (RegistryError, SandboxError) as exc:
        print(exc, file=sys.stderr)
        sys.exit(1)
