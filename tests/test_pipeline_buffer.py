# SPDX-License-Identifier: AGPL-3.0-or-later
"""Tests for PipelineDB buffer module (nexus-qwxz.1)."""
from __future__ import annotations

import json
import threading
import time
from datetime import UTC, datetime
from pathlib import Path

import pytest

from nexus.pipeline_buffer import PipelineDB


@pytest.fixture()
def db(tmp_path: Path) -> PipelineDB:
    """Create a PipelineDB backed by a temp directory."""
    return PipelineDB(tmp_path / "pipeline.db")


# ── Schema & WAL ─────────────────────────────────────────────────────────────


class TestSchema:
    def test_wal_mode_enabled(self, db: PipelineDB) -> None:
        conn = db._conn()
        mode = conn.execute("PRAGMA journal_mode").fetchone()[0]
        assert mode.lower() == "wal"

    def test_tables_created(self, db: PipelineDB) -> None:
        conn = db._conn()
        rows = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
        ).fetchall()
        names = {r[0] for r in rows}
        assert {"pdf_pages", "pdf_chunks", "pdf_pipeline"} <= names

    def test_idempotent_schema(self, tmp_path: Path) -> None:
        """Opening twice on the same DB does not raise."""
        path = tmp_path / "pipeline.db"
        PipelineDB(path)
        PipelineDB(path)  # should not raise


# ── Thread Safety ────────────────────────────────────────────────────────────


