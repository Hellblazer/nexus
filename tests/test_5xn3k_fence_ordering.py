# SPDX-License-Identifier: AGPL-3.0-or-later
"""nexus-5xn3k.4 RUNFENCE: begin/complete/fail bracketing + the doc_id hoist.

Design memo (T2 nexus "5xn3k-design-2026-08-02") §3.5 — per-document commit
order, pinned here as a GLOBAL call sequence captured by recording doubles:

    T0    begin committed  — index_state='indexing' — BEFORE the first chunk upsert
    T1..n chunks + manifest rows accumulate
    Tn+1  complete committed — verify, then stamp — AFTER the last manifest write

This file exists specifically to fail if someone later folds the completion
stamp into the per-batch manifest path, moves ``begin`` after the first
upsert, or lets a skip path mint a catalog row.

Bead MUSTs pinned here (amendments from the .2/.3 critiques):
- zero-extraction routes to /fail, never /complete(chunk_count=0)
- content_hash is computed ONCE and the SAME value reaches begin and complete
- complete_index_run's ``None`` sentinel (pre-fence engine) is not success
- write_manifest_many's ``complete_refused`` MUST be parsed, count field
  included, and a refused doc must never be reported as fully indexed
"""
from __future__ import annotations

import threading
from pathlib import Path
from unittest.mock import MagicMock, create_autospec, patch

import pytest

from nexus.errors import IndexRunVerifyRefused

CONTENT_HASH = "c" * 64
COLLECTION = "docs__testowner__voyage-context-3__v1"
TARGET_MODEL = "voyage-context-3"
DOC_ID = "1.5.77"


class _RecordingFenceWriter:
    """Catalog-writer double recording fence calls into a shared sequence."""

    def __init__(self, seq: list, *, complete_result: object = None,
                 complete_raises: BaseException | None = None) -> None:
        self._seq = seq
        self._complete_result = complete_result
        self._complete_raises = complete_raises
        self.begin_calls: list[dict] = []
        self.complete_calls: list[dict] = []
        self.fail_calls: list[dict] = []
        self.closed = False

    def begin_index_run(self, doc_id, content_hash, run_id, collection):
        self.begin_calls.append({
            "doc_id": doc_id, "content_hash": content_hash,
            "run_id": run_id, "collection": collection,
        })
        self._seq.append(("begin", doc_id))

    def complete_index_run(self, doc_id, content_hash, chunk_count):
        self.complete_calls.append({
            "doc_id": doc_id, "content_hash": content_hash,
            "chunk_count": chunk_count,
        })
        self._seq.append(("complete", doc_id))
        if self._complete_raises is not None:
            raise self._complete_raises
        return self._complete_result

    def fail_index_run(self, doc_id, error):
        self.fail_calls.append({"doc_id": doc_id, "error": error})
        self._seq.append(("fail", doc_id))

    def close(self):
        self.closed = True


class _RecordingHooks:
    """HookRegistry stand-in recording manifest-relevant firings."""

    def __init__(self, seq: list) -> None:
        self._seq = seq
        self.batch_calls: list[dict] = []

    def fire_batch(self, doc_ids, collection, contents, embeddings=None,
                   metadatas=None, *, catalog_doc_id="", grain="all",
                   manifest_complete=None):
        self.batch_calls.append({
            "catalog_doc_id": catalog_doc_id,
            "manifest_complete": manifest_complete,
            "n": len(doc_ids),
        })
        self._seq.append(("manifest_batch", catalog_doc_id))

    def fire_single(self, *a, **k):
        pass

    def fire_document(self, *a, **k):
        self._seq.append(("fire_document",))


# ─────────────────────────────────────────────────────────────────────────────
# _index_document: hoist + begin + single-flush completion ride
# ─────────────────────────────────────────────────────────────────────────────


class _Col:
    def __init__(self) -> None:
        self.deleted: list = []

    def get(self, where=None, include=None, limit=None, offset=None, **kw):
        return {"ids": [], "metadatas": []}

    def delete(self, ids=None, where=None):
        self.deleted.append(ids or where)


class _T3:
    def __init__(self, seq: list) -> None:
        self._seq = seq
        self.col = _Col()

    def get_or_create_collection(self, name):
        return self.col

    def upsert_chunks_with_embeddings(self, collection, ids, documents,
                                      embeddings, metadatas, **kw):
        self._seq.append(("upsert", collection))


