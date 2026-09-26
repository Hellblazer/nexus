# SPDX-License-Identifier: AGPL-3.0-or-later
"""nexus-edjmu: a pipeline row is one run of one document, not a content hash.

The bead's scenario, end to end through ``pipeline_index_pdf`` against the
fake engine (whose contract ``tests/db/test_pipeline_fake_engine_parity.py``
pins to the Java ``PipelineHandlerTest``): a byte-identical PDF at a second
path, with the first path's row still present as a leftover, must upload its
own chunks and fire the manifest hook with its OWN catalog document, and
must never hang. The parked key-widen attempt (branch
edjmu-wal-per-row-wip, T2 nexus/critique-nexus-edjmu-33q80-pipeline-key)
deadlocked here because the second row's counters were seeded from the
sibling's already-uploaded WAL; its own test had to monkeypatch
``delete_pipeline_data`` to a no-op to construct the precondition. Nothing
is monkeypatched here: the leftover is seeded exactly as a crash between
``mark_completed`` and ``delete_pipeline_data`` leaves it.
"""
from __future__ import annotations

import threading
from pathlib import Path
from unittest.mock import MagicMock, create_autospec, patch

import httpx
import pytest

from nexus.db.t3 import T3Database
from nexus.db.http_pipeline_client import HttpPipelineDB, PipelineRunFenced
from nexus.pipeline_stages import pipeline_index_pdf
from tests.pipeline_fake_engine import FakePipelineEngine, make_fake_engine_db
from tests.test_pipeline_stages import _P_CHK, _P_EXT, _embed, _er, _fx, _tc

_HASH = "same-bytes-" + "0" * 53


