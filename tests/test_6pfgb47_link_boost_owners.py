# SPDX-License-Identifier: AGPL-3.0-or-later
"""nexus-6pfgb.47 (RDR-236 Phase 2 follow-up): the link boost counts only owners in the hit's collection.

``_owner_doc_ids`` is stamped tenant-wide (a chash is a function of text alone, so two documents in
different collections that share a chunk both name it). ``apply_link_boost`` runs before the display
lookup's own collection filter, so a hit in collection X was boosted by the links of an identical-text
document in collection Y. The boost now takes the page's one display lookup (the same ``homes`` the title,
frecency, bibliography and agent readers filter on) and drops an owner known to live elsewhere.
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from nexus.scoring import apply_link_boost
from nexus.search_engine import search_cross_corpus
from nexus.types import SearchResult

COL_X = "knowledge__x__m__v1"
COL_Y = "knowledge__y__m__v1"
X_DOC, Y_DOC = "1.2.1", "1.1.1"  # Y sorts first in the engine's order, so the hit's doc_id is Y's
H = "7" * 64


class _FakeT3:
    _voyage_client = None

    def search(self, query, collection_names, n_results=10, where=None):
        return [{"id": "hit", "content": "shared text", "distance": 0.2, "chunk_text_hash": H}]


class _Catalog:
    def __init__(self, links: dict[str, list[str]]) -> None:
        self._links = links
        self.links_calls: list[list[str]] = []
        self.resolve_calls = 0

    def chash_positions(self, chashes):
        return [
            {"chash": H, "doc_id": Y_DOC, "position": 0, "chunk_count": 1},
            {"chash": H, "doc_id": X_DOC, "position": 0, "chunk_count": 1},
        ]

    def resolve_many(self, doc_ids):
        self.resolve_calls += 1
        homes = {X_DOC: COL_X, Y_DOC: COL_Y}
        return {
            d: SimpleNamespace(file_path=f"/{d}.md", title=f"Doc {d}", physical_collection=homes[d])
            for d in doc_ids if d in homes
        }

    def links_from_batch(self, doc_ids):
        self.links_calls.append(list(doc_ids))
        return {d: [{"link_type": t} for t in self._links[d]] for d in doc_ids if d in self._links}


def _search(catalog: _Catalog) -> SearchResult:
    out = search_cross_corpus(
        "q", [COL_X], n_results=1, t3=_FakeT3(), catalog=catalog, link_boost=True,
    )
    assert len(out) == 1
    return out[0]


def _baseline() -> float:
    return _search(_Catalog({})).hybrid_score


def test_a_link_on_an_identical_text_document_in_another_collection_does_not_boost_the_hit() -> None:
    base = _baseline()
    cat = _Catalog({Y_DOC: ["implements"]})

    hit = _search(cat)

    assert hit.hybrid_score == pytest.approx(base)
    assert cat.links_calls, "the link lookup must still run (non-vacuity: Y's links were fetched and refused)"


def test_a_link_on_the_hits_own_collection_owner_still_boosts_it() -> None:
    base = _baseline()

    hit = _search(_Catalog({X_DOC: ["implements"]}))

    assert hit.hybrid_score == pytest.approx(base + 0.15)


def test_the_page_still_makes_one_link_call_and_one_display_lookup() -> None:
    cat = _Catalog({X_DOC: ["implements"]})

    _search(cat)

    assert len(cat.links_calls) == 1
    assert cat.resolve_calls == 1


def test_an_owner_the_lookup_did_not_resolve_keeps_its_links() -> None:
    """A dropped lookup (best-effort) must not silently turn the link boost off."""
    r = SearchResult(
        id="r", content="t", distance=0.2, collection=COL_X, hybrid_score=0.5,
        metadata={"doc_id": "9.9.9", "_owner_doc_ids": ["9.9.9"]},
    )
    cat = _Catalog({"9.9.9": ["implements"]})

    apply_link_boost([r], cat, homes=lambda: {})

    assert r.hybrid_score == pytest.approx(0.65)
