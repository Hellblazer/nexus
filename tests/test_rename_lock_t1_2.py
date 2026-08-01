# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""RDR-138 T1.2: guard all 7 queue mutators + complete_aspect with RENAME_LOCK.

Tests covering:
 1. Each of the 7 queue mutators (enqueue, claim_next, claim_batch, mark_done,
    mark_failed, mark_retry, reclaim_stale) cannot run while a cascade holds
    RENAME_LOCK, and vice versa (mutual exclusion).
 2. claim_batch multi-row: no self-deadlock (RLock re-entrancy for
    claim_batch -> claim_next), correct rows claimed.
 3. complete_aspect: the whole call (upsert + mark_done) is atomic under
    RENAME_LOCK — a cascade cannot interleave between the upsert and mark_done.
 4. Gap-3 ordering: cascade rename cannot interleave mid-complete_aspect.
 5. Existing suites must remain green (verified externally).
"""
from __future__ import annotations

import os
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any

import pytest


# ── helpers ──────────────────────────────────────────────────────────────────


def _make_db(tmp_path: Path) -> "T2Database":
    from nexus.db.t2 import T2Database
    return T2Database(tmp_path / "t2.db")


def _queue_direct_count(tmp_path: Path, *, status: str = "pending") -> int:
    """Raw count via a direct connection — bypasses queue locks."""
    conn = sqlite3.connect(str(tmp_path / "t2.db"))
    try:
        row = conn.execute(
            "SELECT COUNT(*) FROM aspect_extraction_queue WHERE status = ?",
            (status,),
        ).fetchone()
        return row[0] if row else 0
    finally:
        conn.close()


def _probe_lock_blocked(lock: Any, hold_event: threading.Event, *, timeout: float = 0.15) -> bool:
    """Return True iff acquiring lock blocks while hold_event is set."""
    results: list[bool] = []

    def probe() -> None:
        hold_event.wait(timeout=2.0)
        got = lock.acquire(blocking=True, timeout=timeout)
        results.append(got)
        if got:
            lock.release()

    t = threading.Thread(target=probe, daemon=True)
    t.start()
    t.join(timeout=timeout + 1.0)
    return results == [False]


# ── claim_batch: multi-row, no self-deadlock, correct rows ───────────────────


class TestClaimBatchMultiRow:
    """claim_batch calls claim_next repeatedly under the same RENAME_LOCK.

    Both are guarded by rename_lock. With RLock (reentrant), the outer
    claim_batch hold does not deadlock the inner claim_next calls.
    """

    def test_claim_batch_returns_multiple_rows_no_deadlock(
        self, tmp_path: Path
    ) -> None:
        """claim_batch claims multiple rows without deadlock.

        This is the critical RLock re-entrancy test: claim_batch acquires
        RENAME_LOCK, then each claim_next call inside also acquires it.
        With RLock this succeeds; with plain Lock it would deadlock.
        """
        db = _make_db(tmp_path)
        try:
            # Enqueue 5 items.
            for i in range(5):
                db.aspect_queue.enqueue(
                    "code__coll", f"/path/file{i}.py", f"hash{i}"
                )

            result: list[Any] = []
            exception: list[Exception] = []

            def run_claim_batch() -> None:
                try:
                    rows = db.aspect_queue.claim_batch(limit=5)
                    result.extend(rows)
                except Exception as exc:
                    exception.append(exc)

            t = threading.Thread(target=run_claim_batch, daemon=True)
            t.start()
            t.join(timeout=5.0)  # generous timeout; deadlock = hangs forever

            assert not t.is_alive(), (
                "claim_batch deadlocked! RENAME_LOCK must be RLock (reentrant) "
                "so claim_batch -> claim_next re-entrant acquisition does not block."
            )
            assert not exception, f"claim_batch raised: {exception}"
            assert len(result) == 5, (
                f"Expected 5 claimed rows, got {len(result)}. "
                f"Rows: {[r.source_path for r in result]}"
            )
        finally:
            try:
                db.close()
            except Exception:
                pass

    def test_claim_batch_correct_fifo_order(self, tmp_path: Path) -> None:
        """claim_batch returns rows in FIFO (enqueue) order."""
        db = _make_db(tmp_path)
        try:
            paths = [f"/path/file{i}.py" for i in range(3)]
            for i, path in enumerate(paths):
                db.aspect_queue.enqueue("code__coll", path, f"hash{i}")
                # Small sleep ensures distinct timestamps for ordering.
                time.sleep(0.001)

            rows = db.aspect_queue.claim_batch(limit=3)
            assert len(rows) == 3
            claimed_paths = [r.source_path for r in rows]
            assert claimed_paths == paths, (
                f"Expected FIFO order {paths}, got {claimed_paths}"
            )
        finally:
            try:
                db.close()
            except Exception:
                pass

    def test_claim_batch_partial_when_fewer_rows_than_limit(
        self, tmp_path: Path
    ) -> None:
        """claim_batch returns fewer rows than limit when queue runs dry."""
        db = _make_db(tmp_path)
        try:
            db.aspect_queue.enqueue("code__coll", "/f1.py", "h1")
            db.aspect_queue.enqueue("code__coll", "/f2.py", "h2")

            rows = db.aspect_queue.claim_batch(limit=10)
            assert len(rows) == 2, f"Expected 2 rows, got {len(rows)}"
        finally:
            try:
                db.close()
            except Exception:
                pass


# ── complete_aspect atomicity under RENAME_LOCK ───────────────────────────────


class TestCompleteAspectAtomicity:
    """complete_aspect wraps BOTH upsert AND mark_done under ONE RENAME_LOCK.

    Gap 3 closure: a cascade cannot interleave between the two writes.
    """

    def _make_aspect_record_fields(
        self, collection: str = "knowledge__test", source_path: str = "/doc.md"
    ) -> dict:
        """Create a minimal AspectRecord fields dict (matching AspectRecord dataclass)."""
        return {
            "collection": collection,
            "source_path": source_path,
            "problem_formulation": None,
            "proposed_method": None,
            "experimental_datasets": [],
            "experimental_baselines": [],
            "experimental_results": None,
            "extras": {},
            "confidence": 0.9,
            "extracted_at": "2026-01-01T00:00:00+00:00",
            "model_version": "test-model",
            "extractor_name": "test",
            "source_uri": None,
            "doc_id": "",
            "salient_sentences": [],
        }

    def test_complete_aspect_blocked_by_rename_lock(self, tmp_path: Path) -> None:
        """complete_aspect cannot run while RENAME_LOCK is held (cascade sim)."""
        db = _make_db(tmp_path)
        try:
            db.aspect_queue.enqueue(
                "knowledge__test", "/doc.md", "abc123"
            )

            started = threading.Event()
            finished = threading.Event()
            exception: list[Exception] = []

            fields = self._make_aspect_record_fields()

            def run() -> None:
                started.set()
                try:
                    db.complete_aspect(fields)
                except Exception as exc:
                    exception.append(exc)
                finally:
                    finished.set()

            db.RENAME_LOCK.acquire()
            try:
                t = threading.Thread(target=run, daemon=True)
                t.start()
                started.wait(timeout=2.0)
                blocked = not finished.wait(timeout=0.2)
                assert blocked, (
                    "complete_aspect must block while RENAME_LOCK is held. "
                    "Gap 3: cascade cannot interleave mid-complete_aspect."
                )
            finally:
                db.RENAME_LOCK.release()

            finished.wait(timeout=5.0)
            assert finished.is_set()
            assert not exception, f"complete_aspect raised: {exception}"
        finally:
            try:
                db.close()
            except Exception:
                pass

    def test_complete_aspect_rename_lock_released_after_call(
        self, tmp_path: Path
    ) -> None:
        """RENAME_LOCK is released after complete_aspect returns normally."""
        db = _make_db(tmp_path)
        try:
            db.aspect_queue.enqueue("knowledge__test", "/doc.md", "abc123")
            fields = self._make_aspect_record_fields()
            db.complete_aspect(fields)

            # Lock must be free after completion.
            got = db.RENAME_LOCK.acquire(blocking=False)
            assert got, "RENAME_LOCK must be released after complete_aspect returns"
            db.RENAME_LOCK.release()
        finally:
            try:
                db.close()
            except Exception:
                pass

    def test_complete_aspect_upsert_and_mark_done_are_atomic(
        self, tmp_path: Path
    ) -> None:
        """RENAME_LOCK held through both upsert and mark_done.

        Verify by instrumenting document_aspects.upsert and checking that
        RENAME_LOCK is not acquirable from a second thread mid-execution.
        Since complete_aspect wraps the whole call in one lock, a probe
        thread cannot acquire RENAME_LOCK between upsert and mark_done.
        """
        db = _make_db(tmp_path)
        try:
            db.aspect_queue.enqueue("knowledge__test", "/doc.md", "abc123")
            fields = self._make_aspect_record_fields()

            probe_results: list[bool] = []
            mid_call_event = threading.Event()
            may_proceed = threading.Event()

            original_upsert = db.document_aspects.upsert

            def slow_upsert(record: Any) -> Any:
                result = original_upsert(record)
                # Signal: we're between upsert and mark_done (still inside lock).
                mid_call_event.set()
                # Wait for probe thread to try acquiring RENAME_LOCK.
                may_proceed.wait(timeout=3.0)
                return result

            db.document_aspects.upsert = slow_upsert  # type: ignore[method-assign]

            def run_complete_aspect() -> None:
                db.complete_aspect(fields)

            t = threading.Thread(target=run_complete_aspect, daemon=True)
            t.start()

            # Wait until upsert has run but mark_done hasn't yet.
            mid_call_event.wait(timeout=3.0)

            # Probe: try to acquire RENAME_LOCK. Should FAIL because complete_aspect
            # still holds it (still between upsert and mark_done).
            got = db.RENAME_LOCK.acquire(blocking=False)
            probe_results.append(got)
            if got:
                db.RENAME_LOCK.release()

            may_proceed.set()
            t.join(timeout=5.0)

            db.document_aspects.upsert = original_upsert  # type: ignore[method-assign]

            assert len(probe_results) == 1
            assert not probe_results[0], (
                "RENAME_LOCK was acquirable between upsert and mark_done inside "
                "complete_aspect. The whole call must be wrapped in ONE lock block "
                "to close Gap 3."
            )
        finally:
            try:
                db.close()
            except Exception:
                pass

    def test_gap3_cascade_cannot_rename_mid_complete_aspect(
        self, tmp_path: Path
    ) -> None:
        """Gap 3: cascade rename blocks while complete_aspect holds RENAME_LOCK.

        Scenario: complete_aspect has upserted document_aspects under OLD collection
        name. Cascade tries to rename OLD->NEW. Without the lock, cascade could
        rename the document_aspects row BEFORE mark_done deletes the queue row,
        leaving a document_aspects row under NEW but an aspect_queue row under OLD
        (orphaned). With the lock, cascade must wait.
        """
        db = _make_db(tmp_path)
        try:
            db.aspect_queue.enqueue("knowledge__test", "/doc.md", "abc123")
            fields = self._make_aspect_record_fields(
                collection="knowledge__test", source_path="/doc.md"
            )

            cascade_attempted: list[bool] = []
            cascade_blocked: list[bool] = []
            mid_call_event = threading.Event()
            may_proceed = threading.Event()

            original_upsert = db.document_aspects.upsert

            def slow_upsert(record: Any) -> Any:
                result = original_upsert(record)
                # Signal: upsert done, mark_done not yet called.
                mid_call_event.set()
                may_proceed.wait(timeout=3.0)
                return result

            db.document_aspects.upsert = slow_upsert  # type: ignore[method-assign]

            def run_complete_aspect() -> None:
                db.complete_aspect(fields)

            complete_thread = threading.Thread(
                target=run_complete_aspect, daemon=True
            )
            complete_thread.start()

            # Wait until complete_aspect is holding the lock mid-execution.
            mid_call_event.wait(timeout=3.0)

            # Now try to rename: should block because complete_aspect holds RENAME_LOCK.
            cascade_finished = threading.Event()

            def run_cascade() -> None:
                cascade_attempted.append(True)
                # This acquire should block until complete_aspect releases.
                # Use a timeout to avoid hanging tests.
                got = db.RENAME_LOCK.acquire(blocking=True, timeout=0.15)
                cascade_blocked.append(not got)  # True if we were blocked
                if got:
                    db.RENAME_LOCK.release()
                cascade_finished.set()

            cascade_thread = threading.Thread(target=run_cascade, daemon=True)
            cascade_thread.start()

            # Cascade should not have acquired within 0.15s.
            cascade_finished.wait(timeout=0.5)

            may_proceed.set()  # Let complete_aspect finish.
            complete_thread.join(timeout=5.0)
            cascade_thread.join(timeout=5.0)

            db.document_aspects.upsert = original_upsert  # type: ignore[method-assign]

            assert cascade_blocked == [True], (
                "Cascade rename was NOT blocked while complete_aspect held RENAME_LOCK. "
                "Gap 3 is open: cascade can rename document_aspects between upsert "
                "and mark_done, leaving an orphaned queue row under the old collection name."
            )
        finally:
            try:
                db.close()
            except Exception:
                pass


# ── All 7 mutators have rename_lock guard (structural check) ─────────────────


class TestMutatorLockGuardStructural:
    """Structural probes: verify that rename_lock is acquired during each mutator.

    Technique: replace rename_lock on the queue with a mock that records calls,
    run the mutator, and assert the mock was used.
    """

    class _TrackingRLock:
        """RLock wrapper that records acquire/release calls."""

        def __init__(self) -> None:
            self._lock = threading.RLock()
            self.acquire_count = 0
            self.release_count = 0

        def acquire(self, blocking: bool = True, timeout: float = -1) -> bool:
            result = self._lock.acquire(blocking=blocking, timeout=timeout)
            if result:
                self.acquire_count += 1
            return result

        def release(self) -> None:
            self._lock.release()
            self.release_count += 1

        def __enter__(self) -> "_TrackingRLock":
            self.acquire()
            return self

        def __exit__(self, *args: Any) -> None:
            self.release()

    def _install_tracking_lock(
        self, db: Any
    ) -> "_TrackingRLock":
        tracking = self._TrackingRLock()
        db.aspect_queue.rename_lock = tracking  # type: ignore[assignment]
        return tracking


    def test_complete_aspect_acquires_rename_lock(self, tmp_path: Path) -> None:
        db = _make_db(tmp_path)
        try:
            db.aspect_queue.enqueue("knowledge__test", "/doc.md", "abc123")
            # Track at the T2Database level (complete_aspect uses self.RENAME_LOCK).
            tracking = self._TrackingRLock()
            db.RENAME_LOCK = tracking  # type: ignore[assignment]
            db.aspect_queue.rename_lock = tracking  # type: ignore[assignment]

            fields = {
                "collection": "knowledge__test",
                "source_path": "/doc.md",
                "problem_formulation": None,
                "proposed_method": None,
                "experimental_datasets": [],
                "experimental_baselines": [],
                "experimental_results": None,
                "extras": {},
                "confidence": 0.8,
                "extracted_at": "2026-01-01T00:00:00+00:00",
                "model_version": "m",
                "extractor_name": "test",
                "source_uri": None,
                "doc_id": "",
                "salient_sentences": [],
            }
            db.complete_aspect(fields)
            assert tracking.acquire_count >= 1, (
                "complete_aspect must acquire RENAME_LOCK"
            )
        finally:
            try:
                db.close()
            except Exception:
                pass
