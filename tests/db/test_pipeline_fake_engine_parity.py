# SPDX-License-Identifier: AGPL-3.0-or-later
"""Semantic parity for the pipeline fake engine (RDR-186 .16).

Ports the retired ``test_pipeline_buffer.py`` scenarios to run through the
REAL ``HttpPipelineDB`` against ``FakePipelineEngine`` — keeping the fake
honest to the server contract the stage tests now stand on (the
authoritative server pins are the Java ``PipelineHandlerTest``). SQLite-
specific scenarios (WAL, per-thread connections, schema idempotency) died
with the substrate.
"""
from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

import pytest

from tests.pipeline_fake_engine import FakePipelineEngine, make_fake_engine_db

_T0 = datetime(2026, 7, 18, 12, 0, 0, tzinfo=UTC)


class _Clock:
    """Fixed, manually-advanced clock (house rule: no wall-clock sleeps)."""

    def __init__(self) -> None:
        self.now = _T0

    def __call__(self) -> datetime:
        return self.now

    def advance(self, **kwargs: float) -> None:
        self.now += timedelta(**kwargs)


@pytest.fixture()
def clock() -> _Clock:
    return _Clock()


@pytest.fixture()
def rig(clock: _Clock):
    return make_fake_engine_db(clock=clock)


@pytest.fixture()
def db(rig):
    return rig[0]


@pytest.fixture()
def engine(rig) -> FakePipelineEngine:
    return rig[1]


class TestPipeline:
    @pytest.mark.parametrize("setup,expected", [
        ("new", "created"),
        ("running_recent", "skip"),
        ("running_stale", "resuming"),
        ("failed", "resuming"),
        ("completed", "skip"),
    ])
    def test_create_pipeline(self, db, clock, setup, expected):
        if setup == "new":
            assert db.create_pipeline("h1", "/a.pdf", "docs__test") == expected
            return
        db.create_pipeline("h1", "/a.pdf", "docs__test")
        if setup == "running_stale":
            clock.advance(minutes=6)
        elif setup == "failed":
            db.mark_failed("h1", "boom")
        elif setup == "completed":
            db.mark_completed("h1")
        assert db.create_pipeline("h1", "/a.pdf", "docs__test") == expected

    def test_get_pipeline_state(self, db):
        db.create_pipeline("h1", "/a.pdf", "docs__test")
        state = db.get_pipeline_state("h1")
        assert state["status"] == "running" and state["pdf_path"] == "/a.pdf"
        assert state["collection"] == "docs__test" and state["pages_extracted"] == 0

    def test_get_pipeline_state_missing(self, db):
        assert db.get_pipeline_state("nonexistent") is None

    @pytest.mark.parametrize("kwargs,check_field,check_val,untouched_field,untouched_val", [
        ({"pages_extracted": 5, "chunks_created": 20}, "pages_extracted", 5, None, None),
        ({"chunks_uploaded": 10}, "chunks_uploaded", 10, "pages_extracted", 0),
    ])
    def test_update_progress(self, db, kwargs, check_field, check_val, untouched_field, untouched_val):
        db.create_pipeline("h1", "/a.pdf", "docs__test")
        db.update_progress("h1", **kwargs)
        state = db.get_pipeline_state("h1")
        assert state[check_field] == check_val
        if untouched_field:
            assert state[untouched_field] == untouched_val


