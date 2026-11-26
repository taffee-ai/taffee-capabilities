"""S-005: the umbrella catalog is generated from the verified index."""

import contextlib
import io
import json
import shutil
import tempfile
import unittest
from pathlib import Path

import yaml

from capability.catalog_export import CatalogError, build_catalog, catalog_entry, main, render, write_catalog
from capability.index import build_index

from fixture_support import FIXTURES, TOOL_VERSIONS, new_tree, write_manifest
from test_capctl import RealCatalogCase

COMMIT = "0123456789abcdef0123456789abcdef01234567"


class RealCatalogExportTest(RealCatalogCase):
    def out(self):
        return Path(self._tmp.name) / "capabilities.yaml"

    def load(self, commit=None):
        path = write_catalog(self.root, self.out(), commit)
        return yaml.safe_load(path.read_text(encoding="utf-8"))

    def test_the_header_carries_the_index_digests_and_policy(self):
        index = json.loads((self.root / "index" / "index.json").read_bytes())
        data = self.load()
        self.assertEqual((data["version"], data["pillar"], data["repo"]), (1, "capabilities", "taffee-capabilities"))
        self.assertEqual(data["policy_version"], index["policy_version"])
        self.assertEqual(data["inputs_digest"], index["inputs_digest"])
        self.assertEqual(data["index_digest"], index["index_digest"])
        self.assertIsNone(data["index_version"])

    def test_the_commit_is_recorded_when_given(self):
        self.assertEqual(self.load(COMMIT)["index_version"], COMMIT)

    def test_a_malformed_commit_is_refused_and_nothing_is_written(self):
        for bad in ("abc", COMMIT.upper(), COMMIT + "0", "g" * 40, ""):
            with self.subTest(commit=bad):
                with self.assertRaises(CatalogError):
                    write_catalog(self.root, self.out(), bad)
                self.assertFalse(self.out().exists())

    def test_entries_are_sorted_and_complete(self):
        data = self.load()
        self.assertEqual([e["name"] for e in data["entries"]],
                         ["git-log", "glob", "graph-query", "grep", "unawaited-async"])
        for entry in data["entries"]:
            self.assertEqual(list(entry), ["name", "kind", "version", "digest", "label", "owner", "summary", "source", "evidence"])
            self.assertEqual(entry["kind"], "tool")
            self.assertEqual(entry["owner"], "capabilities-tools-owner")

    def test_a_tool_digest_is_its_identity_and_labels_come_from_the_index(self):
        index = json.loads((self.root / "index" / "index.json").read_bytes())
        by_name = {e["name"]: e for e in index["entries"]}
        for entry in self.load()["entries"]:
            with self.subTest(tool=entry["name"]):
                self.assertEqual(entry["digest"], by_name[entry["name"]]["identity"])
                self.assertEqual(entry["label"], by_name[entry["name"]]["label"])

    def test_tested_tools_point_at_their_evidence_folder(self):
        entries = {e["name"]: e for e in self.load()["entries"]}
        for tool in ("git-log", "glob", "grep", "unawaited-async"):
            self.assertEqual(entries[tool]["label"], "Tested")
            self.assertEqual(entries[tool]["source"], f"taffee-capabilities/library/{tool}")
            self.assertEqual(entries[tool]["evidence"], f"taffee-capabilities/evidence/tools/{tool}/{TOOL_VERSIONS[tool]}/")

    def test_a_declared_tool_with_no_implementation_has_no_source_or_evidence(self):
        entry = {e["name"]: e for e in self.load()["entries"]}["graph-query"]
        self.assertEqual((entry["label"], entry["source"], entry["evidence"]), ("Declared", None, None))

    def test_output_is_deterministic_text(self):
        first = render(build_catalog(self.root, COMMIT))
        second = render(build_catalog(self.root, COMMIT))
        self.assertEqual(first, second)
        self.assertNotIn(b"\r", first)
        self.assertTrue(first.endswith(b"\n") and not first.endswith(b"\n\n"))
        text = first.decode("utf-8")
        self.assertIn("GENERATED", text.splitlines()[1])
        self.assertIn("Do not edit by hand", text)

    def test_only_the_index_is_read(self):
        before = render(build_catalog(self.root, COMMIT))
        for folder in ("catalog", "library", "evidence"):
            shutil.rmtree(self.root / folder)
        self.assertEqual(render(build_catalog(self.root, COMMIT)), before)

    def test_a_tampered_index_is_an_error_and_an_existing_file_is_untouched(self):
        self.out().write_text("previous catalog\n", encoding="utf-8")
        path = self.root / "index" / "index.json"
        path.write_bytes(path.read_bytes().replace(b'"Tested"', b'"Approved"', 1))
        with self.assertRaises(CatalogError):
            write_catalog(self.root, self.out(), COMMIT)
        self.assertEqual(self.out().read_text(encoding="utf-8"), "previous catalog\n")
        self.assertEqual([p.name for p in self.out().parent.iterdir() if p.name.endswith(".tmp")], [])

    def test_a_missing_index_is_an_error_and_no_file_is_written(self):
        (self.root / "index" / "index.json").unlink()
        with self.assertRaises(CatalogError):
            write_catalog(self.root, self.out())
        self.assertFalse(self.out().exists())