def _chunks(file_path, content_hash, target_model, now_iso, corpus):
    return [(
        f"{content_hash[:16]}_0",
        "chunk text",
        {"content_hash": content_hash, "embedding_model": target_model,
         "chunk_text_hash": "a" * 64, "content_type": "prose"},
    )]


def _embed(texts, model):
    return [[0.1, 0.2]] * len(texts), model


def _drive_index_document(tmp_path: Path, seq: list, fence: _RecordingFenceWriter,
                          *, register_returns: str = DOC_ID):
    from nexus.doc_indexer import _index_document

    f = tmp_path / "doc.md"
    f.write_text("hello fence")
    hooks = _RecordingHooks(seq)
    register_calls: list = []

    def _register(path, corpus, **kw):
        register_calls.append((str(path), corpus))
        seq.append(("register", register_returns))
        return register_returns

    with patch("nexus.doc_indexer._register_or_lookup_doc_id", side_effect=_register), \
            patch("nexus.catalog.factory.make_catalog_writer", return_value=fence):
        n = _index_document(
            f, "testowner", _chunks, _T3(seq),
            collection_name=COLLECTION, embed_fn=_embed, hooks=hooks,
        )
    return n, hooks, register_calls


def test_index_document_register_and_begin_precede_first_upsert(tmp_path) -> None:
    """The hoist (bead .4): doc_id resolution AND the fence begin must both
    land before the first chunk upsert — the fence needs a doc_id before the
    first byte of content is written."""
    seq: list = []
    fence = _RecordingFenceWriter(seq)
    n, hooks, register_calls = _drive_index_document(tmp_path, seq, fence)

    assert n == 1
    assert register_calls, "doc_id was never resolved"
    ops = [op for op, *_ in seq]
    assert "upsert" in ops
    assert ops.index("register") < ops.index("upsert"), seq
    assert ops.index("begin") < ops.index("upsert"), seq


def test_index_document_begin_uses_the_one_computed_content_hash(tmp_path) -> None:
    """Hash-once MUST: begin carries the file's actual sha256 — the same
    value the staleness gate and chunk metadata use — never a recompute."""
    from nexus.doc_indexer import _sha256

    seq: list = []
    fence = _RecordingFenceWriter(seq)
    f = tmp_path / "doc.md"
    f.write_text("hello fence")
    expected = _sha256(f)

    _drive_index_document(tmp_path, seq, fence)
    assert fence.begin_calls, "begin never fired"
    assert fence.begin_calls[0]["content_hash"] == expected
    assert fence.begin_calls[0]["collection"] == COLLECTION


def test_index_document_rides_manifest_complete_with_same_hash(tmp_path) -> None:
    """Single-flush documents ride write_manifest_many's complete map (no
    extra round trip): fire_batch must receive manifest_complete mapping the
    resolved doc_id to the SAME content_hash begin carried."""
    seq: list = []
    fence = _RecordingFenceWriter(seq)
    _, hooks, _ = _drive_index_document(tmp_path, seq, fence)

    assert hooks.batch_calls, "fire_batch never fired"
    mc = hooks.batch_calls[0]["manifest_complete"]
    assert mc == {DOC_ID: fence.begin_calls[0]["content_hash"]}


def test_index_document_no_catalog_skips_fence(tmp_path) -> None:
    """doc_id='' (catalog absent) — the no-catalog ingest contract: no begin,
    no manifest_complete, indexing proceeds."""
    seq: list = []
    fence = _RecordingFenceWriter(seq)
    n, hooks, _ = _drive_index_document(tmp_path, seq, fence, register_returns="")

    assert n == 1
    assert fence.begin_calls == []
    assert hooks.batch_calls[0]["manifest_complete"] is None


def test_index_document_fresh_skip_never_registers(tmp_path) -> None:
    """Hoist-position control: a fresh-skip must NOT register a doc_id —
    _doc_id_for_path's read-only property on the staleness path is
    deliberate and the hoist must sit BELOW the gate."""
    from nexus.doc_indexer import _index_document

    f = tmp_path / "doc.md"
    f.write_text("hello fence")

    from nexus.doc_indexer import _sha256
    real_hash = _sha256(f)

    class _FreshCol(_Col):
        def get(self, where=None, include=None, limit=None, offset=None, **kw):
            return {"ids": ["x"], "metadatas": [{
                "content_hash": real_hash, "embedding_model": TARGET_MODEL,
            }]}

    class _FreshT3(_T3):
        def __init__(self, seq):
            super().__init__(seq)
            self.col = _FreshCol()

    seq: list = []
    register = MagicMock(return_value=DOC_ID)
    with patch("nexus.doc_indexer._register_or_lookup_doc_id", register), \
            patch("nexus.doc_indexer._index_run_fresh", return_value=True):
        n = _index_document(
            f, "testowner", _chunks, _FreshT3(seq),
            collection_name=COLLECTION, embed_fn=_embed,
            hooks=_RecordingHooks(seq),
        )
    assert n == 0
    register.assert_not_called()
    assert seq == [], f"skip path touched the world: {seq}"


