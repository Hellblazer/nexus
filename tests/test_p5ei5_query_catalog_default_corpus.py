# SPDX-License-Identifier: AGPL-3.0-or-later
"""query(content_type=/subtree=) with the default corpus must reach every corpus.

nexus-p5ei5. The 7.64.1 shakeout: ``query(question=..., content_type="rdr")``
answered "No documents found matching catalog filters", and the same call
with ``corpus="rdr"`` returned rdr-127 and others. The ``corpus`` parameter
said catalog params override it; the code resolved the default
``"knowledge"`` anyway, so the catalog-filtered SQL only ever saw
knowledge__* collections, and the message blamed the catalog. ``subtree``
had the same defect (surface A, A7).

Earlier tests stubbed ``_resolve_corpus_target`` out, so the default corpus
never took part. These resolve it for real against a live-shaped
collection list.
"""
from __future__ import annotations

import pytest

from nexus.mcp import core

KNOWLEDGE_COL = "knowledge__notes__voyage-context-3__v1"
RDR_COL = "rdr__nexus-1-1__voyage-context-3__v1"
CODE_COL = "code__nexus-1-1__voyage-code-3__v1"
RDR_HIT = {"id": "1.1.4830", "content": "RDR-211 tuple claim lease",
           "distance": 0.3, "collection": RDR_COL, "chash": "a" * 64}


class _CatalogFilteringT3:
    """The combined-query SQL: returns the catalog-matching row only when
    its collection is among those asked for."""

    def __init__(self) -> None:
        self.meta_calls: list[list[str]] = []

    def search_metadata_scoped(self, query, collection_names, *, content_type=None,
                               author=None, year=None, corpus=None, subtree=None,
                               where=None, n_results=10):
        self.meta_calls.append(list(collection_names))
        return [RDR_HIT] if RDR_COL in collection_names else []


@pytest.fixture
def t3(monkeypatch) -> _CatalogFilteringT3:
    fake = _CatalogFilteringT3()
    monkeypatch.setattr(core, "_get_t3", lambda: fake)
    names = [KNOWLEDGE_COL, RDR_COL, CODE_COL]
    monkeypatch.setattr(core, "_get_collection_names", lambda: names)
    monkeypatch.setattr(core, "_get_collection_counts", lambda: {n: 100 for n in names})
    def _row(n: str) -> dict:
        content_type, owner_id, model, version = n.split("__")
        return {"name": n, "content_type": content_type, "owner_id": owner_id,
                "embedding_model": model, "model_version": version,
                "lifecycle_state": "live"}

    rows = {n: _row(n) for n in names}
    monkeypatch.setattr("nexus.mcp_infra.get_collection_row",
                        lambda name, refresh=False: rows.get(name))
    monkeypatch.setattr("nexus.db.http_vector_client.is_service_backed", lambda db: True)

    class _Cat:
        def get_owner_by_prefix(self, prefix):
            return {"tumbler_prefix": prefix}

    monkeypatch.setattr(core, "_get_catalog", lambda: _Cat())
    return fake


def test_content_type_with_default_corpus_finds_the_rdr(t3) -> None:
    out = core.query("tuple claim lease renewal design", content_type="rdr", structured=True)
    assert out["ids"] == ["1.1.4830"], (out, t3.meta_calls)


def test_subtree_with_default_corpus_finds_the_rdr(t3) -> None:
    out = core.query("tuple claim lease renewal design", subtree="1.1", structured=True)
    assert out["ids"] == ["1.1.4830"], (out, t3.meta_calls)


def test_an_explicit_corpus_still_narrows(t3) -> None:
    out = core.query("q", content_type="rdr", corpus="knowledge")
    assert "No documents found" in out
    assert "in corpus 'knowledge'" in out, "the empty message must name what was searched"


def test_a_corpus_named_content_type_narrows_the_default_to_that_corpus(t3) -> None:
    """Critique finding: every catalog-param query paid for all ~111
    collections. content_type="rdr" names its corpus; query only that."""
    core.query("q", content_type="rdr", structured=True)
    assert t3.meta_calls == [[RDR_COL]]


def test_follow_links_keeps_every_corpus_even_with_a_corpus_named_content_type(t3) -> None:
    """Review finding on a463fa4ce: content_type filters the graph hop's
    SEEDS; its targets can live in any corpus (code cites an RDR). Narrowing
    the default corpus to content_type there drops them silently."""
    seen: list[list[str]] = []

    def search_graph_hop(query, seeds, collection_names, **kw):
        seen.append(list(collection_names))
        return []

    t3.search_graph_hop = search_graph_hop

    class _SeedCatalog:
        def get_owner_by_prefix(self, prefix):
            return {"tumbler_prefix": prefix}

        def by_content_type(self, content_type):
            from types import SimpleNamespace
            return [SimpleNamespace(tumbler="1.1.7")]

    import nexus.mcp.core as _core
    _core_get = _core._get_catalog
    try:
        _core._get_catalog = lambda: _SeedCatalog()
        core.query("q", content_type="code", follow_links="cites", structured=True)
    finally:
        _core._get_catalog = _core_get
    assert RDR_COL in {c for call in seen for c in call}, seen
