"""S-008: the read-only catalog service. Contract: docs/design/shared/S-008_catalog_service/CONTRACT-NOTE.md.

The transport-free core (`CatalogService.handle`) carries the checks; a short socket test covers the HTTP layer.
"""

import copy
import hashlib
import http.client
import json
import tempfile
import threading
import unittest
from pathlib import Path

from capability.authz import Authorizer, DenyAllAuthorizer, DevAuthorizer
from capability.canon import canonical_bytes
from capability.index import build_index
from capability.manifest import load_manifest
from capability.registry import Registry, RegistryError
from capability.revocation import GENESIS, RevocationClient, entry_hash, load_public_key
from capability.service import CatalogService, LIST_KEYS, make_server

from fixture_support import FIXTURES, REPO, add_fixture_tool, new_tree, write_manifest
from test_pins import _copy_catalog
from revocation_support import Clocks, chain, checkpoint, make_key

DEV = {"X-Dev-Principal": "user:tester"}
ROOT = None  # a copy of the real catalog inputs with a freshly built index (the repository commits no index/)
_TMP = None
GOLDEN = json.loads((REPO / "tests" / "golden" / "index.json").read_bytes())
FORBIDDEN = ("location", "entry", "code_digest", "implementations", "sandbox", "/", "python")


def setUpModule():
    global ROOT, _TMP
    _TMP = tempfile.TemporaryDirectory()
    ROOT = Path(_TMP.name) / "caps"
    ROOT.mkdir()
    _copy_catalog(ROOT)
    build_index(ROOT)


def tearDownModule():
    _TMP.cleanup()


def call(service, method, target, headers=DEV, body=b""):
    status, out, data = service.handle(method, target, headers, body)
    return status, out, json.loads(data)


class FakeIndexRegistry(Registry):
    """The real registry over the real tree, with extra index entries (for cases the golden index cannot show)."""

    def __init__(self, root, extra=()):
        super().__init__(root)
        self.extra = list(extra)

    def verified_index(self):
        index = copy.deepcopy(super().verified_index())
        index["entries"] = index["entries"] + copy.deepcopy(self.extra)
        return index

    def _entries(self, visible=None):
        entries = self.verified_index()["entries"]
        return entries if visible is None else [entry for entry in entries if visible(entry)]


class ConstructionTest(unittest.TestCase):
    def test_there_is_no_default_authorizer(self):
        with self.assertRaises(TypeError):
            CatalogService(Registry(ROOT))
        with self.assertRaises(ValueError):
            CatalogService(Registry(ROOT), None)
        with self.assertRaises(ValueError):
            CatalogService(Registry(ROOT), object())

    def test_deny_all_never_identifies_anyone(self):
        service = CatalogService(Registry(ROOT), DenyAllAuthorizer())
        self.assertEqual(call(service, "GET", "/v1/capabilities")[0], 401)
        self.assertEqual(call(service, "GET", "/v1/capabilities", {"X-Dev-Principal": "user:x"})[0], 401)

    def test_dev_authorizer_is_marked_and_needs_its_header(self):
        self.assertTrue(DevAuthorizer.DEVELOPMENT_ONLY)
        service = CatalogService(Registry(ROOT), DevAuthorizer())
        self.assertEqual(call(service, "GET", "/v1/capabilities", {})[0], 401)
        for bad in ("", " x", "a" * 129, "x\ny"):
            self.assertEqual(call(service, "GET", "/v1/capabilities", {"X-Dev-Principal": bad})[0], 401, bad)

    def test_a_failing_authorizer_refuses_the_request(self):
        class Broken(Authorizer):
            def principal(self, headers):
                return DevAuthorizer().principal(headers)

            def may_see(self, principal, ref):
                raise RuntimeError("secret /path")

        status, _, body = call(CatalogService(Registry(ROOT), Broken()), "GET", "/v1/capabilities")
        self.assertEqual((status, body["code"]), (503, "AuthorizerUnavailable"))
        self.assertNotIn("secret", json.dumps(body))


