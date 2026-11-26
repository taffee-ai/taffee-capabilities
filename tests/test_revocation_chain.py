"""S-003 M2: chain, sequence, checkpoints and the age-adjusted freshness deadline."""

import unittest

from capability.revocation import GENESIS, RevocationClient, RevocationError, entry_hash
from revocation_support import ClientCase, chain, checkpoint, entry_fields, sign


class ChainTest(ClientCase):
    def test_first_entry_links_to_genesis(self):
        entries = chain(self.private, 1)
        self.assertEqual(entries[0]["prev"], GENESIS)
        self.assertEqual(self.client.ingest(entries), 1)
        self.assertEqual(self.client.head_seq, 1)

    def test_first_entry_with_any_other_prev_is_broken(self):
        bad = sign(self.private, entry_fields(1, "sha256:" + "1" * 64))
        self.assertEqual(self.code(self.client.ingest, [bad]), "ChainBroken")
        self.assertTrue(self.client.faulted)
        self.assertEqual(self.client.head_seq, 0)

    def test_contiguous_chain_advances_the_head(self):
        entries = chain(self.private, 3)
        self.assertEqual(self.client.ingest(entries), 3)
        self.assertEqual(self.client.head_hash, entry_hash(entries[-1]))

    def test_broken_prev_is_stale(self):
        entries = chain(self.private, 1)
        wrong = sign(self.private, entry_fields(2, "sha256:" + "2" * 64))
        self.client.ingest(entries)
        self.client.accept_checkpoint(self.cp(1, entries))
        self.assertTrue(self.client.is_fresh())
        self.assertEqual(self.code(self.client.ingest, [wrong]), "ChainBroken")
        self.assertFalse(self.client.is_fresh())
        self.assertEqual(self.client.head_seq, 1)

    def test_gap_is_stale_until_filled_and_attested(self):
        entries = chain(self.private, 3)
        self.assertEqual(self.code(self.client.ingest, [entries[0], entries[2]]), "Gap")
        self.assertEqual(self.client.head_seq, 1)
        self.assertFalse(self.client.is_fresh())
        self.client.ingest(entries)
        self.assertEqual(self.client.head_seq, 3)
        self.assertFalse(self.client.is_fresh())
        self.client.accept_checkpoint(self.cp(1, entries))
        self.assertTrue(self.client.is_fresh())

    def test_identical_duplicate_is_skipped(self):
        entries = chain(self.private, 2)
        self.client.ingest(entries)
        self.assertEqual(self.client.ingest(entries), 0)
        self.assertFalse(self.client.faulted)

    def test_different_entry_at_a_held_seq_is_a_fork(self):
        entries = chain(self.private, 2)
        self.client.ingest(entries)
        fork = sign(self.private, entry_fields(2, entry_hash(entries[0]), reference="OTHER"))
        self.assertEqual(self.code(self.client.ingest, [fork]), "ChainBroken")
        self.assertTrue(self.client.faulted)


