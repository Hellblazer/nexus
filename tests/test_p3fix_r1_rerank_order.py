# SPDX-License-Identifier: AGPL-3.0-or-later
"""RDR-243 Phase 3 fix round r1 (nexus-rfvyy.8), item 4: the client keeps the engine's rerank merge for held rows.

The engine's RerankStage holds sensitive rows back from a text-exporting reranker and merges them back at the slot
they had in the input (vector order); they carry ``sensitive: true`` and no ``rerank_score``. The CLI and MCP
consumers used to put every row without a ``rerank_score`` after all scored rows, which undid the merge (a sensitive
top-1 sank to the tail and could fall off the page), and an all-sensitive collection's loud degrade flipped the
whole page to the calibrated ``partial_rerank`` ordering with a warning.
"""
from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest
from click.testing import CliRunner

from nexus.cli import main
from nexus.search_engine import benign_held_degrades, engine_held_slots, restore_held_slots
from nexus.types import SearchResult

COL = "knowledge__test"


def _r(rid: str, *, sensitive: bool = False, score: float | None = None, dist: float = 0.5, col: str = COL,
       content: str = "body") -> SearchResult:
    meta: dict = {"sensitive": sensitive}
    if score is not None:
        meta["rerank_score"] = score
    return SearchResult(id=rid, content=content, distance=dist, collection=col, metadata=meta)


# ── the helpers ──────────────────────────────────────────────────────────────


def test_engine_held_slots_are_the_sensitive_rows_without_a_score() -> None:
    rows = [_r("s", sensitive=True), _r("a", score=0.9), _r("t", sensitive=True, score=0.8), _r("b")]
    assert [(slot, r.id) for slot, r in engine_held_slots(rows)] == [(0, "s")], (
        "a scored sensitive row (a local reranker scores everything) and an unscored clean row are not held"
    )


def test_restore_held_slots_puts_a_held_row_back_at_its_input_index() -> None:
    held = _r("s", sensitive=True)
    a, b, c = _r("a", score=0.9), _r("b", score=0.5), _r("c")
    out = restore_held_slots([a, b, c], [(0, held)])
    assert [r.id for r in out] == ["s", "a", "b", "c"]
    out = restore_held_slots([a, b, c], [(2, held)])
    assert [r.id for r in out] == ["a", "b", "s", "c"]
    assert [r.id for r in restore_held_slots([a], [(7, held)])] == ["a", "s"], "a slot past the end appends"


def test_a_degrade_whose_every_row_is_held_is_benign() -> None:
    rows = [_r("s1", sensitive=True), _r("s2", sensitive=True), _r("x", col="other", score=0.4)]
    meta = {COL: {"degraded": True, "error": "every rerankable row is sensitive"}, "other": {"degraded": False}}
    assert benign_held_degrades(meta, rows) == {COL}


def test_a_degrade_with_a_clean_unscored_row_is_not_benign() -> None:
    rows = [_r("s1", sensitive=True), _r("c1")]
    assert benign_held_degrades({COL: {"degraded": True}}, rows) == set()


def test_a_degrade_of_a_collection_with_no_rows_is_not_benign() -> None:
    assert benign_held_degrades({COL: {"degraded": True}}, [_r("x", col="other")]) == set()


# ── nx search ────────────────────────────────────────────────────────────────

SECRET_BODY = "the secret merger body"


def _cli_search(rows: list[SearchResult], rerank_meta: dict, monkeypatch, *, n: int = 10, extra=()):
    from tests.commands.test_search_cmd_sensitivity import _CLOUD_ENV, _LOAD_CFG, _NoStorageT2, _mock_t3  # type: ignore[import-not-found]
    from tests.conftest import patched_mcp_infra_t3

    for k, v in _CLOUD_ENV.items():
        monkeypatch.setenv(k, v)
    monkeypatch.delenv("CLAUDECODE", raising=False)
    monkeypatch.delenv("CLAUDE_CODE_ENTRYPOINT", raising=False)
    mock_t3 = _mock_t3()
    mock_t3.supports_server_rerank = True

    def fake(*_a, rerank_meta_out=None, **_kw):
        if rerank_meta_out is not None:
            rerank_meta_out.update(rerank_meta)
        return [SearchResult(id=r.id, content=r.content, distance=r.distance, collection=r.collection,
                             metadata=dict(r.metadata)) for r in rows]

    with patch("nexus.commands.search_cmd._t3", return_value=mock_t3), patched_mcp_infra_t3(mock_t3), \
         patch("nexus.commands.search_cmd.search_cross_corpus", fake), \
         patch("nexus.commands.search_cmd.load_config", return_value=_LOAD_CFG), \
         patch("nexus.db.t2.T2Database", _NoStorageT2):
        return CliRunner().invoke(main, ["search", "q", "--corpus", "knowledge", "--lexical", "--json", "-m", str(n), *extra])


def _ids(result) -> list[str]:
    import json

    assert result.exit_code == 0, result.output
    return [item["id"] for item in json.loads(result.stdout)]


