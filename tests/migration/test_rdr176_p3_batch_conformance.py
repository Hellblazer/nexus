# SPDX-License-Identifier: AGPL-3.0-or-later
"""RDR-176 Phase 3 (Gap 1) — universal O(N/batch) transfer-count conformance.

The managed migration must ship each store's rows in a BOUNDED number of HTTP
round-trips — ``ceil(N / batch_cap)`` — never one request per row. The 6.0.0
dogfood took hours because the T2 ETLs (memory, plans, telemetry, taxonomy) POST
once per row: 190k topic_assignments = 190k requests. This test pins the
contract for EVERY store with NO exemption: the number of POSTs to a store's
import route must be exactly ``ceil(N / cap)``.

Instrumentation is uniform: every Http* store exposes ``self._client`` (an
``httpx.Client``); we swap it for one backed by an ``httpx.MockTransport`` that
COUNTS requests per path and returns a permissive canned 200. This measures the
TRUE round-trip count and exercises the real store + ETL code (including the
batch path once it lands), with no live service.

Failing-first (bead nexus-t9rmg.17): the per-row T2 ETLs do N POSTs, so the
exact ``== ceil(N/cap)`` assertion FAILS today; bead .18 (Java import_batch +
client batching) makes them conformant. chash is the already-batched control
that must already pass.
"""
from __future__ import annotations

import json
import math
import sqlite3
from collections import Counter
from pathlib import Path

import httpx
import pytest

from nexus.db.chroma_quotas import QUOTAS
from nexus.db.t2 import T2Database
from nexus.db.t2.chash_etl import migrate_chash_rows
from nexus.db.t2.http_chash_index import HttpChashIndex
from nexus.db.t2.http_memory_store import HttpMemoryStore
from nexus.db.t2.http_plan_library import HttpPlanLibrary
from nexus.db.t2.http_taxonomy_store import HttpTaxonomyStore
from nexus.db.t2.http_telemetry_store import HttpTelemetryStore
from nexus.db.t2.memory_etl import migrate_memory_rows
from nexus.db.t2.plan_etl import migrate_plan_rows
from nexus.db.t2.taxonomy_etl import migrate_taxonomy_rows
from nexus.db.t2.telemetry_etl import migrate_telemetry_rows

#: The per-call record cap (ChromaDB / service quota). A conformant ETL sends
#: ceil(N / CAP) batches.
CAP = QUOTAS.MAX_RECORDS_PER_WRITE  # 300

#: Row count per store: strictly > CAP so a per-row ETL (N posts) is clearly
#: distinguishable from a batched one (ceil(N/CAP) posts).
N_ROWS = CAP + 50  # 350 → ceil(350/300) == 2


def _counting_client(counter: Counter) -> httpx.Client:
    """An httpx.Client whose MockTransport counts POSTs per path and returns a
    permissive 200 covering every store's response-parsing shape."""

    def _handler(request: httpx.Request) -> httpx.Response:
        if request.method == "POST":
            counter[request.url.path] += 1
        # Permissive body: import_entry reads ["id"], batch paths may read
        # ["imported"]/["ids"], others ["written"]/["count"]/["deleted"].
        return httpx.Response(
            200,
            json={
                "id": 1, "imported": 1, "count": 1, "written": 1,
                "deleted": 0, "ids": [1], "ok": True,
            },
        )

    return httpx.Client(base_url="http://svc", transport=httpx.MockTransport(_handler))


def _seed_memory(db: Path, n: int) -> None:
    T2Database.bootstrap_schema(db)
    conn = sqlite3.connect(str(db))
    try:
        conn.executemany(
            "INSERT INTO memory (project, title, content, timestamp, tags, "
            "ttl, access_count, last_accessed) VALUES (?,?,?,?,?,?,?,?)",
            [
                (f"proj{i % 3}", f"title-{i}", f"content {i}",
                 "2026-05-15T08:30:00Z", "a,b", None, 0, None)
                for i in range(n)
            ],
        )
        conn.commit()
    finally:
        conn.close()


def _run_memory_etl(db: Path, store: object) -> None:
    migrate_memory_rows(db, store)


def _build_memory_store() -> object:
    return HttpMemoryStore(base_url="http://svc", _token="t")


# ── plans (per-row today: store.import_plan per row) ─────────────────────────


def _seed_plans(db: Path, n: int) -> None:
    T2Database.bootstrap_schema(db)
    conn = sqlite3.connect(str(db))
    try:
        conn.executemany(
            "INSERT INTO plans (project, query, plan_json, created_at) "
            "VALUES (?,?,?,?)",
            [
                (f"proj{i % 3}", f"query-{i}", json.dumps({"steps": [i]}),
                 "2026-05-15T08:30:00Z")
                for i in range(n)
            ],
        )
        conn.commit()
    finally:
        conn.close()


def _run_plans_etl(db: Path, store: object) -> None:
    migrate_plan_rows(db, store)