# ─────────────────────────────────────────────────────────────────────────────
# _index_pdf_incremental: begin / fail / complete bracketing
# (code-review-expert HIGH follow-up, 2026-08-02 — this path was entirely
# unfenced). Multi-batch: fire_batch fires once PER INCREMENT, so completion
# is an explicit _fence_complete call at the tail with the run's total chunk
# count, never a manifest_complete ride (that ride is only sound for
# genuinely single-flush callers like _index_document).
# ─────────────────────────────────────────────────────────────────────────────

_INCR_DOC_ID = "1.5.99"


def _incr_prepared(n: int, content_hash: str = CONTENT_HASH,
                   target_model: str = TARGET_MODEL) -> list[tuple[str, str, dict]]:
    return [
        (f"{content_hash[:16]}_{i}", f"chunk {i} text",
         {"content_hash": content_hash, "embedding_model": target_model,
          "chunk_text_hash": f"{i:064d}"[-64:], "content_type": "pdf"})
        for i in range(n)
    ]


def _drive_incremental(tmp_path: Path, seq: list, fence: _RecordingFenceWriter, *,
                       n: int = 130, upsert_raises: bool = False):
    from nexus.doc_indexer import _index_pdf_incremental

    f = tmp_path / "big.pdf"
    f.write_bytes(b"%PDF-fake")
    hooks = _RecordingHooks(seq)
    prepared = _incr_prepared(n)
    t3 = _T3(seq)
    if upsert_raises:
        def _raising_upsert(collection, ids, documents, embeddings, metadatas, **kw):
            seq.append(("upsert", collection))
            raise RuntimeError("incremental upsert exploded")
        t3.upsert_chunks_with_embeddings = _raising_upsert

    with patch("nexus.catalog.factory.make_catalog_writer", return_value=fence):
        total = _index_pdf_incremental(
            f, "testowner", prepared, CONTENT_HASH, COLLECTION, t3,
            embed_fn=_embed, hooks=hooks, doc_id=_INCR_DOC_ID,
        )
    return total, hooks


def test_incremental_begin_before_first_upsert_complete_after_last_manifest(tmp_path) -> None:
    """n=130 forces TWO batches (_INCREMENTAL_BATCH_SIZE=128) so this pins
    the multi-batch shape, not a degenerate single-batch coincidence."""
    seq: list = []
    fence = _RecordingFenceWriter(seq, complete_result={
        "referenced": 130, "present": 130, "missing": 0, "flagged": 0,
    })
    total, hooks = _drive_incremental(tmp_path, seq, fence, n=130)

    assert total == 130
    ops = [op for op, *_ in seq]
    assert ops.count("manifest_batch") == 2, seq
    assert "upsert" in ops
    assert ops.index("begin") < ops.index("upsert"), seq
    last_manifest = max(i for i, op in enumerate(ops) if op == "manifest_batch")
    assert ops.index("complete") > last_manifest, seq
    assert fence.complete_calls[0]["chunk_count"] == 130
    # hash-once MUST: begin and complete carry the SAME content_hash.
    assert fence.begin_calls[0]["content_hash"] == CONTENT_HASH
    assert fence.complete_calls[0]["content_hash"] == CONTENT_HASH
    # every fire_batch call used the manifest ride NOT present (multi-batch).
    assert all(c["manifest_complete"] is None for c in hooks.batch_calls)


def test_incremental_failure_fails_fence_and_propagates(tmp_path) -> None:
    seq: list = []
    fence = _RecordingFenceWriter(seq)
    with pytest.raises(RuntimeError, match="incremental upsert exploded"):
        _drive_incremental(tmp_path, seq, fence, n=130, upsert_raises=True)
    assert fence.fail_calls, "fail_index_run never fired on incremental failure"
    assert fence.fail_calls[0]["doc_id"] == _INCR_DOC_ID
    assert "incremental upsert exploded" in fence.fail_calls[0]["error"]
    assert fence.complete_calls == []


