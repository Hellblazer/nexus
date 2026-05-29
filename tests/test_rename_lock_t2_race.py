# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""RDR-138 T2 (nexus-troas): rename-cascade vs aspect-worker race regression suite.

This suite proves the *behavioural* guarantees the RENAME_LOCK fix (T1.1 +
T1.2) actually delivers against a concurrently-running cascade, with exact
assertions (``== N``, never ``>= N`` — see
``feedback_exact_assertions_for_fixture_regression``).

A note on scope, grounded in 150x empirical loops run while authoring this
suite (T1 scratch ``rdr-138 nexus-troas pre-write empirical findings``):

* The original canary's total-loss ``(0, 0)`` came from a LEAKED worker that
  ``mark_done``-ed unsupported ``code__`` work it never extracted. RENAME_LOCK
  does not (and should not) stop a *completing* worker from emptying the queue —
  a ``claim_next`` + ``mark_done`` pair racing the cascade legitimately yields
  ``(0, 0)`` because ``mark_done`` means "done, delete it". Layer 2's autouse
  ``_reset_aspect_worker_singleton`` fixture removed the leaked worker from the
  test suite; production avoids it because the worker only acts on supported
  collections.

* The verifiable loss-prevention invariant is therefore about an *in-flight*
  (claimed, not-yet-completed) row: it must survive a concurrent rename
  (Scenario 1, ``TestInflightRowPreservation``).

* For Gap 3 the fix's real guarantee is that ``complete_aspect``'s
  ``document_aspects.upsert`` + ``aspect_queue.mark_done`` cannot be SPLIT by a
  cascade — when ``complete_aspect`` runs (it holds RENAME_LOCK across both
  writes) the queue row is cleared and ``document_aspects`` lands under exactly
  one collection (Scenario 2a, ``TestCompleteAspectCascadeAtomicity``).

