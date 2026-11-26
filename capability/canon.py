"""Canonical JSON form (D-S1-05).

One form for everything the pillar hashes or writes as an index: UTF-8, sorted
keys, two-space indentation, LF line endings, and a final newline. Two runs on
equal data give equal bytes, so a digest over these bytes is reproducible.
"""

import json


def canonical_bytes(value: object) -> bytes:
    """Return the canonical byte form of a JSON-compatible value."""
    text = json.dumps(
        value,
        sort_keys=True,
        indent=2,
        ensure_ascii=False,
        allow_nan=False,
    )
    return (text + "\n").encode("utf-8")
