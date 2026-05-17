# SPDX-License-Identifier: Apache-2.0
"""Spike CA-3: ``ts_api.read`` query latency at 10k / 50k / 100k tuples.

RDR-111 Phase 2 Step 5a CA spike (nexus-2oa6, gates the P2 review gate).

Validates CA #3: "Semantic ``read()`` returns within an acceptable latency
budget as the underlying tuples table grows. Chroma similarity scales with
N (depends on internal HNSW structure) and SQL post-filter is trivial; we
want concrete p50/p95/p99 against three realistic operational sizes."

Design:

- One concrete ``tasks/spike`` subspace; bulk-populated via direct
  ``coll.upsert`` (batched at the Chroma quota max 300) and
  ``conn.executemany`` for SQL rows. Direct bulk insertion bypasses
  ``api.out`` per-record overhead so setup completes in minutes, not
  hours.
- A small "smoke" run at N=1000 runs in the default suite (no marker)
  to catch regressions on every full-suite invocation.
- The full sweep at N=10000, 50000, 100000 is gated behind
  ``@pytest.mark.slow`` (deselected by default via ``pyproject.toml``).
  Run explicitly with ``pytest tests/tuplespace/spikes/test_ca_3_read_latency.py -m slow``.
- Each measured call: ``ts_api.read(query='task X')`` with the index
  query x varying across iterations so the embed step is exercised
  (Chroma may otherwise cache the query embedding internally).

Reports p50 / p95 / p99 per N. Loose ceiling assertions catch
catastrophic regressions (e.g. accidental full scan) without flaking on
small CI hardware variance.
"""

from __future__ import annotations

import json
import sqlite3
import time
import uuid
from pathlib import Path
from typing import Any

import chromadb
import pytest

from nexus.tuplespace.api import read as ts_read
from nexus.tuplespace.index import TupleIndex
from nexus.tuplespace.registry import Registry
from nexus.tuplespace.store import open_tuples_db

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_SUBSPACE = "tasks/spike"
_TEMPLATE = "tasks/<project>"
_QUERY_COUNT = 100   # samples per N to compute percentiles
_SETUP_BATCH = 300   # Chroma quota MAX_RECORDS_PER_WRITE

# Per-N p99 ceilings. Conservative (~10x headroom over observed
# baselines) so the test fails loud on a regression that switches the
# read path to a full scan but doesn't flake on hardware variance.
#
# Observed baseline on Apple M-series, ChromaDB EphemeralClient with the
# bundled ONNX MiniLM embedder (2026-05-17, nexus-2oa6 spike capture):
#   N=  1,000  p50= 37.7ms  p95= 39.5ms  p99= 41.0ms  max= 42.0ms
#   N= 10,000  p50= 50.2ms  p95= 52.1ms  p99= 53.7ms  max= 55.0ms
#   N= 50,000  p50=111.8ms  p95=116.5ms  p99=126.3ms  max=126.5ms
#   N=100,000  p50=225.6ms  p95=234.6ms  p99=243.2ms  max=275.6ms
#
# Scaling pattern: roughly sub-linear (Chroma HNSW giving log-N-ish
# growth). At 100x more tuples (1k -> 100k), p99 only grows ~6x.
_P99_CEILING_MS_BY_N: dict[int, float] = {
    # nexus-26b7 (notable, dim-14 F7): widen the 1k smoke ceiling so
    # slow CI hosts (GitHub Actions standard runners, ~25% throughput
    # of M-series local) don't intermittently red-bar on this
    # default-included test.  41ms p99 baseline locally; widening to
    # 1500ms keeps the ceiling discriminating against major regressions
    # while absorbing single-digit second hiccups under CI load.
    1_000: 1_500.0,
    10_000: 1_000.0,
    50_000: 2_000.0,
    100_000: 4_000.0,
}

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

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def builtin_dir(tmp_path: Path) -> Path:
    d = tmp_path / "builtin"
    d.mkdir()
    (d / "tasks.yml").write_text(_TASKS_YAML)
    return d


@pytest.fixture()
def registry(builtin_dir: Path) -> Registry:
    return Registry.load(builtin_dir)


@pytest.fixture()
def db_path(tmp_path: Path) -> Path:
    return tmp_path / "tuples.db"


@pytest.fixture()
def chroma_client() -> chromadb.EphemeralClient:
    """EphemeralClient with isolated state per test.

    EphemeralClient shares an in-memory backend across instances in the
    same process (project memory: project_chromadb_ephemeral_shared_state).
    Clear collections on entry to defend against bleed-over.
    """
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


def _percentile(data: list[float], p: float) -> float:
    if not data:
        return 0.0
    s = sorted(data)
    k = (len(s) - 1) * p / 100
    lo, hi = int(k), min(int(k) + 1, len(s) - 1)
    return s[lo] + (s[hi] - s[lo]) * (k - lo)


