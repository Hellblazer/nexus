# SPDX-License-Identifier: AGPL-3.0-or-later
"""RDR-178 epic-closing acceptance test (bead nexus-te885.2, Wave 3).

The RDR's defining claim — that ``migrate all`` (T2) + ``migrate vectors``
(vectors) is safe to run UNATTENDED, survives a mid-run 5xx blip with zero
permanently-failed batches, and a second immediate run is a fast no-op — has
no dedicated test: eleven green wave-1/wave-2 children individually prove
their own slice (verify-fill delta correctness, the breaker's retry/trip
mechanics, per-store wiring) but none of them compose the WHOLE unattended
run at corpus scale with a fault injected mid-stream. This module is that
composed gate (gate-critique CRITICAL, 2026-07-02).

Design (worked out via sequential-thinking before writing this module):

* Drives ``nexus.migration.orchestrator.migrate_all`` — the LIBRARY entry,
  not the ``click`` CLI wrapper. RDR-159's own module docstring documents
  the CLI as "a thin wrapper over migrate_all: it constructs the sources,
  supplies a progress callback, persists the report, and maps gates onto
  exit codes — there is exactly one orchestration code path." Driving this
  through ``CliRunner`` across 8 stores' worth of service-URL/token/breaker
  flags would add CLI-argument-parsing surface noise without adding
  coverage of the corpus-scale/fault-survival claim, which is a
  library-level concern — the same precedent ``tests/migration/
  test_orchestrator.py`` itself establishes for exercising ``migrate_all``.
* ``build_store_etls`` is monkeypatched (test_orchestrator.py's own
  precedent) so REAL per-store ETL functions run against IN-PROCESS fakes
  instead of constructing live ``Http*Store`` clients against a real
  service. Two tiers of fake, matched to what each store's claim actually
  needs:
    - catalog / telemetry carry the bead's LOAD-BEARING claims (the
      2026-07-01 incident shape was chash+catalog; the chash leg retired
      with the ETL store, RDR-187/nexus-piwya.10, and catalog carries the
      incident vehicle alone; R4 is telemetry-specific) — these get REAL
      ``verify_fill_*`` functions driven against STATEFUL fakes (mirroring
      test_verify_fill_regression.py's non-tautology discipline: the same
      dict a fake's write path mutates is what its identity/count surface
      reads, so "second pass is a true no-op" is a genuine observation, not
      a replay of a canned snapshot).
    - memory / plans / taxonomy / aspects / aspects_queue are NOT
      re-tested here (each has its own dedicated ETL test file) — simple
      counting spies matching test_orchestrator.py's ``_fake_etls`` prove
      the COMPOSED ladder still reaches them and that memory/plans/
      taxonomy's skip-on-parity fold-in (Gap 7) engages on the no-op
      re-run, without re-deriving their per-row transform correctness.
* The T2 mid-run 5xx burst is injected on the CATALOG leg's import path
  (a call-count burst window around the fake's ``_post``, raising
  ``ConnectionError`` — the socket-error branch of the retryable
  classification; the HTTP-status branch is exhaustively covered in
  test_rdr178_gap3_circuit_breaker.py) — the 2026-07-01 incident's
  mid-run-burst SHAPE; it moved here from the chash leg when RDR-187
  (nexus-piwya.10) retired that store. Telemetry is NOT separately
  fault-injected in the same run: the breaker mechanics
  themselves are exhaustively unit-tested once in
  test_rdr178_gap3_circuit_breaker.py and shared verbatim by every ETL
  call site (``_etl_batch_with_breaker``); re-instantiating the burst per
  surface would re-prove the same mechanism without adding a new
  assertion axis (test_verify_fill_regression.py's own module docstring
  makes the identical scoping call for its five surfaces).
* The vectors leg's blip lands on its VERIFY-FILL (delta) pass, not the
  first full migrate — per the R5 critic note, ``HttpVectorClient.
  existing_ids`` (the identity PROBE) has no retry and fails OPEN to an
  empty set on a transport error, which is only distinguishable from "the
  target really holds none of this batch" once the target already holds
  SOME data (a virgin target's "probe reports empty" is correct regardless
  of any fault). So the fault window is applied to the SECOND (delta) pass
  against a partially-pre-populated target — the write path (``upsert_
  chunks``) is still breaker-retried and safe; the identity probe is not,
  and the test asserts write-safety (final counts, zero failed) rather
  than exact missing/filled counts, exactly as R5 prescribes.

Out of scope (residual, tracked elsewhere): the 300-row pagination boundary
of the vector/catalog identity-fetch FAKES specifically (they answer presence
from in-memory dicts with no limit/offset walk — same disclosed boundary as
test_verify_fill_regression.py; the vector read path's real 300-cap paging
IS crossed; the chash 200-row batching axis retired with its store,
RDR-187); the ``existing_ids`` fail-open fix itself
(te885.6); the docker/live-service ``tests/e2e/migration-rehearsal`` journey
(cloud-gated, a different test tier entirely).
"""
from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any

import chromadb
import pytest

import nexus.retry as retry
from nexus.db.http_vector_client import VectorServiceError
from nexus.migration import orchestrator as orch
from nexus.migration.etl_registry import EtlSources, StoreEtl
from nexus.migration.vector_etl import migrate_collections, verify_fill_collections
from nexus.retry import EtlCircuitBreaker

