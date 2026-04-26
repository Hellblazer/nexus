# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""RDR-089 follow-up (nexus-qeo8): T2 ``aspect_extraction_queue`` store.

Contract tests for the durable WAL-buffer queue feeding the async
aspect-extraction worker. Schema mirrors ``document_aspects`` on the
identity columns ``(collection, source_path)`` so lifecycle is
parallel.

The store is used by:
  * ``aspect_extraction_enqueue_hook`` — registered as a
    ``post_document_hook``; writes one row per fired document.
  * ``AspectExtractionWorker._run_loop`` — drains the queue,
    invokes ``extract_aspects``, writes results to
    ``document_aspects``, and deletes the queue row on success.

States: ``pending`` (initial, awaiting claim) → ``in_progress``
(claimed by a worker) → DELETE on success or ``failed`` after the
last retry. Re-enqueue of an already-failed row resets it to
``pending`` so re-extraction on a new model version re-uses the
queue path.
"""
from __future__ import annotations

import sqlite3
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from nexus.db.t2 import T2Database


# ── Init + schema ────────────────────────────────────────────────────────────


class TestSchema:
    def test_init_creates_table(self, tmp_path: Path) -> None:
        from nexus.db.t2.aspect_extraction_queue import AspectExtractionQueue

        store = AspectExtractionQueue(tmp_path / "t2.db")
        try:
            tables = {
                r[0] for r in store.conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                ).fetchall()
            }
            assert "aspect_extraction_queue" in tables
        finally:
            store.close()

    def test_primary_key_is_collection_and_source_path(self, tmp_path: Path) -> None:
        from nexus.db.t2.aspect_extraction_queue import AspectExtractionQueue

        store = AspectExtractionQueue(tmp_path / "t2.db")
        try:
            pk_cols = sorted(
                r[1]
                for r in store.conn.execute(
                    "PRAGMA table_info(aspect_extraction_queue)"
                ).fetchall()
                if r[5] > 0
            )
        finally:
            store.close()
        assert pk_cols == ["collection", "source_path"]

    def test_status_index_exists(self, tmp_path: Path) -> None:
        """The worker SELECTs by status='pending' on every poll;
        the secondary index on status keeps that an index seek."""
        from nexus.db.t2.aspect_extraction_queue import AspectExtractionQueue

        store = AspectExtractionQueue(tmp_path / "t2.db")
        try:
            indexes = {
                r[0] for r in store.conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='index' "
                    "AND tbl_name='aspect_extraction_queue'"
                ).fetchall()
            }
        finally:
            store.close()
        assert "idx_aspect_queue_status" in indexes


# ── Enqueue ──────────────────────────────────────────────────────────────────


class TestEnqueue:
    def test_enqueue_inserts_pending_row(self, tmp_path: Path) -> None:
        from nexus.db.t2.aspect_extraction_queue import AspectExtractionQueue

        store = AspectExtractionQueue(tmp_path / "t2.db")
        try:
            store.enqueue("knowledge__delos", "/p1.pdf", content_hash="abc")
            row = store.conn.execute(
                "SELECT collection, source_path, content_hash, status, retry_count "
                "FROM aspect_extraction_queue"
            ).fetchone()
        finally:
            store.close()
        assert row == ("knowledge__delos", "/p1.pdf", "abc", "pending", 0)

    def test_enqueue_idempotent_on_duplicate(self, tmp_path: Path) -> None:
        """Re-enqueue at the same (collection, source_path) refreshes the
        row in place (same content_hash → no-op; new content_hash → reset
        retry_count, status='pending', new enqueued_at)."""
        from nexus.db.t2.aspect_extraction_queue import AspectExtractionQueue

        store = AspectExtractionQueue(tmp_path / "t2.db")
        try:
            store.enqueue("knowledge__delos", "/p1.pdf", content_hash="abc")
            store.enqueue("knowledge__delos", "/p1.pdf", content_hash="abc")
            count = store.conn.execute(
                "SELECT COUNT(*) FROM aspect_extraction_queue"
            ).fetchone()[0]
        finally:
            store.close()
        assert count == 1

    def test_enqueue_resets_failed_row_to_pending(self, tmp_path: Path) -> None:
        """A re-enqueue of a row that had previously failed resets it to
        pending so the next worker run will retry. Use case: the
        extractor recipe was upgraded and the row needs re-extraction."""
        from nexus.db.t2.aspect_extraction_queue import AspectExtractionQueue

        store = AspectExtractionQueue(tmp_path / "t2.db")
        try:
            store.enqueue("knowledge__delos", "/p1.pdf")
            store.mark_failed("knowledge__delos", "/p1.pdf", error="boom")
            store.enqueue("knowledge__delos", "/p1.pdf", content_hash="new")
            row = store.conn.execute(
                "SELECT status, retry_count, content_hash "
                "FROM aspect_extraction_queue"
            ).fetchone()
        finally:
            store.close()
        assert row == ("pending", 0, "new")


# ── Claim / mark_done / mark_failed / mark_retry ─────────────────────────────


class TestClaimDone:
    def test_claim_next_returns_pending_row(self, tmp_path: Path) -> None:
        from nexus.db.t2.aspect_extraction_queue import AspectExtractionQueue

        store = AspectExtractionQueue(tmp_path / "t2.db")
        try:
            store.enqueue("knowledge__delos", "/p1.pdf", content_hash="abc")
            row = store.claim_next()
        finally:
            store.close()
        assert row is not None
        assert row.collection == "knowledge__delos"
        assert row.source_path == "/p1.pdf"
        assert row.content_hash == "abc"
        assert row.retry_count == 0

    def test_claim_next_returns_none_when_empty(self, tmp_path: Path) -> None:
        from nexus.db.t2.aspect_extraction_queue import AspectExtractionQueue

        store = AspectExtractionQueue(tmp_path / "t2.db")
        try:
            assert store.claim_next() is None
        finally:
            store.close()

    def test_claim_next_skips_in_progress_rows(self, tmp_path: Path) -> None:
        """A row already claimed (status='in_progress') is NOT re-claimed
        by a second call."""
        from nexus.db.t2.aspect_extraction_queue import AspectExtractionQueue

        store = AspectExtractionQueue(tmp_path / "t2.db")
        try:
            store.enqueue("knowledge__delos", "/p1.pdf")
            store.claim_next()  # claims the only row
            second = store.claim_next()
        finally:
            store.close()
        assert second is None

    def test_claim_next_skips_failed_rows(self, tmp_path: Path) -> None:
        """``failed`` rows are terminal until re-enqueued."""
        from nexus.db.t2.aspect_extraction_queue import AspectExtractionQueue

        store = AspectExtractionQueue(tmp_path / "t2.db")
        try:
            store.enqueue("knowledge__delos", "/p1.pdf")
            store.mark_failed("knowledge__delos", "/p1.pdf", error="x")
            assert store.claim_next() is None
        finally:
            store.close()

    def test_claim_next_marks_status_in_progress(self, tmp_path: Path) -> None:
        from nexus.db.t2.aspect_extraction_queue import AspectExtractionQueue

        store = AspectExtractionQueue(tmp_path / "t2.db")
        try:
            store.enqueue("knowledge__delos", "/p1.pdf")
            store.claim_next()
            row = store.conn.execute(
                "SELECT status FROM aspect_extraction_queue"
            ).fetchone()
        finally:
            store.close()
        assert row[0] == "in_progress"

    def test_mark_done_deletes_row(self, tmp_path: Path) -> None:
        from nexus.db.t2.aspect_extraction_queue import AspectExtractionQueue

        store = AspectExtractionQueue(tmp_path / "t2.db")
        try:
            store.enqueue("knowledge__delos", "/p1.pdf")
            store.mark_done("knowledge__delos", "/p1.pdf")
            count = store.conn.execute(
                "SELECT COUNT(*) FROM aspect_extraction_queue"
            ).fetchone()[0]
        finally:
            store.close()
        assert count == 0

    def test_mark_failed_increments_retry_count(self, tmp_path: Path) -> None:
        from nexus.db.t2.aspect_extraction_queue import AspectExtractionQueue

        store = AspectExtractionQueue(tmp_path / "t2.db")
        try:
            store.enqueue("knowledge__delos", "/p1.pdf")
            store.mark_failed("knowledge__delos", "/p1.pdf", error="boom1")
            row = store.conn.execute(
                "SELECT status, retry_count, last_error "
                "FROM aspect_extraction_queue"
            ).fetchone()
        finally:
            store.close()
        assert row == ("failed", 1, "boom1")

    def test_mark_retry_resets_to_pending_and_increments(
        self, tmp_path: Path,
    ) -> None:
        """``mark_retry`` puts the row back in the pending pool
        (next claim will pick it up), incrementing ``retry_count``."""
        from nexus.db.t2.aspect_extraction_queue import AspectExtractionQueue

        store = AspectExtractionQueue(tmp_path / "t2.db")
        try:
            store.enqueue("knowledge__delos", "/p1.pdf")
            store.claim_next()
            store.mark_retry("knowledge__delos", "/p1.pdf")
            again = store.claim_next()
        finally:
            store.close()
        assert again is not None
        assert again.retry_count == 1


# ── Reclaim stale ────────────────────────────────────────────────────────────


class TestReclaimStale:
    def test_reclaim_stale_resets_old_in_progress_rows(
        self, tmp_path: Path,
    ) -> None:
        """Rows stuck in ``in_progress`` longer than the timeout are
        reset to ``pending`` (handles worker process death)."""
        from nexus.db.t2.aspect_extraction_queue import AspectExtractionQueue

        store = AspectExtractionQueue(tmp_path / "t2.db")
        try:
            store.enqueue("knowledge__delos", "/p1.pdf")
            store.claim_next()
            # Force last_attempt_at into the past
            store.conn.execute(
                "UPDATE aspect_extraction_queue "
                "SET last_attempt_at = datetime('now', '-10 minutes')"
            )
            store.conn.commit()

            reclaimed = store.reclaim_stale(timeout_seconds=60)
            row = store.conn.execute(
                "SELECT status FROM aspect_extraction_queue"
            ).fetchone()
        finally:
            store.close()
        assert reclaimed == 1
        assert row[0] == "pending"

    def test_reclaim_stale_leaves_recent_in_progress_rows_alone(
        self, tmp_path: Path,
    ) -> None:
        from nexus.db.t2.aspect_extraction_queue import AspectExtractionQueue

        store = AspectExtractionQueue(tmp_path / "t2.db")
        try:
            store.enqueue("knowledge__delos", "/p1.pdf")
            store.claim_next()
            reclaimed = store.reclaim_stale(timeout_seconds=300)
            row = store.conn.execute(
                "SELECT status FROM aspect_extraction_queue"
            ).fetchone()
        finally:
            store.close()
        assert reclaimed == 0
        assert row[0] == "in_progress"


# ── Pending count + listing ──────────────────────────────────────────────────


class TestListing:
    def test_pending_count(self, tmp_path: Path) -> None:
        from nexus.db.t2.aspect_extraction_queue import AspectExtractionQueue

        store = AspectExtractionQueue(tmp_path / "t2.db")
        try:
            assert store.pending_count() == 0
            store.enqueue("knowledge__delos", "/p1.pdf")
            store.enqueue("knowledge__delos", "/p2.pdf")
            store.enqueue("knowledge__a", "/p3.pdf")
            assert store.pending_count() == 3
            store.claim_next()  # one row goes to in_progress
            assert store.pending_count() == 2
        finally:
            store.close()

    def test_list_pending_returns_ordered_by_enqueued_at(
        self, tmp_path: Path,
    ) -> None:
        """The worker drains FIFO. ``list_pending`` reflects the same
        order claim_next would use."""
        from nexus.db.t2.aspect_extraction_queue import AspectExtractionQueue

        store = AspectExtractionQueue(tmp_path / "t2.db")
        try:
            store.enqueue("knowledge__delos", "/p1.pdf")
            store.enqueue("knowledge__delos", "/p2.pdf")
            store.enqueue("knowledge__delos", "/p3.pdf")
            rows = store.list_pending(limit=10)
        finally:
            store.close()
        paths = [r.source_path for r in rows]
        assert paths == ["/p1.pdf", "/p2.pdf", "/p3.pdf"]


# ── Concurrency: claim_next is atomic under multi-thread ─────────────────────


class TestConcurrency:
    def test_concurrent_claim_does_not_double_dispatch(
        self, tmp_path: Path,
    ) -> None:
        """Two threads racing claim_next on the same queue must each
        get a distinct row. No row appears twice; no row is skipped."""
        from nexus.db.t2.aspect_extraction_queue import AspectExtractionQueue

        store = AspectExtractionQueue(tmp_path / "t2.db")
        try:
            for i in range(20):
                store.enqueue("knowledge__delos", f"/p{i}.pdf")

            seen: list = []
            seen_lock = threading.Lock()

            def drain():
                while True:
                    row = store.claim_next()
                    if row is None:
                        return
                    with seen_lock:
                        seen.append(row.source_path)

            with ThreadPoolExecutor(max_workers=4) as ex:
                futures = [ex.submit(drain) for _ in range(4)]
                for f in futures:
                    f.result()
        finally:
            store.close()
        assert len(seen) == 20
        assert len(set(seen)) == 20  # no duplicates


# ── Facade wiring ────────────────────────────────────────────────────────────


class TestFacadeWiring:
    def test_t2database_exposes_aspect_queue(self, tmp_path: Path) -> None:
        with T2Database(tmp_path / "t2.db") as db:
            assert hasattr(db, "aspect_queue")
            from nexus.db.t2.aspect_extraction_queue import AspectExtractionQueue
            assert isinstance(db.aspect_queue, AspectExtractionQueue)

    def test_t2database_close_releases_aspect_queue(self, tmp_path: Path) -> None:
        path = tmp_path / "t2.db"
        with T2Database(path) as db:
            db.aspect_queue.enqueue("knowledge__delos", "/p1.pdf")
        with T2Database(path) as db2:
            row = db2.aspect_queue.claim_next()
            assert row is not None


# ── Migration sanity ─────────────────────────────────────────────────────────


class TestMigration:
    def test_migration_creates_table(self, tmp_path: Path) -> None:
        from nexus.db.migrations import migrate_aspect_extraction_queue_table

        db_path = tmp_path / "post_migrate.db"
        raw = sqlite3.connect(str(db_path))
        migrate_aspect_extraction_queue_table(raw)
        tables = {
            r[0] for r in raw.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        }
        raw.close()
        assert "aspect_extraction_queue" in tables

    def test_migration_idempotent(self, tmp_path: Path) -> None:
        from nexus.db.migrations import migrate_aspect_extraction_queue_table

        db_path = tmp_path / "idempotent.db"
        raw = sqlite3.connect(str(db_path))
        migrate_aspect_extraction_queue_table(raw)
        migrate_aspect_extraction_queue_table(raw)  # no-op
        raw.close()
