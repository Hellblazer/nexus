# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""RDR-089 Phase 1.1: T2 ``document_aspects`` domain store contract.

Contract tests for the document-aspect store — round-trip upsert,
idempotent overwrite semantics, list/delete by collection,
extractor-version filter, and facade wiring.

Ported (nexus-i711w Stage 2 sub-stage A3): the SQLite ``DocumentAspects``
store is deleted; ``T2Database(path).document_aspects`` is now an
``HttpDocumentAspectsStore`` unconditionally, and the suite's autouse
engine substrate (``_pin_t2_substrate``) gives every test a real
engine-backed store on a fresh tenant. Every raw-SQL assertion below was
converted to its public-read equivalent (``get`` / ``list_by_collection``).

The PG schema itself is Liquibase-owned
(``service/src/main/resources/db/changelog/aspects-001-baseline.xml``);
the old TestSchema class that pinned the SQLite DDL (sqlite_master /
PRAGMA table_info / WAL journal mode) died with the store — see the
tombstone below. The HTTP wire contract is pinned by
``tests/db/test_http_aspects_stores.py``; the real-service integration
twin is ``tests/db/test_http_aspects_stores_integration.py``.
"""
from __future__ import annotations

import sqlite3
from datetime import datetime
from pathlib import Path

import pytest

from nexus.db.t2 import T2Database
from nexus.db.t2.records import AspectRecord


def _make_record(
    *,
    collection: str = "knowledge__delos",
    source_path: str = "/papers/p1.pdf",
    problem_formulation: str = "Sharded write-ahead log...",
    proposed_method: str = "Hybrid Paxos with...",
    experimental_datasets: list[str] | None = None,
    experimental_baselines: list[str] | None = None,
    experimental_results: str = "30% throughput improvement",
    extras: dict | None = None,
    confidence: float | None = 0.92,
    extracted_at: str = "2026-04-25T17:00:00+00:00",
    model_version: str = "claude-haiku-4-5-20251001",
    extractor_name: str = "haiku-aspect-v1",
) -> AspectRecord:
    return AspectRecord(
        collection=collection,
        source_path=source_path,
        problem_formulation=problem_formulation,
        proposed_method=proposed_method,
        experimental_datasets=experimental_datasets or ["TPC-C", "YCSB"],
        experimental_baselines=experimental_baselines or ["raft", "paxos"],
        experimental_results=experimental_results,
        extras=extras or {"venue": "VLDB", "year": 2023},
        confidence=confidence,
        extracted_at=extracted_at,
        model_version=model_version,
        extractor_name=extractor_name,
    )


def _instant(ts: str) -> datetime:
    """Parse an ISO timestamp, tolerating the engine's ``...Z`` suffix.

    The engine stores ``extracted_at`` as timestamptz and reads it back
    normalized to ``yyyy-MM-ddTHH:mm:ss.SSSSSSZ`` (AspectRepository.formatTs),
    so timestamp equality is by INSTANT, not by string.
    """
    return datetime.fromisoformat(ts.replace("Z", "+00:00"))


@pytest.fixture()
def store(tmp_path: Path):
    """Engine-backed document_aspects store via the T2Database facade."""
    with T2Database(tmp_path / "t2.db") as db:
        yield db.document_aspects


# ── Schema ───────────────────────────────────────────────────────────────────
#
# TestSchema stood here (nexus-i711w Stage 2 sub-stage A3). Its four tests
# pinned the SQLite store's OWN mechanics — table creation at construction,
# the RDR-locked column list via PRAGMA table_info, the (collection,
# source_path) compound PK, and WAL journal mode. The store is deleted, so
# the subject is gone: the schema is now PG DDL owned by Liquibase
# (aspects-001-baseline.xml — surrogate ``id`` PK + UNIQUE (tenant_id,
# collection, source_path) natural key, RLS FORCE'd), and schema drift is a
# Liquibase-changeset diff, not a client-side assertion. The natural-key
# SEMANTICS the PK test protected (one row per (collection, source_path),
# multiple chunks collapse to one aspect row) survive below in
# TestIdempotentUpsert, asserted through public reads.


# ── Round-trip upsert + get ──────────────────────────────────────────────────


class TestUpsertGet:
    """Round-trip semantics: upsert(record) then get(...) returns
    a structurally-equal record. JSON fields deserialize on read."""

    def test_upsert_then_get_returns_same_record(self, store) -> None:
        rec = _make_record()
        store.upsert(rec)
        got = store.get("knowledge__delos", "/papers/p1.pdf")

        assert got is not None
        assert got.collection == rec.collection
        assert got.source_path == rec.source_path
        assert got.problem_formulation == rec.problem_formulation
        assert got.proposed_method == rec.proposed_method
        assert got.experimental_datasets == rec.experimental_datasets
        assert got.experimental_baselines == rec.experimental_baselines
        assert got.experimental_results == rec.experimental_results
        assert got.extras == rec.extras
        assert got.confidence == rec.confidence
        # Timestamptz round-trip: same instant, engine-normalized rendering.
        assert _instant(got.extracted_at) == _instant(rec.extracted_at)
        assert got.model_version == rec.model_version
        assert got.extractor_name == rec.extractor_name

    def test_get_missing_returns_none(self, store) -> None:
        assert store.get("knowledge__nope", "/missing.pdf") is None

    def test_json_fields_round_trip_typed(self, store) -> None:
        """Datasets/baselines (lists) and extras (dict) must survive the
        JSON wire + storage round-trip with their Python types AND value
        types intact (e.g. ``year`` stays ``int``, not ``"2023"``).

        Successor to the raw-column ``json.loads`` assertion — the storage
        encoding is the engine's concern now; what the caller owns is
        typed fidelity through the public read.
        """
        store.upsert(_make_record())
        got = store.get("knowledge__delos", "/papers/p1.pdf")
        assert got is not None
        assert got.experimental_datasets == ["TPC-C", "YCSB"]
        assert got.experimental_baselines == ["raft", "paxos"]
        assert got.extras == {"venue": "VLDB", "year": 2023}
        assert isinstance(got.extras["year"], int)


class TestConfidenceFloor:
    """nexus-17wf: rows with NULL or sub-floor confidence must be
    DROPPED at upsert (no row written, upsert returns False). The
    2026-05-08 prod probe found 125 of 753 rows (16.6%) committed with
    NULL or zero confidence, polluting downstream consumers
    (``nx aspects show``, retrieval ranking, telemetry) that treated
    them as authoritative. Per the project's no-silent-fallback
    principle, the right shape is reject + log.

    The gate is enforced ENGINE-side now (AspectRepository.MIN_CONFIDENCE
    = 0.3, mirroring the retired Python ``_MIN_CONFIDENCE``); these tests
    pin the caller-visible contract through the HTTP store.
    """

    def test_null_confidence_is_dropped(self, store) -> None:
        written = store.upsert(_make_record(confidence=None))
        assert written is False
        assert store.get("knowledge__delos", "/papers/p1.pdf") is None

    def test_zero_confidence_is_dropped(self, store) -> None:
        written = store.upsert(_make_record(confidence=0.0))
        assert written is False
        assert store.get("knowledge__delos", "/papers/p1.pdf") is None

    def test_confidence_at_floor_is_persisted(self, store) -> None:
        """A confidence equal to the floor (0.3) is accepted; only
        STRICTLY-below values are dropped. Lock the boundary so a
        future tightening of the floor surfaces as a deliberate
        diff rather than a silent drop.
        """
        written = store.upsert(_make_record(confidence=0.3))
        assert written is True
        assert store.get("knowledge__delos", "/papers/p1.pdf") is not None

    def test_confidence_just_below_floor_is_dropped(self, store) -> None:
        written = store.upsert(_make_record(confidence=0.29))
        assert written is False
        assert store.get("knowledge__delos", "/papers/p1.pdf") is None

    def test_high_confidence_unaffected(self, store) -> None:
        """Real extractions (avg confidence 0.823 in the 2026-05-08
        probe) clear the floor comfortably. Lock the contract so a
        future change that accidentally tightens the floor too high
        surfaces in the test diff.
        """
        assert store.upsert(_make_record(confidence=0.823)) is True
        got = store.get("knowledge__delos", "/papers/p1.pdf")
        assert got is not None
        assert got.confidence == 0.823


# ── Idempotent overwrite (RDR Upsert Semantics — load-bearing) ───────────────


class TestIdempotentUpsert:
    """Repeat upsert is a COMPLETE OVERWRITE — no diff/merge, no
    deviation log. The stored row reflects the latest extraction
    verbatim. RDR pins this contract.
    """

    def test_repeat_upsert_overwrites_all_fields(self, store) -> None:
        store.upsert(_make_record(problem_formulation="Old framing"))
        store.upsert(
            _make_record(
                problem_formulation="New framing",
                confidence=0.55,
                extras={"venue": "SOSP", "year": 2024},
            ),
        )
        got = store.get("knowledge__delos", "/papers/p1.pdf")

        assert got is not None
        assert got.problem_formulation == "New framing"
        assert got.confidence == 0.55
        assert got.extras == {"venue": "SOSP", "year": 2024}

    def test_repeat_upsert_does_not_duplicate_rows(self, store) -> None:
        store.upsert(_make_record())
        store.upsert(_make_record(confidence=0.1))  # sub-floor: rejected
        store.upsert(_make_record(confidence=0.5))
        rows = store.list_by_collection("knowledge__delos")
        assert len(rows) == 1

    def test_distinct_source_paths_create_distinct_rows(self, store) -> None:
        store.upsert(_make_record(source_path="/papers/p1.pdf"))
        store.upsert(_make_record(source_path="/papers/p2.pdf"))
        rows = store.list_by_collection("knowledge__delos")
        assert len(rows) == 2

    def test_distinct_collections_create_distinct_rows(self, store) -> None:
        store.upsert(_make_record(collection="knowledge__a"))
        store.upsert(_make_record(collection="knowledge__b"))
        assert len(store.list_by_collection("knowledge__a")) == 1
        assert len(store.list_by_collection("knowledge__b")) == 1


# ── List + delete ────────────────────────────────────────────────────────────


class TestListDelete:
    def test_list_by_collection(self, store) -> None:
        store.upsert(_make_record(source_path="/p1.pdf"))
        store.upsert(_make_record(source_path="/p2.pdf"))
        store.upsert(_make_record(collection="knowledge__other", source_path="/p3.pdf"))
        rows = store.list_by_collection("knowledge__delos")
        assert len(rows) == 2
        paths = sorted(r.source_path for r in rows)
        assert paths == ["/p1.pdf", "/p2.pdf"]

    def test_list_pagination(self, store) -> None:
        for i in range(5):
            store.upsert(_make_record(source_path=f"/p{i}.pdf"))
        page1 = store.list_by_collection("knowledge__delos", limit=2, offset=0)
        page2 = store.list_by_collection("knowledge__delos", limit=2, offset=2)
        page3 = store.list_by_collection("knowledge__delos", limit=2, offset=4)
        assert len(page1) == 2
        assert len(page2) == 2
        assert len(page3) == 1
        # Pagination yields disjoint sets.
        all_paths = (
            {r.source_path for r in page1}
            | {r.source_path for r in page2}
            | {r.source_path for r in page3}
        )
        assert len(all_paths) == 5

    def test_delete_removes_row(self, store) -> None:
        store.upsert(_make_record())
        store.delete("knowledge__delos", "/papers/p1.pdf")
        assert store.get("knowledge__delos", "/papers/p1.pdf") is None

    def test_delete_missing_is_no_op(self, store) -> None:
        store.delete("knowledge__nope", "/missing.pdf")  # must not raise


# ── Extractor-version filter ─────────────────────────────────────────────────


class TestVersionFilter:
    """``list_by_extractor_version(extractor_name, max_version)`` returns
    rows whose ``extractor_name`` matches and ``model_version`` is
    strictly less than ``max_version``. Used by re-extraction logic to
    find documents whose aspects were captured by an older model and
    should be re-run.
    """

    def test_filter_returns_rows_below_version(self, store) -> None:
        store.upsert(_make_record(
            source_path="/old.pdf", model_version="claude-haiku-4-1",
        ))
        store.upsert(_make_record(
            source_path="/mid.pdf", model_version="claude-haiku-4-3",
        ))
        store.upsert(_make_record(
            source_path="/new.pdf", model_version="claude-haiku-4-5-20251001",
        ))
        rows = store.list_by_extractor_version(
            "haiku-aspect-v1", "claude-haiku-4-5-20251001",
        )
        paths = sorted(r.source_path for r in rows)
        assert paths == ["/mid.pdf", "/old.pdf"]

    def test_filter_strict_less_than_excludes_equal(self, store) -> None:
        """Filter is STRICT less-than. A row with ``model_version`` equal
        to the threshold is NOT returned (no duplicate re-extraction).
        """
        store.upsert(_make_record(
            source_path="/x.pdf", model_version="claude-haiku-4-5-20251001",
        ))
        rows = store.list_by_extractor_version(
            "haiku-aspect-v1", "claude-haiku-4-5-20251001",
        )
        assert rows == []

    def test_filter_scopes_to_extractor_name(self, store) -> None:
        """The filter is scoped: rows from a different ``extractor_name``
        are not considered, even if ``model_version`` is below threshold.
        """
        store.upsert(_make_record(
            source_path="/a.pdf",
            extractor_name="haiku-aspect-v1",
            model_version="claude-haiku-4-1",
        ))
        store.upsert(_make_record(
            source_path="/b.pdf",
            extractor_name="custom-extractor",
            model_version="claude-haiku-4-1",
        ))
        rows = store.list_by_extractor_version(
            "haiku-aspect-v1", "claude-haiku-4-5",
        )
        assert [r.source_path for r in rows] == ["/a.pdf"]


# ── Facade wiring ────────────────────────────────────────────────────────────


class TestFacadeWiring:
    """``T2Database`` exposes the store as ``db.document_aspects``
    alongside the existing ``db.memory`` / ``db.plans`` / ``db.taxonomy``
    / ``db.telemetry`` / ``db.chash_index``.
    """

    def test_t2database_exposes_document_aspects(self, tmp_path: Path) -> None:
        """Since nexus-i711w Stage 2 sub-stage A3 the facade constructs
        HttpDocumentAspectsStore UNCONDITIONALLY — the SQLite arm (and the
        ``local_t2_backend`` pin this test used to carry) is gone.
        """
        from nexus.db.t2.http_document_aspects_store import (
            HttpDocumentAspectsStore,
        )

        with T2Database(tmp_path / "t2.db") as db:
            assert hasattr(db, "document_aspects")
            assert isinstance(db.document_aspects, HttpDocumentAspectsStore)

    def test_facade_round_trip_through_property(self, tmp_path: Path) -> None:
        with T2Database(tmp_path / "t2.db") as db:
            db.document_aspects.upsert(_make_record())
            got = db.document_aspects.get("knowledge__delos", "/papers/p1.pdf")
            assert got is not None
            assert got.problem_formulation.startswith("Sharded")

    def test_row_persists_across_facade_lifetimes(self, tmp_path: Path) -> None:
        """Close one facade, open another: the row is still readable.

        Successor to the WAL-write-lock release test — the lock contention
        it guarded against died with the SQLite connection, but the
        caller-visible half (rows outlive the facade instance that wrote
        them, and ``close()`` leaves the store reusable) still holds.
        """
        path = tmp_path / "t2.db"
        with T2Database(path) as db:
            db.document_aspects.upsert(_make_record())
        with T2Database(path) as db2:
            got = db2.document_aspects.get("knowledge__delos", "/papers/p1.pdf")
            assert got is not None


# ── Migration sanity ─────────────────────────────────────────────────────────


class TestMigration:
    """The migration entry idempotently creates the table and is
    no-op when the table already exists (CREATE IF NOT EXISTS pattern).

    Still exercises the SQLite MIGRATION SOURCE (``nexus.db.migrations``)
    — retained per the NO-SQLite directive's carve-out: SQLite stays a
    migration source until RDR-155 P4b deletes the tooling.
    """

    def test_migration_creates_table(self, tmp_path: Path) -> None:
        from nexus.db.migrations import migrate_document_aspects_table

        db_path = tmp_path / "post_migrate.db"
        raw = sqlite3.connect(str(db_path))
        migrate_document_aspects_table(raw)

        tables = {
            r[0] for r in raw.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        }
        raw.close()
        assert "document_aspects" in tables

    def test_migration_idempotent(self, tmp_path: Path) -> None:
        from nexus.db.migrations import migrate_document_aspects_table

        db_path = tmp_path / "idempotent.db"
        raw = sqlite3.connect(str(db_path))
        migrate_document_aspects_table(raw)
        # Second call must be a no-op.
        migrate_document_aspects_table(raw)
        raw.close()
