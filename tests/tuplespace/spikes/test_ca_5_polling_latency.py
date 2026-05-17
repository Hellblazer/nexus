# SPDX-License-Identifier: Apache-2.0
"""Spike CA-5: Polling-latency observation, 10-worker work queue.

RDR-110 Phase 1 Step 7 CA spike (nexus-tq96).

Validates CA #5: "Polling-based take (default block=False) is workable for v1
agentic patterns; blocking take (block=True) on the data_version wake mechanism
delivers ~1-2ms median cross-process wake latency."

Design:
- block=False path: pre-populate 100 tuples, then race N_CONSUMERS concurrent
  pollers to drain the queue.  Measures per-take() latency from attempt to
  success, and overall drain throughput.  Uses autocommit connections
  (isolation_level=None) to avoid Python sqlite3 implicit-BEGIN lock contention.
- block=True proxy path: _DataVersionWatcher data_version detection latency.
  block=True is feature-flagged OFF in take() (RDR-112 §A2).  We instantiate
  _DataVersionWatcher directly and time commit -> wake_event latency using raw
  SQLite writes, bypassing Chroma embed overhead.
  # storage-boundary-allow: spike-harness (RDR-110 Phase 1 Step 7 CA spike)
- Report: p50 / p95 / p99 for both paths.
"""

from __future__ import annotations

import hashlib
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any

import chromadb
import pytest

from nexus.tuplespace.api import out, take, ack
from nexus.tuplespace.index import TupleIndex
from nexus.tuplespace.registry import Registry
from nexus.tuplespace.store import open_tuples_db
from nexus.tuplespace.watcher import _DataVersionWatcher

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

N_CONSUMERS: int = 10
N_TUPLES: int = 100
POLL_SLEEP_S: float = 0.001   # 1 ms between poll attempts (block=False path)
TIMEOUT_S: float = 60.0

_TASKS_YAML = """
name: tasks/<project>
tier: project
content_type: text
embed_from: content
dimensions:
  status:     { type: enum, values: [open, in_progress, done, cancelled], required: true }
  priority:   { type: enum, values: [P0, P1, P2, P3, P4], required: true }
  created_by: { type: string, required: true }
take:
  enabled: true
  mode: semantic
  floor: 0.0
  margin: 0.0
  default_lease_seconds: 60
read:
  default_floor: 0.0
  default_n: 5
tiers: [project]
retention_seconds: 86400
"""

# Exact-mode subspace for block=False polling test (bypasses Chroma embed).
# Chroma embed (~300ms/call) would dominate latency and mask the SQLite polling
# mechanism under test.  Exact mode exercises the same SQLite CAS UPDATE path.
_LOCKS_YAML = """
name: locks/<resource>
tier: project
content_type: text
embed_from: content
dimensions:
  resource: { type: string, required: true }
  holder:   { type: string, required: true }
take:
  enabled: true
  mode: exact
  match_keys: [resource]
  default_lease_seconds: 60
read:
  default_floor: 0.0
  default_n: 1
tiers: [project]
retention_seconds: 3600
"""

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture()
def builtin_dir(tmp_path: Path) -> Path:
    d = tmp_path / "builtin"
    d.mkdir()
    (d / "tasks.yml").write_text(_TASKS_YAML)
    (d / "locks.yml").write_text(_LOCKS_YAML)
    return d


@pytest.fixture()
def registry(builtin_dir: Path) -> Registry:
    return Registry.load(builtin_dir)


@pytest.fixture()
def db_path(tmp_path: Path) -> Path:
    return tmp_path / "tuples.db"


@pytest.fixture()
def chroma_client() -> chromadb.EphemeralClient:
    client = chromadb.EphemeralClient()
    for coll in client.list_collections():
        client.delete_collection(coll.name)
    yield client
    for coll in client.list_collections():
        client.delete_collection(coll.name)


@pytest.fixture()
def index(registry: Registry, chroma_client: chromadb.EphemeralClient) -> TupleIndex:
    return TupleIndex.from_registry(registry, chroma_client)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _open_conn(db_path: Path) -> sqlite3.Connection:
    """Open a WAL connection in autocommit mode (isolation_level=None).

    Autocommit avoids Python sqlite3 implicit BEGIN transactions that can
    cause 'database is locked' errors when multiple connections are open
    under WAL with concurrent DML.
    """
    conn = sqlite3.connect(str(db_path), check_same_thread=False, isolation_level=None)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=10000")
    return conn