class TestPages:
    def test_write_and_read(self, db):
        db.write_page("h1", 0, "first page")
        db.write_page("h1", 1, "second page")
        pages = db.read_pages("h1")
        assert len(pages) == 2 and pages[0]["page_text"] == "first page"

    def test_write_with_metadata(self, db):
        meta = {"font": "Times", "tables": 2}
        db.write_page("h1", 0, "text", metadata=meta)
        assert json.loads(db.read_pages("h1")[0]["metadata_json"]) == meta

    def test_read_empty(self, db):
        assert db.read_pages("nonexistent") == []

    def test_write_idempotent(self, db):
        """Page writes REPLACE (latest text wins) — even within one
        buffered batch."""
        db.write_page("h1", 0, "first")
        db.write_page("h1", 0, "updated")
        pages = db.read_pages("h1")
        assert len(pages) == 1 and pages[0]["page_text"] == "updated"

    def test_read_pages_from_offset(self, db):
        for i in range(4):
            db.write_page("h1", i, f"p{i}")
        rows = db.read_pages_from("h1", 2)
        assert [r["page_index"] for r in rows] == [2, 3]


class TestChunks:
    def test_write_and_read(self, db):
        db.write_chunk("h1", 0, "chunk text", "cid-0")
        db.write_chunk("h1", 1, "chunk text 2", "cid-1")
        chunks = db.read_ready_chunks("h1")
        assert len(chunks) == 2 and chunks[0]["chunk_text"] == "chunk text"

    def test_write_with_metadata(self, db):
        meta = {"source_page": 3}
        db.write_chunk("h1", 0, "text", "cid-0", metadata=meta)
        assert json.loads(db.read_ready_chunks("h1")[0]["metadata_json"]) == meta

    @pytest.mark.parametrize("total,mark_indices,expected_ready", [
        (2, [0], 1), (5, [0, 1, 2], 2),
    ])
    def test_mark_uploaded(self, db, total, mark_indices, expected_ready):
        for i in range(total):
            db.write_chunk("h1", i, f"text{i}", f"cid-{i}")
        db.mark_uploaded("h1", mark_indices)
        assert len(db.read_ready_chunks("h1")) == expected_ready

    def test_write_idempotent_preserves_original(self, db):
        """Chunk writes IGNORE on conflict (first write wins) — an
        existing row is never overwritten (idempotent resume)."""
        db.write_chunk("h1", 0, "original", "cid-0")
        db.write_chunk("h1", 0, "should-be-ignored", "cid-0-new")
        chunks = db.read_ready_chunks("h1")
        assert len(chunks) == 1 and chunks[0]["chunk_text"] == "original"

    def test_write_ignore_preserves_embedding(self, db):
        db.write_chunk("h1", 0, "text", "cid-0", embedding=b"\x00\x01\x02")
        db.flush("h1")
        db.write_chunk("h1", 0, "text", "cid-0")  # resume replay, no embedding
        rows = db.read_ready_chunks("h1")
        assert rows[0]["embedding"] == b"\x00\x01\x02"

    def test_counts_blank_hash_is_zero_not_global(self, db, engine):
        """PipelineHandler.handleCounts contract: blank/absent content_hash
        yields embedded_chunks=0, never a cross-pipeline sum (.16 critic
        Significant #3 — the drift class the parity suite must pin)."""
        db.write_chunk("h1", 0, "t", "cid-0", embedding=b"\x01")
        db.flush("h1")
        assert engine.counts({})["embedded_chunks"] == 0
        assert engine.counts({"content_hash": ""})["embedded_chunks"] == 0
        assert engine.counts({"content_hash": "h1"})["embedded_chunks"] == 1

    def test_uploadable_requires_embedding_sentinel_counts(self, db):
        """Uploadable = embedding present and not uploaded; the b'' service
        sentinel COUNTS as present (nexus-9n1u3) — only SQL-NULL means
        not-embedded."""
        db.write_chunk("h1", 0, "no-embed", "cid-0", embedding=None)
        db.write_chunk("h1", 1, "sentinel", "cid-1", embedding=b"")
        db.write_chunk("h1", 2, "vector", "cid-2", embedding=b"\x01")
        rows = db.read_uploadable_chunks("h1")
        assert [r["chunk_index"] for r in rows] == [1, 2]
        assert db.count_embedded_chunks("h1") == 2


