# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""nexus-ann50: the MCP search tool caps per_collection_k at 40 on the per-collection route.

Measured 2026-10-09 against the managed service (T2 nexus/ann50-k-sweep-2026-10-09): at k=40 the
pool's 30 nearest rows by raw distance matched k=300 on all 8 queries, and an MCP search took
2.41 s median against 2.99 s at the default 60/120. The cap never goes below n_results, applies
only without server rerank (a reranked page is pool-sensitive, nexus-abdp2), and the CLI keeps
the default. The real ``search_cross_corpus`` runs against the route fake of
tests/test_search_per_collection_route.py.
"""
from __future__ import annotations

import inspect

import pytest

import nexus.db.http_vector_client as hvc
import nexus.search_engine as se
from nexus.commands import search_cmd
from nexus.mcp import core
from nexus.search_engine import search_cross_corpus

from tests.test_search_per_collection_route import (  # type: ignore[import-not-found]
    _BGE,
    _FakeEngine,
    _cols,
    _rows,
)


@pytest.fixture(autouse=True)
def _fresh_process_state(monkeypatch):
    monkeypatch.setattr(se, "_mixed_model_groups", {})
    monkeypatch.setattr(hvc, "_per_collection_absent_warned", False)
    monkeypatch.delenv(hvc.PER_COLLECTION_ROUTE_ENV, raising=False)


def _k_sent(monkeypatch, cols: list[str], n: int, **kw) -> int:
    engine = _FakeEngine(monkeypatch, {c: _rows("r", 3, 0.2) for c in cols})
    search_cross_corpus("q", cols, n, engine.client, threshold_override=float("inf"),
                        cluster_by=None, **kw)
    (call,) = engine.route_calls()
    return call["per_collection_k"]


class TestTheCap:
    def test_lowers_the_mcp_page_one_k_to_the_cap(self, monkeypatch):
        docs = _cols("docs", _BGE, 2)          # x4: n=30 -> 120 by default
        assert _k_sent(monkeypatch, docs, 30) == 120, "control: the default this lowers"
        assert _k_sent(monkeypatch, docs, 30, per_collection_k_cap=40) == 40
        code = _cols("code", _BGE, 2)          # x2: n=30 -> 60
        assert _k_sent(monkeypatch, code, 30, per_collection_k_cap=40) == 40

    def test_never_below_n_results(self, monkeypatch):
        # A deep page (offset 100 -> fetch_n 130) still asks each collection for 130.
        assert _k_sent(monkeypatch, _cols("docs", _BGE, 2), 130, per_collection_k_cap=40) == 130

    def test_never_raises_k(self, monkeypatch):
        # n=5 on code: default max(5, 10) = 10, below the cap.
        assert _k_sent(monkeypatch, _cols("code", _BGE, 2), 5, per_collection_k_cap=40) == 10

    def test_absent_cap_leaves_the_request_unchanged(self, monkeypatch):
        assert _k_sent(monkeypatch, _cols("docs", _BGE, 2), 30, per_collection_k_cap=None) == 120


class TestTheMcpSearchTool:
    def _seen_caps(self, monkeypatch, **search_kw) -> list:
        seen: list = []

        def fake_search(query, target, **kw):
            seen.append(kw.get("per_collection_k_cap", "absent"))
            return []

        class _T3:
            supports_server_rerank = True

        monkeypatch.setattr(core, "_get_t3", lambda: _T3())
        monkeypatch.setattr(core, "_resolve_corpus_target", lambda corpus, t3, **kw: ["knowledge__a"])
        monkeypatch.setattr("nexus.search_engine.search_cross_corpus", fake_search)
        monkeypatch.setattr(core, "_search_taxonomy", lambda: None)
        core._page_cache_invalidate()
        try:
            core.search(query="anything", corpus="knowledge", **search_kw)
        finally:
            core._page_cache_invalidate()
        return seen

    def test_default_search_passes_the_cap(self, monkeypatch):
        assert self._seen_caps(monkeypatch) == [core._MCP_PER_COLLECTION_K]
        assert core._MCP_PER_COLLECTION_K == 40

    def test_a_reranked_search_keeps_the_full_k(self, monkeypatch):
        assert self._seen_caps(monkeypatch, lexical=True) == [None]


def test_the_cli_keeps_the_default_k():
    assert "per_collection_k_cap" not in inspect.getsource(search_cmd)