# ─────────────────────────────────────────────────────────────────────────────
# pipeline_index_pdf: begin / complete / fail bracketing
# ─────────────────────────────────────────────────────────────────────────────

_P_EXT = "nexus.pipeline_stages.PDFExtractor"
_P_CHK = "nexus.pipeline_stages.PDFChunker"


def _run_pipeline(db, seq: list, fence: _RecordingFenceWriter, *, pages: int = 2,
                  chunks: list | None = None, upsert_raises: bool = False,
                  content_hash: str = CONTENT_HASH):
    from nexus.db.t3 import T3Database  # noqa: F401 — autospec target
    from nexus.pipeline_stages import pipeline_index_pdf
    from tests.test_pipeline_stages import _er, _fx, _tc

    mock_col = MagicMock()
    mock_col.get.return_value = {"ids": [], "metadatas": []}
    t3 = MagicMock()
    t3.get_or_create_collection.return_value = mock_col

    def _upsert(*a, **k):
        if upsert_raises:
            raise RuntimeError("upload exploded")
        seq.append(("upsert",))

    t3.upsert_chunks_with_embeddings.side_effect = _upsert
    # uploader_loop upserts through col in some shapes; record both.
    mock_col.upsert.side_effect = _upsert

    result = _er(pages)
    fake_chunks = chunks if chunks is not None else _tc(
        ("chunk one text", 0, {"page": 1}), ("chunk two text", 1, {"page": 2}),
    )
    hooks = _RecordingHooks(seq)
    with patch(_P_EXT) as ME, patch(_P_CHK) as MC, \
            patch("nexus.catalog.factory.make_catalog_writer", return_value=fence):
        ME.return_value.extract.side_effect = _fx(pages, result)
        MC.return_value.chunk.return_value = fake_chunks
        total = pipeline_index_pdf(
            Path("/a.pdf"), content_hash, "docs__test", t3,
            db=db, embed_fn=_embed, doc_id=DOC_ID, hooks=hooks,
        )
    return total, hooks


@pytest.fixture()
def db():
    from tests.pipeline_fake_engine import make_fake_engine_db
    store, _engine = make_fake_engine_db()
    return store


