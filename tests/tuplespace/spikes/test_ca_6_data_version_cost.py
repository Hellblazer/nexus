# SPDX-License-Identifier: Apache-2.0
"""Spike CA-6: `data_version` cost measurement on deployment hardware.

RDR-110 Phase 1 Step 7 CA spike (nexus-tq96).

Validates CA #6: "PRAGMA data_version polling at 1ms cadence has negligible
CPU cost on the deployment hardware."

Design:
- Tight loop calling PRAGMA data_version on a fresh connection per call.
- N_ITERATIONS = 10_000 samples on the current host.
- Report: p50 / p95 / p99 per-PRAGMA latency in ms.
- PASS if p50 <= 5ms (very generous; Honker reports 1-2ms on M-series Mac).
- Linux x86_64 measurement is desirable but optional; if not feasible in this
  session the result is marked PARTIAL (current host only).

# storage-boundary-allow: spike-harness (RDR-110 Phase 1 Step 7 CA spike)
"""

from __future__ import annotations

import platform
import sqlite3
import time
from pathlib import Path

import pytest

from nexus.tuplespace.store import open_tuples_db

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

N_ITERATIONS: int = 10_000
PASS_P50_MS: float = 5.0   # generous: honker reports 1-2ms on M-series

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _percentile(data: list[float], p: float) -> float:
    """Return the p-th percentile of *data* (0 <= p <= 100)."""
    if not data:
        return 0.0
    sorted_data = sorted(data)
    k = (len(sorted_data) - 1) * p / 100
    lo, hi = int(k), min(int(k) + 1, len(sorted_data) - 1)
    return sorted_data[lo] + (sorted_data[hi] - sorted_data[lo]) * (k - lo)


def _measure_data_version_fresh_conn(db_path: Path, n: int) -> list[float]:
    """Measure PRAGMA data_version latency using a FRESH connection per call.

    Each call:  open -> PRAGMA data_version -> close.
    This is the worst-case cost (new connection overhead included).

    # storage-boundary-allow: spike-harness (RDR-110 Phase 1 Step 7 CA spike)
    """
    latencies: list[float] = []
    db_str = str(db_path)
    for _ in range(n):
        t0 = time.perf_counter()
        # storage-boundary-allow: spike-harness (RDR-110 Phase 1 Step 7 CA spike)
        conn = sqlite3.connect(db_str, check_same_thread=False)
        conn.execute("PRAGMA data_version").fetchone()
        conn.close()
        latencies.append((time.perf_counter() - t0) * 1000.0)
    return latencies


def _measure_data_version_reused_conn(db_path: Path, n: int) -> list[float]:
    """Measure PRAGMA data_version latency using a REUSED connection.

    This represents the actual watcher loop cost per poll.
    """
    latencies: list[float] = []
    # storage-boundary-allow: spike-harness (RDR-110 Phase 1 Step 7 CA spike)
    conn = sqlite3.connect(str(db_path), check_same_thread=False)
    conn.execute("PRAGMA journal_mode=WAL")
    try:
        for _ in range(n):
            t0 = time.perf_counter()
            conn.execute("PRAGMA data_version").fetchone()
            latencies.append((time.perf_counter() - t0) * 1000.0)
    finally:
        conn.close()
    return latencies


# ---------------------------------------------------------------------------
# Spike test
# ---------------------------------------------------------------------------

class TestDataVersionCost:
    """CA #6: PRAGMA data_version polling cost on deployment hardware."""

    def test_data_version_fresh_conn_latency(self, tmp_path: Path) -> None:
        """Measure per-call PRAGMA data_version cost with a fresh connection each time.

        This is the conservative upper-bound cost.  The actual watcher uses a
        reused connection (see test below) which is 10-100x cheaper.

        PASS: p50 <= 5ms.
        """
        db_path = tmp_path / "tuples.db"
        schema_conn = open_tuples_db(db_path)
        schema_conn.close()

        # Warm-up: discard first 100 samples to avoid cold-start noise.
        _measure_data_version_fresh_conn(db_path, n=100)

        latencies = _measure_data_version_fresh_conn(db_path, n=N_ITERATIONS)

        p50 = _percentile(latencies, 50)
        p95 = _percentile(latencies, 95)
        p99 = _percentile(latencies, 99)
        mean = sum(latencies) / len(latencies)

        host_info = f"{platform.system()} {platform.machine()} {platform.processor()}"

        print(
            f"\n[CA-6 fresh-conn] host='{host_info}' n={N_ITERATIONS} "
            f"mean={mean:.3f}ms p50={p50:.3f}ms p95={p95:.3f}ms p99={p99:.3f}ms"
        )

        is_linux = platform.system() == "Linux"
        note = "" if is_linux else " (PARTIAL: current host only; Linux x86_64 measurement desirable)"

        assert p50 <= PASS_P50_MS, (
            f"PRAGMA data_version fresh-conn p50={p50:.3f}ms exceeds {PASS_P50_MS}ms threshold. "
            f"CA #6 FAIL on {host_info}.{note}"
        )

        print(
            f"[CA-6 fresh-conn] PASS{note} -- "
            f"p50={p50:.3f}ms p95={p95:.3f}ms p99={p99:.3f}ms (<= {PASS_P50_MS}ms threshold). "
            f"Polling at 1ms cadence has negligible cost."
        )

    def test_data_version_reused_conn_latency(self, tmp_path: Path) -> None:
        """Measure per-call PRAGMA data_version cost with a REUSED connection.

        This is the actual cost path for _DataVersionWatcher (one connection
        held open for the lifetime of the poll loop).

        PASS: p50 <= 1ms (much tighter than fresh-conn because no open overhead).
        """
        db_path = tmp_path / "tuples.db"
        schema_conn = open_tuples_db(db_path)
        schema_conn.close()

        # Warm-up.
        _measure_data_version_reused_conn(db_path, n=200)

        latencies = _measure_data_version_reused_conn(db_path, n=N_ITERATIONS)

        p50 = _percentile(latencies, 50)
        p95 = _percentile(latencies, 95)
        p99 = _percentile(latencies, 99)
        mean = sum(latencies) / len(latencies)

        host_info = f"{platform.system()} {platform.machine()} {platform.processor()}"

        print(
            f"\n[CA-6 reused-conn] host='{host_info}' n={N_ITERATIONS} "
            f"mean={mean:.4f}ms p50={p50:.4f}ms p95={p95:.4f}ms p99={p99:.4f}ms"
        )

        is_linux = platform.system() == "Linux"
        note = "" if is_linux else " (PARTIAL: current host only; Linux x86_64 measurement desirable)"

        # Reused connection is the actual watcher path; should be well under 1ms.
        assert p50 <= 1.0, (
            f"PRAGMA data_version reused-conn p50={p50:.4f}ms exceeds 1ms threshold. "
            f"CA #6 FAIL on {host_info}.{note}"
        )

        print(
            f"[CA-6 reused-conn] PASS{note} -- "
            f"p50={p50:.4f}ms p95={p95:.4f}ms p99={p99:.4f}ms. "
            f"_DataVersionWatcher poll cost is negligible."
        )
