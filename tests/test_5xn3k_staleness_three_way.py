# SPDX-License-Identifier: AGPL-3.0-or-later
"""nexus-5xn3k.3 RUNFENCE: the staleness three-way, driven at BOTH gates.

Design memo (T2 nexus "5xn3k-design-2026-08-02") §3.1 and §3.4, as amended by
T2 nexus/5xn3k-design-amendment-2026-10-26 (Sam's ruling, applied in nexus-3ajpd):

    fresh  <=>  entry.index_state == 'complete'
           AND  entry.index_content_hash == content_hash
           AND  one chunk of the document's own manifest is present in T3
                with the target embedding model

    * 'indexing' / 'failed'      -> stale, definitively. No T3 probe.
    * NULL / absent field        -> stale. The fence has nothing to say and
                                     no other document-level record carries
                                     the hash a completed run verified.
    * fence unreadable            -> stale, logged at WARNING.

Before nexus-3ajpd the gate began with ``col.get(where={"content_hash": h},
limit=1)`` and a NULL fence fell through to a verify that was a bare
``return True`` since RDR-191 Phase 6, so a document with no fence
skipped on any chunk row stamped with its hash. A chunk row is shared by every
document holding that text and carries the LAST writer's hash, so that read
judged an unchanged document stale and a changed one current;
``tests/test_3ajpd_staleness_from_catalog.py`` pins those scenarios.

Both `_index_document` (the prose gate) and `index_pdf` (the PDF gate) share
the SAME decision helper, `_document_is_current` — this file drives BOTH real
gate functions (not just the helper in isolation) so a future edit that
re-inlines a different check at only one of the two call sites is caught
here, not later.

Both gates are made cheap to drive past the "not fresh" branch by mocking
their chunk-producing callback (``chunk_fn`` for ``_index_document``,
``nexus.doc_indexer._pdf_chunks`` for ``index_pdf``) to return an empty
list — ``if not prepared: return 0`` fires immediately, so "the gate did NOT
skip" is observable as "the chunk callback WAS called" without any real
chunking/embedding.
"""
from __future__ import annotations

import logging
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import structlog

from nexus.doc_indexer import _index_document, index_pdf

CONTENT_HASH = "c" * 64
OTHER_HASH = "d" * 64
COLLECTION = "docs__testowner__voyage-context-3__v1"
TARGET_MODEL = "voyage-context-3"


def _configure_structlog_to_stdlib() -> None:
    # Reroutes structlog through stdlib logging so caplog can see it — the
    # documented pattern (tests/test_indexer_utils_repo.py) for this repo's
    # WARNING-vs-DEBUG log-level assertions.
    structlog.configure(
        processors=[structlog.stdlib.render_to_log_kwargs],
        wrapper_class=structlog.stdlib.BoundLogger,
        logger_factory=structlog.stdlib.LoggerFactory(),
    )


class _Entry:
    def __init__(self, tumbler: str, index_state, index_content_hash: str) -> None:
        self.tumbler = tumbler
        self.index_state = index_state
        self.index_content_hash = index_content_hash
        self.physical_collection = COLLECTION


class _Col:
    """T3 collection stub: the presence-and-model probe of one chunk the
    document's own manifest names (`ids=[chash], include=["metadatas"]`)."""

    def __init__(self, existing_meta: list[dict]) -> None:
        self._existing_meta = existing_meta
        self.get_calls: list[dict] = []

    def get(self, where=None, include=None, limit=None, ids=None, **kw):
        self.get_calls.append({"where": where, "ids": ids})
        if where is not None:
            raise AssertionError(f"no gate may probe T3 by metadata any more: {where}")
        return {"metadatas": self._existing_meta}


class _T3:
    def __init__(self, col: _Col) -> None:
        self._col = col

    def get_or_create_collection(self, name: str) -> _Col:
        return self._col