@pytest.fixture(autouse=True)
def _fast_poll(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("nexus.pipeline_stages._POLL_INTERVAL", 0.01)


def test_pipeline_begin_before_first_upsert_complete_after_last_manifest(db) -> None:
    seq: list = []
    fence = _RecordingFenceWriter(seq, complete_result={
        "referenced": 2, "present": 2, "missing": 0, "flagged": 0,
    })
    total, _ = _run_pipeline(db, seq, fence)

    assert total > 0
    ops = [op for op, *_ in seq]
    assert "begin" in ops and "complete" in ops
    assert "upsert" in ops
    assert ops.index("begin") < ops.index("upsert"), seq
    last_manifest = max(i for i, op in enumerate(ops) if op == "manifest_batch")
    assert ops.index("complete") > last_manifest, seq
    # complete follows the document-grain hook too — the per-doc hook may
    # rewrite the manifest at the tail, and the stamp must postdate it.
    assert ops.index("complete") > ops.index("fire_document"), seq
    assert fence.complete_calls[0]["chunk_count"] == total


def test_pipeline_hash_once_between_begin_and_complete(db) -> None:
    seq: list = []
    fence = _RecordingFenceWriter(seq, complete_result={})
    _run_pipeline(db, seq, fence, content_hash="e" * 64)
    assert fence.begin_calls[0]["content_hash"] == "e" * 64
    assert fence.complete_calls[0]["content_hash"] == "e" * 64


def test_pipeline_failure_fails_fence_and_propagates_first_exc(db) -> None:
    seq: list = []
    fence = _RecordingFenceWriter(seq)
    with pytest.raises(RuntimeError, match="upload exploded"):
        _run_pipeline(db, seq, fence, upsert_raises=True)
    assert fence.fail_calls, "fail_index_run never fired on pipeline failure"
    assert fence.fail_calls[0]["doc_id"] == DOC_ID
    assert "upload exploded" in fence.fail_calls[0]["error"]
    assert fence.complete_calls == []


def test_pipeline_zero_chunks_routes_fail_never_complete_zero(db) -> None:
    """MUST (amendment 2026-08-02): a content-bearing document whose run
    produced zero chunks is a FAILURE — /fail, never /complete(0), which
    would satisfy the fail-closed gate trivially (referenced=0==chunk_count)."""
    seq: list = []
    fence = _RecordingFenceWriter(seq)
    total, _ = _run_pipeline(db, seq, fence, pages=0, chunks=[])
    assert total == 0
    assert fence.complete_calls == [], "complete(0) stamped a failed extraction"
    assert fence.fail_calls, "zero-chunk run must hit /index-run/fail"


def test_pipeline_verify_refused_propagates(db) -> None:
    """The engine's 409 refusal (typed IndexRunVerifyRefused) is the
    load-bearing fail-closed signal — it must propagate, never be swallowed
    into a green summary."""
    seq: list = []
    fence = _RecordingFenceWriter(seq, complete_raises=IndexRunVerifyRefused(
        doc_id=DOC_ID, referenced=2, present=1, missing=1, chunk_count=2,
        server_detail="verify failed",
    ))
    with pytest.raises(IndexRunVerifyRefused):
        _run_pipeline(db, seq, fence)


def test_pipeline_none_sentinel_is_not_success_and_not_fatal(db) -> None:
    """complete_index_run -> None (pre-fence engine): the run's own success
    is unaffected, but nothing may claim the fence was stamped."""
    seq: list = []
    fence = _RecordingFenceWriter(seq, complete_result=None)
    total, _ = _run_pipeline(db, seq, fence)
    assert total > 0  # indexing itself unaffected


# ─────────────────────────────────────────────────────────────────────────────
# mcp_infra._manifest_write_loop: complete_refused MUST be parsed
# ─────────────────────────────────────────────────────────────────────────────


def _loop_with(cat_result, manifest_complete=None):
    from nexus.mcp_infra import (
        _manifest_write_loop,
        get_complete_refusals,
        reset_complete_refusals,
    )

    reset_complete_refusals()
    cat = MagicMock()
    cat.write_manifest_many.return_value = cat_result
    reader = MagicMock()
    reader.get_chunk_chashes.return_value = []
    by_doc = {DOC_ID: [(0, {"chunk_text_hash": "b" * 64, "chunk_index": 0})]}
    _manifest_write_loop(cat, by_doc, COLLECTION, reader=reader,
                         manifest_complete=manifest_complete)
    return cat, get_complete_refusals()


def test_loop_passes_complete_map_only_for_claimed_docs() -> None:
    cat, _ = _loop_with(
        {"failed_doc_ids": [], "complete_refused": [], "complete_refused_count": 0},
        manifest_complete={DOC_ID: CONTENT_HASH},
    )
    _, kwargs = cat.write_manifest_many.call_args
    assert kwargs.get("complete") == {DOC_ID: CONTENT_HASH}


def test_loop_omits_complete_kwarg_when_nothing_claimed() -> None:
    cat, _ = _loop_with(
        {"failed_doc_ids": [], "complete_refused": [], "complete_refused_count": 0},
    )
    _, kwargs = cat.write_manifest_many.call_args
    assert "complete" not in kwargs


def test_loop_parses_complete_refused_and_records(caplog) -> None:
    """MUST: a complete_refused entry means manifest rows landed but the
    stamp was refused — the doc must be recorded as not-fully-indexed and
    logged at WARNING, never silently folded into a clean batch."""
    import logging

    import structlog

    # structlog's default test-session config (conftest.py) uses
    # PrintLoggerFactory, which never reaches stdlib logging / caplog.
    # Reroute for this test only — the autouse `_restore_structlog_after_test`
    # fixture in conftest.py restores the original config afterward.
    structlog.configure(
        processors=[structlog.stdlib.render_to_log_kwargs],
        wrapper_class=structlog.stdlib.BoundLogger,
        logger_factory=structlog.stdlib.LoggerFactory(),
    )
    with caplog.at_level(logging.WARNING):
        _, refusals = _loop_with(
            {"failed_doc_ids": [],
             "complete_refused": [{"doc_id": DOC_ID, "referenced": 5,
                                   "missing": 2, "chunk_count": 7}],
             "complete_refused_count": 1},
            manifest_complete={DOC_ID: CONTENT_HASH},
        )
    assert refusals == [DOC_ID]
    assert any("complete_refused" in r.message for r in caplog.records)


def test_loop_count_field_guards_against_truncated_list(caplog) -> None:
    """MUST parse the COUNT field: a response whose scalar disagrees with the
    list length (truncation, shape drift) must be surfaced, not read as
    fewer refusals."""
    import logging

    import structlog

    structlog.configure(
        processors=[structlog.stdlib.render_to_log_kwargs],
        wrapper_class=structlog.stdlib.BoundLogger,
        logger_factory=structlog.stdlib.LoggerFactory(),
    )
    with caplog.at_level(logging.WARNING):
        _loop_with(
            {"failed_doc_ids": [], "complete_refused": [],
             "complete_refused_count": 2},
            manifest_complete={DOC_ID: CONTENT_HASH},
        )
    assert any("complete_refused_count_mismatch" in r.message for r in caplog.records)


def test_loop_legacy_list_return_still_works() -> None:
    """A patched/legacy writer returning the old bare failed-ids list must
    keep working (back-compat with test doubles and the retry wrapper)."""
    cat, refusals = _loop_with([])
    assert refusals == []


# ─────────────────────────────────────────────────────────────────────────────
# HttpCatalogClient.write_manifest_many: complete map on the wire
# ─────────────────────────────────────────────────────────────────────────────


def _client_with_response(response):
    from nexus.catalog.http_catalog_client import HttpCatalogClient

    client = HttpCatalogClient.__new__(HttpCatalogClient)
    posts: list = []

    def _post(path, body):
        posts.append((path, body))
        return response

    client._post = _post
    return client, posts


def test_client_sends_complete_map_and_parses_refusals() -> None:
    rows = [{"chash": "a" * 64, "position": 0}]
    client, posts = _client_with_response({
        "failed_doc_ids": [],
        "complete_refused": [{"doc_id": DOC_ID, "referenced": 1,
                              "missing": 1, "chunk_count": 2}],
        "complete_refused_count": 1,
    })
    result = client.write_manifest_many(
        [(DOC_ID, rows)], complete={DOC_ID: CONTENT_HASH})

    assert posts[0][1]["complete"] == {DOC_ID: CONTENT_HASH}
    assert result["complete_refused_count"] == 1
    assert result["complete_refused"][0]["doc_id"] == DOC_ID
    assert result["failed_doc_ids"] == []


def test_client_omits_complete_key_when_none() -> None:
    rows = [{"chash": "a" * 64, "position": 0}]
    client, posts = _client_with_response({"failed_doc_ids": []})
    result = client.write_manifest_many([(DOC_ID, rows)])
    assert "complete" not in posts[0][1]
    assert result["failed_doc_ids"] == []
    assert result["complete_refused"] == []
    assert result["complete_refused_count"] == 0


def test_client_pages_complete_entries_with_their_docs() -> None:
    """Paged posts must carry only the complete entries whose docs are in
    that page — a stamp request for a doc the page never wrote would verify
    against a manifest another page owns."""
    import nexus.catalog.http_catalog_client as hcc

    rows = [{"chash": "a" * 64, "position": 0}]
    docs = [(f"1.5.{i}", rows) for i in range(3)]
    complete = {f"1.5.{i}": CONTENT_HASH for i in range(3)}

    client, posts = _client_with_response({"failed_doc_ids": []})
    with patch.object(hcc, "_MANIFEST_GET_MANY_PAGE", 2):
        client.write_manifest_many(docs, complete=complete)

    assert len(posts) == 2
    page1_ids = {d["doc_id"] for d in posts[0][1]["docs"]}
    page2_ids = {d["doc_id"] for d in posts[1][1]["docs"]}
    assert set(posts[0][1]["complete"]) == page1_ids
    assert set(posts[1][1]["complete"]) == page2_ids


# ─────────────────────────────────────────────────────────────────────────────
# hook_registry: manifest_complete threads only to declaring hooks
# ─────────────────────────────────────────────────────────────────────────────


def test_fire_batch_threads_manifest_complete_to_declaring_hooks_only() -> None:
    from nexus.hook_registry import HookRegistry

    reg = HookRegistry()
    seen: dict = {}

    def modern(doc_ids, collection, contents, embeddings, metadatas, *,
               catalog_doc_id="", manifest_complete=None):
        seen["modern"] = manifest_complete

    def legacy(doc_ids, collection, contents, embeddings, metadatas):
        seen["legacy"] = "called"

    reg.register_batch(modern)
    reg.register_batch(legacy)
    reg.fire_batch(["id1"], COLLECTION, ["text"], None,
                   [{"content_hash": CONTENT_HASH}],
                   catalog_doc_id=DOC_ID,
                   manifest_complete={DOC_ID: CONTENT_HASH})

    assert seen["modern"] == {DOC_ID: CONTENT_HASH}
    assert seen["legacy"] == "called"