class ServiceTest(unittest.TestCase):
    def setUp(self):
        self.service = CatalogService(Registry(ROOT), DevAuthorizer())

    def get(self, target, headers=DEV):
        return call(self.service, "GET", target, headers)

    def test_list_returns_every_entry_with_exactly_the_allow_listed_keys(self):
        status, headers, body = self.get("/v1/capabilities")
        self.assertEqual(status, 200)
        self.assertEqual(headers["Cache-Control"], "no-store")
        self.assertEqual([e["name"] for e in body["entries"]], [e["name"] for e in GOLDEN["entries"]])
        for entry in body["entries"]:
            self.assertEqual(tuple(sorted(entry)), tuple(sorted(LIST_KEYS)))

    def test_filters_by_kind_label_and_text(self):
        names = lambda target: [e["name"] for e in self.get(target)[2]["entries"]]
        self.assertEqual(names("/v1/capabilities?kind=agent"), [])
        self.assertNotIn("graph-query", names("/v1/capabilities?minLabel=Tested"))
        self.assertEqual(names("/v1/capabilities?q=UNAWAITED"), ["unawaited-async"])
        self.assertEqual(names("/v1/capabilities?q=zzzznomatch"), [])

    def test_versions_and_detail(self):
        status, _, body = self.get("/v1/capabilities/git-log")
        self.assertEqual((status, [e["version"] for e in body["entries"]]), (200, ["1.2.0"]))
        status, _, detail = self.get("/v1/capabilities/git-log/1.2.0")
        self.assertEqual(status, 200)
        self.assertEqual(detail["manifest_view"]["projection_of"], detail["manifest_digest"])
        self.assertTrue(detail["evidence"])

    def test_detail_projection_has_exactly_the_allowed_keys_and_no_path_or_executable(self):
        _, _, detail = self.get("/v1/capabilities/git-log/1.2.0")
        view = detail["manifest_view"]
        self.assertEqual(sorted(view), sorted(["name", "kind", "version", "summary", "owner", "requires", "spec",
                                               "projection_of"]))
        self.assertEqual(sorted(view["spec"]),
                         sorted(["inputSchema", "postconditions", "idempotent", "readOnly", "timeoutMs", "boundary"]))
        self.assertEqual(sorted(detail), sorted(list(LIST_KEYS) + ["evidence", "manifest_view"]))
        text = json.dumps(detail)
        for word in ("location", "entry", "code_digest", "implementations", "sandbox", "library/"):
            self.assertNotIn(word, text)

    def test_projection_drops_sandbox_and_unknown_spec_keys(self):
        entry = copy.deepcopy(GOLDEN["entries"][0])
        entry["manifest"]["spec"]["sandbox"] = {"filesystem": "x"}
        entry["manifest"]["spec"]["surprise"] = {"location": "/secret"}
        view = CatalogService._manifest_view(entry)
        self.assertNotIn("sandbox", view["spec"])
        self.assertNotIn("surprise", view["spec"])
        self.assertNotIn("implementations", view)

    def test_agent_and_model_projections_exclude_source(self):
        for kind, fields in (("agent", {"outcomes": ["o"], "tools": [], "roles": [], "limits": {}}),
                             ("model", {"provider": "p", "category": "c", "licence": "l", "dataTerms": "d"})):
            entry = copy.deepcopy(GOLDEN["entries"][0])
            entry["kind"] = kind
            entry["manifest"] = {"name": "x-thing", "kind": kind, "version": "1.0.0", "summary": "s", "owner": "o",
                                 "requires": [], "source": {"type": "library", "location": "/secret"}, **fields}
            view = CatalogService._manifest_view(entry)
            self.assertNotIn("source", view)
            self.assertNotIn("/secret", json.dumps(view))
            self.assertEqual(set(fields) - set(view), set())

    def test_index_view_is_a_separate_redacted_document_with_its_own_digest(self):
        status, _, view = self.get("/v1/index")
        self.assertEqual(status, 200)
        self.assertEqual(sorted(view), ["entries", "schema", "source", "view_digest"])
        self.assertEqual(view["schema"], "capability-index-view/1")
        self.assertEqual(view["source"], {"index_digest": GOLDEN["index_digest"], "inputs_digest": GOLDEN["inputs_digest"],
                                          "policy_version": GOLDEN["policy_version"]})
        body = {k: v for k, v in view.items() if k != "view_digest"}
        self.assertEqual(view["view_digest"], "sha256:" + hashlib.sha256(canonical_bytes(body)).hexdigest())
        self.assertNotEqual(view["view_digest"], GOLDEN["index_digest"])
        self.assertNotIn("index_digest", view)
        for word in ("location", "code_digest", "implementations", "main.py"):
            self.assertNotIn(word, json.dumps(view))

    def test_resolve_ok_and_body_shape(self):
        status, _, body = call(self.service, "POST", "/v1/resolve", body=b'{"name":"git-log","kind":"tool","range":"^1.0.0","minLabel":"Tested"}')
        self.assertEqual(status, 200)
        self.assertEqual(sorted(body), ["digest", "kind", "label", "name", "requires", "revocation", "version"])
        self.assertEqual((body["name"], body["version"], body["label"], body["revocation"]),
                         ("git-log", "1.2.0", "Tested", "unchecked"))
        self.assertEqual(body["digest"], GOLDEN["entries"][0]["identity"])
        self.assertEqual(body["requires"], ["repo.read"])

    def test_resolve_below_label_names_have_and_need(self):
        status, _, body = call(self.service, "POST", "/v1/resolve", body=b'{"name":"graph-query","minLabel":"Tested"}')
        self.assertEqual((status, body["code"], body["have"], body["need"]), (409, "BelowLabel", "Declared", "Tested"))

    def test_resolve_errors(self):
        post = lambda raw: call(self.service, "POST", "/v1/resolve", body=raw)
        self.assertEqual(post(b'{"name":"nothing-here"}')[0:1], (404,))
        self.assertEqual(post(b'{"name":"git-log","range":"^9.0.0"}')[0], 404)
        self.assertEqual(post(b'{"name":"git-log","range":"banana"}')[2]["code"], "InvalidRange")
        for bad in (b"", b"[]", b"{", b'{"name":"git-log","name":"glob"}', b'{"name":"git-log","extra":1}',
                    b'{"range":"*"}', b'{"name":"git-log","minLabel":"Gold"}', b'{"name":"git-log","kind":"x"}',
                    b'{"name":"git-log","range":' + b'1' * 5000 + b'}', b'{"name":"Git-Log"}', b'{"name":NaN}',
                    b'{"name":"git-log","range":"' + b"1" * 40 + b'"}'):
            self.assertEqual(post(bad)[0], 400, bad[:40])

    def test_request_bounds_and_methods(self):
        self.assertEqual(self.get("/v1/capabilities?bogus=1")[0], 400)
        self.assertEqual(self.get("/v1/capabilities?kind=tool&kind=agent")[0], 400)
        self.assertEqual(self.get("/v1/capabilities?kind=nonsense")[0], 400)
        self.assertEqual(self.get("/v1/capabilities?minLabel=Gold")[0], 400)
        self.assertEqual(self.get("/v1/capabilities?q=" + "a" * 201)[0], 400)
        self.assertEqual(self.get("/v1/index?x=1")[0], 400)
        self.assertEqual(call(self.service, "POST", "/v1/capabilities")[0], 405)
        self.assertEqual(call(self.service, "GET", "/v1/resolve")[0], 405)
        self.assertEqual(call(self.service, "DELETE", "/v1/index")[0], 405)
        self.assertEqual(self.get("/v2/capabilities")[0], 404)
        self.assertEqual(self.get("/v1/capabilities/Git-Log")[0], 404)
        self.assertEqual(self.get("/v1/capabilities/git-log/01.2.0")[0], 404)
        self.assertEqual(self.get("/v1/capabilities/git-log/1.2.0/extra")[0], 404)

    def test_the_raw_query_string_is_bounded_before_parsing(self):
        self.assertEqual(self.get("/v1/capabilities?kind=" + "t" * 1100)[0], 400)
        self.assertEqual(self.get("/v1/capabilities?q=" + "a" * 1100)[0], 400)
        self.assertEqual(self.get("/v1/capabilities?" + "&".join(f"k{i}=1" for i in range(300)))[0], 400)
        self.assertEqual(self.get("/v1/capabilities?q=" + "a" * 200)[0], 200)

    def test_an_oversized_query_is_refused_before_it_is_parsed(self):
        import capability.service as module
        seen = []
        real = module.parse_qs
        module.parse_qs = lambda raw, **kw: (seen.append(len(raw)), real(raw, **kw))[1]
        try:
            self.assertEqual(self.get("/v1/capabilities?kind=" + "t" * 1100)[0], 400)
            self.assertEqual(seen, [])
            self.assertEqual(self.get("/v1/capabilities?kind=tool")[0], 200)
            self.assertEqual(len(seen), 1)
        finally:
            module.parse_qs = real

    def test_no_error_body_carries_a_path_or_record_text(self):
        for target in ("/v1/capabilities/nothing", "/v1/capabilities/git-log/9.9.9", "/v1/capabilities?bogus=1"):
            body = self.get(target)[2]
            self.assertEqual(sorted(body), ["code", "message"])
            self.assertNotIn("/", body["message"])


