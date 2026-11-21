# SPDX-License-Identifier: AGPL-3.0-or-later
"""nexus-6pfgb.12 (RDR-236 Phase 2): ``query``'s document card and the quality boost read bibliographic
fields from the OWNING documents, not from the shared chunk row.

A chunk row is one per chunk text, so its ``bib_*`` keys are the last writer's: two documents sharing a
chunk, one enriched and one not, used to give the unenriched one the enriched one's citations (a boost it
could never clear, since a clean re-index does not touch ``bib_*``) and a card mixing its own title with the
other's bib line. ``search_engine._attach_display_paths`` resolves the page's owners once (the lookup the
title and the provenance line already ride) and stamps what the readers need; the boost and the card read
only that. The catalog is a stub here, with the row deliberately stale in every case.
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest

import nexus.mcp_infra as mi
import nexus.search_engine as se
from nexus.context_annotations import (
    DOC_ID_INFERRED_KEY,
    OWNER_BIB_KEY,
    OWNER_CHUNK_COUNT_KEY,
    OWNER_QUALITY_KEY,
    entry_facts,
)
from nexus.mcp import core
from nexus.scoring import apply_quality_boost, quality_score
from nexus.search_engine import _attach_display_paths
from nexus.types import SearchResult

COLL = "knowledge__papers__m__v1"
A, B = "1.1.1", "1.1.2"
H_SHARED, H_A, H_B = "5" * 64, "a" * 64, "b" * 64

#: What the LAST writer left on every row: citations, year, authors, venue nobody here owns.
STALE_ROW = {"bib_citation_count": 500, "bib_year": 1999, "bib_authors": "Row Author", "bib_venue": "RowVenue"}


def _entry(tumbler: str, title: str, *, year: int = 0, authors: str = "", venue: str = "", cites: int = 0,
           collection: str = COLL) -> SimpleNamespace:
    return SimpleNamespace(
        tumbler=tumbler, title=title, file_path=f"/papers/{title}.pdf", physical_collection=collection,
        indexed_at="2026-10-01T00:00:00+00:00", bib_year=year, year=0, bib_authors=authors, bib_venue=venue,
        bib_citation_count=cites, author="", meta={},
    )


class _Catalog:
    """What ``_attach_display_paths`` and ``query`` ask of the catalog, with the calls counted."""

    def __init__(self, entries: dict[str, SimpleNamespace]) -> None:
        self.entries = entries
        self.resolve_many_calls: list[list[str]] = []
        self.docs_for_chashes_calls = 0
        self.get_manifests_calls = 0

    def resolve_many(self, tumblers):
        self.resolve_many_calls.append(list(tumblers))
        return {t: self.entries[t] for t in tumblers if t in self.entries}

    def docs_for_chashes(self, chashes):
        self.docs_for_chashes_calls += 1
        return {}

    def get_manifests(self, doc_ids):
        self.get_manifests_calls += 1
        return {}


def _hit(rid: str, chash: str, doc_id: str, owners: list[str], *, inferred: bool = False, dist: float = 0.2,
         **extra) -> SearchResult:
    meta = {"chunk_text_hash": chash, "doc_id": doc_id, "_owner_doc_ids": owners, "chunk_count": 3,
            **STALE_ROW, **extra}
    if inferred:
        meta[DOC_ID_INFERRED_KEY] = True
    return SearchResult(id=rid, content=f"text of {rid}", distance=dist, collection=COLL, metadata=meta)


@pytest.fixture
def papers():
    """A enriched (3 citations, 2024), B not: both rows say 500 citations and 1999."""
    return _Catalog({
        A: _entry(A, "Paper A", year=2024, authors="Alice", venue="NeurIPS", cites=3),
        B: _entry(B, "Paper B"),
    })


# ── the stamps: one lookup per page, the attributed owner's own values ────────────────────────


def test_each_hit_is_stamped_with_its_attributed_owners_bib_from_the_pages_one_lookup(papers) -> None:
    hits = [
        _hit("a1", H_A, A, [A]),
        _hit("b1", H_B, B, [B]),
        _hit("shared-as-b", H_SHARED, B, [A, B]),  # the row names B (the last writer)
        _hit("shared-as-a", H_SHARED, A, [A, B]),  # a legacy row that names A
    ]
    _attach_display_paths(hits, papers)

    a1, b1, shared_b, shared_a = hits
    assert a1.metadata[OWNER_BIB_KEY] == {"bib_year": 2024, "bib_authors": "Alice", "bib_venue": "NeurIPS", "bib_citation_count": 3}
    assert a1.metadata[OWNER_QUALITY_KEY] == {"bib_citation_count": 3, "bib_year": 2024}
    for unenriched in (b1, shared_b):
        assert OWNER_BIB_KEY not in unenriched.metadata, "B states no bib: the row's is not B's"
        assert OWNER_QUALITY_KEY not in unenriched.metadata
    assert shared_a.metadata[OWNER_BIB_KEY]["bib_citation_count"] == 3
    assert len(papers.resolve_many_calls) == 1, "the page resolves its owners once, for title, provenance and bib alike"
    assert papers.docs_for_chashes_calls == 0


def test_a_hit_with_no_row_doc_id_takes_the_best_of_its_owners_for_the_boost_and_the_chosen_owner_for_the_card(papers) -> None:
    """The chosen document is arbitrary (the engine's order), so the boost, like frecency, takes the best
    its live owners state rather than penalise an enriched owner; the card shows the chosen document's own."""
    papers.entries[B] = _entry(B, "Paper B", year=1999, cites=500)
    hit = _hit("shared", H_SHARED, A, [A, B], inferred=True)
    _attach_display_paths([hit], papers)
    assert hit.metadata[OWNER_QUALITY_KEY] == {"bib_citation_count": 500, "bib_year": 1999}
    assert hit.metadata[OWNER_BIB_KEY]["bib_citation_count"] == 3, "the card is the chosen document's own"


def test_an_owner_in_another_collection_is_not_this_hits_owner(papers) -> None:
    papers.entries[A] = _entry(A, "Paper A", year=2024, cites=3, collection="docs__other__m__v1")
    hit = _hit("a1", H_A, A, [A])
    _attach_display_paths([hit], papers)
    assert OWNER_BIB_KEY not in hit.metadata and OWNER_QUALITY_KEY not in hit.metadata


def test_bib_year_falls_back_to_the_catalog_year_like_the_catalog_branch() -> None:
    entry = _entry(A, "Paper A", cites=3)
    entry.year = 2019
    assert entry_facts(entry)["bib_year"] == 2019
    hit = _hit("a1", H_A, A, [A])
    _attach_display_paths([hit], _Catalog({A: entry}))
    assert hit.metadata[OWNER_QUALITY_KEY] == {"bib_citation_count": 3, "bib_year": 2019}


# ── the boost: the owner's citations, never the row's ─────────────────────────────────────────


def _scored(rid: str, base: float, **meta) -> SearchResult:
    r = SearchResult(id=rid, content=rid, distance=0.2, collection=COLL, metadata=dict(meta))
    r.hybrid_score = base
    return r


def test_the_boost_ignores_the_rows_citations() -> None:
    r = _scored("row-only", 0.5, **STALE_ROW)
    apply_quality_boost([r])
    assert r.hybrid_score == 0.5, "a row's 500 citations belong to its last writer, not to this hit"


def test_the_boost_uses_the_owners_citations_and_year_and_orders_hits_by_them() -> None:
    modest = _scored("modest", 0.5, **STALE_ROW, **{OWNER_QUALITY_KEY: {"bib_citation_count": 3, "bib_year": 2024}})
    famous = _scored("famous", 0.5, bib_citation_count=0, **{OWNER_QUALITY_KEY: {"bib_citation_count": 5000, "bib_year": 2024}})
    out = apply_quality_boost([modest, famous])
    from datetime import date

    age = (date.today() - date(2024, 6, 15)).days
    assert modest.hybrid_score == pytest.approx(0.5 + 0.1 * quality_score(3, age_days=age))
    assert famous.hybrid_score == pytest.approx(0.5 + 0.1 * quality_score(5000, age_days=age))
    assert [r.id for r in sorted(out, key=lambda r: r.hybrid_score, reverse=True)] == ["famous", "modest"], (
        "ordered by the owners' counts; by the rows' (500 vs 0) it would be the reverse"
    )


def test_the_boost_stays_off_code_collections_whatever_the_owner_says() -> None:
    r = SearchResult(id="c", content="c", distance=0.2, collection="code__repo__m__v1",
                     metadata={OWNER_QUALITY_KEY: {"bib_citation_count": 5000, "bib_year": 2024}})
    r.hybrid_score = 0.7
    apply_quality_boost([r])
    assert r.hybrid_score == 0.7


# ── the owner's chunk count: a legacy row's chunk_count never beats the manifest (Phase 2 review H1) ──


class _PositionsCatalog:
    """The engine's chash-positions route: A's manifest has 7 rows."""

    def chash_positions(self, chashes):
        return [{"chash": c, "doc_id": A, "position": 2, "chunk_count": 7} for c in chashes]


class _ManifestOnlyCatalog:
    """An engine without the route: the reverse lookup and the manifest give A 7 rows."""

    chash_positions = None

    def docs_for_chashes(self, chashes):
        return {c: [A] for c in chashes}

    def get_manifests(self, doc_ids):
        return {d: [{"chash": H_A if i == 2 else f"{i:064d}", "position": i} for i in range(7)] for d in doc_ids}


@pytest.mark.parametrize("catalog", [_PositionsCatalog(), _ManifestOnlyCatalog()], ids=["chash_positions", "manifest"])
def test_a_stale_row_chunk_count_never_beats_the_owners_manifest_count(catalog) -> None:
    stale = SearchResult(id="s", content="x", distance=0.2, collection=COLL,
                         metadata={"chunk_text_hash": H_A, "chunk_count": 99})
    bare = SearchResult(id="b", content="x", distance=0.2, collection=COLL, metadata={"chunk_text_hash": H_A})
    se._attach_doc_ids_from_catalog([stale, bare], catalog)
    assert stale.metadata[OWNER_CHUNK_COUNT_KEY] == 7
    assert bare.metadata[OWNER_CHUNK_COUNT_KEY] == 7
    assert stale.metadata["chunk_count"] == 99, "the row's own key keeps its setdefault semantics (the scorer reads it)"
    assert bare.metadata["chunk_count"] == 7


# ── query's card, end to end through the real attach and the real boosts ─────────────────────


def _wire_query(monkeypatch, cat: _Catalog, hits: list[SearchResult]) -> None:
    monkeypatch.setattr(core, "_get_t3", lambda: object())
    monkeypatch.setattr(core, "_resolve_corpus_target", lambda corpus, _t3, **_kw: [COLL])
    monkeypatch.setattr(core, "_get_catalog", lambda: cat)
    monkeypatch.setattr(core, "_get_collection_names", lambda: [COLL])
    monkeypatch.setattr(core, "_search_taxonomy", lambda: None)
    monkeypatch.setattr(
        mi, "get_collection_row",
        lambda name: {"content_type": "knowledge", "owner_id": "papers", "embedding_model": "x", "lifecycle_state": "live"},
    )

    def fake_cross_corpus(*_a, catalog=None, **_kw):
        # the real step that stamps the owners' facts, as search_cross_corpus runs it last
        _attach_display_paths(hits, catalog)
        return hits

    monkeypatch.setattr(se, "search_cross_corpus", fake_cross_corpus)


def _card(text: str, title: str) -> list[str]:
    lines = text.splitlines()
    start = next(i for i, ln in enumerate(lines) if title in ln and ln.lstrip()[:1].isdigit())
    end = next((i for i in range(start + 1, len(lines)) if lines[i][:1].isdigit()), len(lines))
    return lines[start:end]


def test_the_card_shows_each_documents_own_bib_never_the_rows_and_makes_no_extra_catalog_call(monkeypatch, papers) -> None:
    hits = [
        _hit("b1", H_B, B, [B], dist=0.10),
        _hit("a1", H_A, A, [A], dist=0.15),
        _hit("shared", H_SHARED, B, [A, B], dist=0.30),  # the shared chunk, the row naming B
    ]
    _wire_query(monkeypatch, papers, hits)

    text = core.query("a question", corpus=COLL, limit=10)

    card_a, card_b = _card(text, "Paper A"), _card(text, "Paper B")
    assert any("2024 · Alice · NeurIPS · 3 citations" in ln for ln in card_a), card_a
    assert not any("citations" in ln or "Alice" in ln for ln in card_b), f"B states no bib; got {card_b}"
    assert "Row Author" not in text and "500 citations" not in text and "1999" not in text, text
    assert papers.docs_for_chashes_calls == 0, "the owners were stamped on the hits already: no second chash lookup"
    assert papers.get_manifests_calls == 0, "chunk counts ride the same stamps"
    assert len(papers.resolve_many_calls) == 1, "one owner lookup for the whole page"
    assert any("3 chunks" in ln for ln in card_a), "the chunk count still reads from the hit's stamp"


def test_the_card_groups_hits_by_the_stamped_document_not_a_second_lookup(monkeypatch, papers) -> None:
    hits = [_hit("a1", H_A, A, [A], dist=0.1), _hit("a2", "c" * 64, A, [A], dist=0.2), _hit("b1", H_B, B, [B], dist=0.3)]
    _wire_query(monkeypatch, papers, hits)
    text = core.query("another question", corpus=COLL, limit=10)
    assert text.count("Paper A") == 1 and text.count("Paper B") == 1
    assert "Found 2 documents" in text


# ── the catalog-parameter branch: the same card, one batch ─────────────────────────────────────


class _SvcT3:
    def __init__(self, rows):
        self.rows = rows

    def search_metadata_scoped(self, *a, **kw):
        return self.rows


def test_the_catalog_branch_batches_its_per_row_resolve_and_manifest(monkeypatch) -> None:
    from nexus.catalog.tumbler import Tumbler

    entries = {
        "1.2.3": SimpleNamespace(tumbler=Tumbler.parse("1.2.3"), title="Doc One", author="", year=0, physical_collection=COLL,
                                 chunk_count=2, bib_year=2023, bib_authors="Smith", bib_venue="ICML", bib_citation_count=7,
                                 indexed_at="", index_state=None),
        "1.2.4": SimpleNamespace(tumbler=Tumbler.parse("1.2.4"), title="Doc Two", author="", year=0, physical_collection=COLL,
                                 chunk_count=2, bib_year=0, bib_authors="", bib_venue="", bib_citation_count=0,
                                 indexed_at="", index_state=None),
    }

    class _Cat(_Catalog):
        def __init__(self):
            super().__init__(entries)
            self.resolve_calls = 0
            self.get_manifest_calls = 0
            self.manifest_batches: list[list[str]] = []

        def resolve(self, tumbler):  # the per-row form this branch used to call once per row
            self.resolve_calls += 1
            return entries.get(str(tumbler))

        def get_manifest(self, doc_id):
            self.get_manifest_calls += 1
            return [object()] * 4

        def get_manifests(self, doc_ids):
            self.manifest_batches.append(list(doc_ids))
            return {d: [object()] * 4 for d in doc_ids}

    cat = _Cat()
    rows = [
        {"id": "1.2.3", "content": "one", "distance": 0.1, "collection": COLL, "chash": "a" * 64},
        {"id": "1.2.4", "content": "two", "distance": 0.2, "collection": COLL, "chash": "b" * 64},
    ]
    monkeypatch.setattr(core, "_get_t3", lambda: _SvcT3(rows))
    monkeypatch.setattr(core, "_resolve_corpus_target", lambda corpus, _t3, **_kw: [COLL])
    monkeypatch.setattr(core, "_get_catalog", lambda: cat)
    monkeypatch.setattr(core, "_get_collection_names", lambda: [COLL])
    monkeypatch.setattr(core, "collection_model", lambda name: "m")  # the registry read, which needs a tenant
    monkeypatch.setattr("nexus.db.http_vector_client.is_service_backed", lambda db: True)

    text = core.query("q", author="Smith", structured=False)

    assert "2023 · Smith · ICML · 7 citations" in text and "[4 chunks]" in text
    assert (cat.resolve_calls, cat.get_manifest_calls) == (0, 0), "no per-row round trip"
    assert len(cat.resolve_many_calls) == 1 and cat.manifest_batches == [["1.2.3", "1.2.4"]]
