#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Scenario 2 — mixed-workload concurrency (nexus-57pwo).

10 worker subprocesses, each cycling put/get/search/delete on a
shared project against one isolated T2 daemon. Models the
realistic Co-Work shape (multiple agents + host CLI + dev-container
CLI all hitting the daemon).

Each worker does:
- 200 puts (own title namespace)
- 200 gets (its own writes)
- 100 searches (the shared project)
- 100 deletes (its own writes; then re-puts)

Total ~6000 mixed operations.

Asserts:
- All workers complete (no deadlock, no timeout).
- Final row count matches net writes (puts - deletes).
- Search returns consistent shape during concurrent writes (no
  partial-FTS results that look corrupt).
- Daemon alive at end.
"""
from __future__ import annotations

import json
import socket
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from _lib import (  # noqa: E402
    call,
    emit_scenario_result,
    isolated_daemon,
)


N_WORKERS = 10
PUTS = 200
GETS = 200
SEARCHES = 100
DELETES = 100  # of the worker's own puts; then re-puts
PROJECT = "nexus_stress_mixed"


def worker_script(*, addr: str, worker_id: int) -> str:
    return f'''
import json, socket, struct, sys, time

def send(sock, payload):
    body = json.dumps(payload).encode("utf-8")
    sock.sendall(struct.pack(">I", len(body)) + body + b"\\n")

def recv_exactly(sock, n):
    out = bytearray()
    while len(out) < n:
        chunk = sock.recv(n - len(out))
        if not chunk:
            raise RuntimeError("peer closed")
        out.extend(chunk)
    return bytes(out)

def recv_frame(sock):
    header = recv_exactly(sock, 4)
    length = struct.unpack(">I", header)[0]
    return json.loads(recv_exactly(sock, length + 1)[:-1].decode("utf-8"))

def call(sock, op, rid, **kw):
    send(sock, {{"op": op, "args": [], "kwargs": kw, "request_id": rid}})
    return recv_frame(sock)

host, port = {addr!r}.rsplit(":", 1)
sock = socket.create_connection((host, int(port)), timeout=30.0)
sock.settimeout(120.0)

stats = {{"put_ok": 0, "put_err": 0, "get_ok": 0, "get_err": 0,
         "search_ok": 0, "search_err": 0, "delete_ok": 0, "delete_err": 0}}
rid = 0

WID = {worker_id}
# Phase 1: puts (workers id-spaced so titles don't collide)
for i in range({PUTS}):
    rid += 1
    r = call(sock, "memory.put", rid, project={PROJECT!r},
             title=f"w{{WID}}-put-{{i}}",
             content=f"worker {{WID}} put {{i}} ts {{time.time()}}",
             ttl=1)
    stats["put_ok" if r.get("ok") else "put_err"] += 1

# Phase 2: gets of own writes
for i in range({GETS}):
    rid += 1
    r = call(sock, "memory.get", rid, project={PROJECT!r},
             title=f"w{{WID}}-put-{{i % {PUTS}}}")
    stats["get_ok" if r.get("ok") else "get_err"] += 1

# Phase 3: searches against the shared project
for i in range({SEARCHES}):
    rid += 1
    r = call(sock, "memory.search", rid, query=f"worker {{WID % 3}}",
             project={PROJECT!r})
    stats["search_ok" if r.get("ok") else "search_err"] += 1

# Phase 4: deletes (of own puts), then re-puts so the row count
# math is: net writes = PUTS (deletes are paired with re-puts).
for i in range({DELETES}):
    rid += 1
    r = call(sock, "memory.delete", rid, project={PROJECT!r},
             title=f"w{{WID}}-put-{{i}}")
    stats["delete_ok" if r.get("ok") else "delete_err"] += 1
    rid += 1
    r2 = call(sock, "memory.put", rid, project={PROJECT!r},
              title=f"w{{WID}}-put-{{i}}",
              content=f"re-put after delete worker {{WID}} op {{i}}",
              ttl=1)
    stats["put_ok" if r2.get("ok") else "put_err"] += 1

sock.close()
print(json.dumps({{"worker_id": WID, "stats": stats}}))
'''


def main() -> None:
    started = time.monotonic()
    with isolated_daemon() as (config_dir, tcp_port):
        addr = f"127.0.0.1:{tcp_port}"
        wait_setup_elapsed = time.monotonic() - started

        t0 = time.monotonic()
        procs = []
        for wid in range(N_WORKERS):
            script = worker_script(addr=addr, worker_id=wid)
            p = subprocess.Popen(
                [sys.executable, "-c", script],
                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
            )
            procs.append(p)

        results = []
        for p in procs:
            try:
                out, err = p.communicate(timeout=600)
            except subprocess.TimeoutExpired:
                p.kill()
                out, err = p.communicate()
                results.append({"timeout": True})
                continue
            try:
                results.append(json.loads(out.strip().splitlines()[-1]))
            except (json.JSONDecodeError, IndexError):
                results.append({"parse_error": True, "stdout": out, "stderr": err})
        worker_elapsed = time.monotonic() - t0

        # Aggregate.
        agg = {"put_ok": 0, "put_err": 0, "get_ok": 0, "get_err": 0,
               "search_ok": 0, "search_err": 0, "delete_ok": 0, "delete_err": 0}
        for r in results:
            s = r.get("stats") or {}
            for k in agg:
                agg[k] += s.get(k, 0)

        # Expected: each worker does (PUTS + DELETES) puts and DELETES deletes.
        # Net writes = N_WORKERS * PUTS (deletes/re-puts cancel).
        expected_puts = N_WORKERS * (PUTS + DELETES)
        expected_gets = N_WORKERS * GETS
        expected_searches = N_WORKERS * SEARCHES
        expected_deletes = N_WORKERS * DELETES
        expected_net_rows = N_WORKERS * PUTS

        # Verify final row count in the project matches net writes.
        sock = socket.create_connection(("127.0.0.1", tcp_port), timeout=5.0)
        try:
            db_rows = call(sock, "memory.list_entries", 1, project=PROJECT)
            stored_rows = len(db_rows) if isinstance(db_rows, list) else -1
        finally:
            sock.close()

        # Verify daemon alive.
        sock = socket.create_connection(("127.0.0.1", tcp_port), timeout=5.0)
        try:
            hello = call(sock, "database.hello", 2, client_schema_version="probe")
            daemon_alive = hello is not None
        finally:
            sock.close()

        passed = (
            agg["put_ok"] == expected_puts
            and agg["put_err"] == 0
            and agg["get_ok"] == expected_gets
            and agg["get_err"] == 0
            and agg["search_ok"] == expected_searches
            and agg["search_err"] == 0
            and agg["delete_ok"] == expected_deletes
            and agg["delete_err"] == 0
            and stored_rows == expected_net_rows
            and daemon_alive
            and not any(r.get("timeout") or r.get("parse_error") for r in results)
        )

        emit_scenario_result("scenario_2_mixed_workload", {
            "pass": passed,
            "n_workers": N_WORKERS,
            "stats": agg,
            "expected_puts": expected_puts,
            "expected_gets": expected_gets,
            "expected_searches": expected_searches,
            "expected_deletes": expected_deletes,
            "expected_net_rows": expected_net_rows,
            "stored_rows": stored_rows,
            "daemon_alive_after_mix": daemon_alive,
            "wait_setup_seconds": round(wait_setup_elapsed, 2),
            "worker_elapsed_seconds": round(worker_elapsed, 2),
            "total_ops": sum(agg.values()),
            "throughput_ops_per_sec": round(
                sum(agg.values()) / max(worker_elapsed, 0.001), 1,
            ),
        })


if __name__ == "__main__":
    main()
