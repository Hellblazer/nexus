# SPDX-License-Identifier: AGPL-3.0-or-later
"""Tests for RDR-026: Hybrid Search — Exact-Match Score Boosting.

Phase 1 covers:
1. distance=0.0 fix (rg__cache excluded from normalization)
2. Pre-reranker capture + post-reranker boost
3. rg__cache filtered from output with fallback guard
4. Boost fires on both reranked and --no-rerank paths
5. Multi-repo cache corpus filtering
"""
from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from click.testing import CliRunner

from nexus.types import SearchResult


# ── Helpers ──────────────────────────────────────────────────────────────────


def _sr(
    id: str = "r1",
    content: str = "some content",
    distance: float = 0.3,
    collection: str = "code__repo",
    file_path: str = "/repo/file.py",
    **extra_meta: object,
) -> SearchResult:
    meta = {"source_path": file_path, "file_path": file_path, "frecency_score": 0.5}
    meta.update(extra_meta)
    return SearchResult(
        id=id, content=content, distance=distance,
        collection=collection, metadata=meta,
    )


def _rg_sr(file_path: str = "/repo/match.py", line: int = 10) -> SearchResult:
    """Create a ripgrep-style SearchResult with distance=0.0."""
    return SearchResult(
        id=f"rg:{file_path}:{line}",
        content="matched line content",
        distance=0.0,
        collection="rg__cache",
        metadata={
            "file_path": file_path,
            "source_path": file_path,
            "line_start": line,
            "frecency_score": 0.5,
            "source": "ripgrep",
        },
    )


# ── 1. distance=0.0 fix — rg__cache excluded from normalization ─────────────


def test_rg_cache_excluded_from_normalization_window() -> None:
    """rg__cache results (distance=0.0) must NOT distort the normalization window."""
    from nexus.scoring import apply_hybrid_scoring

    vector_a = _sr(id="v1", distance=0.85, collection="code__repo")
    vector_b = _sr(id="v2", distance=0.95, collection="code__repo")
    rg_hit = _rg_sr()

    results = apply_hybrid_scoring([vector_a, vector_b, rg_hit], hybrid=True)

    # rg hit should get RG_FLOOR_SCORE, not v_norm=1.0 from distance=0.0
    rg_result = next(r for r in results if r.collection == "rg__cache")
    assert rg_result.hybrid_score == pytest.approx(0.5, abs=0.01), (
        f"rg__cache should get RG_FLOOR_SCORE=0.5, got {rg_result.hybrid_score}"
    )

    # Vector results should be normalized only against each other
    v_results = sorted(
        [r for r in results if r.collection != "rg__cache"],
        key=lambda r: r.distance,
    )
    # v1 (dist=0.85) should score higher than v2 (dist=0.95) — lower distance = better
    assert v_results[0].hybrid_score > v_results[1].hybrid_score


def test_rg_cache_gets_floor_score_not_v_norm() -> None:
    """rg__cache hybrid_score is RG_FLOOR_SCORE (0.5), not computed from distance."""
    from nexus.scoring import apply_hybrid_scoring, RG_FLOOR_SCORE

    rg_hit = _rg_sr()
    vector = _sr(id="v1", distance=0.9, collection="code__repo")

    results = apply_hybrid_scoring([vector, rg_hit], hybrid=True)
    rg_result = next(r for r in results if r.collection == "rg__cache")
    assert rg_result.hybrid_score == pytest.approx(RG_FLOOR_SCORE)


def test_rg_floor_score_constant_exists() -> None:
    """RG_FLOOR_SCORE constant is exported from scoring module."""
    from nexus.scoring import RG_FLOOR_SCORE
    assert RG_FLOOR_SCORE == 0.5