def _build_plans_store() -> object:
    return HttpPlanLibrary(base_url="http://svc", _token="t")


# ── chash (already-batched control — must already pass at its 200 cap) ───────


def _seed_chash(db: Path, n: int) -> None:
    T2Database.bootstrap_schema(db)
    conn = sqlite3.connect(str(db))
    try:
        conn.executemany(
            "INSERT INTO chash_index (chash, physical_collection, created_at) "
            "VALUES (?,?,?)",
            [
                (f"{i:064x}", "knowledge__rehearsal__minilm-l6-v2-384__v1",
                 "2026-05-15T08:30:00Z")
                for i in range(n)
            ],
        )
        conn.commit()
    finally:
        conn.close()


def _run_chash_etl(db: Path, store: object) -> None:
    migrate_chash_rows(db, store)


def _build_chash_store() -> object:
    return HttpChashIndex(base_url="http://svc", _token="t")


# ── telemetry (per-row today across 6 tables) ────────────────────────────────


def _seed_telemetry(db: Path, n: int) -> None:
    """Seed N hook_failures rows (one of the six telemetry tables) — enough to
    prove the per-table batch path; the other five stay empty (0 POSTs)."""
    T2Database.bootstrap_schema(db)
    conn = sqlite3.connect(str(db))
    try:
        conn.executemany(
            "INSERT INTO hook_failures (doc_id, collection, hook_name, error, "
            "occurred_at, batch_doc_ids, is_batch, chain) VALUES (?,?,?,?,?,?,?,?)",
            [
                (f"doc{i}", "knowledge__rehearsal__minilm-l6-v2-384__v1",
                 "post_store", f"err {i}", "2026-05-15T08:30:00Z", None, 0, "single")
                for i in range(n)
            ],
        )
        conn.commit()
    finally:
        conn.close()


def _run_telemetry_etl(db: Path, store: object) -> None:
    migrate_telemetry_rows(db, store)


def _build_telemetry_store() -> object:
    return HttpTelemetryStore(base_url="http://svc", _token="t")


# ── taxonomy (the 190k-row dogfood offender: store.import_* per row) ──────────


def _seed_taxonomy(db: Path, n: int) -> None:
    """Seed N topics rows (one of the four taxonomy kinds) — enough to prove the
    per-kind batch path; assignments/links/meta stay empty (0 POSTs)."""
    T2Database.bootstrap_schema(db)
    conn = sqlite3.connect(str(db))
    try:
        conn.executemany(
            "INSERT INTO topics (id, label, parent_id, collection, centroid_hash, "
            "doc_count, created_at, review_status, terms) VALUES (?,?,?,?,?,?,?,?,?)",
            [
                (i + 1, f"topic-{i}", None,
                 "knowledge__rehearsal__minilm-l6-v2-384__v1", f"{i:032x}",
                 0, "2026-05-15T08:30:00Z", "approved", "a b c")
                for i in range(n)
            ],
        )
        conn.commit()
    finally:
        conn.close()


def _run_taxonomy_etl(db: Path, store: object) -> None:
    migrate_taxonomy_rows(db, store)


def _build_taxonomy_store() -> object:
    return HttpTaxonomyStore(base_url="http://svc", _token="t")


# (name, build_store, seed, run_etl, import_path, cap)
_STORES = [
    ("memory", _build_memory_store, _seed_memory, _run_memory_etl, "/v1/memory/import_batch", CAP),
    ("plans", _build_plans_store, _seed_plans, _run_plans_etl, "/v1/plans/import_batch", CAP),
    ("telemetry", _build_telemetry_store, _seed_telemetry, _run_telemetry_etl, "/v1/telemetry/import_batch", CAP),
    ("taxonomy", _build_taxonomy_store, _seed_taxonomy, _run_taxonomy_etl, "/v1/taxonomy/import_batch", CAP),
    ("chash", _build_chash_store, _seed_chash, _run_chash_etl, "/v1/chash/import", 200),
]


@pytest.mark.parametrize("name,build_store,seed,run_etl,import_path,cap", _STORES)
def test_etl_transfer_count_is_o_n_over_batch(
    name: str, build_store, seed, run_etl, import_path: str, cap: int, tmp_path: Path
) -> None:
    """The store's import route must receive exactly ceil(N/cap) POSTs — bounded
    batches, never one per row."""
    db = tmp_path / f"{name}.db"
    seed(db, N_ROWS)

    counter: Counter = Counter()
    store = build_store()
    store._client = _counting_client(counter)  # uniform round-trip counter
    try:
        run_etl(db, store)
    finally:
        store.close()

    expected = math.ceil(N_ROWS / cap)
    assert counter[import_path] == expected, (
        f"{name}: expected {expected} batched POST(s) to {import_path} for "
        f"{N_ROWS} rows (cap {cap}), got {counter[import_path]} — "
        f"{'per-row, not batched' if counter[import_path] >= N_ROWS else 'unexpected count'}"
    )
