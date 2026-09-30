# SPDX-License-Identifier: AGPL-3.0-or-later
"""RDR-223 P2.4 (nexus-z0o2p.14): the client-side contracts of the ``nx index repo`` oversize
fallbacks that need no engine: what a request carries, which errors the run survives, and which
topologies keep the old write. These run in PR CI; the engine-backed journeys are in
``tests/integration/test_rdr223_index_repo_oversize_journey.py``.

All three fallbacks (code, prose and RDR, PDF) are driven through the same recording catalog writer.
"""
from __future__ import annotations

from pathlib import Path
from typing import Callable
from unittest.mock import MagicMock

import httpx
import pytest

from nexus.chunk_batcher import ChunkBatcher
from nexus.db.http_vector_client import HttpVectorClient
from nexus.errors import BatchWriteFailedError, CombinedWriteEmbedTimeoutError, IndexRunVerifyRefused
from nexus.hook_registry import HookRegistry
from nexus.index_context import IndexContext
from nexus.oversize_write import OversizeWriteDeferred

_DOC = "1.9.42"
_MODEL = "voyage-context-3"
_CCE = "docs__oversize-units__voyage-context-3__v1"
_CODE = "code__oversize-units__voyage-code-3__v1"
_NOW = "2026-09-30T00:00:00"
_REQ = httpx.Request("POST", "http://x")


def _status(code: int) -> httpx.HTTPStatusError:
    return httpx.HTTPStatusError("boom", request=_REQ, response=httpx.Response(code, request=_REQ))


class _RecordingCat:
    """A catalog writer that answers the way a healthy engine does and records each request.
    ``raises`` maps a method name to an exception it raises when called."""

    def __init__(self, *, refuse_stamp: bool = False, last_append_extra: dict | None = None,
                 refuse_in_write_many: bool = False, raises: dict | None = None) -> None:
        self.calls: list[tuple[str, dict]] = []
        self._refuse = refuse_stamp
        self._last_append_extra = last_append_extra or {}
        self._refuse_in_write_many = refuse_in_write_many
        self._raises = raises or {}

    def _maybe_raise(self, name: str) -> None:
        if name in self._raises:
            raise self._raises[name]

    def begin_index_run(self, doc_id, content_hash, run_id, collection, **kw):
        self.calls.append(("begin", {"doc_id": doc_id, "content_hash": content_hash, **kw}))
        self._maybe_raise("begin_index_run")
        return {"prior_chashes": [], "prior_count": 0}

    def write_manifest_many(self, docs, *, complete=None, sweep=True, chunks=None, **kw):
        self.calls.append(("write_many", {"rows": docs[0][1], "chunks": chunks or [],
                                          "sweep": sweep, "complete": complete, **kw}))
        self._maybe_raise("write_manifest_many")
        resp: dict = {"chunks_written": len(chunks or [])}
        if self._refuse_in_write_many:
            resp["complete_refused"] = [{
                "doc_id": docs[0][0], "referenced": len(docs[0][1]), "missing": 1,
                "chunk_count": len(docs[0][1])}]
        return resp

    def append_manifest_chunks(self, doc_id, rows, *, chunk_payload=None, sweep_chashes=None, **kw):
        self.calls.append(("append", {"rows": rows, "chunks": chunk_payload or [],
                                      "sweep_chashes": sweep_chashes, **kw}))
        self._maybe_raise("append_manifest_chunks")
        return {"chunks_written": len(chunk_payload or []), "chunks_unreferenced": 0,
                **self._last_append_extra}

    def complete_index_run(self, doc_id, content_hash, chunk_count):
        self.calls.append(("complete", {"doc_id": doc_id, "chunk_count": chunk_count}))
        self._maybe_raise("complete_index_run")
        if self._refuse:
            raise IndexRunVerifyRefused(
                doc_id=doc_id, referenced=chunk_count, present=chunk_count - 1, missing=1,
                chunk_count=chunk_count)
        return {"ok": True}

    def fail_index_run(self, doc_id, error):
        self.calls.append(("fail", {"doc_id": doc_id, "error": error}))

    def close(self) -> None:
        pass

    def kinds(self) -> list[str]:
        return [k for k, _ in self.calls]


