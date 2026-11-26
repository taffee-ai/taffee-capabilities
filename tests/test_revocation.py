"""S-003 M1: entries, signatures, targets and the genesis head."""

import base64
import hashlib
import json
import unittest

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from capability.revocation import (
    GENESIS,
    RevocationError,
    entry_hash,
    load_public_key,
    parse_feed_line,
    signed_bytes,
    verify_entry,
)

PREV = "sha256:" + "0" * 64
DIGEST = "sha256:" + "a" * 64


def make_key():
    private = Ed25519PrivateKey.generate()
    public = private.public_key().public_bytes(
        serialization.Encoding.Raw, serialization.PublicFormat.Raw
    )
    return private, base64.b64encode(public).decode("ascii")


def sign(private, fields):
    entry = dict(fields)
    entry["sig"] = base64.b64encode(private.sign(signed_bytes(fields))).decode("ascii")
    return entry


def base_fields(target=None):
    return {
        "seq": 1,
        "prev": GENESIS,
        "target": target or {"kind": "tool", "name": "git-log", "version": "1.0.0"},
        "reason": "incident",
        "actor": "security-role",
        "time": "2026-10-18T00:00:00Z",
        "reference": "INC-1",
    }


class GenesisTest(unittest.TestCase):
    def test_genesis_is_sha256_of_the_labelled_bytes(self):
        want = "sha256:" + hashlib.sha256(b"aicore-revocation-genesis/1").hexdigest()
        self.assertEqual(GENESIS, want)


class VerifyEntryTest(unittest.TestCase):
    def setUp(self):
        self.private, self.encoded = make_key()
        self.public = load_public_key(self.encoded)

    def test_valid_entry_verifies(self):
        entry = sign(self.private, base_fields())
        self.assertEqual(verify_entry(entry, self.public), entry)

    def test_one_changed_byte_fails(self):
        entry = sign(self.private, base_fields())
        entry["reference"] = "INC-2"
        with self.assertRaises(RevocationError) as ctx:
            verify_entry(entry, self.public)
        self.assertEqual(ctx.exception.code, "BadSignature")

    def test_signature_from_another_key_fails(self):
        other, _ = make_key()
        entry = sign(other, base_fields())
        with self.assertRaises(RevocationError) as ctx:
            verify_entry(entry, self.public)
        self.assertEqual(ctx.exception.code, "BadSignature")

    def test_digest_target_verifies(self):
        entry = sign(self.private, base_fields({"kind": "tool", "digest": DIGEST}))
        self.assertEqual(verify_entry(entry, self.public)["target"]["digest"], DIGEST)

    def test_name_only_target_is_refused(self):
        entry = sign(self.private, base_fields({"kind": "tool", "name": "git-log"}))
        with self.assertRaises(RevocationError) as ctx:
            verify_entry(entry, self.public)
        self.assertEqual(ctx.exception.code, "TargetInvalid")

    def test_target_without_kind_is_refused(self):
        entry = sign(self.private, base_fields({"name": "git-log", "version": "1.0.0"}))
        with self.assertRaises(RevocationError) as ctx:
            verify_entry(entry, self.public)
        self.assertEqual(ctx.exception.code, "TargetInvalid")

    def test_unknown_reason_is_refused(self):
        fields = base_fields()
        fields["reason"] = "because"
        with self.assertRaises(RevocationError) as ctx:
            verify_entry(sign(self.private, fields), self.public)
        self.assertEqual(ctx.exception.code, "EntryShape")

    def test_seq_zero_is_refused(self):
        fields = base_fields()
        fields["seq"] = 0
        with self.assertRaises(RevocationError) as ctx:
            verify_entry(sign(self.private, fields), self.public)
        self.assertEqual(ctx.exception.code, "EntryShape")

    def test_extra_field_is_refused(self):
        entry = sign(self.private, base_fields())
        entry["extra"] = 1
        with self.assertRaises(RevocationError) as ctx:
            verify_entry(entry, self.public)
        self.assertEqual(ctx.exception.code, "EntryShape")


