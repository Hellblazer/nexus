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
from nexus.db.t2.catalog_etl import _import_table
from nexus.db.t2.chash_etl import migrate_chash_rows
from nexus.db.t2.aspects_etl import (
    migrate_aspects,
    migrate_highlights,
    migrate_promotion_log,
    migrate_queue,
)
from nexus.db.t2.http_aspect_queue import HttpAspectQueue
from nexus.db.t2.http_chash_index import HttpChashIndex
from nexus.db.t2.http_document_aspects_store import HttpDocumentAspectsStore
from nexus.db.t2.http_document_highlights_store import HttpDocumentHighlightsStore
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

#: Row count per store: strictly > 2*CAP so a per-row ETL (N posts) is clearly
#: distinguishable from a batched one, AND so the EXACT cap matters —
#: ceil(650/300)=3 but ceil(650/200)=4, so a regression to a 200-row batch
#: (review H-1) would now FAIL the assertion instead of coincidentally passing.
N_ROWS = 2 * CAP + 50  # 650 → ceil(650/300) == 3


def _counting_client(counter: Counter) -> httpx.Client:
    """An httpx.Client whose MockTransport counts POSTs per path and returns a
    permissive 200 covering every store's response-parsing shape."""

    def _handler(request: httpx.Request) -> httpx.Response:
        if request.method == "POST":
            counter[request.url.path] += 1
            # Taxonomy multiplexes four kinds over ONE route
            # (/v1/taxonomy/import_batch) with the kind in the body. The
            # assignment/link kinds REQUIRE prerequisite topics to be seeded
            # (else they orphan-skip), and those topics POST to the same route —
            # so count per-kind too, letting a per-kind entry assert its own
            # batch path in isolation from the prerequisite topics' POSTs.
            try:
                body = json.loads(request.content)
                if isinstance(body, dict) and "kind" in body:
                    counter[f"{request.url.path}#{body['kind']}"] += 1
            except (ValueError, TypeError):
                pass
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


# ── aspects + aspect_queue (per-row today across 4 import paths) ──────────────


def _seed_aspects(db: Path, n: int) -> None:
    T2Database.bootstrap_schema(db)
    conn = sqlite3.connect(str(db))
    try:
        conn.executemany(
            "INSERT INTO document_aspects (collection, source_path, "
            "problem_formulation, confidence, extracted_at, model_version, "
            "extractor_name) VALUES (?,?,?,?,?,?,?)",
            [
                ("knowledge__rehearsal__minilm-l6-v2-384__v1", f"/p/doc{i}.pdf",
                 f"problem {i}", 0.9, "2026-05-15T08:30:00Z", "v1", "claude")
                for i in range(n)
            ],
        )
        conn.commit()
    finally:
        conn.close()


def _run_aspects_etl(db: Path, store: object) -> None:
    migrate_aspects(db, store)


def _build_aspects_store() -> object:
    return HttpDocumentAspectsStore(base_url="http://svc", _token="t")


def _seed_queue(db: Path, n: int) -> None:
    T2Database.bootstrap_schema(db)
    conn = sqlite3.connect(str(db))
    try:
        conn.executemany(
            "INSERT INTO aspect_extraction_queue (collection, source_path, "
            "content_hash, status, enqueued_at) VALUES (?,?,?,?,?)",
            [
                ("knowledge__rehearsal__minilm-l6-v2-384__v1", f"/p/doc{i}.pdf",
                 f"{i:064x}", "pending", "2026-05-15T08:30:00Z")
                for i in range(n)
            ],
        )
        conn.commit()
    finally:
        conn.close()


def _run_queue_etl(db: Path, store: object) -> None:
    migrate_queue(db, store)


def _build_queue_store() -> object:
    return HttpAspectQueue(base_url="http://svc", _token="t")


# ── Per-table / per-kind seeders for the SHARED-ROUTE stores ─────────────────
# Critic finding (2026-06-30): seeding only ONE telemetry table (hook_failures)
# or ONE taxonomy kind (topics) leaves the OTHER tables/kinds' batch paths
# unverified — an empty table contributes 0 POSTs, so `== ceil(N/cap)` passes
# vacuously off the single seeded table. topic_assignments (the literal 190k-row
# dogfood offender) was the worst case. Fix: one entry PER table/kind, each
# seeding N>cap rows into exactly that table so its own batch path drives the
# count. The ETL still walks every table; only the seeded one POSTs.

_TS = "2026-05-15T08:30:00Z"
_COLL = "knowledge__rehearsal__minilm-l6-v2-384__v1"


def _seed_into(db: Path, sql: str, rows: list) -> None:
    T2Database.bootstrap_schema(db)
    conn = sqlite3.connect(str(db))
    try:
        conn.executemany(sql, rows)
        conn.commit()
    finally:
        conn.close()


