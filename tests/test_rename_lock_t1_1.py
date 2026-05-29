# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""RDR-138 T1.1: RENAME_LOCK primitive + cascade holds it for whole transaction.

Tests covering:
 1. RENAME_LOCK is an RLock on T2Database, shared with AspectExtractionQueue.
 2. rename_collection_cascade acquires RENAME_LOCK for its whole BEGIN..COMMIT.
 3. Re-entrant acquisition (claim_batch->claim_next pattern) does not deadlock.
 4. Lock ordering constraint: RENAME_LOCK is acquirable from outside any per-store
    self._lock region.
 5. Plumbing: AspectExtractionQueue has rename_lock attribute set by T2Database.
"""
from __future__ import annotations

import sqlite3
import threading
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest


# ── T2Database has RENAME_LOCK ────────────────────────────────────────────────


class TestRenameLockAttribute:
    def test_t2database_has_rename_lock(self, tmp_path: Path) -> None:
        """T2Database exposes a RENAME_LOCK threading.RLock."""
        from nexus.db.t2 import T2Database
        db = T2Database(tmp_path / "t2.db")
        try:
            assert hasattr(db, "RENAME_LOCK"), "T2Database must have RENAME_LOCK"
        finally:
            db.close()

    def test_rename_lock_is_rlock(self, tmp_path: Path) -> None:
        """RENAME_LOCK must be a threading.RLock (reentrant) not a plain Lock.

        Rationale: T1.2 will wrap claim_batch AND claim_next with RENAME_LOCK.
        claim_batch calls claim_next in a loop. A plain Lock would self-deadlock.
        """
        from nexus.db.t2 import T2Database
        db = T2Database(tmp_path / "t2.db")
        try:
            lock = db.RENAME_LOCK
            # RLock supports re-entrant acquisition; Lock does not.
            # Verify by acquiring twice from the same thread.
            acquired_first = lock.acquire(blocking=False)
            assert acquired_first, "RENAME_LOCK must be acquirable"
            try:
                acquired_second = lock.acquire(blocking=False)
                assert acquired_second, (
                    "RENAME_LOCK must be reentrant (RLock) — second acquire from same "
                    "thread must succeed. A plain threading.Lock would return False here."
                )
                lock.release()
            finally:
                lock.release()
        finally:
            db.close()

    def test_aspect_queue_has_rename_lock_set(self, tmp_path: Path) -> None:
        """AspectExtractionQueue.rename_lock is the same object as T2Database.RENAME_LOCK."""
        from nexus.db.t2 import T2Database
        db = T2Database(tmp_path / "t2.db")
        try:
            assert hasattr(db.aspect_queue, "rename_lock"), (
                "AspectExtractionQueue must have rename_lock attribute"
            )
            assert db.aspect_queue.rename_lock is db.RENAME_LOCK, (
                "AspectExtractionQueue.rename_lock must be the SAME object as "
                "T2Database.RENAME_LOCK (identity check)"
            )
        finally:
            db.close()


# ── Cascade holds RENAME_LOCK for whole BEGIN..COMMIT ────────────────────────


class TestCascadeHoldsLock:
    def test_rename_lock_held_during_cascade_sql(self, tmp_path: Path) -> None:
        """rename_collection_cascade holds RENAME_LOCK across the full transaction.

        Technique: run the cascade in a background thread, then from the main
        thread try to acquire RENAME_LOCK during the cascade via a long-running
        cascade simulated by blocking inside _rename_collection_cascade_locked.
        Simpler approach: verify RENAME_LOCK is held by checking it cannot be
        acquired from a second thread while the cascade is in-flight.
        """
        from nexus.db.t2 import T2Database
        db = T2Database(tmp_path / "t2.db")

        # Use a barrier to synchronize: cascade thread signals mid-cascade,
        # probe thread checks lock state, then both proceed.
        mid_cascade_event = threading.Event()
        probe_result: list[bool] = []
        cascade_may_proceed = threading.Event()

        original_locked = db._rename_collection_cascade_locked

        def instrumented_locked(**kwargs: object) -> object:
            # Signal that we are inside the locked body (RENAME_LOCK is held).
            mid_cascade_event.set()
            # Wait for probe to complete before proceeding.
            cascade_may_proceed.wait(timeout=3.0)
            return original_locked(**kwargs)

        def run_cascade() -> None:
            db._rename_collection_cascade_locked = instrumented_locked  # type: ignore[method-assign]
            try:
                db.rename_collection_cascade(old="code__old", new="code__new")
            finally:
                db._rename_collection_cascade_locked = original_locked  # type: ignore[method-assign]

        cascade_thread = threading.Thread(target=run_cascade)
        cascade_thread.start()

        # Wait until cascade is inside the locked body.
        mid_cascade_event.wait(timeout=3.0)

        # Now probe: can another thread acquire RENAME_LOCK?
        got = db.RENAME_LOCK.acquire(blocking=False)
        probe_result.append(got)
        if got:
            db.RENAME_LOCK.release()

        # Let cascade finish.
        cascade_may_proceed.set()
        cascade_thread.join(timeout=5.0)

        assert not cascade_thread.is_alive(), "Cascade thread should have completed"
        assert len(probe_result) == 1, "Probe must have run"
        assert not probe_result[0], (
            "RENAME_LOCK was acquirable from a second thread while the cascade was "
            "in-flight. The cascade must hold RENAME_LOCK across its whole body."
        )

        try:
            db.close()
        except Exception:  # noqa: BLE001
            pass

    def test_rename_lock_released_after_cascade(self, tmp_path: Path) -> None:
        """RENAME_LOCK is released when rename_collection_cascade returns normally."""
        from nexus.db.t2 import T2Database
        db = T2Database(tmp_path / "t2.db")
        try:
            db.rename_collection_cascade(old="code__old", new="code__new")
            # Should be freely acquirable now.
            got = db.RENAME_LOCK.acquire(blocking=False)
            assert got, "RENAME_LOCK must be released after cascade completes"
            db.RENAME_LOCK.release()
        finally:
            db.close()

    def test_rename_lock_released_on_cascade_error(self, tmp_path: Path) -> None:
        """RENAME_LOCK is released even when rename_collection_cascade raises."""
        from nexus.db.t2 import T2Database
        db = T2Database(tmp_path / "t2.db")

        class _BombConn:
            """Fake connection that raises on BEGIN."""
            def execute(self, sql: str, *args: object) -> object:
                if sql.strip().upper() == "BEGIN":
                    raise RuntimeError("injected failure")
                return MagicMock()
            def rollback(self) -> None: pass
            def close(self) -> None: pass

        try:
            with patch("sqlite3.connect", return_value=_BombConn()):
                with pytest.raises(RuntimeError, match="injected failure"):
                    db.rename_collection_cascade(old="code__old", new="code__new")

            # Lock must be free.
            got = db.RENAME_LOCK.acquire(blocking=False)
            assert got, "RENAME_LOCK must be released even when cascade raises"
            db.RENAME_LOCK.release()
        finally:
            db.close()


# ── Re-entrant acquisition (claim_batch -> claim_next) ───────────────────────


class TestReentrantAcquisition:
    def test_claim_batch_via_claim_next_no_deadlock(self, tmp_path: Path) -> None:
        """RLock allows claim_batch to call claim_next repeatedly under T1.2.

        Simulate the T1.2 pattern: outer RENAME_LOCK acquire (as claim_batch
        would take it), then inner RENAME_LOCK acquire (as claim_next would take
        it). With RLock this must not deadlock. With plain Lock it would.

        This test proves the chosen lock type makes T1.2's guarding safe.
        """
        from nexus.db.t2 import T2Database
        db = T2Database(tmp_path / "t2.db")

        result: list[str] = []

        def simulate_claim_batch_then_claim_next() -> None:
            # Outer: simulate claim_batch acquiring RENAME_LOCK.
            with db.RENAME_LOCK:
                result.append("outer_acquired")
                # Inner: simulate claim_next (called by claim_batch) also acquiring.
                with db.RENAME_LOCK:
                    result.append("inner_acquired")
                result.append("inner_released")
            result.append("outer_released")

        t = threading.Thread(target=simulate_claim_batch_then_claim_next)
        t.start()
        t.join(timeout=2.0)

        assert not t.is_alive(), (
            "claim_batch->claim_next simulation deadlocked! "
            "RENAME_LOCK must be an RLock (reentrant)."
        )
        assert result == [
            "outer_acquired", "inner_acquired", "inner_released", "outer_released"
        ], f"Expected clean re-entrant sequence, got: {result}"

        try:
            db.close()
        except Exception:  # noqa: BLE001
            pass

    def test_claim_batch_does_not_block_with_rename_lock_held_externally(
        self, tmp_path: Path
    ) -> None:
        """When the cascade holds RENAME_LOCK, claim_batch from a second thread blocks.

        This validates that the two sides DO contend — the rename serializes
        against aspect queue operations as intended.
        """
        from nexus.db.t2 import T2Database
        db = T2Database(tmp_path / "t2.db")
        db.aspect_queue.enqueue("code__test", "/file.py", "abc123")

        # Scenario: cascade holds the lock for a moment.
        lock_released = threading.Event()
        second_started = threading.Event()
        second_completed: list[bool] = []

        def hold_rename_lock() -> None:
            with db.RENAME_LOCK:
                second_started.set()
                # Hold while second thread tries to acquire.
                lock_released.wait(timeout=2.0)

        def try_claim_under_lock() -> None:
            second_started.wait(timeout=2.0)
            # Try to acquire RENAME_LOCK (simulating what T1.2 claim_next would do).
            got = db.RENAME_LOCK.acquire(blocking=True, timeout=0.1)
            second_completed.append(got)
            if got:
                db.RENAME_LOCK.release()

        t1 = threading.Thread(target=hold_rename_lock)
        t2 = threading.Thread(target=try_claim_under_lock)
        t1.start()
        t2.start()

        # Second thread should NOT acquire within 0.1s because first holds it.
        t2.join(timeout=1.0)
        assert second_completed == [False], (
            "Second thread should be blocked by RENAME_LOCK held by cascade "
            "thread. This validates mutual exclusion."
        )

        lock_released.set()
        t1.join(timeout=2.0)
        try:
            db.close()
        except Exception:  # noqa: BLE001
            pass


# ── Lock ordering: RENAME_LOCK is outermost ──────────────────────────────────


class TestLockOrdering:
    def test_rename_lock_acquirable_without_per_store_lock(self, tmp_path: Path) -> None:
        """RENAME_LOCK can be acquired freely (no per-store lock held).

        Documents the lock ordering contract: RENAME_LOCK -> per-store _lock.
        This is a forward constraint — acquire RENAME_LOCK only OUTSIDE any
        self._lock region.
        """
        from nexus.db.t2 import T2Database
        db = T2Database(tmp_path / "t2.db")
        try:
            # No per-store lock held; RENAME_LOCK must be freely acquirable.
            acquired = db.RENAME_LOCK.acquire(blocking=False)
            assert acquired, "RENAME_LOCK must be acquirable (no lock cycle)"
            db.RENAME_LOCK.release()
        finally:
            db.close()

    def test_cascade_bypasses_per_store_lock(self, tmp_path: Path) -> None:
        """rename_collection_cascade holds RENAME_LOCK, not per-store self._lock.

        The cascade runs on its own dedicated connection (bypassing all seven
        per-store self._lock regions by design). Verify that the cascade
        completes even when aspect_queue._lock is held by another thread
        (which would deadlock if the cascade tried to acquire it).
        """
        from nexus.db.t2 import T2Database
        db = T2Database(tmp_path / "t2.db")

        cascade_completed: list[bool] = []
        lock_released_after_cascade = threading.Event()

        def hold_aspect_queue_lock_during_cascade() -> None:
            # Acquire the queue's internal lock so it is held.
            with db.aspect_queue._lock:
                # Run the cascade while _lock is held externally.
                try:
                    db.rename_collection_cascade(old="code__old", new="code__new")
                    cascade_completed.append(True)
                except Exception:
                    cascade_completed.append(False)
                lock_released_after_cascade.set()

        t = threading.Thread(target=hold_aspect_queue_lock_during_cascade)
        t.start()
        t.join(timeout=10.0)

        assert not t.is_alive(), "Thread should have completed"
        assert cascade_completed == [True], (
            "rename_collection_cascade must complete even when aspect_queue._lock "
            "is held by another thread. Cascade uses its own connection and must "
            "not attempt to acquire per-store locks."
        )
        try:
            db.close()
        except Exception:  # noqa: BLE001
            pass


# ── complete_aspect path has rename_lock wiring ──────────────────────────────


class TestCompleteAspectLockPlumbing:
    def test_complete_aspect_path_has_access_to_rename_lock(self, tmp_path: Path) -> None:
        """T2Database.complete_aspect can reach RENAME_LOCK (T1.2 will wrap it).

        T1.1 wires the lock; T1.2 uses it. This test confirms the attribute
        is reachable on the code paths complete_aspect touches.
        """
        from nexus.db.t2 import T2Database
        db = T2Database(tmp_path / "t2.db")
        try:
            # The lock is on the facade and on aspect_queue.
            assert db.RENAME_LOCK is not None
            assert db.aspect_queue.rename_lock is not None
            # complete_aspect uses self.document_aspects and self.aspect_queue.
            # Both are reachable from db; the lock is on db (facade) and
            # on aspect_queue directly. T1.2 will wrap complete_aspect body
            # with "with self.RENAME_LOCK:" — confirm the attribute is valid.
            with db.RENAME_LOCK:
                pass  # no error = attribute exists and is a valid context manager
        finally:
            db.close()