@pytest.fixture
def cat(monkeypatch) -> _RecordingCat:
    return _install(monkeypatch, _RecordingCat())


def _install(monkeypatch, rec: _RecordingCat) -> _RecordingCat:
    import nexus.doc_indexer as di
    import nexus.mcp_infra as mi

    monkeypatch.setattr(mi, "get_catalog_writer", lambda *a, **k: rec)
    monkeypatch.setattr(di, "_fence_begin", lambda *a, **k: None)
    monkeypatch.setattr(di, "_fence_fail", lambda *a, **k: None)
    monkeypatch.setattr("nexus.retry.time.sleep", lambda *_a, **_k: None)
    return rec


def _http_db() -> MagicMock:
    """A service-backed T3 that fails the test if anything on it is called."""
    return MagicMock(spec=HttpVectorClient)


def _rejecting_batcher() -> ChunkBatcher:
    return ChunkBatcher(flush=lambda *a: None, max_chunks=1)


def _cap(monkeypatch, n: int) -> None:
    import nexus.db.http_vector_client as hvc

    monkeypatch.setattr(hvc, "per_collection_chunk_cap", lambda *a, **k: n)


def _ctx(repo: Path, *, corpus: str, doc_id: str = _DOC, batcher, db=None,
         hooks=None) -> IndexContext:
    return IndexContext(
        col=None, db=db if db is not None else _http_db(), voyage_key="", voyage_client=None,
        repo_path=repo, corpus=corpus, embedding_model=_MODEL, git_meta={}, now_iso=_NOW,
        chunk_lines=6, force=True, doc_id_resolver=lambda p: doc_id, hooks=hooks, batcher=batcher)


# ── one runner per fallback ──────────────────────────────────────────────────


def _md(repo: Path, sections: int) -> Path:
    p = repo / "notes.md"
    p.write_text("".join(
        f"# Section {i}\n\nParagraph {i} of the oversize units file.\n\n" for i in range(sections)),
        encoding="utf-8")
    return p


def _run_prose(tmp_path, monkeypatch, *, doc_id=_DOC, batcher="reject", db=None, hooks=None,
               sections=40) -> int:
    from nexus.prose_indexer import index_prose_file

    b = _rejecting_batcher() if batcher == "reject" else batcher
    ctx = _ctx(tmp_path, corpus=_CCE, doc_id=doc_id, batcher=b, db=db, hooks=hooks)
    return index_prose_file(ctx, _md(tmp_path, sections))


def _run_code(tmp_path, monkeypatch, *, doc_id=_DOC, batcher="reject", db=None, hooks=None,
              sections=40) -> int:
    from nexus.code_indexer import index_code_file

    p = tmp_path / "mod.py"
    p.write_text("".join(
        f"def fn_{i}():\n    return {i}  # oversize units unique {i}\n\n" for i in range(sections)),
        encoding="utf-8")
    b = _rejecting_batcher() if batcher == "reject" else batcher
    ctx = _ctx(tmp_path, corpus=_CODE, doc_id=doc_id, batcher=b, db=db, hooks=hooks)
    return index_code_file(ctx, p)


