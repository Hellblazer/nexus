# SPDX-License-Identifier: AGPL-3.0-or-later
"""GC A/B load driver: deterministic concurrent T2 load against a running
nexus-service, measuring latency percentiles / throughput / 5xx counts.

Op mix per iteration (the Java-heap-allocating path: HTTP -> Jackson ->
jOOQ -> Postgres): memory/put (INSERT), memory/get (SELECT),
memory/search (FTS). Fixed iteration budget per worker — never
wall-clock-paced (nexus-y92yf: wall-clock loops couple volume to host
speed and manufacture flaky timing signatures).

Usage: load_driver.py BASE_URL TOKEN WORKERS ITERATIONS
Emits a single JSON line on stdout: percentiles (ms) per op, totals.
"""
from __future__ import annotations

import concurrent.futures
import json
import statistics
import sys
import time
import urllib.error
import urllib.request

BASE, TOKEN = sys.argv[1], sys.argv[2]
WORKERS, ITERS = int(sys.argv[3]), int(sys.argv[4])

HDRS = {"Authorization": f"Bearer {TOKEN}", "Content-Type": "application/json"}


def _call(method: str, path: str, body: dict | None) -> tuple[int, float]:
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(BASE + path, data=data, headers=HDRS, method=method)
    t0 = time.perf_counter()
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            resp.read()
            return resp.status, (time.perf_counter() - t0) * 1000
    except urllib.error.HTTPError as e:
        e.read()
        return e.code, (time.perf_counter() - t0) * 1000


def worker(wid: int) -> dict[str, list]:
    lat: dict[str, list[float]] = {"put": [], "get": [], "search": []}
    errs = 0
    for i in range(ITERS):
        # Distinct titles keep INSERT volume real (no upsert-collapse to one row).
        title = f"w{wid}-i{i}"
        content = f"gc ab load row {wid} {i} " + "payload " * 40  # ~360B docs
        for op, method, path, body in (
            ("put", "POST", "/v1/memory/put",
             {"project": "gcab", "title": title, "content": content,
              "tags": "gcab", "ttl": 1}),
            ("get", "GET", f"/v1/memory/get?project=gcab&title={title}", None),
            ("search", "POST", "/v1/memory/search",
             {"query": f"load row {wid}", "project": "gcab"}),
        ):
            code, ms = _call(method, path, body)
            if code >= 500:
                errs += 1
            else:
                lat[op].append(ms)
    return {"lat": lat, "errs": errs}


def pct(xs: list[float], p: float) -> float:
    return round(statistics.quantiles(xs, n=100)[int(p) - 1], 2) if len(xs) >= 2 else -1


t_start = time.perf_counter()
with concurrent.futures.ThreadPoolExecutor(max_workers=WORKERS) as ex:
    results = list(ex.map(worker, range(WORKERS)))
wall = time.perf_counter() - t_start

out: dict = {
    "workers": WORKERS, "iterations": ITERS,
    "total_requests": WORKERS * ITERS * 3,
    "wall_s": round(wall, 2),
    "rps": round(WORKERS * ITERS * 3 / wall, 1),
    "server_5xx": sum(r["errs"] for r in results),
}
for op in ("put", "get", "search"):
    xs = [ms for r in results for ms in r["lat"][op]]
    out[op] = {"n": len(xs), "p50": pct(xs, 50), "p95": pct(xs, 95),
               "p99": pct(xs, 99), "max": round(max(xs), 2) if xs else -1}
print(json.dumps(out))
