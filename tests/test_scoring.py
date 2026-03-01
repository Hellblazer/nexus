"""scoring.py edge cases: empty inputs, error paths, interleaving."""
from unittest.mock import MagicMock, patch

import pytest

from nexus.scoring import (
    _file_size_factor,
    apply_hybrid_scoring,
    min_max_normalize,
    rerank_results,
    round_robin_interleave,
)
from nexus.types import SearchResult


def _result(coll: str = "code__repo", dist: float = 0.3, frecency: float = 0.5) -> SearchResult:
    return SearchResult(
        id="r1",
        content="some content",
        distance=dist,
        collection=coll,
        metadata={"frecency_score": frecency},
    )


# ── min_max_normalize ────────────────────────────────────────────────────────

def test_min_max_normalize_empty_window_raises() -> None:
    with pytest.raises(ValueError, match="window must be non-empty"):
        min_max_normalize(0.5, [])


def test_min_max_normalize_single_element() -> None:
    assert min_max_normalize(42.0, [42.0]) == 1.0


def test_min_max_normalize_identical_values() -> None:
    """When all window values are the same, result collapses to ~0.0."""
    result = min_max_normalize(5.0, [5.0, 5.0, 5.0])
    assert result == pytest.approx(0.0, abs=1e-6)


def test_min_max_normalize_typical() -> None:
    result = min_max_normalize(0.5, [0.0, 1.0])
    assert result == pytest.approx(0.5, abs=1e-6)


# ── apply_hybrid_scoring ─────────────────────────────────────────────────────

def test_apply_hybrid_scoring_empty_results() -> None:
    assert apply_hybrid_scoring([], hybrid=True) == []


def test_apply_hybrid_scoring_no_code_corpus_warning() -> None:
    """--hybrid with only docs results logs warning, uses v_norm only."""
    r = _result(coll="docs__corpus", dist=0.2)
    results = apply_hybrid_scoring([r], hybrid=True)
    assert len(results) == 1
    assert results[0].hybrid_score is not None


def test_apply_hybrid_scoring_code_corpus_uses_frecency() -> None:
    r = _result(coll="code__repo", dist=0.2, frecency=0.8)
    results = apply_hybrid_scoring([r], hybrid=True)
    assert results[0].hybrid_score > 0


# ── rerank_results ───────────────────────────────────────────────────────────

def test_rerank_results_empty() -> None:
    assert rerank_results([], "query") == []


def test_rerank_results_exception_returns_original() -> None:
    """When reranker raises, results are returned in original order."""
    mock_client = MagicMock()
    mock_client.rerank.side_effect = Exception("API error")
    r = _result()

    with patch("nexus.scoring._voyage_client", return_value=mock_client):
        results = rerank_results([r], "query", top_k=1)

    assert len(results) == 1
    assert results[0].id == "r1"


# ── round_robin_interleave ───────────────────────────────────────────────────

def test_round_robin_interleave_empty_groups() -> None:
    assert round_robin_interleave([]) == []


def test_round_robin_interleave_single_empty_group() -> None:
    assert round_robin_interleave([[]]) == []


def test_round_robin_interleave_mixed_lengths() -> None:
    a = _result(coll="code__a", dist=0.1)
    b = _result(coll="code__b", dist=0.2)
    c = _result(coll="code__a", dist=0.3)
    result = round_robin_interleave([[a, c], [b]])
    assert [r.distance for r in result] == [0.1, 0.2, 0.3]


# ── _file_size_factor ────────────────────────────────────────────────────────

def test_file_size_factor_at_threshold() -> None:
    """chunk_count == threshold → factor == 1.0 (no penalty)."""
    assert _file_size_factor(30) == pytest.approx(1.0)


def test_file_size_factor_above_threshold() -> None:
    """chunk_count > threshold → factor < 1.0."""
    assert _file_size_factor(37) == pytest.approx(30 / 37, abs=0.001)


def test_file_size_factor_below_threshold() -> None:
    """chunk_count < threshold → capped at 1.0."""
    assert _file_size_factor(10) == pytest.approx(1.0)


def test_file_size_factor_zero_chunks() -> None:
    """chunk_count == 0 → treated as 1 via max(1, 0), factor == 1.0."""
    assert _file_size_factor(0) == pytest.approx(1.0)


# ── file-size penalty in apply_hybrid_scoring ────────────────────────────────

def _sized_result(coll: str, dist: float, chunks: int) -> SearchResult:
    return SearchResult(
        id=f"{coll}-d{dist}",
        content="content",
        distance=dist,
        collection=coll,
        metadata={"frecency_score": 0.5, "chunk_count": chunks},
    )


def test_size_penalty_applied_regardless_of_hybrid_flag() -> None:
    """File-size penalty applies to code__ results even when hybrid=False."""
    # Three results so middle one (dist=0.5) gets v_norm=0.5
    r_a = _sized_result("code__repo", 0.0, 5)   # v_norm=1.0, factor=1.0 → score=1.0
    r_b = _sized_result("code__repo", 0.5, 60)  # v_norm=0.5, factor=30/60=0.5 → score=0.25
    r_c = _sized_result("code__repo", 1.0, 5)   # v_norm=0.0, factor=1.0 → score=0.0
    results = apply_hybrid_scoring([r_a, r_b, r_c], hybrid=False)
    score_map = {r.distance: r.hybrid_score for r in results}
    assert score_map[0.5] == pytest.approx(0.25, abs=1e-6)


@pytest.mark.parametrize("coll", ["docs__corpus", "knowledge__notes"])
def test_size_penalty_not_applied_to_non_code_results(coll: str) -> None:
    """File-size penalty NOT applied to docs__ or knowledge__ results."""
    r_a = _sized_result(coll, 0.0, 5)   # v_norm=1.0 → score=1.0
    r_b = _sized_result(coll, 0.5, 60)  # v_norm=0.5, no penalty → score=0.5
    r_c = _sized_result(coll, 1.0, 5)   # v_norm=0.0 → score=0.0
    results = apply_hybrid_scoring([r_a, r_b, r_c], hybrid=False)
    score_map = {r.distance: r.hybrid_score for r in results}
    assert score_map[0.5] == pytest.approx(0.5, abs=1e-6)