class StrictFieldsTest(unittest.TestCase):
    """Signed but malformed fields are refused. The signature is valid in every case."""

    def setUp(self):
        self.private, encoded = make_key()
        self.public = load_public_key(encoded)

    def refused(self, mutate, code):
        fields = base_fields()
        mutate(fields)
        with self.assertRaises(RevocationError) as ctx:
            verify_entry(sign(self.private, fields), self.public)
        self.assertEqual(ctx.exception.code, code)

    def test_prev_must_be_exactly_64_lowercase_hex(self):
        for bad in ("sha256:", "sha256:" + "0" * 63, "sha256:" + "0" * 65,
                    "sha256:" + "A" * 64, "sha256:" + "0" * 64 + "\n", "md5:" + "0" * 64, 7):
            with self.subTest(prev=bad):
                self.refused(lambda f, b=bad: f.update(prev=b), "EntryShape")

    def test_digest_target_must_be_exactly_64_lowercase_hex(self):
        for bad in ("sha256:", "sha256:xyz", "sha256:" + "A" * 64, "sha256:" + "a" * 64 + "\n"):
            with self.subTest(digest=bad):
                self.refused(
                    lambda f, b=bad: f.update(target={"kind": "tool", "digest": b}), "TargetInvalid"
                )

    def test_name_and_version_must_follow_the_manifest_grammar(self):
        for name, version in (("", "1.0.0"), ("Git-Log", "1.0.0"), ("g", "1.0.0"),
                              ("git_log", "1.0.0"), ("git-log\n", "1.0.0"),
                              ("git-log", ""), ("git-log", "1.0"), ("git-log", "01.0.0"),
                              ("git-log", "1.0.0\n"), ("git-log", "^1.0.0"), (5, "1.0.0")):
            with self.subTest(name=name, version=version):
                self.refused(
                    lambda f, n=name, v=version: f.update(
                        target={"kind": "tool", "name": n, "version": v}
                    ),
                    "TargetInvalid",
                )

    def test_target_with_digest_and_name_is_refused(self):
        self.refused(
            lambda f: f.update(
                target={"kind": "tool", "digest": DIGEST, "name": "git-log", "version": "1.0.0"}
            ),
            "TargetInvalid",
        )

    def test_actor_and_reference_must_be_non_empty_strings(self):
        for field in ("actor", "reference"):
            for bad in ("", 5, None, "x" * 257, "a\x00b", "a\nb"):
                with self.subTest(field=field, value=bad):
                    self.refused(lambda f, k=field, b=bad: f.update({k: b}), "EntryShape")

    def test_time_must_be_a_utc_timestamp(self):
        for bad in ("", "2026-10-18", "2026-10-18T00:00:00", "2026-10-18T00:00:00+00:00",
                    "2026-13-18T00:00:00Z", 0, None):
            with self.subTest(time=bad):
                self.refused(lambda f, b=bad: f.update(time=b), "EntryShape")

    def test_time_must_be_the_exact_zero_padded_form(self):
        for bad in ("2026-1-8T0:0:0Z", "2026-10-8T00:00:00Z", "2026-10-18T0:00:00Z", "2026-10-18T00:0:00Z",
                    " 2026-10-18T00:00:00Z", "2026-10-18T00:00:00Z\n", "2026-10-18T00:00:00z",
                    "\uff12\uff10\uff12\uff16-10-18T00:00:00Z", "2026-02-30T00:00:00Z", "2026-10-18T24:00:00Z"):
            with self.subTest(time=bad):
                self.refused(lambda f, b=bad: f.update(time=b), "EntryShape")

    def sign_surrogate(self, **override):
        """Sign bytes that a lone surrogate forced out of the canonical encoder, as an attacker could."""
        fields = base_fields()
        fields.update(override)
        raw = json.dumps(fields, sort_keys=True, indent=2, ensure_ascii=False, allow_nan=False) + "\n"
        signed = dict(fields)
        signed["sig"] = base64.b64encode(self.private.sign(raw.encode("utf-8", "surrogatepass"))).decode("ascii")
        return signed

    def test_text_that_cannot_be_utf8_is_a_typed_failure_not_a_crash(self):
        for field in ("actor", "reference"):
            for bad in ("\ud800", "ok\udfff", "\ud83d"):
                with self.subTest(field=field, value=ascii(bad)):
                    with self.assertRaises(RevocationError) as ctx:
                        verify_entry(self.sign_surrogate(**{field: bad}), self.public)
                    self.assertEqual(ctx.exception.code, "EntryShape")

    def test_a_json_line_with_an_escaped_lone_surrogate_is_a_typed_failure(self):
        line = json.dumps(dict(sign(self.private, base_fields()), actor="x")).replace('"actor": "x"', '"actor": "\\ud800"')
        self.assertIn("\\ud800", line)
        parsed = parse_feed_line(line)
        self.assertEqual(parsed["actor"], "\ud800")
        with self.assertRaises(RevocationError) as ctx:
            verify_entry(parsed, self.public)
        self.assertEqual(ctx.exception.code, "EntryShape")

    def test_boolean_seq_is_refused(self):
        self.refused(lambda f: f.update(seq=True), "EntryShape")


