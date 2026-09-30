# SPDX-License-Identifier: AGPL-3.0-or-later
"""RDR-223 P2.4 (nexus-z0o2p.14): the client-side contracts of the ``nx index repo`` oversize
fallbacks that need no engine: what a request carries, which errors the run survives, and which
topologies keep the old write.

The engine-backed journeys are in ``tests/integration/test_rdr223_index_repo_oversize_journey.py``.
"""
from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import httpx
import pytest

from nexus.chunk_batcher import ChunkBatcher
from nexus.errors import BatchWriteFailedError, CombinedWriteEmbedTimeoutError, IndexRunVerifyRefused
from nexus.hook_registry import HookRegistry
from nexus.index_context import IndexContext

_DOC = "1.9.42"
_COLLECTION = "docs__oversize-units__voyage-context-3__v1"
_MODEL = "voyage-context-3"


class _RecordingCat:
    """A catalog writer that answers the way a healthy engine does and records each request."""

    def __init__(self, *, refuse_stamp: bool = False, last_append_extra: dict | None = None) -> None:
        self.calls: list[tuple[str, dict]] = []
        self._refuse = refuse_stamp
        self._last_append_extra = last_append_extra or {}

    def begin_index_run(self, doc_id, content_hash, run_id, collection, **kw):
        self.calls.append(("begin", {"doc_id": doc_id, "content_hash": content_hash, **kw}))
        return {"prior_chashes": [], "prior_count": 0}

    def write_manifest_many(self, docs, *, complete=None, sweep=True, chunks=None, **kw):
        self.calls.append(("write_many", {"rows": docs[0][1], "chunks": chunks or [],
                                          "sweep": sweep, "complete": complete, **kw}))
        return {"chunks_written": len(chunks or [])}

    def append_manifest_chunks(self, doc_id, rows, *, chunk_payload=None, sweep_chashes=None, **kw):
        self.calls.append(("append", {"rows": rows, "chunks": chunk_payload or [],
                                      "sweep_chashes": sweep_chashes, **kw}))
        return {"chunks_written": len(chunk_payload or []), "chunks_unreferenced": 0,
                **self._last_append_extra}

    def complete_index_run(self, doc_id, content_hash, chunk_count):
        self.calls.append(("complete", {"doc_id": doc_id, "chunk_count": chunk_count}))
        if self._refuse:
            raise IndexRunVerifyRefused(
                doc_id=doc_id, referenced=chunk_count, present=chunk_count - 1, missing=1,
                chunk_count=chunk_count)
        return {"ok": True}

    def fail_index_run(self, doc_id, error):
        self.calls.append(("fail", {"doc_id": doc_id, "error": error}))

    def close(self) -> None:
        pass


@pytest.fixture
def cat(monkeypatch) -> _RecordingCat:
    import nexus.doc_indexer as di
    import nexus.mcp_infra as mi

    rec = _RecordingCat()
    monkeypatch.setattr(mi, "get_catalog_writer", lambda *a, **k: rec)
    monkeypatch.setattr(di, "_fence_begin", lambda *a, **k: None)
    monkeypatch.setattr(di, "_fence_fail", lambda *a, **k: None)
    return rec


def _md(repo: Path, sections: int) -> Path:
    p = repo / "notes.md"
    p.write_text("".join(
        f"# Section {i}\n\nParagraph {i} of the oversize units file.\n\n" for i in range(sections)),
        encoding="utf-8")
    return p


def _ctx(repo: Path, *, doc_id: str = _DOC, batcher, db=None, hooks=None) -> IndexContext:
    return IndexContext(
        col=None, db=db if db is not None else MagicMock(), voyage_key="", voyage_client=None,
        repo_path=repo, corpus=_COLLECTION, embedding_model=_MODEL, git_meta={},
        now_iso="2026-09-30T00:00:00", force=True, doc_id_resolver=lambda p: doc_id,
        hooks=hooks, batcher=batcher)


def _rejecting_batcher() -> ChunkBatcher:
    return ChunkBatcher(flush=lambda *a: None, max_chunks=1)


def _cap(monkeypatch, n: int) -> None:
    import nexus.db.http_vector_client as hvc

    monkeypatch.setattr(hvc, "per_collection_chunk_cap", lambda *a, **k: n)


# ── the per-request grouping is the old upsert paging ─────────────────────────


