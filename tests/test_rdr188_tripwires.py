# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""RDR-188 P2.4 (nexus-9o6y2.11) — cross-cutting End-State tripwires.

The invariant this file guards: **zero client Voyage consumption on the
search path.** These tests turned GREEN when .9/.19 deleted the client
rerank machinery and stay green as re-introduction guards — a future diff
that re-adds a client Voyage read, a client rerank call, or an invisible
degrade must go RED here before review ever sees it.

Scope note (R1): MCP search/query and nx_answer never reranked — no rerank
assertions for them, by design.
"""
from __future__ import annotations

import importlib
import json
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from click.testing import CliRunner

from nexus.cli import main
from nexus.db.http_vector_client import HttpVectorClient
from nexus.types import SearchResult

_SRC = Path(__file__).resolve().parents[1] / "src" / "nexus"

#: The search path: every module a `nx search` request flows through
#: client-side. NONE of them may import voyageai or reference the deleted
#: client-factory. (Category-(b) migration-source legacies — pipeline_stages,
#: doc_indexer, indexer, db/t3 etc. — are OUTSIDE the search path and die at
#: RDR-155 P4b, not here.)
_SEARCH_PATH_MODULES = [
    _SRC / "commands" / "search_cmd.py",
    _SRC / "search_engine.py",
    _SRC / "scoring.py",
    _SRC / "db" / "http_vector_client.py",
]

_CLOUD_ENV = {
    "CHROMA_API_KEY": "k", "VOYAGE_API_KEY": "v",
    "CHROMA_TENANT": "t", "CHROMA_DATABASE": "d",
}


# ── 1. Static: the deleted symbols stay deleted ─────────────────────────────


def test_client_rerank_symbols_are_gone():
    """.9/.19's deletions, pinned: re-adding any of these is a tripwire RED."""
    import nexus.db as db
    import nexus.scoring as scoring

    for name in ("rerank_results", "_rerank_cloud", "_rerank_local", "_RERANK_MODEL"):
        assert not hasattr(scoring, name), f"scoring.{name} re-introduced"
    for name in ("get_voyage_client", "_build_voyage_client",
                 "reset_voyage_client_cache_for_tests"):
        assert not hasattr(db, name), f"nexus.db.{name} re-introduced"


def test_search_path_sources_never_mention_voyage_client():
    """Source-level lint over the search path: no voyageai import, no client
    factory reference. Docstring/comment mentions of the RETIREMENT are fine —
    the lint targets code tokens, so it strips comment lines first."""
    offenders: list[str] = []
    for mod in _SEARCH_PATH_MODULES:
        code_lines = [
            ln for ln in mod.read_text().splitlines()
            if not ln.lstrip().startswith("#")
        ]
        code = "\n".join(code_lines)
        for token in ("import voyageai", "voyageai.Client", "get_voyage_client("):
            if token in code:
                offenders.append(f"{mod.name}: {token}")
    assert not offenders, (
        "client Voyage consumption re-introduced on the search path "
        f"(RDR-188 End State violated): {offenders}"
    )


# ── 2. Runtime: search succeeds with voyageai UNIMPORTABLE ──────────────────


class _VoyageImportTrap:
    """Meta-path finder that fails ANY voyageai import loudly."""

    def find_spec(self, fullname, path=None, target=None):
        if fullname == "voyageai" or fullname.startswith("voyageai."):
            raise ImportError(
                "RDR-188 tripwire: the client attempted to import voyageai on "
                "the search path — zero client Voyage consumption is the End State."
            )
        return None


def _result(id: str, collection: str, distance: float, score: float | None = None) -> SearchResult:
    md: dict = {"rerank_score": score} if score is not None else {}
    return SearchResult(id=id, content=f"c {id}", distance=distance,
                        collection=collection, metadata=md)


def _t3_mock(collections: list[str]) -> MagicMock:
    mock = MagicMock(spec=HttpVectorClient)
    mock.list_collections.return_value = [{"name": n} for n in collections]
    mock.supports_server_rerank = True
    return mock


@pytest.fixture
def cloud_env(monkeypatch):
    for k, v in _CLOUD_ENV.items():
        monkeypatch.setenv(k, v)
    monkeypatch.delenv("NX_EMBEDDINGS_RERANKER_MODEL", raising=False)