def _run_pdf(tmp_path, monkeypatch, *, doc_id=_DOC, batcher="reject", db=None, hooks=None,
             sections=40) -> int:
    import hashlib

    import nexus.doc_indexer as di
    from nexus.indexer import _index_pdf_file
    from nexus.metadata_schema import make_chunk_metadata

    p = tmp_path / "big.pdf"
    p.write_bytes(b"%PDF-1.4 oversize units\n")
    ch = hashlib.sha256(p.read_bytes()).hexdigest()
    prepared = []
    for i in range(sections):
        text = f"Page {i} of the oversize units pdf."
        h = hashlib.sha256(text.encode()).hexdigest()
        meta = make_chunk_metadata(
            content_type="pdf", chunk_text_hash=h, content_hash=ch, chunk_start_char=i * 10,
            chunk_end_char=i * 10 + len(text), page_number=i + 1, indexed_at=_NOW,
            embedding_model=_MODEL, title="Big", tags="pdf", category="paper")
        prepared.append((h, text, meta))
    monkeypatch.setattr(di, "_pdf_chunks", lambda *a, **k: [(i, t, dict(m)) for i, t, m in prepared])
    b = _rejecting_batcher() if batcher == "reject" else batcher
    return _index_pdf_file(
        p, tmp_path, _CCE, _MODEL, None, db if db is not None else _http_db(), "", {}, _NOW, 0.0,
        force=True, embed_fn=lambda texts: [[] for _ in texts], doc_id_resolver=lambda p_: doc_id,
        hooks=hooks, batcher=b)


_RUNNERS: dict[str, Callable] = {"code": _run_code, "prose": _run_prose, "pdf": _run_pdf}


@pytest.fixture(params=sorted(_RUNNERS))
def run(request) -> Callable:
    return _RUNNERS[request.param]


# ── the wiring, per fallback ─────────────────────────────────────────────────