# telemetry: the other five tables (hook_failures = _seed_telemetry above)
def _seed_tel_relevance(db: Path, n: int) -> None:
    _seed_into(db, "INSERT INTO relevance_log (query, chunk_id, action, timestamp) "
               "VALUES (?,?,?,?)", [(f"q{i}", f"c{i}", "click", _TS) for i in range(n)])


def _seed_tel_search(db: Path, n: int) -> None:
    _seed_into(db, "INSERT INTO search_telemetry (ts, query_hash, collection, raw_count, "
               "kept_count) VALUES (?,?,?,?,?)", [(_TS, f"h{i}", _COLL, 10, 5) for i in range(n)])


def _seed_tel_tier(db: Path, n: int) -> None:
    _seed_into(db, "INSERT INTO tier_writes (session_id, ts, tool, tier) VALUES (?,?,?,?)",
               [(f"s{i}", _TS, "search", "T3") for i in range(n)])


def _seed_tel_nx(db: Path, n: int) -> None:
    _seed_into(db, "INSERT INTO nx_answer_runs (question, step_count, final_text, cost_usd, "
               "duration_ms, created_at) VALUES (?,?,?,?,?,?)",
               [(f"q{i}", 1, "ans", 0.01, 100, _TS) for i in range(n)])


def _seed_tel_frecency(db: Path, n: int) -> None:
    _seed_into(db, "INSERT INTO frecency (chunk_id, embedded_at, ttl_days, frecency_score, "
               "miss_count, last_hit_at) VALUES (?,?,?,?,?,?)",
               [(f"c{i}", _TS, 30, 0.5, 0, _TS) for i in range(n)])


# taxonomy: the other three kinds (topics = _seed_taxonomy above). Assignments
# and links orphan-skip any row whose topic_id is not a SOURCE topic, so each
# seeds the prerequisite topics it references (ids 1..n+1). Those topics POST to
# the SAME route, hence the per-kind counting in _counting_client.
def _seed_tax_assignments(db: Path, n: int) -> None:
    T2Database.bootstrap_schema(db)
    conn = sqlite3.connect(str(db))
    try:
        conn.executemany(
            "INSERT INTO topics (id, label, parent_id, collection, centroid_hash, "
            "doc_count, created_at, review_status, terms) VALUES (?,?,?,?,?,?,?,?,?)",
            [(i + 1, f"t{i}", None, _COLL, f"{i:032x}", 0, _TS, "approved", "a b")
             for i in range(n + 1)],
        )
        conn.executemany(
            "INSERT INTO topic_assignments (doc_id, topic_id, assigned_by, "
            "source_collection) VALUES (?,?,?,?)",
            [(f"d{i}", i + 1, "discover", _COLL) for i in range(n)],
        )
        conn.commit()
    finally:
        conn.close()


def _seed_tax_links(db: Path, n: int) -> None:
    T2Database.bootstrap_schema(db)
    conn = sqlite3.connect(str(db))
    try:
        conn.executemany(
            "INSERT INTO topics (id, label, parent_id, collection, centroid_hash, "
            "doc_count, created_at, review_status, terms) VALUES (?,?,?,?,?,?,?,?,?)",
            [(i + 1, f"t{i}", None, _COLL, f"{i:032x}", 0, _TS, "approved", "a b")
             for i in range(n + 2)],
        )
        conn.executemany(
            "INSERT INTO topic_links (from_topic_id, to_topic_id, link_count, "
            "link_types) VALUES (?,?,?,?)",
            [(1, i + 2, 3, "relates") for i in range(n)],
        )
        conn.commit()
    finally:
        conn.close()


def _seed_tax_meta(db: Path, n: int) -> None:
    _seed_into(db, "INSERT INTO taxonomy_meta (collection, last_discover_doc_count, "
               "last_discover_at) VALUES (?,?,?)", [(f"coll{i}", 0, _TS) for i in range(n)])


# aspects: the highlights and promotion_log paths (distinct ROUTES, prod runs
# all three via migrate_without_queue; the bare _run_aspects_etl above only hit
# /v1/aspects/import).
def _seed_highlights(db: Path, n: int) -> None:
    _seed_into(db, "INSERT INTO document_highlights (doc_id, source_uri, collection, "
               "highlights_md, mentions_md, ingested_at) VALUES (?,?,?,?,?,?)",
               [(f"d{i}", f"file://d{i}", _COLL, "h", "m", _TS) for i in range(n)])


def _run_highlights_etl(db: Path, store: object) -> None:
    migrate_highlights(db, store)


