# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""RDR-188 P2.1 (nexus-9o6y2.8) — `nx search` repoints reranking to the server.

The client no longer calls Voyage: the per-collection fan-out carries
``rerank=true`` (P1.2 fused-stage fields), the client consumes server
``rerank_score``s, and — the Gap-2 contract — the server's structured
degraded-rerank state is SURFACED to the user on stderr, never WARN-only
invisible. Also covers nexus-7jvlv: the retired ``embeddings.rerankerModel``
client knob emits a LOUD deprecation notice instead of going silently inert.
"""
from __future__ import annotations

import json
from unittest.mock import MagicMock, patch

import pytest
from click.testing import CliRunner

from nexus.cli import main
from nexus.db.http_vector_client import HttpVectorClient
from nexus.types import SearchResult

_CLOUD_ENV = {
    "CHROMA_API_KEY": "k", "VOYAGE_API_KEY": "v",
    "CHROMA_TENANT": "t", "CHROMA_DATABASE": "d",
}
_CFG = {"embeddings": {"rerankerModel": "rerank-2.5"}}


@pytest.fixture
def runner() -> CliRunner:
    return CliRunner()


@pytest.fixture
def cloud_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for k, v in _CLOUD_ENV.items():
        monkeypatch.setenv(k, v)
    monkeypatch.delenv("NX_EMBEDDINGS_RERANKER_MODEL", raising=False)


def _result(id: str, collection: str, distance: float, score: float | None = None) -> SearchResult:
    md: dict = {}
    if score is not None:
        md["rerank_score"] = score
    return SearchResult(
        id=id, content=f"content {id}", distance=distance,
        collection=collection, metadata=md,
    )


def _t3_mock(collections: list[str]) -> MagicMock:
    mock = MagicMock(spec=HttpVectorClient)
    mock.list_collections.return_value = [{"name": n} for n in collections]
    # Real HttpVectorClient carries the capability marker (class attr).
    mock.supports_server_rerank = True
    return mock


def _invoke(runner, mock_t3, fake_cross_corpus, args, cfg=_CFG):
    with patch("nexus.commands.search_cmd._t3", return_value=mock_t3), \
         patch("nexus.commands.search_cmd.search_cross_corpus", side_effect=fake_cross_corpus), \
         patch("nexus.commands.search_cmd.load_config", return_value=cfg):
        return runner.invoke(main, args)


def _fake_retrieval(results, meta_per_collection=None, captured=None):
    """A search_cross_corpus stand-in that records the rerank kwargs and
    populates ``rerank_meta_out`` like the real engine plumb does."""
    def fake(q, cols, n_results, t3, where=None, *, rerank=False,
             rerank_meta_out=None, **kwargs):
        if captured is not None:
            captured.append({"rerank": rerank, "collections": list(cols)})
        if rerank and rerank_meta_out is not None and meta_per_collection:
            rerank_meta_out.update(meta_per_collection)
        return list(results)
    return fake


# ── Server scores consumed (order + request plumb) ──────────────────────────


def test_rerank_requested_and_server_scores_order_output(runner, cloud_env):
    """Multi-collection search requests server rerank and orders output by
    the returned rerank_score — no client Voyage involvement anywhere."""
    results = [
        _result("near", "knowledge__test", 0.1, score=0.05),
        _result("far", "rdr__nexus", 0.4, score=0.95),
        _result("mid", "knowledge__test", 0.2, score=0.50),
    ]
    captured: list[dict] = []
    mock_t3 = _t3_mock(["knowledge__test", "rdr__nexus"])
    # nexus-9o6y2.9: the client Voyage factory is DELETED, not merely unused —
    # the strongest possible "client must not touch Voyage" guarantee.
    import nexus.db as _db
    assert not hasattr(_db, "get_voyage_client")

    res = _invoke(runner, mock_t3, _fake_retrieval(results, captured=captured),
                  ["search", "query", "--corpus", "knowledge,rdr", "--json"])

    assert res.exit_code == 0, res.output
    assert captured and captured[0]["rerank"] is True
    items = json.loads(res.stdout)
    # Server scores INVERT distance order — a client that ignored them fails here.
    assert [i["id"] for i in items] == ["far", "mid", "near"]


def test_no_rerank_flag_suppresses_server_rerank(runner, cloud_env):
    captured: list[dict] = []
    mock_t3 = _t3_mock(["knowledge__test", "rdr__nexus"])

    res = _invoke(runner, mock_t3,
                  _fake_retrieval([_result("a", "knowledge__test", 0.1)], captured=captured),
                  ["search", "query", "--no-rerank", "--corpus", "knowledge,rdr"])

    assert res.exit_code == 0, res.output
    assert captured and captured[0]["rerank"] is False


def test_single_collection_skips_server_rerank(runner, cloud_env):
    captured: list[dict] = []
    mock_t3 = _t3_mock(["knowledge__test"])

    res = _invoke(runner, mock_t3,
                  _fake_retrieval([_result("a", "knowledge__test", 0.1)], captured=captured),
                  ["search", "query", "--corpus", "knowledge"])

    assert res.exit_code == 0, res.output
    assert captured and captured[0]["rerank"] is False


def test_backend_without_capability_never_requests_rerank(runner, cloud_env):
    """A legacy backend (no supports_server_rerank marker) is never asked to
    rerank — the kwarg stays False, no crash, distance order preserved."""
    captured: list[dict] = []
    mock_t3 = MagicMock()
    del mock_t3.supports_server_rerank  # plain MagicMock would fabricate it
    mock_t3.list_collections.return_value = [
        {"name": "knowledge__test"}, {"name": "rdr__nexus"}]

    res = _invoke(runner, mock_t3,
                  _fake_retrieval([_result("a", "knowledge__test", 0.1)], captured=captured),
                  ["search", "query", "--corpus", "knowledge,rdr"])

    assert res.exit_code == 0, res.output
    assert captured and captured[0]["rerank"] is False


# ── Gap 2: degradation is SURFACED, never invisible ─────────────────────────


def test_degraded_rerank_is_surfaced_on_stderr(runner, cloud_env):
    """The server's structured degrade field reaches the USER (stderr), and
    results still render in distance order — loud, not WARN-only invisible."""
    results = [
        _result("a", "knowledge__test", 0.1),
        _result("b", "rdr__nexus", 0.2),
    ]
    meta = {
        "knowledge__test": {"degraded": True, "error": "Voyage AI rerank failed: HTTP 500"},
        "rdr__nexus": {"degraded": True, "error": "Voyage AI rerank failed: HTTP 500"},
    }
    mock_t3 = _t3_mock(["knowledge__test", "rdr__nexus"])

    res = _invoke(runner, mock_t3, _fake_retrieval(results, meta_per_collection=meta),
                  ["search", "query", "--corpus", "knowledge,rdr"])

    assert res.exit_code == 0, res.output
    assert "server rerank degraded" in res.output
    assert "HTTP 500" in res.output


def test_partial_degrade_scored_rows_lead_and_degrade_still_surfaced(runner, cloud_env):
    """One collection degraded, one scored: scored rows lead (by score), the
    degraded collection's rows follow, and the degrade is surfaced."""
    results = [
        _result("unscored", "knowledge__test", 0.05),
        _result("scored-low", "rdr__nexus", 0.3, score=0.2),
        _result("scored-high", "rdr__nexus", 0.4, score=0.9),
    ]
    meta = {"knowledge__test": {"degraded": True, "error": "boom"}}
    mock_t3 = _t3_mock(["knowledge__test", "rdr__nexus"])

    res = _invoke(runner, mock_t3, _fake_retrieval(results, meta_per_collection=meta),
                  ["search", "query", "--corpus", "knowledge,rdr", "--json"])

    assert res.exit_code == 0, res.output
    assert "server rerank degraded" in res.output
    items = json.loads(res.stdout)
    assert [i["id"] for i in items] == ["scored-high", "scored-low", "unscored"]


