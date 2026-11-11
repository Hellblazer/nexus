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


def _seed_stub_and_controls() -> None:
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


def test_stub_pdf_chunks_are_named_and_controls_are_not(t2_service_env, monkeypatch) -> None:
    """One seeded tenant, judged twice: by the scan, per owning document (an engine without the bounded route), and by
    the engine's route (0.1.189), per collection. Both count the two stub chunks and the URI-less one, neither counts a
    control, and the URI-less owner gets its own remedy either way."""
    _seed_stub_and_controls()

    route_row = _row(health._check_pdf_stub_metadata())
    assert not route_row.ok and route_row.warn, route_row.detail
    assert route_row.detail.startswith("3 PDF chunk(s) in 1 collection(s)"), route_row.detail
    assert "file:///tmp/rte90/FootprintRAG.pdf" in route_row.detail and "content_hash bbbbbbbbbbbb" in route_row.detail
    assert not any(c in route_row.detail for c in ("Healthy.pdf", "Old.pdf", "notes.md")), route_row.detail
    assert any("no source URI" in f for f in route_row.fix_suggestions), route_row.fix_suggestions
    assert any("--force" in f for f in route_row.fix_suggestions), route_row.fix_suggestions

    monkeypatch.setattr(health, "_pdf_stub_route", lambda: None)
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


def _legacy_collection(
    monkeypatch, sizes: list[int], stub_docs: set[int] = frozenset(), *,
    stub_chunks: set[tuple[int, int]] = frozenset(), missing_docs: set[int] = frozenset(),
    noisy_docs: set[int] = frozenset(),
):
    """Method-less PDF documents behind a stub catalog, document ``d`` holding ``sizes[d]`` chunks. The
    documents in *stub_docs* carry the upload placeholder on every chunk, *stub_chunks* names single
    ``(document, chunk index)`` placeholders, and the rest are titled legacy rows (indexed before
    nexus-6pfgb.6: no extraction_method, but healthy). The chunks of *missing_docs* have no T3 row.
    The manifests of *noisy_docs* open with two empty chashes and two repeats of the first chunk.
    Returns the column; ``col.requested`` is every id it was asked for."""
    entries, held, n = [], {}, 0
    for d, size in enumerate(sizes):
        tumbler = f"1.1.{d + 1}"
        entries.append(SimpleNamespace(
            tumbler=tumbler, title=f"doc{d}", meta={}, content_type="paper", file_path=f"/doc{d}.pdf",
            source_uri=f"file:///doc{d}.pdf", physical_collection=_COLLECTION,
        ))
        held[tumbler] = [f"{n + i + 1:064x}" for i in range(size)]
        n += size

    class _Reader:
        def list_by_collection(self, collection):
            return entries

        def get_manifests(self, doc_ids):
            def rows(t):
                c = held[t]
                noise = ["", "", c[0], c[0]] if int(t.rsplit(".", 1)[1]) - 1 in noisy_docs else []
                return [SimpleNamespace(chash=x, collection=_COLLECTION) for x in noise + c]
            return {t: rows(t) for t in doc_ids}

        def close(self):
            pass

    chash = lambda d, i: held[f"1.1.{d + 1}"][i]  # noqa: E731
    stub = {c for d in stub_docs for c in held[f"1.1.{d + 1}"]} | {chash(d, i) for d, i in stub_chunks}
    gone = {c for d in missing_docs for c in held[f"1.1.{d + 1}"]}

    class _Col:
        def __init__(self) -> None:
            self.requested: list[str] = []

        def get(self, *, ids=None, include=None, limit=None, **kw):
            assert "where" not in kw
            self.requested.extend(ids)
            hit = [c for c in ids if c not in gone]
            return {
                "ids": hit,
                "metadatas": [{"content_type": "pdf", "title": "" if c in stub else "Old Paper"} for c in hit],
            }

    monkeypatch.setattr("nexus.catalog.factory.make_catalog_reader", lambda: _Reader())
    col = _Col()
    monkeypatch.setattr("nexus.db.make_t3", lambda: _t3_of(col))
    monkeypatch.setattr("nexus.db.limits.MAX_QUERY_RESULTS", 10)  # the read bound is 20 pages of 10 = 200 chunks
    return col