class VisibilityTest(unittest.TestCase):
    HIDDEN = {"tool:glob:1.3.0"}

    def setUp(self):
        self.service = CatalogService(Registry(ROOT), DevAuthorizer(hidden=self.HIDDEN))
        self.open = CatalogService(Registry(ROOT), DevAuthorizer())

    def test_hidden_entries_are_omitted_from_list_index_and_search(self):
        names = [e["name"] for e in call(self.service, "GET", "/v1/capabilities")[2]["entries"]]
        self.assertNotIn("glob", names)
        self.assertEqual(len(names), len(GOLDEN["entries"]) - 1)
        self.assertNotIn("glob", [e["name"] for e in call(self.service, "GET", "/v1/index")[2]["entries"]])
        self.assertEqual([e["name"] for e in call(self.service, "GET", "/v1/capabilities?q=glob")[2]["entries"]], ["grep"])  # grep merely mentions glob

    def test_hidden_equals_absent_byte_for_byte_on_every_read_path(self):
        pairs = (("/v1/capabilities/glob", "/v1/capabilities/no-such-tool"),
                 ("/v1/capabilities/glob/1.3.0", "/v1/capabilities/no-such-tool/1.3.0"))
        for hidden, absent in pairs:
            a, b = self.service.handle("GET", hidden, DEV), self.service.handle("GET", absent, DEV)
            self.assertEqual(a, b, hidden)
            self.assertEqual(a[0], 404)
        a = self.service.handle("POST", "/v1/resolve", DEV, b'{"name":"glob"}')
        b = self.service.handle("POST", "/v1/resolve", DEV, b'{"name":"no-such-tool"}')
        self.assertEqual((a, a[0]), (b, 404))

    def test_a_hidden_entry_cannot_be_resolved_below_label_or_through_a_range(self):
        # glob is Tested; hiding it must read as not found, never as BelowLabel
        status, _, body = call(self.service, "POST", "/v1/resolve", body=b'{"name":"glob","minLabel":"Approved"}')
        self.assertEqual((status, body["code"]), (404, "NotFound"))

    def test_below_label_reports_the_best_visible_label_not_a_hidden_one(self):
        newer = copy.deepcopy(next(e for e in GOLDEN["entries"] if e["name"] == "git-log"))
        newer["version"], newer["label"] = "1.3.0", "Declared"
        service = CatalogService(FakeIndexRegistry(ROOT, [newer]), DevAuthorizer(hidden={"tool:git-log:1.3.0"}))
        status, _, body = call(service, "POST", "/v1/resolve", body=b'{"name":"git-log","minLabel":"Approved"}')
        self.assertEqual((status, body["code"], body["have"]), (409, "BelowLabel", "Tested"))

    def test_hidden_kind_does_not_create_ambiguity(self):
        twin = copy.deepcopy(GOLDEN["entries"][0])
        twin["kind"] = "agent"
        registry = FakeIndexRegistry(ROOT, [twin])
        visible_both = CatalogService(registry, DevAuthorizer())
        self.assertEqual(call(visible_both, "GET", "/v1/capabilities/git-log")[0], 409)
        self.assertEqual(call(visible_both, "GET", "/v1/capabilities/git-log/1.2.0")[0], 409)
        self.assertEqual(call(visible_both, "GET", "/v1/capabilities/git-log?kind=tool")[0], 200)
        hidden_agent = CatalogService(registry, DevAuthorizer(hidden={"agent:git-log:1.2.0"}))
        self.assertEqual(call(hidden_agent, "GET", "/v1/capabilities/git-log")[0], 200)
        self.assertEqual(call(hidden_agent, "GET", "/v1/capabilities/git-log/1.2.0")[0], 200)

    def test_registry_visible_predicate_applies_before_every_step(self):
        registry = Registry(ROOT)
        hide_glob = lambda entry: entry["name"] != "glob"
        self.assertNotIn("glob", [row["name"] for row in registry.list(visible=hide_glob)])
        with self.assertRaises(RegistryError) as ctx:
            registry.describe("glob", visible=hide_glob)
        self.assertEqual(ctx.exception.code, "NotFound")
        with self.assertRaises(RegistryError) as ctx:
            registry.resolve("glob", languages=["python"], visible=hide_glob)
        self.assertEqual(ctx.exception.code, "NotFound")
        self.assertEqual(registry.resolve("glob", languages=["python"])["name"], "glob")


class FailClosedTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = new_tree(Path(self._tmp.name))
        add_fixture_tool(self.root, "python")
        manifest = load_manifest(FIXTURES / "valid" / "tool.yaml")
        manifest["implementations"] = [{"language": "python", "location": "library/fixture-tool/python"}]
        write_manifest(self.root, "tool", "fixture-tool", manifest)
        build_index(self.root, fixture_mode=True)

    def tearDown(self):
        self._tmp.cleanup()

    def service(self, registry=None):
        return CatalogService(registry or Registry(self.root), DevAuthorizer())

    def test_a_corrupt_index_is_a_closed_500_for_every_endpoint(self):
        path = self.root / "index" / "index.json"
        path.write_bytes(path.read_bytes() + b" ")
        service = self.service()
        for method, target, body in (("GET", "/v1/capabilities", b""), ("GET", "/v1/index", b""),
                                     ("GET", "/v1/capabilities/fixture-tool", b""),
                                     ("POST", "/v1/resolve", b'{"name":"fixture-tool"}')):
            status, headers, out = call(service, method, target, body=body)
            self.assertEqual((status, out["code"]), (500, "IndexError"), target)
            self.assertNotIn("Retry-After", headers)
            self.assertNotIn(str(self.root), json.dumps(out))

    def test_a_changed_implementation_is_an_integrity_error_without_retry_after(self):
        (self.root / "library" / "fixture-tool" / "python" / "main.py").write_text("changed = 1\n")
        status, headers, out = call(self.service(), "POST", "/v1/resolve", body=b'{"name":"fixture-tool"}')
        self.assertEqual((status, out["code"]), (500, "IntegrityError"))
        self.assertNotIn("Retry-After", headers)

    def revoking_registry(self, fresh=True, revoked=False):
        private, encoded = make_key()
        clocks = Clocks()
        client = RevocationClient(load_public_key(encoded), wall=clocks.wall, monotonic=clocks.mono)
        row = json.loads((self.root / "index" / "index.json").read_bytes())["entries"][0]
        targets = [{"kind": "tool", "digest": row["identity"]}] if revoked else []
        entries = chain(private, len(targets), targets) if targets else []
        if fresh:
            client.ingest(entries)
            head = entry_hash(entries[-1]) if entries else GENESIS
            client.accept_checkpoint(checkpoint(private, 1, len(entries), head, clocks.wall_now))
        return Registry(self.root, revocation=client)

    def test_checked_revoked_and_stale(self):
        status, _, out = call(self.service(self.revoking_registry()), "POST", "/v1/resolve", body=b'{"name":"fixture-tool"}')
        self.assertEqual((status, out["revocation"]), (200, "checked"))
        status, _, out = call(self.service(self.revoking_registry(revoked=True)), "POST", "/v1/resolve", body=b'{"name":"fixture-tool"}')
        self.assertEqual((status, out["code"], sorted(out)), (410, "Revoked", ["code", "message", "reason", "seq"]))
        status, headers, out = call(self.service(self.revoking_registry(fresh=False)), "POST", "/v1/resolve", body=b'{"name":"fixture-tool"}')
        self.assertEqual((status, out["code"], headers["Retry-After"]), (503, "RevocationStale", "30"))