@pytest.mark.parametrize("cap", [64, 16, 7])
def test_each_request_carries_the_chunks_the_old_upsert_page_did(cat, tmp_path, monkeypatch, cap) -> None:
    """The engine embeds one request's new chunks together (voyage-context-3 contextual
    embedding is per request: ``CombinedWriteService`` hands a request's chunks to
    ``EmbedderRouter.embedForCollectionWithUsage`` in ONE call, exactly as
    ``PgVectorRepository.upsertChunksInternal`` does for one ``upsert-chunks`` page). So the
    embedding of a chunk changes only if the set of chunks sharing its request changes. The old
    fallback sent ``HttpVectorClient.upsert_chunks`` pages cut by ``_upsert_page_bounds(n, cap,
    None, None)`` (no byte budget for CCE); the writer must cut the same pages."""
    from nexus.db.http_vector_client import _upsert_page_bounds
    from nexus.prose_indexer import index_prose_file

    _cap(monkeypatch, cap)
    n_sections = 150
    ctx = _ctx(tmp_path, batcher=_rejecting_batcher())
    index_prose_file(ctx, _md(tmp_path, n_sections))

    data = [(k, b) for k, b in cat.calls if k in ("write_many", "append")]
    sizes = [len(b["chunks"]) for _, b in data]
    n = sum(sizes)
    assert n > cap                                            # non-vacuity: several requests
    assert sizes == [e - s for s, e in _upsert_page_bounds(n, cap, None, None)]
    # In file order, first the write_many and then appends, the stamp after the last.
    assert [k for k, _ in data] == ["write_many"] + ["append"] * (len(data) - 1)
    rows = [r for _, b in data for r in b["rows"]]
    assert [r["position"] for r in rows] == list(range(n))
    assert cat.calls[-1][0] == "complete" and cat.calls[-1][1]["chunk_count"] == n


def test_the_sweep_the_engine_ran_and_the_one_it_skipped_reach_the_run_summary(tmp_path, monkeypatch) -> None:
    """The old path recorded each write's swept count and every skipped sweep (with the engine's
    reason) for the end-of-run summary; a skipped sweep must never be silent."""
    import nexus.doc_indexer as di
    import nexus.mcp_infra as mi
    from nexus.prose_indexer import index_prose_file

    rec = _RecordingCat(last_append_extra={
        "swept": 7,
        "sweep_detail": [
            {"doc_id": _DOC, "errored": True, "reason": "gate_timeout"},
            {"doc_id": _DOC, "errored": False},
        ]})
    monkeypatch.setattr(mi, "get_catalog_writer", lambda *a, **k: rec)
    monkeypatch.setattr(di, "_fence_begin", lambda *a, **k: None)
    _cap(monkeypatch, 16)
    mi.reset_superseded_sweep_stats()

    index_prose_file(_ctx(tmp_path, batcher=_rejecting_batcher()), _md(tmp_path, 40))

    stats = mi.get_superseded_sweep_stats()
    # Every append response carries the canned extra, so each of the appends counts it.
    appends = len([k for k, _ in rec.calls if k == "append"])
    assert appends >= 2
    assert stats["swept"] == 7 * appends
    assert stats["skipped"] == [
        {"doc_id": _DOC, "collection": _COLLECTION, "reason": "gate_timeout"}] * appends


# ── what the run survives ────────────────────────────────────────────────────


def test_a_refused_stamp_is_recorded_and_the_run_goes_on(tmp_path, monkeypatch) -> None:
    """The old fallback recorded a refused completion stamp in the record-level collector and
    went on (the manifest hook swallowed it); the writer raises it. The fallback must record it
    and keep the run alive, and the file's hooks still fire."""
    import nexus.doc_indexer as di
    import nexus.mcp_infra as mi
    from nexus.prose_indexer import index_prose_file

    rec = _RecordingCat(refuse_stamp=True)
    monkeypatch.setattr(mi, "get_catalog_writer", lambda *a, **k: rec)
    monkeypatch.setattr(di, "_fence_begin", lambda *a, **k: None)
    _cap(monkeypatch, 16)
    mi.reset_complete_refusals()
    fired: list[str] = []
    hooks = HookRegistry()
    hooks.register_batch(lambda ids, coll, contents, emb, metas: fired.append(coll))

    n = index_prose_file(_ctx(tmp_path, batcher=_rejecting_batcher(), hooks=hooks), _md(tmp_path, 40))

    assert n > 16
    assert mi.get_complete_refusals() == [_DOC]
    assert fired == [_COLLECTION]
    assert not any(k == "fail" for k, _ in rec.calls)         # the refusal leaves the fence as begin did


