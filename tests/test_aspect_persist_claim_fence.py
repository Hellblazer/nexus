# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""RDR-235 client half (nexus-5r2sc.3.1): ``complete_aspect`` takes a claim pair.

The engine tests run against the real engine substrate, with a real task on the ``aspects`` tuple
queue. Test Plan scenario 9 (with a claim: the write and the tuple ack happen
and the table is untouched; without one: the write and ``mark_done`` happen and
a queued task for that document stays queued) and scenario 4's client half (a
lapse between the write and the ack leaves the aspect row, and the ack answers
``ClaimNotFound``), plus the refusals of the claim fence (404, 403 twice) and the
pair rule (one of the two is a ``ValueError``).

VACUITY (each run red, then restored, recorded in the bead): stubbing out the
claim branch of ``T2Database.complete_aspect`` (calling ``mark_done`` instead of
``tuples.ack``) fails the claim-path (which also holds the RENAME_LOCK probe) and
lapse tests; dropping the pair from ``HttpDocumentAspectsStore.upsert``'s body
fails the refusal test, because the engine then writes unfenced and nothing raises.

Each engine test is one scenario with several steps, folded to stay under the tenant
ceiling (tests/storage_touch_ceiling.toml); a failed step names itself in its
assertion message. The pair rule fails before any store is reached and runs
without a tenant (nexus-9edxe).
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


@pytest.fixture(autouse=True)
def _aspect_queue_table_mode(monkeypatch: pytest.MonkeyPatch) -> None:
    """This module tests the aspect queue TABLE path, so it says so: the unset
    default is ``tuples`` since nexus-lnwn1 (RDR-235 Activation Step 1)."""
    monkeypatch.setenv("NX_ASPECT_QUEUE", "table")

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


@pytest.mark.t2_tenant
class TestScenario9Persist:
    def test_with_a_claim_then_without_one_the_write_and_its_own_clear_happen(
        self, tmp_path: Path,
    ) -> None:
        """Both halves of scenario 9 on one tenant (nexus-9edxe folded the two): the
        claim path (steps named 'claim'), then the table path ('table')."""
        db = _db(tmp_path)
        patched = pytest.MonkeyPatch()  # not the fixture: undo() must not revert the suite's own patches
        try:
            doc_id, claim_id = _claimed_task(db)
            # A table row for the same document: the claim path must not clear it.
            db.aspect_queue.enqueue(_COLLECTION, _SOURCE, "h", doc_id=doc_id)
            assert db.aspect_queue.pending_count() == 1

            # The RENAME_LOCK probe rides BOTH real calls, the fenced upsert and the
            # ack: another thread must block on the lock during each, or a lock
            # that covers only one half (nexus-5r2sc.3.9 M3) would stay green.
            def lock_is_held_by_the_caller() -> bool:
                got: list[bool] = []

                def probe() -> None:
                    ok = db.RENAME_LOCK.acquire(blocking=True, timeout=0.2)
                    got.append(ok)
                    if ok:
                        db.RENAME_LOCK.release()

                t = threading.Thread(target=probe, daemon=True)
                t.start()
                t.join(timeout=2.0)
                return got == [False]

            real_ack = db.tuples.ack
            real_upsert = db.document_aspects.upsert
            probes: list[str] = []

            def probing_ack(*a: Any, **kw: Any) -> Any:
                if lock_is_held_by_the_caller():
                    probes.append("ack")
                return real_ack(*a, **kw)

            def probing_upsert(*a: Any, **kw: Any) -> Any:
                if lock_is_held_by_the_caller():
                    probes.append("upsert")
                return real_upsert(*a, **kw)

            patched.setattr(db.tuples, "ack", probing_ack)
            patched.setattr(db.document_aspects, "upsert", probing_upsert)

            written = db.complete_aspect(_fields(doc_id), claim_id=claim_id, claimant="W:0")

            assert written is True, "claim: written"
            assert _aspect_row(db, doc_id) is not None, "claim: the aspect row exists"
            with pytest.raises(ClaimNotFoundError):  # the ack consumed the claim
                real_ack(claim_id, "W:0")
            assert _task_rows(db) == 0, "claim: the task is consumed"
            assert db.aspect_queue.pending_count() == 1, "claim: mark_done must not run on the claim path"
            assert probes == ["upsert", "ack"], (
                "claim: another thread must block on RENAME_LOCK during the fenced upsert AND the ack"
            )
            patched.undo()

            # Without a claim: the write and mark_done happen, and a queued task for the
            # document stays queued (the claim path's table row is still pending: 1).
            table_doc = register_real_doc_id(title=_stamp_title())
            _enqueue_task(db, table_doc)
            db.aspect_queue.enqueue(_COLLECTION, "/table.md", "h", doc_id=table_doc)
            assert db.aspect_queue.pending_count() == 2

            written = db.complete_aspect({**_fields(table_doc), "source_path": "/table.md"})

            assert written is True, "table: written"
            assert _aspect_row(db, table_doc) is not None, "table: the aspect row exists"
            assert db.aspect_queue.pending_count() == 1, "table: today's path clears its own table row only"
            assert _task_rows(db) == 1, "table: no client can delete a task by document (F22)"
            assert _claim(db, "W:1"), "table: and the task is still claimable"
        finally:
            patched.undo()
            db.close()


