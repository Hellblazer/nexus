# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""RDR-138 T1.2: RENAME_LOCK guards complete_aspect, and a cascade rename cannot interleave
with it.

Tests covering:
 1. complete_aspect (the persist call) cannot run while a cascade holds RENAME_LOCK, and
    a cascade cannot run while it does (mutual exclusion).
 2. Gap-3 ordering: a cascade rename cannot interleave mid-complete_aspect.
 3. Structural: complete_aspect acquires RENAME_LOCK.

The aspect queue's claim verbs (claim_batch and the table mutators) left with the table
(nexus-hbnml); their lock proofs are gone with them.
"""
from __future__ import annotations

import os
import threading
from pathlib import Path
from typing import Any

import pytest

from tests._catalog_fixture_ops import register_real_doc_id


pytestmark = pytest.mark.t2_tenant

# hygiene-001-1 (nexus-tk070.p6a follow-on): document_aspects.doc_id carries a REAL FK to
# catalog_documents(tenant_id, tumbler) via the engine's POST /v1/aspects/upsert 400 -- see the
# identical cache in tests/test_document_aspects_store.py for the full
# rationale. One real registration per test, memoized on the test's
# freshly-minted NX_SERVICE_TOKEN.
_doc_id_cache: dict[str, str] = {}


def _shared_doc_id() -> str:
    key = os.environ.get("NX_SERVICE_TOKEN", "")
    if key not in _doc_id_cache:
        _doc_id_cache[key] = register_real_doc_id()
    return _doc_id_cache[key]


# ── helpers ──────────────────────────────────────────────────────────────────


def _make_db(tmp_path: Path) -> "T2Database":
    from nexus.db.t2 import T2Database
    return T2Database(tmp_path / "t2.db")


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
            "doc_id": _shared_doc_id(),
            "salient_sentences": [],
        }

    def test_complete_aspect_blocked_by_rename_lock(self, tmp_path: Path) -> None:
        """complete_aspect cannot run while RENAME_LOCK is held (cascade sim)."""
        db = _make_db(tmp_path)
        try:

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

    def test_complete_aspect_holds_rename_lock_through_the_upsert(
        self, tmp_path: Path
    ) -> None:
        """RENAME_LOCK is held through the upsert, so a probe cannot take it mid-call.

        Verify by instrumenting document_aspects.upsert and checking that
        RENAME_LOCK is not acquirable from a second thread mid-execution.
        complete_aspect wraps the whole call in one lock, so a probe thread
        cannot acquire RENAME_LOCK between the upsert and the return.
        """
        db = _make_db(tmp_path)
        try:
            fields = self._make_aspect_record_fields()

            probe_results: list[bool] = []
            mid_call_event = threading.Event()
            may_proceed = threading.Event()

            original_upsert = db.document_aspects.upsert

            def slow_upsert(record: Any) -> Any:
                result = original_upsert(record)
                # Signal: the upsert is done, the call is still inside the lock.
                mid_call_event.set()
                # Wait for probe thread to try acquiring RENAME_LOCK.
                may_proceed.wait(timeout=3.0)
                return result

            db.document_aspects.upsert = slow_upsert  # type: ignore[method-assign]

            def run_complete_aspect() -> None:
                db.complete_aspect(fields)

            t = threading.Thread(target=run_complete_aspect, daemon=True)
            t.start()

            # Wait until the upsert has run.
            mid_call_event.wait(timeout=3.0)

            # Probe: try to acquire RENAME_LOCK. Should FAIL because complete_aspect
            # still holds it (still inside the call).
            got = db.RENAME_LOCK.acquire(blocking=False)
            probe_results.append(got)
            if got:
                db.RENAME_LOCK.release()

            may_proceed.set()
            t.join(timeout=5.0)

            db.document_aspects.upsert = original_upsert  # type: ignore[method-assign]

            assert len(probe_results) == 1
            assert not probe_results[0], (
                "RENAME_LOCK was acquirable between the upsert and the return of "
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

        complete_aspect has upserted document_aspects under the OLD collection name.
        Without the lock, a cascade could rename that row before the call returns,
        so the call would finish under the stale name. With the lock, the cascade waits.
        """
        db = _make_db(tmp_path)
        try:
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
                # Signal: upsert done, the call has not returned.
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
                "Gap 3 is open: cascade can rename document_aspects while complete_aspect "
                "is mid-call."
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
                "doc_id": _shared_doc_id(),
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
