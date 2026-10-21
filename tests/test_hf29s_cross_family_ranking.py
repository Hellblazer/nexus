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
from nexus.scoring import _calibration_factor_for_model, apply_hybrid_scoring
from nexus.search_engine import search_cross_corpus
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
    monkeypatch: pytest.MonkeyPatch,
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
    res = _invoke(
        runner, _cli_t3(), _fake_retrieval(_cli_rows(True, True)), _CLI_ARGS,
    )
    assert res.exit_code == 0, res.output
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


def _mcp_search(query: str, rows: list[SearchResult], degraded: dict[str, dict]):
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
                          structured=True)
    finally:
        mcp_infra.inject_t3(None)


@pytest.mark.t2_tenant
def test_mcp_both_groups_scored_merge_by_rerank_score() -> None:
    out = _mcp_search("hf29s mcp both scored", _mcp_rows(True, True), {})
    assert out["ids"] == ["v-hi", "v-mid", "b-rel"]


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
                      lexical=True).content[0].text
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
    def make(cls, monkeypatch: pytest.MonkeyPatch, mode: str | None):
        from nexus.db.http_vector_client import HttpVectorClient

        client = HttpVectorClient()
        monkeypatch.setattr(
            client.__class__, "search",
            lambda self, query, collection_names, n_results=10, where=None:
                list(cls.ROWS.get(collection_names[0], [])),
        )
        monkeypatch.setattr(client.__class__, "embedding_mode", lambda self: mode)
        monkeypatch.setattr(client.__class__, "supports_per_collection_search", False)
        return client


@pytest.mark.t2_tenant
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


@pytest.mark.t2_tenant
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


@pytest.mark.t2_tenant
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


@pytest.mark.t2_tenant
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
