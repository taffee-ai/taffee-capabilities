"""S-002 M4: entry checks, per-resolve digests, and the Match (SPEC §5, §6.2 steps 6 to 9)."""

import json
import os
import shutil
import tempfile
import unittest
from pathlib import Path

from capability.index import build_index
from capability.manifest import load_manifest
from capability.match import check_entry
from capability.registry import Registry, RegistryError

from fixture_support import FIXTURES, REPO, TOOL_VERSIONS, add_fixture_tool, copy_suite, new_tree, write_manifest


class TwoLanguageTreeTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = new_tree(Path(self._tmp.name))
        add_fixture_tool(self.root, "python")
        add_fixture_tool(self.root, "bash")
        manifest = load_manifest(FIXTURES / "valid" / "tool.yaml")
        manifest["implementations"] = [
            {"language": "python", "location": "library/fixture-tool/python"},
            {"language": "bash", "location": "library/fixture-tool/bash"},
        ]
        write_manifest(self.root, "tool", "fixture-tool", manifest)
        build_index(self.root, fixture_mode=True)
        self.registry = Registry(self.root)

    def tearDown(self):
        self._tmp.cleanup()

    def _python_main(self) -> Path:
        return self.root / "library" / "fixture-tool" / "python" / "main.py"


class MatchTest(TwoLanguageTreeTest):
    def test_match_carries_inert_metadata_for_the_chosen_language(self):
        match = self.registry.resolve("fixture-tool", languages=["python"])
        self.assertEqual(match["language"], "python")
        self.assertEqual(match["implementation_location"], "library/fixture-tool/python")
        self.assertEqual(match["entry_path"], "library/fixture-tool/python/main.py")
        self.assertEqual(match["revocation"], "unchecked")
        json.dumps(match)  # plain data, no callable

    def test_match_identities_equal_the_index_when_nothing_changed(self):
        index = json.loads((self.root / "index" / "index.json").read_bytes())
        entry = index["entries"][0]
        match = self.registry.resolve("fixture-tool", languages=["python"])
        self.assertEqual(match["manifest_digest"], entry["manifest_digest"])
        self.assertEqual(match["code_digest"], next(i["code_digest"] for i in entry["implementations"]
                                                     if i["language"] == "python"))

    def test_a_changed_byte_gives_integrity_error_on_the_next_call_with_no_cache(self):
        self.registry.resolve("fixture-tool", languages=["python"])
        self._python_main().write_text("print('changed')\n", encoding="utf-8")
        with self.assertRaises(RegistryError) as ctx:
            self.registry.resolve("fixture-tool", languages=["python"])
        self.assertEqual(ctx.exception.code, "IntegrityError")

    def test_resolve_does_not_run_the_entry(self):
        marker = self.root / "ran.marker"
        self._python_main().write_text(
            f"import pathlib\npathlib.Path({str(marker)!r}).write_text('ran')\n", encoding="utf-8")
        build_index(self.root, fixture_mode=True)
        self.registry = Registry(self.root)
        self.registry.resolve("fixture-tool", languages=["python"])
        self.assertFalse(marker.exists())

    def test_no_implementation_stops_before_any_entry_check(self):
        with self.assertRaises(RegistryError) as ctx:
            self.registry.resolve("fixture-tool", languages=["typescript"])
        self.assertEqual(ctx.exception.code, "NoImplementation")


