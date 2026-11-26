"""S-003 M3: a failure keeps the last verified state, and recovery is authenticated."""

import json
import unittest

from capability.revocation import RevocationError, entry_hash
from revocation_support import ClientCase, chain, entry_fields, sign


class FailureKeepsStateTest(ClientCase):
    def tampered(self, entry):
        bad = dict(entry)
        bad["reference"] = "TAMPERED"
        return bad

    def test_a_tampered_entry_is_not_used_and_makes_the_client_stale(self):
        entries = chain(self.private, 2)
        self.client.ingest(entries[:1])
        self.client.accept_checkpoint(self.cp(1, entries[:1]))
        self.assertTrue(self.client.is_fresh())
        self.assertEqual(self.code(self.client.ingest, [self.tampered(entries[1])]), "BadSignature")
        self.assertEqual(self.client.entries, entries[:1])
        self.assertEqual(self.client.head_seq, 1)
        self.assertEqual(self.client.head_hash, entry_hash(entries[0]))
        self.assertTrue(self.client.faulted)
        self.assertFalse(self.client.is_fresh())

    def test_entries_verified_before_the_failure_are_kept(self):
        entries = chain(self.private, 3)
        batch = [entries[0], entries[1], self.tampered(entries[2])]
        self.assertEqual(self.code(self.client.ingest, batch), "BadSignature")
        self.assertEqual(self.client.entries, entries[:2])
        self.assertEqual(self.client.head_seq, 2)

    def test_nothing_after_the_failing_entry_in_the_batch_is_used(self):
        entries = chain(self.private, 3)
        batch = [entries[0], self.tampered(entries[1]), entries[2]]
        self.assertEqual(self.code(self.client.ingest, batch), "BadSignature")
        self.assertEqual(self.client.entries, entries[:1])

    def test_a_valid_replacement_and_a_matching_checkpoint_restore_freshness(self):
        entries = chain(self.private, 2)
        self.client.ingest(entries[:1])
        self.code(self.client.ingest, [self.tampered(entries[1])])
        self.assertFalse(self.client.is_fresh())
        self.client.ingest([entries[1]])
        self.assertEqual(self.client.head_seq, 2)
        self.assertFalse(self.client.is_fresh())
        self.client.accept_checkpoint(self.cp(1, entries))
        self.assertTrue(self.client.is_fresh())

    def test_a_matching_checkpoint_recovers_from_a_spurious_entry(self):
        entries = chain(self.private, 1)
        self.client.ingest(entries)
        junk = sign(self.private, entry_fields(2, "sha256:" + "9" * 64))
        self.code(self.client.ingest, [junk])
        self.assertFalse(self.client.is_fresh())
        self.client.accept_checkpoint(self.cp(1, entries))
        self.assertTrue(self.client.is_fresh())

    def test_a_checkpoint_over_an_unverified_head_is_refused(self):
        entries = chain(self.private, 2)
        self.client.ingest(entries[:1])
        self.code(self.client.ingest, [self.tampered(entries[1])])
        self.assertEqual(self.code(self.client.accept_checkpoint, self.cp(1, entries)), "Behind")
        self.assertFalse(self.client.is_fresh())
        self.assertEqual(self.client.head_seq, 1)

    def test_a_replayed_checkpoint_after_recovery_is_refused(self):
        entries = chain(self.private, 1)
        self.client.ingest(entries)
        recovery = self.cp(1, entries)
        self.client.accept_checkpoint(recovery)
        self.code(self.client.ingest, [self.tampered(sign(self.private, entry_fields(2, entry_hash(entries[0]))))])
        self.assertFalse(self.client.is_fresh())
        self.assertEqual(self.code(self.client.accept_checkpoint, recovery), "Replay")
        self.assertFalse(self.client.is_fresh())
        self.client.accept_checkpoint(self.cp(2, entries))
        self.assertTrue(self.client.is_fresh())

    def forged(self):
        bad = self.cp(5, [])
        bad["serial"] = 6  # the signature no longer covers the content
        return bad

    def test_a_bad_signature_on_a_checkpoint_faults_a_fresh_client(self):
        entries = chain(self.private, 1)
        self.client.ingest(entries)
        self.client.accept_checkpoint(self.cp(1, entries))
        self.assertTrue(self.client.is_fresh())
        head, deadline, serial = self.client.head_hash, self.client.deadline, self.client.last_serial
        self.assertEqual(self.code(self.client.accept_checkpoint, self.forged()), "BadSignature")
        self.assertTrue(self.client.faulted)
        self.assertFalse(self.client.is_fresh())
        self.assertEqual((self.client.head_hash, self.client.deadline, self.client.last_serial), (head, deadline, serial))

    def test_malformed_checkpoints_fault_a_fresh_client(self):
        for bad in (None, "text", [], {}, {"serial": 1}, dict(self.cp(1, []), extra=1),
                    dict(self.cp(1, []), serial="1"), dict(self.cp(1, []), issued_at="2026-1-8T0:0:0Z"),
                    dict(self.cp(1, []), head_hash=b"bytes"), dict(self.cp(1, []), head_hash={1, 2})):
            with self.subTest(bad=str(bad)[:40]):
                client = type(self.client)(self.client._key, wall=self.clocks.wall, monotonic=self.clocks.mono)
                client.accept_checkpoint(self.cp(1, []))
                self.assertTrue(client.is_fresh())
                with self.assertRaises(RevocationError):
                    client.accept_checkpoint(bad)
                self.assertTrue(client.faulted)
                self.assertFalse(client.is_fresh())

    def test_only_a_later_valid_checkpoint_recovers_after_a_bad_one(self):
        self.client.accept_checkpoint(self.cp(1, []))
        self.code(self.client.accept_checkpoint, self.forged())
        self.assertFalse(self.client.is_fresh())
        self.client.accept_checkpoint(self.cp(2, []))
        self.assertTrue(self.client.is_fresh())

    def test_through_poll_a_bad_checkpoint_faults_a_fresh_client_and_is_recorded(self):
        from revocation_support import FakeFeed
        feed = FakeFeed(self.clocks, self.private)
        self.client.poll(feed)
        self.assertTrue(self.client.is_fresh())
        feed.raw_checkpoint = json.dumps(self.forged())
        self.client.poll(feed)
        self.assertEqual(self.client.last_error.code, "BadSignature")
        self.assertTrue(self.client.faulted)
        self.assertFalse(self.client.is_fresh())
        feed.raw_checkpoint = None
        self.client.poll(feed)
        self.assertTrue(self.client.is_fresh())
        self.assertIsNone(self.client.last_error)

    def test_a_replayed_valid_checkpoint_is_still_not_a_fault(self):
        replay = self.cp(1, [])
        self.client.accept_checkpoint(replay)
        self.assertEqual(self.code(self.client.accept_checkpoint, replay), "Replay")
        self.assertFalse(self.client.faulted)
        self.assertTrue(self.client.is_fresh())

    def test_a_failure_never_extends_freshness(self):
        entries = chain(self.private, 1)
        self.client.ingest(entries)
        self.client.accept_checkpoint(self.cp(1, entries))
        deadline = self.client.deadline
        self.code(self.client.ingest, [self.tampered(entries[0])])
        self.assertEqual(self.client.deadline, deadline)


if __name__ == "__main__":
    unittest.main()
