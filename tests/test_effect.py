"""T-008: a tool declares what a call can change (`spec.effect`), and every discovery answer reports it.

`spec.effect` is read-only, reversible or irreversible. It is optional: when absent the effect is read-only for a
tool declared `readOnly: true` and irreversible otherwise. A declared effect must agree with `readOnly`.
"""

import copy
import json
import tempfile
import unittest
from pathlib import Path

import yaml

from capability.admission import Admission, Limits
from capability.authz import DevAdmissionAuthorizer
from capability.manifest import EFFECTS, tool_effect, validate_manifest
from capability.registry_api import RegistryService
from capability.store import SQLiteStore
from capability.store_registry import StoreRegistry

import test_tool_call as tc
from fixture_support import REPO
from registry_support import LANGUAGES, UnconfinedTestSandbox, admission_key, bundle_bytes, fresh_revocation

AUTHOR, APPROVER, AGENT = "author:acme", "approver:sec", "agent:orchestrator"


def manifest(name, read_only, effect=None):
    m = tc.probe_manifest(name, "write-root")
    m["spec"]["readOnly"] = read_only
    if effect is not None:
        m["spec"]["effect"] = effect
    return m


class EffectRuleTest(unittest.TestCase):
    def test_a_declared_effect_is_the_effect(self):
        for effect in EFFECTS:
            with self.subTest(effect=effect):
                self.assertEqual(tool_effect({"readOnly": effect == "read-only", "effect": effect}), effect)

    def test_without_a_declaration_read_only_follows_readonly_and_anything_else_is_irreversible(self):
        self.assertEqual(tool_effect({"readOnly": True}), "read-only")
        self.assertEqual(tool_effect({"readOnly": False}), "irreversible")
        self.assertEqual(tool_effect({}), "irreversible")
        self.assertEqual(tool_effect({"readOnly": "true"}), "irreversible")  # only the boolean counts

    def test_the_three_effects(self):
        self.assertEqual(EFFECTS, ("read-only", "reversible", "irreversible"))


class EffectValidationTest(unittest.TestCase):
    def test_consistent_declarations_are_valid(self):
        for read_only, effect in ((True, "read-only"), (False, "reversible"), (False, "irreversible"),
                                  (True, None), (False, None)):
            with self.subTest(readOnly=read_only, effect=effect):
                self.assertEqual(validate_manifest(manifest("probe-x", read_only, effect)), [])

    def test_an_effect_that_contradicts_readonly_is_refused(self):
        for read_only, effect in ((False, "read-only"), (True, "reversible"), (True, "irreversible")):
            with self.subTest(readOnly=read_only, effect=effect):
                errors = validate_manifest(manifest("probe-x", read_only, effect))
                self.assertEqual(errors, ["spec.effect: read-only exactly when spec.readOnly is true"])

    def test_an_unknown_or_malformed_effect_is_a_schema_error(self):
        for effect in ("destructive", "Reversible", "", 1, None, ["reversible"]):
            with self.subTest(effect=effect):
                m = manifest("probe-x", False)
                m["spec"]["effect"] = effect
                errors = validate_manifest(m)
                self.assertTrue(errors)
                self.assertTrue(all(e.startswith("spec") or "effect" in e for e in errors), errors)


class EffectThroughTheRegistryTest(unittest.TestCase):
    """Admission accepts or refuses on the effect, and every discovery answer reports it."""

    @classmethod
    def setUpClass(cls):
        cls._tmp = tempfile.TemporaryDirectory()
        base = Path(cls._tmp.name)
        cls.store = SQLiteStore(base / "data")
        private, public = admission_key()
        client, _feed, _clocks = fresh_revocation()
        registry = StoreRegistry(cls.store, public, client, LANGUAGES)
        admission = Admission(cls.store, private, UnconfinedTestSandbox(), LANGUAGES, base / "work",
                              limits=Limits(max_bundle_bytes=64 * 1024))
        cls.service = RegistryService(registry, admission, DevAdmissionAuthorizer(approvers={APPROVER}), private,
                                      process_inline=True)
        cls.tools = {"probe-reads": manifest("probe-reads", True), "probe-undo": manifest("probe-undo", False, "reversible"),
                     "probe-final": manifest("probe-final", False, "irreversible"), "probe-plain": manifest("probe-plain", False),
                     "probe-sure": manifest("probe-sure", True, "read-only")}
        for m in cls.tools.values():
            state = cls.submit(m)["state"]
            assert state == "awaiting_approval", state
            status, _, data = cls.service.handle("POST", f"/v2/submissions/{cls.last}/approve",
                                                 {"X-Dev-Principal": APPROVER}, b"")
            assert json.loads(data)["state"] == "admitted"

    @classmethod
    def tearDownClass(cls):
        cls.service.stop()
        cls.store.close()
        cls._tmp.cleanup()

    @classmethod
    def submit(cls, m):
        status, _, data = cls.service.handle("POST", "/v2/submissions", {"X-Dev-Principal": AUTHOR},
                                             bundle_bytes(manifest=copy.deepcopy(m), source=tc.PROBE,
                                                          cases=copy.deepcopy(tc.PROBE_CASES)))
        record = json.loads(data)
        cls.last = record["id"]
        return record

    def get(self, target):
        status, _, data = self.service.handle("GET", target, {"X-Dev-Principal": AGENT}, b"")
        self.assertEqual(status, 200, data)
        return json.loads(data)

    EXPECTED = {"probe-reads": "read-only", "probe-undo": "reversible", "probe-final": "irreversible",
                "probe-plain": "irreversible", "probe-sure": "read-only"}

    def test_describe_reports_the_effect_and_the_declaration(self):
        for name, effect in self.EXPECTED.items():
            with self.subTest(name=name):
                body = self.get(f"/v2/tools/{name}")
                self.assertEqual(body["effect"], effect)
                declared = self.tools[name]["spec"].get("effect")
                self.assertEqual(body["spec"].get("effect"), declared)  # absent stays absent in the declaration

    def test_search_and_list_report_the_effect(self):
        found = {item["name"]: item["effect"] for item in self.get("/v2/search?q=probe&limit=50")["items"]}
        self.assertEqual(found, self.EXPECTED)
        listed = {item["name"]: item["effect"] for item in self.get("/v2/tools?limit=50")["items"]}
        self.assertEqual(listed, self.EXPECTED)

    def test_admission_refuses_a_contradicting_effect(self):
        record = self.submit(manifest("probe-liar", True, "reversible"))
        self.assertEqual(record["state"], "rejected")
        self.assertIn("spec.effect: read-only exactly when spec.readOnly is true", json.dumps(record["problems"]))


class BuiltInToolsEffectTest(unittest.TestCase):
    def test_the_built_in_tools_declare_no_effect_and_report_read_only(self):
        for tool in ("git-log", "glob", "grep", "unawaited-async", "graph-query"):
            with self.subTest(tool=tool):
                spec = yaml.safe_load((REPO / "catalog" / "tool" / tool / "manifest.yaml").read_text())["spec"]
                self.assertNotIn("effect", spec)  # no new versions: the default gives the same answer
                self.assertEqual(tool_effect(spec), "read-only")


if __name__ == "__main__":
    unittest.main()
