# SPDX-License-Identifier: AGPL-3.0-or-later
"""nexus-rte90 (indexing-brittleness P0.6): the doctor row that finds PDF
chunks still carrying the streaming uploader's placeholder metadata.

The streaming PDF uploader writes ``title=''`` and no
``extraction_method`` (``pipeline_stages._pdf_chunk_metadata``); only the
post-pass ``_enrich_metadata_from_extraction`` fills them in. nexus-w94eo
measured 111 chunks across two documents left in that state (a late
upsert-chunks attempt overwrote the post-pass, or the run died before
it). Nothing reported it: ``nx enrich bib`` keys on chunk title and
silently skipped them.

The signature is the CONJUNCTION. A PDF chunk indexed before nexus-1oguj
has no ``extraction_method`` but does have a title, and is healthy; a
markdown chunk may have no title, and is not a PDF chunk. Both are seeded
here as negative controls.

Real engine substrate: the check pages ``/v1/vectors/get`` with a
``where`` filter and ``include_source_uri``, which an in-memory double
does not model.
"""
from __future__ import annotations

import inspect

import pytest

from nexus import health
from nexus.db import make_t3
from nexus.health import _is_pdf_stub_metadata

_COLLECTION = "knowledge__rte90-pdf-stub__bge-base-en-v15-768__v1"
_LABEL = "PDF chunk metadata"


def _meta(**kw) -> dict:
    base = {
        "content_type": "pdf",
        "content_hash": "c" * 64,
        "indexed_at": "2026-09-24T19:04:00Z",
        "page_number": 1,
    }
    base.update(kw)
    return base


def _seed(t3, rows: list[tuple[str, dict]]) -> None:
    t3.upsert_chunks_with_embeddings(
        _COLLECTION,
        ids=[f"{i:064x}" for i in range(1, len(rows) + 1)],
        documents=[text for text, _ in rows],
        embeddings=[],
        metadatas=[meta for _, meta in rows],
    )


def _row(results):
    rows = [r for r in results if r.label == _LABEL]
    assert len(rows) == 1, [r.label for r in results]
    return rows[0]


def test_stub_pdf_chunks_are_named_and_controls_are_not(t2_service_env) -> None:

    t3 = make_t3()
    _seed(t3, [
        # Two stub chunks of one document: the w94eo signature.
        ("stub page two text", _meta(title="", content_hash="a" * 64)),
        ("stub page three text", _meta(title="", content_hash="a" * 64)),
        # Healthy post-pass chunk.
        ("healthy mineru chunk", _meta(title="FootprintRAG", extraction_method="mineru")),
        # Pre-nexus-1oguj PDF chunk: titled, no extraction_method. Healthy.
        ("legacy pdf chunk", _meta(title="Old Paper")),
        # Markdown chunk with an empty title: not a PDF chunk.
        ("markdown chunk", _meta(content_type="markdown", title="")),
    ])

    row = _row(health._check_pdf_stub_metadata())

    assert not row.ok and row.warn, row.detail
    assert "2 PDF chunk(s)" in row.detail, row.detail
    assert _COLLECTION in row.detail, row.detail
    assert "1 document(s)" in row.detail, row.detail
    assert row.fix_suggestions, "a WARN row must name its remedy"


def test_healthy_pdf_chunks_pass_with_a_count(t2_service_env) -> None:

    _seed(make_t3(), [
        ("healthy one", _meta(title="Paper", extraction_method="docling")),
        ("legacy one", _meta(title="Old Paper")),
    ])

    row = _row(health._check_pdf_stub_metadata())

    assert row.ok and not row.warn, row.detail
    # Non-vacuity: a pass must say it looked at something.
    assert "1 knowledge/docs collection(s) checked" in row.detail, row.detail


def test_no_collections_is_not_applicable(t2_service_env) -> None:

    row = _row(health._check_pdf_stub_metadata())

    assert row.ok and not row.warn, row.detail
    assert "not applicable" in row.detail, row.detail


def test_a_collection_past_the_page_cap_is_partial(monkeypatch) -> None:

    full_page = {
        "metadatas": [{"content_type": "pdf", "title": ""}] * 3,
        "source_uris": ["file:///big.pdf"] * 3,
    }

    class _Col:
        def get(self, **kw):
            return full_page

    class _T3:
        def list_collections(self):
            return [{"name": _COLLECTION}]

        def get_collection(self, name):
            return _Col()

    monkeypatch.setattr("nexus.db.make_t3", lambda: _T3())
    monkeypatch.setattr("nexus.db.limits.MAX_QUERY_RESULTS", 3)

    row = _row(health._check_pdf_stub_metadata())

    assert not row.ok and row.warn, row.detail
    assert "PARTIAL" in row.detail and "floor" in row.detail, row.detail


def test_a_collection_that_cannot_be_read_is_not_a_clean_verdict(monkeypatch) -> None:

    class _Col:
        def get(self, **kw):
            raise RuntimeError("engine 503")

    class _T3:
        def list_collections(self):
            return [{"name": _COLLECTION}]

        def get_collection(self, name):
            return _Col()

    monkeypatch.setattr("nexus.db.make_t3", lambda: _T3())

    row = _row(health._check_pdf_stub_metadata())

    assert not row.ok and row.warn, row.detail
    assert "NOT CHECKED" in row.detail and _COLLECTION in row.detail, row.detail


def test_t3_unavailable_degrades_to_a_skip(monkeypatch) -> None:

    def _boom():
        raise RuntimeError("no service")

    monkeypatch.setattr("nexus.db.make_t3", _boom)

    row = _row(health._check_pdf_stub_metadata())

    assert row.ok and "skipped" in row.detail, row.detail


def test_wired_into_run_health_checks() -> None:

    assert "_check_pdf_stub_metadata()" in inspect.getsource(health.run_health_checks)


@pytest.mark.parametrize("title,method,flagged", [
    ("", None, True),
    ("", "", True),
    ("Paper", None, False),
    ("", "mineru", False),
    (None, None, True),
])
def test_stub_predicate(title, method, flagged) -> None:

    meta: dict = {"content_type": "pdf"}
    if title is not None:
        meta["title"] = title
    if method is not None:
        meta["extraction_method"] = method
    assert _is_pdf_stub_metadata(meta) is flagged
