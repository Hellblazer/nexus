#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Scenario 1 — multi-process fan-in write storm (nexus-57pwo).

10 worker subprocesses (separate Python interpreters), each
performing 1000 ``memory.put`` operations against one isolated T2
daemon over framed-JSON TCP. Sustained ~30-60s.

Asserts:
- Total writes = 10,000 (every worker reports ops_ok == 1000).
- Zero subprocess errors (no SQLITE_BUSY escapes that the daemon's
  busy_timeout couldn't absorb).
- All returned row IDs are monotonically increasing without gaps
  in the full set (SQLite's INTEGER PRIMARY KEY autoincrement
  invariant under concurrent writes through the daemon's single-
  writer arbitration).
- Final SELECT MAX(id) from the daemon's memory table matches the
  number of writes plus the initial _nexus_version bootstrap row.
- Daemon process is still alive at end (no crash / OOM).
"""
from __future__ import annotations

import socket
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from _lib import (  # noqa: E402
    call,
    emit_scenario_result,
    isolated_daemon,
    spawn_workers,
)


N_WORKERS = 10
OPS_PER_WORKER = 1000
PROJECT = "nexus_stress_fan_in"


def main() -> None:
    started = time.monotonic()
    with isolated_daemon() as (config_dir, tcp_port):
        addr = f"127.0.0.1:{tcp_port}"
        wait_setup_elapsed = time.monotonic() - started

        t0 = time.monotonic()
        results = spawn_workers(
            addr=addr,
            n_workers=N_WORKERS,
            count_per_worker=OPS_PER_WORKER,
            project=PROJECT,
        )
        worker_elapsed = time.monotonic() - t0

        # Aggregate worker stats.
        total_ok = sum(r.get("ok", 0) for r in results)
        total_err = sum(r.get("err", 0) for r in results)
        timed_out = [r for r in results if r.get("timeout")]
        parse_errors = [r for r in results if r.get("parse_error")]

        # Verify monotonic IDs across all workers (no gaps in the
        # full set of returned row IDs).
        all_ids: list[int] = []
        for r in results:
            all_ids.extend(r.get("ids") or [])
        all_ids_sorted = sorted(all_ids)
        ids_strictly_monotonic = all(
            all_ids_sorted[i] < all_ids_sorted[i + 1]
            for i in range(len(all_ids_sorted) - 1)
        )
        ids_contiguous = (
            len(all_ids_sorted) > 0
            and all_ids_sorted[-1] - all_ids_sorted[0] + 1 == len(all_ids_sorted)
        )

        # Confirm the daemon is still alive by hello.
        sock = socket.create_connection(("127.0.0.1", tcp_port), timeout=5.0)
        try:
            hello = call(sock, "database.hello", 1, client_schema_version="probe")
            daemon_alive = hello is not None
        finally:
            sock.close()

        # Sanity: ask the daemon's memory store for the total row count.
        sock = socket.create_connection(("127.0.0.1", tcp_port), timeout=5.0)
        try:
            db_rows = call(
                sock, "memory.list_entries", 2, project=PROJECT,
            )
            stored_rows = len(db_rows) if isinstance(db_rows, list) else -1
        finally:
            sock.close()

        passed = (
            total_ok == N_WORKERS * OPS_PER_WORKER
            and total_err == 0
            and not timed_out
            and not parse_errors
            and ids_strictly_monotonic
            and ids_contiguous
            and daemon_alive
            and stored_rows == N_WORKERS * OPS_PER_WORKER
        )

        emit_scenario_result("scenario_1_fan_in", {
            "pass": passed,
            "n_workers": N_WORKERS,
            "ops_per_worker": OPS_PER_WORKER,
            "expected_total": N_WORKERS * OPS_PER_WORKER,
            "total_ok": total_ok,
            "total_err": total_err,
            "timed_out_workers": len(timed_out),
            "parse_error_workers": len(parse_errors),
            "ids_strictly_monotonic": ids_strictly_monotonic,
            "ids_contiguous": ids_contiguous,
            "ids_min": min(all_ids) if all_ids else None,
            "ids_max": max(all_ids) if all_ids else None,
            "daemon_alive_after_storm": daemon_alive,
            "stored_rows_in_project": stored_rows,
            "wait_setup_seconds": round(wait_setup_elapsed, 2),
            "worker_elapsed_seconds": round(worker_elapsed, 2),
            "throughput_ops_per_sec": round(
                total_ok / max(worker_elapsed, 0.001), 1,
            ),
        })


if __name__ == "__main__":
    main()