def test_a_write_that_fails_marks_the_fence_failed_and_propagates(cat, tmp_path, monkeypatch) -> None:
    from nexus.prose_indexer import index_prose_file

    _cap(monkeypatch, 16)

    def boom(*a, **k):
        raise BatchWriteFailedError(doc_id=_DOC, batch=2, reason="the engine reported it failed")

    cat.append_manifest_chunks = boom          # type: ignore[method-assign]
    with pytest.raises(BatchWriteFailedError):
        index_prose_file(_ctx(tmp_path, batcher=_rejecting_batcher()), _md(tmp_path, 40))
    assert [k for k, _ in cat.calls if k == "fail"] == ["fail"]


@pytest.mark.parametrize("exc", [
    httpx.HTTPStatusError("gateway", request=httpx.Request("POST", "http://x"),
                          response=httpx.Response(503, request=httpx.Request("POST", "http://x"))),
    httpx.HTTPStatusError("gateway", request=httpx.Request("POST", "http://x"),
                          response=httpx.Response(504, request=httpx.Request("POST", "http://x"))),
    httpx.HTTPStatusError("slow down", request=httpx.Request("POST", "http://x"),
                          response=httpx.Response(429, request=httpx.Request("POST", "http://x"))),
    CombinedWriteEmbedTimeoutError(collection=_COLLECTION, chunk_count=64, original="read timed out"),
], ids=["503", "504", "429", "embed-timeout"])
def test_a_transient_catalog_write_error_defers_the_file_like_a_transient_upsert(exc) -> None:
    """``run_file_loop`` cancels the whole run on the first exception it does not know. The old
    fallback's transient upsert errors (5xx, 429, upsert timeout) deferred the file to the next
    run's staleness check; the same errors now come from the catalog write."""
    from nexus.indexer import _contain_transient_upsert, reset_transient_upsert_deferred_count, \
        transient_upsert_deferred_count

    reset_transient_upsert_deferred_count()

    def fn() -> int:
        raise exc

    assert _contain_transient_upsert(fn, Path("big.md")) == 0
    assert transient_upsert_deferred_count() == 1


@pytest.mark.parametrize("exc", [
    httpx.HTTPStatusError("bad", request=httpx.Request("POST", "http://x"),
                          response=httpx.Response(400, request=httpx.Request("POST", "http://x"))),
    BatchWriteFailedError(doc_id=_DOC, batch=1, reason="failed"),
], ids=["400", "batch-write-failed"])
def test_a_permanent_write_error_still_propagates(exc) -> None:
    from nexus.indexer import _contain_transient_upsert

    def fn() -> int:
        raise exc

    with pytest.raises(type(exc)):
        _contain_transient_upsert(fn, Path("big.md"))


# ── the topologies that keep the old write ───────────────────────────────────


def test_a_file_with_no_catalog_identity_keeps_the_old_upsert_until_its_own_bead(cat, tmp_path, monkeypatch) -> None:
    """A file with no catalog document has no owner row to write a chunk with; counting and
    stopping those files is nexus-z0o2p.20. Until it lands the fallback writes them as before."""
    from nexus.prose_indexer import index_prose_file

    _cap(monkeypatch, 16)
    db = MagicMock()
    index_prose_file(_ctx(tmp_path, doc_id="", batcher=_rejecting_batcher(), db=db), _md(tmp_path, 40))
    assert db.upsert_chunks_with_embeddings.call_count == 1
    assert cat.calls == []


def test_a_run_with_no_chunk_batcher_keeps_the_old_write(cat, tmp_path, monkeypatch) -> None:
    """``_run_index`` builds the ChunkBatcher for every ``HttpVectorClient`` T3, which is every
    real install; a context without one holds a non-HTTP T3 (the in-memory test topology) that
    the engine's combined write cannot reach."""
    from nexus.prose_indexer import index_prose_file

    _cap(monkeypatch, 16)
    db = MagicMock()
    index_prose_file(_ctx(tmp_path, batcher=None, db=db), _md(tmp_path, 40))
    assert db.upsert_chunks_with_embeddings.call_count == 1
    assert [k for k, _ in cat.calls if k in ("write_many", "append")] == []