@pytest.mark.t2_tenant
class TestFenceRefusalsAndTheLapse:
    def test_each_refusal_of_the_fence_writes_nothing_and_a_lapse_after_the_write_leaves_the_row(
        self, tmp_path: Path,
    ) -> None:
        """Four refusals in turn, each leaving the queue as it found it, with scenario 4's
        client half (a lapse between the write and the ack) between the third and
        the fourth: a lapse by release leaves a claimable task, so it goes last, and
        the lapse step consumes its own released task before the fourth begins."""
        db = _db(tmp_path)
        patched = pytest.MonkeyPatch()
        try:
            # 403 ownership: another claimant's pair; the claim stays live.
            doc_id, claim_id = _claimed_task(db, "W:0")
            with pytest.raises(ClaimOwnershipError):
                db.complete_aspect(_fields(doc_id), claim_id=claim_id, claimant="W:9")
            assert _aspect_row(db, doc_id) is None, "ownership wrote"
            db.tuples.ack(claim_id, "W:0")  # the rightful holder still holds it

            # 403 task mismatch: a live claim on another document's task.
            _, claim_id = _claimed_task(db)
            other_doc = register_real_doc_id(title=_stamp_title() + " other")
            with pytest.raises(ClaimTaskMismatchError):
                db.complete_aspect(_fields(other_doc), claim_id=claim_id, claimant="W:0")
            assert _aspect_row(db, other_doc) is None, "task mismatch wrote"
            db.tuples.ack(claim_id, "W:0")

            # 404 not found, the claim acked away.
            doc_id, claim_id = _claimed_task(db)
            db.aspect_queue.enqueue(_COLLECTION, _SOURCE, "h", doc_id=doc_id)
            db.tuples.ack(claim_id, "W:0")
            with pytest.raises(ClaimNotFoundError):
                db.complete_aspect(_fields(doc_id), claim_id=claim_id, claimant="W:0")
            assert _aspect_row(db, doc_id) is None, "a lapse by ack wrote"
            assert db.aspect_queue.pending_count() == 1, "the table is not touched (ack lapse)"
            assert _task_rows(db) == 0

            # Scenario 4's client half: a lapse between the write and the ack leaves the
            # row, and the ack answers ClaimNotFound.
            doc_id, claim_id = _claimed_task(db)
            real_upsert = db.document_aspects.upsert

            def upsert_then_lose_the_claim(record: Any, **kw: Any) -> bool:
                result = real_upsert(record, **kw)
                db.tuples.release(claim_id, "W:0")  # the lapse, after the write
                return result

            patched.setattr(db.document_aspects, "upsert", upsert_then_lose_the_claim)
            with pytest.raises(ClaimNotFoundError):
                db.complete_aspect(_fields(doc_id), claim_id=claim_id, claimant="W:0")
            assert _aspect_row(db, doc_id) is not None, "lapse after the write: the write stands; a rerun overwrites it"
            patched.undo()
            db.tuples.ack(_claim(db, "W:0"), "W:0")  # the released task is claimable again; consume it
            assert _task_rows(db) == 0

            # 404 not found, the claim released (the task is claimable again).
            doc_id, claim_id = _claimed_task(db)
            db.tuples.release(claim_id, "W:0")
            rows_before = _task_rows(db)
            with pytest.raises(ClaimNotFoundError):
                db.complete_aspect(_fields(doc_id), claim_id=claim_id, claimant="W:0")
            assert _aspect_row(db, doc_id) is None, "a lapse by release wrote"
            assert db.aspect_queue.pending_count() == 1, "the table is not touched (release lapse)"
            assert _task_rows(db) == rows_before
        finally:
            patched.undo()
            db.close()


class TestPairRule:
    def test_one_of_the_two_is_a_value_error_and_nothing_is_touched(self) -> None:
        """Refused before any store is reached, so no engine and no tenant: the
        receiver is a stand-in that fails the test on any attribute read
        (``RENAME_LOCK``, ``document_aspects``, ``aspect_queue``, ``tuples``)."""
        from nexus.db.t2 import T2Database

        class _Untouchable:
            def __getattr__(self, name: str) -> Any:
                raise AssertionError(f"complete_aspect reached {name!r} before refusing the pair")

        for pair in ({"claim_id": "c"}, {"claimant": "W:0"}):
            with pytest.raises(ValueError, match="claim_id.*claimant"):
                T2Database.complete_aspect(_Untouchable(), _fields("1.2.3"), **pair)