_BOUND = health._PDF_STUB_MAX_PAGES * 10


def test_a_large_legacy_collection_gets_a_definite_verdict_within_the_read_bound(monkeypatch) -> None:
    """nexus-iho0e: 30 titled method-less documents (600 chunks, three times the 200-chunk read bound) and 2
    real stubs. Before, the bound counted every chunk of every method-less document, so the scan ran out on
    healthy legacy rows and read PARTIAL for good; now only a stub is read in full."""
    col = _legacy_collection(monkeypatch, [20] * 30, {4, 21})

    row = _row(health._check_pdf_stub_metadata())

    assert "PARTIAL" not in row.detail, row.detail
    assert not row.ok and row.warn, row.detail
    assert "40 PDF chunk(s) in 2 document(s)" in row.detail, row.detail
    assert "doc4.pdf (20)" in row.detail and "doc21.pdf (20)" in row.detail, row.detail
    # The other documents were sampled, and the output says so.
    assert "SAMPLED" in row.detail and "backfill-meta" in row.detail, row.detail
    # 28 healthy documents x 4 probes + 2 stub documents x 20 chunks, each chunk once.
    assert len(col.requested) == len(set(col.requested)) == 28 * 4 + 2 * 20, len(col.requested)


def test_a_large_legacy_collection_with_no_stub_says_it_was_cleared_on_a_sample(monkeypatch) -> None:
    col = _legacy_collection(monkeypatch, [20] * 30)

    row = _row(health._check_pdf_stub_metadata())

    assert row.ok and not row.warn and "PARTIAL" not in row.detail, row.detail
    assert "clean on a sample" in row.detail and "backfill-meta" in row.detail, row.detail
    assert "4 chunks" in row.detail and "30 documents" in row.detail, row.detail
    assert len(col.requested) == 30 * 4


def test_a_collection_that_fits_the_bound_is_read_in_full_and_says_nothing_of_sampling(monkeypatch) -> None:
    """Exactly the bound's worth of method-less chunks (10 x 20 = 200): the exact path, the exact text."""
    col = _legacy_collection(monkeypatch, [20] * 10)

    row = _row(health._check_pdf_stub_metadata())

    assert row.ok and "clean on a sample" not in row.detail and "SAMPLED" not in row.detail, row.detail
    assert len(col.requested) == _BOUND


def test_one_chunk_past_the_bound_is_sampled(monkeypatch) -> None:
    col = _legacy_collection(monkeypatch, [20] * 10 + [1])

    row = _row(health._check_pdf_stub_metadata())

    assert row.ok and "clean on a sample" in row.detail, row.detail
    assert len(col.requested) == 10 * 4 + 1


def test_a_document_with_fewer_chunks_than_the_probes_is_read_whole_and_a_stub_in_it_is_counted(monkeypatch) -> None:
    """10 two-chunk and 10 fifty-chunk documents (520 chunks): the small ones are their own sample. One small
    and one large document are stubs; both are counted in full."""
    col = _legacy_collection(monkeypatch, [2] * 10 + [50] * 10, {3, 12})

    row = _row(health._check_pdf_stub_metadata())

    assert "PARTIAL" not in row.detail, row.detail
    assert "52 PDF chunk(s) in 2 document(s)" in row.detail, row.detail
    assert "doc3.pdf (2)" in row.detail and "doc12.pdf (50)" in row.detail, row.detail
    # 10 small documents whole (20) + 10 large x 4 probes (40) + the stub's 46 chunks the sample did not hold.
    assert len(col.requested) == len(set(col.requested)) == 20 + 40 + 46, len(col.requested)


def test_a_collection_too_large_to_sample_is_partial_and_says_why_and_what_to_run(monkeypatch) -> None:
    """100 documents at four probes each is 400 reads against a 200-chunk bound: the scan genuinely cannot
    finish, so it says so, with the reason and the way to shrink the set."""
    col = _legacy_collection(monkeypatch, [20] * 100, {0})

    row = _row(health._check_pdf_stub_metadata())

    assert not row.ok and row.warn, row.detail
    assert "PARTIAL" in row.detail and "floor" in row.detail, row.detail
    assert "method-less" in row.detail and "backfill-meta" in row.detail, row.detail
    assert "SAMPLED" not in row.detail, "the PARTIAL says it; the sampled note is not repeated"
    assert len(col.requested) <= _BOUND, "the bound holds even when it is reached"


