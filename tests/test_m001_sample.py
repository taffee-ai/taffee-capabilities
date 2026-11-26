"""M2: the model sample in M-001 SPEC §5.1 keeps the same structure as the model contract.

The sample's values are placeholders, so this test compares key structure only:
every key the sample declares must be a key the model contract allows, and the
structure must match the passing model fixture. If the sample and the contract
drift apart, this test fails.
"""

import re
import unittest
from pathlib import Path

import yaml

from capability.manifest import _StrictLoader, load_manifest

FIXTURES = Path(__file__).resolve().parent / "fixtures" / "manifests"
SPEC = (
    Path(__file__).resolve().parent.parent
    / "docs" / "design" / "models" / "M-001_model_approval" / "SPEC.md"
)


def _sample_block() -> str:
    text = SPEC.read_text(encoding="utf-8")
    for block in re.findall(r"```yaml\n(.*?)```", text, flags=re.S):
        if block.lstrip().startswith("kind: model"):
            return block
    raise AssertionError("no model sample found in M-001 SPEC")


def _key_tree(value):
    """Keys only, recursively. Scalar and list values are not compared."""
    if isinstance(value, dict):
        return {key: _key_tree(child) for key, child in value.items()}
    return None


class M001SampleTest(unittest.TestCase):
    def test_sample_parses_without_duplicate_keys(self):
        sample = yaml.load(_sample_block(), Loader=_StrictLoader)
        self.assertIsInstance(sample, dict)

    def test_sample_structure_matches_the_model_fixture(self):
        sample = yaml.load(_sample_block(), Loader=_StrictLoader)
        fixture = load_manifest(FIXTURES / "valid" / "model.yaml")
        self.assertEqual(_key_tree(sample), _key_tree(fixture))

    def test_sample_declares_no_route_or_approval_fields(self):
        sample = yaml.load(_sample_block(), Loader=_StrictLoader)
        for forbidden in ("endpoint", "apiKeyEnv", "timeoutMs", "region", "egress", "weights",
                          "costPer1kTokens", "retired", "proposed_by"):
            with self.subTest(field=forbidden):
                self.assertNotIn(forbidden, sample)
                self.assertNotIn(forbidden, sample.get("provider", {}))
                self.assertNotIn(forbidden, sample.get("dataTerms", {}))


if __name__ == "__main__":
    unittest.main()