from tests.db.test_telemetry_etl import _seed_full_telemetry_db  # noqa: PLC2701 — shared test fixture, mirrors test_verify_fill_regression.py's own precedent
from tests.migration.test_verify_fill_regression import (  # noqa: PLC2701 — shared stateful fakes, explicit relay instruction
    _StatefulTelemetryTarget,
)
from tests.migration.test_vector_etl import FakeVectorClient, _coll, _seed_source  # noqa: PLC2701 — shared test fakes


@pytest.fixture(autouse=True)
def _fast_sleep(monkeypatch: pytest.MonkeyPatch) -> None:
    """Every breaker pause / backoff sleep is a no-op — this module injects
    a real burst that trips the breaker at least once per test."""
    monkeypatch.setattr(retry.time, "sleep", lambda _s: None)


# ═══════════════════════════════════════════════════════════════════════════
# Corpus-scale row counts (module constants so every assertion below reads
# against a named quantity instead of a magic number).
# ═══════════════════════════════════════════════════════════════════════════

                              # 200-row import batches per collection (the
                              # incident's "many pages" shape at fixture scale)
_CATALOG_CHUNKS_PER_DOC = 70
_CATALOG_SHARED_CHASH_COUNT = 15  # doc B's first 15 positions reuse doc A's
                                    # chash values verbatim (RDR-108 D1 shape)
_HOOK_FAILURES_N = 60   # mapped relation (nexus.hook_failures)
_NX_ANSWER_RUNS_N = 40  # mapped relation (nexus.nx_answer_runs)
_FRECENCY_N = 50        # unmapped — R4's drift target
_RELEVANCE_N = 12
_SEARCH_N = 12
_TIER_N = 12
_MEMORY_N = 25
_PLANS_N = 18
_TOPICS_N = 14
_ASSIGN_N = 11
_LINKS_N = 7
_ASPECTS_N = 12
_ASPECTS_QUEUE_N = 9


# ═══════════════════════════════════════════════════════════════════════════
# (chash leg retired, RDR-187/nexus-piwya.10 — the burst vehicle below moved
# to the catalog leg)
# stateful store (the 2026-07-01 incident shape).
# ═══════════════════════════════════════════════════════════════════════════


class _BurstWindow:
    """Shared call-count-based 5xx injector: the first *warmup* calls
    succeed, the next *burst* calls 502, everything after recovers —
    mirrors test_rdr178_gap3_circuit_breaker.py's burst design, just with a
    non-zero warmup so the fault genuinely lands MID-RUN rather than at the
    very first call."""

    def __init__(self, *, warmup: int, burst: int) -> None:
        self.warmup = warmup
        self.burst = burst
        self.calls = 0

    def should_fail(self) -> bool:
        self.calls += 1
        return self.warmup < self.calls <= self.warmup + self.burst


# _make_chash_transport / _ChashFakeStore / _seed_chash_sqlite RETIRED
# (RDR-187/nexus-piwya.10) with the chash ETL store; the 5xx burst rides
# the catalog leg now.


# ═══════════════════════════════════════════════════════════════════════════
# catalog: real migrate_catalog / verify_fill_catalog against a stateful,
# non-faulted fake catalog client (2+ docs, shared chashes — RDR-108 D1).
# ═══════════════════════════════════════════════════════════════════════════

_CAT_OWNER = "1"
_CAT_COLLECTION = "code__cat"
_CAT_DOC_A = "1.1"
_CAT_DOC_B = "1.2"


class _CatalogFakeClient:
    """Full-ETL write surface (``_post``) + verify-fill read surface
    (``list_owners``/``list_collections``/``chashes_for_collection``/
    ``get_manifest``) over the SAME in-memory state."""

    def __init__(self) -> None:
        self.owners: dict[str, dict[str, Any]] = {}
        self.documents: dict[str, dict[str, Any]] = {}
        self.collections: dict[str, dict[str, Any]] = {}
        self.chunks_by_doc: dict[str, dict[int, str]] = {}
        self.doc_collection: dict[str, str] = {}
        self.links: list[dict[str, Any]] = []
        self.posts: list[tuple[str, dict[str, Any]]] = []

    def close(self) -> None:
        pass

    def _post(self, path: str, payload: dict[str, Any]) -> None:
        self.posts.append((path, payload))
        if path == "/import/owner":
            rows = payload["rows"] if "rows" in payload else [payload]
            for row in rows:
                self.owners[row["tumbler_prefix"]] = row
        elif path == "/import/document":
            for row in payload["rows"]:
                self.documents[row["tumbler"]] = row
                self.doc_collection[row["tumbler"]] = row.get("physical_collection") or ""
        elif path == "/import/collection":
            for row in payload["rows"]:
                self.collections[row["name"]] = row
        elif path == "/import/chunk":
            doc_id = payload["doc_id"]
            for row in payload["rows"]:
                self.chunks_by_doc.setdefault(doc_id, {})[row["position"]] = row["chash"]
        elif path == "/import/link":
            self.links.extend(payload["rows"])

    # ── verify-fill surfaces ─────────────────────────────────────────────
    def list_owners(self) -> list[dict[str, Any]]:
        return list(self.owners.values())

    def list_collections(self) -> list[dict[str, Any]]:
        return list(self.collections.values())

    def chashes_for_collection(self, collection: str) -> set[str]:
        chashes: set[str] = set()
        for doc_id, coll in self.doc_collection.items():
            if coll == collection:
                chashes.update(self.chunks_by_doc.get(doc_id, {}).values())
        return chashes

    def get_manifest(self, doc_id: str) -> list[Any]:
        from nexus.catalog.catalog_writes import ManifestRow  # noqa: PLC0415 — real-shape fake, scoped

        return [
            ManifestRow(position=pos, chash=chash)
            for pos, chash in sorted(self.chunks_by_doc.get(doc_id, {}).items())
        ]