@pytest.mark.parametrize("held_first", [True, False])
def test_cli_search_keeps_a_held_sensitive_top_one_where_the_engine_put_it(monkeypatch, held_first) -> None:
    rows = [
        _r("s", sensitive=True, dist=0.1), _r("a", score=0.9, dist=0.2), _r("b", score=0.7, dist=0.3),
    ] if held_first else [
        _r("a", score=0.9, dist=0.2), _r("s", sensitive=True, dist=0.25), _r("b", score=0.7, dist=0.3),
    ]
    out = _cli_search(rows, {COL: {"degraded": False}}, monkeypatch)
    assert _ids(out) == [r.id for r in rows], "the engine's merged order, held row in its slot"


def test_cli_search_an_all_sensitive_collection_does_not_flip_the_page_or_warn_of_a_degrade(monkeypatch) -> None:
    rows = [
        _r("s1", sensitive=True, dist=0.1, col="knowledge__vault"), _r("a", score=0.9, dist=0.4), _r("b", score=0.7, dist=0.3),
        _r("s2", sensitive=True, dist=0.2, col="knowledge__vault"),
    ]
    meta = {"knowledge__vault": {"degraded": True, "error": "every rerankable row is sensitive"}, COL: {"degraded": False}}
    out = _cli_search(rows, meta, monkeypatch)
    assert _ids(out) == ["s1", "a", "b", "s2"], "scored rows by score, held rows at their slots (not calibrated order)"
    assert "server rerank degraded" not in out.stderr
    assert "held back from the reranker" in out.stderr


def test_cli_search_a_real_degrade_still_warns_and_orders_by_the_calibrated_score(monkeypatch) -> None:
    rows = [_r("a", score=0.9, dist=0.4), _r("c", dist=0.1, col="knowledge__other"), _r("b", score=0.7, dist=0.3)]
    meta = {"knowledge__other": {"degraded": True, "error": "boom"}, COL: {"degraded": False}}
    out = _cli_search(rows, meta, monkeypatch)
    assert out.exit_code == 0 and "server rerank degraded" in out.stderr


# ── MCP search ───────────────────────────────────────────────────────────────


def _mcp_search(rows: list[SearchResult], rerank_meta: dict, tmp_path, monkeypatch) -> tuple[list[str], str]:
    import json

    from nexus.mcp_server import _inject_t3, _reset_singletons, _search_render

    monkeypatch.setenv("NEXUS_CONFIG_DIR", str(tmp_path))
    _reset_singletons()
    t3 = MagicMock()
    t3.supports_server_rerank = True
    t3.list_collections.return_value = [
        {"name": COL, "count": 3, "content_type": "knowledge", "owner_id": "1-1",
         "embedding_model": "test-model", "lifecycle_state": "live"},
    ]
    _inject_t3(t3)

    def fake(*_a, rerank_meta_out=None, **_kw):
        if rerank_meta_out is not None:
            rerank_meta_out.update(rerank_meta)
        return [SearchResult(id=r.id, content=r.content, distance=r.distance, collection=r.collection,
                             metadata=dict(r.metadata)) for r in rows]

    try:
        with patch("nexus.search_engine.search_cross_corpus", fake), \
             patch("nexus.config.load_config", return_value={"search": {"query_sanitizer": False}}), \
             patch("nexus.mcp.core._get_t1", side_effect=RuntimeError("no T1 in this test")), \
             patch("nexus.mcp.core._page_cache_get", return_value=None), \
             patch("nexus.mcp.core._page_cache_put"), \
             patch("nexus.mcp.core._mcp_output_posture", lambda: "flag"):
            out = _search_render(query="q", corpus=COL, structured=True, limit=10, lexical=True, sensitivity="include")
    finally:
        _reset_singletons()
    raw = out if isinstance(out, str) else json.dumps(out, default=str)
    data = out if isinstance(out, dict) else json.loads(raw)
    return list(data["ids"]), raw


def test_mcp_search_keeps_a_held_sensitive_top_one_where_the_engine_put_it(tmp_path, monkeypatch) -> None:
    rows = [_r("s", sensitive=True, dist=0.1), _r("a", score=0.9, dist=0.2), _r("b", score=0.7, dist=0.3)]
    ids, _raw = _mcp_search(rows, {COL: {"degraded": False}}, tmp_path, monkeypatch)
    assert ids == ["s", "a", "b"]


def test_mcp_search_an_all_sensitive_collection_neither_degrades_nor_flips_the_page(tmp_path, monkeypatch) -> None:
    rows = [
        _r("s1", sensitive=True, dist=0.1, col="knowledge__vault"), _r("a", score=0.9, dist=0.4), _r("b", score=0.7, dist=0.3),
        _r("s2", sensitive=True, dist=0.2, col="knowledge__vault"),
    ]
    meta = {"knowledge__vault": {"degraded": True, "error": "every rerankable row is sensitive"}, COL: {"degraded": False}}
    ids, raw = _mcp_search(rows, meta, tmp_path, monkeypatch)
    assert ids == ["s1", "a", "b", "s2"]
    assert "degraded in" not in raw