* The ``cascade-fully-before-complete_aspect`` ordering still drifts, because
  ``complete_aspect`` writes ``record.collection`` (the OLD name captured at
  claim time) after the cascade already moved everything to NEW. This is the
  self-healing residue the RDR's own *Failure Modes* paragraph documents
  (``reclaim_stale`` re-pends → re-extract under NEW). Scenario 2b
  (``TestGap3StaleCollectionResidue``) locks that residue in as a KNOWN state
  rather than hiding it, per the 2026-05-29 direction decision ("test the
  actual guarantee + note the residue; no code change").
"""
from __future__ import annotations

import statistics
import threading
import time
from pathlib import Path
from typing import Any

import pytest


# ── helpers ──────────────────────────────────────────────────────────────────


def _make_db(db_path: Path) -> "T2Database":
    """Open a T2Database. The conftest sets ``_DEFAULT_RUN_MIGRATIONS = True``
    session-wide, so the cascade's full schema (incl. taxonomy
    ``source_collection``) is present."""
    from nexus.db.t2 import T2Database
    return T2Database(db_path)


def _aspect_fields(collection: str, source_path: str) -> dict[str, Any]:
    """Minimal ``dataclasses.asdict(AspectRecord)``-shaped dict for
    ``complete_aspect``."""
    return {
        "collection": collection,
        "source_path": source_path,
        "problem_formulation": "p",
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


def _queue_counts(db_path: Path, old: str, new: str) -> tuple[int, int]:
    """Re-open a FRESH connection and count queue rows under (old, new).

    A fresh connection is required: the cascade commits on its own dedicated
    connection, and an already-open reader may hold a stale WAL snapshot.
    Every existing rename test re-opens to verify (test_collection_rename.py).
    """
    with _make_db(db_path) as v:
        aq_old = v.aspect_queue.conn.execute(
            "SELECT COUNT(*) FROM aspect_extraction_queue WHERE collection = ?",
            (old,),
        ).fetchone()[0]
        aq_new = v.aspect_queue.conn.execute(
            "SELECT COUNT(*) FROM aspect_extraction_queue WHERE collection = ?",
            (new,),
        ).fetchone()[0]
    return aq_old, aq_new


def _full_counts(
    db_path: Path, old: str, new: str
) -> tuple[int, int, int, int]:
    """(da_old, da_new, aq_old, aq_new) from a fresh connection."""
    with _make_db(db_path) as v:
        da_old = v.document_aspects.conn.execute(
            "SELECT COUNT(*) FROM document_aspects WHERE collection = ?",
            (old,),
        ).fetchone()[0]
        da_new = v.document_aspects.conn.execute(
            "SELECT COUNT(*) FROM document_aspects WHERE collection = ?",
            (new,),
        ).fetchone()[0]
        aq_old = v.aspect_queue.conn.execute(
            "SELECT COUNT(*) FROM aspect_extraction_queue WHERE collection = ?",
            (old,),
        ).fetchone()[0]
        aq_new = v.aspect_queue.conn.execute(
            "SELECT COUNT(*) FROM aspect_extraction_queue WHERE collection = ?",
            (new,),
        ).fetchone()[0]
    return da_old, da_new, aq_old, aq_new


_OLD = "knowledge__old"
_NEW = "knowledge__new"
_SRC = "doc.md"


# ── Scenario 1: in-flight claimed row survives a concurrent rename ────────────


class TestInflightRowPreservation:
    """Scenario 1 (queue-loss guardrail).

    An in-flight extraction (worker has ``claim_next``-ed a row but has NOT yet
    completed it) racing the cascade must NEVER lose the row: it is renamed to
    NEW and stays ``in_progress``. Looped to exercise both thread orderings.

    This is a guardrail, not a fix-discriminating test: a claim-only worker
    never deletes, so the row survives with or without the lock. It locks the
    "in-flight row survives a rename" contract against future regressions
    (e.g. a future cascade variant that filtered the queue UPDATE by status,
    or a worker that pre-emptively deleted on claim).
    """

    _ITERATIONS = 30

    def test_inflight_claim_racing_cascade_never_loses_row(
        self, tmp_path: Path
    ) -> None:
        outcomes: list[tuple[int, int]] = []
        statuses: list[str] = []

        for i in range(self._ITERATIONS):
            db_path = tmp_path / f"iter{i}" / "memory.db"
            db_path.parent.mkdir(parents=True)

            db = _make_db(db_path)
            try:
                db.aspect_queue.enqueue(_OLD, _SRC, content_hash="h", doc_id="")
            finally:
                db.close()

            db = _make_db(db_path)
            try:
                barrier = threading.Barrier(2)

                def worker() -> None:
                    barrier.wait()
                    # In-flight: claim only, do NOT complete.
                    db.aspect_queue.claim_next()

                def cascade() -> None:
                    barrier.wait()
                    db.rename_collection_cascade(old=_OLD, new=_NEW)

                tw = threading.Thread(target=worker, daemon=True)
                tc = threading.Thread(target=cascade, daemon=True)
                tw.start()
                tc.start()
                tw.join(timeout=10.0)
                tc.join(timeout=10.0)
                assert not tw.is_alive() and not tc.is_alive(), (
                    "deadlock: claim_next/cascade did not complete"
                )
            finally:
                db.close()

            aq_old, aq_new = _queue_counts(db_path, _OLD, _NEW)
            outcomes.append((aq_old, aq_new))
            with _make_db(db_path) as v:
                row = v.aspect_queue.conn.execute(
                    "SELECT status FROM aspect_extraction_queue "
                    "WHERE collection = ?",
                    (_NEW,),
                ).fetchone()
                statuses.append(row[0] if row else "<none>")

        # EXACT: every iteration preserved the row under NEW, never (0, 0).
        assert outcomes == [(0, 1)] * self._ITERATIONS, (
            f"in-flight row not deterministically preserved: {outcomes}"
        )
        assert outcomes.count((0, 0)) == 0, "total-loss (0,0) observed"
        assert statuses == ["in_progress"] * self._ITERATIONS, (
            f"renamed row lost its in_progress status: {statuses}"
        )


# ── Scenario 2a: complete_aspect's two writes are never split by a cascade ────


class TestCompleteAspectCascadeAtomicity:
    """Scenario 2 (Gap 3 — the actual fix guarantee).

    ``complete_aspect`` holds RENAME_LOCK across BOTH ``document_aspects.upsert``
    and ``aspect_queue.mark_done``. A cascade therefore cannot interleave
    between the two writes. When ``complete_aspect`` runs (before the cascade),
    the queue row is cleared by ``mark_done`` (no orphan) and ``document_aspects``
    lands under exactly one collection.
    """

    def test_complete_before_cascade_clears_queue_and_no_drift(
        self, tmp_path: Path
    ) -> None:
        """Deterministic ordering: complete_aspect fully, then cascade.

        document_aspects(OLD) + queue cleared, then cascade renames the
        document_aspects row OLD->NEW. Final state is consistent: one
        document_aspects row under NEW, queue empty, nothing under OLD.
        """
        db_path = tmp_path / "memory.db"
        db = _make_db(db_path)
        try:
            db.aspect_queue.enqueue(_OLD, _SRC, content_hash="h", doc_id="")
            db.aspect_queue.claim_next()
            db.complete_aspect(_aspect_fields(_OLD, _SRC))
            db.rename_collection_cascade(old=_OLD, new=_NEW)
        finally:
            db.close()

        da_old, da_new, aq_old, aq_new = _full_counts(db_path, _OLD, _NEW)
        assert da_old == 0, "document_aspects drifted: row left under OLD"
        assert da_new == 1, "extraction not preserved under NEW"
        assert aq_old == 0 and aq_new == 0, (
            f"queue not cleared by mark_done — orphan left: "
            f"old={aq_old} new={aq_new}"
        )

    def test_cascade_blocked_mid_complete_aspect_then_consistent(
        self, tmp_path: Path
    ) -> None:
        """A cascade cannot acquire RENAME_LOCK while complete_aspect holds it.

        Instrument ``document_aspects.upsert`` to pause AFTER the upsert but
        BEFORE ``mark_done`` (still inside the lock). A cascade thread that
        tries to run during that window must block (cannot split the call).
        After release, the final state is the clean complete-before-cascade
        outcome: document_aspects under NEW, queue cleared.
        """
        db_path = tmp_path / "memory.db"
        db = _make_db(db_path)
        try:
            db.aspect_queue.enqueue(_OLD, _SRC, content_hash="h", doc_id="")
            db.aspect_queue.claim_next()

            mid_call = threading.Event()
            may_proceed = threading.Event()
            cascade_blocked: list[bool] = []
            original_upsert = db.document_aspects.upsert

            def slow_upsert(record: Any) -> Any:
                result = original_upsert(record)
                mid_call.set()
                may_proceed.wait(timeout=5.0)
                return result

            db.document_aspects.upsert = slow_upsert  # type: ignore[method-assign]

            def run_complete() -> None:
                db.complete_aspect(_aspect_fields(_OLD, _SRC))

            tc = threading.Thread(target=run_complete, daemon=True)
            tc.start()
            assert mid_call.wait(timeout=5.0), "complete_aspect never reached upsert"

            # Cascade attempts to acquire RENAME_LOCK while complete_aspect holds it.
            def run_cascade() -> None:
                got = db.RENAME_LOCK.acquire(blocking=True, timeout=0.2)
                cascade_blocked.append(not got)
                if got:
                    db.RENAME_LOCK.release()

            tcasc = threading.Thread(target=run_cascade, daemon=True)
            tcasc.start()
            tcasc.join(timeout=2.0)

            may_proceed.set()
            tc.join(timeout=5.0)
            db.document_aspects.upsert = original_upsert  # type: ignore[method-assign]

            assert cascade_blocked == [True], (
                "cascade acquired RENAME_LOCK mid-complete_aspect — the "
                "upsert+mark_done pair was splittable (Gap 3 open)"
            )

            # complete_aspect ran fully; now run the (previously blocked) cascade.
            db.rename_collection_cascade(old=_OLD, new=_NEW)
        finally:
            db.close()

        da_old, da_new, aq_old, aq_new = _full_counts(db_path, _OLD, _NEW)
        assert da_old == 0 and da_new == 1, (
            f"document_aspects inconsistent: old={da_old} new={da_new}"
        )
        assert aq_old == 0 and aq_new == 0, (
            f"queue not cleared — orphan: old={aq_old} new={aq_new}"
        )


# ── Scenario 2b: documented self-healing residue (cascade before complete) ────


class TestGap3StaleCollectionResidue:
    """Scenario 2b — the KNOWN residue the fix does NOT close.

    Ordering ``claim(OLD) -> cascade(OLD->NEW) -> complete_aspect(collection=OLD)``:
    ``complete_aspect`` writes ``record.collection`` which is the STALE OLD name
    captured at claim time, after the cascade already moved everything to NEW.
    Result: ``document_aspects`` drifts under OLD and the queue row is orphaned
    ``in_progress`` under NEW (mark_done(OLD) misses the now-NEW row).

    Per the RDR *Failure Modes* paragraph and the 2026-05-29 direction
    decision, this is accepted as self-healing residue rather than a fix gap:
    ``reclaim_stale`` re-pends the orphan so re-extraction runs under NEW. This
    test locks the residue + recovery in as a KNOWN state (it is NOT hidden by
    a weakened assertion).
    """

    def test_cascade_before_complete_drifts_then_reclaim_self_heals(
        self, tmp_path: Path
    ) -> None:
        db_path = tmp_path / "memory.db"
        db = _make_db(db_path)
        try:
            db.aspect_queue.enqueue(_OLD, _SRC, content_hash="h", doc_id="")
            db.aspect_queue.claim_next()
            db.rename_collection_cascade(old=_OLD, new=_NEW)
            # complete_aspect with the stale OLD collection captured at claim.
            db.complete_aspect(_aspect_fields(_OLD, _SRC))

            da_old, da_new, aq_old, aq_new = _full_counts(db_path, _OLD, _NEW)
            # EXACT residue state (documented, not desired):
            assert da_old == 1 and da_new == 0, (
                f"unexpected document_aspects residue: old={da_old} new={da_new}"
            )
            assert aq_old == 0 and aq_new == 1, (
                f"unexpected queue residue: old={aq_old} new={aq_new}"
            )
            status = db.aspect_queue.conn.execute(
                "SELECT status FROM aspect_extraction_queue WHERE collection = ?",
                (_NEW,),
            ).fetchone()[0]
            assert status == "in_progress", "orphan not in_progress"

            # Self-heal: backdate last_attempt_at so the orphan is stale, then
            # reclaim. reclaim_stale re-pends it under NEW -> re-extraction.
            db.aspect_queue.conn.execute(
                "UPDATE aspect_extraction_queue "
                "SET last_attempt_at = '2020-01-01T00:00:00+00:00'"
            )
            db.aspect_queue.conn.commit()
            reclaimed = db.aspect_queue.reclaim_stale(timeout_seconds=60)
            assert reclaimed == 1, "reclaim_stale did not re-pend the orphan"

            healed = db.aspect_queue.conn.execute(
                "SELECT collection, status FROM aspect_extraction_queue"
            ).fetchall()
            assert healed == [(_NEW, "pending")], (
                f"orphan not self-healed to (NEW, pending): {healed}"
            )
        finally:
            db.close()


# ── Scenario 3: throughput — rare rename does not regress claim latency ───────


class TestRenameThroughput:
    """Scenario 3 (throughput probe).

    Renames are rare and short; claims are short. A rare rename must not
    materially regress steady-state ``claim_next`` latency.

    Soft dependency: T1.0 (nexus-2evpz) is the dedicated throughput spike whose
    baseline numbers calibrate this assertion. Until it lands, this is a
    deliberately generous guardrail (median-based, 6x + absolute floor) that
    catches a catastrophic regression (e.g. the lock held across an expensive
    operation) without flaking on CI timing noise. Median is robust to the
    handful of claims that block on a concurrent rename.
    """

    _CLAIMS = 150
    _RENAMES_DURING = 3  # rare, interspersed

    def _median_claim_latency(
        self, db: "T2Database", n: int, rename_every: int | None
    ) -> float:
        latencies: list[float] = []
        coll = _OLD
        for i in range(n):
            db.aspect_queue.enqueue(coll, f"f{i}.py", content_hash="h", doc_id="")
            t0 = time.perf_counter()
            db.aspect_queue.claim_next()
            latencies.append(time.perf_counter() - t0)
            db.aspect_queue.mark_done(coll, f"f{i}.py")
            if rename_every and i > 0 and i % rename_every == 0:
                new = _NEW if coll == _OLD else _OLD
                db.rename_collection_cascade(old=coll, new=new)
                coll = new
        return statistics.median(latencies)

    def test_claim_latency_not_materially_regressed_by_rare_rename(
        self, tmp_path: Path
    ) -> None:
        # Baseline: no renames.
        base_path = tmp_path / "base" / "memory.db"
        base_path.parent.mkdir(parents=True)
        db = _make_db(base_path)
        try:
            baseline = self._median_claim_latency(
                db, self._CLAIMS, rename_every=None
            )
        finally:
            db.close()

        # Contended: a few renames interspersed among the claims.
        cont_path = tmp_path / "cont" / "memory.db"
        cont_path.parent.mkdir(parents=True)
        rename_every = self._CLAIMS // (self._RENAMES_DURING + 1)
        db = _make_db(cont_path)
        try:
            contended = self._median_claim_latency(
                db, self._CLAIMS, rename_every=rename_every
            )
        finally:
            db.close()

        # Generous guardrail: median claim latency must not blow up. The
        # absolute floor absorbs sub-millisecond baseline jitter where a small
        # multiplicative bound would be meaningless.
        ceiling = baseline * 6.0 + 0.003
        assert contended <= ceiling, (
            f"claim latency regressed under rare rename: "
            f"baseline median={baseline * 1e3:.3f}ms "
            f"contended median={contended * 1e3:.3f}ms "
            f"ceiling={ceiling * 1e3:.3f}ms"
        )