def _seed_catalog_sqlite(catalog_db: Path) -> None:
    conn = sqlite3.connect(str(catalog_db))
    conn.execute(
        "CREATE TABLE owners (tumbler_prefix TEXT, name TEXT, owner_type TEXT, "
        "repo_hash TEXT, description TEXT, repo_root TEXT, head_hash TEXT)"
    )
    conn.execute(
        "CREATE TABLE documents (tumbler TEXT, title TEXT, author TEXT, year INT, "
        "content_type TEXT, file_path TEXT, corpus TEXT, physical_collection TEXT, "
        "chunk_count INT, head_hash TEXT, indexed_at TEXT, metadata TEXT, "
        "source_mtime REAL, alias_of TEXT, source_uri TEXT, bib_year INT, "
        "bib_authors TEXT, bib_venue TEXT, bib_citation_count INT, "
        "bib_semantic_scholar_id TEXT, bib_openalex_id TEXT, bib_doi TEXT, "
        "bib_enriched_at TEXT)"
    )
    conn.execute(
        "CREATE TABLE links (id INTEGER PRIMARY KEY, from_tumbler TEXT, "
        "to_tumbler TEXT, link_type TEXT, from_span TEXT, to_span TEXT, "
        "created_by TEXT, created_at TEXT, metadata TEXT)"
    )
    conn.execute(
        "CREATE TABLE collections (name TEXT, content_type TEXT, owner_id TEXT, "
        "embedding_model TEXT, model_version TEXT, display_name TEXT, "
        "legacy_grandfathered INT, superseded_by TEXT, superseded_at TEXT, "
        "created_at TEXT)"
    )
    conn.execute(
        "CREATE TABLE document_chunks (doc_id TEXT, position INT, chash TEXT, "
        "chunk_index INT, line_start INT, line_end INT, char_start INT, char_end INT)"
    )
    conn.execute("CREATE TABLE _meta (key TEXT, value TEXT)")

    conn.execute(
        "INSERT INTO owners VALUES (?, 'owner-1', 'user', '', '', '', '')",
        (_CAT_OWNER,),
    )
    conn.executemany(
        "INSERT INTO documents VALUES (?, 'doc', '', 0, '', '', '', ?, ?, '', '', "
        "NULL, 0, '', '', 0, '', '', 0, '', '', '', '')",
        [
            (_CAT_DOC_A, _CAT_COLLECTION, _CATALOG_CHUNKS_PER_DOC),
            (_CAT_DOC_B, _CAT_COLLECTION, _CATALOG_CHUNKS_PER_DOC),
        ],
    )
    conn.execute(
        "INSERT INTO collections VALUES (?, 'code', '1', 'voyage', 'v1', '', 0, "
        "'', '', '')",
        (_CAT_COLLECTION,),
    )

    # Doc A: chashes "a0000".."a0069" (own positions).
    chunk_rows: list[tuple[str, int, str]] = [
        (_CAT_DOC_A, i, f"a{i:04d}") for i in range(_CATALOG_CHUNKS_PER_DOC)
    ]
    # Doc B: first _CATALOG_SHARED_CHASH_COUNT positions reuse doc A's chash
    # values verbatim (identical chunk text indexed at two doc locations —
    # RDR-108 D1's "ambiguous candidate" shape); the rest are doc B's own.
    for i in range(_CATALOG_CHUNKS_PER_DOC):
        if i < _CATALOG_SHARED_CHASH_COUNT:
            chash = f"a{i:04d}"  # shared with doc A
        else:
            chash = f"b{i:04d}"
        chunk_rows.append((_CAT_DOC_B, i, chash))
    conn.executemany(
        "INSERT INTO document_chunks VALUES (?, ?, ?, 0, 0, 0, 0, 0)", chunk_rows,
    )
    conn.commit()
    conn.close()


# ═══════════════════════════════════════════════════════════════════════════
# telemetry: real migrate_telemetry_rows / verify_fill_telemetry against the
# regression suite's shared stateful target (reused verbatim, per relay).
# ═══════════════════════════════════════════════════════════════════════════


def _seed_telemetry_sqlite(db_path: Path) -> None:
    hooks = [
        {
            "doc_id": f"d{i:04d}", "hook_name": "h1",
            "occurred_at": "2024-01-01T00:00:00+00:00",
        }
        for i in range(_HOOK_FAILURES_N)
    ]
    nx = [
        {
            "question": f"q{i:04d}", "created_at": "2024-01-01T00:00:00+00:00",
            "final_text": "answer",
        }
        for i in range(_NX_ANSWER_RUNS_N)
    ]
    frecency = [{"chunk_id": f"fc{i:04d}"} for i in range(_FRECENCY_N)]
    relevance = [
        {
            "query": f"query{i}", "chunk_id": f"ch{i:04d}", "action": "clicked",
            "timestamp": "2024-01-01T00:00:00+00:00",
        }
        for i in range(_RELEVANCE_N)
    ]
    search = [
        {
            "ts": f"2024-04-{(i % 28) + 1:02d}T00:00:00Z", "query_hash": f"qh{i:04d}",
            "collection": "code__x",
        }
        for i in range(_SEARCH_N)
    ]
    tier = [
        {
            "session_id": f"s{i:04d}", "ts": "2024-01-01T00:00:00+00:00",
            "tool": "search", "tier": "T1",
        }
        for i in range(_TIER_N)
    ]
    _seed_full_telemetry_db(
        db_path, hooks=hooks, nx=nx, frecency=frecency,
        relevance=relevance, search=search, tier=tier,
    )


