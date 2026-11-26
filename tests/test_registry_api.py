"""S-018 R5: the /v2 REST service over a real socket (REG-API-1 to 5)."""

import http.client
import json
import tempfile
import threading
import unittest
from pathlib import Path
from urllib.parse import quote

from capability.admission import Admission, Limits
from capability.authz import DevAdmissionAuthorizer
from capability.registry_api import RegistryService, make_server
from capability.rows import verify_checkpoint
from capability.store import SQLiteStore
from capability.store_registry import StoreRegistry

from registry_support import (
    LANGUAGES, UnconfinedTestSandbox, admission_key, bundle_bytes, fresh_revocation, word_count_manifest,
)

AUTHOR, APPROVER, READER = "author:acme", "approver:sec", "agent:reader"


class RegistryApiTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls._tmp = tempfile.TemporaryDirectory()
        base = Path(cls._tmp.name)
        cls.store = SQLiteStore(base / "data")
        cls.private, cls.public = admission_key()
        cls.client, cls.feed, cls.clocks = fresh_revocation()
        cls.registry = StoreRegistry(cls.store, cls.public, cls.client, LANGUAGES)
        admission = Admission(cls.store, cls.private, UnconfinedTestSandbox(), LANGUAGES, base / "work",
                              limits=Limits(max_bundle_bytes=64 * 1024))
        cls.authz = DevAdmissionAuthorizer(approvers={APPROVER}, hidden={"tool:secret-tool:1.0.0"})
        cls.service = RegistryService(cls.registry, admission, cls.authz, cls.private)
        cls.server = make_server(cls.service)
        cls.port = cls.server.server_address[1]
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()
        # Two tools admitted through the API itself: one public, one hidden from every caller.
        for manifest in (word_count_manifest(), word_count_manifest(name="secret-tool", summary="Count secret words.")):
            status, body = cls.call("POST", "/v2/submissions", AUTHOR, bundle_bytes(manifest=manifest))
            assert status == 202, body
            cls.service.drain()
            status, body = cls.call("POST", f"/v2/submissions/{body['id']}/approve", APPROVER)
            assert status == 200 and body["state"] == "admitted", body

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.service.stop()
        cls.store.close()
        cls._tmp.cleanup()

    @classmethod
    def call(cls, method, path, principal=READER, body=None, raw=False, headers=None):
        conn = http.client.HTTPConnection("127.0.0.1", cls.port, timeout=30)
        sent = dict(headers or {})
        if principal is not None:
            sent["X-Dev-Principal"] = principal
        data = body if isinstance(body, (bytes, type(None))) else json.dumps(body).encode()
        conn.request(method, path, body=data, headers=sent)
        response = conn.getresponse()
        payload = response.read()
        conn.close()
        return (response.status, payload) if raw else (response.status, json.loads(payload))

    # identity and routing
    def test_an_unidentified_caller_is_refused(self):
        self.assertEqual(self.call("GET", "/v2/tools", None), (401, {"code": "Unauthenticated",
                                                                     "message": "the caller is not identified"}))

    def test_unknown_paths_and_methods(self):
        self.assertEqual(self.call("GET", "/v2/nothing")[0], 404)
        self.assertEqual(self.call("GET", "/v1/tools")[0], 404)
        self.assertEqual(self.call("DELETE", "/v2/tools")[0], 405)

    # discovery
    def test_search_returns_the_tool_definition(self):
        status, body = self.call("GET", "/v2/search?q=" + quote("count words"))
        self.assertEqual(status, 200)
        self.assertEqual([i["name"] for i in body["items"]], ["word-count"])
        self.assertEqual(body["items"][0]["tool_definition"]["name"], "word-count")

    def test_list_describe_and_versions(self):
        status, body = self.call("GET", "/v2/tools?limit=10")
        self.assertEqual((status, [i["name"] for i in body["items"]]), (200, ["word-count"]))
        status, body = self.call("GET", "/v2/tools/word-count")
        self.assertEqual((status, body["label"], body["source_type"]), (200, "Validated", "submission"))
        self.assertEqual(self.call("GET", "/v2/tools/word-count/versions")[1]["items"][0]["version"], "1.0.0")

    def test_a_hidden_entry_answers_byte_for_byte_like_an_absent_one(self):
        hidden = self.call("GET", "/v2/tools/secret-tool", raw=True)
        absent = self.call("GET", "/v2/tools/never-existed", raw=True)
        self.assertEqual(hidden, absent)
        self.assertEqual(self.call("GET", "/v2/search?q=secret")[1]["items"], [])

    def test_resolve_returns_facts_and_never_a_path(self):
        status, body = self.call("POST", "/v2/resolve", body={"name": "word-count", "languages": ["python"]})
        self.assertEqual(status, 200)
        self.assertEqual(body["revocation"], "checked")
        self.assertNotIn("implementation_path", body)
        self.assertNotIn(self._tmp.name, json.dumps(body))

    def _service(self):
        client, feed, clocks = fresh_revocation()
        registry = StoreRegistry(self.store, self.public, client, LANGUAGES)
        service = RegistryService(registry, self.service._admission, self.authz, self.private, process_inline=True)
        return service, client, feed, clocks

    def _resolve(self, service):
        status, headers, body = service.handle("POST", "/v2/resolve", {"X-Dev-Principal": READER},
                                               json.dumps({"name": "word-count"}).encode())
        return status, headers, json.loads(body)

    def test_a_revoked_version_answers_410_and_stays_revoked_when_knowledge_goes_stale(self):
        service, client, feed, clocks = self._service()
        feed.publish([{"kind": "tool", "name": "word-count", "version": "1.0.0"}])
        client.poll(feed)
        self.assertEqual(self._resolve(service)[2]["code"], "Revoked")
        feed.up = False
        clocks.advance(31)
        self.assertEqual(self._resolve(service)[0], 410)  # a known revocation is never forgotten

    def test_stale_revocation_knowledge_answers_503_with_retry_after(self):
        service, client, feed, clocks = self._service()
        feed.up = False
        clocks.advance(31)
        status, headers, body = self._resolve(service)
        self.assertEqual((status, headers.get("Retry-After"), body["code"]), (503, "30", "RevocationStale"))

    # limits and closed errors
    def test_bad_requests_get_closed_errors(self):
        for path in ("/v2/search", "/v2/search?q=a&q=b", "/v2/search?q=a&bogus=1", "/v2/tools?limit=0",
                     "/v2/tools?limit=abc", "/v2/tools?cursor=%25%25"):
            with self.subTest(path=path):
                status, body = self.call("GET", path)
                self.assertEqual((status, body["code"]), (400, "InvalidRequest"))
        self.assertEqual(self.call("GET", "/v2/search?q=" + "a" * 3000)[0], 413)
        self.assertEqual(self.call("POST", "/v2/resolve", body=b"[1]")[0], 400)

    def test_an_oversized_body_is_refused_before_it_is_read(self):
        status, body = self.call("POST", "/v2/submissions", AUTHOR, b"x" * (64 * 1024 + 1))
        self.assertEqual((status, body["code"]), (413, "TooLarge"))
        status, body = self.call("POST", "/v2/resolve", READER, b"{" + b" " * 5000 + b"}")
        self.assertEqual(status, 413)

    # admission over the API
    def test_submission_visibility_and_the_approval_rules(self):
        status, record = self.call("POST", "/v2/submissions", AUTHOR,
                                   bundle_bytes(manifest=word_count_manifest(version="2.0.0")))
        self.assertEqual((status, record["state"]), (202, "received"))
        self.service.drain()
        sid = record["id"]
        self.assertEqual(self.call("GET", f"/v2/submissions/{sid}", AUTHOR)[1]["state"], "awaiting_approval")
        self.assertEqual(self.call("GET", f"/v2/submissions/{sid}", READER)[0], 404)  # not theirs
        self.assertEqual(self.call("POST", f"/v2/submissions/{sid}/approve", AUTHOR)[0], 403)
        self.assertEqual(self.call("POST", f"/v2/submissions/{sid}/approve", READER)[0], 403)
        status, body = self.call("POST", f"/v2/submissions/{sid}/reject", APPROVER, {"reason": "duplicate"})
        self.assertEqual((status, body["state"]), (200, "rejected"))
        self.assertEqual(self.call("POST", f"/v2/submissions/{sid}/approve", APPROVER)[1]["code"], "InvalidState")
        self.assertEqual(self.call("GET", "/v2/tools/word-count")[1]["version"], "1.0.0")

    def test_a_malformed_submission_is_refused_at_once(self):
        status, body = self.call("POST", "/v2/submissions", AUTHOR, b'{"schema": "nope"}')
        self.assertEqual((status, body["code"]), (400, "InvalidRequest"))

    def test_the_checkpoint_is_signed_and_matches_the_head(self):
        status, body = self.call("GET", "/v2/checkpoint")
        self.assertEqual(status, 200)
        verify_checkpoint(self.public, body)
        self.assertEqual((body["count"], body["head"]), self.store.head())

    def test_concurrent_searches_all_answer(self):
        results = []

        def worker():
            for _ in range(5):
                results.append(self.call("GET", "/v2/search?q=count")[0])

        threads = [threading.Thread(target=worker) for _ in range(20)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertEqual(results, [200] * 100)


if __name__ == "__main__":
    unittest.main()