class EntryShapeTest(unittest.TestCase):
    def base(self, **extra):
        entry = {
            "name": "thing", "kind": "agent", "version": "1.0.0", "label": "Declared",
            "manifest_digest": "sha256:" + "a" * 64, "evidence": [],
            "manifest": {"owner": "owner-x", "summary": "Does a thing."},
            "source": {"type": "library", "location": "library/thing"},
        }
        entry.update(extra)
        return entry

    def test_a_library_agent_uses_its_manifest_digest_and_location(self):
        entry = catalog_entry(self.base())
        self.assertEqual(entry["digest"], "sha256:" + "a" * 64)
        self.assertEqual(entry["source"], "taffee-capabilities/library/thing")
        self.assertIsNone(entry["evidence"])

    def test_an_external_pin_renders_as_repository_commit_path(self):
        source = {"type": "external", "repository": "https://example.invalid/r.git", "commit": "b" * 40,
                  "path": "agents/x", "tree_sha256": "sha256:" + "c" * 64}
        self.assertEqual(catalog_entry(self.base(source=source))["source"],
                         "https://example.invalid/r.git@" + "b" * 40 + ":agents/x")

    def test_a_model_is_not_given_evidence_paths(self):
        self.assertIsNone(catalog_entry(self.base(kind="model", label="Approved"))["evidence"])


class FixtureAgentTest(unittest.TestCase):
    def test_a_built_fixture_agent_appears_with_its_manifest_digest(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = new_tree(Path(tmp))
            manifest = yaml.safe_load((FIXTURES / "valid" / "agent.yaml").read_text(encoding="utf-8"))
            write_manifest(root, "agent", "fixture-agent", manifest)
            folder = root / "library" / "fixture-agent"
            folder.mkdir(parents=True)
            (folder / "agent.txt").write_text("agent\n", encoding="utf-8")
            index = build_index(root, fixture_mode=True)
            data = build_catalog(root)
        self.assertEqual(len(data["entries"]), 1)
        self.assertEqual(data["entries"][0]["digest"], index["entries"][0]["manifest_digest"])
        self.assertEqual(data["entries"][0]["source"], "taffee-capabilities/library/fixture-agent")


class CommandLineTest(RealCatalogCase):
    def run_main(self, *args):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = main(list(args))
        return code, out.getvalue(), err.getvalue()

    def test_success_prints_the_path_and_writes_the_file(self):
        target = Path(self._tmp.name) / "out.yaml"
        code, out, _ = self.run_main("--root", str(self.root), "--out", str(target), "--commit", COMMIT)
        self.assertEqual((code, out.strip()), (0, str(target)))
        self.assertEqual(yaml.safe_load(target.read_text(encoding="utf-8"))["index_version"], COMMIT)

    def test_an_index_error_exits_1_and_writes_nothing(self):
        (self.root / "index" / "index.json").unlink()
        target = Path(self._tmp.name) / "out.yaml"
        code, _, err = self.run_main("--root", str(self.root), "--out", str(target))
        self.assertEqual(code, 1)
        self.assertIn("error:", err)
        self.assertFalse(target.exists())

    def test_an_unwritable_destination_exits_1(self):
        code, _, err = self.run_main("--root", str(self.root), "--out", str(Path(self._tmp.name) / "no" / "dir" / "x.yaml"))
        self.assertEqual(code, 1)

    def test_bad_arguments_exit_2(self):
        for args in (("--root", "."), ("--out", "x.yaml", "--commit", "short")):
            with self.subTest(args=args):
                with self.assertRaises(SystemExit) as ctx, contextlib.redirect_stderr(io.StringIO()):
                    main(list(args))
                self.assertEqual(ctx.exception.code, 2)


if __name__ == "__main__":
    unittest.main()