# ═══════════════════════════════════════════════════════════════════════════
# memory / plans / taxonomy / aspects / aspects_queue: counting spies (no
# real per-row ETL logic re-exercised here — each has its own test file).
# ═══════════════════════════════════════════════════════════════════════════


class _CountingSpy:
    def __init__(self, store: str, table: str, n: int) -> None:
        self.store = store
        self.table = table
        self.n = n
        self.run_count = 0
        self.landed = 0

    def run(self, _sources: EtlSources, collector: Any) -> dict:
        self.run_count += 1
        self.landed = self.n  # idempotent import: landed count, not accumulated
        collector.count_read(self.store, self.table, self.n)
        collector.count_written(self.store, self.table, self.n)
        return {}


class _TaxonomyCountingSpy:
    def __init__(self, *, topics: int, assignments: int, links: int) -> None:
        self.n = {"topics": topics, "topic_assignments": assignments, "topic_links": links}
        self.run_count = 0
        self.landed = {"topics": 0, "topic_assignments": 0, "topic_links": 0}

    def run(self, _sources: EtlSources, collector: Any) -> dict:
        self.run_count += 1
        for table, n in self.n.items():
            self.landed[table] = n
            collector.count_read("taxonomy", table, n)
            collector.count_written("taxonomy", table, n)
        return {}


def _seed_generic_sqlite_tables(db_path: Path) -> None:
    """Bare-bones ``memory``/``plans``/``topics``/``topic_assignments``/
    ``topic_links`` tables — real enough for each store's own
    ``count_source_rows`` (a plain ``SELECT COUNT(*)``) to work; the row
    CONTENT is irrelevant since these five stores use counting spies, not
    the real per-row ETL, in this composed test."""
    conn = sqlite3.connect(str(db_path))
    conn.execute("CREATE TABLE memory (id INTEGER PRIMARY KEY)")
    conn.executemany(
        "INSERT INTO memory (id) VALUES (?)", [(i,) for i in range(_MEMORY_N)],
    )
    conn.execute("CREATE TABLE plans (id INTEGER PRIMARY KEY)")
    conn.executemany(
        "INSERT INTO plans (id) VALUES (?)", [(i,) for i in range(_PLANS_N)],
    )
    conn.execute("CREATE TABLE topics (id INTEGER PRIMARY KEY)")
    conn.executemany(
        "INSERT INTO topics (id) VALUES (?)", [(i,) for i in range(_TOPICS_N)],
    )
    conn.execute("CREATE TABLE topic_assignments (id INTEGER PRIMARY KEY)")
    conn.executemany(
        "INSERT INTO topic_assignments (id) VALUES (?)", [(i,) for i in range(_ASSIGN_N)],
    )
    conn.execute("CREATE TABLE topic_links (id INTEGER PRIMARY KEY)")
    conn.executemany(
        "INSERT INTO topic_links (id) VALUES (?)", [(i,) for i in range(_LINKS_N)],
    )
    conn.commit()
    conn.close()


# ═══════════════════════════════════════════════════════════════════════════
# Composed count source: LIVE per-relation counts derived from the SAME
# fakes' write-path state (non-tautology discipline, spanning all 8 stores).
# ═══════════════════════════════════════════════════════════════════════════


class _ComposedCountSource:
    def __init__(
        self,
        *,
        catalog: _CatalogFakeClient,
        telemetry: _StatefulTelemetryTarget,
        memory_spy: _CountingSpy,
        plans_spy: _CountingSpy,
        taxonomy_spy: _TaxonomyCountingSpy,
    ) -> None:
        self._catalog = catalog
        self._telemetry = telemetry
        self._memory_spy = memory_spy
        self._plans_spy = plans_spy
        self._taxonomy_spy = taxonomy_spy

    def counts(self, relations: list[str]) -> dict[str, int]:
        return {r: self._resolve(r) for r in relations}

    def _resolve(self, relation: str) -> int:
        if relation == "nexus.catalog_owners":
            return len(self._catalog.owners)
        if relation == "nexus.catalog_documents":
            return len(self._catalog.documents)
        if relation == "nexus.catalog_collections":
            return len(self._catalog.collections)
        if relation == "nexus.catalog_document_chunks":
            return sum(len(m) for m in self._catalog.chunks_by_doc.values())
        if relation == "nexus.catalog_links":
            return len(self._catalog.links)
        if relation == "nexus.hook_failures":
            return len(self._telemetry.present_by_table.get("hook_failures", set()))
        if relation == "nexus.nx_answer_runs":
            return len(self._telemetry.present_by_table.get("nx_answer_runs", set()))
        if relation == "nexus.memory":
            return self._memory_spy.landed
        if relation == "nexus.plans":
            return self._plans_spy.landed
        if relation == "nexus.topics":
            return self._taxonomy_spy.landed["topics"]
        if relation == "nexus.topic_assignments":
            return self._taxonomy_spy.landed["topic_assignments"]
        if relation == "nexus.topic_links":
            return self._taxonomy_spy.landed["topic_links"]
        return 0


