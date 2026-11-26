"""S-003 M4 (part): check(kind, name, version, digest) over the verified chain."""

import unittest

from capability.revocation import RevocationError
from revocation_support import ClientCase, chain

D1 = "sha256:" + "1" * 64
D2 = "sha256:" + "2" * 64


def tool_version(name="git-log", version="1.0.0"):
    return {"kind": "tool", "name": name, "version": version}


class CheckTest(ClientCase):
    def fresh_with(self, targets):
        entries = chain(self.private, len(targets), targets) if targets else []
        self.client.ingest(entries)
        self.client.accept_checkpoint(self.cp(1, entries))
        return entries

    def test_a_fresh_client_with_no_revocations_says_ok(self):
        self.fresh_with([])
        result = self.client.check("tool", "git-log", "1.0.0", D1)
        self.assertEqual((result.status, result.seq), ("ok", 0))

    def test_digest_revocation_matches_only_that_kind_and_digest(self):
        self.fresh_with([{"kind": "tool", "digest": D1}])
        self.assertEqual(self.client.check("tool", "git-log", "1.0.0", D1).status, "revoked")
        self.assertEqual(self.client.check("tool", "git-log", "1.0.0", D2).status, "ok")
        self.assertEqual(self.client.check("agent", "git-log", "1.0.0", D1).status, "ok")

    def test_name_version_revocation_is_caught_against_a_different_digest(self):
        self.fresh_with([tool_version()])
        self.assertEqual(self.client.check("tool", "git-log", "1.0.0", D1).status, "revoked")
        self.assertEqual(self.client.check("tool", "git-log", "1.0.0", D2).status, "revoked")
        self.assertEqual(self.client.check("tool", "git-log", "1.0.1", D2).status, "ok")
        self.assertEqual(self.client.check("tool", "grep", "1.0.0", D2).status, "ok")

    def test_a_tool_revocation_does_not_revoke_an_agent_with_the_same_name_and_version(self):
        self.fresh_with([tool_version()])
        self.assertEqual(self.client.check("agent", "git-log", "1.0.0", D1).status, "ok")
        self.assertEqual(self.client.check("model", "git-log", "1.0.0", D1).status, "ok")

    def test_both_forms_are_evaluated_in_one_check(self):
        entries = self.fresh_with([{"kind": "tool", "digest": D1}, tool_version("grep")])
        by_digest = self.client.check("tool", "git-log", "1.0.0", D1)
        by_name = self.client.check("tool", "grep", "1.0.0", D2)
        self.assertEqual((by_digest.status, by_digest.entry), ("revoked", entries[0]))
        self.assertEqual((by_name.status, by_name.entry), ("revoked", entries[1]))
        self.assertEqual(by_name.seq, 2)

    def test_a_client_that_never_had_a_checkpoint_is_stale(self):
        self.assertEqual(self.client.check("tool", "git-log", "1.0.0", D1).status, "stale")

    def test_a_client_past_its_deadline_is_stale(self):
        self.fresh_with([])
        self.clocks.advance(31)
        self.assertEqual(self.client.check("tool", "git-log", "1.0.0", D1).status, "stale")

    def test_a_faulted_client_is_stale(self):
        entries = self.fresh_with([])
        self.code(self.client.ingest, [dict(chain(self.private, 1)[0], reference="X")])
        self.assertEqual(self.client.check("tool", "git-log", "1.0.0", D1).status, "stale")

    def test_a_verified_revocation_is_reported_even_when_the_client_is_stale(self):
        self.fresh_with([tool_version()])
        self.clocks.advance(31)
        self.assertEqual(self.client.check("tool", "git-log", "1.0.0", D1).status, "revoked")

    def test_an_unverified_entry_never_revokes(self):
        self.fresh_with([])
        forged = dict(chain(self.private, 1, [tool_version()])[0], reference="X")
        self.code(self.client.ingest, [forged])
        result = self.client.check("tool", "git-log", "1.0.0", D1)
        self.assertEqual(result.status, "stale")
        self.assertIsNone(result.entry)

    def test_changing_the_entry_after_ingest_cannot_change_a_later_check(self):
        original = chain(self.private, 1, [{"kind": "tool", "digest": D1}])[0]
        self.client.ingest([original])
        self.client.accept_checkpoint(self.cp(1, [original]))
        original["target"]["digest"] = D2          # the caller still holds the dict
        original["target"]["kind"] = "agent"
        self.assertEqual(self.client.check("tool", "git-log", "1.0.0", D1).status, "revoked")
        self.assertEqual(self.client.check("tool", "git-log", "1.0.0", D2).status, "ok")

    def test_changing_the_returned_entries_cannot_change_a_later_check(self):
        self.fresh_with([{"kind": "tool", "digest": D1}])
        held = self.client.entries
        held[0]["target"]["digest"] = D2
        held.clear()
        self.assertEqual(len(self.client.entries), 1)
        self.assertEqual(self.client.entries[0]["target"]["digest"], D1)
        self.assertEqual(self.client.check("tool", "git-log", "1.0.0", D1).status, "revoked")
        self.assertEqual(self.client.check("tool", "git-log", "1.0.0", D2).status, "ok")

    def test_changing_the_entry_in_a_check_result_cannot_change_a_later_check(self):
        self.fresh_with([{"kind": "tool", "digest": D1}])
        result = self.client.check("tool", "git-log", "1.0.0", D1)
        result.entry["target"]["digest"] = D2
        result.entry["seq"] = 99
        again = self.client.check("tool", "git-log", "1.0.0", D1)
        self.assertEqual((again.status, again.entry["seq"], again.entry["target"]["digest"]), ("revoked", 1, D1))
        self.assertEqual(self.client.check("tool", "git-log", "1.0.0", D2).status, "ok")

    def test_each_check_result_gets_its_own_copy(self):
        self.fresh_with([{"kind": "tool", "digest": D1}])
        one = self.client.check("tool", "git-log", "1.0.0", D1)
        two = self.client.check("tool", "git-log", "1.0.0", D1)
        self.assertIsNot(one.entry, two.entry)
        self.assertEqual(one.entry, two.entry)

    def test_entries_with_types_that_are_not_plain_json_are_refused(self):
        good = chain(self.private, 1)[0]
        for bad in (dict(good, extra={1, 2}), dict(good, actor=b"bytes"), dict(good, target=object()),
                    {1: "non-string key", **{k: v for k, v in good.items()}}):
            with self.subTest(bad=str(bad)[:40]):
                self.assertEqual(self.code(self.client.ingest, [bad]), "EntryShape")
                self.assertEqual(self.client.head_seq, 0)

    def test_malformed_input_is_an_error_not_ok(self):
        self.fresh_with([])
        for args in (("plugin", "git-log", "1.0.0", D1), ("tool", "Git-Log", "1.0.0", D1),
                     ("tool", "git-log", "1.0", D1), ("tool", "git-log", "1.0.0", "sha256:"),
                     ("tool", "git-log", "1.0.0", None)):
            with self.subTest(args=args):
                self.assertEqual(self.code(self.client.check, *args), "CheckInput")


if __name__ == "__main__":
    unittest.main()
