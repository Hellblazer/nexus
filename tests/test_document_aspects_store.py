# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""RDR-089 Phase 1.1: T2 ``DocumentAspects`` domain store.

Contract tests for the document-aspect store — round-trip upsert,
idempotent overwrite semantics, list/delete by collection,
extractor-version filter, and facade wiring.

Schema (locked by RDR — do not change):
    CREATE TABLE document_aspects (
        collection TEXT NOT NULL,
        source_path TEXT NOT NULL,
        problem_formulation TEXT,
        proposed_method TEXT,
        experimental_datasets TEXT,   -- JSON array
        experimental_baselines TEXT,  -- JSON array
        experimental_results TEXT,
        extras TEXT,                  -- JSON object; extensibility anchor
        confidence REAL,
        extracted_at TEXT NOT NULL,
        model_version TEXT NOT NULL,
        extractor_name TEXT NOT NULL,
        PRIMARY KEY (collection, source_path)
    );
"""
from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from nexus.db.t2 import T2Database


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
    confidence: float = 0.92,
    extracted_at: str = "2026-04-25T17:00:00+00:00",
    model_version: str = "claude-haiku-4-5-20251001",
    extractor_name: str = "haiku-aspect-v1",
):
    from nexus.db.t2.document_aspects import AspectRecord

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


# ── Init + schema ────────────────────────────────────────────────────────────


class TestSchema:
    """The store creates its table at construction time (CREATE IF NOT EXISTS
    pattern, mirroring ChashIndex / CatalogTaxonomy)."""

    def test_init_creates_table(self, tmp_path: Path) -> None:
        from nexus.db.t2.document_aspects import DocumentAspects

        store = DocumentAspects(tmp_path / "t2.db")
        try:
            tables = {
                r[0] for r in store.conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                ).fetchall()
            }
            assert "document_aspects" in tables
        finally:
            store.close()

    def test_schema_columns_match_rdr_lock(self, tmp_path: Path) -> None:
        """Schema column names + types match the RDR-locked spec exactly.

        If this assertion fails, the schema has drifted from the
        RDR. Either (a) the RDR is being deliberately revised — bump
        the column list HERE in lockstep with the RDR commit; or (b)
        an unintended column drift slipped in — revert the source.
        """
        from nexus.db.t2.document_aspects import DocumentAspects

        store = DocumentAspects(tmp_path / "t2.db")
        try:
            cols = {
                r[1]: (r[2], r[3])  # name → (type, notnull)
                for r in store.conn.execute(
                    "PRAGMA table_info(document_aspects)"
                ).fetchall()
            }
        finally:
            store.close()

        expected = {
            "collection": ("TEXT", 1),
            "source_path": ("TEXT", 1),
            "problem_formulation": ("TEXT", 0),
            "proposed_method": ("TEXT", 0),
            "experimental_datasets": ("TEXT", 0),
            "experimental_baselines": ("TEXT", 0),
            "experimental_results": ("TEXT", 0),
            "extras": ("TEXT", 0),
            "confidence": ("REAL", 0),
            "extracted_at": ("TEXT", 1),
            "model_version": ("TEXT", 1),
            "extractor_name": ("TEXT", 1),
            # RDR-096 P2.1: nullable URI column.
            "source_uri": ("TEXT", 0),
        }
        assert cols == expected

    def test_primary_key_is_collection_and_source_path(self, tmp_path: Path) -> None:
        """Compound PK ``(collection, source_path)`` — NOT ``(collection, doc_id)``.

        Per-chunk doc_id is intentionally not in the schema. Multiple
        chunks of the same source document map to a single aspect row.
        """
        from nexus.db.t2.document_aspects import DocumentAspects

        store = DocumentAspects(tmp_path / "t2.db")
        try:
            pk_cols = sorted(
                r[1]
                for r in store.conn.execute(
                    "PRAGMA table_info(document_aspects)"
                ).fetchall()
                if r[5] > 0  # pk index > 0 means part of PK
            )
        finally:
            store.close()
        assert pk_cols == ["collection", "source_path"]

    def test_table_journal_mode_wal(self, tmp_path: Path) -> None:
        """Mirror the WAL pattern used by chash_index / memory_store."""
        from nexus.db.t2.document_aspects import DocumentAspects

        store = DocumentAspects(tmp_path / "t2.db")
        try:
            mode = store.conn.execute("PRAGMA journal_mode").fetchone()[0]
        finally:
            store.close()
        assert mode.lower() == "wal"


# ── Round-trip upsert + get ──────────────────────────────────────────────────


class TestUpsertGet:
    """Round-trip semantics: upsert(record) then get(...) returns
    a structurally-equal record. JSON fields deserialize on read."""

    def test_upsert_then_get_returns_same_record(self, tmp_path: Path) -> None:
        from nexus.db.t2.document_aspects import DocumentAspects

        store = DocumentAspects(tmp_path / "t2.db")
        try:
            rec = _make_record()
            store.upsert(rec)
            got = store.get("knowledge__delos", "/papers/p1.pdf")
        finally:
            store.close()

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
        assert got.extracted_at == rec.extracted_at
        assert got.model_version == rec.model_version
        assert got.extractor_name == rec.extractor_name

    def test_get_missing_returns_none(self, tmp_path: Path) -> None:
        from nexus.db.t2.document_aspects import DocumentAspects

        store = DocumentAspects(tmp_path / "t2.db")
        try:
            assert store.get("knowledge__nope", "/missing.pdf") is None
        finally:
            store.close()

    def test_json_fields_persist_as_json_strings(self, tmp_path: Path) -> None:
        """Datasets/baselines (lists) and extras (dict) must serialize
        to TEXT JSON. Verify by reading the raw column.
        """
        from nexus.db.t2.document_aspects import DocumentAspects

        store = DocumentAspects(tmp_path / "t2.db")
        try:
            store.upsert(_make_record())
            row = store.conn.execute(
                "SELECT experimental_datasets, experimental_baselines, extras "
                "FROM document_aspects"
            ).fetchone()
        finally:
            store.close()
        assert json.loads(row[0]) == ["TPC-C", "YCSB"]
        assert json.loads(row[1]) == ["raft", "paxos"]
        assert json.loads(row[2]) == {"venue": "VLDB", "year": 2023}


# ── Idempotent overwrite (RDR Upsert Semantics — load-bearing) ───────────────


class TestIdempotentUpsert:
    """Repeat upsert is a COMPLETE OVERWRITE — no diff/merge, no
    deviation log. The stored row reflects the latest extraction
    verbatim. RDR pins this contract.
    """

    def test_repeat_upsert_overwrites_all_fields(self, tmp_path: Path) -> None:
        from nexus.db.t2.document_aspects import DocumentAspects

        store = DocumentAspects(tmp_path / "t2.db")
        try:
            store.upsert(_make_record(problem_formulation="Old framing"))
            store.upsert(
                _make_record(
                    problem_formulation="New framing",
                    confidence=0.55,
                    extras={"venue": "SOSP", "year": 2024},
                ),
            )
            got = store.get("knowledge__delos", "/papers/p1.pdf")
        finally:
            store.close()

        assert got is not None
        assert got.problem_formulation == "New framing"
        assert got.confidence == 0.55
        assert got.extras == {"venue": "SOSP", "year": 2024}

    def test_repeat_upsert_does_not_duplicate_rows(self, tmp_path: Path) -> None:
        from nexus.db.t2.document_aspects import DocumentAspects

        store = DocumentAspects(tmp_path / "t2.db")
        try:
            store.upsert(_make_record())
            store.upsert(_make_record(confidence=0.1))
            store.upsert(_make_record(confidence=0.5))
            count = store.conn.execute(
                "SELECT COUNT(*) FROM document_aspects"
            ).fetchone()[0]
        finally:
            store.close()
        assert count == 1

    def test_distinct_source_paths_create_distinct_rows(self, tmp_path: Path) -> None:
        from nexus.db.t2.document_aspects import DocumentAspects

        store = DocumentAspects(tmp_path / "t2.db")
        try:
            store.upsert(_make_record(source_path="/papers/p1.pdf"))
            store.upsert(_make_record(source_path="/papers/p2.pdf"))
            count = store.conn.execute(
                "SELECT COUNT(*) FROM document_aspects"
            ).fetchone()[0]
        finally:
            store.close()
        assert count == 2

    def test_distinct_collections_create_distinct_rows(self, tmp_path: Path) -> None:
        from nexus.db.t2.document_aspects import DocumentAspects

        store = DocumentAspects(tmp_path / "t2.db")
        try:
            store.upsert(_make_record(collection="knowledge__a"))
            store.upsert(_make_record(collection="knowledge__b"))
            count = store.conn.execute(
                "SELECT COUNT(*) FROM document_aspects"
            ).fetchone()[0]
        finally:
            store.close()
        assert count == 2


# ── List + delete ────────────────────────────────────────────────────────────


class TestListDelete:
    def test_list_by_collection(self, tmp_path: Path) -> None:
        from nexus.db.t2.document_aspects import DocumentAspects

        store = DocumentAspects(tmp_path / "t2.db")
        try:
            store.upsert(_make_record(source_path="/p1.pdf"))
            store.upsert(_make_record(source_path="/p2.pdf"))
            store.upsert(_make_record(collection="knowledge__other", source_path="/p3.pdf"))
            rows = store.list_by_collection("knowledge__delos")
        finally:
            store.close()
        assert len(rows) == 2
        paths = sorted(r.source_path for r in rows)
        assert paths == ["/p1.pdf", "/p2.pdf"]

    def test_list_pagination(self, tmp_path: Path) -> None:
        from nexus.db.t2.document_aspects import DocumentAspects

        store = DocumentAspects(tmp_path / "t2.db")
        try:
            for i in range(5):
                store.upsert(_make_record(source_path=f"/p{i}.pdf"))
            page1 = store.list_by_collection("knowledge__delos", limit=2, offset=0)
            page2 = store.list_by_collection("knowledge__delos", limit=2, offset=2)
            page3 = store.list_by_collection("knowledge__delos", limit=2, offset=4)
        finally:
            store.close()
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

    def test_delete_removes_row(self, tmp_path: Path) -> None:
        from nexus.db.t2.document_aspects import DocumentAspects

        store = DocumentAspects(tmp_path / "t2.db")
        try:
            store.upsert(_make_record())
            store.delete("knowledge__delos", "/papers/p1.pdf")
            assert store.get("knowledge__delos", "/papers/p1.pdf") is None
        finally:
            store.close()

    def test_delete_missing_is_no_op(self, tmp_path: Path) -> None:
        from nexus.db.t2.document_aspects import DocumentAspects

        store = DocumentAspects(tmp_path / "t2.db")
        try:
            store.delete("knowledge__nope", "/missing.pdf")  # must not raise
        finally:
            store.close()


# ── Extractor-version filter ─────────────────────────────────────────────────


class TestVersionFilter:
    """``list_by_extractor_version(extractor_name, max_version)`` returns
    rows whose ``extractor_name`` matches and ``model_version`` is
    strictly less than ``max_version``. Used by re-extraction logic to
    find documents whose aspects were captured by an older model and
    should be re-run.
    """

    def test_filter_returns_rows_below_version(self, tmp_path: Path) -> None:
        from nexus.db.t2.document_aspects import DocumentAspects

        store = DocumentAspects(tmp_path / "t2.db")
        try:
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
        finally:
            store.close()
        paths = sorted(r.source_path for r in rows)
        assert paths == ["/mid.pdf", "/old.pdf"]

    def test_filter_strict_less_than_excludes_equal(self, tmp_path: Path) -> None:
        """Filter is STRICT less-than. A row with ``model_version`` equal
        to the threshold is NOT returned (no duplicate re-extraction).
        """
        from nexus.db.t2.document_aspects import DocumentAspects

        store = DocumentAspects(tmp_path / "t2.db")
        try:
            store.upsert(_make_record(
                source_path="/x.pdf", model_version="claude-haiku-4-5-20251001",
            ))
            rows = store.list_by_extractor_version(
                "haiku-aspect-v1", "claude-haiku-4-5-20251001",
            )
        finally:
            store.close()
        assert rows == []

    def test_filter_scopes_to_extractor_name(self, tmp_path: Path) -> None:
        """The filter is scoped: rows from a different ``extractor_name``
        are not considered, even if ``model_version`` is below threshold.
        """
        from nexus.db.t2.document_aspects import DocumentAspects

        store = DocumentAspects(tmp_path / "t2.db")
        try:
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
        finally:
            store.close()
        assert [r.source_path for r in rows] == ["/a.pdf"]


# ── Facade wiring ────────────────────────────────────────────────────────────


class TestFacadeWiring:
    """``T2Database`` exposes the new store as ``db.document_aspects``
    alongside the existing ``db.memory`` / ``db.plans`` / ``db.taxonomy``
    / ``db.telemetry`` / ``db.chash_index``.
    """

    def test_t2database_exposes_document_aspects(self, tmp_path: Path) -> None:
        with T2Database(tmp_path / "t2.db") as db:
            assert hasattr(db, "document_aspects")
            from nexus.db.t2.document_aspects import DocumentAspects
            assert isinstance(db.document_aspects, DocumentAspects)

    def test_facade_round_trip_through_property(self, tmp_path: Path) -> None:
        with T2Database(tmp_path / "t2.db") as db:
            db.document_aspects.upsert(_make_record())
            got = db.document_aspects.get("knowledge__delos", "/papers/p1.pdf")
            assert got is not None
            assert got.problem_formulation.startswith("Sharded")

    def test_t2database_close_releases_document_aspects(self, tmp_path: Path) -> None:
        """The facade's ``close()`` must also close the new store's
        connection — verified by reopening the same path and confirming
        the new instance can take the WAL-write lock without contention.
        """
        path = tmp_path / "t2.db"
        with T2Database(path) as db:
            db.document_aspects.upsert(_make_record())
        # Reopen — no leftover connection blocking the WAL-write lock.
        with T2Database(path) as db2:
            got = db2.document_aspects.get("knowledge__delos", "/papers/p1.pdf")
            assert got is not None


# ── Migration sanity ─────────────────────────────────────────────────────────


class TestMigration:
    """The migration entry idempotently creates the table and is
    no-op when the table already exists (CREATE IF NOT EXISTS pattern).
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
