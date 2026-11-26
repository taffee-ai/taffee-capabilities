"""M1: canonical JSON (D-S1-05)."""

import unittest

from capability.canon import canonical_bytes


class CanonicalBytesTest(unittest.TestCase):
    def test_sorted_keys_two_space_indent_and_final_newline(self):
        got = canonical_bytes({"b": 1, "a": [1, 2]})
        want = b'{\n  "a": [\n    1,\n    2\n  ],\n  "b": 1\n}\n'
        self.assertEqual(got, want)

    def test_same_value_same_bytes_across_calls(self):
        value = {"z": {"y": 1, "x": "é"}, "a": None}
        self.assertEqual(canonical_bytes(value), canonical_bytes(value))

    def test_utf8_not_escaped(self):
        self.assertIn("é".encode("utf-8"), canonical_bytes({"k": "é"}))

    def test_nan_is_refused(self):
        with self.assertRaises(ValueError):
            canonical_bytes({"k": float("nan")})


if __name__ == "__main__":
    unittest.main()
