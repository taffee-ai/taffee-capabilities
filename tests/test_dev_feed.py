"""The development revocation feed (capability/dev_feed.py): what it writes, a real client accepts."""

import tempfile
import unittest
from pathlib import Path

from capability.dev_feed import DevFeed
from capability.revocation import FileFeed, RevocationClient, RevocationError, load_public_key


class DevFeedTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self._tmp.name) / "feed"
        self.feed = DevFeed(self.dir)

    def tearDown(self):
        self._tmp.cleanup()

    def client(self):
        client = RevocationClient(load_public_key(self.feed.public_key_b64()))
        client.poll(FileFeed(self.dir / "entries.jsonl", self.dir / "checkpoint.json"))
        return client

    def test_the_key_is_private_and_kept_across_restarts(self):
        self.assertEqual((self.dir / "feed.key").stat().st_mode & 0o777, 0o600)
        self.assertEqual(DevFeed(self.dir).public_key_b64(), self.feed.public_key_b64())

    def test_a_signed_checkpoint_makes_a_real_client_fresh(self):
        self.feed.issue()
        client = self.client()
        self.assertTrue(client.is_fresh(), client.last_error)

    def test_serials_only_increase(self):
        serials = [self.feed.issue()["serial"] for _ in range(3)]
        self.assertEqual(serials, sorted(set(serials)))

    def test_a_revocation_reaches_a_real_client(self):
        self.feed.revoke({"kind": "tool", "name": "word-count", "version": "1.0.0"}, reason="incident")
        client = self.client()
        self.assertTrue(client.is_fresh(), client.last_error)
        self.assertEqual(client.check("tool", "word-count", "1.0.0", "sha256:" + "0" * 64).status, "revoked")

    def test_an_entry_the_client_would_refuse_is_never_written(self):
        """Found by deploy/smoke.py: one bad entry makes every client fail closed until the feed is replaced."""
        self.feed.issue()
        before = (self.dir / "entries.jsonl").read_bytes()
        for target, reason in (({"kind": "tool", "name": "x-tool", "version": "1.0.0"}, "smoke"),
                               ({"kind": "tool", "name": "Bad Name", "version": "1.0.0"}, "incident")):
            with self.subTest(target=target, reason=reason):
                with self.assertRaises(RevocationError):
                    self.feed.revoke(target, reason=reason)
        self.assertEqual((self.dir / "entries.jsonl").read_bytes(), before)
        self.assertTrue(self.client().is_fresh())


if __name__ == "__main__":
    unittest.main()
