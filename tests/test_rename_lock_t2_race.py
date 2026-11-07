# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""RDR-138 T2 (nexus-troas): rename-cascade vs complete_aspect race regression suite.

The RENAME_LOCK fix (T1.1 + T1.2) must hold against a concurrently-running cascade, with
exact assertions (== N, never >= N).

* complete_aspect holds RENAME_LOCK across its document_aspects upsert and its return, so a
  cascade cannot interleave between them (TestCompleteAspectCascadeAtomicity).
* A cascade ordered before complete_aspect leaves document_aspects drifted under the stale
  OLD name. That is the known residue, locked in as a known state
  (TestGap3StaleCollectionResidue).

The aspect queue table and its claim, mark and count verbs left with the table (nexus-hbnml),
and the queue scenarios went with them.
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
# freshly-minted NX_SERVICE_TOKEN. The subject here is the RENAME_LOCK race, not
# attribution, so the doc is a real one rather than a special-cased exemption.
_doc_id_cache: dict[str, str] = {}


def _shared_doc_id() -> str:
    key = os.environ.get("NX_SERVICE_TOKEN", "")
    if key not in _doc_id_cache:
        _doc_id_cache[key] = register_real_doc_id()
    return _doc_id_cache[key]


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
        "doc_id": _shared_doc_id(),
        "salient_sentences": [],
    }


def _aspect_presence(db_path: Path, old: str, new: str, src: str) -> tuple[bool, bool]:
    """(row under OLD?, row under NEW?) via the PUBLIC ``document_aspects.get``
    keyed read — substrate-neutral (RDR-155 P4b P0a'). ``(collection,
    source_path)`` is the store's unique key, so presence is exact-count 1.

    A fresh open is required on the sqlite leg: the cascade commits on its own
    dedicated connection, and an already-open reader may hold a stale WAL
    snapshot (test_collection_rename.py precedent).
    """
    with _make_db(db_path) as v:
        return (
            v.document_aspects.get(old, src) is not None,
            v.document_aspects.get(new, src) is not None,
        )


_OLD = "knowledge__old"
_NEW = "knowledge__new"
_SRC = "doc.md"


# ── Scenario 2a: complete_aspect's two writes are never split by a cascade ────


class TestCompleteAspectCascadeAtomicity:
    """Scenario 2 (Gap 3 — the actual fix guarantee).

    ``complete_aspect`` holds RENAME_LOCK across its ``document_aspects.upsert`` and its
    return, so a cascade cannot interleave between them. When ``complete_aspect`` runs
    (before the cascade), ``document_aspects`` lands under exactly one collection.
    """

    def test_complete_before_cascade_leaves_no_drift(
        self, tmp_path: Path
    ) -> None:
        """Deterministic ordering: complete_aspect fully, then cascade.

        document_aspects(OLD) is written, then the cascade renames it OLD->NEW. Final state
        is consistent: one document_aspects row under NEW, nothing under OLD.
        """
        db_path = tmp_path / "memory.db"
        db = _make_db(db_path)
        try:
            db.complete_aspect(_aspect_fields(_OLD, _SRC))
            db.rename_collection_cascade(old=_OLD, new=_NEW)
        finally:
            db.close()

        da_old, da_new = _aspect_presence(db_path, _OLD, _NEW, _SRC)
        assert not da_old, "document_aspects drifted: row left under OLD"
        assert da_new, "extraction not preserved under NEW"


    def test_cascade_blocked_mid_complete_aspect_then_consistent(
        self, tmp_path: Path
    ) -> None:
        """A cascade cannot acquire RENAME_LOCK while complete_aspect holds it.

        Instrument ``document_aspects.upsert`` to pause AFTER the upsert, while
        complete_aspect still holds the lock. A cascade thread that tries to run during
        that window must block (cannot split the call). After release, the final state
        is the clean complete-before-cascade outcome: document_aspects under NEW.
        """
        db_path = tmp_path / "memory.db"
        db = _make_db(db_path)
        try:

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
                "upsert and return were splittable (Gap 3 open)"
            )

            # complete_aspect ran fully; now run the (previously blocked) cascade.
            db.rename_collection_cascade(old=_OLD, new=_NEW)
        finally:
            db.close()

        da_old, da_new = _aspect_presence(db_path, _OLD, _NEW, _SRC)
        assert (not da_old) and da_new, (
            f"document_aspects inconsistent: old={da_old} new={da_new}"
        )


