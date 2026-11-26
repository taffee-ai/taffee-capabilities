"""capctl verify (S-013): one command that says whether a capability is what the catalog says.

It re-checks from local files and runs no capability code. Every check is reported; a failed
index check makes the rest NOT RUN, because nothing read from a bad index can be trusted.
"""

import argparse
import os
import sys
from dataclasses import dataclass
from pathlib import Path

from .conformance import ConformanceError, cases_digest, load_cases
from .digest import DigestError, tree_digest_roots
from .evidence import EVIDENCE_DIR, check_record
from .index import INDEX_DIR, INPUT_FOLDERS, OPTIONAL_FOLDERS
from .registry import Registry, RegistryError, load_index
from .revocation import FileFeed, RevocationClient, RevocationError, load_public_key

OK, FAIL, SKIP, NOT_RUN = "OK", "FAIL", "SKIP", "NOT RUN"
ORDER = ("index", "current", "entry", "digest", "evidence", "revocation", "label")


@dataclass(frozen=True)
class Check:
    name: str
    status: str
    detail: str


@dataclass
class Report:
    target: str
    checks: list

    @property
    def verified(self) -> bool:
        return not any(check.status in (FAIL, NOT_RUN) for check in self.checks)

    def lines(self) -> list[str]:
        body = [f"{check.name:<18} {check.status:<8} {check.detail}" for check in self.checks]
        return body + ["VERIFIED" if self.verified else "NOT VERIFIED"]


def _check_index(root: Path):
    try:
        index = load_index(root)
    except RegistryError as exc:
        return None, Check("index", FAIL, str(exc))
    return index, Check("index", OK, f"index digest {index['index_digest']}, policy {index['policy_version']}")


def _check_current(root: Path, index: dict) -> Check:
    """Is the tree what the index was built from? Does not gate the other checks."""
    try:
        now = tree_digest_roots(root, INPUT_FOLDERS, exclude=(INDEX_DIR,), optional=OPTIONAL_FOLDERS)
    except DigestError as exc:
        return Check("current", FAIL, f"cannot recompute the inputs digest: {exc}")
    if now != index["inputs_digest"]:
        return Check("current", FAIL, "the tree changed after the index was built "
                                      f"(recorded inputs {index['inputs_digest']}, now {now})")
    return Check("current", OK, "the tree is what the index was built from")


def _find_entry(index: dict, name: str, version: str, kind: str | None):
    rows = [e for e in index["entries"] if e["name"] == name and e["version"] == version
            and (kind is None or e["kind"] == kind)]
    kinds = sorted({e["kind"] for e in rows})
    if not rows:
        return None, Check("entry", FAIL, f"NotFound: {name}@{version} is not in the index")
    if len(kinds) > 1:
        return None, Check("entry", FAIL, f"Ambiguous: {name}@{version} is used by kinds {kinds}; pass --kind")
    return rows[0], Check("entry", OK, f"{rows[0]['kind']} {name}@{version}")


def _check_digests(root: Path, entry: dict) -> list[Check]:
    registry = Registry(root)
    name, version, kind = entry["name"], entry["version"], entry["kind"]
    if kind == "tool":
        languages = [item["language"] for item in entry["implementations"]]
        if not languages:
            return [Check("digest", SKIP, "the tool has no implementations")]
    else:
        languages = [None]
    checks = []
    for language in languages:
        label = f"digest ({language})" if language else "digest"
        try:
            match = registry.resolve(name, version, kind, languages=[language] if language else None)
        except RegistryError as exc:
            status = SKIP if exc.code == "SourceUnavailable" else FAIL
            checks.append(Check(label, status, str(exc)))
            continue
        code = match.get("code_digest")
        checks.append(Check(label, OK, f"manifest {match['manifest_digest']}" + (f", code {code}" if code else "")))
    return checks


def _check_evidence(root: Path, index: dict, entry: dict) -> Check:
    if entry["kind"] != "tool":
        return Check("evidence", SKIP, "no evidence is defined for this kind yet")
    label = entry["label"]
    if label == "Declared":
        return Check("evidence", SKIP, "label is Declared; no evidence is expected")
    refs = dict(ref.split(":", 1) for ref in entry["evidence"] if ":" in ref)
    cases_path = root / "library" / entry["name"] / "conformance.json"
    try:
        disk = cases_digest(load_cases(cases_path))
    except ConformanceError as exc:
        return Check("evidence", FAIL, f"cases cannot be read: {exc}")
    if refs.get("conformance") != disk:
        return Check("evidence", FAIL, "the cases on disk differ from the cases the label was earned on "
                                       f"(index {refs.get('conformance')}, disk {disk})")
    if label == "Validated":
        return Check("evidence", OK, f"cases digest {disk} matches; no test evidence at this label")
    problem = check_record(root, entry["name"], entry["version"], entry["identity"], disk, index["policy_version"])
    if problem:
        return Check("evidence", FAIL, problem)
    import json
    summary = json.loads((root / EVIDENCE_DIR / entry["name"] / entry["version"] / "summary.json").read_text(encoding="utf-8"))
    if refs.get("tests") != summary["fingerprint"]:
        return Check("evidence", FAIL, "the index lists a fingerprint the evidence record does not carry "
                                       f"(index {refs.get('tests')}, record {summary['fingerprint']})")
    if refs.get("record") != summary["evidence_digest"]:
        return Check("evidence", FAIL, "the index lists an evidence digest the record does not carry "
                                       f"(index {refs.get('record')}, record {summary['evidence_digest']})")
    return Check("evidence", OK, f"fingerprint {summary['fingerprint']}, record matches the current identity, cases, suite and log")