def _task_dims() -> dict[str, Any]:
    return {"status": "open", "priority": "P1", "created_by": "producer"}


def _percentile(data: list[float], p: float) -> float:
    if not data:
        return 0.0
    s = sorted(data)
    k = (len(s) - 1) * p / 100
    lo, hi = int(k), min(int(k) + 1, len(s) - 1)
    return s[lo] + (s[hi] - s[lo]) * (k - lo)


# ---------------------------------------------------------------------------
# Spike tests
# ---------------------------------------------------------------------------

class TestPollingLatency:
    """CA #5: polling latency for block=False and data_version block=True paths."""

    def test_block_false_poll_latency(
        self,
        db_path: Path,
        index: TupleIndex,
        registry: Registry,
    ) -> None:
        """Measure N_CONSUMERS-concurrent take() throughput with block=False polling.

        Uses exact mode (locks/<resource>) to bypass Chroma embed overhead and
        isolate the SQLite polling mechanism.  In production, semantic-mode
        take() adds ~30-400ms per call depending on the embed model; the
        block=False path in CA-5 is about the SQLite claim mechanism, not the
        embed latency (which is a separate tuning concern).

        Pre-populate N_TUPLES unique locks, then race N_CONSUMERS concurrent
        pollers to drain the queue round-robin.  Each poller takes one lock
        at a time and immediately acks it.
        """
        # N consumers each claim their "own" slice of locks round-robin.
        # Each lock has a unique resource key so exact-mode matching is 1:1.
        LOCKS_PER_CONSUMER = N_TUPLES // N_CONSUMERS

        setup_conn = open_tuples_db(db_path)
        # Insert N_CONSUMERS * LOCKS_PER_CONSUMER locks, partitioned by resource.
        for c in range(N_CONSUMERS):
            for j in range(LOCKS_PER_CONSUMER):
                resource = f"q-{c}-item-{j:03d}"
                out(
                    conn=setup_conn,
                    index=index,
                    registry=registry,
                    subspace=f"locks/{resource}",
                    content=f"work lock {resource}",
                    dimensions={"resource": resource, "holder": "pending"},
                )
        setup_conn.close()

        success_times: list[float] = []
        attempt_times: list[float] = []
        success_lock = threading.Lock()
        errors: list[Exception] = []
        start_event = threading.Event()

        def consumer_fn(worker_idx: int) -> None:
            conn = _open_conn(db_path)
            try:
                start_event.wait()
                deadline = time.perf_counter() + TIMEOUT_S
                for j in range(LOCKS_PER_CONSUMER):
                    resource = f"q-{worker_idx}-item-{j:03d}"
                    # Poll until we claim this specific lock.
                    while time.perf_counter() < deadline:
                        t_attempt = time.perf_counter()
                        result = take(
                            conn=conn,
                            index=index,
                            registry=registry,
                            subspace=f"locks/{resource}",
                            query="",
                            claimant=f"consumer-{worker_idx}",
                            where={"resource": resource},
                        )
                        if result is not None:
                            t_success = time.perf_counter()
                            _t_dict, claim_id = result
                            ack(conn=conn, claim_id=claim_id, claimant=f"consumer-{worker_idx}")
                            with success_lock:
                                success_times.append(t_success)
                                attempt_times.append(t_attempt)
                            break
                        time.sleep(POLL_SLEEP_S)
            except Exception as exc:
                errors.append(exc)
            finally:
                conn.close()

        consumers = [
            threading.Thread(target=consumer_fn, args=(i,), name=f"cons-{i}", daemon=True)
            for i in range(N_CONSUMERS)
        ]
        for t in consumers:
            t.start()

        t_start = time.perf_counter()
        start_event.set()

        for t in consumers:
            t.join(timeout=TIMEOUT_S)

        t_end = time.perf_counter()

        if errors:
            raise errors[0]

        consumed = len(success_times)
        total_elapsed_ms = (t_end - t_start) * 1000.0
        throughput = consumed / (t_end - t_start) if t_end > t_start else 0.0

        take_latencies_ms = [
            (success_times[i] - attempt_times[i]) * 1000.0
            for i in range(len(success_times))
        ]

        p50 = _percentile(take_latencies_ms, 50) if take_latencies_ms else 0.0
        p95 = _percentile(take_latencies_ms, 95) if take_latencies_ms else 0.0
        p99 = _percentile(take_latencies_ms, 99) if take_latencies_ms else 0.0

        print(
            f"\n[CA-5 block=False exact-mode] "
            f"consumed={consumed}/{N_TUPLES} "
            f"total_elapsed={total_elapsed_ms:.0f}ms "
            f"throughput={throughput:.1f} takes/sec"
        )
        print(f"  per-take p50={p50:.3f}ms p95={p95:.3f}ms p99={p99:.3f}ms")
        print(
            f"  Note: semantic-mode take() latency also includes Chroma embed (~30-400ms "
            f"depending on model). This test isolates the SQLite claim path."
        )

        assert consumed >= N_TUPLES * 0.95, (
            f"Less than 95% of tuples consumed: {consumed}/{N_TUPLES}. "
            "Polling mechanism may be broken."
        )
        print(
            f"[CA-5 block=False] PASS -- {consumed}/{N_TUPLES} tuples claimed by "
            f"{N_CONSUMERS} concurrent pollers (exact mode, SQLite path). "
            f"p50={p50:.3f}ms p95={p95:.3f}ms p99={p99:.3f}ms"
        )

    def test_block_true_data_version_wake_latency(
        self,
        db_path: Path,
        index: TupleIndex,
        registry: Registry,
    ) -> None:
        """Measure _DataVersionWatcher data_version detection latency (block=True proxy).

        block=True is feature-flagged OFF in take() (RDR-112 §A2).  We instantiate
        _DataVersionWatcher directly and time commit -> wake_event latency using
        raw SQLite writes (no Chroma embed overhead).

        # storage-boundary-allow: spike-harness (RDR-110 Phase 1 Step 7 CA spike)
        """
        init_conn = open_tuples_db(db_path)
        init_conn.close()

        wake_event = threading.Event()
        watcher = _DataVersionWatcher(db_path=db_path, wake_event=wake_event)
        watcher.start()

        # storage-boundary-allow: spike-harness (RDR-110 Phase 1 Step 7 CA spike)
        writer_conn = sqlite3.connect(str(db_path), check_same_thread=False, isolation_level=None)
        writer_conn.execute("PRAGMA journal_mode=WAL")

        latencies_ms: list[float] = []
        N_SAMPLES = 100

        try:
            for i in range(N_SAMPLES):
                wake_event.clear()

                tid = hashlib.sha256(f"watcher-test-{i}".encode()).hexdigest()[:32]
                now = time.time()
                writer_conn.execute(
                    "INSERT OR IGNORE INTO tuples "
                    "(id, subspace, template_name, content, dimensions_json, embed_text, created_at) "
                    "VALUES (?, 'tasks/nexus', 'tasks/<project>', ?, '{}', ?, ?)",
                    (tid, f"watcher-test-{i}", f"watcher-test-{i}", now),
                )
                # With isolation_level=None, INSERT auto-commits. data_version increments here.

                t_before = time.perf_counter()
                # Force a commit to ensure data_version increments even in edge cases.
                writer_conn.execute("PRAGMA wal_checkpoint(PASSIVE)")

                fired = wake_event.wait(timeout=0.05)
                t_after = time.perf_counter()

                latencies_ms.append((t_after - t_before) * 1000.0 if fired else 50.0)

        finally:
            watcher.stop()
            writer_conn.close()

        detected = sum(1 for lt in latencies_ms if lt < 50.0)
        p50 = _percentile(latencies_ms, 50)
        p95 = _percentile(latencies_ms, 95)
        p99 = _percentile(latencies_ms, 99)

        print(
            f"\n[CA-5 block=True / watcher] "
            f"samples={N_SAMPLES} detected={detected}/{N_SAMPLES} "
            f"p50={p50:.3f}ms p95={p95:.3f}ms p99={p99:.3f}ms"
        )

        assert detected >= N_SAMPLES * 0.90, (
            f"Watcher fired for only {detected}/{N_SAMPLES} commits (<90%). "
            "data_version detection mechanism may be broken."
        )
        assert p50 <= 5.0, (
            f"block=True wake latency p50={p50:.3f}ms exceeds 5ms threshold. "
            "CA #5 FAIL -- watcher poll cadence may be too slow."
        )
        print(
            f"[CA-5 block=True proxy] PASS -- "
            f"p50={p50:.3f}ms p95={p95:.3f}ms p99={p99:.3f}ms (threshold: p50<=5ms)."
        )
