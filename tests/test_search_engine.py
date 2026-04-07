"""AC1–AC8: Search engine — hybrid scoring, reranking, output formatters."""
from unittest.mock import MagicMock, patch

import pytest

from nexus.formatters import format_json, format_vimgrep
from nexus.scoring import (
    apply_hybrid_scoring,
    hybrid_score,
    min_max_normalize,
    rerank_results,
    round_robin_interleave,
)
from nexus.search_engine import search_cross_corpus
from nexus.types import SearchResult


# ── AC1: Hybrid scoring ───────────────────────────────────────────────────────

def test_min_max_normalize_basic():
    """min_max_normalize maps min→0, max→1, middle proportionally."""
    values = [1.0, 3.0, 5.0]
    assert min_max_normalize(1.0, values) == pytest.approx(0.0, abs=1e-6)
    assert min_max_normalize(5.0, values) == pytest.approx(1.0, abs=1e-6)
    assert min_max_normalize(3.0, values) == pytest.approx(0.5, abs=1e-6)


def test_min_max_normalize_all_equal_returns_zero():
    """All-identical window → denominator is ε, result ≈ 0."""
    values = [2.0, 2.0, 2.0]
    result = min_max_normalize(2.0, values)
    assert result == pytest.approx(0.0, abs=1e-3)


def test_hybrid_score_weights():
    """hybrid_score = 0.7 * vector_norm + 0.3 * frecency_norm."""
    # vector_norm=0.8, frecency_norm=0.5 → 0.7*0.8 + 0.3*0.5 = 0.56 + 0.15 = 0.71
    score = hybrid_score(vector_norm=0.8, frecency_norm=0.5)
    assert score == pytest.approx(0.71, abs=1e-6)


def test_hybrid_score_zero_frecency():
    """For docs/knowledge results with no frecency, score = 0.7 * vector_norm."""
    score = hybrid_score(vector_norm=1.0, frecency_norm=0.0)
    assert score == pytest.approx(0.7, abs=1e-6)


def test_hybrid_score_ripgrep_exact_vector_norm_one():
    """Ripgrep exact-match: vector_norm=1.0 before weighted sum."""
    score = hybrid_score(vector_norm=1.0, frecency_norm=0.6)
    assert score == pytest.approx(0.7 * 1.0 + 0.3 * 0.6, abs=1e-6)


# ── AC2: --hybrid warns when no code corpus ───────────────────────────────────

def test_hybrid_no_code_corpus_warning(capsys):
    """hybrid_score_results logs a warning when no code__ collections in scope."""
    results = [
        SearchResult(id="1", content="text", distance=0.1,
                     collection="docs__papers", metadata={}),
    ]
    apply_hybrid_scoring(results, hybrid=True)
    captured = capsys.readouterr()
    assert "no code corpus" in (captured.out + captured.err).lower()


def test_hybrid_mixed_corpus_no_warning(capsys):
    """With both code__ and docs__ in scope, no warning is printed."""
    results = [
        SearchResult(id="1", content="code", distance=0.1,
                     collection="code__myrepo", metadata={"frecency_score": 1.5}),
        SearchResult(id="2", content="docs", distance=0.2,
                     collection="docs__papers", metadata={}),
    ]
    apply_hybrid_scoring(results, hybrid=True)
    out = capsys.readouterr()
    assert "no code corpus" not in (out.err + out.out).lower()


# ── AC3: Cross-corpus reranking ───────────────────────────────────────────────

def test_rerank_results_returns_unified_ranking():
    """rerank_results reorders results using the reranker model."""
    results = [
        SearchResult(id="1", content="alpha", distance=0.5, collection="code__r", metadata={}),
        SearchResult(id="2", content="beta", distance=0.2, collection="docs__d", metadata={}),
        SearchResult(id="3", content="gamma", distance=0.8, collection="knowledge__k", metadata={}),
    ]
    mock_client = MagicMock()
    mock_client.rerank.return_value = MagicMock(
        results=[
            MagicMock(index=2, relevance_score=0.9),
            MagicMock(index=0, relevance_score=0.7),
            MagicMock(index=1, relevance_score=0.3),
        ]
    )
    with patch("nexus.scoring._voyage_client", return_value=mock_client):
        reranked = rerank_results(results, query="test", model="rerank-2.5", top_k=3)
    assert reranked[0].id == "3"
    assert reranked[1].id == "1"
    assert reranked[2].id == "2"


