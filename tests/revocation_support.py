"""Helpers for the revocation tests: keys, signed chains, signed checkpoints, clocks."""

import base64
import json
import unittest
from datetime import datetime, timezone

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from capability.revocation import (
    GENESIS,
    RevocationClient,
    RevocationError,
    entry_hash,
    load_public_key,
    signed_bytes,
)


def make_key():
    private = Ed25519PrivateKey.generate()
    public = private.public_key().public_bytes(
        serialization.Encoding.Raw, serialization.PublicFormat.Raw
    )
    return private, base64.b64encode(public).decode("ascii")


def sign(private, fields):
    signed = dict(fields)
    signed["sig"] = base64.b64encode(private.sign(signed_bytes(fields))).decode("ascii")
    return signed


def entry_fields(seq, prev, target=None, reference="INC-1"):
    return {
        "seq": seq,
        "prev": prev,
        "target": target or {"kind": "tool", "name": "git-log", "version": "1.0.0"},
        "reason": "incident",
        "actor": "security-role",
        "time": "2026-10-18T00:00:00Z",
        "reference": reference,
    }


def chain(private, count, targets=None):
    """A signed chain of `count` entries starting from GENESIS."""
    entries = []
    prev = GENESIS
    for seq in range(1, count + 1):
        target = targets[seq - 1] if targets else None
        entry = sign(private, entry_fields(seq, prev, target, reference=f"INC-{seq}"))
        entries.append(entry)
        prev = entry_hash(entry)
    return entries


def iso(epoch):
    return datetime.fromtimestamp(epoch, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def checkpoint(private, serial, latest_seq, head_hash, issued_epoch):
    return sign(
        private,
        {
            "serial": serial,
            "latest_seq": latest_seq,
            "head_hash": head_hash,
            "issued_at": iso(issued_epoch),
        },
    )


class Clocks:
    """Wall and monotonic clocks the test controls. Start at a whole second."""

    def __init__(self, wall=1_800_000_000.0, mono=5_000.0):
        self.wall_now = wall
        self.mono_now = mono

    def wall(self):
        return self.wall_now

    def mono(self):
        return self.mono_now

    def advance(self, seconds):
        """Real time passing: both clocks move."""
        self.wall_now += seconds
        self.mono_now += seconds

    def jump_wall(self, seconds):
        """The wall clock is set; the monotonic clock does not move."""
        self.wall_now += seconds


class ClientCase(unittest.TestCase):
    """A client with controlled clocks and a key; helpers to build checkpoints and read codes."""

    def setUp(self):
        self.private, encoded = make_key()
        self.clocks = Clocks()
        self.client = RevocationClient(
            load_public_key(encoded), wall=self.clocks.wall, monotonic=self.clocks.mono
        )

    def cp(self, serial, entries, age=0.0):
        """A checkpoint over `entries`, issued `age` seconds ago on the wall clock."""
        head = entry_hash(entries[-1]) if entries else GENESIS
        return checkpoint(
            self.private, serial, len(entries), head, self.clocks.wall_now - age
        )

    def code(self, call, *args):
        with self.assertRaises(RevocationError) as ctx:
            call(*args)
        return ctx.exception.code


class FakeFeed:
    """A test double for the transport: serves lines and a checkpoint, and can go down."""

    def __init__(self, clocks, private):
        self.clocks = clocks
        self.private = private
        self.entries = []
        self.serial = 0
        self.up = True
        self.raw_checkpoint = None

    def publish(self, targets):
        self.entries = chain(self.private, len(targets), list(targets))

    def entries_since(self, seq):
        if not self.up:
            raise ConnectionError("feed is down")
        return [json.dumps(e) + "\n" for e in self.entries[seq:]]

    def checkpoint(self):
        if not self.up:
            raise ConnectionError("feed is down")
        if self.raw_checkpoint is not None:
            return self.raw_checkpoint
        self.serial += 1
        head = entry_hash(self.entries[-1]) if self.entries else None
        return json.dumps(
            checkpoint(self.private, self.serial, len(self.entries), head or GENESIS, self.clocks.wall_now)
        )
