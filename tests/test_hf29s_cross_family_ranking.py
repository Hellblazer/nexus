# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""RDR-210 P1.5 (nexus-hf29s): cross-family ranking, client side.

Technical Design 12. One engine can now hold a bge family and a Voyage family
(``dual``), so every surface that merges across families needs one rule:

- ``scoring.py`` lists ``bge-base-en-v15-768`` at an INTERIM factor of 1.30
  (Sam, 2026-10-18: the largest factor 210-research-17 counted inversions
  for; the derived best-F1 factor is 1.38); an unlisted model keeps the default
  and logs it.
- ``_grouped_combined_query`` merges model groups by CALIBRATED distance.
- Rerank-on merges (``nx search``, MCP ``search`` with ``lexical=true``) stop
  sorting scored rows ahead of unscored ones when a collection DEGRADED, and
  order every row by the calibrated ``hybrid_score`` instead.
- Distance thresholds are chosen per collection by its model: a Voyage
  collection is filtered on a ``voyage`` or ``dual`` engine, a bge collection
  never, an ``onnx-local`` engine filters neither.

FIXTURE LIMIT (read before trusting the inversion test). 210-research-17
recorded its summary statistics and its inversion counts but not the 20
per-query distance lists, and they are in neither T2 nor the repo. The
inversion test below therefore uses the RECORDED QUANTILES as its fixture
(bge off-topic top-10 minimum 0.369; relevant p50/p90 0.33/0.40 for bge and
0.63/0.75 for context-3; off-topic top-10 median 0.76 for context-3), through
the real ``apply_hybrid_scoring``. It does not reproduce the record's 12-and-5
counts and it does not stand in for them; Phase 4 Step 3 (nexus-t189m) measures
the value on real per-query data.
"""
from __future__ import annotations

import json
from unittest.mock import patch

import pytest
import structlog
from click.testing import CliRunner

import nexus.mcp_infra as mcp_infra
from nexus import scoring
from nexus.mcp import core as mcp_core
from nexus.scoring import (
    _calibration_factor_for_model,
    apply_hybrid_scoring,
    order_by_calibrated_distance,
)
from nexus.search_engine import apply_ranking_boosts, search_cross_corpus
from nexus.types import SearchResult
from tests.test_mcp_server import _HYBRID_DEFAULT_ON_CFG, _mock_t3, search
from tests.test_search_cmd_server_rerank import (
    _fake_retrieval,
    _invoke,
    _result,
    cloud_env,  # noqa: F401 — pytest fixture re-exported for this module
    runner,  # noqa: F401 — pytest fixture re-exported for this module
)
from tests.conftest import catalog_row_for_collection_name

BGE = "bge-base-en-v15-768"
BGE_DOCS = f"docs__bgeowner__{BGE}__v1"
VOY_RDR = "rdr__voyowner__voyage-context-3__v1"
BGE_KNOW = f"knowledge__bgeowner__{BGE}__v1"


def _row_for(name: str) -> dict:
    """A catalog row whose embedding_model is the name's own token (the
    shared ``catalog_row_for_collection_name`` only knows Voyage tokens)."""
    row = catalog_row_for_collection_name(name)
    parts = name.split("__")
    if len(parts) >= 3:
        row["embedding_model"] = parts[2]
    return row


@pytest.fixture
def rows_by_name(monkeypatch: pytest.MonkeyPatch) -> None:
    """``get_collection_row`` answers from the name's own model token."""
    monkeypatch.setattr(mcp_infra, "get_collection_row", _row_for)


# ── 1. the scoring table ────────────────────────────────────────────────────


def test_bge_factor_is_the_measured_ratio_within_1e_6() -> None:
    assert _calibration_factor_for_model(BGE) == pytest.approx(1.30, abs=1e-6)
    assert _calibration_factor_for_model(BGE) == pytest.approx(0.45 / (0.45 / 1.30), abs=1e-6)
    # The Voyage entries are untouched.
    assert _calibration_factor_for_model("voyage-code-3") == pytest.approx(1.0)
    assert _calibration_factor_for_model("voyage-context-3") == pytest.approx(0.45 / 0.65)