def test_every_fallback_writes_through_the_writer_and_never_touches_the_db(
    cat, tmp_path, monkeypatch, run,
) -> None:
    """Chunks and owner rows go out in the same requests, the T3 handle is never used, the manifest
    hook is excluded from the file's hook dispatch, and the other hooks fire once for the file."""
    import nexus.mcp_infra as mi

    _cap(monkeypatch, 7)
    fired: list[int] = []
    manifest_hook_calls: list[int] = []

    def manifest_hook_spy(ids, coll, contents, emb, metas, *, catalog_doc_id="", manifest_complete=None):
        manifest_hook_calls.append(len(ids))

    monkeypatch.setattr(mi, "manifest_write_batch_hook", manifest_hook_spy)
    hooks = HookRegistry()
    hooks.register_batch(lambda ids, coll, contents, emb, metas: fired.append(len(ids)))
    hooks.register_batch(manifest_hook_spy)
    db = _http_db()

    n = run(tmp_path, monkeypatch, db=db, hooks=hooks)

    assert db.mock_calls == []                                   # ctx.db is never touched
    data = [(k, b) for k, b in cat.calls if k in ("write_many", "append")]
    assert len(data) == -(-n // 7) > 1                           # non-vacuity: several requests
    assert [k for k, _ in data] == ["write_many"] + ["append"] * (len(data) - 1)
    assert all(len(b["chunks"]) <= 7 for _, b in data)
    assert sum(len(b["chunks"]) for _, b in data) == n
    assert not data[0][1]["sweep"] and data[0][1]["complete"] is None
    assert cat.kinds()[0] == "begin" and cat.kinds()[-1] == "complete"
    assert cat.calls[-1][1]["chunk_count"] == n
    rows = [r for _, b in data for r in b["rows"]]
    assert [r["position"] for r in rows] == list(range(n))
    assert [r["chunk_index"] for r in rows] == list(range(n))    # position, as on the batcher path
    # Every request asks for the metadata merge (the old upsert's semantics), naming the keys this
    # writer owns and dropped, and never the enrichment keys.
    keys = [b["metadata_delete_keys"] for _, b in data]
    assert all(b["metadata_merge"] is True for _, b in data)
    assert keys[0] and all(k == keys[0] for k in keys)
    assert not any(k.startswith("bib_") for k in keys[0])
    assert fired == [n]                                          # the file's hooks fire once
    assert manifest_hook_calls == []                             # and the manifest hook is skipped


def test_a_single_request_file_is_stamped_in_its_write_many_and_a_refusal_there_is_survived(
    tmp_path, monkeypatch, run,
) -> None:
    """A file the batcher refused can still fit ONE writer request (the writer cap is above the
    batcher's here). Its completion rides the write_many, and the engine refusing it there is
    recorded and survived, like a refusal of a separate stamp."""
    import nexus.mcp_infra as mi

    rec = _install(monkeypatch, _RecordingCat(refuse_in_write_many=True))
    _cap(monkeypatch, 500)
    mi.reset_complete_refusals()

    n = run(tmp_path, monkeypatch)

    data = [(k, b) for k, b in rec.calls if k in ("write_many", "append")]
    assert [k for k, _ in data] == ["write_many"] and n > 1
    assert data[0][1]["sweep"] is True and data[0][1]["complete"] == {_DOC: data[0][1]["complete"][_DOC]}
    assert mi.get_complete_refusals() == [_DOC]
    assert "fail" not in rec.kinds()                             # the refusal leaves the fence as begin did


# ── duplicate chunk text: first occurrence wins ──────────────────────────────


def test_a_chash_repeated_at_two_positions_keeps_the_first_occurrences_metadata(
    cat, monkeypatch,
) -> None:
    """The old upsert (first-wins in-batch dedup) and the ChunkBatcher keep the FIRST occurrence's
    chunk metadata for identical text; the writer's own chunk index is last-wins, so the caller
    dedups. Every position still gets its own row."""
    import hashlib

    from nexus.oversize_write import write_oversize_file

    _cap(monkeypatch, 3)

    def h(t: str) -> str:
        return hashlib.sha256(t.encode()).hexdigest()

    texts = ["same text", "other text", "same text", "third", "fourth"]
    ids = [h(t) for t in texts]
    metas = [{"chunk_text_hash": ids[i], "line_start": 10 * (i + 1), "line_end": 10 * (i + 1) + 4,
              "title": f"occurrence-{i}"} for i in range(len(texts))]

    write_oversize_file(
        catalog_doc_id=_DOC, content_hash="h", collection=_CODE, ids=ids, documents=texts,
        metadatas=metas)

    sent = [c for k, b in cat.calls if k in ("write_many", "append") for c in b["chunks"]]
    same = [c for c in sent if c["chash"] == ids[0]]
    assert len(same) == 1
    assert same[0]["metadata"]["title"] == "occurrence-0" and same[0]["metadata"]["line_start"] == 10
    rows = [r for k, b in cat.calls if k in ("write_many", "append") for r in b["rows"]]
    assert [(r["position"], r["chash"]) for r in rows] == list(enumerate(ids))
    assert rows[2]["line_start"] == 30                            # its own row keeps its own span
    # The engine compares the completion count with count(*) over the manifest ROWS (one per
    # position), so a repeated chash counts at every position: 5 rows, 4 distinct chashes.
    done = [b for k, b in cat.calls if k == "complete"]
    assert len(rows) == 5 and len(set(ids)) == 4
    assert [d["chunk_count"] for d in done] == [5]


# ── the per-request grouping is the old upsert paging ─────────────────────────


@pytest.mark.parametrize("cap", [64, 16, 7])
def test_each_request_carries_the_chunks_the_old_upsert_page_did(cat, tmp_path, monkeypatch, cap) -> None:
    """For CCE collections (and onnx-local) the engine embeds one request's new chunks together
    (CCE contextual embedding is per request: ``CombinedWriteService`` hands a
    request's chunks to ``EmbedderRouter.embedForCollectionWithUsage`` in ONE call, exactly as
    ``PgVectorRepository.upsertChunksInternal`` does for one ``upsert-chunks`` page). So the
    embedding of a chunk changes only if the set of chunks sharing its request changes. The old
    fallback sent ``HttpVectorClient.upsert_chunks`` pages cut by ``_upsert_page_bounds(n, cap,
    None, None)`` (no byte budget for CCE or onnx-local); the writer must cut the same pages. (A
    code collection on the cloud code embedder also closed pages on a byte budget; the writer
    does not, and its embedding is not contextual.)
    This test drives a recording catalog and asserts page cuts only; it names no embedder
    and runs in either mode."""
    from nexus.db.http_vector_client import _upsert_page_bounds

    _cap(monkeypatch, cap)
    n = _run_prose(tmp_path, monkeypatch, sections=150)

    data = [(k, b) for k, b in cat.calls if k in ("write_many", "append")]
    sizes = [len(b["chunks"]) for _, b in data]
    assert n > cap and sum(sizes) == n
    assert sizes == [e - s for s, e in _upsert_page_bounds(n, cap, None, None)]


# ── sweep accounting ─────────────────────────────────────────────────────────


def test_the_sweep_the_engine_ran_and_the_one_it_skipped_reach_the_run_summary(tmp_path, monkeypatch) -> None:
    """The old path recorded each write's swept count and every skipped sweep (with the engine's
    reason) for the end-of-run summary; a skipped sweep must never be silent."""
    import nexus.mcp_infra as mi

    rec = _install(monkeypatch, _RecordingCat(last_append_extra={
        "swept": 7,
        "sweep_detail": [
            {"doc_id": _DOC, "errored": True, "reason": "gate_timeout"},
            {"doc_id": _DOC, "errored": False},
        ]}))
    _cap(monkeypatch, 16)
    mi.reset_superseded_sweep_stats()

    _run_prose(tmp_path, monkeypatch)

    stats = mi.get_superseded_sweep_stats()
    appends = rec.kinds().count("append")
    assert appends >= 2
    assert stats["swept"] == 7 * appends
    assert stats["skipped"] == [
        {"doc_id": _DOC, "collection": _CCE, "reason": "gate_timeout"}] * appends


# ── what the run survives ────────────────────────────────────────────────────


def test_a_refused_stamp_is_recorded_and_the_run_goes_on(tmp_path, monkeypatch) -> None:
    """The old fallback recorded a refused completion stamp in the record-level collector and
    went on (the manifest hook swallowed it); the writer raises it. The fallback must record it
    and keep the run alive, and the file's hooks still fire."""
    import nexus.mcp_infra as mi

    rec = _install(monkeypatch, _RecordingCat(refuse_stamp=True))
    _cap(monkeypatch, 16)
    mi.reset_complete_refusals()
    fired: list[str] = []
    hooks = HookRegistry()
    hooks.register_batch(lambda ids, coll, contents, emb, metas: fired.append(coll))

    n = _run_prose(tmp_path, monkeypatch, hooks=hooks)

    assert n > 16
    assert mi.get_complete_refusals() == [_DOC]
    assert fired == [_CCE]
    assert "fail" not in rec.kinds()


def test_a_write_that_fails_marks_the_fence_failed_and_propagates(cat, tmp_path, monkeypatch) -> None:
    _cap(monkeypatch, 16)
    cat._raises["append_manifest_chunks"] = BatchWriteFailedError(
        doc_id=_DOC, batch=2, reason="the engine reported it failed")
    with pytest.raises(BatchWriteFailedError):
        _run_prose(tmp_path, monkeypatch)
    assert cat.kinds().count("fail") == 1


@pytest.mark.parametrize("where,exc", [
    ("append_manifest_chunks", _status(503)),
    ("append_manifest_chunks", _status(504)),
    ("append_manifest_chunks", _status(429)),
    ("write_manifest_many", CombinedWriteEmbedTimeoutError(
        collection=_CCE, chunk_count=64, original="read timed out")),
    ("begin_index_run", httpx.ConnectError("refused", request=_REQ)),
    ("complete_index_run", httpx.ReadTimeout("slow", request=_REQ)),
], ids=["append-503", "append-504", "append-429", "embed-timeout", "begin-connect", "complete-timeout"])
def test_a_transient_write_error_defers_the_file_and_marks_the_fence_failed(
    tmp_path, monkeypatch, where, exc,
) -> None:
    """``run_file_loop`` cancels the whole run on the first exception it does not know. The old
    fallback's transient upsert errors deferred the file; the write's transient outcomes
    (a gateway or rate-limit status, the embed timeout, a connectivity error the writer's own
    bounded retry could not outlast, on ANY of its requests) do too, and the run summary names
    the file."""
    from nexus.indexer import (
        _contain_transient_upsert,
        reset_transient_upsert_deferred_count,
        transient_upsert_deferred_count,
        transient_upsert_deferred_paths,
    )

    rec = _install(monkeypatch, _RecordingCat(raises={where: exc}))
    _cap(monkeypatch, 16)
    reset_transient_upsert_deferred_count()

    out = _contain_transient_upsert(lambda: _run_prose(tmp_path, monkeypatch), Path("big.md"))

    assert out == 0
    assert transient_upsert_deferred_count() == 1
    assert transient_upsert_deferred_paths() == ["big.md"]
    if where != "begin_index_run":                    # nothing was begun, so there is nothing to fail
        assert rec.kinds().count("fail") == 1


def test_the_deferral_marker_carries_the_cause(cat, tmp_path, monkeypatch) -> None:
    _cap(monkeypatch, 16)
    cat._raises["append_manifest_chunks"] = _status(503)
    with pytest.raises(OversizeWriteDeferred) as ei:
        _run_prose(tmp_path, monkeypatch)
    assert isinstance(ei.value.__cause__, httpx.HTTPStatusError)
    assert ei.value.doc_id == _DOC and ei.value.collection == _CCE


@pytest.mark.parametrize("exc", [
    _status(503),
    _status(400),
    BatchWriteFailedError(doc_id=_DOC, batch=1, reason="failed"),
], ids=["503-outside-the-write", "400", "batch-write-failed"])
def test_an_error_that_is_not_a_transient_write_outcome_still_propagates(exc) -> None:
    """Only the write's own transient outcomes defer. An ``httpx.HTTPStatusError`` from the doc-id
    resolver or a hook is not one, and a permanent write failure stays loud."""
    from nexus.indexer import _contain_transient_upsert

    def fn() -> int:
        raise exc

    with pytest.raises(type(exc)):
        _contain_transient_upsert(fn, Path("big.md"))


def test_a_400_from_the_write_itself_propagates_unwrapped(cat, tmp_path, monkeypatch) -> None:
    _cap(monkeypatch, 16)
    cat._raises["append_manifest_chunks"] = _status(400)
    with pytest.raises(httpx.HTTPStatusError):
        _run_prose(tmp_path, monkeypatch)
    assert cat.kinds().count("fail") == 1


# ── which path a fallback takes ──────────────────────────────────────────────


def test_an_identity_less_oversize_file_writes_zero_chunks_and_is_counted(
    cat, tmp_path, monkeypatch, run,
) -> None:
    """RDR-223 (nexus-z0o2p.20): a file with no catalog document has no owner row to write a
    chunk with, so the fallback writes nothing on a service-backed T3 and records the file in the
    identity-drop collector the flush route uses (``written=False``: the run summary names it and
    the run fails). It used to write the chunks with the ownerless upsert."""
    from nexus.mcp_infra import get_manifest_identity_drops, reset_manifest_identity_drops

    reset_manifest_identity_drops()
    _cap(monkeypatch, 16)
    db = _http_db()
    n = run(tmp_path, monkeypatch, doc_id="", db=db)
    assert n == 0
    assert db.mock_calls == [], db.mock_calls       # no upsert, no other T3 call
    assert cat.calls == []                          # and no catalog write either
    drops = get_manifest_identity_drops()
    assert len(drops) == 1 and drops[0]["written"] is False
    assert drops[0]["batch_size"] > 16              # an oversize file: more chunks than one batch
    [f] = drops[0]["files"]
    assert f["cause"] == "oversize_no_catalog_document" and f["chunks"] == drops[0]["batch_size"]


class _CountingHooks(HookRegistry):
    """A registry with one real hook on each chain, each counting its calls, so a fallback
    that fires ANY chain (through ``fire_*`` or behind a ``has_*_hooks`` guard) is seen."""

    def __init__(self) -> None:
        super().__init__()
        self.calls: list[str] = []
        self.register_single(lambda doc_id, collection, content: self.calls.append("single"))
        self.register_batch(lambda *a, **kw: self.calls.append("batch"))
        self.register_document(lambda *a, **kw: self.calls.append("document"))


def test_the_spy_hooks_fire_when_a_file_is_written(cat, tmp_path, monkeypatch, run) -> None:
    """Positive control for the test below: on the old upsert path the same counting registry
    does see the hooks fire, so an empty ``calls`` there means none fired, not that the spy
    cannot see them."""
    _cap(monkeypatch, 16)
    hooks = _CountingHooks()
    run(tmp_path, monkeypatch, doc_id="", db=MagicMock(), hooks=hooks)  # not an HttpVectorClient
    assert hooks.calls, "the spy saw no hook fire on a file that was written"


def test_an_identity_less_oversize_file_fires_no_hook(cat, tmp_path, monkeypatch, run) -> None:
    """RDR-223 (nexus-z0o2p.20): a refused file wrote nothing, so no post-store hook (taxonomy,
    aspects, manifest) runs for it. Pinned with a spy on every chain, for code, prose and pdf:
    a variant that refuses the write but still falls through to the hooks fails here."""
    _cap(monkeypatch, 16)
    hooks = _CountingHooks()
    db = _http_db()
    assert run(tmp_path, monkeypatch, doc_id="", db=db, hooks=hooks) == 0
    assert hooks.calls == [], hooks.calls
    assert db.mock_calls == [] and cat.calls == []


def test_an_identity_less_file_on_a_non_service_t3_keeps_its_write(
    cat, tmp_path, monkeypatch, run,
) -> None:
    """The in-memory test topology has no owner concept; it keeps the old write and records no
    drop."""
    from nexus.mcp_infra import get_manifest_identity_drops, reset_manifest_identity_drops

    reset_manifest_identity_drops()
    _cap(monkeypatch, 16)
    db = MagicMock()                                # not an HttpVectorClient
    run(tmp_path, monkeypatch, doc_id="", db=db, hooks=HookRegistry())
    assert db.upsert_chunks_with_embeddings.call_count == 1
    assert get_manifest_identity_drops() == []


def test_a_non_service_t3_keeps_the_old_write(cat, tmp_path, monkeypatch, run) -> None:
    """The in-memory test topology holds a non-service T3 the engine's combined write cannot
    reach. The path is chosen by what the T3 is, not by whether a batcher happens to exist."""
    _cap(monkeypatch, 16)
    db = MagicMock()                           # not an HttpVectorClient
    run(tmp_path, monkeypatch, db=db, hooks=HookRegistry())
    assert db.upsert_chunks_with_embeddings.call_count == 1
    assert [k for k in cat.kinds() if k in ("write_many", "append")] == []


def test_a_service_backed_t3_with_no_batcher_is_a_wiring_bug_and_fails_loud(
    cat, tmp_path, monkeypatch, run,
) -> None:
    """``_run_index`` builds the ChunkBatcher for every HttpVectorClient. A service-backed T3 with
    none must not silently take the old upsert (a P3.2 outage) or the writer; it raises."""
    _cap(monkeypatch, 16)
    db = _http_db()
    with pytest.raises(RuntimeError, match="no ChunkBatcher"):
        run(tmp_path, monkeypatch, batcher=None, db=db)
    assert db.mock_calls == [] and cat.calls == []


def test_an_identity_less_file_with_no_batcher_never_needs_the_writer(
    cat, tmp_path, monkeypatch, run,
) -> None:
    """A file with no catalog document never reaches the writer, so a service-backed T3 that
    carries no ChunkBatcher is no wiring bug for it: it is refused before the batcher is asked
    about (``use_writer`` answers False on identity too)."""
    from nexus.oversize_write import use_writer

    assert use_writer(_http_db(), None, "") is False
    _cap(monkeypatch, 16)
    db = _http_db()
    assert run(tmp_path, monkeypatch, doc_id="", batcher=None, db=db) == 0
    assert db.mock_calls == [] and cat.calls == []


# ── which exception ended the write ──────────────────────────────────────────


def _raised_inside_a_handler(first: BaseException, second: BaseException) -> BaseException:
    """``second`` raised while ``first`` is being handled, as ``RefreshableHttpStoreMixin._request``
    does: its re-resolve retry runs inside the ``except`` block of the first attempt, so the retry's
    exception carries the first attempt's as ``__context__`` (implicit chaining, no ``from``)."""
    try:
        try:
            raise first
        except type(first):
            raise second
    except BaseException as caught:  # noqa: BLE001 — capture the chained exception for the test
        return caught


def _transient(exc: BaseException) -> bool:
    from nexus.oversize_write import _is_transient_write_error

    return _is_transient_write_error(exc)


def test_a_connect_failure_that_the_clients_retry_could_not_outlast_is_transient() -> None:
    """The client's retry raises the SECOND attempt's transport error with the first as its
    ``__context__``; the top-level exception is the connectivity failure that ended the write."""
    exc = _raised_inside_a_handler(
        httpx.ConnectError("first", request=_REQ), httpx.ConnectError("second", request=_REQ))
    assert isinstance(exc.__context__, httpx.ConnectError)
    assert _transient(exc) is True


def test_a_transport_drop_reframed_with_from_is_transient() -> None:
    """A client that reframes a transport drop as an application error says so with ``raise ... from``."""
    try:
        try:
            raise httpx.ReadTimeout("slow", request=_REQ)
        except httpx.ReadTimeout as drop:
            raise RuntimeError("the write failed") from drop
    except RuntimeError as reframed:
        assert _transient(reframed) is True


def test_a_permanent_400_raised_while_handling_a_transport_error_is_not_transient() -> None:
    """The retry attempt after a transport error can itself end on a real 400. Its ``__context__``
    is the first attempt's ConnectError, which did not end the write: the 400 did."""
    exc = _raised_inside_a_handler(httpx.ConnectError("first", request=_REQ), _status(400))
    assert isinstance(exc.__context__, httpx.ConnectError)
    assert _transient(exc) is False


def test_an_application_error_raised_in_a_transport_handler_without_from_is_not_transient() -> None:
    exc = _raised_inside_a_handler(
        httpx.ConnectError("first", request=_REQ), ValueError("the response was not JSON"))
    assert _transient(exc) is False


def test_an_endpoint_the_client_could_not_re_resolve_aborts_the_run() -> None:
    """The client's re-resolve gives up with ``ServiceEndpointUnresolvableError`` raised inside
    the ConnectError handler that triggered it; the file is not deferred."""
    from nexus.db.service_endpoint import ServiceEndpointUnresolvableError  # noqa: PLC0415

    exc = _raised_inside_a_handler(
        httpx.ConnectError("first", request=_REQ),
        ServiceEndpointUnresolvableError("lease not republished"))
    assert _transient(exc) is False


def test_a_permanent_400_after_a_transport_error_propagates_from_the_writer(
    tmp_path, monkeypatch,
) -> None:
    """End to end through ``write_oversize_file``: the file is not deferred, the 400 surfaces."""
    bad = _raised_inside_a_handler(httpx.ConnectError("first", request=_REQ), _status(400))
    rec = _install(monkeypatch, _RecordingCat(raises={"append_manifest_chunks": bad}))
    _cap(monkeypatch, 16)
    with pytest.raises(httpx.HTTPStatusError) as ei:
        _run_prose(tmp_path, monkeypatch)
    assert not isinstance(ei.value, OversizeWriteDeferred)
    assert ei.value.response.status_code == 400
    assert rec.kinds().count("fail") == 1