def test_stale_engine_surfaces_upgrade_convergence_note(runner, cloud_env):
    """An engine predating the fused stage returns bare arrays; the client
    surfaces the convergence path — never a refusal, never silence."""
    results = [_result("a", "knowledge__test", 0.1)]
    meta = {"knowledge__test": {
        "degraded": True, "stale_engine": True,
        "error": "engine predates server-side rerank; `nx upgrade` converges the local engine",
    }}
    mock_t3 = _t3_mock(["knowledge__test", "rdr__nexus"])

    res = _invoke(runner, mock_t3, _fake_retrieval(results, meta_per_collection=meta),
                  ["search", "query", "--corpus", "knowledge,rdr"])

    assert res.exit_code == 0, res.output
    assert "server rerank degraded" in res.output
    assert "nx upgrade" in res.output


# ── nexus-7jvlv: rerankerModel knob retires LOUDLY, not silently ────────────


def test_non_default_rerankermodel_emits_deprecation_notice(runner, cloud_env):
    cfg = {"embeddings": {"rerankerModel": "rerank-2.5-lite"}}
    mock_t3 = _t3_mock(["knowledge__test", "rdr__nexus"])

    res = _invoke(runner, mock_t3, _fake_retrieval([_result("a", "knowledge__test", 0.1)]),
                  ["search", "query", "--corpus", "knowledge,rdr"], cfg=cfg)

    assert res.exit_code == 0, res.output
    assert "rerankerModel" in res.output
    assert "NX_RERANK_MODEL" in res.output


def test_env_rerankermodel_override_emits_deprecation_notice(
    runner, cloud_env, monkeypatch,
):
    monkeypatch.setenv("NX_EMBEDDINGS_RERANKER_MODEL", "rerank-2.5-lite")
    mock_t3 = _t3_mock(["knowledge__test", "rdr__nexus"])

    res = _invoke(runner, mock_t3, _fake_retrieval([_result("a", "knowledge__test", 0.1)]),
                  ["search", "query", "--corpus", "knowledge,rdr"])

    assert res.exit_code == 0, res.output
    assert "NX_RERANK_MODEL" in res.output


def test_default_rerankermodel_stays_silent(runner, cloud_env):
    mock_t3 = _t3_mock(["knowledge__test", "rdr__nexus"])

    res = _invoke(runner, mock_t3, _fake_retrieval([_result("a", "knowledge__test", 0.1)]),
                  ["search", "query", "--corpus", "knowledge,rdr"])

    assert res.exit_code == 0, res.output
    assert "rerankerModel" not in res.output