class _FakeCat:
    """Doubles as both reader and writer where needed — `by_file_path`
    returning the entry directly short-circuits `_register_or_lookup_doc_id`
    before it ever needs a writer, so no separate writer double is required.
    """

    def __init__(self, entry: _Entry, verify_result=None, verify_exc: Exception | None = None) -> None:
        self.entry = entry
        self.verify_result = verify_result
        self.verify_exc = verify_exc
        self.manifest_verify_calls = 0
        self.by_doc_id_calls = 0

    # prose gate identity (_doc_id_for_path) + PDF gate registration
    def by_source_uri(self, uri: str):
        return self.entry

    def by_file_path(self, owner, fp):
        return self.entry

    def curator_owner_tumbler_by_name(self, name: str):
        return "9.9"

    # the fence read (_index_fence_state)
    def by_doc_id(self, doc_id: str):
        self.by_doc_id_calls += 1
        return self.entry

    # the retired fall-through verify — UNUSED since RDR-191 Phase 6
    # (nexus-o8dil.33); nexus-3ajpd deleted its caller. Kept as a spy so
    # tests can assert manifest_verify_calls == 0 (no gate probes the engine's
    # verify), rather than deleting the double and losing that check.
    def manifest_verify(self, doc_id: str):
        self.manifest_verify_calls += 1
        if self.verify_exc is not None:
            raise self.verify_exc
        return self.verify_result

    # the presence probe (_own_chunk_metadata): the document's own manifest
    def get_manifest(self, doc_id: str):
        return [SimpleNamespace(chash="c" * 64, position=0)]

    def close(self) -> None:
        pass  # _register_or_lookup_doc_id closes both reader and writer


def _patch_catalog(monkeypatch, cat: _FakeCat) -> None:
    monkeypatch.setattr(
        "nexus.catalog.factory.make_catalog_reader", lambda *a, **k: cat, raising=False,
    )
    monkeypatch.setattr(
        "nexus.catalog.factory.make_catalog_writer", lambda *a, **k: MagicMock(), raising=False,
    )


def _dummy_embed(docs, model):
    return ([[0.0]] * len(docs), model)


# ── the shared decision helper, driven through `_index_document` (prose gate) ──


def _run_prose_gate(monkeypatch, entry: _Entry, *, content_hash: str = CONTENT_HASH,
                     verify_result=None, verify_exc=None):
    monkeypatch.setattr("nexus.doc_indexer._sha256", lambda path: content_hash)
    cat = _FakeCat(entry, verify_result=verify_result, verify_exc=verify_exc)
    _patch_catalog(monkeypatch, cat)

    col = _Col([{"content_hash": content_hash, "embedding_model": TARGET_MODEL}])
    t3 = _T3(col)
    chunk_fn = MagicMock(return_value=[])

    result = _index_document(
        Path("/tmp/nexus-5xn3k-threeway/doc.md"), "testowner", chunk_fn, t3,
        collection_name=COLLECTION, embed_fn=_dummy_embed, force=False,
    )
    return result, chunk_fn, cat


def test_prose_complete_and_hash_match_skips_no_probe(monkeypatch) -> None:
    entry = _Entry("1.1.1", "complete", CONTENT_HASH)
    result, chunk_fn, cat = _run_prose_gate(monkeypatch, entry)
    assert result == 0
    assert chunk_fn.call_count == 0, "complete+hash-match must skip without re-chunking"
    assert cat.manifest_verify_calls == 0, "complete+hash-match must not probe manifest/verify"


def test_prose_complete_but_hash_mismatch_is_stale(monkeypatch) -> None:
    entry = _Entry("1.1.1", "complete", OTHER_HASH)
    result, chunk_fn, cat = _run_prose_gate(monkeypatch, entry)
    assert chunk_fn.call_count == 1, "a completed run for DIFFERENT content must not skip"
    assert cat.manifest_verify_calls == 0, "a definitive complete/mismatch needs no probe"


def test_prose_indexing_is_stale_no_probe(monkeypatch) -> None:
    entry = _Entry("1.1.1", "indexing", CONTENT_HASH)
    result, chunk_fn, cat = _run_prose_gate(monkeypatch, entry)
    assert chunk_fn.call_count == 1, "'indexing' must never read as done"
    assert cat.manifest_verify_calls == 0, "'indexing' is definitive — no probe"


def test_prose_failed_is_stale_no_probe(monkeypatch) -> None:
    entry = _Entry("1.1.1", "failed", CONTENT_HASH)
    result, chunk_fn, cat = _run_prose_gate(monkeypatch, entry)
    assert chunk_fn.call_count == 1, "'failed' must never read as done"
    assert cat.manifest_verify_calls == 0, "'failed' is definitive — no probe"


def test_prose_unknown_fence_is_stale(monkeypatch) -> None:
    """nexus-3ajpd: a NULL fence has nothing to say, so the document is
    indexed (which stamps its fence). It used to skip on a chunk row matched
    by content_hash, which is the last writer's hash and not this document's."""
    entry = _Entry("1.1.1", None, "")
    result, chunk_fn, cat = _run_prose_gate(monkeypatch, entry)
    assert chunk_fn.call_count == 1, "no fence, no skip"
    assert cat.manifest_verify_calls == 0


