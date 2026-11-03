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

Real engine substrate: the check asks the catalog for the PDF documents of
the collection that record no ``extraction_method`` and reads their manifest
chunks by id; it sends no ``where`` to the vector routes, because since
nexus-u9dc5 the engine judges ``title`` on the owning document's catalog
title (every PDF has one, its file stem) and a ``title == ""`` filter would
name nothing. Every document here is registered with a non-blank title, as
pre-flight does, so a scan that filtered on the row title through the engine
finds nothing and fails these tests.
"""
from __future__ import annotations

import inspect
from types import SimpleNamespace

import pytest

from nexus import health
from nexus.db import make_t3
from nexus.health import _is_pdf_stub_metadata
from tests._catalog_fixture_ops import ActiveCatalog
from tests._chunk_seed import seed_chunks_direct

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


def _seed(t3, rows: list[tuple[str, dict]]) -> list[str]:
    ids = [f"{i:064x}" for i in range(1, len(rows) + 1)]
    seed_chunks_direct(
        _COLLECTION,
        ids=ids,
        documents=[text for text, _ in rows],
        embed=True,
        metadatas=[meta for _, meta in rows],
    )
    return ids


def _own(chashes: list[str], file_path: str, meta: dict | None = None, content_type: str = "paper") -> None:
    """Register a document at *file_path* (titled with its file name, as pre-flight does) whose manifest
    names *chashes*; *meta* is its catalog meta."""
    cat = ActiveCatalog()
    owner = cat.register_owner("rte90-owner", "curator")
    tumbler = str(cat.register(
        owner, file_path.rsplit("/", 1)[-1], content_type=content_type,
        file_path=file_path, physical_collection=_COLLECTION,
        chunk_count=len(chashes), meta=meta or {},
    ))
    cat.write_manifest(
        tumbler, [{"chash": c, "position": i} for i, c in enumerate(chashes)],
        collection=_COLLECTION,
    )


def _own_without_uri(chashes: list[str], title: str) -> None:
    """Register a document with NO file path / source URI whose manifest
    names *chashes* -- RDR-192 Step 5 (nexus-wbfpw.10) makes a manifest-less
    chunk invisible to every content read, this check's own scan included,
    so the only way left to reach the "resolves to no catalog source URI"
    branch is a real, live-owned chunk whose owner just has no URI.
    """
    cat = ActiveCatalog()
    owner = cat.register_owner("rte90-owner", "curator")
    tumbler = str(cat.register(
        owner, title, content_type="paper",
        physical_collection=_COLLECTION, chunk_count=len(chashes),
    ))
    cat.write_manifest(
        tumbler, [{"chash": c, "position": i} for i, c in enumerate(chashes)],
        collection=_COLLECTION,
    )


def _row(results):
    rows = [r for r in results if r.label == _LABEL]
    assert len(rows) == 1, [r.label for r in results]
    return rows[0]


def test_stub_pdf_chunks_are_named_and_controls_are_not(t2_service_env) -> None:
    t3 = make_t3()
    ids = _seed(t3, [
        # Two stub chunks of one document: the w94eo signature.
        ("stub page two text", _meta(title="", content_hash="a" * 64)),
        ("stub page three text", _meta(title="", content_hash="a" * 64)),
        # A stub chunk whose OWNING document has no source URI (RDR-192 Step
        # 5, nexus-wbfpw.10: a manifest-less chunk is invisible to this
        # check's own scan now, so "resolves to no catalog source URI" can
        # only mean the owner itself has none).
        ("orphan stub text", _meta(title="", content_hash="b" * 64)),
        # Healthy post-pass chunk.
        ("healthy mineru chunk", _meta(title="FootprintRAG", extraction_method="mineru")),
        # Pre-nexus-1oguj PDF chunk: titled, no extraction_method. Healthy.
        ("legacy pdf chunk", _meta(title="Old Paper")),
        # Markdown chunk with an empty title: not a PDF chunk.
        ("markdown chunk", _meta(content_type="markdown", title="")),
    ])

    _own(ids[:2], "/tmp/rte90/FootprintRAG.pdf")
    _own_without_uri([ids[2]], "orphan stub document")
    # The controls are owned too, so each one is judged, not merely invisible.
    _own([ids[3]], "/tmp/rte90/Healthy.pdf", meta={"extraction_method": "mineru"})
    _own([ids[4]], "/tmp/rte90/Old.pdf")
    _own([ids[5]], "/tmp/rte90/notes.md", content_type="markdown")

    row = _row(health._check_pdf_stub_metadata())

    assert not row.ok and row.warn, row.detail
    assert "2 PDF chunk(s) in 1 document(s)" in row.detail, row.detail
    assert "FootprintRAG.pdf (2)" in row.detail, row.detail
    # The unowned-URI chunk is reported apart, by content hash, with its own remedy.
    assert "1 more placeholder PDF chunk(s) resolve to no catalog source URI" in row.detail, row.detail
    assert "content_hash bbbbbbbbbbbb (1)" in row.detail, row.detail
    assert any("registered without one" in f or "no source URI" in f for f in row.fix_suggestions), (
        row.fix_suggestions
    )
    assert any("--force" in f for f in row.fix_suggestions), row.fix_suggestions


def test_healthy_pdf_chunks_pass_with_a_count(t2_service_env) -> None:
    _seed(make_t3(), [
        ("healthy one", _meta(title="Paper", extraction_method="mineru")),
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


def _fake_catalog(monkeypatch, chashes: list[str], *, listed: list | None = None) -> None:
    """One live PDF document with no extraction record, holding *chashes*, behind a stub catalog reader."""
    doc = SimpleNamespace(
        tumbler="1.1.1", title="big", meta={}, content_type="paper", file_path="/big.pdf",
        source_uri="file:///big.pdf", physical_collection=_COLLECTION,
    )

    class _Reader:
        def list_by_collection(self, collection):
            return [doc] if listed is None else listed

        def get_manifests(self, doc_ids):
            return {"1.1.1": [SimpleNamespace(chash=c, collection=_COLLECTION) for c in chashes]}

        def close(self):
            pass

    monkeypatch.setattr("nexus.catalog.factory.make_catalog_reader", lambda: _Reader())


class _IdCol:
    """``col.get(ids=...)`` over rows that are all placeholders; it refuses a ``where``."""

    def __init__(self) -> None:
        self.reads: list[list[str]] = []

    def get(self, *, ids=None, include=None, limit=None, **kw):
        assert "where" not in kw, "the scan must not send a where to the vector routes"
        self.reads.append(list(ids))
        return {"ids": list(ids), "metadatas": [{"content_type": "pdf", "title": ""}] * len(ids)}


def _t3_of(col) -> object:
    class _T3:
        def list_collections(self):
            return [{"name": _COLLECTION}]

        def get_or_create_collection(self, name):
            return col

    return _T3()


def test_a_collection_past_the_chunk_cap_is_partial(monkeypatch) -> None:
    cap = health._PDF_STUB_MAX_PAGES * 3
    _fake_catalog(monkeypatch, [f"{i:064x}" for i in range(cap + 1)])
    col = _IdCol()
    monkeypatch.setattr("nexus.db.make_t3", lambda: _t3_of(col))
    monkeypatch.setattr("nexus.db.limits.MAX_QUERY_RESULTS", 3)

    row = _row(health._check_pdf_stub_metadata())

    assert not row.ok and row.warn, row.detail
    assert "PARTIAL" in row.detail and "floor" in row.detail, row.detail
    assert sum(len(r) for r in col.reads) == cap, "no more than the cap is read"


def _legacy_collection(monkeypatch, docs: int, per_doc: int, stub_docs: set[int]):
    """*docs* method-less PDF documents of *per_doc* chunks each behind a stub catalog; the documents whose
    index is in *stub_docs* carry the upload placeholder on every chunk, the rest are titled legacy rows
    (indexed before nexus-6pfgb.6, so no extraction_method, but healthy). Returns the column that serves them."""
    entries, held = [], {}
    for d in range(docs):
        tumbler = f"1.1.{d + 1}"
        entries.append(SimpleNamespace(
            tumbler=tumbler, title=f"doc{d}", meta={}, content_type="paper", file_path=f"/doc{d}.pdf",
            source_uri=f"file:///doc{d}.pdf", physical_collection=_COLLECTION,
        ))
        held[tumbler] = [f"{d * per_doc + i + 1:064x}" for i in range(per_doc)]

    class _Reader:
        def list_by_collection(self, collection):
            return entries

        def get_manifests(self, doc_ids):
            return {t: [SimpleNamespace(chash=c, collection=_COLLECTION) for c in held[t]] for t in doc_ids}

        def close(self):
            pass

    stub_chashes = {c for d in stub_docs for c in held[f"1.1.{d + 1}"]}

    class _Col:
        def __init__(self) -> None:
            self.read: list[str] = []

        def get(self, *, ids=None, include=None, limit=None, **kw):
            assert "where" not in kw
            self.read.extend(ids)
            return {
                "ids": list(ids),
                "metadatas": [
                    {"content_type": "pdf", "title": "" if c in stub_chashes else "Old Paper"} for c in ids
                ],
            }

    monkeypatch.setattr("nexus.catalog.factory.make_catalog_reader", lambda: _Reader())
    return _Col()


def test_a_large_legacy_collection_gets_a_definite_verdict_within_the_read_bound(monkeypatch) -> None:
    """nexus-iho0e: 30 titled method-less documents (600 chunks, three times the 200-chunk read bound) and 2
    real stubs. Before, the bound counted every chunk of every method-less document, so the scan ran out on
    healthy legacy rows and read PARTIAL for good; now only a stub is read in full."""
    col = _legacy_collection(monkeypatch, docs=30, per_doc=20, stub_docs={4, 21})
    monkeypatch.setattr("nexus.db.make_t3", lambda: _t3_of(col))
    monkeypatch.setattr("nexus.db.limits.MAX_QUERY_RESULTS", 10)

    row = _row(health._check_pdf_stub_metadata())

    assert "PARTIAL" not in row.detail, row.detail
    assert not row.ok and row.warn, row.detail
    assert "40 PDF chunk(s) in 2 document(s)" in row.detail, row.detail
    assert "doc4.pdf (20)" in row.detail and "doc21.pdf (20)" in row.detail, row.detail
    assert len(col.read) == len(set(col.read)) <= health._PDF_STUB_MAX_PAGES * 10, "each chunk once, within the bound"


def test_a_large_legacy_collection_with_no_stub_reads_clean(monkeypatch) -> None:
    col = _legacy_collection(monkeypatch, docs=30, per_doc=20, stub_docs=set())
    monkeypatch.setattr("nexus.db.make_t3", lambda: _t3_of(col))
    monkeypatch.setattr("nexus.db.limits.MAX_QUERY_RESULTS", 10)

    row = _row(health._check_pdf_stub_metadata())

    assert row.ok and not row.warn and "PARTIAL" not in row.detail, row.detail
    assert 0 < len(col.read) < 600, "a sample, not the whole 600"


def test_a_collection_too_large_to_sample_is_partial_and_says_why_and_what_to_run(monkeypatch) -> None:
    """100 documents at four probes each is 400 reads against a 200-chunk bound: the scan genuinely cannot
    finish, so it says so, with the reason and the way to shrink the set."""
    col = _legacy_collection(monkeypatch, docs=100, per_doc=20, stub_docs={0})
    monkeypatch.setattr("nexus.db.make_t3", lambda: _t3_of(col))
    monkeypatch.setattr("nexus.db.limits.MAX_QUERY_RESULTS", 10)

    row = _row(health._check_pdf_stub_metadata())

    assert not row.ok and row.warn, row.detail
    assert "PARTIAL" in row.detail and "floor" in row.detail, row.detail
    assert "method-less" in row.detail and "backfill-meta" in row.detail, row.detail
    assert len(col.read) <= health._PDF_STUB_MAX_PAGES * 10, "the bound holds even when it is reached"


def test_a_collection_that_cannot_be_read_is_not_a_clean_verdict(monkeypatch) -> None:
    class _Col:
        def get(self, **kw):
            raise RuntimeError("engine 503")

    _fake_catalog(monkeypatch, ["a" * 64])
    monkeypatch.setattr("nexus.db.make_t3", lambda: _t3_of(_Col()))

    row = _row(health._check_pdf_stub_metadata())

    assert not row.ok and row.warn, row.detail
    assert "NOT CHECKED" in row.detail and _COLLECTION in row.detail, row.detail


def test_an_unreadable_catalog_is_not_a_clean_verdict(monkeypatch) -> None:
    def boom():
        raise ConnectionError("catalog down")

    monkeypatch.setattr("nexus.catalog.factory.make_catalog_reader", boom)
    monkeypatch.setattr("nexus.db.make_t3", lambda: _t3_of(_IdCol()))

    row = _row(health._check_pdf_stub_metadata())

    assert not row.ok and row.warn, row.detail
    assert "NOT CHECKED" in row.detail and _COLLECTION in row.detail, row.detail


def test_t3_unavailable_degrades_to_a_skip(monkeypatch) -> None:
    def _boom():
        raise RuntimeError("no service")

    monkeypatch.setattr("nexus.db.make_t3", _boom)

    row = _row(health._check_pdf_stub_metadata())

    assert row.ok and "skipped" in row.detail, row.detail


def test_exactly_the_cap_is_complete(monkeypatch) -> None:
    """The cap's worth of chunks and no more: the scan finished."""
    cap = health._PDF_STUB_MAX_PAGES * 3
    _fake_catalog(monkeypatch, [f"{i:064x}" for i in range(cap)])
    monkeypatch.setattr("nexus.db.make_t3", lambda: _t3_of(_IdCol()))
    monkeypatch.setattr("nexus.db.limits.MAX_QUERY_RESULTS", 3)

    row = _row(health._check_pdf_stub_metadata())

    assert "PARTIAL" not in row.detail, row.detail
    assert f"{cap} PDF chunk(s) in 1 document(s)" in row.detail, row.detail


