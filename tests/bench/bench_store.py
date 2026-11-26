"""S-018 R9 scale benchmark. Not a unit test: it records figures for 07_VERIFICATION.md; it asserts nothing.

Usage (in the test image, from the repository root):
    python tests/bench/bench_store.py OUT_DIR [N]

Builds a SQLiteStore with N signed tool rows (default 100000), then measures, through StoreRegistry:
describe, resolve, a rare-term search, a common-term search, a list page, and searches from 100 concurrent
threads in-process and over HTTP. Prints one JSON object.
"""

import http.client
import json
import os
import random
import statistics
import sys
import threading
import time
from pathlib import Path
from urllib.parse import quote

ROOT = Path(__file__).resolve().parents[2]
sys.path[:0] = [str(ROOT), str(ROOT / "tests")]

from capability.admission import append_signed  # noqa: E402
from capability.authz import DevAdmissionAuthorizer  # noqa: E402
from capability.registry_api import RegistryService, make_server  # noqa: E402
from capability.rows import verify_store  # noqa: E402
from capability.store import SQLiteStore  # noqa: E402
from capability.store_registry import StoreRegistry  # noqa: E402

from registry_support import LANGUAGES, WORD_COUNT, admission_key, fresh_revocation, synthetic_fields  # noqa: E402

VOCAB_SIZE = 2000


def timings(fn, runs):
    out = []
    for _ in range(runs):
        start = time.perf_counter()
        fn()
        out.append((time.perf_counter() - start) * 1000)
    return out


def summary(ms):
    ms = sorted(ms)
    return {"p50_ms": round(statistics.median(ms), 2), "p95_ms": round(ms[int(len(ms) * 0.95) - 1], 2),
            "max_ms": round(ms[-1], 2), "n": len(ms)}


def concurrent(fn, threads=100, each=20):
    latencies, lock = [], threading.Lock()

    def worker():
        local = timings(fn, each)
        with lock:
            latencies.extend(local)

    start = time.perf_counter()
    pool = [threading.Thread(target=worker) for _ in range(threads)]
    for thread in pool:
        thread.start()
    for thread in pool:
        thread.join()
    wall = time.perf_counter() - start
    return {**summary(latencies), "threads": threads, "requests": len(latencies),
            "throughput_per_s": round(len(latencies) / wall, 1)}


def main(out_dir, n):
    rng = random.Random(1)
    vocab = [f"w{i:04d}" for i in range(VOCAB_SIZE)]
    store = SQLiteStore(Path(out_dir) / "data")
    private, public = admission_key()
    code = store.put_code({"main.py": WORD_COUNT.encode()})
    start = time.perf_counter()
    for i in range(n):
        words = " ".join(rng.choice(vocab) for _ in range(8))
        fields = synthetic_fields(f"tool-{i:06d}", summary=f"Synthetic {words} common.",
                                  params={"q": f"Parameter {rng.choice(vocab)}."}, code_digests={"python": code})
        append_signed(store, private, fields)
    insert_s = time.perf_counter() - start
    size_mb = round(sum(p.stat().st_size for p in (Path(out_dir) / "data").glob("registry.sqlite3*")) / 1e6, 1)

    client, _, _ = fresh_revocation()
    registry = StoreRegistry(store, public, client, LANGUAGES)
    target = f"tool-{n // 2:06d}"
    rare = registry.describe(target)["summary"].split()[1]
    result = {
        "rows": n, "insert_s": round(insert_s, 1), "rows_per_s": round(n / insert_s), "db_mb": size_mb,
        "describe": summary(timings(lambda: registry.describe(target), 200)),
        "resolve": summary(timings(lambda: registry.resolve(target, languages=["python"]), 200)),
        "search_rare_term": summary(timings(lambda: registry.search(rare), 200)),
        "search_common_term_top10": summary(timings(lambda: registry.search("common"), 50)),
        "list_page_50": summary(timings(lambda: registry.list(limit=50), 200)),
        "search_in_process_100_threads": concurrent(lambda: registry.search(rare)),
    }

    from capability.admission import Admission
    from registry_support import UnconfinedTestSandbox

    admission = Admission(store, private, UnconfinedTestSandbox(), LANGUAGES, Path(out_dir) / "work")
    service = RegistryService(registry, admission, DevAdmissionAuthorizer(), private)
    server = make_server(service)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    port = server.server_address[1]

    def http_search():
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=60)
        conn.request("GET", "/v2/search?q=" + quote(rare), headers={"X-Dev-Principal": "agent:bench"})
        response = conn.getresponse()
        response.read()
        conn.close()
        assert response.status == 200

    result["search_http_100_clients"] = concurrent(http_search)
    start = time.perf_counter()
    result["verify_store_rows"] = verify_store(store, public)
    result["verify_store_s"] = round(time.perf_counter() - start, 1)
    result["cpu_count"] = os.cpu_count()
    server.shutdown()
    print(json.dumps(result, indent=1))


if __name__ == "__main__":
    main(sys.argv[1], int(sys.argv[2]) if len(sys.argv) > 2 else 100000)