def _check_revocation(entry: dict, client, feed, skip: bool, error: str | None) -> Check:
    if error:
        return Check("revocation", FAIL, error)
    if client is None or feed is None:
        if skip:
            return Check("revocation", SKIP, "not checked (--skip-revocation)")
        return Check("revocation", FAIL, "no revocation feed configured; give a feed or pass --skip-revocation")
    client.poll(feed)
    if client.last_error is not None:
        return Check("revocation", FAIL, str(client.last_error))
    digest = entry["identity"] if entry["kind"] == "tool" else entry["manifest_digest"]
    try:
        result = client.check(entry["kind"], entry["name"], entry["version"], digest)
    except RevocationError as exc:
        return Check("revocation", FAIL, str(exc))
    if result.status == "revoked":
        reason = result.entry["reason"] if result.entry else "unknown"
        return Check("revocation", FAIL, f"revoked at feed seq {result.entry['seq']} ({reason})")
    if result.status == "stale":
        return Check("revocation", FAIL, "the revocation feed is not fresh; failing closed")
    return Check("revocation", OK, f"checked at seq {result.seq}, fresh ({client.seconds_left():.0f}s left)")


def verify(root, name: str, version: str, kind: str | None = None, client=None, feed=None,
           skip_revocation: bool = False, revocation_error: str | None = None) -> Report:
    """Verify one capability version under ``root``. Never raises for a failed check."""
    root = Path(root)
    target = f"{name}@{version}"
    index, check = _check_index(root)
    if index is None:
        return Report(target, [check] + [Check(n, NOT_RUN, "the index could not be trusted") for n in ORDER[1:]])
    checks = [check, _check_current(root, index)]
    entry, found = _find_entry(index, name, version, kind)
    checks.append(found)
    if entry is None:
        return Report(target, checks + [Check(n, NOT_RUN, "no entry to verify") for n in ORDER[3:]])
    checks.extend(_check_digests(root, entry))
    checks.append(_check_evidence(root, index, entry))
    checks.append(_check_revocation(entry, client, feed, skip_revocation, revocation_error))
    checks.append(Check("label", OK, entry["label"]))
    return Report(target, checks)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="capctl")
    sub = parser.add_subparsers(dest="command", required=True)
    verify_parser = sub.add_parser("verify", help="verify one capability version")
    verify_parser.add_argument("target", help="name@version")
    verify_parser.add_argument("--kind", choices=("tool", "agent", "model"))
    verify_parser.add_argument("--root", default=".")
    verify_parser.add_argument("--revocation-key", default=os.environ.get("CAPCTL_REVOCATION_KEY"),
                               help="base64 Ed25519 public key (or CAPCTL_REVOCATION_KEY)")
    verify_parser.add_argument("--feed-file", help="revocation entries, one per line (development transport)")
    verify_parser.add_argument("--feed-checkpoint", help="the current revocation checkpoint file")
    verify_parser.add_argument("--skip-revocation", action="store_true",
                               help="do not check revocation; the report says it was skipped")
    args = parser.parse_args(argv)

    if "@" not in args.target or args.target.count("@") != 1 or not all(args.target.split("@")):
        parser.error("target must be name@version")
    name, version = args.target.split("@")
    feed_args = (args.feed_file, args.feed_checkpoint)
    if any(feed_args) and not all(feed_args):
        parser.error("--feed-file and --feed-checkpoint go together")
    if all(feed_args) and args.skip_revocation:
        parser.error("--skip-revocation contradicts a feed")

    client = feed = error = None
    if all(feed_args):
        try:
            client = RevocationClient(load_public_key(args.revocation_key or ""))
            feed = FileFeed(args.feed_file, args.feed_checkpoint)
        except RevocationError as exc:
            error = str(exc)
    report = verify(args.root, name, version, args.kind, client, feed, args.skip_revocation, error)
    print("\n".join(report.lines()))
    return 0 if report.verified else 1


if __name__ == "__main__":
    sys.exit(main())
