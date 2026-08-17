# SPDX-License-Identifier: AGPL-3.0-or-later
"""nexus-5xn3k.3 RUNFENCE: the staleness three-way, driven at BOTH gates.

Design memo (T2 nexus "5xn3k-design-2026-08-02") §3.4:

    fresh  <=>  a chunk matches (content_hash, embedding_model)  [today's condition]
           AND  entry.index_state == 'complete'
           AND  entry.index_content_hash == content_hash

    * 'indexing' / 'failed'      -> stale, definitively. No probe.
    * NULL / absent field        -> fall through to manifest/verify (ONE
                                     engine call, via _manifest_is_fully_present).
    * verify unreachable          -> fail open at WARNING.

RDR-191 PHASE 6 REBASE (nexus-o8dil.33), 2026-08-15: the manifest-chunk FK
makes `_manifest_is_fully_present`'s underlying question provably always
True (see its own docstring) — it is now a bare `return True`, never calls
`manifest_verify`, and never fails or warns. The 'unknown' fall-through
branch tests below are rebased accordingly: the branch still runs (no probe
skipped), but the probe itself is a no-op that always says "fresh" — the
distinction this file still proves is DEFINITIVE-no-probe (complete/
indexing/failed, unaffected by Phase 6) vs FALL-THROUGH (unknown, still
reaches `_manifest_is_fully_present`, which Phase 6 changed the answer of).

Both `_index_document` (the prose gate) and `index_pdf` (the PDF gate) share
the SAME decision helper, `_index_run_fresh` — this file drives BOTH real
gate functions (not just the helper in isolation) so a future edit that
re-inlines the old two-condition check at only one of the two call sites is
caught here, not later.

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
    """T3 collection stub: only the initial chunk-hash existence check
    (`where=..., include=["metadatas"], limit=1`) is exercised — the manifest
    presence check moved server-side (RUNFENCE) and no longer touches T3."""

    def __init__(self, existing_meta: list[dict]) -> None:
        self._existing_meta = existing_meta
        self.get_calls: list[dict] = []

    def get(self, where=None, include=None, limit=None, ids=None, **kw):
        self.get_calls.append({"where": where, "ids": ids})
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

    # the fall-through verify (_manifest_is_fully_present) — UNUSED as of
    # RDR-191 Phase 6 (nexus-o8dil.33): the function no longer calls
    # manifest_verify at all (it is a bare `return True`). Kept as a spy so
    # tests can assert manifest_verify_calls == 0 (the regression this
    # rebase pins), rather than deleting the double and losing that check.
    def manifest_verify(self, doc_id: str):
        self.manifest_verify_calls += 1
        if self.verify_exc is not None:
            raise self.verify_exc
        return self.verify_result

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


def test_prose_unknown_falls_through_to_verify_clean_skips(monkeypatch) -> None:
    entry = _Entry("1.1.1", None, "")
    result, chunk_fn, cat = _run_prose_gate(
        monkeypatch, entry, verify_result={"referenced": 2, "present": 2, "missing": 0},
    )
    assert chunk_fn.call_count == 0, "a clean verify must still skip"
    assert cat.manifest_verify_calls == 0, (
        "RDR-191 Phase 6: _manifest_is_fully_present no longer calls "
        "manifest_verify at all"
    )


def test_prose_unknown_falls_through_no_longer_re_indexes_on_a_stale_verify_result(
    monkeypatch,
) -> None:
    """RDR-191 Phase 6 rebase (nexus-o8dil.33) of the former
    test_prose_unknown_falls_through_to_verify_missing_is_stale: the
    manifest-chunk FK makes 'missing' provably always False, so
    _manifest_is_fully_present no longer calls manifest_verify or branches
    on its result at all. A verify_result claiming missing chunks (were it
    ever consulted) would no longer change the outcome — the document
    always reads as fresh and skips. This is a POSITIVE proof the fixture
    is provided but ignored, not merely "nothing happens": the fixture uses
    the SAME shape the old test used to force a re-index, to make clear the
    behavior changed, not that the fixture was silently dropped."""
    entry = _Entry("1.1.1", None, "")
    result, chunk_fn, cat = _run_prose_gate(
        monkeypatch, entry, verify_result={"referenced": 3, "present": 1, "missing": 2},
    )
    assert chunk_fn.call_count == 0, (
        "the FK makes 'missing' unreachable; the document now always "
        "reads as fresh regardless of the (unconsulted) verify_result"
    )
    assert cat.manifest_verify_calls == 0


def test_prose_unknown_verify_unreachable_no_longer_touches_the_catalog(monkeypatch) -> None:
    """RDR-191 Phase 6 rebase of the former
    test_prose_unknown_verify_unreachable_fails_open_and_warns: there is no
    verify call left to be unreachable, so verify_exc (were it ever
    consulted) changes nothing and no WARNING is logged — the fail-open
    behavior is now unconditional truth, not a caught exception."""
    entry = _Entry("1.1.1", None, "")
    result, chunk_fn, cat = _run_prose_gate(
        monkeypatch, entry, verify_exc=RuntimeError("engine unreachable"),
    )
    assert chunk_fn.call_count == 0, "the document reads as fresh and skips unconditionally"
    assert cat.manifest_verify_calls == 0


def test_prose_fence_read_itself_unreadable_fails_open_and_warns(monkeypatch, caplog) -> None:
    """The entry lookup (by_doc_id) itself throws — distinct from the
    manifest_verify fallback throwing. Must ALSO fail open at WARNING."""
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
    assert chunk_fn.call_count == 0, "a clean fallback verify must still skip"
    assert any(
        r.msg == "index_fence_read_failed" and r.levelname == "WARNING"
        for r in caplog.records
    ), "an unreadable fence entry must log at WARNING, never DEBUG"


# ── the same decision helper, driven through `index_pdf` (PDF gate) ─────────


def _run_pdf_gate(monkeypatch, entry: _Entry, *, content_hash: str = CONTENT_HASH,
                   verify_result=None, verify_exc=None):
    monkeypatch.setattr("nexus.doc_indexer._sha256", lambda path: content_hash)
    cat = _FakeCat(entry, verify_result=verify_result, verify_exc=verify_exc)
    _patch_catalog(monkeypatch, cat)

    col = _Col([{"content_hash": content_hash, "embedding_model": TARGET_MODEL}])
    t3 = _T3(col)
    pdf_chunks_mock = MagicMock(return_value=[])
    monkeypatch.setattr("nexus.doc_indexer._pdf_chunks", pdf_chunks_mock)

    result = index_pdf(
        Path("/tmp/nexus-5xn3k-threeway/doc.pdf"), "testowner", t3,
        collection_name=COLLECTION, embed_fn=_dummy_embed, force=False,
        streaming="never",
    )
    return result, pdf_chunks_mock, cat


def test_pdf_complete_and_hash_match_skips_no_probe(monkeypatch) -> None:
    entry = _Entry("1.1.1", "complete", CONTENT_HASH)
    result, pdf_chunks_mock, cat = _run_pdf_gate(monkeypatch, entry)
    assert pdf_chunks_mock.call_count == 0
    assert cat.manifest_verify_calls == 0


def test_pdf_indexing_is_stale_no_probe(monkeypatch) -> None:
    entry = _Entry("1.1.1", "indexing", CONTENT_HASH)
    result, pdf_chunks_mock, cat = _run_pdf_gate(monkeypatch, entry)
    assert pdf_chunks_mock.call_count == 1
    assert cat.manifest_verify_calls == 0


def test_pdf_failed_is_stale_no_probe(monkeypatch) -> None:
    entry = _Entry("1.1.1", "failed", CONTENT_HASH)
    result, pdf_chunks_mock, cat = _run_pdf_gate(monkeypatch, entry)
    assert pdf_chunks_mock.call_count == 1
    assert cat.manifest_verify_calls == 0


def test_pdf_unknown_falls_through_to_verify_clean_skips(monkeypatch) -> None:
    entry = _Entry("1.1.1", None, "")
    result, pdf_chunks_mock, cat = _run_pdf_gate(
        monkeypatch, entry, verify_result={"referenced": 2, "present": 2, "missing": 0},
    )
    assert pdf_chunks_mock.call_count == 0
    assert cat.manifest_verify_calls == 0, (
        "RDR-191 Phase 6: _manifest_is_fully_present no longer calls "
        "manifest_verify at all"
    )


def test_pdf_unknown_falls_through_no_longer_re_indexes_on_a_stale_verify_result(
    monkeypatch,
) -> None:
    """RDR-191 Phase 6 rebase (nexus-o8dil.33) of the former
    test_pdf_unknown_falls_through_to_verify_missing_is_stale — mirrors the
    prose-gate sibling test's rebase rationale."""
    entry = _Entry("1.1.1", None, "")
    result, pdf_chunks_mock, cat = _run_pdf_gate(
        monkeypatch, entry, verify_result={"referenced": 3, "present": 1, "missing": 2},
    )
    assert pdf_chunks_mock.call_count == 0, (
        "the FK makes 'missing' unreachable; the document now always "
        "reads as fresh regardless of the (unconsulted) verify_result"
    )
    assert cat.manifest_verify_calls == 0


def test_pdf_unknown_verify_unreachable_no_longer_touches_the_catalog(monkeypatch) -> None:
    entry = _Entry("1.1.1", None, "")
    result, pdf_chunks_mock, cat = _run_pdf_gate(
        monkeypatch, entry, verify_exc=RuntimeError("engine unreachable"),
    )
    assert pdf_chunks_mock.call_count == 0, "the document reads as fresh and skips unconditionally"
    assert cat.manifest_verify_calls == 0