class HttpTest(unittest.TestCase):
    def test_the_socket_layer(self):
        server = make_server(Registry(ROOT), DevAuthorizer())
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            host, port = server.server_address
            self.assertEqual(host, "127.0.0.1")
            conn = http.client.HTTPConnection(host, port, timeout=10)
            conn.request("GET", "/v1/capabilities?q=glob", headers=DEV)
            reply = conn.getresponse()
            self.assertEqual((reply.status, reply.getheader("Content-Type")), (200, "application/json"))
            self.assertEqual(json.loads(reply.read())["entries"][0]["name"], "glob")
            conn.request("GET", "/v1/capabilities")
            reply = conn.getresponse()
            reply.read()
            self.assertEqual(reply.status, 401)
            conn.request("GET", "/v1/capabilities?kind=" + "t" * 5000, headers=DEV)
            reply = conn.getresponse()
            reply.read()
            self.assertEqual(reply.status, 400)
            conn.request("GET", "/v1/capabilities?kind=" + "t" * 70000, headers=DEV)  # beyond the server's own request-line limit
            reply = conn.getresponse()
            reply.read()
            self.assertIn(reply.status, (400, 414))
            conn.close()
            conn = http.client.HTTPConnection(host, port, timeout=10)
            conn.request("POST", "/v1/resolve", body=b"x" * 5000, headers={**DEV, "Content-Length": "5000"})
            reply = conn.getresponse()
            reply.read()
            self.assertEqual(reply.status, 400)
            conn.request("POST", "/v1/resolve", body=b'{"name":"git-log"}', headers=DEV)
            reply = conn.getresponse()
            self.assertEqual(json.loads(reply.read())["name"], "git-log")
        finally:
            server.shutdown()
            server.server_close()


class UntouchedArtifactsTest(unittest.TestCase):
    def test_the_golden_index_still_verifies_against_the_tree(self):
        index = Registry(ROOT).verified_index()
        self.assertEqual(index, GOLDEN)
        self.assertEqual(index["index_digest"], "sha256:2568a039c0e8a006addc56e32715283a5c4f5a5592b3cdd50f2182a78dc967e5")


if __name__ == "__main__":
    unittest.main()