def test_unlisted_model_gets_the_default_and_logs_it_once(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(scoring, "_UNLISTED_MODELS_LOGGED", set())
    with structlog.testing.capture_logs() as logs:
        first = _calibration_factor_for_model("some-future-model-1")
        second = _calibration_factor_for_model("some-future-model-1")
        other = _calibration_factor_for_model("some-future-model-2")
    assert first == second == other == pytest.approx(0.45 / 0.55)
    unlisted = [e for e in logs if e["event"] == "calibration_model_unlisted"]
    assert [e["model"] for e in unlisted] == ["some-future-model-1", "some-future-model-2"]
    assert unlisted[0]["default_threshold"] == pytest.approx(0.55)


def test_a_listed_model_does_not_log_as_unlisted() -> None:
    with structlog.testing.capture_logs() as logs:
        _calibration_factor_for_model(BGE)
        _calibration_factor_for_model("voyage-context-3")
    assert not [e for e in logs if e["event"] == "calibration_model_unlisted"]


# ── 2. apply_hybrid_scoring over the recorded quantiles ─────────────────────


def _hit(collection: str, tag: str, distance: float) -> SearchResult:
    return SearchResult(
        id=f"{collection}-{tag}", content=tag, distance=distance,
        collection=collection, metadata={},
    )


def _top(results: list[SearchResult]) -> str:
    return apply_hybrid_scoring(results, hybrid=False)[0].content


# Recorded in 210-research-17 (spike, chas, develop e34467fdb).
_BGE_OFFTOPIC_MIN = 0.369
_BGE_RELEVANT_P50, _BGE_RELEVANT_P90 = 0.33, 0.40
_VOY_RELEVANT_P50, _VOY_RELEVANT_P90 = 0.63, 0.75
_VOY_OFFTOPIC_MEDIAN = 0.76


def test_default_factor_let_the_closest_off_topic_bge_chunk_beat_a_median_relevant_voyage_chunk(
    rows_by_name,
) -> None:
    """The failure the spike found, on the recorded quantiles: with bge on the
    0.818 default, its CLOSEST off-topic chunk (0.369) beats a median relevant
    Voyage chunk (0.63)."""
    results = [
        _hit(BGE_DOCS, "bge-irrelevant", _BGE_OFFTOPIC_MIN),
        _hit(VOY_RDR, "voyage-relevant", _VOY_RELEVANT_P50),
    ]
    default_factor = _calibration_factor_for_model("never-measured-model")
    assert default_factor * _BGE_OFFTOPIC_MIN < (0.45 / 0.65) * _VOY_RELEVANT_P50
    # bge scored as an unlisted model (the pre-P1.5 behaviour): the bge hit wins.
    with patch.dict(scoring._CALIBRATION_THRESHOLDS_BY_MODEL, clear=False):
        scoring._CALIBRATION_THRESHOLDS_BY_MODEL.pop(BGE)
        assert _top(results) == "bge-irrelevant"
    # With the entry, the relevant Voyage chunk wins.
    assert _top(results) == "voyage-relevant"


def test_bge_entry_orders_the_recorded_median_cases_correctly_in_both_directions(
    rows_by_name,
) -> None:
    # Direction 1: closest off-topic bge chunk vs median relevant Voyage chunk.
    d1 = [
        _hit(BGE_DOCS, "bge-irrelevant", _BGE_OFFTOPIC_MIN),
        _hit(VOY_RDR, "voyage-relevant", _VOY_RELEVANT_P50),
    ]
    assert _top(d1) == "voyage-relevant"
    # Direction 2: median off-topic Voyage chunk vs median relevant bge chunk.
    d2 = [
        _hit(VOY_RDR, "voyage-irrelevant", _VOY_OFFTOPIC_MEDIAN),
        _hit(BGE_DOCS, "bge-relevant", _BGE_RELEVANT_P50),
    ]
    assert _top(d2) == "bge-relevant"


def test_the_interim_1_30_keeps_the_tail_case_the_derived_1_38_loses(
    rows_by_name,
) -> None:
    """The reason the entry is held at 1.30 until Phase 4 Step 3 (nexus-t189m)
    measures on real data. On the recorded quantiles a p90-relevant bge chunk
    (0.40) sits ahead of a MEDIAN off-topic Voyage chunk (0.76 * 0.692 = 0.526)
    at 1.30 (0.40 * 1.30 = 0.52) and behind it at the derived 1.38 (0.554). The
    record counted inversions at 1.15 to 1.30 and never at 1.38 on its per-query
    data. This test pins a LIMIT of the higher factor, not a goal."""
    tail = [
        _hit(VOY_RDR, "voyage-irrelevant", _VOY_OFFTOPIC_MEDIAN),
        _hit(BGE_DOCS, "bge-relevant", _BGE_RELEVANT_P90),
    ]
    assert _top(tail) == "bge-relevant"
    # The same pair at the derived 1.38 (threshold 0.325), which the record never counted:
    with patch.dict(scoring._CALIBRATION_THRESHOLDS_BY_MODEL, {BGE: 0.325}):
        assert _top(tail) == "voyage-irrelevant"
    # Voyage p90 relevant against the closest bge off-topic chunk (a marginal inversion
    # the factor cannot fix without breaking direction 1; the intended tripwire):
    assert _top([
        _hit(BGE_DOCS, "bge-irrelevant", _BGE_OFFTOPIC_MIN),
        _hit(VOY_RDR, "voyage-relevant", _VOY_RELEVANT_P90),
    ]) == "bge-irrelevant"


# ── 3. _grouped_combined_query interleaves by calibrated distance ───────────


def test_grouped_combined_query_interleaves_by_calibrated_distance(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(mcp_infra, "get_collection_row", _row_for)
    rows = {
        BGE_KNOW: [
            {"id": "b1", "collection": BGE_KNOW, "distance": 0.33},   # 0.429
            {"id": "b2", "collection": BGE_KNOW, "distance": 0.50},   # 0.650
        ],
        VOY_RDR: [
            {"id": "v1", "collection": VOY_RDR, "distance": 0.63},    # 0.436
            {"id": "v2", "collection": VOY_RDR, "distance": 0.70},    # 0.485
        ],
    }
    merged = mcp_core._grouped_combined_query(
        [BGE_KNOW, VOY_RDR], lambda group: list(rows[group[0]]),
    )
    # Raw-distance order would be b1 (0.33), b2 (0.50), v1 (0.63), v2 (0.70).
    assert [r["id"] for r in merged] == ["b1", "v1", "v2", "b2"]
    # The engine's distances are reported untouched.
    assert {r["id"]: r["distance"] for r in merged} == {
        "b1": 0.33, "b2": 0.50, "v1": 0.63, "v2": 0.70,
    }


def test_grouped_combined_query_reorders_a_single_family_voyage_code_plus_docs_target(
    monkeypatch: pytest.MonkeyPatch, cloud_mode: None,
) -> None:
    """nexus-ndr0t B1, pinned deliberately (Technical Design 12): a cloud or
    ``voyage`` query over ``code__`` plus ``docs__`` spans TWO models
    (voyage-code-3 at 1.0, voyage-context-3 at 0.45/0.65 = 0.692), so it
    reorders against raw distance even though no bge collection is involved.
    This is the rule working as designed, not a bge-only effect; the release
    note says so. If the factors for the Voyage models change, this order
    changes with them and this test is the tripwire."""
    monkeypatch.setattr(mcp_infra, "get_collection_row", _row_for)
    code = "code__owner__voyage-code-3__v1"
    docs = "docs__owner__voyage-context-3__v1"
    assert _calibration_factor_for_model("voyage-code-3") == pytest.approx(1.0)
    assert _calibration_factor_for_model("voyage-context-3") == pytest.approx(0.45 / 0.65)
    rows = {
        code: [{"id": "c1", "collection": code, "distance": 0.70}],      # 0.700
        docs: [
            {"id": "d1", "collection": docs, "distance": 0.90},          # 0.623
            {"id": "d2", "collection": docs, "distance": 0.60},          # 0.415
        ],
    }
    merged = mcp_core._grouped_combined_query(
        [code, docs], lambda group: list(rows[group[0]]),
    )
    # Raw-distance order would be d2 (0.60), c1 (0.70), d1 (0.90).
    assert [r["id"] for r in merged] == ["d2", "d1", "c1"]
    # The engine's distances are reported untouched.
    assert {r["id"]: r["distance"] for r in merged} == {"c1": 0.70, "d1": 0.90, "d2": 0.60}


def test_grouped_combined_query_single_model_keeps_raw_order(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(mcp_infra, "get_collection_row", _row_for)
    other_bge = f"docs__other__{BGE}__v1"
    rows = {
        BGE_KNOW: [{"id": "a", "collection": BGE_KNOW, "distance": 0.40}],
        other_bge: [{"id": "b", "collection": other_bge, "distance": 0.30}],
    }
    merged = mcp_core._grouped_combined_query(
        [BGE_KNOW, other_bge], lambda group: [r for c in group for r in rows[c]],
    )
    assert [r["id"] for r in merged] == ["b", "a"]


def test_grouped_combined_query_keeps_a_missing_distance_last(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(mcp_infra, "get_collection_row", _row_for)
    rows = {
        BGE_KNOW: [{"id": "none", "collection": BGE_KNOW, "distance": None}],
        VOY_RDR: [{"id": "v", "collection": VOY_RDR, "distance": 0.9}],
    }
    merged = mcp_core._grouped_combined_query(
        [BGE_KNOW, VOY_RDR], lambda group: list(rows[group[0]]),
    )
    assert [r["id"] for r in merged] == ["v", "none"]


# ── 4. rerank-on merges: nx search ──────────────────────────────────────────

_CLI_ARGS = ["search", "query", "--corpus", "knowledge,rdr", "--json"]


def _cli_t3():
    from unittest.mock import MagicMock

    from nexus.db.http_vector_client import HttpVectorClient

    mock = MagicMock(spec=HttpVectorClient)
    mock.list_collections.return_value = [
        {"name": n, **_row_for(n)} for n in (BGE_KNOW, VOY_RDR)
    ]
    mock.supports_server_rerank = True
    return mock


def _cli_rows(bge_scored: bool, voy_scored: bool):
    return [
        _result("b-rel", BGE_KNOW, 0.33, 0.20 if bge_scored else None),
        _result("v-mid", VOY_RDR, 0.63, 0.80 if voy_scored else None),
        _result("v-hi", VOY_RDR, 0.75, 0.90 if voy_scored else None),
    ]


@pytest.mark.t2_tenant
def test_cli_both_groups_scored_merge_by_rerank_score(runner, cloud_env):  # noqa: F811
    # nexus-ml9vu: the resolver the retrieval hands back reaches the boosts
    # (this test already boots a tenant; the wiring check rides on it).
    sentinel = lambda name: None  # noqa: E731
    rows = _fake_retrieval(_cli_rows(True, True))

    def retrieval(*a, **kw):
        kw["model_of_out"].append(sentinel)
        return rows(*a, **kw)

    seen: list = []

    def boosts(results, **kw):
        seen.append(kw.get("model_of"))
        return apply_ranking_boosts(results, **kw)

    with patch("nexus.commands.search_cmd.apply_ranking_boosts", boosts):
        res = _invoke(runner, _cli_t3(), retrieval, _CLI_ARGS)
    assert res.exit_code == 0, res.output
    assert seen == [sentinel]
    assert "server rerank degraded" not in res.output
    # rerank_score order, not calibrated distance (which would put v-mid first).
    assert [i["id"] for i in json.loads(res.stdout)] == ["v-hi", "v-mid", "b-rel"]


@pytest.mark.t2_tenant
def test_cli_one_group_degraded_falls_back_to_calibrated_order_with_the_warning(
    runner, cloud_env,  # noqa: F811
):
    meta = {BGE_KNOW: {"degraded": True, "error": "rate limited"}}
    res = _invoke(
        runner, _cli_t3(),
        _fake_retrieval(_cli_rows(False, True), meta_per_collection=meta), _CLI_ARGS,
    )
    assert res.exit_code == 0, res.output
    assert "server rerank degraded" in res.output
    assert "rate limited" in res.output
    # Calibrated at the interim 1.30: b-rel 0.429, v-mid 0.436, v-hi 0.519. The old
    # "scored rows lead" order was v-hi, v-mid, b-rel and sank the degraded family's row.
    assert [i["id"] for i in json.loads(res.stdout)] == ["b-rel", "v-mid", "v-hi"]


@pytest.mark.t2_tenant
def test_cli_unscored_tail_without_a_degrade_still_trails_the_scored_rows(
    runner, cloud_env,  # noqa: F811
):
    """The engine scores a capped candidate set, so an unscored tail with NO
    degrade is the normal shape and keeps "scored rows lead"."""
    rows = [
        _result("scored", VOY_RDR, 0.75, 0.9),
        _result("tail", BGE_KNOW, 0.33),
    ]
    res = _invoke(runner, _cli_t3(), _fake_retrieval(rows), _CLI_ARGS)
    assert res.exit_code == 0, res.output
    assert [i["id"] for i in json.loads(res.stdout)] == ["scored", "tail"]


# ── 5. rerank-on merges: MCP search ─────────────────────────────────────────


def _mcp_rows(bge_scored: bool, voy_scored: bool) -> list[SearchResult]:
    def md(score: float | None) -> dict:
        return {} if score is None else {"rerank_score": score}

    return [
        SearchResult(id="b-rel", content="x", distance=0.33, collection=BGE_KNOW,
                     metadata=md(0.20 if bge_scored else None)),
        SearchResult(id="v-mid", content="x", distance=0.63, collection=VOY_RDR,
                     metadata=md(0.80 if voy_scored else None)),
        SearchResult(id="v-hi", content="x", distance=0.75, collection=VOY_RDR,
                     metadata=md(0.90 if voy_scored else None)),
    ]


def _mcp_resolver(name: str) -> str | None:
    """Stands in for the resolver ``search_cross_corpus`` hands back (nexus-ml9vu)."""
    return None


def _mcp_search(
    query: str, rows: list[SearchResult], degraded: dict[str, dict],
    boosts_model_of: list | None = None,
):
    t3 = _mock_t3([
        {"name": BGE_KNOW, "count": 3, "embedding_model": BGE},
        {"name": VOY_RDR, "count": 3, "embedding_model": "voyage-context-3"},
    ])
    t3.supports_server_rerank = True

    def _cross_corpus(*a, **kw):
        if kw.get("rerank_meta_out") is not None:
            kw["rerank_meta_out"].update(degraded)
        kw["model_of_out"].append(_mcp_resolver)
        return list(rows)

    def _boosts(results, **kw):
        if boosts_model_of is not None:
            boosts_model_of.append(kw.get("model_of"))
        return apply_ranking_boosts(results, **kw)

    try:
        with patch("nexus.search_engine.search_cross_corpus", _cross_corpus), \
             patch("nexus.search_engine.apply_ranking_boosts", _boosts), \
             patch("nexus.config.load_config", return_value=_HYBRID_DEFAULT_ON_CFG):
            return search(query=query, corpus=f"{BGE_KNOW},{VOY_RDR}", lexical=True,
                          structured=True)
    finally:
        mcp_infra.inject_t3(None)


@pytest.mark.t2_tenant
def test_mcp_both_groups_scored_merge_by_rerank_score() -> None:
    seen: list = []
    out = _mcp_search("hf29s mcp both scored", _mcp_rows(True, True), {}, boosts_model_of=seen)
    assert out["ids"] == ["v-hi", "v-mid", "b-rel"]
    # nexus-ml9vu: the resolver the retrieval handed back reaches the boosts.
    assert seen == [_mcp_resolver]


@pytest.mark.t2_tenant
def test_mcp_one_group_degraded_falls_back_to_calibrated_order_with_the_note() -> None:
    out = _mcp_search(
        "hf29s mcp one degraded", _mcp_rows(False, True),
        {BGE_KNOW: {"degraded": True, "error": "rate limited"}},
    )
    assert out["ids"] == ["b-rel", "v-mid", "v-hi"]  # calibrated at the interim 1.30: 0.429, 0.436, 0.519
    # The degrade is still surfaced, and says what order the rows are in.
    # Served from the page cache, which carries the note of the entry it returns.
    try:
        text = search(query="hf29s mcp one degraded", corpus=f"{BGE_KNOW},{VOY_RDR}",
                      lexical=True)
    finally:
        mcp_infra.inject_t3(None)
    assert "lexical rerank degraded" in text
    assert "calibrated distance order" in text


def _mcp_clustered_search(query: str, rows: list[SearchResult], degraded: dict[str, dict]):
    """``_mcp_search`` with ``cluster_by="semantic"``; rows carry cluster labels."""
    labels = {"b-rel": "cluster-bge", "v-mid": "cluster-voyage", "v-hi": "cluster-voyage"}
    for r in rows:
        r.metadata["_cluster_label"] = labels[r.id]
    t3 = _mock_t3([
        {"name": BGE_KNOW, "count": 3, "embedding_model": BGE},
        {"name": VOY_RDR, "count": 3, "embedding_model": "voyage-context-3"},
    ])
    t3.supports_server_rerank = True

    def _cross_corpus(*a, **kw):
        if kw.get("rerank_meta_out") is not None:
            kw["rerank_meta_out"].update(degraded)
        return list(rows)

    try:
        with patch("nexus.search_engine.search_cross_corpus", _cross_corpus), \
             patch("nexus.config.load_config", return_value=_HYBRID_DEFAULT_ON_CFG):
            return search(query=query, corpus=f"{BGE_KNOW},{VOY_RDR}", lexical=True,
                          cluster_by="semantic", structured=True)
    finally:
        mcp_infra.inject_t3(None)


@pytest.mark.t2_tenant
def test_mcp_unscored_tail_without_a_degrade_still_trails_the_scored_rows() -> None:
    """Mirror of the CLI test: the engine scores a capped candidate set, so an
    unscored tail with NO degrade is the normal shape and keeps "scored rows
    lead", even though the tail row is closer by calibrated distance."""
    rows = [
        SearchResult(id="scored", content="x", distance=0.75, collection=VOY_RDR,
                     metadata={"rerank_score": 0.9}),
        SearchResult(id="tail", content="x", distance=0.33, collection=BGE_KNOW,
                     metadata={}),
    ]
    out = _mcp_search("hf29s mcp unscored tail no degrade", rows, {})
    assert out["ids"] == ["scored", "tail"]


@pytest.mark.t2_tenant
def test_mcp_clustered_partial_rerank_orders_clusters_and_rows_by_the_calibrated_score() -> None:
    """The clustered branch with a partial rerank (one collection degraded, the
    other scored): clusters and the rows inside them order by the calibrated
    ``hybrid_score``, not by rerank score. With "scored rows lead" the cluster
    holding the scored Voyage rows would come first and b-rel last."""
    out = _mcp_clustered_search(
        "hf29s mcp clustered partial", _mcp_rows(False, True),
        {BGE_KNOW: {"degraded": True, "error": "rate limited"}},
    )
    # Calibrated at the interim 1.30: b-rel 0.429, v-mid 0.436, v-hi 0.519. The
    # bge cluster's best row leads; the voyage cluster follows, its rows in score order.
    assert out["ids"] == ["b-rel", "v-mid", "v-hi"]


@pytest.mark.t2_tenant
def test_mcp_clustered_all_scored_orders_clusters_by_rerank_score() -> None:
    """Control for the test above: with no degrade the same rows and clusters
    order by rerank score, so the partial test discriminates the branch."""
    out = _mcp_clustered_search("hf29s mcp clustered all scored", _mcp_rows(True, True), {})
    assert out["ids"] == ["v-hi", "v-mid", "b-rel"]


# ── 6. thresholds are per collection family ─────────────────────────────────


class _ModeT3:
    """A real HttpVectorClient reporting *mode*, serving fixed rows."""

    VOY = "docs__v__voyage-context-3__v1"
    BGE = f"docs__b__{BGE}__v1"
    #: 0.30 is inside every threshold, 0.80 is past the docs threshold (0.65).
    ROWS = {
        VOY: [{"id": "voy-near", "content": "k", "distance": 0.30},
              {"id": "voy-far", "content": "d", "distance": 0.80}],
        BGE: [{"id": "bge-near", "content": "k", "distance": 0.30},
              {"id": "bge-far", "content": "d", "distance": 0.80}],
    }

    @classmethod
    def make(cls, monkeypatch: pytest.MonkeyPatch, mode: str | None, row_resolver=None):
        from nexus.db.http_vector_client import HttpVectorClient

        client = HttpVectorClient(_row_resolver=row_resolver)
        monkeypatch.setattr(
            client.__class__, "search",
            lambda self, query, collection_names, n_results=10, where=None:
                list(cls.ROWS.get(collection_names[0], [])),
        )
        monkeypatch.setattr(client.__class__, "embedding_mode", lambda self: mode)
        monkeypatch.setattr(client.__class__, "supports_per_collection_search", False)

        def _no_stored_vectors(self, collection_name, ids, on_progress=None):
            # The ranking these tests pin never needs stored vectors; the fetch fails and search
            # logs it, as it did against the engine (nothing stored), without reaching storage.
            raise RuntimeError("no stored vectors in this fixture")

        monkeypatch.setattr(client.__class__, "get_embeddings", _no_stored_vectors)
        return client


@pytest.mark.parametrize(
    "mode, voyage_filtered, bge_filtered",
    [
        ("voyage", True, False),
        ("dual", True, False),
        ("onnx-local", False, False),
        (None, False, False),
    ],
    ids=["voyage", "dual", "onnx-local", "unknown"],
)
def test_threshold_follows_the_collection_model_not_the_engine_mode(
    monkeypatch, mode, voyage_filtered, bge_filtered,
):
    t3 = _ModeT3.make(monkeypatch, mode)
    results = search_cross_corpus("q", [_ModeT3.VOY, _ModeT3.BGE], 10, t3)
    ids = {r.id for r in results}
    assert ("voy-far" not in ids) is voyage_filtered
    assert ("bge-far" not in ids) is bge_filtered
    assert {"voy-near", "bge-near"} <= ids


def test_an_explicit_threshold_override_still_applies_to_every_family(monkeypatch):
    t3 = _ModeT3.make(monkeypatch, "onnx-local")
    results = search_cross_corpus(
        "q", [_ModeT3.VOY, _ModeT3.BGE], 10, t3, threshold_override=0.5,
    )
    assert {r.id for r in results} == {"voy-near", "bge-near"}


def test_a_fake_engine_with_the_attribute_gate_filters_voyage_not_bge():
    """Test fakes (not HttpVectorClient) keep the attribute-only gate; the
    per-family rule still holds for them."""
    class _Fake:
        _voyage_client = object()

        def embedding_mode(self):
            return "dual"

        def search(self, query, collection_names, n_results=10, where=None, **kw):
            return list(_ModeT3.ROWS.get(collection_names[0], []))

    results = search_cross_corpus("q", [_ModeT3.VOY, _ModeT3.BGE], 10, _Fake())
    ids = {r.id for r in results}
    assert "voy-far" not in ids and "bge-far" in ids


# ── 7. a legacy (model-token-less) name on a dual engine (nexus-ndr0t S3/B4) ─
#
# Decision of record: an unknown family under a dual engine gets NO threshold.
# A dual engine serves a bge family beside the Voyage one, a name that carries
# no model token cannot say which family holds its vectors, and the 0.45-0.65
# prefix thresholds are Voyage-calibrated (they admit every bge result). A
# non-dual engine keeps the old rule: a legacy name keeps the prefix threshold.

LEGACY = "docs__legacyowner"
_LEGACY_ROWS = [
    {"id": "leg-near", "content": "k", "distance": 0.30},
    {"id": "leg-far", "content": "d", "distance": 0.80},
]


def _legacy_t3(monkeypatch: pytest.MonkeyPatch, mode: str | None):
    t3 = _ModeT3.make(monkeypatch, mode)
    monkeypatch.setitem(_ModeT3.ROWS, LEGACY, list(_LEGACY_ROWS))
    return t3


def test_a_legacy_name_has_no_model_token_so_the_family_is_unknown() -> None:
    from nexus.search_engine import _model_token

    assert _model_token(LEGACY) is None


def test_threshold_for_collection_model_legacy_name_by_engine_kind() -> None:
    from nexus.search_engine import _threshold_for_collection_model

    cfg = {"search": {"distance_threshold": {"docs": 0.65, "default": 0.55}}}
    # Non-dual (the default): a legacy name keeps the prefix threshold, as before.
    assert _threshold_for_collection_model(LEGACY, cfg) == pytest.approx(0.65)
    assert _threshold_for_collection_model(LEGACY, cfg, dual=False) == pytest.approx(0.65)
    # Dual: the family is unknown, so no threshold.
    assert _threshold_for_collection_model(LEGACY, cfg, dual=True) is None
    # A named Voyage collection is still filtered on a dual engine; bge still never.
    assert _threshold_for_collection_model(_ModeT3.VOY, cfg, dual=True) == pytest.approx(0.65)
    assert _threshold_for_collection_model(_ModeT3.BGE, cfg, dual=True) is None
    assert _threshold_for_collection_model(_ModeT3.BGE, cfg, dual=False) is None


@pytest.mark.parametrize(
    "mode, legacy_filtered",
    [
        ("voyage", True),       # a voyage-only engine: the legacy name is Voyage, as before
        ("dual", False),        # the change: unknown family on a dual engine, no threshold
        ("onnx-local", False),  # thresholds off for the engine
        (None, False),          # mode unknown: thresholds off
    ],
    ids=["voyage", "dual", "onnx-local", "unknown"],
)
def test_legacy_name_threshold_by_engine_mode(monkeypatch, mode, legacy_filtered):
    t3 = _legacy_t3(monkeypatch, mode)
    results = search_cross_corpus("q", [LEGACY, _ModeT3.VOY, _ModeT3.BGE], 10, t3)
    ids = {r.id for r in results}
    assert ("leg-far" not in ids) is legacy_filtered
    assert "leg-near" in ids
    # The named families keep their rule on every engine (test 6 pins it too).
    assert ("voy-far" not in ids) is (mode in ("voyage", "dual"))
    assert "bge-far" in ids


def test_an_explicit_override_still_filters_a_legacy_name_on_a_dual_engine(monkeypatch):
    t3 = _legacy_t3(monkeypatch, "dual")
    results = search_cross_corpus("q", [LEGACY], 10, t3, threshold_override=0.5)
    assert {r.id for r in results} == {"leg-near"}


def test_the_doctor_probe_judges_a_legacy_name_by_the_same_rule(monkeypatch):
    """`nx doctor` must not judge a model-less name by a threshold search no
    longer applies to it on a dual engine. The end-to-end probe pin is in
    tests/test_doctor_search.py."""
    from nexus.doctor_search import _default_threshold_for

    cfg = {"search": {"distance_threshold": {"docs": 0.65, "default": 0.55}}}
    monkeypatch.setattr("nexus.config.load_config", lambda *a, **k: cfg)

    class _Engine:
        _voyage_client = object()  # the attribute gate search keeps for fakes

        def __init__(self, mode):
            self._mode = mode

        def embedding_mode(self):
            return self._mode

    assert _default_threshold_for(LEGACY, _Engine("dual")) is None
    assert _default_threshold_for(LEGACY, _Engine("voyage")) == pytest.approx(0.65)
    assert _default_threshold_for(_ModeT3.VOY, _Engine("dual")) == pytest.approx(0.65)
    # No handle: search applies no thresholds without a Voyage signal.
    assert _default_threshold_for(LEGACY) is None


def test_a_fake_engine_reporting_dual_gives_a_legacy_name_no_threshold(monkeypatch):
    class _Fake:
        _voyage_client = object()

        def embedding_mode(self):
            return "dual"

        def search(self, query, collection_names, n_results=10, where=None, **kw):
            return list(_LEGACY_ROWS)

    results = search_cross_corpus("q", [LEGACY], 10, _Fake())
    assert {r.id for r in results} == {"leg-near", "leg-far"}


# ── 8. the family comes from the collection ROW, the name is the fallback ────
# (RDR-210 Phase 4 Step 1 residual S-R3-3, nexus-noayy)
#
# A Voyage sibling registered under a legacy (non-four-segment) name carries no
# model token, so grouping and the per-family threshold, both of which parsed the
# name, grouped it alone and (on a dual engine) dropped its threshold. The row
# says voyage-context-3; search now reads it, once per search and from the
# listing the process already holds.

LEGACY_VOY = "docs__legacyvoy"
LEGACY_BGE = "docs__legacybge"


def _row(name: str, model: str) -> dict:
    return {
        "name": name, "content_type": "docs", "owner_id": name.split("__")[1],
        "embedding_model": model, "lifecycle_state": "live",
    }


class _RowCatalog:
    """A row resolver that counts its lookups, answering from a fixed table."""

    def __init__(self, rows: dict[str, dict]) -> None:
        self.rows = rows
        self.calls: list[str] = []

    def __call__(self, name: str) -> dict | None:
        self.calls.append(name)
        return self.rows.get(name)


def _recording_t3(monkeypatch: pytest.MonkeyPatch, mode: str | None, catalog: _RowCatalog):
    """A client whose ``search`` answers every requested collection from
    ``_ModeT3.ROWS`` (each row tagged with its collection) and records the
    batches it was asked for."""
    t3 = _ModeT3.make(monkeypatch, mode, row_resolver=catalog)
    batches: list[list[str]] = []

    def _search(self, query, collection_names, n_results=10, where=None, **kw):
        batches.append(list(collection_names))
        return [
            {**r, "collection": c}
            for c in collection_names for r in _ModeT3.ROWS.get(c, [])
        ]

    monkeypatch.setattr(t3.__class__, "search", _search)
    t3.batches = batches
    return t3


def _seed_rows(monkeypatch: pytest.MonkeyPatch, *names: str) -> None:
    for n in names:
        monkeypatch.setitem(_ModeT3.ROWS, n, list(_LEGACY_ROWS))


def test_a_legacy_name_whose_row_says_voyage_is_filtered_on_a_dual_engine(monkeypatch):
    """The case the interim rule (nexus-ndr0t S3) drops: the row names the
    family, so the Voyage threshold applies to the legacy name again."""
    _seed_rows(monkeypatch, LEGACY_VOY)
    catalog = _RowCatalog({LEGACY_VOY: _row(LEGACY_VOY, "voyage-model-b")})
    t3 = _recording_t3(monkeypatch, "dual", catalog)
    results = search_cross_corpus("q", [LEGACY_VOY, _ModeT3.BGE], 10, t3)
    ids = {r.id for r in results}
    assert "leg-near" in ids
    assert "leg-far" not in ids          # past the docs threshold (0.65): filtered
    assert "bge-far" in ids              # the bge family stays unfiltered


def test_a_legacy_name_whose_row_says_bge_gets_no_threshold(monkeypatch):
    _seed_rows(monkeypatch, LEGACY_BGE)
    catalog = _RowCatalog({LEGACY_BGE: _row(LEGACY_BGE, BGE)})
    t3 = _recording_t3(monkeypatch, "voyage", catalog)
    results = search_cross_corpus("q", [LEGACY_BGE], 10, t3)
    assert {r.id for r in results} == {"leg-near", "leg-far"}


def test_a_legacy_voyage_row_groups_with_the_conformant_voyage_family(monkeypatch):
    """One combined request for the Voyage family, one for the bge family: the
    legacy name no longer sits alone in a singleton batch."""
    _seed_rows(monkeypatch, LEGACY_VOY)
    # The row names the conformant sibling's own model, read off its name, so they group.
    catalog = _RowCatalog({LEGACY_VOY: _row(LEGACY_VOY, _ModeT3.VOY.split("__")[2])})
    t3 = _recording_t3(monkeypatch, "dual", catalog)
    search_cross_corpus("q", [LEGACY_VOY, _ModeT3.VOY, _ModeT3.BGE], 10, t3)
    assert sorted(map(sorted, t3.batches)) == sorted(
        [sorted([LEGACY_VOY, _ModeT3.VOY]), [_ModeT3.BGE]]
    )


def test_a_name_with_no_token_and_no_row_still_gets_no_threshold_on_a_dual_engine(monkeypatch):
    """The interim rule is the fallback and it is unchanged."""
    _seed_rows(monkeypatch, LEGACY)
    catalog = _RowCatalog({})            # no row backs the name
    t3 = _recording_t3(monkeypatch, "dual", catalog)
    results = search_cross_corpus("q", [LEGACY], 10, t3)
    assert {r.id for r in results} == {"leg-near", "leg-far"}
    # ...and on a non-dual Voyage engine it keeps the prefix threshold.
    t3 = _recording_t3(monkeypatch, "voyage", _RowCatalog({}))
    assert {r.id for r in search_cross_corpus("q", [LEGACY], 10, t3)} == {"leg-near"}


def test_a_row_without_a_model_is_authoritative_and_not_guessed_from_the_name(monkeypatch):
    """Row presence alone gates the fallback (resolve_row_preferred): an
    incomplete row on a conformant Voyage name is a model-less family, so on a
    dual engine it gets no threshold rather than the name's."""
    _seed_rows(monkeypatch, _ModeT3.VOY)
    monkeypatch.setitem(_ModeT3.ROWS, _ModeT3.VOY, list(_LEGACY_ROWS))
    row = _row(_ModeT3.VOY, "voyage-model-b")
    del row["embedding_model"]
    t3 = _recording_t3(monkeypatch, "dual", _RowCatalog({_ModeT3.VOY: row}))
    assert {r.id for r in search_cross_corpus("q", [_ModeT3.VOY], 10, t3)} == {"leg-near", "leg-far"}


def test_conformant_names_are_unchanged_and_each_name_is_looked_up_once(monkeypatch):
    """No request multiplication: one lookup per distinct collection per
    search, however many batches, groups and threshold reads follow."""
    catalog = _RowCatalog({
        _ModeT3.VOY: _row(_ModeT3.VOY, "voyage-model-b"),
        _ModeT3.BGE: _row(_ModeT3.BGE, BGE),
    })
    t3 = _recording_t3(monkeypatch, "dual", catalog)
    results = search_cross_corpus("q", [_ModeT3.VOY, _ModeT3.BGE, _ModeT3.VOY], 10, t3)
    ids = {r.id for r in results}
    assert "voy-far" not in ids and "bge-far" in ids
    assert sorted(catalog.calls) == sorted([_ModeT3.VOY, _ModeT3.BGE])


def test_a_fake_handle_without_a_row_resolver_falls_back_to_the_name(monkeypatch):
    """Test fakes and injected stubs carry no catalog: name parsing only, and
    no reach into the process-wide row cache (a read of it fails the test)."""
    from nexus.search_engine import _group_collections_by_embedding_model

    def _no_cache_read(*a, **k):
        raise AssertionError("a handle with no row resolver must not read the row cache")

    monkeypatch.setattr(mcp_infra, "get_collection_row", _no_cache_read)

    class _Fake:
        _voyage_client = object()

        def embedding_mode(self):
            return "dual"

        def search(self, query, collection_names, n_results=10, where=None, **kw):
            return list(_LEGACY_ROWS)

    assert {r.id for r in search_cross_corpus("q", [LEGACY], 10, _Fake())} == {"leg-near", "leg-far"}
    assert _group_collections_by_embedding_model([_ModeT3.VOY, _ModeT3.BGE]) == [
        [_ModeT3.VOY], [_ModeT3.BGE],
    ]


def test_the_doctor_probe_reads_the_family_from_the_row_too(monkeypatch):
    from nexus.doctor_search import _default_threshold_for

    cfg = {"search": {"distance_threshold": {"docs": 0.65, "default": 0.55}}}
    monkeypatch.setattr("nexus.config.load_config", lambda *a, **k: cfg)
    catalog = _RowCatalog({LEGACY_VOY: _row(LEGACY_VOY, "voyage-model-b")})
    t3 = _recording_t3(monkeypatch, "dual", catalog)
    assert _default_threshold_for(LEGACY_VOY, t3) == pytest.approx(0.65)
    assert _default_threshold_for(LEGACY, t3) is None


def test_the_default_tenant_reads_rows_from_the_cache_already_held_and_fetches_nothing(monkeypatch):
    """The process-default client adds NO request to learn a model: it reads the
    collections cache the caller listed through, and never lists or refreshes."""
    from nexus.db.http_vector_client import HttpVectorClient

    monkeypatch.setattr(mcp_infra, "_collections_cache", mcp_infra._collections_cache)
    mcp_infra.prime_collections_cache([_row(LEGACY_VOY, "voyage-model-b")])

    def _no_fetch(*a, **k):
        raise AssertionError("a row read must not fetch")

    monkeypatch.setattr(HttpVectorClient, "list_collections", _no_fetch)
    monkeypatch.setattr(mcp_infra, "_refresh_collections_cache_if_stale", _no_fetch)
    got = HttpVectorClient()._resolve_collection_rows([LEGACY_VOY, "docs__cold"])
    assert got[LEGACY_VOY]["embedding_model"] == "voyage-model-b"
    assert got["docs__cold"] is None      # not in the cache: the name answers


def test_a_non_default_tenant_client_reads_the_listing_once_not_once_per_name(monkeypatch):
    """A client for another tenant has no shared row cache; resolving its rows
    costs ONE listing for the whole search, never one per collection."""
    from nexus.db.http_vector_client import HttpVectorClient

    listings: list[int] = []
    rows = [_row(LEGACY_VOY, "voyage-model-b"), _row(_ModeT3.BGE, BGE)]

    def _list(self, *a, **k):
        listings.append(1)
        return rows

    monkeypatch.setattr(HttpVectorClient, "list_collections", _list)
    monkeypatch.setattr("nexus.db.http_vector_client._process_default_tenant", lambda: "default")
    client = HttpVectorClient(tenant="other")
    got = client._resolve_collection_rows([LEGACY_VOY, _ModeT3.BGE, "docs__absent"])
    assert len(listings) == 1
    assert got[LEGACY_VOY]["embedding_model"] == "voyage-model-b"
    assert got[_ModeT3.BGE]["embedding_model"] == BGE
    assert got["docs__absent"] is None


def test_a_default_tenant_client_filters_a_legacy_voyage_row_from_the_primed_cache(monkeypatch):
    """The production wiring end to end: a plain default-tenant HttpVectorClient
    (no injected resolver), the collections cache primed as the caller's listing
    primes it, and search_cross_corpus. The legacy name groups and filters as
    Voyage, and the search adds no listing."""
    from nexus.db.http_vector_client import HttpVectorClient

    _seed_rows(monkeypatch, LEGACY_VOY)
    monkeypatch.setattr(mcp_infra, "_collections_cache", mcp_infra._collections_cache)
    mcp_infra.prime_collections_cache([_row(LEGACY_VOY, "voyage-model-b"), _row(_ModeT3.BGE, BGE)])

    def _no_fetch(*a, **k):
        raise AssertionError("the search must not list or refresh")

    monkeypatch.setattr(HttpVectorClient, "list_collections", _no_fetch)
    monkeypatch.setattr(mcp_infra, "_refresh_collections_cache_if_stale", _no_fetch)
    t3 = _ModeT3.make(monkeypatch, "dual")
    ids = {r.id for r in search_cross_corpus("q", [LEGACY_VOY, _ModeT3.BGE], 10, t3)}
    assert "leg-near" in ids
    assert "leg-far" not in ids
    assert "bge-far" in ids


def test_a_model_less_name_with_no_row_logs_once_that_the_family_is_unresolved(monkeypatch):
    from structlog.testing import capture_logs

    from nexus.search_engine import _collection_model_resolver

    t3 = _ModeT3.make(monkeypatch, "dual")
    monkeypatch.setattr(mcp_infra, "_collections_cache", mcp_infra._collections_cache)
    mcp_infra.prime_collections_cache([])
    with capture_logs() as logs:
        model_of = _collection_model_resolver(t3, [LEGACY])
        assert model_of(LEGACY) is None
        assert model_of(LEGACY) is None
    unresolved = [e for e in logs if e.get("event") == "collection_model_unresolved"]
    assert len(unresolved) == 1 and unresolved[0]["collection"] == LEGACY


# ── nexus-ml9vu: calibration reads the model from the row, as grouping does ──
#
# nexus-noayy made grouping and the thresholds row-preferred; the calibration
# factor still came from the NAME, so a legacy name whose row says Voyage was
# scored with the factor of whatever the install embeds legacy names with now.

#: A non-local model the calibration table does not list: it behaves as Voyage.
_VOY_B = "voyage-model-b"
_CONFORMANT_VOY = f"docs__v__{_VOY_B}__v1"
_CONFORMANT_BGE = f"docs__b__{BGE}__v1"


def _row_models(**by_name: str):
    """A ``name -> model | None`` function over a fixed table (the shape of
    ``_collection_model_resolver``'s answer)."""
    return lambda name: by_name.get(name)


@pytest.fixture
def bge_profile_legacy(monkeypatch: pytest.MonkeyPatch) -> None:
    """A bge-profile dual engine: a legacy name's own derivation answers bge.
    Scoring's code-collection read finds no row (nothing is listed)."""
    monkeypatch.setattr("nexus.corpus.resolve_read_embedding_model", lambda content_type: BGE)
    monkeypatch.setattr(mcp_infra, "get_collection_row", lambda name, **kw: None)


def _legacy_pair() -> list[SearchResult]:
    return [_hit(LEGACY_VOY, "legacy", 0.50), _hit(_CONFORMANT_VOY, "conformant", 0.60)]


def test_an_omitted_resolver_means_the_name_derivation(
    bge_profile_legacy,
) -> None:
    """With no ``model_of`` the factors come from the name alone, as before the
    resolver existed: a legacy name takes the install's own derivation (bge
    here). That is the documented consequence of omitting it, not the intended
    answer for a legacy Voyage row; the resolver is what corrects it."""
    factors = scoring._resolve_calibration_factors(_legacy_pair())
    assert factors[LEGACY_VOY] == pytest.approx(_calibration_factor_for_model(BGE))
    assert factors[_CONFORMANT_VOY] == pytest.approx(_calibration_factor_for_model(_VOY_B))


def test_a_legacy_voyage_row_and_a_conformant_voyage_collection_are_one_model(
    bge_profile_legacy,
) -> None:
    results = _legacy_pair()
    model_of = _row_models(**{LEGACY_VOY: _VOY_B})
    factors = scoring._resolve_calibration_factors(results, model_of=model_of)
    assert factors == {LEGACY_VOY: 1.0, _CONFORMANT_VOY: 1.0}
    ranked = apply_hybrid_scoring(results, hybrid=False, model_of=model_of)
    assert [r.content for r in ranked] == ["legacy", "conformant"]
    assert ranked[0].hybrid_score == pytest.approx(1.0)


def test_a_legacy_voyage_row_beside_a_bge_collection_gets_each_its_own_factor(
    bge_profile_legacy,
) -> None:
    results = [_hit(LEGACY_VOY, "legacy", 0.50), _hit(_CONFORMANT_BGE, "bge", 0.40)]
    factors = scoring._resolve_calibration_factors(
        results, model_of=_row_models(**{LEGACY_VOY: _VOY_B}),
    )
    assert factors[LEGACY_VOY] == pytest.approx(_calibration_factor_for_model(_VOY_B))
    assert factors[_CONFORMANT_BGE] == pytest.approx(_calibration_factor_for_model(BGE))
    assert factors[LEGACY_VOY] != pytest.approx(factors[_CONFORMANT_BGE])


def test_a_none_resolver_changes_nothing(bge_profile_legacy) -> None:
    results = [_hit(LEGACY_VOY, "legacy", 0.50), _hit(_CONFORMANT_BGE, "bge", 0.40)]
    assert scoring._resolve_calibration_factors(results, model_of=None) == (
        scoring._resolve_calibration_factors(results)
    )
    assert [r.content for r in order_by_calibrated_distance(results, model_of=None)] == [
        r.content for r in order_by_calibrated_distance(results)
    ]


def test_a_name_the_resolver_cannot_answer_falls_back_to_the_name_derivation(
    bge_profile_legacy,
) -> None:
    """No token and no row (the resolver answers None): the install's own
    derivation decides, as before, so a bge-profile legacy name sits with a bge
    collection and the set is one model."""
    results = [_hit(LEGACY, "legacy", 0.50), _hit(_CONFORMANT_BGE, "bge", 0.40)]
    factors = scoring._resolve_calibration_factors(results, model_of=lambda name: None)
    assert factors == {LEGACY: 1.0, _CONFORMANT_BGE: 1.0}


def test_a_row_model_the_table_does_not_list_still_logs_and_takes_the_default(
    bge_profile_legacy, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(scoring, "_UNLISTED_MODELS_LOGGED", set())
    results = [_hit(LEGACY_VOY, "legacy", 0.50), _hit(_CONFORMANT_BGE, "bge", 0.40)]
    with structlog.testing.capture_logs() as logs:
        factors = scoring._resolve_calibration_factors(
            results, model_of=_row_models(**{LEGACY_VOY: "row-model-not-in-table"}),
        )
    assert factors[LEGACY_VOY] == pytest.approx(0.45 / 0.55)
    assert [e["model"] for e in logs if e["event"] == "calibration_model_unlisted"] == [
        "row-model-not-in-table",
    ]


def test_the_boost_window_cut_uses_the_resolver_too(bge_profile_legacy) -> None:
    """``order_by_calibrated_distance`` cuts the window the boosts score: with
    the row's model the legacy row is the nearest, so a window of one keeps it."""
    results = _legacy_pair()
    model_of = _row_models(**{LEGACY_VOY: _VOY_B})
    assert [r.content for r in order_by_calibrated_distance(results, model_of=model_of)] == [
        "legacy", "conformant",
    ]
    ranked = apply_ranking_boosts(results, window=1, model_of=model_of)
    assert [r.content for r in ranked] == ["legacy", "conformant"]
    assert ranked[0].hybrid_score == pytest.approx(1.0)
    # Without it, today's cut: the bge factor pushes the legacy row behind.
    assert apply_ranking_boosts(_legacy_pair(), window=1)[0].content == "conformant"


@pytest.mark.parametrize("window", [None, 2], ids=["no-window", "window-covers-both"])
def test_the_boosts_score_the_head_with_the_resolvers_factors(bge_profile_legacy, window) -> None:
    """The hybrid scoring inside ``apply_ranking_boosts`` gets the resolver too:
    with a window of one the head is a single row that scores 1.0 whatever the
    factors, so only a window that holds both rows (or none, as MCP ``query``
    calls it) can tell. The legacy Voyage row is the nearest on the shared scale."""
    model_of = _row_models(**{LEGACY_VOY: _VOY_B})
    ranked = apply_ranking_boosts(_legacy_pair(), window=window, model_of=model_of)
    assert [r.content for r in ranked] == ["legacy", "conformant"]
    assert ranked[0].hybrid_score == pytest.approx(1.0)
    # Without it the bge factor pushes the legacy row behind (today's cut).
    assert apply_ranking_boosts(_legacy_pair(), window=window)[0].content == "conformant"


def test_search_hands_back_the_resolver_it_built_and_it_ranks_the_legacy_row_first(
    monkeypatch, bge_profile_legacy,
):
    """The resolver ``search_cross_corpus`` builds from the rows (no second row
    read) is what the caller passes on to the boosts."""
    monkeypatch.setitem(_ModeT3.ROWS, LEGACY_VOY, [{"id": "leg", "content": "l", "distance": 0.50}])
    monkeypatch.setitem(
        _ModeT3.ROWS, _ModeT3.VOY, [{"id": "con", "content": "c", "distance": 0.60}],
    )
    voy_model = _ModeT3.VOY.split("__")[2]
    catalog = _RowCatalog({LEGACY_VOY: _row(LEGACY_VOY, voy_model)})
    t3 = _recording_t3(monkeypatch, "dual", catalog)
    out: list = []
    results = search_cross_corpus("q", [LEGACY_VOY, _ModeT3.VOY], 10, t3, model_of_out=out)
    assert len(out) == 1 and out[0](LEGACY_VOY) == voy_model
    boosted = apply_ranking_boosts(results, window=1, model_of=out[0])
    assert [r.id for r in boosted] == ["leg", "con"]
    assert apply_ranking_boosts(results, window=1)[0].id == "con"
    # One lookup per name for the whole search, boosts included.
    assert sorted(catalog.calls) == sorted([LEGACY_VOY, _ModeT3.VOY])


def _wiring_probe(rows: list[SearchResult]):
    """A ``search_cross_corpus`` stand-in that hands back a sentinel resolver
    through ``model_of_out`` and records what ``apply_ranking_boosts`` is given."""
    seen: dict = {}

    def sentinel(name: str) -> str | None:
        return None

    def _cross_corpus(*a, **kw):
        kw["model_of_out"].append(sentinel)
        return list(rows)

    def _boosts(results, **kw):
        seen["model_of"] = kw.get("model_of")
        return results

    return sentinel, seen, _cross_corpus, _boosts


def test_mcp_query_passes_the_retrieval_resolver_to_the_boosts() -> None:
    rows = [_hit(_CONFORMANT_BGE, "b", 0.40)]
    sentinel, seen, cross_corpus, boosts = _wiring_probe(rows)
    _mock_t3([{"name": _CONFORMANT_BGE, "count": 1, "embedding_model": BGE}])
    try:
        with patch("nexus.search_engine.search_cross_corpus", cross_corpus), \
             patch("nexus.search_engine.apply_ranking_boosts", boosts), \
             patch("nexus.config.load_config", return_value=_HYBRID_DEFAULT_ON_CFG):
            mcp_core.query(question="ml9vu wiring query", corpus=_CONFORMANT_BGE, structured=True)
    finally:
        mcp_infra.inject_t3(None)
    assert seen["model_of"] is sentinel