class _LiveTelemetryMappedCountSource:
    """The ``count_source`` argument ``verify_fill_telemetry`` takes
    directly (distinct from the composed migrate_all-level source above,
    same live-derivation discipline)."""

    def __init__(self, telemetry: _StatefulTelemetryTarget) -> None:
        self._telemetry = telemetry

    def counts(self, relations: list[str]) -> dict[str, int]:
        mapping = {
            "nexus.hook_failures": "hook_failures",
            "nexus.nx_answer_runs": "nx_answer_runs",
        }
        return {
            r: len(self._telemetry.present_by_table.get(mapping.get(r, ""), set()))
            for r in relations
        }


# ═══════════════════════════════════════════════════════════════════════════
# Corpus bundle: builds the full fixture + monkeypatches migrate_all's store
# construction seams, returning everything a test needs to drive two passes.
# ═══════════════════════════════════════════════════════════════════════════


class _Corpus:
    def __init__(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, *, inject_burst: bool) -> None:
        self.sqlite_path = tmp_path / "t2.db"
        self.catalog_db = tmp_path / ".catalog.db"

        _seed_telemetry_sqlite(self.sqlite_path)
        _seed_generic_sqlite_tables(self.sqlite_path)
        _seed_catalog_sqlite(self.catalog_db)

        # ── catalog (carries the 5xx-burst incident vehicle — RDR-187 moved
        # it here from the retired chash leg) ──────────────────────────────
        self.catalog_fake = _CatalogFakeClient()
        self.catalog_breaker = EtlCircuitBreaker()
        # burst sized past the breaker's consecutive-failure trip threshold
        # (the retired chash vehicle used 9 for the same reason) so the trip
        # is OBSERVED, then recovery follows.
        self.catalog_window = _BurstWindow(
            warmup=2, burst=9 if inject_burst else 0,
        )
        _orig_post = self.catalog_fake._post

        def _burst_post(path: str, payload: dict) -> None:
            if self.catalog_window.should_fail():
                raise ConnectionError("simulated 5xx burst (catalog import)")
            _orig_post(path, payload)

        self.catalog_fake._post = _burst_post

        # ── telemetry ──────────────────────────────────────────────────────
        self.telemetry_target = _StatefulTelemetryTarget({})
        self.telemetry_breaker = EtlCircuitBreaker()
        self.telemetry_count_source = _LiveTelemetryMappedCountSource(self.telemetry_target)

        # ── generic stores ────────────────────────────────────────────────
        self.memory_spy = _CountingSpy("memory", "memory", _MEMORY_N)
        self.plans_spy = _CountingSpy("plans", "plans", _PLANS_N)
        self.taxonomy_spy = _TaxonomyCountingSpy(
            topics=_TOPICS_N, assignments=_ASSIGN_N, links=_LINKS_N,
        )
        self.aspects_spy = _CountingSpy("aspects", "document_aspects", _ASPECTS_N)
        self.aspects_queue_spy = _CountingSpy(
            "aspects_queue", "aspect_extraction_queue", _ASPECTS_QUEUE_N,
        )

        self.count_source = _ComposedCountSource(
            catalog=self.catalog_fake,
            telemetry=self.telemetry_target,
            memory_spy=self.memory_spy,
            plans_spy=self.plans_spy,
            taxonomy_spy=self.taxonomy_spy,
        )

        self._wire(monkeypatch)

    @property
    def sources(self) -> EtlSources:
        return EtlSources(sqlite_path=self.sqlite_path, catalog_db_path=self.catalog_db)

    def _wire(self, monkeypatch: pytest.MonkeyPatch) -> None:
        corpus = self

        def _build_store_etls(_sources: EtlSources) -> list[StoreEtl]:
            def _catalog(sources: EtlSources, collector: Any) -> dict:
                from nexus.db.t2.catalog_etl import migrate_catalog  # noqa: PLC0415

                return migrate_catalog(
                    sources.catalog_db_path, corpus.catalog_fake,
                    collector=collector, breaker=corpus.catalog_breaker,
                )

            def _telemetry(sources: EtlSources, collector: Any) -> dict:
                from nexus.db.t2.telemetry_etl import migrate_telemetry_rows  # noqa: PLC0415

                return migrate_telemetry_rows(
                    sources.sqlite_path, corpus.telemetry_target,
                    collector=collector, breaker=corpus.telemetry_breaker,
                )

            return [
                StoreEtl("memory", corpus.memory_spy.run),
                StoreEtl("plans", corpus.plans_spy.run),
                StoreEtl("telemetry", _telemetry),
                StoreEtl("taxonomy", corpus.taxonomy_spy.run),
                StoreEtl("aspects", corpus.aspects_spy.run),
                StoreEtl("catalog", _catalog),
                StoreEtl("aspects_queue", corpus.aspects_queue_spy.run),
            ]

        monkeypatch.setattr(orch, "build_store_etls", _build_store_etls)
        monkeypatch.setattr(orch, "_open_catalog_client", lambda: corpus.catalog_fake)
        monkeypatch.setattr(orch, "_open_telemetry_store", lambda: corpus.telemetry_target)