class CheckpointTest(ClientCase):
    def test_empty_chain_checkpoint_makes_the_client_fresh(self):
        self.client.accept_checkpoint(self.cp(1, []))
        self.assertTrue(self.client.is_fresh())

    def test_never_fresh_without_a_checkpoint(self):
        self.client.ingest(chain(self.private, 2))
        self.assertFalse(self.client.is_fresh())

    def test_freshness_lasts_thirty_seconds_then_fails_closed(self):
        self.client.accept_checkpoint(self.cp(1, []))
        self.clocks.advance(29.9)
        self.assertTrue(self.client.is_fresh())
        self.clocks.advance(0.2)
        self.assertFalse(self.client.is_fresh())

    def test_empty_fetch_refreshes_nothing(self):
        self.client.accept_checkpoint(self.cp(1, []))
        self.clocks.advance(20)
        self.assertEqual(self.client.ingest([]), 0)
        self.clocks.advance(11)
        self.assertFalse(self.client.is_fresh())

    def test_wall_clock_jump_does_not_change_freshness(self):
        self.client.accept_checkpoint(self.cp(1, []))
        self.clocks.jump_wall(10_000)
        self.assertTrue(self.client.is_fresh())
        self.clocks.jump_wall(-20_000)
        self.assertTrue(self.client.is_fresh())
        self.clocks.advance(31)
        self.assertFalse(self.client.is_fresh())

    def test_replayed_checkpoint_refreshes_nothing(self):
        replay = self.cp(1, [])
        self.client.accept_checkpoint(replay)
        deadline = self.client.deadline
        self.clocks.advance(20)
        self.assertEqual(self.code(self.client.accept_checkpoint, replay), "Replay")
        self.assertEqual(self.client.deadline, deadline)
        self.assertFalse(self.client.faulted)
        self.clocks.advance(11)
        self.assertFalse(self.client.is_fresh())

    def test_a_higher_serial_refreshes(self):
        self.client.accept_checkpoint(self.cp(1, []))
        self.clocks.advance(25)
        self.client.accept_checkpoint(self.cp(2, []))
        self.clocks.advance(25)
        self.assertTrue(self.client.is_fresh())

    def test_lower_serial_is_rollback_and_faults(self):
        self.client.accept_checkpoint(self.cp(5, []))
        self.assertEqual(self.code(self.client.accept_checkpoint, self.cp(4, [])), "Rollback")
        self.assertTrue(self.client.faulted)
        self.assertFalse(self.client.is_fresh())

    def test_latest_seq_below_the_head_is_rollback(self):
        entries = chain(self.private, 2)
        self.client.ingest(entries)
        stale = self.cp(1, entries[:1])
        self.assertEqual(self.code(self.client.accept_checkpoint, stale), "Rollback")
        self.assertTrue(self.client.faulted)

    def test_checkpoint_ahead_of_the_chain_faults_as_behind(self):
        entries = chain(self.private, 2)
        self.client.ingest(entries[:1])
        self.assertEqual(self.code(self.client.accept_checkpoint, self.cp(1, entries)), "Behind")
        self.assertTrue(self.client.faulted)
        self.assertFalse(self.client.is_fresh())

    def test_wrong_head_hash_is_chain_broken(self):
        entries = chain(self.private, 1)
        self.client.ingest(entries)
        bad = checkpoint(self.private, 1, 1, "sha256:" + "3" * 64, self.clocks.wall_now)
        self.assertEqual(self.code(self.client.accept_checkpoint, bad), "ChainBroken")
        self.assertTrue(self.client.faulted)

    def test_unsigned_checkpoint_refreshes_nothing(self):
        forged = self.cp(1, [])
        forged["serial"] = 2
        self.assertEqual(self.code(self.client.accept_checkpoint, forged), "BadSignature")
        self.assertFalse(self.client.is_fresh())


class CheckpointShapeTest(ClientCase):
    """Signed but malformed checkpoints are refused. The signature is valid in every case."""

    def refused(self, **override):
        fields = {
            "serial": 1,
            "latest_seq": 0,
            "head_hash": GENESIS,
            "issued_at": "2026-10-18T00:00:00Z",
        }
        fields.update(override)
        self.assertEqual(
            self.code(self.client.accept_checkpoint, sign(self.private, fields)), "CheckpointShape"
        )
        self.assertFalse(self.client.is_fresh())

    def test_head_hash_must_be_exactly_64_lowercase_hex(self):
        for bad in ("sha256:", "sha256:" + "A" * 64, "sha256:" + "0" * 63, GENESIS + "\n", 5):
            with self.subTest(head_hash=bad):
                self.refused(head_hash=bad)

    def test_serial_and_latest_seq_must_be_integers_in_range(self):
        for bad in (0, -1, True, 1.0, "1", None):
            with self.subTest(serial=bad):
                self.refused(serial=bad)
        for bad in (-1, True, 0.5, "0", None):
            with self.subTest(latest_seq=bad):
                self.refused(latest_seq=bad)

    def test_issued_at_must_be_a_utc_timestamp(self):
        for bad in ("", "2026-10-18", "2026-10-18T00:00:00+00:00", 0, None, "2026-1-8T0:0:0Z", "2026-10-8T00:00:00Z",
                    " 2026-10-18T00:00:00Z", "2026-10-18T00:00:00Z\n", "\uff12\uff10\uff12\uff16-10-18T00:00:00Z"):
            with self.subTest(issued_at=bad):
                self.refused(issued_at=bad)