# ── Scenario 2b: documented self-healing residue (cascade before complete) ────


class TestGap3StaleCollectionResidue:
    """The stale-name residue the fix does NOT close.

    Ordering ``cascade(OLD->NEW) -> complete_aspect(collection=OLD)``: complete_aspect writes
    the OLD name it was given, after the cascade already moved the rows to NEW, so
    document_aspects drifts under OLD. The concurrent case is closed by the tests above;
    this ordering is accepted residue (2026-05-29 direction decision: test the guarantee,
    note the residue, no code change). This test locks the residue in as a KNOWN state.
    """

    def test_cascade_before_complete_drifts_under_the_stale_name(self, tmp_path: Path) -> None:
        db_path = tmp_path / "memory.db"
        db = _make_db(db_path)
        try:
            db.rename_collection_cascade(old=_OLD, new=_NEW)
            # complete_aspect with the stale OLD collection, after the cascade moved on.
            db.complete_aspect(_aspect_fields(_OLD, _SRC))
        finally:
            db.close()

        da_old, da_new = _aspect_presence(db_path, _OLD, _NEW, _SRC)
        assert da_old and not da_new, (
            f"unexpected document_aspects residue: old={da_old} new={da_new}"
        )


# ── Hardening (T4 test-validator follow-ups) ──────────────────────────────────


class _TrackingRLock:
    """RLock wrapper recording acquire calls (for negative-structural gates)."""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self.acquire_count = 0

    def acquire(self, blocking: bool = True, timeout: float = -1) -> bool:
        result = self._lock.acquire(blocking=blocking, timeout=timeout)
        if result:
            self.acquire_count += 1
        return result

    def release(self) -> None:
        self._lock.release()

    def __enter__(self) -> "_TrackingRLock":
        self.acquire()
        return self

    def __exit__(self, *_: Any) -> None:
        self.release()


class TestReadOnlyMethodsDoNotSerialize:
    """Negative-structural gate: the census read must NOT acquire rename_lock.

    tuple_counts holds no lock; it reads the engine's aspects census. This gate catches a
    future refactor that promotes a reader into a rename-serialized participant
    (test-validator gap, 2026-05-29).
    """

    def test_read_only_methods_omit_rename_lock(self, tmp_path: Path) -> None:
        db = _make_db(tmp_path / "memory.db")
        try:
            tracking = _TrackingRLock()
            db.aspect_queue.rename_lock = tracking  # type: ignore[assignment]

            db.aspect_queue.tuple_counts()

            assert tracking.acquire_count == 0, (
                "a read-only queue method acquired rename_lock — reads must "
                "stay unblocked while a cascade holds the lock"
            )
        finally:
            db.close()


class TestCompleteAspectReleasesLockOnError:
    """complete_aspect must release RENAME_LOCK even when a write raises
    between the upsert and the return.

    The ``with self.RENAME_LOCK:`` block guarantees release on exception
    (Python semantics), but a future refactor to manual acquire/release could
    leak the lock and wedge every subsequent rename + queue mutator. This
    locks the release-on-error contract (test-validator gap, 2026-05-29).
    """

    def test_lock_released_after_upsert_raises(self, tmp_path: Path) -> None:
        db = _make_db(tmp_path / "memory.db")
        try:

            def boom(_record: Any) -> Any:
                raise RuntimeError("upsert exploded")

            db.document_aspects.upsert = boom  # type: ignore[method-assign]

            with pytest.raises(RuntimeError, match="upsert exploded"):
                db.complete_aspect(_aspect_fields(_OLD, _SRC))

            # Lock must be free — a leaked lock would wedge all renames.
            got = db.RENAME_LOCK.acquire(blocking=False)
            assert got, "RENAME_LOCK leaked after complete_aspect raised"
            db.RENAME_LOCK.release()
        finally:
            db.close()
