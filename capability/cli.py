"""capability-validate: build the capability index, or report why it cannot be built (S-001 §9).

Usage: python -m capability.cli [ROOT]

Exit status 0 when the index was written; 1 when any capability is invalid.
"""

import argparse
import sys
from pathlib import Path

from .index import BuildError, build_index


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="capability-validate")
    parser.add_argument("root", nargs="?", default=".", help="repository root (default: current folder)")
    parser.add_argument("--pin-repo", action="append", default=[], metavar="URL=PATH",
                        help="local clone for a pinned repository URL (repeatable)")
    parser.add_argument("--fixture-mode", action="store_true",
                        help="skip the publication gate (mechanism tests only; never for the catalog)")
    args = parser.parse_args(argv)
    pin_repos = {}
    for item in args.pin_repo:
        url, sep, clone = item.partition("=")
        if not sep or not url or not clone:
            parser.error(f"--pin-repo expects URL=PATH, got {item!r}")
        pin_repos[url] = Path(clone)
    try:
        index = build_index(Path(args.root), pin_repos=pin_repos, fixture_mode=args.fixture_mode)
    except BuildError as exc:
        for error in exc.errors:
            print(error, file=sys.stderr)
        print(f"capability-validate: no index written ({len(exc.errors)} problem(s))", file=sys.stderr)
        return 1
    print(f"index: {len(index['entries'])} entries, index_digest {index['index_digest']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