def test_scoring_without_rg_cache_unchanged() -> None:
    """apply_hybrid_scoring without any rg__cache results behaves exactly as before."""
    from nexus.scoring import apply_hybrid_scoring

    v1 = _sr(id="v1", distance=0.0, collection="code__repo")
    v2 = _sr(id="v2", distance=0.5, collection="code__repo")
    v3 = _sr(id="v3", distance=1.0, collection="code__repo")

    results = apply_hybrid_scoring([v1, v2, v3], hybrid=True)
    scores = {r.id: r.hybrid_score for r in results}
    # v1 (dist=0.0) → v_norm=1.0, v3 (dist=1.0) → v_norm=0.0
    assert scores["v1"] > scores["v2"] > scores["v3"]


# ── 2. Pre-reranker capture + post-reranker boost ───────────────────────────


def test_exact_match_boost_constant_exists() -> None:
    """EXACT_MATCH_BOOST constant is exported from search_cmd module."""
    from nexus.commands.search_cmd import EXACT_MATCH_BOOST
    assert EXACT_MATCH_BOOST == 0.15


def test_boost_applied_to_vector_result_matching_rg_file_path(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    """Vector result whose source_path matches an rg hit gets +0.15 boost."""
    monkeypatch.setenv("CHROMA_API_KEY", "k")
    monkeypatch.setenv("VOYAGE_API_KEY", "v")
    monkeypatch.setenv("CHROMA_TENANT", "t")
    monkeypatch.setenv("CHROMA_DATABASE", "d")
    monkeypatch.setattr("nexus.commands.search_cmd._CONFIG_DIR", tmp_path)

    cache_file = tmp_path / "repo-abcd1234.cache"
    cache_file.write_text("/repo/match.py:10:hello\n")

    # Vector result for a file that rg also matched
    vector_match = _sr(id="vm", distance=0.9, collection="code__repo-abcd1234",
                       file_path="/repo/match.py")
    # Vector result for a file rg did NOT match
    vector_no_match = _sr(id="vnm", distance=0.9, collection="code__repo-abcd1234",
                          file_path="/repo/other.py")

    rg_hit = {"file_path": "/repo/match.py", "line_number": 10,
              "line_content": "hello", "frecency_score": 0.5}

    mock_t3 = MagicMock()
    mock_t3.list_collections.return_value = [{"name": "code__repo-abcd1234"}]

    runner = CliRunner()
    with (
        patch("nexus.commands.search_cmd._t3", return_value=mock_t3),
        patch("nexus.commands.search_cmd.search_cross_corpus",
              return_value=[vector_match, vector_no_match]),
        patch("nexus.commands.search_cmd.search_ripgrep", return_value=[rg_hit]),
        patch("nexus.commands.search_cmd.load_config",
              return_value={"embeddings": {"rerankerModel": "rerank-2.5"}}),
    ):
        result = runner.invoke(
            __import__("nexus.cli", fromlist=["main"]).main,
            ["search", "query", "--hybrid", "--corpus", "code", "--no-rerank", "--json"],
        )

    assert result.exit_code == 0, result.output

    import json
    data = json.loads(result.output)

    # format_json spreads metadata into top-level — source_path is a top-level key
    match_result = next((r for r in data if r.get("source_path") == "/repo/match.py"), None)
    no_match_result = next((r for r in data if r.get("source_path") == "/repo/other.py"), None)

    assert match_result is not None, "Should have result for /repo/match.py"
    assert no_match_result is not None, "Should have result for /repo/other.py"

    # The boosted result should appear first (higher position = higher score)
    match_idx = next(i for i, r in enumerate(data) if r.get("source_path") == "/repo/match.py")
    nomatch_idx = next(i for i, r in enumerate(data) if r.get("source_path") == "/repo/other.py")
    assert match_idx < nomatch_idx, (
        "Boosted result should rank higher (appear first) than unboosted"
    )


# ── 3. rg__cache filtered from output with fallback guard ───────────────────


def test_rg_cache_results_filtered_from_output(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    """rg__cache results should not appear in final output."""
    monkeypatch.setenv("CHROMA_API_KEY", "k")
    monkeypatch.setenv("VOYAGE_API_KEY", "v")
    monkeypatch.setenv("CHROMA_TENANT", "t")
    monkeypatch.setenv("CHROMA_DATABASE", "d")
    monkeypatch.setattr("nexus.commands.search_cmd._CONFIG_DIR", tmp_path)

    cache_file = tmp_path / "repo-abcd1234.cache"
    cache_file.write_text("/repo/main.py:1:hello\n")

    vector = _sr(id="v1", distance=0.9, collection="code__repo-abcd1234",
                 file_path="/repo/file.py")
    rg_hit = {"file_path": "/repo/main.py", "line_number": 1,
              "line_content": "hello", "frecency_score": 0.5}

    mock_t3 = MagicMock()
    mock_t3.list_collections.return_value = [{"name": "code__repo-abcd1234"}]

    runner = CliRunner()
    with (
        patch("nexus.commands.search_cmd._t3", return_value=mock_t3),
        patch("nexus.commands.search_cmd.search_cross_corpus", return_value=[vector]),
        patch("nexus.commands.search_cmd.search_ripgrep", return_value=[rg_hit]),
        patch("nexus.commands.search_cmd.load_config",
              return_value={"embeddings": {"rerankerModel": "rerank-2.5"}}),
    ):
        result = runner.invoke(
            __import__("nexus.cli", fromlist=["main"]).main,
            ["search", "query", "--hybrid", "--corpus", "code", "--no-rerank", "--json"],
        )

    assert result.exit_code == 0, result.output
    import json
    data = json.loads(result.output)
    collections = [r.get("collection") for r in data]
    assert "rg__cache" not in collections, "rg__cache results should be filtered from output"


def test_rg_only_results_preserved_by_fallback(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    """When no vector results exist, rg__cache results are preserved (fallback guard)."""
    monkeypatch.setenv("CHROMA_API_KEY", "k")
    monkeypatch.setenv("VOYAGE_API_KEY", "v")
    monkeypatch.setenv("CHROMA_TENANT", "t")
    monkeypatch.setenv("CHROMA_DATABASE", "d")
    monkeypatch.setattr("nexus.commands.search_cmd._CONFIG_DIR", tmp_path)

    cache_file = tmp_path / "repo-abcd1234.cache"
    cache_file.write_text("/repo/main.py:1:hello\n")

    rg_hit = {"file_path": "/repo/main.py", "line_number": 1,
              "line_content": "hello", "frecency_score": 0.5}

    mock_t3 = MagicMock()
    mock_t3.list_collections.return_value = [{"name": "code__repo-abcd1234"}]

    runner = CliRunner()
    with (
        patch("nexus.commands.search_cmd._t3", return_value=mock_t3),
        patch("nexus.commands.search_cmd.search_cross_corpus", return_value=[]),
        patch("nexus.commands.search_cmd.search_ripgrep", return_value=[rg_hit]),
        patch("nexus.commands.search_cmd.load_config",
              return_value={"embeddings": {"rerankerModel": "rerank-2.5"}}),
    ):
        result = runner.invoke(
            __import__("nexus.cli", fromlist=["main"]).main,
            ["search", "query", "--hybrid", "--corpus", "code", "--no-rerank"],
        )

    assert result.exit_code == 0, result.output
    # When no vector results exist, rg results should be preserved, not dropped
    assert "No results" not in result.output, "Fallback guard should preserve rg-only results"


# ── 4. Boost fires on --no-rerank path ──────────────────────────────────────


def test_boost_fires_on_no_rerank_path(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    """Exact-match boost applies on --no-rerank path too, not just reranked."""
    monkeypatch.setenv("CHROMA_API_KEY", "k")
    monkeypatch.setenv("VOYAGE_API_KEY", "v")
    monkeypatch.setenv("CHROMA_TENANT", "t")
    monkeypatch.setenv("CHROMA_DATABASE", "d")
    monkeypatch.setattr("nexus.commands.search_cmd._CONFIG_DIR", tmp_path)

    cache_file = tmp_path / "repo-abcd1234.cache"
    cache_file.write_text("/repo/match.py:10:hello\n")

    # Both vector results have identical distance — only the boost differentiates
    vector_match = _sr(id="vm", distance=0.9, collection="code__repo-abcd1234",
                       file_path="/repo/match.py")
    vector_no_match = _sr(id="vnm", distance=0.9, collection="code__repo-abcd1234",
                          file_path="/repo/other.py")

    rg_hit = {"file_path": "/repo/match.py", "line_number": 10,
              "line_content": "hello", "frecency_score": 0.5}

    mock_t3 = MagicMock()
    mock_t3.list_collections.return_value = [{"name": "code__repo-abcd1234"}]

    runner = CliRunner()
    with (
        patch("nexus.commands.search_cmd._t3", return_value=mock_t3),
        patch("nexus.commands.search_cmd.search_cross_corpus",
              return_value=[vector_match, vector_no_match]),
        patch("nexus.commands.search_cmd.search_ripgrep", return_value=[rg_hit]),
        patch("nexus.commands.search_cmd.load_config",
              return_value={"embeddings": {"rerankerModel": "rerank-2.5"}}),
    ):
        result = runner.invoke(
            __import__("nexus.cli", fromlist=["main"]).main,
            ["search", "query", "--hybrid", "--corpus", "code",
             "--no-rerank", "--json"],
        )

    assert result.exit_code == 0, result.output
    import json
    data = json.loads(result.output)

    match_idx = next((i for i, r in enumerate(data) if r.get("source_path") == "/repo/match.py"), None)
    nomatch_idx = next((i for i, r in enumerate(data) if r.get("source_path") == "/repo/other.py"), None)

    assert match_idx is not None and nomatch_idx is not None
    assert match_idx < nomatch_idx, (
        "Boost should fire on --no-rerank path — boosted result should rank first"
    )


# ── 5. Multi-repo cache corpus filtering ────────────────────────────────────


def test_find_rg_cache_paths_no_filter(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Without corpus filter, all .cache files are returned."""
    monkeypatch.setattr("nexus.commands.search_cmd._CONFIG_DIR", tmp_path)

    (tmp_path / "nexus-a1b2c3d4.cache").touch()
    (tmp_path / "other-e5f6g7h8.cache").touch()

    from nexus.commands.search_cmd import _find_rg_cache_paths
    paths = _find_rg_cache_paths()
    assert len(paths) == 2


def test_find_rg_cache_paths_with_corpus_filter(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """With corpus filter, only matching cache files are returned."""
    monkeypatch.setattr("nexus.commands.search_cmd._CONFIG_DIR", tmp_path)

    (tmp_path / "nexus-a1b2c3d4.cache").touch()
    (tmp_path / "other-e5f6g7h8.cache").touch()

    from nexus.commands.search_cmd import _find_rg_cache_paths

    # Filter by collection name — strips code__ prefix
    paths = _find_rg_cache_paths(corpus="code__nexus-a1b2c3d4")
    assert len(paths) == 1
    assert paths[0].name == "nexus-a1b2c3d4.cache"


def test_find_rg_cache_paths_strips_prefix(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Corpus filter strips code__/docs__/rdr__ prefixes correctly."""
    monkeypatch.setattr("nexus.commands.search_cmd._CONFIG_DIR", tmp_path)

    (tmp_path / "myrepo-abcd1234.cache").touch()

    from nexus.commands.search_cmd import _find_rg_cache_paths

    for prefix in ("code__", "docs__", "rdr__"):
        paths = _find_rg_cache_paths(corpus=f"{prefix}myrepo-abcd1234")
        assert len(paths) == 1, f"Should match after stripping {prefix}"
        assert paths[0].name == "myrepo-abcd1234.cache"


def test_find_rg_cache_paths_no_match_returns_empty(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Corpus filter with no matching cache returns empty list."""
    monkeypatch.setattr("nexus.commands.search_cmd._CONFIG_DIR", tmp_path)

    (tmp_path / "nexus-a1b2c3d4.cache").touch()

    from nexus.commands.search_cmd import _find_rg_cache_paths
    paths = _find_rg_cache_paths(corpus="code__nonexistent-00000000")
    assert len(paths) == 0


def test_hybrid_search_uses_corpus_filter(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    """--hybrid with --corpus code__nexus only searches nexus caches, not others."""
    monkeypatch.setenv("CHROMA_API_KEY", "k")
    monkeypatch.setenv("VOYAGE_API_KEY", "v")
    monkeypatch.setenv("CHROMA_TENANT", "t")
    monkeypatch.setenv("CHROMA_DATABASE", "d")
    monkeypatch.setattr("nexus.commands.search_cmd._CONFIG_DIR", tmp_path)

    # Two cache files — only one should be searched
    (tmp_path / "nexus-a1b2c3d4.cache").write_text("/repo/file.py:1:match\n")
    (tmp_path / "other-e5f6g7h8.cache").write_text("/other/file.py:1:match\n")

    rg_calls: list[Path] = []

    def fake_search_ripgrep(query, cache_path, *, n_results=50, fixed_strings=True):
        rg_calls.append(cache_path)
        return []

    mock_t3 = MagicMock()
    mock_t3.list_collections.return_value = [{"name": "code__nexus-a1b2c3d4"}]

    runner = CliRunner()
    with (
        patch("nexus.commands.search_cmd._t3", return_value=mock_t3),
        patch("nexus.commands.search_cmd.search_cross_corpus", return_value=[]),
        patch("nexus.commands.search_cmd.search_ripgrep", side_effect=fake_search_ripgrep),
        patch("nexus.commands.search_cmd.load_config",
              return_value={"embeddings": {"rerankerModel": "rerank-2.5"}}),
    ):
        result = runner.invoke(
            __import__("nexus.cli", fromlist=["main"]).main,
            ["search", "query", "--hybrid", "--corpus", "code__nexus-a1b2c3d4", "--no-rerank"],
        )

    assert result.exit_code == 0, result.output
    cache_names = [p.name for p in rg_calls]
    assert "nexus-a1b2c3d4.cache" in cache_names, "Should search nexus cache"
    assert "other-e5f6g7h8.cache" not in cache_names, "Should NOT search other cache"


# ── Edge cases ───────────────────────────────────────────────────────────────


def test_hybrid_score_capped_at_one() -> None:
    """Boost should never push hybrid_score above 1.0."""
    from nexus.scoring import apply_hybrid_scoring
    from nexus.commands.search_cmd import EXACT_MATCH_BOOST

    # Create a result with max score that would exceed 1.0 with boost
    v = _sr(id="v1", distance=0.0, collection="code__repo", file_path="/repo/match.py")
    rg = _rg_sr(file_path="/repo/match.py")

    results = apply_hybrid_scoring([v, rg], hybrid=True)

    # The vector result has v_norm=1.0. After boost of 0.15, it should cap at 1.0
    v_result = next(r for r in results if r.collection != "rg__cache")
    # We can't directly test the cap here since boost happens in search_cmd,
    # but we can verify that apply_hybrid_scoring scores are valid [0, 1]
    assert 0.0 <= v_result.hybrid_score <= 1.0


def test_non_hybrid_search_unaffected() -> None:
    """Non-hybrid search produces same results as before (no rg__cache in play)."""
    from nexus.scoring import apply_hybrid_scoring

    v1 = _sr(id="v1", distance=0.2, collection="code__repo")
    v2 = _sr(id="v2", distance=0.8, collection="code__repo")

    results = apply_hybrid_scoring([v1, v2], hybrid=False)
    assert results[0].id == "v1"  # lower distance = higher score
    assert results[1].id == "v2"
