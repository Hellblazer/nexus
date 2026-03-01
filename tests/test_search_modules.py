"""Tests verifying the module split: scoring.py, formatters.py, search_engine.py.

These tests import directly from the canonical modules to confirm correct placement.
"""
import pytest

from nexus.types import SearchResult


# ── Canonical module import paths ────────────────────────────────────────────

def test_scoring_imports():
    """All scoring functions importable from nexus.scoring."""
    from nexus.scoring import (  # noqa: F401
        min_max_normalize,
        hybrid_score,
        apply_hybrid_scoring,
        rerank_results,
        round_robin_interleave,
    )


def test_formatters_imports():
    """All formatter functions importable from nexus.formatters."""
    from nexus.formatters import (  # noqa: F401
        format_vimgrep,
        format_json,
        format_plain,
        format_plain_with_context,
    )


def test_search_engine_orchestration_imports():
    """Orchestration functions importable from nexus.search_engine."""
    from nexus.search_engine import (  # noqa: F401
        search_cross_corpus,
        fetch_mxbai_results,
    )


def test_search_result_from_types():
    """SearchResult importable from nexus.types (canonical path)."""
    from nexus.types import SearchResult  # noqa: F401


# ── Functional spot-checks on canonical import paths ─────────────────────────

def test_scoring_min_max_normalize_works():
    """min_max_normalize from nexus.scoring returns correct values."""
    from nexus.scoring import min_max_normalize
    values = [1.0, 3.0, 5.0]
    assert min_max_normalize(1.0, values) == pytest.approx(0.0, abs=1e-6)
    assert min_max_normalize(5.0, values) == pytest.approx(1.0, abs=1e-6)


def test_scoring_hybrid_score_works():
    """hybrid_score from nexus.scoring computes correct weighted sum."""
    from nexus.scoring import hybrid_score
    assert hybrid_score(0.8, 0.5) == pytest.approx(0.71, abs=1e-6)


def test_scoring_round_robin_interleave_works():
    """round_robin_interleave from nexus.scoring alternates correctly."""
    from nexus.scoring import round_robin_interleave
    a = [SearchResult(id="a1", content="a", distance=0.1, collection="c", metadata={})]
    b = [SearchResult(id="b1", content="b", distance=0.2, collection="d", metadata={})]
    result = round_robin_interleave([a, b])
    assert [r.id for r in result] == ["a1", "b1"]


def test_formatters_format_vimgrep_works():
    """format_vimgrep from nexus.formatters produces correct output."""
    from nexus.formatters import format_vimgrep
    results = [
        SearchResult(
            id="1",
            content="def foo():",
            distance=0.1,
            collection="code__r",
            metadata={"source_path": "./foo.py", "line_start": 10},
        )
    ]
    lines = format_vimgrep(results)
    assert lines == ["./foo.py:10:0:def foo():"]


def test_formatters_format_plain_works():
    """format_plain from nexus.formatters produces correct output."""
    from nexus.formatters import format_plain
    results = [
        SearchResult(
            id="1",
            content="hello\nworld",
            distance=0.1,
            collection="c",
            metadata={"source_path": "f.py", "line_start": 1},
        )
    ]
    lines = format_plain(results)
    assert lines == ["f.py:1:hello", "f.py:2:world"]


def test_formatters_format_json_valid():
    """format_json from nexus.formatters produces valid JSON."""
    import json as _json
    from nexus.formatters import format_json
    results = [
        SearchResult(id="x", content="c", distance=0.5, collection="c", metadata={})
    ]
    parsed = _json.loads(format_json(results))
    assert parsed[0]["id"] == "x"


def test_no_circular_imports():
    """scoring and formatters must not import from search_engine."""
    import importlib
    import sys

    # Save original module state so we can restore it after the test
    saved_modules = dict(sys.modules)

    try:
        # Remove cached nexus modules to force fresh import
        for mod in list(sys.modules.keys()):
            if mod.startswith("nexus."):
                del sys.modules[mod]

        # These should all import cleanly without pulling in search_engine internals
        scoring = importlib.import_module("nexus.scoring")
        formatters = importlib.import_module("nexus.formatters")

        # Verify search_engine is not in their __dict__ as an imported sub-module
        assert not hasattr(scoring, "search_engine"), "scoring must not import search_engine"
        assert not hasattr(formatters, "search_engine"), "formatters must not import search_engine"
    finally:
        # Restore original sys.modules to avoid contaminating subsequent tests
        sys.modules.clear()
        sys.modules.update(saved_modules)


# ── Bug-fix tests ────────────────────────────────────────────────────────────

def test_format_json_metadata_does_not_shadow_canonical_fields():
    """Metadata keys must not overwrite canonical fields (id, content, distance, collection)."""
    import json as _json
    from nexus.formatters import format_json

    results = [
        SearchResult(
            id="canonical-id",
            content="canonical-content",
            distance=0.3,
            collection="code__repo",
            metadata={"id": "EVIL", "content": "EVIL", "distance": 999.0, "collection": "EVIL"},
        )
    ]
    parsed = _json.loads(format_json(results))
    item = parsed[0]
    assert item["id"] == "canonical-id", f"id was overwritten: {item['id']}"
    assert item["content"] == "canonical-content", f"content was overwritten: {item['content']}"
    assert item["distance"] == pytest.approx(0.3), f"distance was overwritten: {item['distance']}"
    assert item["collection"] == "code__repo", f"collection was overwritten: {item['collection']}"


def test_rerank_results_degrades_on_api_error():
    """rerank_results must return original results[:n] when Voyage API raises an exception."""
    from nexus.scoring import rerank_results
    from unittest.mock import patch, MagicMock

    results = [
        SearchResult(id=f"r{i}", content=f"content {i}", distance=float(i) * 0.1,
                     collection="docs__test", metadata={})
        for i in range(5)
    ]

    mock_client = MagicMock()
    mock_client.rerank.side_effect = Exception("API outage")

    with patch("nexus.scoring._voyage_client", return_value=mock_client):
        output = rerank_results(results, query="test query", top_k=3)

    # Must degrade gracefully: return up to n original results, not raise
    assert len(output) <= 3
    assert all(r.id.startswith("r") for r in output)


def test_min_max_normalize_empty_raises():
    """min_max_normalize must raise ValueError when window is empty."""
    from nexus.scoring import min_max_normalize

    with pytest.raises(ValueError, match="non-empty"):
        min_max_normalize(1.0, [])


def test_rerank_results_empty_input_returns_empty():
    """rerank_results with no results must return [] without raising."""
    from nexus.scoring import rerank_results

    assert rerank_results([], query="anything") == []


# ── Gap 4: apply_hybrid_scoring warns when hybrid=True but no code corpus ────

def test_apply_hybrid_scoring_warns_no_code_corpus():
    """When hybrid=True but no code__ corpus, a warning is logged."""
    from unittest.mock import patch as _patch
    from nexus.scoring import apply_hybrid_scoring

    results = [
        SearchResult(
            id="d1",
            content="some doc content",
            distance=0.3,
            collection="docs__test",
            metadata={},
        ),
        SearchResult(
            id="d2",
            content="another doc",
            distance=0.5,
            collection="knowledge__notes",
            metadata={},
        ),
    ]

    with _patch("nexus.scoring._log") as mock_log:
        scored = apply_hybrid_scoring(results, hybrid=True)

    # Warning should have been emitted
    mock_log.warning.assert_called_once()
    assert "no code corpus" in mock_log.warning.call_args[0][0].lower()

    # Results should still be returned (with vector-only scoring)
    assert len(scored) == 2
    assert all(r.hybrid_score >= 0.0 for r in scored)