@pytest.fixture(autouse=True)
def _fast_poll(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("nexus.pipeline_stages._POLL_INTERVAL", 0.01)


@pytest.fixture(autouse=True)
def _stub_fence_complete(monkeypatch: pytest.MonkeyPatch) -> None:
    # As in tests/test_pipeline_stages.py: the autospec T3 double lands no
    # chunk in the real substrate, so the fence's verify-then-stamp would
    # refuse; the fence is tests/test_5xn3k_fence_ordering.py's territory.
    monkeypatch.setattr("nexus.doc_indexer._fence_complete", lambda *a, **k: None)


@pytest.fixture()
def engine() -> FakePipelineEngine:
    return make_fake_engine_db()[1]


def _client_for(engine: FakePipelineEngine) -> HttpPipelineDB:
    """A fresh instance sharing *engine* (one instance per run, as production)."""
    db, _ = make_fake_engine_db()
    db._client = httpx.Client(transport=httpx.MockTransport(engine.handler))
    db._clock = engine.clock
    return db


def _seed_completed_leftover(engine: FakePipelineEngine, pdf_path: str, collection: str) -> int | None:
    """A run that reached mark_completed and died before delete_pipeline_data."""
    db = _client_for(engine)
    db.create_pipeline(_HASH, pdf_path, collection)
    db.write_page(_HASH, 0, "Page 0 content.")
    db.write_chunk(_HASH, 0, "chunk 0", "cid-0", embedding=b"")
    db.mark_uploaded(_HASH, [0])
    db.update_progress(_HASH, total_pages=1, pages_extracted=1, chunks_created=1, chunks_uploaded=1)
    db.mark_completed(_HASH)
    return db.pipeline_id_for(_HASH)


def _run(engine: FakePipelineEngine, pdf_path: str, collection: str, doc_id: str, *, force: bool = False):
    hooks = MagicMock()
    t3 = create_autospec(T3Database, instance=True)
    col = MagicMock()
    col.get.return_value = {"ids": [], "metadatas": []}
    t3.get_or_create_collection.return_value = col
    fc = _tc(("chunk 0", 0, {"page_number": 1, "chunk_type": "text"}),
             ("chunk 1", 1, {"page_number": 2, "chunk_type": "text"}))
    result: dict = {}

    def _go() -> None:
        with patch(_P_EXT) as ME, patch(_P_CHK) as MC:
            ME.return_value.extract.side_effect = _fx(2, _er(2))
            MC.return_value.chunk.return_value = fc
            result["total"] = pipeline_index_pdf(
                Path(pdf_path), _HASH, collection, t3,
                db=_client_for(engine), embed_fn=_embed, corpus="test",
                doc_id=doc_id, hooks=hooks, force=force,
            )

    # An untimed wait() inside pipeline_index_pdf is the parked attempt's
    # failure mode; run on a thread so a hang is a failed assertion, not a
    # hung suite.
    worker = threading.Thread(target=_go, daemon=True)
    worker.start()
    worker.join(timeout=30)
    assert not worker.is_alive(), "pipeline_index_pdf hung (the sibling-seeded counter deadlock)"
    return result["total"], t3, hooks


class TestSecondDocumentSharingTheBytes:
    def test_second_path_with_a_completed_leftover_uploads_its_own_chunks(self, engine) -> None:
        leftover_id = _seed_completed_leftover(engine, "/docs/a.pdf", "docs__test")

        total, t3, hooks = _run(engine, "/docs/b.pdf", "docs__test", doc_id="1.9.2")

        assert total == 2, "the second path must index, never return the leftover's 0"
        t3.upsert_chunks_with_embeddings.assert_called_once()
        assert hooks.fire_batch.call_args.kwargs["catalog_doc_id"] == "1.9.2"
        # The leftover is untouched: a different document's run.
        rows = engine.rows_for(_HASH)
        assert [r["pdf_path"] for r in rows] == ["/docs/a.pdf"]
        assert rows[0]["pipeline_id"] == leftover_id
        assert rows[0]["status"] == "completed"

    def test_second_collection_same_path_is_its_own_run(self, engine) -> None:
        _seed_completed_leftover(engine, "/docs/a.pdf", "docs__one")
        total, t3, hooks = _run(engine, "/docs/a.pdf", "docs__two", doc_id="1.9.3")
        assert total == 2
        t3.upsert_chunks_with_embeddings.assert_called_once()
        assert t3.upsert_chunks_with_embeddings.call_args.args[0] == "docs__two"
        assert [r["collection"] for r in engine.rows_for(_HASH)] == ["docs__one"]

    def test_same_document_completed_leftover_reruns_instead_of_skipping(self, engine) -> None:
        """The tombstone-and-reindex trigger: the same path, a leftover
        completed row, a NEW catalog document. Pre-fix this was the bead's
        exact symptom via a different trigger."""
        _seed_completed_leftover(engine, "/docs/a.pdf", "docs__test")
        total, t3, hooks = _run(engine, "/docs/a.pdf", "docs__test", doc_id="1.9.4")
        assert total == 2
        t3.upsert_chunks_with_embeddings.assert_called_once()
        assert hooks.fire_batch.call_args.kwargs["catalog_doc_id"] == "1.9.4"
        assert engine.rows_for(_HASH) == [], "the re-run's own cleanup removed its row"

    def test_force_on_one_document_leaves_the_siblings_run(self, engine) -> None:
        """The pre-create --force delete names THIS document; a sibling
        sharing the bytes keeps its row and WAL (the critic's ship-blocker
        on the design memo)."""
        sibling_id = _seed_completed_leftover(engine, "/docs/a.pdf", "docs__test")
        total, _t3, _hooks = _run(engine, "/docs/b.pdf", "docs__test", doc_id="1.9.5", force=True)
        assert total == 2
        rows = engine.rows_for(_HASH)
        assert [r["pipeline_id"] for r in rows] == [sibling_id]
        assert engine.wal_hashes() == {_HASH}, "the sibling's pages and chunks survive A's force"

    def test_two_documents_concurrently_on_their_own_instances(self, engine) -> None:
        """Both runs live at once, each on its own HttpPipelineDB; neither
        seeds from the other and both complete."""
        results: dict[str, tuple] = {}

        def _one(path: str, doc_id: str) -> None:
            results[path] = _run(engine, path, "docs__test", doc_id=doc_id)

        ta = threading.Thread(target=_one, args=("/docs/x.pdf", "1.9.6"), daemon=True)
        tb = threading.Thread(target=_one, args=("/docs/y.pdf", "1.9.7"), daemon=True)
        ta.start(); tb.start()
        ta.join(timeout=45); tb.join(timeout=45)
        assert not ta.is_alive() and not tb.is_alive()
        assert {results["/docs/x.pdf"][0], results["/docs/y.pdf"][0]} == {2}
        assert results["/docs/x.pdf"][2].fire_batch.call_args.kwargs["catalog_doc_id"] == "1.9.6"
        assert results["/docs/y.pdf"][2].fire_batch.call_args.kwargs["catalog_doc_id"] == "1.9.7"
        assert engine.rows_for(_HASH) == []


def _hold_a_deferred_sweep(doc_id: str) -> None:
    """nexus-4pj54: seed the state an earlier batch of this run leaves when
    its REPLACE had no completeness claim, so the fenced abort has a
    deferred sweep to dispose of."""
    from nexus.mcp_infra import _stash_pending_sweep, reset_superseded_sweep_stats

    reset_superseded_sweep_stats()
    _stash_pending_sweep(doc_id, "docs__test", {"superseded-" + doc_id})


def _assert_deferred_sweep_discarded(doc_id: str) -> None:
    """The fenced path skips _fence_fail, so it must discard the held
    sweep itself: dropped, counted, and never swept."""
    from nexus.mcp_infra import _PENDING_SWEEP_CANDIDATES, get_superseded_sweep_stats

    assert doc_id not in _PENDING_SWEEP_CANDIDATES, "a fenced run leaked its deferred sweep"
    stats = get_superseded_sweep_stats()
    assert stats["deferred_discarded"] == 1
    assert stats["swept"] == 0, "a fenced run must never sweep"


class TestRunEpochFence:
    """nexus-8vu8p: a run the engine fenced stops without touching the row,
    its WAL, or the catalog document, all of which belong to the new owner.
    """

    def _owner_takes_over(self, engine: FakePipelineEngine) -> HttpPipelineDB:
        """A second instance resumes the run: the row's epoch bumps and the
        owner writes its own page. The row is made resumable by marking it
        failed engine-side (an unfenced legacy-shaped call) rather than by
        aging its heartbeat, which the stale run's own threads keep
        refreshing while they poll."""
        for pid, row in engine.pipelines.items():
            if row["content_hash"] == _HASH:
                engine.fail({"pipeline_id": pid, "error": "made resumable"})
        owner = _client_for(engine)
        assert owner.create_pipeline(_HASH, "/docs/a.pdf", "docs__test") == "resuming"
        owner.write_page(_HASH, 0, "owner's page")
        owner.flush(_HASH)
        return owner

    def test_stale_runs_own_write_is_fenced_and_it_touches_nothing(self, engine, monkeypatch) -> None:
        """The takeover lands mid-extraction (the stale run blocked on a
        page); the stale run's next flush is fenced, it raises
        PipelineRunFenced, and it touches neither the row nor the WAL nor
        the catalog document."""
        fence_fail = MagicMock()
        monkeypatch.setattr("nexus.doc_indexer._fence_fail", fence_fail)
        _hold_a_deferred_sweep("1.9.8")
        stale = _client_for(engine)
        owner_box: dict = {}

        def _extract_then_lose_ownership(pdf_path, *, on_page=None, **kw):
            owner_box["owner"] = self._owner_takes_over(engine)
            on_page(0, "stale page 0", {"page_number": 1, "text_length": 12})
            return _er(1)

        hooks = MagicMock()
        t3 = create_autospec(T3Database, instance=True)
        with patch(_P_EXT) as ME, patch(_P_CHK) as MC:
            ME.return_value.extract.side_effect = _extract_then_lose_ownership
            MC.return_value.chunk.return_value = _tc(("chunk 0", 0, {"page_number": 1, "chunk_type": "text"}))
            with pytest.raises(PipelineRunFenced) as exc:
                pipeline_index_pdf(Path("/docs/a.pdf"), _HASH, "docs__test", t3,
                                   db=stale, embed_fn=_embed, doc_id="1.9.8", hooks=hooks)
        owner = owner_box["owner"]
        pid = owner.pipeline_id_for(_HASH)
        assert exc.value.pipeline_id == pid
        assert (exc.value.run_epoch, exc.value.current_epoch) == (0, 1)
        row = engine.pipelines[pid]
        assert row["status"] == "resuming", "the stale run must not mark the owner's row failed"
        assert row["run_epoch"] == 1
        assert [r["page_text"] for r in owner.read_pages(_HASH)] == ["owner's page"], "the owner's WAL survives"
        fence_fail.assert_not_called()
        t3.upsert_chunks_with_embeddings.assert_not_called()
        _assert_deferred_sweep_discarded("1.9.8")

    def test_fence_discovered_at_cleanup_skips_the_catalog_stamp(self, engine, monkeypatch) -> None:
        """The stale run's ORIGINAL failure is unrelated; the takeover happens
        before its terminal bookkeeping runs, so the fence is discovered
        inside mark_failed's /fail call. _fence_fail must still be skipped."""
        fence_fail = MagicMock()
        monkeypatch.setattr("nexus.doc_indexer._fence_fail", fence_fail)
        _hold_a_deferred_sweep("1.9.9")
        stale = _client_for(engine)
        owner_box: dict = {}

        def _extract_then_lose_ownership(pdf_path, **kw):
            owner_box["owner"] = self._owner_takes_over(engine)
            raise RuntimeError("OCR died")

        t3 = create_autospec(T3Database, instance=True)
        with patch(_P_EXT) as ME, patch(_P_CHK) as MC:
            ME.return_value.extract.side_effect = _extract_then_lose_ownership
            MC.return_value.chunk.return_value = []
            # Either the extractor's own error or the chunker's (it reads the
            # owner's unfenced page and produces no chunk) wins FIRST_EXCEPTION;
            # what matters is that the first exception is NOT the fence.
            with pytest.raises(RuntimeError) as exc:
                pipeline_index_pdf(Path("/docs/a.pdf"), _HASH, "docs__test", t3,
                                   db=stale, embed_fn=_embed, doc_id="1.9.9", hooks=MagicMock())
        assert not isinstance(exc.value, PipelineRunFenced)
        owner = owner_box["owner"]
        row = engine.pipelines[owner.pipeline_id_for(_HASH)]
        assert row["status"] == "resuming", "the stale cleanup's mark_failed was fenced"
        assert row["run_epoch"] == 1
        assert [r["page_text"] for r in owner.read_pages(_HASH)] == ["owner's page"], "the stale clear_wal was fenced"
        fence_fail.assert_not_called()
        _assert_deferred_sweep_discarded("1.9.9")

    def test_completed_leftover_reset_fences_the_first_runs_delayed_write(self, engine) -> None:
        first = _client_for(engine)
        first.create_pipeline(_HASH, "/docs/a.pdf", "docs__test")
        first.mark_completed(_HASH)
        second = _client_for(engine)
        assert second.create_pipeline(_HASH, "/docs/a.pdf", "docs__test") == "created"
        assert second.run_epoch_for(_HASH) == 1
        first.write_page(_HASH, 0, "delayed")
        with pytest.raises(PipelineRunFenced):
            first.flush(_HASH)
        assert second.read_pages(_HASH) == []