def _build_highlights_store() -> object:
    return HttpDocumentHighlightsStore(base_url="http://svc", _token="t")


def _seed_promotion(db: Path, n: int) -> None:
    _seed_into(db, "INSERT INTO aspect_promotion_log (field_name, sql_type, column_added, "
               "rows_backfilled, rows_pruned, pruned, promoted_at) VALUES (?,?,?,?,?,?,?)",
               [(f"field{i}", "TEXT", 1, 0, 0, 0, _TS) for i in range(n)])


def _run_promotion_etl(db: Path, store: object) -> None:
    migrate_promotion_log(db, store)


# (name, build_store, seed, run_etl, import_path, cap)
_STORES = [
    ("memory", _build_memory_store, _seed_memory, _run_memory_etl, "/v1/memory/import_batch", CAP),
    ("plans", _build_plans_store, _seed_plans, _run_plans_etl, "/v1/plans/import_batch", CAP),
    # telemetry: one entry per table — every table's batch path proven independently
    ("telemetry.hook_failures", _build_telemetry_store, _seed_telemetry, _run_telemetry_etl, "/v1/telemetry/import_batch", CAP),
    ("telemetry.relevance_log", _build_telemetry_store, _seed_tel_relevance, _run_telemetry_etl, "/v1/telemetry/import_batch", CAP),
    ("telemetry.search_telemetry", _build_telemetry_store, _seed_tel_search, _run_telemetry_etl, "/v1/telemetry/import_batch", CAP),
    ("telemetry.tier_writes", _build_telemetry_store, _seed_tel_tier, _run_telemetry_etl, "/v1/telemetry/import_batch", CAP),
    ("telemetry.nx_answer_runs", _build_telemetry_store, _seed_tel_nx, _run_telemetry_etl, "/v1/telemetry/import_batch", CAP),
    ("telemetry.frecency", _build_telemetry_store, _seed_tel_frecency, _run_telemetry_etl, "/v1/telemetry/import_batch", CAP),
    # taxonomy: one entry per kind — topic_assignments is the 190k dogfood offender
    ("taxonomy.topics", _build_taxonomy_store, _seed_taxonomy, _run_taxonomy_etl, "/v1/taxonomy/import_batch#topic", CAP),
    ("taxonomy.topic_assignments", _build_taxonomy_store, _seed_tax_assignments, _run_taxonomy_etl, "/v1/taxonomy/import_batch#assignment", CAP),
    ("taxonomy.topic_links", _build_taxonomy_store, _seed_tax_links, _run_taxonomy_etl, "/v1/taxonomy/import_batch#link", CAP),
    ("taxonomy.taxonomy_meta", _build_taxonomy_store, _seed_tax_meta, _run_taxonomy_etl, "/v1/taxonomy/import_batch#meta", CAP),
    # aspects: all three distinct routes (document_aspects, highlights, promotion_log)
    ("aspects.document_aspects", _build_aspects_store, _seed_aspects, _run_aspects_etl, "/v1/aspects/import", CAP),
    ("aspects.highlights", _build_highlights_store, _seed_highlights, _run_highlights_etl, "/v1/aspects/highlights/import", CAP),
    ("aspects.promotion_log", _build_aspects_store, _seed_promotion, _run_promotion_etl, "/v1/aspects/promotion/import", CAP),
    ("aspect_queue", _build_queue_store, _seed_queue, _run_queue_etl, "/v1/aspects/queue/import", CAP),
    ("chash", _build_chash_store, _seed_chash, _run_chash_etl, "/v1/chash/import", 200),
]


def test_catalog_import_table_is_o_n_over_batch() -> None:
    """RDR-176 P3: the catalog ETL's _import_table primitive (used by owners,
    documents, collections, links) must POST ceil(N/CAP) array batches, not N
    per-row requests. Catalog uses an HttpCatalogClient + a heavy migrate_catalog
    orchestration (orphan detection, next_seq re-POST), so the batching contract
    is pinned here on the primitive directly; the full orchestration is covered
    by tests/db/test_catalog_etl.py."""
    posted: list[int] = []
    result = _import_table(
        table="owners",
        rows=[{"tumbler_prefix": str(i)} for i in range(N_ROWS)],
        transform=lambda r: r,
        import_fn=lambda rows: posted.append(len(rows)),
        batch_log_every=10_000,
    )
    assert len(posted) == math.ceil(N_ROWS / CAP), (
        f"expected {math.ceil(N_ROWS / CAP)} array POST(s) for {N_ROWS} rows, "
        f"got {len(posted)} — per-row, not batched"
    )
    assert sum(posted) == N_ROWS  # every row shipped exactly once
    assert result["written"] == N_ROWS


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
