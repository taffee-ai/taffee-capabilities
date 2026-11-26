"""M2: schemas, manifest loading and validation (S-001 §4, §5, §11)."""

import copy
import tempfile
import unittest
from pathlib import Path

from capability.digest import manifest_digest
from capability.manifest import ManifestError, check_manifest_file, load_manifest, validate_manifest

FIXTURES = Path(__file__).resolve().parent / "fixtures" / "manifests"


def _valid_tool() -> dict:
    return load_manifest(FIXTURES / "valid" / "tool.yaml")


def _valid_agent() -> dict:
    return load_manifest(FIXTURES / "valid" / "agent.yaml")


def _errors_mentioning(errors, text):
    return [e for e in errors if text in e]


class ValidFixturesTest(unittest.TestCase):
    def test_tool_agent_model_fixtures_pass(self):
        for name in ("tool.yaml", "agent.yaml", "model.yaml"):
            with self.subTest(fixture=name):
                self.assertEqual(validate_manifest(load_manifest(FIXTURES / "valid" / name)), [])

    def test_check_manifest_file_returns_manifest(self):
        manifest = check_manifest_file(FIXTURES / "valid" / "tool.yaml")
        self.assertEqual(manifest["name"], "fixture-tool")


class InvalidFixturesTest(unittest.TestCase):
    def test_agent_without_outcomes_fails(self):
        errors = validate_manifest(load_manifest(FIXTURES / "invalid" / "agent-no-outcomes.yaml"))
        self.assertTrue(_errors_mentioning(errors, "outcomes"))

    def test_model_floating_alias_and_bad_category_fail(self):
        errors = validate_manifest(load_manifest(FIXTURES / "invalid" / "model-floating-version.yaml"))
        self.assertTrue(_errors_mentioning(errors, "version"))
        self.assertTrue(_errors_mentioning(errors, "modelId"))
        self.assertTrue(_errors_mentioning(errors, "modelVersion"))
        self.assertTrue(_errors_mentioning(errors, "category"))

    def test_raw_credential_in_model_is_refused_by_field_name(self):
        errors = validate_manifest(load_manifest(FIXTURES / "invalid" / "model-raw-key.yaml"))
        self.assertIn("provider.apiKey: unknown field", errors)

    def test_placeholder_owner_fails(self):
        errors = validate_manifest(load_manifest(FIXTURES / "invalid" / "tool-blank-owner.yaml"))
        self.assertTrue(_errors_mentioning(errors, "owner: placeholder"))

    def test_duplicate_key_is_refused_at_load(self):
        with self.assertRaises(ManifestError) as ctx:
            load_manifest(FIXTURES / "invalid" / "tool-duplicate-key.yaml")
        self.assertIn("duplicate key", str(ctx.exception))

    def test_date_looking_value_is_text_and_an_unknown_field(self):
        manifest = load_manifest(FIXTURES / "invalid" / "tool-date-value.yaml")
        self.assertEqual(manifest["released"], "2026-10-17")
        self.assertIn("released: unknown field", validate_manifest(manifest))

    def test_unquoted_no_is_reported_with_a_hint(self):
        manifest = load_manifest(FIXTURES / "valid" / "model.yaml")
        manifest["dataTerms"]["trainingUse"] = False  # what an unquoted `no` loads as
        errors = validate_manifest(manifest)
        self.assertTrue(_errors_mentioning(errors, "quote the value"))

    def test_provider_version_that_looks_like_a_date_is_text(self):
        manifest = load_manifest(FIXTURES / "valid" / "model.yaml")
        self.assertEqual(manifest["provider"]["modelVersion"], "2025-06-01")
        self.assertEqual(manifest["version"], "1.0.0")

    def test_model_top_level_version_must_be_semantic(self):
        manifest = load_manifest(FIXTURES / "valid" / "model.yaml")
        manifest["version"] = "2025-06-01"
        self.assertTrue(_errors_mentioning(validate_manifest(manifest), "version"))