def test_a_document_that_records_a_method_or_is_not_a_pdf_is_never_read(monkeypatch) -> None:
    def doc(tumbler, ct, path, meta):
        return SimpleNamespace(tumbler=tumbler, title="t", meta=meta, content_type=ct, file_path=path,
                               source_uri="", physical_collection=_COLLECTION)

    listed = [
        doc("1.1.1", "paper", "/a.pdf", {"extraction_method": "mineru"}),
        doc("1.1.2", "markdown", "/b.md", {}),
        doc("1.1.3", "knowledge", "", {}),
    ]
    _fake_catalog(monkeypatch, ["a" * 64], listed=listed)
    col = _IdCol()
    monkeypatch.setattr("nexus.db.make_t3", lambda: _t3_of(col))

    row = _row(health._check_pdf_stub_metadata())

    assert row.ok and not row.warn, row.detail
    assert col.reads == [], "no candidate, no chunk read"


def test_wired_into_run_health_checks() -> None:
    assert "_check_pdf_stub_metadata()" in inspect.getsource(health.run_health_checks)


def test_the_scan_sends_no_where_so_an_engine_that_judges_title_on_the_catalog_cannot_blind_it(monkeypatch) -> None:
    """Since nexus-u9dc5 the engine judges a ``where`` on ``title`` on the owning document's catalog title
    (a PDF's is its stem, never blank). The scan names the stub from the catalog's document list and the
    row it reads by id, so it finds the stub whatever the engine does with a ``where``."""
    _fake_catalog(monkeypatch, ["a" * 64, "b" * 64])
    col = _IdCol()
    monkeypatch.setattr("nexus.db.make_t3", lambda: _t3_of(col))

    row = _row(health._check_pdf_stub_metadata())

    assert not row.ok and "big.pdf (2)" in row.detail, row.detail


@pytest.mark.parametrize("title,has_method,flagged", [
    ("", False, True),
    ("Paper", False, False),
    ("", True, False),
    (None, False, True),
])
def test_stub_predicate(title, has_method, flagged) -> None:
    """The placeholder title is the row's; whether the post-pass ran is the OWNING document's own catalog
    record (nexus-6pfgb.16), passed in. The row's ``extraction_method`` is the last writer's and not read."""
    meta: dict = {"content_type": "pdf", "extraction_method": "mineru"}
    if title is not None:
        meta["title"] = title
    assert _is_pdf_stub_metadata(meta, document_has_method=has_method) is flagged
