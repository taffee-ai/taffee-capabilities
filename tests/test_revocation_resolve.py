"""S-003 M4: resolve uses the revocation check (CAP-REV-10)."""

import json
import tempfile
import unittest
from pathlib import Path

from capability.index import build_index
from capability.manifest import load_manifest
from capability.registry import Registry, RegistryError
from capability.revocation import GENESIS, RevocationClient, entry_hash, load_public_key

from fixture_support import FIXTURES, add_fixture_tool, new_tree, write_manifest
from revocation_support import Clocks, chain, checkpoint, make_key

OTHER_DIGEST = "sha256:" + "7" * 64


class ResolveRevocationTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = new_tree(Path(self._tmp.name))
        add_fixture_tool(self.root, "python")
        manifest = load_manifest(FIXTURES / "valid" / "tool.yaml")
        manifest["implementations"] = [{"language": "python", "location": "library/fixture-tool/python"}]
        write_manifest(self.root, "tool", "fixture-tool", manifest)
        build_index(self.root, fixture_mode=True)
        row = json.loads((self.root / "index" / "index.json").read_bytes())["entries"][0]
        self.row = row
        self.private, encoded = make_key()
        self.clocks = Clocks()
        self.client = RevocationClient(
            load_public_key(encoded), wall=self.clocks.wall, monotonic=self.clocks.mono
        )
        self.registry = Registry(self.root, revocation=self.client)

    def tearDown(self):
        self._tmp.cleanup()

    def fresh(self, targets=()):
        entries = chain(self.private, len(targets), list(targets)) if targets else []
        self.client.ingest(entries)
        head = entry_hash(entries[-1]) if entries else GENESIS
        self.client.accept_checkpoint(
            checkpoint(self.private, 1, len(entries), head, self.clocks.wall_now)
        )
        return entries

    def resolve(self):
        return self.registry.resolve("fixture-tool", languages=["python"])

    def code(self):
        with self.assertRaises(RegistryError) as ctx:
            self.resolve()
        return ctx.exception

    def test_a_fresh_clean_client_returns_a_checked_match_with_the_seq(self):
        self.fresh()
        match = self.resolve()
        self.assertEqual(match["revocation"], "checked")
        self.assertEqual(match["revocation_seq"], 0)

    def test_the_match_carries_the_seq_it_was_checked_against(self):
        self.fresh([{"kind": "tool", "digest": OTHER_DIGEST}, {"kind": "tool", "name": "grep", "version": "1.0.0"}])
        self.assertEqual(self.resolve()["revocation_seq"], 2)

    def test_without_a_client_the_match_stays_unchecked(self):
        match = Registry(self.root).resolve("fixture-tool", languages=["python"])
        self.assertEqual(match["revocation"], "unchecked")
        self.assertNotIn("revocation_seq", match)

    def test_a_revoked_identity_is_revoked(self):
        entries = self.fresh([{"kind": "tool", "digest": self.row["identity"]}])
        error = self.code()
        self.assertEqual(error.code, "Revoked")
        self.assertEqual(error.entry, entries[0])
        self.assertEqual(error.seq, 1)

    def test_a_name_version_revocation_is_revoked_whatever_the_digest(self):
        self.fresh([{"kind": "tool", "name": self.row["name"], "version": self.row["version"]}])
        self.assertEqual(self.code().code, "Revoked")

    def test_a_digest_that_is_not_the_identity_does_not_revoke(self):
        self.fresh([{"kind": "tool", "digest": self.row["manifest_digest"]}])
        self.assertEqual(self.resolve()["revocation"], "checked")

    def test_another_kind_with_the_same_name_and_version_does_not_revoke(self):
        self.fresh([{"kind": "agent", "name": self.row["name"], "version": self.row["version"]}])
        self.assertEqual(self.resolve()["revocation"], "checked")

    def test_a_client_that_never_had_a_checkpoint_fails_resolve_closed(self):
        self.assertEqual(self.code().code, "RevocationStale")

    def test_a_client_past_its_deadline_fails_resolve_closed(self):
        self.fresh()
        self.resolve()
        self.clocks.advance(31)
        self.assertEqual(self.code().code, "RevocationStale")

    def test_a_restored_feed_makes_resolve_work_again_without_a_restart(self):
        self.fresh()
        self.clocks.advance(31)
        self.assertEqual(self.code().code, "RevocationStale")
        self.client.accept_checkpoint(checkpoint(self.private, 2, 0, GENESIS, self.clocks.wall_now))
        self.assertEqual(self.resolve()["revocation"], "checked")

    def test_an_integrity_error_comes_before_the_revocation_answer(self):
        self.fresh([{"kind": "tool", "name": self.row["name"], "version": self.row["version"]}])
        main = self.root / "library" / "fixture-tool" / "python" / "main.py"
        main.write_text("print('changed')\n", encoding="utf-8")
        self.assertEqual(self.code().code, "IntegrityError")

    def test_no_implementation_comes_before_the_revocation_answer(self):
        self.fresh()
        with self.assertRaises(RegistryError) as ctx:
            self.registry.resolve("fixture-tool", languages=["typescript"])
        self.assertEqual(ctx.exception.code, "NoImplementation")


if __name__ == "__main__":
    unittest.main()