def test_prose_fence_read_itself_unreadable_is_stale_and_warns(monkeypatch, caplog) -> None:
    """The entry lookup (by_doc_id) itself throws. The document reads as
    stale (nothing says it is current) and the failure logs at WARNING,
    never DEBUG: a fence that silently stops working recreates the ac4id bug
    one layer up."""
    _configure_structlog_to_stdlib()

    monkeypatch.setattr("nexus.doc_indexer._sha256", lambda path: CONTENT_HASH)
    cat = MagicMock()
    cat.by_source_uri.return_value = _Entry("1.1.1", None, "")
    cat.by_doc_id.side_effect = RuntimeError("catalog down")
    cat.manifest_verify.return_value = {"referenced": 0, "present": 0, "missing": 0}
    _patch_catalog(monkeypatch, cat)

    col = _Col([{"content_hash": CONTENT_HASH, "embedding_model": TARGET_MODEL}])
    t3 = _T3(col)
    chunk_fn = MagicMock(return_value=[])

    with caplog.at_level(logging.WARNING, logger="nexus.doc_indexer"):
        result = _index_document(
            Path("/tmp/nexus-5xn3k-threeway/doc.md"), "testowner", chunk_fn, t3,
            collection_name=COLLECTION, embed_fn=_dummy_embed, force=False,
        )
    assert chunk_fn.call_count == 1, "an unreadable fence never reads as current"
    assert any(
        r.msg == "index_fence_read_failed" and r.levelname == "WARNING"
        for r in caplog.records
    ), "an unreadable fence entry must log at WARNING, never DEBUG"


# ── the same decision helper, driven through `index_pdf` (PDF gate) ─────────


def _run_pdf_gate(monkeypatch, tmp_path: Path, entry: _Entry, *, content_hash: str = CONTENT_HASH,
                   verify_result=None, verify_exc=None):
    monkeypatch.setattr("nexus.doc_indexer._sha256", lambda path: content_hash)
    cat = _FakeCat(entry, verify_result=verify_result, verify_exc=verify_exc)
    _patch_catalog(monkeypatch, cat)

    col = _Col([{"content_hash": content_hash, "embedding_model": TARGET_MODEL}])
    t3 = _T3(col)
    pdf_chunks_mock = MagicMock(return_value=[])
    monkeypatch.setattr("nexus.doc_indexer._pdf_chunks", pdf_chunks_mock)

    # nexus-1sd0f (round 3): index_pdf now stats the path directly (a
    # zero-byte pre-registration guard, before any of the mocks above
    # apply) -- unlike the prose gate above (which calls
    # _index_document, a level below index_markdown's own guard), the
    # PDF gate is driven through index_pdf itself, so the fake path
    # must actually exist with non-zero size. It lives under tmp_path: a
    # fixed /tmp path is shared by every user on a pool runner, and one
    # left by another user is unwritable here.
    pdf_path = tmp_path / "doc.pdf"
    pdf_path.write_bytes(b"%PDF-1.4 fixture content for the staleness-gate test\n")

    result = index_pdf(
        pdf_path, "testowner", t3,
        collection_name=COLLECTION, embed_fn=_dummy_embed, force=False,
        streaming="never",
    )
    return result, pdf_chunks_mock, cat


def test_pdf_complete_and_hash_match_skips_no_probe(monkeypatch, tmp_path) -> None:
    entry = _Entry("1.1.1", "complete", CONTENT_HASH)
    result, pdf_chunks_mock, cat = _run_pdf_gate(monkeypatch, tmp_path, entry)
    assert pdf_chunks_mock.call_count == 0
    assert cat.manifest_verify_calls == 0


def test_pdf_indexing_is_stale_no_probe(monkeypatch, tmp_path) -> None:
    entry = _Entry("1.1.1", "indexing", CONTENT_HASH)
    result, pdf_chunks_mock, cat = _run_pdf_gate(monkeypatch, tmp_path, entry)
    assert pdf_chunks_mock.call_count == 1
    assert cat.manifest_verify_calls == 0


def test_pdf_failed_is_stale_no_probe(monkeypatch, tmp_path) -> None:
    entry = _Entry("1.1.1", "failed", CONTENT_HASH)
    result, pdf_chunks_mock, cat = _run_pdf_gate(monkeypatch, tmp_path, entry)
    assert pdf_chunks_mock.call_count == 1
    assert cat.manifest_verify_calls == 0


def test_pdf_unknown_fence_is_stale(monkeypatch, tmp_path) -> None:
    """nexus-3ajpd: mirrors the prose gate."""
    entry = _Entry("1.1.1", None, "")
    result, pdf_chunks_mock, cat = _run_pdf_gate(monkeypatch, tmp_path, entry)
    assert pdf_chunks_mock.call_count == 1
    assert cat.manifest_verify_calls == 0