class FieldRulesTest(unittest.TestCase):
    def test_unknown_field_fails_with_its_path(self):
        manifest = _valid_tool()
        manifest["foo"] = 1
        self.assertIn("foo: unknown field", validate_manifest(manifest))

    def test_missing_owner_fails_with_path_owner(self):
        manifest = _valid_tool()
        del manifest["owner"]
        self.assertTrue(_errors_mentioning(validate_manifest(manifest), "owner"))

    def test_name_rule(self):
        for bad in ("Git_Log", "a"):
            with self.subTest(name=bad):
                manifest = _valid_tool()
                manifest["name"] = bad
                self.assertTrue(_errors_mentioning(validate_manifest(manifest), "name"))
        manifest = _valid_tool()
        manifest["name"] = "git-log"
        self.assertEqual(validate_manifest(manifest), [])

    def test_version_rule(self):
        manifest = _valid_tool()
        manifest["version"] = "1.0"
        self.assertTrue(_errors_mentioning(validate_manifest(manifest), "version"))
        manifest["version"] = "1.0.0"
        self.assertEqual(validate_manifest(manifest), [])

    def test_label_written_in_manifest_is_refused(self):
        manifest = _valid_tool()
        manifest["label"] = "Tested"
        self.assertIn("label: unknown field", validate_manifest(manifest))

    def test_unknown_kind_is_refused(self):
        manifest = _valid_tool()
        manifest["kind"] = "widget"
        self.assertEqual(validate_manifest(manifest), ["kind: must be one of tool, agent, model"])

    def test_external_source_path_cannot_escape(self):
        manifest = _valid_agent()
        manifest["source"] = {
            "type": "external",
            "repository": "https://example.com/org/repo",
            "commit": "a" * 40,
            "path": "../outside",
            "tree_sha256": "sha256:" + "0" * 64,
        }
        self.assertTrue(validate_manifest(manifest))

    def test_external_source_commit_must_be_full_sha(self):
        manifest = _valid_agent()
        manifest["source"] = {
            "type": "external",
            "repository": "https://example.com/org/repo",
            "commit": "cb90f45",
            "path": "harness/tools/git-log.ts",
            "tree_sha256": "sha256:" + "0" * 64,
        }
        self.assertTrue(validate_manifest(manifest))

    def test_external_source_with_file_path_passes(self):
        manifest = _valid_agent()
        manifest["source"] = {
            "type": "external",
            "repository": "https://example.com/org/repo",
            "commit": "a" * 40,
            "path": "harness/tools/git-log.ts",
            "tree_sha256": "sha256:" + "0" * 64,
        }
        self.assertEqual(validate_manifest(manifest), [])

    def test_timeout_must_be_positive(self):
        manifest = _valid_tool()
        manifest["spec"]["timeoutMs"] = 0
        self.assertTrue(validate_manifest(manifest))


class YamlIdentityTest(unittest.TestCase):
    """M2 acceptance: a YAML comment does not change manifest_digest; a value does."""

    def _write(self, folder: Path, name: str, text: str) -> Path:
        path = folder / name
        path.write_text(text, encoding="utf-8")
        return path

    def test_comment_does_not_change_digest_and_value_does(self):
        base = (FIXTURES / "valid" / "tool.yaml").read_text(encoding="utf-8")
        with tempfile.TemporaryDirectory() as tmp:
            folder = Path(tmp)
            plain = self._write(folder, "plain.yaml", base)
            commented = self._write(folder, "commented.yaml", "# a comment added by an editor\n" + base)
            changed = self._write(folder, "changed.yaml", base.replace("timeoutMs: 5000", "timeoutMs: 6000"))
            d_plain = manifest_digest(load_manifest(plain))
            d_commented = manifest_digest(load_manifest(commented))
            d_changed = manifest_digest(load_manifest(changed))
        self.assertEqual(d_plain, d_commented)
        self.assertNotEqual(d_plain, d_changed)


if __name__ == "__main__":
    unittest.main()
