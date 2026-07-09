# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""nexus-3l6gz: combined-query MCP tools drop a whole embedding-model group.

Root cause (see the bead / debugger report): ``search_graph_hop`` and
``search_metadata_scoped`` resolve a multi-prefix corpus (e.g. ``code,docs``)
to a FLAT collection list that can span two embedding models
(``voyage-code-3`` + ``voyage-context-3``, both 1024-dim) and pass the whole
list to the service in ONE call. The service
(``PgVectorRepository.searchGraphHopWithTokens``) guards only *dimension*
homogeneity (``requireHomogeneousDim``) and then embeds the query with
``collectionNames.get(0)``'s model only, using that single vector across BOTH
vector spaces. There is NO grouping by embedding model anywhere in the stack,
so a corpus spanning two models is queried in one model's space — the other
model's chunks are not correctly retrieved and the result collapses to empty.

The correct contract: group the resolved collections by embedding model, issue
ONE combined-query per model group, and merge — exactly what
``search_topic_scoped`` already does with its per-collection loop, and what
graph-hop / metadata-scoped do NOT.

These tests fail on the current (single mixed-model call) code and pass once
the tool groups by embedding model.
"""
from __future__ import annotations

from nexus.corpus import embedding_model_for_collection_name
from nexus.mcp import core

# Two conformant collections, SAME dimension (1024), DIFFERENT embedding model.
# This is the exact shape of the live repro: prefix `code` -> voyage-code-3,
# prefix `docs` -> voyage-context-3.
CODE_COL = "code__acme-1-1__voyage-code-3__v1"
DOCS_COL = "docs__acme-1-1__voyage-context-3__v1"


def _model(coll: str) -> str:
    return embedding_model_for_collection_name(coll) or coll


class _ModelAwareServiceT3:
    """Faithful stand-in for the service combined-query path.

    Mirrors ``PgVectorRepository`` semantics that the debugger confirmed by
    reading the Java source: the query is embedded ONCE with
    ``collection_names[0]``'s model and every collection's chunks are ranked
    against that single vector. A chunk is therefore only retrievable when the
    query is embedded in the chunk's OWN model space. We model that as: a call
    returns the seeded rows whose ``collection`` is in the requested set AND
    whose model matches ``model(collection_names[0])``. A call whose collection
    list spans two models thus retrieves only the first model's rows — the
    observed multi-model data loss.
    """

    def __init__(self, rows: list[dict]) -> None:
        self.rows = rows
        self.graph_calls: list[list[str]] = []
        self.meta_calls: list[list[str]] = []

    def _retrieve(self, collection_names: list[str]) -> list[dict]:
        if not collection_names:
            return []
        query_model = _model(collection_names[0])
        requested = set(collection_names)
        return [
            r for r in self.rows
            if r.get("collection") in requested
            and _model(r["collection"]) == query_model
        ]

    def search_graph_hop(self, query, seeds, collection_names, *, link_type=None,
                         depth=1, direction="both", where=None, n_results=10):
        self.graph_calls.append(list(collection_names))
        return self._retrieve(list(collection_names))

    def search_metadata_scoped(self, query, collection_names, *, content_type=None,
                               author=None, year=None, corpus=None, subtree=None,
                               where=None, n_results=10):
        self.meta_calls.append(list(collection_names))
        return self._retrieve(list(collection_names))


def _wire(monkeypatch, t3, target: list[str]) -> None:
    monkeypatch.setattr(core, "_get_t3", lambda: t3)
    monkeypatch.setattr(core, "_resolve_corpus_target", lambda corpus, t3: target)
    monkeypatch.setattr(
        "nexus.db.http_vector_client.is_service_backed", lambda db: True)


def _max_models_per_call(calls: list[list[str]]) -> int:
    return max((len({_model(c) for c in cols}) for cols in calls), default=0)


class TestSearchGraphHopMultiModel:
    def test_multimodel_corpus_does_not_drop_the_hit(self, monkeypatch):
        # Seed 1.9.80's reachable hit lives in the DOCS (voyage-context-3) group,
        # while `_resolve_corpus_target("code,docs")` puts the CODE
        # (voyage-code-3) collection first -> the single mixed-model call embeds
        # the query with voyage-code-3 and never retrieves the docs hit.
        hit = {"id": "1.9.80", "content": "reachable doc", "distance": 0.2,
               "collection": DOCS_COL, "chash": "a" * 32}
        t3 = _ModelAwareServiceT3([hit])
        _wire(monkeypatch, t3, [CODE_COL, DOCS_COL])

        out = core.search_graph_hop("q", ["1.9.80"], corpus="code,docs", depth=2)

        assert "1.9.80" in out, (
            "multi-model corpus dropped the reachable hit; "
            f"tool returned {out!r}")

    def test_never_issues_a_mixed_model_client_call(self, monkeypatch):
        # Contract (assumption-free): each combined-query call must be
        # embedding-model-homogeneous, because one query vector cannot serve two
        # vector spaces. The tool must split the resolved corpus by model.
        hit = {"id": "1.9.80", "content": "reachable doc", "distance": 0.2,
               "collection": CODE_COL, "chash": "a" * 32}
        t3 = _ModelAwareServiceT3([hit])
        _wire(monkeypatch, t3, [CODE_COL, DOCS_COL])

        core.search_graph_hop("q", ["1.9.80"], corpus="code,docs", depth=2)

        assert t3.graph_calls, "graph-hop must be called"
        assert _max_models_per_call(t3.graph_calls) == 1, (
            "search_graph_hop passed a mixed-embedding-model collection list to "
            f"the service in one call: {t3.graph_calls}")

    def test_hits_in_both_model_groups_survive_the_merge(self, monkeypatch):
        # Positive assertion (debugger-recommended): once each model group is
        # queried separately, a hit in EITHER group must survive the merge —
        # not just the group that happens to be collection_names[0].
        code_hit = {"id": "1.1.1", "content": "code hit", "distance": 0.5,
                    "collection": CODE_COL, "chash": "b" * 32}
        docs_hit = {"id": "1.2.2", "content": "docs hit", "distance": 0.1,
                    "collection": DOCS_COL, "chash": "c" * 32}
        t3 = _ModelAwareServiceT3([code_hit, docs_hit])
        _wire(monkeypatch, t3, [CODE_COL, DOCS_COL])

        out = core.search_graph_hop("q", ["1.9.80"], corpus="code,docs", depth=2,
                                    structured=True)

        assert set(out["ids"]) == {"1.1.1", "1.2.2"}, (
            f"both model-group hits must survive the merge; got {out['ids']!r}")
        assert out["distances"] == sorted(out["distances"]), (
            "graph-hop merge must be distance-ascending across model groups")


class TestSearchMetadataScopedMultiModel:
    """Sibling tool shares the defect: same single mixed-model call shape."""

    def test_multimodel_corpus_does_not_drop_the_hit(self, monkeypatch):
        hit = {"id": "1.9.80", "content": "reachable doc", "distance": 0.2,
               "collection": DOCS_COL, "chash": "a" * 32}
        t3 = _ModelAwareServiceT3([hit])
        _wire(monkeypatch, t3, [CODE_COL, DOCS_COL])

        out = core.search_metadata_scoped("q", corpus="code,docs")

        assert "1.9.80" in out, (
            "multi-model corpus dropped the hit; "
            f"tool returned {out!r}")

    def test_never_issues_a_mixed_model_client_call(self, monkeypatch):
        hit = {"id": "1.9.80", "content": "reachable doc", "distance": 0.2,
               "collection": CODE_COL, "chash": "a" * 32}
        t3 = _ModelAwareServiceT3([hit])
        _wire(monkeypatch, t3, [CODE_COL, DOCS_COL])

        core.search_metadata_scoped("q", corpus="code,docs")

        assert t3.meta_calls, "metadata-scoped must be called"
        assert _max_models_per_call(t3.meta_calls) == 1, (
            "search_metadata_scoped passed a mixed-embedding-model collection "
            f"list to the service in one call: {t3.meta_calls}")

    def test_hits_in_both_model_groups_survive_merge_distance_ascending(self, monkeypatch):
        # Positive assertion (debugger-recommended): hits in TWO model groups
        # both survive the merge, AND the merged order is globally
        # distance-ascending across groups (not just call-order-ascending) —
        # the weaker (0.5) code-group hit must NOT precede the stronger (0.1)
        # docs-group hit just because its group was queried first.
        code_hit = {"id": "1.1.1", "content": "code hit", "distance": 0.5,
                    "collection": CODE_COL, "chash": "b" * 32}
        docs_hit = {"id": "1.2.2", "content": "docs hit", "distance": 0.1,
                    "collection": DOCS_COL, "chash": "c" * 32}
        t3 = _ModelAwareServiceT3([code_hit, docs_hit])
        _wire(monkeypatch, t3, [CODE_COL, DOCS_COL])

        out = core.search_metadata_scoped("q", corpus="code,docs", structured=True)

        assert out["ids"] == ["1.2.2", "1.1.1"], (
            "both model-group hits must survive the merge, ordered "
            f"distance-ascending across groups; got {out['ids']!r}")
        assert out["distances"] == [0.1, 0.5]


# ═════════════════════════════════════════════════════════════════════════════
# nexus-hg745: query()'s service-mode catalog-routing branch shares the exact
# nexus-3l6gz defect at its own 3 raw call sites (metadata-scoped without
# follow_links, the follow_links-no-seeds metadata-scoped fallback, and
# graph-hop with resolved seeds) — reachable via
# query(corpus="all"/"code,docs", author=/content_type=/follow_links=/subtree=)
# in service mode. All 3 sites now route through the same
# _grouped_combined_query fix as the two standalone tools.
# ═════════════════════════════════════════════════════════════════════════════

def _wire_query(monkeypatch, t3, target: list[str]) -> None:
    """Minimal wiring for query()'s service-mode catalog branch.

    Mirrors tests/test_query_repoint.py's `_wire`, pared to what the
    service-mode branch touches when the test never falls into text-form
    re-hydration (structured=True) or subtree/follow_links seed resolution
    via the catalog object itself: _get_t3, _resolve_corpus_target,
    is_service_backed, and a non-None catalog stand-in (only its identity
    is checked — `if cat is None: return Error` — before this branch).
    """
    monkeypatch.setattr(core, "_get_t3", lambda: t3)
    monkeypatch.setattr(core, "_resolve_corpus_target", lambda corpus, _t3: target)
    monkeypatch.setattr(
        "nexus.db.http_vector_client.is_service_backed", lambda db: True)
    monkeypatch.setattr(core, "_get_catalog", lambda: object())


class TestQueryServiceModeMultiModel:
    def test_metadata_scoped_path_does_not_drop_the_hit(self, monkeypatch):
        # content_type (no follow_links) routes through the "else" branch's
        # single search_metadata_scoped call site (~1806).
        hit = {"id": "1.9.80", "content": "reachable doc", "distance": 0.2,
               "collection": DOCS_COL, "chash": "a" * 32}
        t3 = _ModelAwareServiceT3([hit])
        _wire_query(monkeypatch, t3, [CODE_COL, DOCS_COL])

        out = core.query("q", corpus="code,docs", content_type="paper", structured=True)

        assert "1.9.80" in out["ids"], (
            f"query()'s metadata-scoped branch dropped the multi-model hit; got {out!r}")

    def test_metadata_scoped_path_never_issues_a_mixed_model_call(self, monkeypatch):
        hit = {"id": "1.9.80", "content": "reachable doc", "distance": 0.2,
               "collection": CODE_COL, "chash": "a" * 32}
        t3 = _ModelAwareServiceT3([hit])
        _wire_query(monkeypatch, t3, [CODE_COL, DOCS_COL])

        core.query("q", corpus="code,docs", content_type="paper", structured=True)

        assert t3.meta_calls, "search_metadata_scoped must be called"
        assert _max_models_per_call(t3.meta_calls) == 1, (
            "query()'s metadata-scoped branch passed a mixed-embedding-model "
            f"collection list to the service in one call: {t3.meta_calls}")

    def test_graph_hop_path_with_seeds_does_not_drop_the_hit(self, monkeypatch):
        # follow_links + subtree resolves real seeds via cat.descendants() ->
        # the search_graph_hop call site (~1796). follow_links must be
        # truthy to enter this branch at all; subtree supplies the seeds.
        hit = {"id": "1.9.80", "content": "reachable doc", "distance": 0.2,
               "collection": DOCS_COL, "chash": "a" * 32}
        t3 = _ModelAwareServiceT3([hit])
        _wire_query(monkeypatch, t3, [CODE_COL, DOCS_COL])
        monkeypatch.setattr(
            core, "_get_catalog",
            lambda: type("C", (), {
                "descendants": lambda self, prefix: [{"tumbler": "1.9.80"}],
            })())

        out = core.query("q", corpus="code,docs", follow_links="cites",
                         subtree="1.9", structured=True)

        assert t3.graph_calls, "search_graph_hop must be called"
        assert "1.9.80" in out["ids"], (
            f"query()'s graph-hop branch dropped the multi-model hit; got {out!r}")
        assert _max_models_per_call(t3.graph_calls) == 1, (
            "query()'s graph-hop branch passed a mixed-embedding-model "
            f"collection list to the service in one call: {t3.graph_calls}")

    def test_follow_links_no_seeds_fallback_never_issues_a_mixed_model_call(self, monkeypatch):
        # follow_links with NO resolvable seeds falls through to the
        # metadata-scoped fallback call site (~1786) — the third raw site.
        hit = {"id": "1.9.80", "content": "reachable doc", "distance": 0.2,
               "collection": DOCS_COL, "chash": "a" * 32}
        t3 = _ModelAwareServiceT3([hit])
        _wire_query(monkeypatch, t3, [CODE_COL, DOCS_COL])
        monkeypatch.setattr(
            core, "_get_catalog",
            lambda: type("C", (), {"find": lambda self, q, content_type=None: []})())

        out = core.query("q", corpus="code,docs", follow_links="cites", structured=True)

        assert t3.graph_calls == [], "no seeds resolved -> graph_hop must NOT be called"
        assert t3.meta_calls, "the metadata-scoped fallback must be called"
        assert _max_models_per_call(t3.meta_calls) == 1, (
            "query()'s follow_links-no-seeds fallback passed a mixed-embedding-model "
            f"collection list to the service in one call: {t3.meta_calls}")
        assert "1.9.80" in out["ids"], (
            f"the fallback dropped the multi-model hit; got {out!r}")


# ═════════════════════════════════════════════════════════════════════════════
# Partial-group-failure semantics (substantive-critic Significant #1): a
# later model group's exception must abort the WHOLE tool call — no silent
# partial result set built only from groups that succeeded first. Matches
# search_topic_scoped's existing (uncaught) per-collection loop precedent.
# ═════════════════════════════════════════════════════════════════════════════

class _PartialFailureServiceT3:
    """First combined-query call succeeds; every call after it raises.

    With two model groups this means: first group's call returns real rows,
    second group's call raises -- used to prove the merge is all-or-nothing.
    """

    def __init__(self, first_call_rows: list[dict]) -> None:
        self.first_call_rows = first_call_rows
        self.calls = 0

    def search_metadata_scoped(self, query, collection_names, **kwargs):
        self.calls += 1
        if self.calls == 1:
            return self.first_call_rows
        raise RuntimeError("simulated second-model-group service failure")

    def search_graph_hop(self, query, seeds, collection_names, **kwargs):
        self.calls += 1
        if self.calls == 1:
            return self.first_call_rows
        raise RuntimeError("simulated second-model-group service failure")


class TestPartialGroupFailureIsAllOrNothing:
    def test_metadata_scoped_second_group_failure_returns_error_not_partial_rows(
        self, monkeypatch,
    ):
        first_group_hit = {"id": "1.1.1", "content": "ok", "distance": 0.1,
                            "collection": CODE_COL, "chash": "a" * 32}
        t3 = _PartialFailureServiceT3([first_group_hit])
        _wire(monkeypatch, t3, [CODE_COL, DOCS_COL])

        out = core.search_metadata_scoped("q", corpus="code,docs")

        assert isinstance(out, str) and out.startswith("Error:"), (
            "a later model group's failure must abort the whole call and "
            f"surface as an error, never a partial row set; got {out!r}")

    def test_graph_hop_second_group_failure_returns_error_not_partial_rows(
        self, monkeypatch,
    ):
        first_group_hit = {"id": "1.1.1", "content": "ok", "distance": 0.1,
                            "collection": CODE_COL, "chash": "a" * 32}
        t3 = _PartialFailureServiceT3([first_group_hit])
        _wire(monkeypatch, t3, [CODE_COL, DOCS_COL])

        out = core.search_graph_hop("q", ["1.9.80"], corpus="code,docs", depth=2)

        assert isinstance(out, str) and out.startswith("Error:"), (
            "a later model group's failure must abort the whole call and "
            f"surface as an error, never a partial row set; got {out!r}")