class ParseFeedLineTest(unittest.TestCase):
    def setUp(self):
        self.private, encoded = make_key()
        self.public = load_public_key(encoded)

    def line_for(self, entry):
        return json.dumps(entry, sort_keys=True) + "\n"

    def test_parsed_line_verifies(self):
        entry = sign(self.private, base_fields())
        parsed = parse_feed_line(self.line_for(entry))
        self.assertEqual(verify_entry(parsed, self.public), entry)

    def test_bytes_line_without_a_final_newline_parses(self):
        entry = sign(self.private, base_fields())
        parsed = parse_feed_line(json.dumps(entry).encode("utf-8"))
        self.assertEqual(parsed, entry)

    def test_a_changed_byte_in_the_line_fails_verification(self):
        entry = sign(self.private, base_fields())
        line = self.line_for(entry).replace("INC-1", "INC-2")
        with self.assertRaises(RevocationError) as ctx:
            verify_entry(parse_feed_line(line), self.public)
        self.assertEqual(ctx.exception.code, "BadSignature")

    def test_malformed_lines_are_refused(self):
        for bad in ("", "\n", "   ", "{", "not json", "[1, 2]", "42", "null", '"text"',
                    '{"a": 1}\n{"b": 2}', '{"a": NaN}', '{"a": Infinity}', "{\"a\": 1}\r",
                    b"\xff\xfe", 5):
            with self.subTest(line=bad):
                with self.assertRaises(RevocationError) as ctx:
                    parse_feed_line(bad)
                self.assertEqual(ctx.exception.code, "LineInvalid")

    def test_duplicate_keys_are_refused(self):
        with self.assertRaises(RevocationError) as ctx:
            parse_feed_line('{"seq": 1, "seq": 2}')
        self.assertEqual(ctx.exception.code, "LineInvalid")

    def test_an_oversize_line_is_refused(self):
        with self.assertRaises(RevocationError) as ctx:
            parse_feed_line('{"a": "' + "x" * 70_000 + '"}')
        self.assertEqual(ctx.exception.code, "LineTooLong")


class KeyTest(unittest.TestCase):
    def test_absent_key_is_a_typed_error(self):
        with self.assertRaises(RevocationError) as ctx:
            load_public_key("")
        self.assertEqual(ctx.exception.code, "KeyMissing")

    def test_malformed_key_is_a_typed_error(self):
        with self.assertRaises(RevocationError) as ctx:
            load_public_key("not-a-key")
        self.assertEqual(ctx.exception.code, "KeyMissing")


class EntryHashTest(unittest.TestCase):
    def test_hash_includes_sig(self):
        private, _ = make_key()
        entry = sign(private, base_fields())
        other = dict(entry, sig=entry["sig"][:-4] + "AAAA")
        self.assertNotEqual(entry_hash(entry), entry_hash(other))

    def test_hash_is_sha256_prefixed(self):
        private, _ = make_key()
        self.assertTrue(entry_hash(sign(private, base_fields())).startswith("sha256:"))


if __name__ == "__main__":
    unittest.main()