def test_search_reranks_with_voyageai_unimportable(cloud_env):
    """The credential tripwire, strongest form: a service-mode multi-collection
    search with rerank completes successfully while the voyageai package is
    UNIMPORTABLE — so no code path can construct a client even lazily. Any
    re-introduced client Voyage read on the search path raises the trap."""
    trap = _VoyageImportTrap()
    saved = sys.modules.pop("voyageai", None)
    sys.meta_path.insert(0, trap)
    try:
        results = [
            _result("far", "rdr__nexus", 0.4, score=0.9),
            _result("near", "knowledge__test", 0.1, score=0.2),
        ]

        def fake(q, cols, n_results, t3, where=None, *, rerank=False,
                 rerank_meta_out=None, **kw):
            return list(results)

        mock_t3 = _t3_mock(["knowledge__test", "rdr__nexus"])
        with patch("nexus.commands.search_cmd._t3", return_value=mock_t3), \
             patch("nexus.commands.search_cmd.search_cross_corpus", side_effect=fake), \
             patch("nexus.commands.search_cmd.load_config",
                   return_value={"embeddings": {"rerankerModel": "rerank-2.5"}}):
            res = CliRunner().invoke(
                main, ["search", "query", "--corpus", "knowledge,rdr", "--json"])

        assert res.exit_code == 0, res.output
        items = json.loads(res.stdout)
        assert [i["id"] for i in items] == ["far", "near"]  # server scores consumed
    finally:
        sys.meta_path.remove(trap)
        if saved is not None:
            sys.modules["voyageai"] = saved


def test_trap_itself_is_live():
    """Non-vacuity: the trap must actually fire on a voyageai import — a
    finder that silently returned None would make the test above vacuous."""
    trap = _VoyageImportTrap()
    saved = sys.modules.pop("voyageai", None)
    sys.meta_path.insert(0, trap)
    try:
        with pytest.raises(ImportError, match="RDR-188 tripwire"):
            importlib.import_module("voyageai")
    finally:
        sys.meta_path.remove(trap)
        if saved is not None:
            sys.modules["voyageai"] = saved


# ── 3. Score-order parity vs the retired client baseline ───────────────────


def test_server_score_order_matches_retired_client_ordering(cloud_env):
    """Parity pin: the retired client reranker ordered by relevance descending,
    wrote the relevance into hybrid_score, and truncated to top_k=n. The
    server-score consumption path must produce the IDENTICAL ordering and
    score mutation for the same relevance values on a fixed fixture."""
    relevance = {"a": 0.31, "b": 0.87, "c": 0.55, "d": 0.12}
    results = [
        _result("a", "knowledge__x", 0.10, score=relevance["a"]),
        _result("b", "rdr__x", 0.20, score=relevance["b"]),
        _result("c", "knowledge__x", 0.30, score=relevance["c"]),
        _result("d", "docs__x", 0.40, score=relevance["d"]),
    ]
    # The retired _rerank_cloud contract, computed analytically:
    old_order = sorted(relevance, key=relevance.__getitem__, reverse=True)[:3]

    def fake(q, cols, n_results, t3, where=None, **kw):
        return list(results)

    mock_t3 = _t3_mock(["knowledge__x", "rdr__x", "docs__x"])
    with patch("nexus.commands.search_cmd._t3", return_value=mock_t3), \
         patch("nexus.commands.search_cmd.search_cross_corpus", side_effect=fake), \
         patch("nexus.commands.search_cmd.load_config",
               return_value={"embeddings": {"rerankerModel": "rerank-2.5"}}):
        res = CliRunner().invoke(
            main, ["search", "query", "--corpus", "knowledge,rdr,docs",
                   "-m", "3", "--json"])

    assert res.exit_code == 0, res.output
    items = json.loads(res.stdout)
    assert [i["id"] for i in items] == old_order
    # The server relevance rides each row (metadata spread) — same signal the
    # retired client wrote into hybrid_score before ordering.
    for item in items:
        assert item["rerank_score"] == pytest.approx(relevance[item["id"]])


# ── 4. Degrade surfaced — the cross-cutting invariant copy ──────────────────


def test_degrade_surface_invariant(cloud_env):
    """Gap 2's invariant owned by this suite (independent of the .8 unit
    tests): a degraded server rerank is USER-VISIBLE, never silent."""
    def fake(q, cols, n_results, t3, where=None, *, rerank=False,
             rerank_meta_out=None, **kw):
        if rerank and rerank_meta_out is not None:
            rerank_meta_out["knowledge__test"] = {
                "degraded": True, "error": "engine says no"}
        return [_result("a", "knowledge__test", 0.1)]

    mock_t3 = _t3_mock(["knowledge__test", "rdr__nexus"])
    with patch("nexus.commands.search_cmd._t3", return_value=mock_t3), \
         patch("nexus.commands.search_cmd.search_cross_corpus", side_effect=fake), \
         patch("nexus.commands.search_cmd.load_config",
               return_value={"embeddings": {"rerankerModel": "rerank-2.5"}}):
        res = CliRunner().invoke(main, ["search", "query", "--corpus", "knowledge,rdr"])

    assert res.exit_code == 0, res.output
    assert "server rerank degraded" in res.output
    assert "engine says no" in res.output