def test_round_robin_interleave_no_rerank():
    """round_robin_interleave alternates results across collections."""
    code_results = [
        SearchResult(id="c1", content="c1", distance=0.1, collection="code__r", metadata={}),
        SearchResult(id="c2", content="c2", distance=0.2, collection="code__r", metadata={}),
    ]
    doc_results = [
        SearchResult(id="d1", content="d1", distance=0.3, collection="docs__d", metadata={}),
    ]
    merged = round_robin_interleave([code_results, doc_results])
    ids = [r.id for r in merged]
    # Round-robin: c1, d1, c2
    assert ids == ["c1", "d1", "c2"]


def test_cross_corpus_overfetch():
    """search_cross_corpus over-fetches per corpus: 2x code, 4x docs."""
    mock_t3 = MagicMock()
    mock_t3.search.return_value = []
    # code__r → 2x (20), docs__d → 4x (40)
    search_cross_corpus(
        query="test", collections=["code__r", "docs__d"],
        n_results=10, t3=mock_t3
    )
    calls = mock_t3.search.call_args_list
    assert len(calls) == 2
    code_call = [c for c in calls if c.args[1] == ["code__r"]][0]
    docs_call = [c for c in calls if c.args[1] == ["docs__d"]][0]
    assert code_call.kwargs.get("n_results") == 20   # 10 * 2
    assert docs_call.kwargs.get("n_results") == 40   # 10 * 4



# ── AC7: Output formatters ────────────────────────────────────────────────────

def test_format_vimgrep():
    """format_vimgrep produces path:line:0:content lines."""
    results = [
        SearchResult(id="1", content="    def authenticate(user, token):",
                     distance=0.1, collection="code__r",
                     metadata={"source_path": "./auth.py", "line_start": 42}),
    ]
    lines = format_vimgrep(results)
    assert lines[0] == "./auth.py:42:0:    def authenticate(user, token):"


def test_format_vimgrep_missing_source_path():
    """format_vimgrep falls back to empty path when metadata lacks source_path."""
    results = [
        SearchResult(id="1", content="some text", distance=0.1,
                     collection="knowledge__k", metadata={}),
    ]
    lines = format_vimgrep(results)
    assert len(lines) == 1
    assert ":0:" in lines[0]


def test_format_json_valid():
    """format_json produces valid JSON with id, content, distance fields."""
    import json
    results = [
        SearchResult(id="abc123", content="some text", distance=0.42,
                     collection="code__r",
                     metadata={"source_path": "./x.py"}),
    ]
    output = format_json(results)
    parsed = json.loads(output)
    assert isinstance(parsed, list)
    assert parsed[0]["id"] == "abc123"
    assert parsed[0]["distance"] == pytest.approx(0.42)
    assert "content" in parsed[0]


def test_format_json_includes_metadata():
    """format_json embeds metadata fields."""
    import json
    results = [
        SearchResult(id="1", content="x", distance=0.1, collection="code__r",
                     metadata={"source_path": "./a.py", "line_start": 10}),
    ]
    parsed = json.loads(format_json(results))
    assert parsed[0].get("source_path") == "./a.py"



# ── AC8: min_max_normalize over combined window ───────────────────────────────

def test_min_max_normalize_over_combined_not_per_corpus():
    """Normalization uses combined window, not per-corpus."""
    # code result: distance=0.1, doc result: distance=0.9
    # Combined window min=0.1, max=0.9
    distances = [0.1, 0.9]
    norm_code = min_max_normalize(0.1, distances)
    norm_doc = min_max_normalize(0.9, distances)
    assert norm_code == pytest.approx(0.0, abs=1e-6)
    assert norm_doc == pytest.approx(1.0, abs=1e-6)

    # If per-corpus: code [0.1] → both 0.0; docs [0.9] → both 0.0
    # Combined window correctly distinguishes them
    assert norm_code < norm_doc