class TestCleanup:
    def test_delete_pipeline_data(self, db):
        db.create_pipeline("h1", "/a.pdf", "docs__test")
        db.write_page("h1", 0, "page text")
        db.write_chunk("h1", 0, "chunk text", "cid-0")
        db.delete_pipeline_data("h1")
        assert db.get_pipeline_state("h1") is None
        assert db.read_pages("h1") == [] and db.read_ready_chunks("h1") == []

    def test_delete_nonexistent(self, db):
        db.delete_pipeline_data("ghost")

    def test_delete_pipeline_data_for_collection(self, db):
        db.create_pipeline("h_keep", "/k.pdf", "docs__keep")
        db.write_page("h_keep", 0, "keep")
        db.create_pipeline("h_drop", "/d.pdf", "knowledge__delos")
        db.write_page("h_drop", 0, "drop")
        db.flush_all()

        assert db.delete_pipeline_data_for_collection("knowledge__delos") == 1
        assert db.get_pipeline_state("h_drop") is None
        assert db.read_pages("h_drop") == []
        assert db.get_pipeline_state("h_keep") is not None
        assert len(db.read_pages("h_keep")) == 1

    def test_delete_pipeline_data_for_collection_no_rows(self, db):
        assert db.delete_pipeline_data_for_collection("docs__ghost") == 0

    def test_heartbeat_updated(self, db, clock):
        db.create_pipeline("h1", "/a.pdf", "docs__test")
        t0 = db.get_pipeline_state("h1")["updated_at"]
        clock.advance(seconds=1)
        db.update_progress("h1", pages_extracted=1)
        assert db.get_pipeline_state("h1")["updated_at"] > t0

    def test_bad_field_raises(self, db):
        db.create_pipeline("h1", "/a.pdf", "docs__test")
        with pytest.raises(Exception, match="[Uu]nknown progress fields"):
            db.update_progress("h1", nonexistent_field=1)

    def test_clear_orphan_wal_preserves_pipeline_row(self, db):
        """nexus-2fyb C-int-2: WAL clear drops pages+chunks but keeps the
        failed pipeline row's audit trail."""
        db.create_pipeline("h1", "/a.pdf", "docs__test")
        db.write_page("h1", 0, "p")
        db.write_chunk("h1", 0, "c", "cid-0")
        db.mark_failed("h1", "math pdf without MinerU")
        db.clear_orphan_wal("h1")
        assert db.read_pages("h1") == [] and db.read_ready_chunks("h1") == []
        state = db.get_pipeline_state("h1")
        assert state["status"] == "failed" and state["error"] == "math pdf without MinerU"


class TestScanOrphanedPipelines:
    @pytest.mark.parametrize("setup,expected_orphan", [
        ("missing_pdf", True),
        ("stale_running", True),
        ("recent_running", False),
        ("completed_old", False),
        ("failed_missing_pdf", True),
    ])
    def test_orphan_detection(self, db, clock, setup, expected_orphan):
        pdf = "/nonexistent/gone.pdf" if "missing" in setup else __file__
        db.create_pipeline("h1", pdf, "docs__test")
        if "stale" in setup:
            clock.advance(minutes=6)
        elif "completed" in setup:
            db.mark_completed("h1")
            clock.advance(minutes=60)
        elif "failed" in setup:
            db.mark_failed("h1", "crash")
        orphans = db.scan_orphaned_pipelines()
        assert ("h1" in orphans) == expected_orphan

    def test_delete_cleans_all_tables(self, db):
        db.create_pipeline("h1", "/nonexistent/gone.pdf", "docs__test")
        db.write_page("h1", 0, "page text")
        db.write_chunk("h1", 0, "chunk text", "cid-0")
        orphans = db.scan_orphaned_pipelines(delete=True)
        assert "h1" in orphans
        assert db.get_pipeline_state("h1") is None and db.read_pages("h1") == []

    def test_empty_database(self, db):
        assert db.scan_orphaned_pipelines() == []