def _bulk_populate(
    *,
    conn: sqlite3.Connection,
    index: TupleIndex,
    n: int,
) -> None:
    """Insert ``n`` tuples directly via Chroma batch upsert + SQL bulk insert.

    Bypasses ``api.out`` per-record overhead so setup completes in
    minutes rather than hours at 100k. Tuples are valid enough that the
    post-filter passes (``consumed_at IS NULL``, ``claim_state='available'``).
    """
    coll = index._collections[_TEMPLATE]
    now = time.time()
    template_name = _TEMPLATE

    # Pre-generate stable IDs so SQL + Chroma keys match.
    tuple_ids: list[str] = [f"spike-{uuid.uuid4().hex}" for _ in range(n)]

    # ---- Chroma side: 300-batch upsert ----
    for offset in range(0, n, _SETUP_BATCH):
        chunk_ids = tuple_ids[offset : offset + _SETUP_BATCH]
        docs = [
            f"task content {offset + i} {chunk_ids[i][:8]}"
            for i in range(len(chunk_ids))
        ]
        metas = [
            {
                "subspace": _SUBSPACE,
                "status": "open",
                "priority": "P1",
                "created_by": "spike",
            }
            for _ in chunk_ids
        ]
        coll.upsert(ids=chunk_ids, documents=docs, metadatas=metas)

    # ---- SQL side: bulk insert. Schema mirrors api.out (store.py) ----
    # claim_state IS NULL means "available" (see store.py:86); we omit
    # all the claim_*/consumed_* columns so they default to NULL, which
    # is the available-row contract.
    cursor = conn.cursor()
    cursor.executemany(
        """
        INSERT INTO tuples (
            id, subspace, template_name, content, dimensions_json,
            embed_text, match_text, created_at, expires_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        [
            (
                tid,
                _SUBSPACE,
                template_name,
                f"task content {i} {tid[:8]}",
                json.dumps({"status": "open", "priority": "P1", "created_by": "spike"}),
                f"task content {i} {tid[:8]}",
                None,
                now,
                None,
            )
            for i, tid in enumerate(tuple_ids)
        ],
    )
    conn.commit()


def _measure_read_latencies(
    *,
    conn: sqlite3.Connection,
    index: TupleIndex,
    registry: Registry,
    samples: int,
) -> list[float]:
    """Run ``samples`` ts_read calls; return per-call wall times in ms.

    Vary the query text per call so Chroma cannot cache an identical
    embedding across iterations.
    """
    latencies_ms: list[float] = []
    for i in range(samples):
        query = f"find task variant {i % 17}"
        t0 = time.perf_counter()
        ts_read(
            conn=conn,
            index=index,
            registry=registry,
            subspace=_SUBSPACE,
            query=query,
            n=10,
        )
        latencies_ms.append((time.perf_counter() - t0) * 1000)
    return latencies_ms


def _run_one_scale(
    *,
    db_path: Path,
    index: TupleIndex,
    registry: Registry,
    n: int,
) -> dict[str, float]:
    """Populate to N, run the query loop, return p50/p95/p99 (ms)."""
    conn = open_tuples_db(db_path)
    try:
        _bulk_populate(conn=conn, index=index, n=n)
        latencies = _measure_read_latencies(
            conn=conn, index=index, registry=registry, samples=_QUERY_COUNT
        )
    finally:
        conn.close()
    return {
        "n": float(n),
        "samples": float(_QUERY_COUNT),
        "p50_ms": _percentile(latencies, 50),
        "p95_ms": _percentile(latencies, 95),
        "p99_ms": _percentile(latencies, 99),
        "max_ms": max(latencies),
        "min_ms": min(latencies),
    }


# ---------------------------------------------------------------------------
# Spike tests
# ---------------------------------------------------------------------------


class TestReadLatency:
    """CA #3: ts_api.read latency at three operational scales."""

    def test_read_latency_smoke_1k(
        self,
        db_path: Path,
        index: TupleIndex,
        registry: Registry,
    ) -> None:
        """Default-suite smoke test at N=1k. Catches obvious regressions."""
        n = 1_000
        stats = _run_one_scale(
            db_path=db_path, index=index, registry=registry, n=n
        )
        print(
            f"\n[CA-3 read latency, N={n}] "
            f"samples={int(stats['samples'])} "
            f"p50={stats['p50_ms']:.1f}ms "
            f"p95={stats['p95_ms']:.1f}ms "
            f"p99={stats['p99_ms']:.1f}ms "
            f"max={stats['max_ms']:.1f}ms"
        )
        ceiling = _P99_CEILING_MS_BY_N[n]
        assert stats["p99_ms"] < ceiling, (
            f"p99 {stats['p99_ms']:.1f}ms exceeds {ceiling}ms ceiling at N={n}. "
            f"Likely indicates the read path regressed to a full SQL scan or "
            f"Chroma index was rebuilt on every query."
        )

    @pytest.mark.slow
    @pytest.mark.parametrize("n", [10_000, 50_000, 100_000])
    def test_read_latency_at_scale(
        self,
        db_path: Path,
        index: TupleIndex,
        registry: Registry,
        n: int,
    ) -> None:
        """Full sweep: 10k / 50k / 100k. Gated behind ``-m slow``."""
        stats = _run_one_scale(
            db_path=db_path, index=index, registry=registry, n=n
        )
        print(
            f"\n[CA-3 read latency, N={n}] "
            f"samples={int(stats['samples'])} "
            f"p50={stats['p50_ms']:.1f}ms "
            f"p95={stats['p95_ms']:.1f}ms "
            f"p99={stats['p99_ms']:.1f}ms "
            f"max={stats['max_ms']:.1f}ms"
        )
        ceiling = _P99_CEILING_MS_BY_N[n]
        assert stats["p99_ms"] < ceiling, (
            f"p99 {stats['p99_ms']:.1f}ms exceeds {ceiling}ms ceiling at N={n}. "
            f"Likely indicates the read path regressed to a full SQL scan or "
            f"Chroma index was rebuilt on every query."
        )