# ═══════════════════════════════════════════════════════════════════════════
# 1-3, 6: corpus-scale unattended first pass survives a mid-run 5xx burst.
# ═══════════════════════════════════════════════════════════════════════════


class TestComposedUnattendedT2PassSurvivesBurst:
    def test_first_pass_zero_permanent_failures_verified_breaker_engaged(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        corpus = _Corpus(tmp_path, monkeypatch, inject_burst=True)

        report = orch.migrate_all(
            corpus.sources, count_source=corpus.count_source, verify_fill=False,
        )

        # (2) zero permanently-failed batches, clean report.
        assert report["summary"]["total_failed"] == 0
        # (2) verification == "verified" (never "passed" — 5b98dcca vocabulary fix).
        assert report["verification"] == "verified"
        assert "mismatch" not in report["verification"]

        # Every store actually ran (corpus spans ALL T2 stores).
        assert corpus.memory_spy.run_count == 1
        assert corpus.plans_spy.run_count == 1
        assert corpus.taxonomy_spy.run_count == 1
        assert corpus.aspects_spy.run_count == 1
        assert corpus.aspects_queue_spy.run_count == 1

        # Catalog: 2 docs, shared-chash collapse — distinct chashes are
        # 2*_CATALOG_CHUNKS_PER_DOC - _CATALOG_SHARED_CHASH_COUNT (dedup by
        # chash value, RDR-108 D1), never the raw row count.
        distinct_chashes = corpus.catalog_fake.chashes_for_collection(_CAT_COLLECTION)
        assert len(distinct_chashes) == (
            2 * _CATALOG_CHUNKS_PER_DOC - _CATALOG_SHARED_CHASH_COUNT
        )
        assert len(corpus.catalog_fake.documents) == 2

        # Telemetry: all six tables present in the landed state.
        for table, n in (
            ("hook_failures", _HOOK_FAILURES_N),
            ("nx_answer_runs", _NX_ANSWER_RUNS_N),
            ("frecency", _FRECENCY_N),
            ("relevance_log", _RELEVANCE_N),
            ("search_telemetry", _SEARCH_N),
            ("tier_writes", _TIER_N),
        ):
            assert len(corpus.telemetry_target.present_by_table.get(table, set())) == n

        # (6) the breaker was OBSERVED handling the burst — not bypassed.
        # (RDR-187: the burst rides the catalog leg now.)
        assert corpus.catalog_breaker.trip_count >= 1
        assert corpus.catalog_window.calls > corpus.catalog_window.warmup + corpus.catalog_window.burst

    def test_vectors_delta_pass_under_blip_preserves_write_safety(
        self, tmp_path: Path,
    ) -> None:
        """(3) R5: under the blip, assert write-safety (dest counts ==
        source, zero permanently-failed) — NEVER exact missing/filled
        counts, since ``existing_ids`` fails open (no retry) and a
        wide-enough blip can degrade the whole probe to a false
        "everything missing" signal (te885.6 tracks the read-side fix).
        The write path (``upsert_chunks``) IS breaker-retried and is what
        this test actually gates on."""
        source_client = chromadb.EphemeralClient()
        for col in source_client.list_collections():
            source_client.delete_collection(col.name)

        name = _coll("rdr178-acceptance")
        source_count = 450  # forces 2 read pages at the 300-row query cap
        ids = _seed_source(source_client, name, source_count)

        # Pass 1: clean full migrate — populates the target completely.
        clean_target = FakeVectorClient()
        report1 = migrate_collections(source_client, clean_target, leg="local")
        assert report1.ok is True
        assert clean_target.count(name) == source_count

        # Simulate a small amount of local drift since the last migrate
        # (the verify-fill scenario itself) plus wrap the SAME state in a
        # fault-injecting client for pass 2.
        holed = ids[100:105]
        for missing_id in holed:
            clean_target.store[name].pop(missing_id, None)
        clean_target.upsert_calls.clear()

        class _BurstyVerifyFillClient(FakeVectorClient):
            """Wraps the SAME store dict: ``existing_ids`` fails OPEN
            (returns empty — HttpVectorClient's documented degrade
            contract, R5) for the first *probe_burst* calls;
            ``upsert_chunks`` 502s (retryable, breaker-observed) for the
            first *upsert_burst* calls, then both recover."""

            def __init__(self, store: dict, upsert_calls: list, *, probe_burst: int, upsert_burst: int) -> None:
                super().__init__()
                self.store = store
                self.upsert_calls = upsert_calls
                self._probe_burst = probe_burst
                self._upsert_burst = upsert_burst
                self.probe_attempts = 0
                self.upsert_attempts = 0

            def existing_ids(self, collection: str, ids: list[str]) -> set[str]:  # noqa: A002
                self.probe_attempts += 1
                if self.probe_attempts <= self._probe_burst:
                    return set()  # fail-open: no exception, just empty (R5)
                return super().existing_ids(collection, ids)

            def upsert_chunks(self, collection, ids, documents, metadatas=None, *, embeddings=None):  # noqa: A002
                self.upsert_attempts += 1
                if self.upsert_attempts <= self._upsert_burst:
                    raise VectorServiceError(
                        f"edge blip #{self.upsert_attempts}", code=502,
                    )
                super().upsert_chunks(collection, ids, documents, metadatas, embeddings=embeddings)

        fault_client = _BurstyVerifyFillClient(
            clean_target.store, clean_target.upsert_calls,
            probe_burst=2,  # both read-pages' probes degrade to empty
            upsert_burst=2,  # first 2 upsert attempts 502, then recover
        )
        breaker = EtlCircuitBreaker()

        report2 = verify_fill_collections(
            source_client, fault_client, leg="local", breaker=breaker,
        )
        result = report2.results[0]

        # Write-safety (R5): the target genuinely holds every source id at
        # the end — upsert is idempotent, so re-sending falsely-"missing"
        # rows is harmless. This is the hard gate; NEVER a permanent failure.
        assert fault_client.count(name) == source_count
        assert result.status != "failed"

        # R5's residual, REPRODUCED (not dodged): when the probe degrades
        # widely enough (here: BOTH read-pages' existing_ids calls fail
        # open), the never-blind-fill "suspicious probe" heuristic
        # correctly refuses to call this a trusted "filled" — it reports
        # "indeterminate" instead (verified empirically: missing_count and
        # filled_count both come back as the FULL source_count, 450, not
        # the true 5-row hole — a false whole-collection resend under the
        # blip, exactly the failure mode R5 flags as the vectors leg's
        # residual gap, deferred to te885.6). The design is honest about
        # this: report.ok is False, never a silent green.
        assert result.status == "indeterminate"
        assert report2.ok is False
        # (3) R5's explicit instruction: do NOT assert exact missing/filled
        # counts here — they are inflated by the probe degradation, not a
        # reflection of the true 5-row hole. What's asserted is the SAFETY
        # invariant above (write-safety) and the fact that the writes this
        # inflated signal triggered still converged, idempotently, to the
        # correct final state.

        # (6) breaker observed handling the burst on the write path — every
        # upsert batch that hit the fault window recovered via retry.
        assert fault_client.upsert_attempts > 2  # at least one retry occurred beyond the 2-call burst


# ═══════════════════════════════════════════════════════════════════════════
# 5: an immediate second unattended pass is a fast no-op.
# ═══════════════════════════════════════════════════════════════════════════


class TestComposedSecondPassIsFastNoop:
    def test_second_pass_skips_or_delta_fills_zero_no_full_resend(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        corpus = _Corpus(tmp_path, monkeypatch, inject_burst=True)

        report1 = orch.migrate_all(
            corpus.sources, count_source=corpus.count_source, verify_fill=False,
        )
        assert report1["summary"]["total_failed"] == 0

        # Snapshot write-path activity BEFORE the second pass.
        catalog_posts_before = len(corpus.catalog_fake.posts)
        telemetry_imports_before = len(corpus.telemetry_target.import_calls)

        report2 = orch.migrate_all(
            corpus.sources, count_source=corpus.count_source, verify_fill=True,
        )

        assert report2["summary"]["total_failed"] == 0

        # memory/plans/taxonomy: parity -> folded into skip_stores (Gap 7),
        # never even called a second time.
        assert set(report2.get("skipped_stores", [])) == {"memory", "plans", "taxonomy"}
        assert corpus.memory_spy.run_count == 1  # unchanged since pass 1
        assert corpus.plans_spy.run_count == 1
        assert corpus.taxonomy_spy.run_count == 1

        # catalog: all 5 mapped tables at parity -> zero further POSTs.
        assert len(corpus.catalog_fake.posts) == catalog_posts_before
        outer_catalog = report2["verify_fill"]["outer"]["catalog"]
        for table in ("owners", "documents", "collections", "document_chunks", "links"):
            assert outer_catalog[table]["status"] == "parity"
        assert "fallback" not in report2["verify_fill"]["results"]["catalog"]

        # telemetry: mapped tables (hook_failures/nx_answer_runs) skip their
        # probe entirely on parity; unmapped tables are probed (reads, not
        # writes) but find nothing missing -> zero import calls (bead's own
        # "zero or near-zero rows transmitted" tolerance for the READ side).
        assert len(corpus.telemetry_target.import_calls) == telemetry_imports_before
        outer_telemetry = report2["verify_fill"]["outer"]["telemetry"]
        assert outer_telemetry["hook_failures"]["status"] == "parity"
        assert outer_telemetry["nx_answer_runs"]["status"] == "parity"
        assert "hook_failures" not in report2["verify_fill"]["results"]["telemetry"]["fill"]
        assert "nx_answer_runs" not in report2["verify_fill"]["results"]["telemetry"]["fill"]

        # aspects / aspects_queue: NO delta-fill surface wired yet (documented
        # scope boundary, RDR-178 wave-2) — they DO fully re-run every pass.
        # Asserted explicitly (not silently tolerated) so a future delta-fill
        # landing for these two is a visible, deliberate test update.
        assert corpus.aspects_spy.run_count == 2
        assert corpus.aspects_queue_spy.run_count == 2

        # Total telemetry FILL work this pass is genuinely zero (no drift
        # was introduced between passes in this scenario).
        assert report2["verify_fill"]["results"]["telemetry"]["total_filled"] == 0
        assert report2["verify_fill"]["results"]["catalog"]["total_filled"] == 0


# ═══════════════════════════════════════════════════════════════════════════
# 4 (R4): telemetry drift in an UNMAPPED table must not be reported
# clean/skipped at the composed migrate_all level.
# ═══════════════════════════════════════════════════════════════════════════


class TestTelemetryUnmappedDriftNotSilentlySkipped:
    def test_frecency_drift_delta_filled_hook_failures_nx_answer_runs_stay_parity(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        corpus = _Corpus(tmp_path, monkeypatch, inject_burst=False)

        report1 = orch.migrate_all(
            corpus.sources, count_source=corpus.count_source, verify_fill=False,
        )
        assert report1["summary"]["total_failed"] == 0

        # Punch a hole ONLY into frecency (unmapped) — hook_failures and
        # nx_answer_runs (mapped) stay fully landed, at parity.
        hole = 4
        present = corpus.telemetry_target.present_by_table["frecency"]
        punched = set(list(present)[:hole])
        corpus.telemetry_target.present_by_table["frecency"] = present - punched
        imports_before = len(corpus.telemetry_target.import_calls)

        report2 = orch.migrate_all(
            corpus.sources, count_source=corpus.count_source, verify_fill=True,
        )

        assert report2["summary"]["total_failed"] == 0
        outer = report2["verify_fill"]["outer"]["telemetry"]
        # Mapped tables: genuinely at parity (R4's premise).
        assert outer["hook_failures"]["status"] == "parity"
        assert outer["nx_answer_runs"]["status"] == "parity"
        # Unmapped table: ALWAYS indeterminate at the outer level (no PG
        # relation mapping exists for frecency) — this is NOT "clean/skipped".
        assert outer["frecency"]["status"] == "indeterminate"

        fill = report2["verify_fill"]["results"]["telemetry"]["fill"]
        # The store did NOT silently skip frecency: it ran real per-table
        # delta-fill and sent EXACTLY the punched hole — never zero (which
        # would mean "treated indeterminate as clean"), never the full
        # table (which would mean "fell back to blind resend").
        assert fill["frecency"]["missing"] == hole
        assert fill["frecency"]["filled"] == hole
        assert fill["frecency"]["filled"] != _FRECENCY_N

        # hook_failures/nx_answer_runs, genuinely at parity, are skipped
        # (zero probe) — the fill dict never even mentions them.
        assert "hook_failures" not in fill
        assert "nx_answer_runs" not in fill

        # Confirms the write actually happened (not a report artifact only).
        assert len(corpus.telemetry_target.import_calls) == imports_before + 1
        sent_table_names = {t for t, _rows in corpus.telemetry_target.import_calls[imports_before:]}
        assert sent_table_names == {"frecency"}
        assert corpus.telemetry_target.present_by_table["frecency"] == present  # hole re-closed


class TestWatermarkThirdPassShortcut:
    """nexus-te885.10 part 2: after a breaker-clean verify-fill pass recorded
    watermarks for the four count-unmapped tables, the NEXT no-op pass probes
    only source rows above each watermark — zero rows on an unchanged source —
    instead of re-probing the entire table contents."""

    def test_third_pass_probes_above_watermark_only(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        corpus = _Corpus(tmp_path, monkeypatch, inject_burst=False)
        monkeypatch.setenv("NEXUS_CONFIG_DIR", str(tmp_path / "wmcfg"))

        report1 = orch.migrate_all(
            corpus.sources, count_source=corpus.count_source, verify_fill=False,
        )
        assert report1["summary"]["total_failed"] == 0

        # Pass 2 (verify-fill): full probe (no watermark yet), breaker-clean,
        # so it ADVANCES the watermarks for all four unmapped tables.
        report2 = orch.migrate_all(
            corpus.sources, count_source=corpus.count_source, verify_fill=True,
        )
        assert report2["summary"]["total_failed"] == 0

        # Pass 3: capture the min_rowid every read uses.
        from nexus.db.t2 import telemetry_etl as etl_mod
        seen: dict[str, int] = {}
        real_read = etl_mod.read_rows_for_fill

        def _spy(conn, table, *, collector=None, min_rowid=0):
            seen[table] = min_rowid
            return real_read(conn, table, collector=collector, min_rowid=min_rowid)

        monkeypatch.setattr(etl_mod, "read_rows_for_fill", _spy)
        report3 = orch.migrate_all(
            corpus.sources, count_source=corpus.count_source, verify_fill=True,
        )
        assert report3["summary"]["total_failed"] == 0

        from nexus.migration.verify_fill_watermark import (
            RETENTION_HORIZON_TABLES,
            WATERMARK_TABLES,
        )
        fill3 = report3["verify_fill"]["results"]["telemetry"]["fill"]
        for table in WATERMARK_TABLES:
            if table in RETENTION_HORIZON_TABLES:
                # The corpus's relevance fixtures are 2024-era — past the
                # retention horizon. nexus-ots8o: that must read as
                # EXPIRED_UNVERIFIABLE (no watermark, no probe, and above
                # all never dressed as verified parity).
                assert seen.get(table, 0) == 0
                assert fill3[table]["status"] == "expired_unverifiable"
                assert fill3[table]["horizon_excluded"] > 0
                continue
            assert seen.get(table, 0) > 0, (
                f"pass 3 must probe {table} above the pass-2 watermark, "
                f"got min_rowid={seen.get(table)}"
            )
            assert fill3[table]["status"] == "parity"
            assert fill3[table]["filled"] == 0