class EntryErrorTest(TwoLanguageTreeTest):
    def test_missing_entry_is_entry_error_missing(self):
        self._python_main().unlink()
        with self.assertRaises(RegistryError) as ctx:
            self.registry.resolve("fixture-tool", languages=["python"])
        self.assertEqual(ctx.exception.code, "EntryError")
        self.assertEqual(ctx.exception.reason, "Missing")

    def test_entry_that_is_a_link_is_entry_error_link(self):
        target = self.root / "elsewhere.py"
        target.write_text("print('x')\n", encoding="utf-8")
        self._python_main().unlink()
        os.symlink(target, self._python_main())
        with self.assertRaises(RegistryError) as ctx:
            self.registry.resolve("fixture-tool", languages=["python"])
        self.assertEqual(ctx.exception.reason, "Link")

    def test_a_dangling_entry_link_is_entry_error_link_not_missing(self):
        self._python_main().unlink()
        os.symlink(self.root / "does-not-exist.py", self._python_main())
        with self.assertRaises(RegistryError) as ctx:
            self.registry.resolve("fixture-tool", languages=["python"])
        self.assertEqual(ctx.exception.reason, "Link")

    def test_implementation_folder_reached_through_a_link_is_entry_error_link(self):
        real = self.root / "real-python"
        shutil.move(str(self.root / "library" / "fixture-tool" / "python"), str(real))
        os.symlink(real, self.root / "library" / "fixture-tool" / "python")
        with self.assertRaises(RegistryError) as ctx:
            self.registry.resolve("fixture-tool", languages=["python"])
        self.assertEqual(ctx.exception.reason, "Link")

    def test_entry_resolving_outside_its_folder_is_escape(self):
        folder = self.root / "library" / "fixture-tool" / "python"
        (self.root / "outside.py").write_text("print('x')\n", encoding="utf-8")
        with self.assertRaises(RegistryError) as ctx:
            check_entry(self.root, folder, "../../../outside.py")
        self.assertEqual(ctx.exception.reason, "Escape")


class AgentAndModelTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = new_tree(Path(self._tmp.name))

    def tearDown(self):
        self._tmp.cleanup()

    def test_library_agent_resolves_with_its_code_digest(self):
        agent = load_manifest(FIXTURES / "valid" / "agent.yaml")
        folder = self.root / "library" / "fixture-agent"
        folder.mkdir(parents=True)
        (folder / "agent.txt").write_text("agent\n", encoding="utf-8")
        write_manifest(self.root, "agent", "fixture-agent", agent)
        build_index(self.root, fixture_mode=True)
        match = Registry(self.root).resolve("fixture-agent")
        self.assertEqual(match["kind"], "agent")
        self.assertIsNotNone(match["code_digest"])
        self.assertNotIn("language", match)

    def test_external_model_is_source_unavailable(self):
        import subprocess
        from capability.pins import archive_tree_digest

        repo = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, repo, True)
        (repo / "model.txt").write_text("model\n", encoding="utf-8")
        git = ["git", "-c", "user.name=t", "-c", "user.email=t@example.com", "-C", str(repo)]
        subprocess.run(["git", "init", "-q", str(repo)], check=True)
        subprocess.run(git + ["add", "."], check=True)
        subprocess.run(git + ["-c", "commit.gpgsign=false", "commit", "-q", "-m", "m"], check=True)
        commit = subprocess.run(git + ["rev-parse", "HEAD"], check=True, capture_output=True, text=True).stdout.strip()
        url = "https://example.com/org/model-repo"
        model = load_manifest(FIXTURES / "valid" / "model.yaml")
        model["source"] = {"type": "external", "repository": url, "commit": commit, "path": "model.txt",
                           "tree_sha256": archive_tree_digest(repo, commit, "model.txt")}
        write_manifest(self.root, "model", "fixture-model", model)
        build_index(self.root, fixture_mode=True, pin_repos={url: repo})
        with self.assertRaises(RegistryError) as ctx:
            Registry(self.root).resolve("fixture-model")
        self.assertEqual(ctx.exception.code, "SourceUnavailable")


class PolicyBindingTest(TwoLanguageTreeTest):
    """The entry comes from the verified index. A later policy change cannot change the Match."""

    def test_policy_change_after_indexing_does_not_change_the_match(self):
        before = self.registry.resolve("fixture-tool", languages=["python"])
        policy = self.root / "policy" / "languages.yaml"
        policy.write_text(policy.read_text(encoding="utf-8").replace("entry: main.py", "entry: other.py", 1),
                          encoding="utf-8")
        after = self.registry.resolve("fixture-tool", languages=["python"])
        self.assertEqual(before["entry_path"], "library/fixture-tool/python/main.py")
        self.assertEqual(after, before)

    def test_a_second_candidate_file_in_the_folder_is_refused_by_the_code_digest(self):
        (self.root / "library" / "fixture-tool" / "python" / "other.py").write_text("print('x')\n", encoding="utf-8")
        with self.assertRaises(RegistryError) as ctx:
            self.registry.resolve("fixture-tool", languages=["python"])
        self.assertEqual(ctx.exception.code, "IntegrityError")