def test_missing_t3_rows_cannot_overrun_the_bound_on_the_sampled_path(monkeypatch) -> None:
    """The bound counts ids asked for, not rows that came back. Ten healthy documents have no T3 rows at all
    (40 of the 84 probes return nothing); a 150-chunk stub document then has 146 chunks left, and only the
    116 the bound still allows are requested."""
    col = _legacy_collection(monkeypatch, [150] + [20] * 20, {0}, missing_docs=set(range(1, 11)))

    row = _row(health._check_pdf_stub_metadata())

    assert len(col.requested) == len(set(col.requested)) == _BOUND, len(col.requested)
    assert "PARTIAL" in row.detail and "doc0.pdf" in row.detail, row.detail
    # A placeholder document too big to read in full is fixed by re-indexing it, not by backfill-meta.
    partial = row.detail.split("PARTIAL:", 1)[1].split("SAMPLED:", 1)[0]
    assert "Re-index" in partial and "backfill-meta" not in partial, partial


def test_a_stub_only_in_an_unsampled_chunk_is_missed_and_the_output_says_it_was_a_sample(monkeypatch) -> None:
    """The known limit, pinned: chunk 1 of a 20-chunk document is not among the probes (0, 6, 13, 19)."""
    _legacy_collection(monkeypatch, [20] * 30, stub_chunks={(3, 1)})

    row = _row(health._check_pdf_stub_metadata())

    assert row.ok and "clean on a sample" in row.detail, row.detail


def test_a_placeholder_suffix_of_a_method_less_document_is_caught_through_the_last_probe(monkeypatch) -> None:
    """nexus-4z845, the scan side only. A method-less document whose placeholder sits on a suffix of its
    manifest, ending at the last chunk, is seen by the last probe and then read in full. That is the shape
    a post-pass leaves when the process dies between two of update_chunks' 300-id batches (they go in
    manifest order and stop at the first failure) before the catalog hook records a method. A post-pass
    that fails by exception is not this case: the hook still records extraction_method, so the scan skips
    the document, and it is the stranded-indexing row (index_state stays 'indexing' when the post-pass did
    not land) that names it, with the kept buffer retrying the post-pass on the next run. The fixture is
    scaled down (3 of 20 chunks, which no probe but the last, of 0, 6, 13 and 19, reaches); it exercises
    the scan, not the producer's batching. If the manifest's last chunk is another document's restamped
    row, the suffix ends before it and only an earlier probe can see it."""
    _legacy_collection(monkeypatch, [20] * 30, stub_chunks={(3, i) for i in range(17, 20)})

    row = _row(health._check_pdf_stub_metadata())

    assert not row.ok, row.detail
    assert "3 PDF chunk(s)" in row.detail and "/doc3.pdf (3)" in row.detail, row.detail


def test_empty_and_repeated_manifest_chashes_do_not_take_probe_slots(monkeypatch) -> None:
    """A manifest row with no chash, or a repeat of one, is not a chunk. Left in, they shift the spread:
    here chunk 6, which the four probes of a clean 20-chunk document reach, would be skipped."""
    col = _legacy_collection(monkeypatch, [20] * 30, stub_chunks={(7, 6)}, noisy_docs={7})

    row = _row(health._check_pdf_stub_metadata())

    assert not row.ok and "doc7.pdf (1)" in row.detail, row.detail
    assert "" not in col.requested, "an empty chash is never sent to the engine"


@pytest.mark.parametrize("n,k,picked", [
    (0, 4, []),
    (1, 4, [0]),
    (3, 4, [0, 1, 2]),
    (4, 4, [0, 1, 2, 3]),
    (5, 4, [0, 1, 3, 4]),
    (20, 4, [0, 6, 13, 19]),
])
def test_spread_sample(n, k, picked) -> None:
    items = [f"c{i}" for i in range(n)]
    assert health._spread_sample(items, k) == [items[i] for i in picked]


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