class TestThreadSafety:
    def test_per_thread_connections(self, db: PipelineDB) -> None:
        """Each thread gets its own connection."""
        ids: dict[str, int] = {}

        def capture(name: str) -> None:
            ids[name] = id(db._conn())

        t1 = threading.Thread(target=capture, args=("a",))
        t2 = threading.Thread(target=capture, args=("b",))
        t1.start()
        t2.start()
        t1.join()
        t2.join()

        assert ids["a"] != ids["b"], "threads must get distinct connections"

    def test_concurrent_writes(self, db: PipelineDB) -> None:
        """Multiple threads can write pages concurrently without errors."""
        hash_ = "abc123"
        db.create_pipeline(hash_, "/test.pdf", "docs__test")
        errors: list[Exception] = []

        def write_page(page_idx: int) -> None:
            try:
                db.write_page(hash_, page_idx, f"page {page_idx} text")
            except Exception as exc:
                errors.append(exc)

        threads = [threading.Thread(target=write_page, args=(i,)) for i in range(10)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert errors == []
        pages = db.read_pages(hash_)
        assert len(pages) == 10


# ── Pipeline CRUD ────────────────────────────────────────────────────────────


class TestPipeline:
    def test_create_pipeline_new(self, db: PipelineDB) -> None:
        result = db.create_pipeline("hash1", "/a.pdf", "docs__test")
        assert result == "created"

    def test_create_pipeline_skip_running(self, db: PipelineDB) -> None:
        """Running pipeline with recent heartbeat → skip."""
        db.create_pipeline("hash1", "/a.pdf", "docs__test")
        result = db.create_pipeline("hash1", "/a.pdf", "docs__test")
        assert result == "skip"

    def test_create_pipeline_resume_stale(self, db: PipelineDB) -> None:
        """Running pipeline with old heartbeat → resuming (crashed)."""
        db.create_pipeline("hash1", "/a.pdf", "docs__test")
        # Backdate updated_at to make it stale
        conn = db._conn()
        conn.execute(
            "UPDATE pdf_pipeline SET updated_at = '2020-01-01T00:00:00+00:00' WHERE content_hash = ?",
            ("hash1",),
        )
        conn.commit()
        result = db.create_pipeline("hash1", "/a.pdf", "docs__test")
        assert result == "resuming"

    def test_create_pipeline_resume_failed(self, db: PipelineDB) -> None:
        """Failed pipeline → resuming."""
        db.create_pipeline("hash1", "/a.pdf", "docs__test")
        conn = db._conn()
        conn.execute(
            "UPDATE pdf_pipeline SET status = 'failed' WHERE content_hash = ?",
            ("hash1",),
        )
        conn.commit()
        result = db.create_pipeline("hash1", "/a.pdf", "docs__test")
        assert result == "resuming"

    def test_create_pipeline_skip_completed(self, db: PipelineDB) -> None:
        """Completed pipeline → skip."""
        db.create_pipeline("hash1", "/a.pdf", "docs__test")
        conn = db._conn()
        conn.execute(
            "UPDATE pdf_pipeline SET status = 'completed' WHERE content_hash = ?",
            ("hash1",),
        )
        conn.commit()
        result = db.create_pipeline("hash1", "/a.pdf", "docs__test")
        assert result == "skip"

    def test_get_pipeline_state(self, db: PipelineDB) -> None:
        db.create_pipeline("hash1", "/a.pdf", "docs__test")
        state = db.get_pipeline_state("hash1")
        assert state is not None
        assert state["status"] == "running"
        assert state["pdf_path"] == "/a.pdf"
        assert state["collection"] == "docs__test"
        assert state["pages_extracted"] == 0

    def test_get_pipeline_state_missing(self, db: PipelineDB) -> None:
        assert db.get_pipeline_state("nonexistent") is None

    def test_update_progress(self, db: PipelineDB) -> None:
        db.create_pipeline("hash1", "/a.pdf", "docs__test")
        db.update_progress("hash1", pages_extracted=5, chunks_created=20)
        state = db.get_pipeline_state("hash1")
        assert state["pages_extracted"] == 5
        assert state["chunks_created"] == 20

    def test_update_progress_partial(self, db: PipelineDB) -> None:
        """Only specified fields are updated."""
        db.create_pipeline("hash1", "/a.pdf", "docs__test")
        db.update_progress("hash1", chunks_uploaded=10)
        state = db.get_pipeline_state("hash1")
        assert state["chunks_uploaded"] == 10
        assert state["pages_extracted"] == 0  # untouched


# ── Page CRUD ────────────────────────────────────────────────────────────────


class TestPages:
    def test_write_and_read_pages(self, db: PipelineDB) -> None:
        db.write_page("hash1", 0, "first page")
        db.write_page("hash1", 1, "second page")
        pages = db.read_pages("hash1")
        assert len(pages) == 2
        assert pages[0]["page_index"] == 0
        assert pages[0]["page_text"] == "first page"
        assert pages[1]["page_index"] == 1

    def test_write_page_with_metadata(self, db: PipelineDB) -> None:
        meta = {"font": "Times", "tables": 2}
        db.write_page("hash1", 0, "text", metadata=meta)
        pages = db.read_pages("hash1")
        assert json.loads(pages[0]["metadata_json"]) == meta

    def test_read_pages_empty(self, db: PipelineDB) -> None:
        assert db.read_pages("nonexistent") == []

    def test_write_page_idempotent(self, db: PipelineDB) -> None:
        """Writing the same page twice does not raise (INSERT OR REPLACE)."""
        db.write_page("hash1", 0, "first")
        db.write_page("hash1", 0, "updated")
        pages = db.read_pages("hash1")
        assert len(pages) == 1
        assert pages[0]["page_text"] == "updated"


# ── Chunk CRUD ───────────────────────────────────────────────────────────────


class TestChunks:
    def test_write_and_read_chunks(self, db: PipelineDB) -> None:
        db.write_chunk("hash1", 0, "chunk text", "chunk-id-0")
        db.write_chunk("hash1", 1, "chunk text 2", "chunk-id-1")
        chunks = db.read_ready_chunks("hash1")
        assert len(chunks) == 2
        assert chunks[0]["chunk_text"] == "chunk text"

    def test_write_chunk_with_metadata(self, db: PipelineDB) -> None:
        meta = {"source_page": 3}
        db.write_chunk("hash1", 0, "text", "cid-0", metadata=meta)
        chunks = db.read_ready_chunks("hash1")
        assert json.loads(chunks[0]["metadata_json"]) == meta

    def test_mark_uploaded(self, db: PipelineDB) -> None:
        db.write_chunk("hash1", 0, "text", "cid-0")
        db.write_chunk("hash1", 1, "text2", "cid-1")
        db.mark_uploaded("hash1", [0])
        # read_ready_chunks returns only un-uploaded
        ready = db.read_ready_chunks("hash1")
        assert len(ready) == 1
        assert ready[0]["chunk_index"] == 1

    def test_mark_uploaded_batch(self, db: PipelineDB) -> None:
        for i in range(5):
            db.write_chunk("hash1", i, f"text{i}", f"cid-{i}")
        db.mark_uploaded("hash1", [0, 1, 2])
        ready = db.read_ready_chunks("hash1")
        assert len(ready) == 2

    def test_write_chunk_idempotent(self, db: PipelineDB) -> None:
        """INSERT OR IGNORE keeps the original row (preserves embeddings on resume)."""
        db.write_chunk("hash1", 0, "original", "cid-0")
        db.write_chunk("hash1", 0, "should-be-ignored", "cid-0-new")
        chunks = db.read_ready_chunks("hash1")
        assert len(chunks) == 1
        assert chunks[0]["chunk_text"] == "original"
        assert chunks[0]["chunk_id"] == "cid-0"

    def test_write_chunk_ignore_preserves_embedding(self, db: PipelineDB) -> None:
        """A duplicate write must not wipe an embedding set by the embed step."""
        db.write_chunk("hash1", 0, "text", "cid-0")
        # Simulate embed step writing an embedding
        conn = db._conn()
        conn.execute(
            "UPDATE pdf_chunks SET embedding = ? WHERE content_hash = ? AND chunk_index = ?",
            (b"\x00\x01\x02", "hash1", 0),
        )
        conn.commit()
        # Duplicate write from chunker — must be ignored
        db.write_chunk("hash1", 0, "text", "cid-0")
        row = conn.execute(
            "SELECT embedding FROM pdf_chunks WHERE content_hash = ? AND chunk_index = ?",
            ("hash1", 0),
        ).fetchone()
        assert row[0] == b"\x00\x01\x02"


# ── Cleanup ──────────────────────────────────────────────────────────────────


class TestCleanup:
    def test_delete_pipeline_data(self, db: PipelineDB) -> None:
        hash_ = "hash1"
        db.create_pipeline(hash_, "/a.pdf", "docs__test")
        db.write_page(hash_, 0, "page text")
        db.write_chunk(hash_, 0, "chunk text", "cid-0")

        db.delete_pipeline_data(hash_)

        assert db.get_pipeline_state(hash_) is None
        assert db.read_pages(hash_) == []
        assert db.read_ready_chunks(hash_) == []

    def test_delete_nonexistent(self, db: PipelineDB) -> None:
        """Deleting nonexistent hash does not raise."""
        db.delete_pipeline_data("ghost")


# ── Heartbeat ────────────────────────────────────────────────────────────────


class TestHeartbeat:
    def test_update_progress_updates_heartbeat(self, db: PipelineDB) -> None:
        db.create_pipeline("hash1", "/a.pdf", "docs__test")
        state_before = db.get_pipeline_state("hash1")
        time.sleep(0.05)
        db.update_progress("hash1", pages_extracted=1)
        state_after = db.get_pipeline_state("hash1")
        assert state_after["updated_at"] > state_before["updated_at"]


# ── Edge Cases ───────────────────────────────────────────────────────────────


class TestEdgeCases:
    def test_update_progress_bad_field(self, db: PipelineDB) -> None:
        db.create_pipeline("hash1", "/a.pdf", "docs__test")
        with pytest.raises(ValueError, match="Unknown progress fields"):
            db.update_progress("hash1", nonexistent_field=1)

    def test_update_progress_no_fields(self, db: PipelineDB) -> None:
        """Calling with no fields is a no-op (no heartbeat refresh)."""
        db.create_pipeline("hash1", "/a.pdf", "docs__test")
        state_before = db.get_pipeline_state("hash1")
        db.update_progress("hash1")
        state_after = db.get_pipeline_state("hash1")
        assert state_after["updated_at"] == state_before["updated_at"]


# ── Orphan Scan ──────────────────────────────────────────────────────────────


class TestScanOrphanedPipelines:
    def test_detects_missing_pdf(self, db: PipelineDB) -> None:
        """Entry with non-existent pdf_path is orphaned."""
        db.create_pipeline("hash1", "/nonexistent/gone.pdf", "docs__test")
        orphans = db.scan_orphaned_pipelines()
        assert "hash1" in orphans

    def test_detects_stale_running(self, db: PipelineDB) -> None:
        """Running entry with old heartbeat is orphaned."""
        db.create_pipeline("hash1", __file__, "docs__test")  # use this test file as valid path
        conn = db._conn()
        conn.execute(
            "UPDATE pdf_pipeline SET updated_at = '2020-01-01T00:00:00+00:00' WHERE content_hash = ?",
            ("hash1",),
        )
        conn.commit()
        orphans = db.scan_orphaned_pipelines()
        assert "hash1" in orphans

    def test_recent_running_not_orphaned(self, db: PipelineDB) -> None:
        """Running entry with recent heartbeat is NOT orphaned."""
        db.create_pipeline("hash1", __file__, "docs__test")
        orphans = db.scan_orphaned_pipelines()
        assert orphans == []

    def test_completed_not_orphaned(self, db: PipelineDB) -> None:
        """Completed entry is never orphaned regardless of age."""
        db.create_pipeline("hash1", __file__, "docs__test")
        db.mark_completed("hash1")
        conn = db._conn()
        conn.execute(
            "UPDATE pdf_pipeline SET updated_at = '2020-01-01T00:00:00+00:00' WHERE content_hash = ?",
            ("hash1",),
        )
        conn.commit()
        orphans = db.scan_orphaned_pipelines()
        assert orphans == []

    def test_failed_with_missing_pdf_is_orphaned(self, db: PipelineDB) -> None:
        """Failed entry where PDF no longer exists is orphaned."""
        db.create_pipeline("hash1", "/nonexistent/gone.pdf", "docs__test")
        db.mark_failed("hash1", "crash")
        orphans = db.scan_orphaned_pipelines()
        assert "hash1" in orphans

    def test_delete_cleans_all_tables(self, db: PipelineDB) -> None:
        """delete=True removes orphan data from all three tables."""
        db.create_pipeline("hash1", "/nonexistent/gone.pdf", "docs__test")
        db.write_page("hash1", 0, "page text")
        db.write_chunk("hash1", 0, "chunk text", "cid-0")

        orphans = db.scan_orphaned_pipelines(delete=True)
        assert "hash1" in orphans
        assert db.get_pipeline_state("hash1") is None
        assert db.read_pages("hash1") == []
        assert db.read_ready_chunks("hash1") == []

    def test_empty_database(self, db: PipelineDB) -> None:
        """No entries → no orphans."""
        assert db.scan_orphaned_pipelines() == []
