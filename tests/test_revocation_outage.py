"""S-003 M5: outage and recovery through the transport interface."""

import unittest
from revocation_support import ClientCase, FakeFeed

D = "sha256:" + "5" * 64


class OutageTest(ClientCase):
    def setUp(self):
        super().setUp()
        self.feed = FakeFeed(self.clocks, self.private)

    def check(self):
        return self.client.check("tool", "git-log", "1.0.0", D).status

    def test_a_polling_client_is_fresh(self):
        self.client.poll(self.feed)
        self.assertEqual(self.check(), "ok")
        self.assertIsNone(self.client.last_error)

    def test_a_feed_stopped_past_the_bound_gives_stale(self):
        self.client.poll(self.feed)
        self.feed.up = False
        self.clocks.advance(20)
        self.client.poll(self.feed)
        self.assertEqual(self.client.last_error.code, "SourceUnavailable")
        self.assertEqual(self.check(), "ok")
        self.clocks.advance(11)
        self.client.poll(self.feed)
        self.assertEqual(self.check(), "stale")

    def test_a_restored_feed_gives_ok_again_without_a_restart(self):
        self.client.poll(self.feed)
        self.feed.up = False
        self.clocks.advance(40)
        self.client.poll(self.feed)
        self.assertEqual(self.check(), "stale")
        self.feed.up = True
        self.client.poll(self.feed)
        self.assertEqual(self.check(), "ok")
        self.assertIsNone(self.client.last_error)

    def test_no_state_is_lost_across_the_outage(self):
        self.feed.publish([{"kind": "tool", "digest": D}])
        self.client.poll(self.feed)
        held = list(self.client.entries)
        self.feed.up = False
        self.clocks.advance(120)
        self.client.poll(self.feed)
        self.assertEqual(self.client.entries, held)
        self.assertEqual(self.client.head_seq, 1)
        self.assertEqual(self.check(), "revoked")
        self.feed.up = True
        self.client.poll(self.feed)
        self.assertEqual(self.client.entries, held)
        self.assertEqual(self.check(), "revoked")

    def test_a_revocation_published_during_the_outage_arrives_on_recovery(self):
        self.client.poll(self.feed)
        self.feed.up = False
        self.feed.publish([{"kind": "tool", "name": "git-log", "version": "1.0.0"}])
        self.clocks.advance(40)
        self.client.poll(self.feed)
        self.assertEqual(self.check(), "stale")
        self.feed.up = True
        self.client.poll(self.feed)
        self.assertEqual(self.check(), "revoked")
        self.assertEqual(self.client.head_seq, 1)

    def test_polling_faster_than_checkpoints_is_not_a_fault(self):
        self.client.poll(self.feed)
        first = self.feed.checkpoint()
        self.feed.raw_checkpoint = first
        self.client.poll(self.feed)
        self.client.poll(self.feed)
        self.assertFalse(self.client.faulted)
        self.assertIsNone(self.client.last_error)

    def test_a_garbage_line_faults_the_client_and_is_recorded(self):
        self.client.poll(self.feed)
        self.feed.entries_since = lambda seq: ["not json\n"]
        self.client.poll(self.feed)
        self.assertEqual(self.client.last_error.code, "LineInvalid")
        self.assertEqual(self.check(), "stale")

    def test_an_unparsable_checkpoint_faults_the_client(self):
        self.client.poll(self.feed)
        self.feed.raw_checkpoint = "{"
        self.client.poll(self.feed)
        self.assertEqual(self.client.last_error.code, "LineInvalid")
        self.assertEqual(self.check(), "stale")


if __name__ == "__main__":
    unittest.main()
