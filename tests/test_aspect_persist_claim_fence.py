# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""RDR-235 client half (nexus-5r2sc.3.1): ``complete_aspect`` takes a claim pair.

Against the real engine substrate, with a real task on the ``aspects`` tuple
queue. Test Plan scenario 9 (with a claim: the write and the tuple ack happen
and the table is untouched; without one: the write and ``mark_done`` happen and
a queued task for that document stays queued) and scenario 4's client half (a
lapse between the write and the ack leaves the aspect row, and the ack answers
``ClaimNotFound``), plus the refusals of the claim fence (404, 403 twice) and the
pair rule (one of the two is a ``ValueError``).

VACUITY (each run red, then restored, recorded in the bead): stubbing out the
claim branch of ``T2Database.complete_aspect`` (calling ``mark_done`` instead of
``tuples.ack``) fails the claim-path, lapse and RENAME_LOCK tests; dropping the
pair from ``HttpDocumentAspectsStore.upsert``'s body fails the refusal tests,
because the engine then writes unfenced and nothing raises.
"""
from __future__ import annotations

import json
import os
import threading
from pathlib import Path
from typing import Any

import pytest

from nexus.db.t2.http_tuple_store import (
    ClaimNotFoundError,
    ClaimOwnershipError,
    ClaimTaskMismatchError,
)
from tests._catalog_fixture_ops import register_real_doc_id

pytestmark = pytest.mark.t2_tenant

_SUBSPACE = "aspects"
_KEYS = {"queue": "aspects"}
_COLLECTION = "knowledge__test"
_SOURCE = "/doc.md"


def _stamp_title() -> str:
    return f"claim fence doc {os.getpid()}"


def _db(tmp_path: Path) -> Any:
    from nexus.db.t2 import T2Database

    return T2Database(tmp_path / "t2.db")


def _fields(doc_id: str) -> dict[str, Any]:
    return {
        "collection": _COLLECTION,
        "source_path": _SOURCE,
        "problem_formulation": "pf",
        "proposed_method": None,
        "experimental_datasets": [],
        "experimental_baselines": [],
        "experimental_results": None,
        "extras": {},
        "confidence": 0.9,
        "extracted_at": "2026-10-29T00:00:00+00:00",
        "model_version": "test-model",
        "extractor_name": "test",
        "source_uri": None,
        "doc_id": doc_id,
        "salient_sentences": [],
    }


def _enqueue_task(db: Any, doc_id: str) -> None:
    """The producer's ``out``: one task per document, nonce = doc_id."""
    db.tuples.out(
        _SUBSPACE,
        dict(_KEYS),
        body=json.dumps({
            "collection": _COLLECTION, "source_path": _SOURCE,
            "doc_id": doc_id, "content_hash": "h",
        }),
        nonce=doc_id,
    )


def _claim(db: Any, claimant: str) -> str:
    claimed = db.tuples.inp(_SUBSPACE, dict(_KEYS), claimant=claimant, lease_s=600)
    assert claimed is not None, "the task just enqueued must be claimable"
    return claimed[1]


def _task_rows(db: Any) -> int:
    return len(db.tuples.rd(_SUBSPACE, dict(_KEYS)))


def _aspect_row(db: Any, doc_id: str) -> Any:
    return db.document_aspects.get_by_doc_id(doc_id)


def _claimed_task(db: Any, claimant: str = "W:0") -> tuple[str, str]:
    doc_id = register_real_doc_id(title=_stamp_title())
    _enqueue_task(db, doc_id)
    return doc_id, _claim(db, claimant)


class TestScenario9Persist:
    def test_with_a_claim_the_write_and_the_tuple_ack_happen_and_the_table_is_untouched(
        self, tmp_path: Path,
    ) -> None:
        db = _db(tmp_path)
        try:
            doc_id, claim_id = _claimed_task(db)
            # A table row for the same document: the claim path must not clear it.
            db.aspect_queue.enqueue(_COLLECTION, _SOURCE, "h", doc_id=doc_id)
            assert db.aspect_queue.pending_count() == 1

            written = db.complete_aspect(_fields(doc_id), claim_id=claim_id, claimant="W:0")

            assert written is True
            assert _aspect_row(db, doc_id) is not None
            with pytest.raises(ClaimNotFoundError):  # the ack consumed the claim
                db.tuples.ack(claim_id, "W:0")
            assert _task_rows(db) == 0
            assert db.aspect_queue.pending_count() == 1, "mark_done must not run on the claim path"
        finally:
            db.close()

    def test_without_a_claim_the_write_and_mark_done_happen_and_a_queued_task_stays_queued(
        self, tmp_path: Path,
    ) -> None:
        db = _db(tmp_path)
        try:
            doc_id = register_real_doc_id(title=_stamp_title())
            _enqueue_task(db, doc_id)
            db.aspect_queue.enqueue(_COLLECTION, _SOURCE, "h", doc_id=doc_id)
            assert db.aspect_queue.pending_count() == 1

            written = db.complete_aspect(_fields(doc_id))

            assert written is True
            assert _aspect_row(db, doc_id) is not None
            assert db.aspect_queue.pending_count() == 0, "today's path clears the table row"
            assert _task_rows(db) == 1, "no client can delete a task by document (F22)"
            assert _claim(db, "W:1"), "and the task is still claimable"
        finally:
            db.close()