class AgeBudgetTest(ClientCase):
    def test_delayed_checkpoint_gets_only_the_freshness_it_has_left(self):
        self.client.accept_checkpoint(self.cp(1, [], age=20))
        self.clocks.advance(9)
        self.assertTrue(self.client.is_fresh())
        self.clocks.advance(2)
        self.assertFalse(self.client.is_fresh())

    def test_twenty_nine_seconds_old_has_one_second_left(self):
        self.client.accept_checkpoint(self.cp(1, [], age=29))
        self.clocks.advance(0.5)
        self.assertTrue(self.client.is_fresh())
        self.clocks.advance(1)
        self.assertFalse(self.client.is_fresh())

    def test_thirty_seconds_old_is_refused(self):
        self.assertEqual(
            self.code(self.client.accept_checkpoint, self.cp(1, [], age=30)), "CheckpointTooOld"
        )
        self.assertFalse(self.client.is_fresh())

    def test_first_fetch_after_restart_refuses_an_old_checkpoint(self):
        restarted = RevocationClient(
            self.client._key, wall=self.clocks.wall, monotonic=self.clocks.mono
        )
        old = self.cp(9001, [], age=40)
        with self.assertRaises(RevocationError) as ctx:
            restarted.accept_checkpoint(old)
        self.assertEqual(ctx.exception.code, "CheckpointTooOld")
        self.assertFalse(restarted.is_fresh())
        restarted.accept_checkpoint(self.cp(9002, [], age=20))
        self.assertTrue(restarted.is_fresh())

    def fresh_then(self, bad):
        """A fresh client receives `bad`: it must fault, with its verified state unmoved."""
        self.client.accept_checkpoint(self.cp(1, []))
        self.assertTrue(self.client.is_fresh())
        before = (self.client.head_seq, self.client.head_hash, self.client.deadline, self.client.last_serial)
        with self.assertRaises(RevocationError) as ctx:
            self.client.accept_checkpoint(bad)
        self.assertTrue(self.client.faulted)
        self.assertFalse(self.client.is_fresh())
        self.assertEqual(before, (self.client.head_seq, self.client.head_hash, self.client.deadline, self.client.last_serial))
        return ctx.exception.code

    def test_a_too_old_checkpoint_faults_a_fresh_client_and_a_later_one_recovers(self):
        self.assertEqual(self.fresh_then(self.cp(2, [], age=40)), "CheckpointTooOld")
        self.client.accept_checkpoint(self.cp(3, [], age=5))
        self.assertTrue(self.client.is_fresh())

    def test_a_checkpoint_exactly_at_the_age_bound_faults_and_one_second_inside_does_not(self):
        self.assertEqual(self.fresh_then(self.cp(2, [], age=30)), "CheckpointTooOld")
        self.client.accept_checkpoint(self.cp(3, [], age=29))
        self.assertTrue(self.client.is_fresh())

    def test_a_checkpoint_from_the_future_faults_a_fresh_client_and_a_later_one_recovers(self):
        self.assertEqual(self.fresh_then(self.cp(2, [], age=-15)), "CheckpointFromFuture")
        self.client.accept_checkpoint(self.cp(3, [], age=-5))
        self.assertTrue(self.client.is_fresh())

    def test_through_poll_an_out_of_window_checkpoint_faults_a_fresh_client(self):
        import json
        from capability.revocation import GENESIS
        from revocation_support import FakeFeed, checkpoint
        for age, code in ((40, "CheckpointTooOld"), (-15, "CheckpointFromFuture")):
            with self.subTest(code=code):
                client = type(self.client)(self.client._key, wall=self.clocks.wall, monotonic=self.clocks.mono)
                feed = FakeFeed(self.clocks, self.private)
                client.poll(feed)
                self.assertTrue(client.is_fresh())
                feed.raw_checkpoint = json.dumps(
                    checkpoint(self.private, 50, 0, GENESIS, self.clocks.wall_now - age))
                client.poll(feed)
                self.assertEqual(client.last_error.code, code)
                self.assertTrue(client.faulted)
                self.assertFalse(client.is_fresh())
                feed.raw_checkpoint = None
                client.poll(feed)
                self.assertTrue(client.is_fresh())
                self.assertIsNone(client.last_error)

    def test_a_replay_of_an_accepted_serial_is_still_the_one_refusal_that_does_not_fault(self):
        replay = self.cp(1, [])
        self.client.accept_checkpoint(replay)
        self.clocks.advance(5)
        self.assertEqual(self.code(self.client.accept_checkpoint, replay), "Replay")
        self.assertFalse(self.client.faulted)
        self.assertTrue(self.client.is_fresh())

    def test_five_seconds_ahead_is_accepted_with_a_full_window(self):
        self.client.accept_checkpoint(self.cp(1, [], age=-5))
        self.clocks.advance(29)
        self.assertTrue(self.client.is_fresh())

    def test_fifteen_seconds_ahead_is_refused(self):
        self.assertEqual(
            self.code(self.client.accept_checkpoint, self.cp(1, [], age=-15)), "CheckpointFromFuture"
        )
        self.assertFalse(self.client.is_fresh())

    def test_never_ok_more_than_thirty_seconds_after_issue(self):
        for age in (0, 10, 20, 29):
            client = RevocationClient(
                self.client._key, wall=self.clocks.wall, monotonic=self.clocks.mono
            )
            issued = self.clocks.wall_now - age
            client.accept_checkpoint(checkpoint(self.private, 1, 0, GENESIS, issued))
            self.clocks.advance(30 - age - 1)
            self.assertTrue(client.is_fresh(), age)
            self.clocks.advance(2)
            self.assertFalse(client.is_fresh(), age)
            self.assertLessEqual(self.clocks.wall_now - issued, 31)


if __name__ == "__main__":
    unittest.main()
