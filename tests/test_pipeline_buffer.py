# SPDX-License-Identifier: AGPL-3.0-or-later
from __future__ import annotations

import json
import threading
import time
from pathlib import Path

import pytest

from nexus.pipeline_buffer import PipelineDB


@pytest.fixture()
def db(tmp_path: Path) -> PipelineDB:
    return PipelineDB(tmp_path / "pipeline.db")


# ── Schema & WAL ─────────────────────────────────────────────────────────────

class TestSchema:
    def test_wal_mode_enabled(self, db):
        assert db._conn().execute("PRAGMA journal_mode").fetchone()[0].lower() == "wal"

    def test_tables_created(self, db):
        rows = db._conn().execute(
            "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
        ).fetchall()
        assert {"pdf_pages", "pdf_chunks", "pdf_pipeline"} <= {r[0] for r in rows}

    def test_idempotent_schema(self, tmp_path):
        path = tmp_path / "pipeline.db"
        PipelineDB(path)
        PipelineDB(path)


# ── Thread Safety ────────────────────────────────────────────────────────────

class TestThreadSafety:
    def test_per_thread_connections(self, db):
        ids: dict[str, int] = {}
        def capture(name):
            ids[name] = id(db._conn())
        threads = [threading.Thread(target=capture, args=(n,)) for n in ("a", "b")]
        for t in threads: t.start()
        for t in threads: t.join()
        assert ids["a"] != ids["b"]

    def test_concurrent_writes(self, db):
        db.create_pipeline("abc123", "/test.pdf", "docs__test")
        errors: list[Exception] = []
        def write_page(i):
            try: db.write_page("abc123", i, f"page {i} text")
            except Exception as e: errors.append(e)
        threads = [threading.Thread(target=write_page, args=(i,)) for i in range(10)]
        for t in threads: t.start()
        for t in threads: t.join()
        assert errors == [] and len(db.read_pages("abc123")) == 10


# ── Pipeline CRUD ────────────────────────────────────────────────────────────

class TestPipeline:
    def _set_status(self, db, hash_, status=None, stale=False):
        conn = db._conn()
        if status:
            conn.execute("UPDATE pdf_pipeline SET status = ? WHERE content_hash = ?", (status, hash_))
        if stale:
            conn.execute(
                "UPDATE pdf_pipeline SET updated_at = '2020-01-01T00:00:00+00:00' WHERE content_hash = ?",
                (hash_,))
        conn.commit()

    @pytest.mark.parametrize("setup,expected", [
        ("new", "created"),
        ("running_recent", "skip"),
        ("running_stale", "resuming"),
        ("failed", "resuming"),
        ("completed", "skip"),
    ])
    def test_create_pipeline(self, db, setup, expected):
        if setup == "new":
            assert db.create_pipeline("h1", "/a.pdf", "docs__test") == expected
            return
        db.create_pipeline("h1", "/a.pdf", "docs__test")
        if setup == "running_stale":
            self._set_status(db, "h1", stale=True)
        elif setup == "failed":
            self._set_status(db, "h1", status="failed")
        elif setup == "completed":
            self._set_status(db, "h1", status="completed")
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


# ── Page CRUD ────────────────────────────────────────────────────────────────

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
        db.write_page("h1", 0, "first")
        db.write_page("h1", 0, "updated")
        pages = db.read_pages("h1")
        assert len(pages) == 1 and pages[0]["page_text"] == "updated"


# ── Chunk CRUD ───────────────────────────────────────────────────────────────

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
        db.write_chunk("h1", 0, "original", "cid-0")
        db.write_chunk("h1", 0, "should-be-ignored", "cid-0-new")
        chunks = db.read_ready_chunks("h1")
        assert len(chunks) == 1 and chunks[0]["chunk_text"] == "original"

    def test_write_ignore_preserves_embedding(self, db):
        db.write_chunk("h1", 0, "text", "cid-0")
        conn = db._conn()
        conn.execute(
            "UPDATE pdf_chunks SET embedding = ? WHERE content_hash = ? AND chunk_index = ?",
            (b"\x00\x01\x02", "h1", 0))
        conn.commit()
        db.write_chunk("h1", 0, "text", "cid-0")
        row = conn.execute(
            "SELECT embedding FROM pdf_chunks WHERE content_hash = ? AND chunk_index = ?",
            ("h1", 0)).fetchone()
        assert row[0] == b"\x00\x01\x02"


# ── Cleanup & Heartbeat & Edge Cases ────────────────────────────────────────

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

    def test_heartbeat_updated(self, db):
        db.create_pipeline("h1", "/a.pdf", "docs__test")
        t0 = db.get_pipeline_state("h1")["updated_at"]
        time.sleep(0.05)
        db.update_progress("h1", pages_extracted=1)
        assert db.get_pipeline_state("h1")["updated_at"] > t0

    def test_bad_field_raises(self, db):
        db.create_pipeline("h1", "/a.pdf", "docs__test")
        with pytest.raises(ValueError, match="Unknown progress fields"):
            db.update_progress("h1", nonexistent_field=1)

    def test_no_fields_noop(self, db):
        db.create_pipeline("h1", "/a.pdf", "docs__test")
        t0 = db.get_pipeline_state("h1")["updated_at"]
        db.update_progress("h1")
        assert db.get_pipeline_state("h1")["updated_at"] == t0


# ── Orphan Scan ──────────────────────────────────────────────────────────────

class TestScanOrphanedPipelines:
    @pytest.mark.parametrize("setup,expected_orphan", [
        ("missing_pdf", True),
        ("stale_running", True),
        ("recent_running", False),
        ("completed_old", False),
        ("failed_missing_pdf", True),
    ])
    def test_orphan_detection(self, db, setup, expected_orphan):
        pdf = "/nonexistent/gone.pdf" if "missing" in setup else __file__
        db.create_pipeline("h1", pdf, "docs__test")
        conn = db._conn()
        if "stale" in setup:
            conn.execute(
                "UPDATE pdf_pipeline SET updated_at = '2020-01-01T00:00:00+00:00' WHERE content_hash = ?",
                ("h1",))
            conn.commit()
        elif "completed" in setup:
            db.mark_completed("h1")
            conn.execute(
                "UPDATE pdf_pipeline SET updated_at = '2020-01-01T00:00:00+00:00' WHERE content_hash = ?",
                ("h1",))
            conn.commit()
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