class TestFenceRefusals:
    @pytest.mark.parametrize("lapse", ["release", "ack"])
    def test_a_lapsed_claim_raises_not_found_and_writes_nothing(
        self, tmp_path: Path, lapse: str,
    ) -> None:
        db = _db(tmp_path)
        try:
            doc_id, claim_id = _claimed_task(db)
            db.aspect_queue.enqueue(_COLLECTION, _SOURCE, "h", doc_id=doc_id)
            if lapse == "release":
                db.tuples.release(claim_id, "W:0")
            else:
                db.tuples.ack(claim_id, "W:0")
            rows_before = _task_rows(db)

            with pytest.raises(ClaimNotFoundError):
                db.complete_aspect(_fields(doc_id), claim_id=claim_id, claimant="W:0")

            assert _aspect_row(db, doc_id) is None, "a refused fence writes nothing"
            assert db.aspect_queue.pending_count() == 1, "the table is not touched"
            assert _task_rows(db) == rows_before
        finally:
            db.close()

    def test_another_claimants_pair_raises_ownership_and_leaves_the_claim_live(
        self, tmp_path: Path,
    ) -> None:
        db = _db(tmp_path)
        try:
            doc_id, claim_id = _claimed_task(db, "W:0")

            with pytest.raises(ClaimOwnershipError):
                db.complete_aspect(_fields(doc_id), claim_id=claim_id, claimant="W:9")

            assert _aspect_row(db, doc_id) is None
            db.tuples.ack(claim_id, "W:0")  # the rightful holder still holds it
        finally:
            db.close()

    def test_a_claim_on_another_documents_task_raises_task_mismatch(
        self, tmp_path: Path,
    ) -> None:
        db = _db(tmp_path)
        try:
            _, claim_id = _claimed_task(db)
            other_doc = register_real_doc_id(title=_stamp_title() + " other")

            with pytest.raises(ClaimTaskMismatchError):
                db.complete_aspect(_fields(other_doc), claim_id=claim_id, claimant="W:0")

            assert _aspect_row(db, other_doc) is None
            db.tuples.ack(claim_id, "W:0")
        finally:
            db.close()


class TestScenario4ClientHalf:
    def test_a_lapse_between_the_write_and_the_ack_leaves_the_row_and_the_ack_answers_404(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        db = _db(tmp_path)
        try:
            doc_id, claim_id = _claimed_task(db)
            real_upsert = db.document_aspects.upsert

            def upsert_then_lose_the_claim(record: Any, **kw: Any) -> bool:
                result = real_upsert(record, **kw)
                db.tuples.release(claim_id, "W:0")  # the lapse, after the write
                return result

            monkeypatch.setattr(db.document_aspects, "upsert", upsert_then_lose_the_claim)

            with pytest.raises(ClaimNotFoundError):
                db.complete_aspect(_fields(doc_id), claim_id=claim_id, claimant="W:0")

            assert _aspect_row(db, doc_id) is not None, "the write stands; a rerun overwrites it"
        finally:
            db.close()


class TestPairRule:
    @pytest.mark.parametrize("pair", [{"claim_id": "c"}, {"claimant": "W:0"}])
    def test_one_of_the_two_is_a_value_error_and_nothing_is_touched(
        self, tmp_path: Path, pair: dict[str, str],
    ) -> None:
        db = _db(tmp_path)
        try:
            doc_id, claim_id = _claimed_task(db)
            db.aspect_queue.enqueue(_COLLECTION, _SOURCE, "h", doc_id=doc_id)

            with pytest.raises(ValueError, match="claim_id.*claimant"):
                db.complete_aspect(_fields(doc_id), **pair)

            assert _aspect_row(db, doc_id) is None
            assert db.aspect_queue.pending_count() == 1
            db.tuples.ack(claim_id, "W:0")  # the claim is still live
        finally:
            db.close()


class TestRenameLock:
    def test_the_claim_path_holds_rename_lock_across_the_write_and_the_ack(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        db = _db(tmp_path)
        try:
            doc_id, claim_id = _claimed_task(db)
            real_ack = db.tuples.ack
            probes: list[bool] = []

            def probing_ack(*a: Any, **kw: Any) -> Any:
                got: list[bool] = []

                def probe() -> None:
                    ok = db.RENAME_LOCK.acquire(blocking=True, timeout=0.2)
                    got.append(ok)
                    if ok:
                        db.RENAME_LOCK.release()

                t = threading.Thread(target=probe, daemon=True)
                t.start()
                t.join(timeout=2.0)
                probes.append(got == [False])
                return real_ack(*a, **kw)

            monkeypatch.setattr(db.tuples, "ack", probing_ack)

            db.complete_aspect(_fields(doc_id), claim_id=claim_id, claimant="W:0")

            assert probes == [True], "another thread must block on RENAME_LOCK during the ack"
        finally:
            db.close()