class DigestOrderTest(unittest.TestCase):
    """SPEC §6.2: the manifest is checked before the code, for agents as for tools."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = new_tree(Path(self._tmp.name))
        agent = load_manifest(FIXTURES / "valid" / "agent.yaml")
        folder = self.root / "library" / "fixture-agent"
        folder.mkdir(parents=True)
        (folder / "agent.txt").write_text("agent\n", encoding="utf-8")
        write_manifest(self.root, "agent", "fixture-agent", agent)
        build_index(self.root, fixture_mode=True)

    def tearDown(self):
        self._tmp.cleanup()

    def test_dual_fault_reports_the_manifest_error_first(self):
        import json
        from capability.canon import canonical_bytes
        from capability.index import index_digest

        path = self.root / "index" / "index.json"
        data = json.loads(path.read_bytes())
        data["entries"][0]["manifest_digest"] = "sha256:" + "0" * 64
        data["index_digest"] = index_digest(data)
        path.write_bytes(canonical_bytes(data))
        (self.root / "library" / "fixture-agent" / "agent.txt").write_text("changed\n", encoding="utf-8")
        with self.assertRaises(RegistryError) as ctx:
            Registry(self.root).resolve("fixture-agent")
        self.assertEqual(ctx.exception.code, "IntegrityError")
        self.assertIn("manifest_digest", str(ctx.exception))


class FiveToolsTest(unittest.TestCase):
    """Since T-001 four of the five resolve to a Match for python; graph-query still has none."""

    def _registry(self, tmp):
        root = Path(tmp)
        shutil.copytree(REPO / "schemas", root / "schemas")
        shutil.copytree(REPO / "policy", root / "policy")
        shutil.copytree(REPO / "catalog", root / "catalog")
        shutil.copytree(REPO / "library", root / "library")
        shutil.copytree(REPO / "evidence", root / "evidence")
        copy_suite(root)
        build_index(root)
        return Registry(root)

    def test_four_resolve_to_a_python_match(self):
        with tempfile.TemporaryDirectory() as tmp:
            registry = self._registry(tmp)
            for name in ["git-log", "glob", "grep", "unawaited-async"]:
                with self.subTest(tool=name):
                    match = registry.resolve(name, languages=["python"])
                    self.assertEqual(match["language"], "python")
                    self.assertEqual(match["version"], TOOL_VERSIONS[name])
                    self.assertEqual(match["entry_path"], "library/%s/python/main.py" % name)
                    self.assertEqual(match["label"], "Tested")
                    self.assertEqual(match["revocation"], "unchecked")

    def test_a_language_the_tool_lacks_is_no_implementation(self):
        with tempfile.TemporaryDirectory() as tmp:
            registry = self._registry(tmp)
            for name in ["git-log", "glob", "grep", "unawaited-async"]:
                with self.subTest(tool=name):
                    with self.assertRaises(RegistryError) as ctx:
                        registry.resolve(name, languages=["typescript"])
                    self.assertEqual(ctx.exception.code, "NoImplementation")

    def test_graph_query_resolves_to_no_implementation(self):
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(RegistryError) as ctx:
                self._registry(tmp).resolve("graph-query", languages=["python"])
            self.assertEqual(ctx.exception.code, "NoImplementation")

    def test_a_changed_byte_in_a_real_tool_is_an_integrity_error(self):
        with tempfile.TemporaryDirectory() as tmp:
            registry = self._registry(tmp)
            main = Path(tmp) / "library" / "grep" / "python" / "main.py"
            main.write_text(main.read_text(encoding="utf-8") + "\n# tampered\n", encoding="utf-8")
            with self.assertRaises(RegistryError) as ctx:
                registry.resolve("grep", languages=["python"])
            self.assertEqual(ctx.exception.code, "IntegrityError")


if __name__ == "__main__":
    unittest.main()
