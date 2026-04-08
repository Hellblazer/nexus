# SPDX-License-Identifier: AGPL-3.0-or-later
from __future__ import annotations

import json
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
    return SearchResult(
        id=f"rg:{file_path}:{line}",
        content="matched line content",
        distance=0.0,
        collection="rg__cache",
        metadata={
            "file_path": file_path, "source_path": file_path,
            "line_start": line, "frecency_score": 0.5, "source": "ripgrep",
        },
    )


def _rg_dict(file_path: str = "/repo/match.py", line: int = 10,
             content: str = "hello", frecency: float = 0.5) -> dict:
    return {"file_path": file_path, "line_number": line,
            "line_content": content, "frecency_score": frecency}


def _cli_main():
    return __import__("nexus.cli", fromlist=["main"]).main


# ── Fixtures ─────────────────────────────────────────────────────────────────


@pytest.fixture()
def cli_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    """Set up env vars and CONFIG_DIR for CLI integration tests."""
    for k, v in [("CHROMA_API_KEY", "k"), ("VOYAGE_API_KEY", "v"),
                 ("CHROMA_TENANT", "t"), ("CHROMA_DATABASE", "d")]:
        monkeypatch.setenv(k, v)
    monkeypatch.setattr("nexus.commands.search_cmd._CONFIG_DIR", tmp_path)
    return tmp_path


@pytest.fixture()
def mock_t3():
    t3 = MagicMock()
    t3.list_collections.return_value = [{"name": "code__repo-abcd1234"}]
    return t3


def _run_hybrid_cli(cli_env, mock_t3, *, vectors=None, rg_hits=None,
                    extra_args=(), config_extra=None, cache_lines=""):
    """Run hybrid search CLI with standard mock setup. Returns CliRunner result."""
    if cache_lines:
        (cli_env / "repo-abcd1234.cache").write_text(cache_lines)

    cfg = {"embeddings": {"rerankerModel": "rerank-2.5"}}
    if config_extra:
        cfg.update(config_extra)

    runner = CliRunner()
    with (
        patch("nexus.commands.search_cmd._t3", return_value=mock_t3),
        patch("nexus.commands.search_cmd.search_cross_corpus",
              return_value=vectors or []),
        patch("nexus.commands.search_cmd.search_ripgrep",
              return_value=rg_hits or []),
        patch("nexus.commands.search_cmd.load_config", return_value=cfg),
    ):
        args = ["search", "query", "--hybrid", "--corpus", "code",
                "--no-rerank", *extra_args]
        return runner.invoke(_cli_main(), args)


# ── Constants ────────────────────────────────────────────────────────────────


@pytest.mark.parametrize("module,attr,expected", [
    ("nexus.scoring", "RG_FLOOR_SCORE", 0.5),
    ("nexus.commands.search_cmd", "EXACT_MATCH_BOOST", 0.15),
    ("nexus.commands.search_cmd", "RG_ONLY_PENALTY", 0.8),
])
def test_constants(module, attr, expected):
    mod = __import__(module, fromlist=[attr])
    assert getattr(mod, attr) == expected


# ── Scoring unit tests ───────────────────────────────────────────────────────


def test_rg_cache_excluded_from_normalization_window():
    from nexus.scoring import apply_hybrid_scoring

    va = _sr(id="v1", distance=0.85, collection="code__repo")
    vb = _sr(id="v2", distance=0.95, collection="code__repo")
    results = apply_hybrid_scoring([va, vb, _rg_sr()], hybrid=True)

    rg_result = next(r for r in results if r.collection == "rg__cache")
    assert rg_result.hybrid_score == pytest.approx(0.5, abs=0.01)

    v_results = sorted(
        [r for r in results if r.collection != "rg__cache"],
        key=lambda r: r.distance,
    )
    assert v_results[0].hybrid_score > v_results[1].hybrid_score


def test_rg_cache_gets_floor_score():
    from nexus.scoring import RG_FLOOR_SCORE, apply_hybrid_scoring

    results = apply_hybrid_scoring(
        [_sr(id="v1", distance=0.9, collection="code__repo"), _rg_sr()],
        hybrid=True,
    )
    rg_result = next(r for r in results if r.collection == "rg__cache")
    assert rg_result.hybrid_score == pytest.approx(RG_FLOOR_SCORE)


def test_scoring_without_rg_cache_unchanged():
    from nexus.scoring import apply_hybrid_scoring

    results = apply_hybrid_scoring([
        _sr(id="v1", distance=0.0, collection="code__repo"),
        _sr(id="v2", distance=0.5, collection="code__repo"),
        _sr(id="v3", distance=1.0, collection="code__repo"),
    ], hybrid=True)
    scores = {r.id: r.hybrid_score for r in results}
    assert scores["v1"] > scores["v2"] > scores["v3"]


def test_hybrid_score_in_valid_range():
    from nexus.scoring import apply_hybrid_scoring

    v = _sr(id="v1", distance=0.0, collection="code__repo", file_path="/repo/match.py")
    results = apply_hybrid_scoring([v, _rg_sr(file_path="/repo/match.py")], hybrid=True)
    v_result = next(r for r in results if r.collection != "rg__cache")
    assert 0.0 <= v_result.hybrid_score <= 1.0


def test_non_hybrid_search_unaffected():
    from nexus.scoring import apply_hybrid_scoring

    results = apply_hybrid_scoring([
        _sr(id="v1", distance=0.2, collection="code__repo"),
        _sr(id="v2", distance=0.8, collection="code__repo"),
    ], hybrid=False)
    assert results[0].id == "v1"
    assert results[1].id == "v2"


def test_rg_no_vector_overlap_no_crash():
    from nexus.scoring import apply_hybrid_scoring

    results = apply_hybrid_scoring(
        [_rg_sr(file_path="/repo/a.py"), _rg_sr(file_path="/repo/b.py")],
        hybrid=True,
    )
    assert len(results) == 2
    for r in results:
        assert r.hybrid_score == pytest.approx(0.5)


# ── Boost math ───────────────────────────────────────────────────────────────


@pytest.mark.parametrize("base,expected", [(0.6, 0.75), (0.9, 1.0)])
def test_boost_math(base, expected):
    from nexus.commands.search_cmd import EXACT_MATCH_BOOST
    assert min(1.0, base + EXACT_MATCH_BOOST) == pytest.approx(expected)


# ── Cache path discovery ────────────────────────────────────────────────────


@pytest.fixture()
def cache_dir(tmp_path, monkeypatch):
    monkeypatch.setattr("nexus.commands.search_cmd._CONFIG_DIR", tmp_path)
    (tmp_path / "nexus-a1b2c3d4.cache").touch()
    (tmp_path / "other-e5f6g7h8.cache").touch()
    return tmp_path


def test_find_rg_cache_paths_no_filter(cache_dir):
    from nexus.commands.search_cmd import _find_rg_cache_paths
    assert len(_find_rg_cache_paths()) == 2


def test_find_rg_cache_paths_with_corpus_filter(cache_dir):
    from nexus.commands.search_cmd import _find_rg_cache_paths
    paths = _find_rg_cache_paths(corpus="code__nexus-a1b2c3d4")
    assert len(paths) == 1
    assert paths[0].name == "nexus-a1b2c3d4.cache"


@pytest.mark.parametrize("prefix", ["code__", "docs__", "rdr__"])
def test_find_rg_cache_paths_strips_prefix(tmp_path, monkeypatch, prefix):
    monkeypatch.setattr("nexus.commands.search_cmd._CONFIG_DIR", tmp_path)
    (tmp_path / "myrepo-abcd1234.cache").touch()
    from nexus.commands.search_cmd import _find_rg_cache_paths
    paths = _find_rg_cache_paths(corpus=f"{prefix}myrepo-abcd1234")
    assert len(paths) == 1
    assert paths[0].name == "myrepo-abcd1234.cache"


def test_find_rg_cache_paths_no_match(cache_dir):
    from nexus.commands.search_cmd import _find_rg_cache_paths
    assert len(_find_rg_cache_paths(corpus="code__nonexistent-00000000")) == 0


# ── CLI integration: boost ordering ─────────────────────────────────────────


@pytest.mark.parametrize("test_id", ["boost_applied", "boost_no_rerank"])
def test_boost_ranks_matching_file_higher(cli_env, mock_t3, test_id):
    vm = _sr(id="vm", distance=0.9, collection="code__repo-abcd1234",
             file_path="/repo/match.py")
    vnm = _sr(id="vnm", distance=0.9, collection="code__repo-abcd1234",
              file_path="/repo/other.py")
    result = _run_hybrid_cli(
        cli_env, mock_t3,
        vectors=[vm, vnm],
        rg_hits=[_rg_dict()],
        extra_args=["--json"],
        cache_lines="/repo/match.py:10:hello\n",
    )
    assert result.exit_code == 0, result.output
    data = json.loads(result.output)
    match_idx = next(i for i, r in enumerate(data) if r.get("source_path") == "/repo/match.py")
    nomatch_idx = next(i for i, r in enumerate(data) if r.get("source_path") == "/repo/other.py")
    assert match_idx < nomatch_idx


# ── CLI integration: rg filtering ────────────────────────────────────────────


def test_rg_signal_filtered_from_output(cli_env, mock_t3):
    vector = _sr(id="v1", distance=0.9, collection="code__repo-abcd1234",
                 file_path="/repo/file.py")
    result = _run_hybrid_cli(
        cli_env, mock_t3,
        vectors=[vector],
        rg_hits=[_rg_dict(file_path="/repo/file.py", line=1)],
        extra_args=["--json"],
        cache_lines="/repo/file.py:1:hello\n",
        config_extra={"search": {"hybrid_default": False}},
    )
    assert result.exit_code == 0, result.output
    data = json.loads(result.output)
    assert len(data) == 1
    assert data[0].get("collection") == "code__repo-abcd1234"


def test_rg_only_preserved_when_no_vectors(cli_env, mock_t3):
    result = _run_hybrid_cli(
        cli_env, mock_t3,
        rg_hits=[_rg_dict(file_path="/repo/main.py", line=1)],
        cache_lines="/repo/main.py:1:hello\n",
    )
    assert result.exit_code == 0, result.output
    assert "No results" not in result.output


# ── CLI integration: corpus filter ───────────────────────────────────────────


def test_hybrid_search_uses_corpus_filter(cli_env, mock_t3):
    (cli_env / "nexus-a1b2c3d4.cache").write_text("/repo/file.py:1:match\n")
    (cli_env / "other-e5f6g7h8.cache").write_text("/other/file.py:1:match\n")

    rg_calls: list[Path] = []

    def fake_rg(query, cache_path, *, n_results=50, fixed_strings=True, timeout=10):
        rg_calls.append(cache_path)
        return []

    mock_t3.list_collections.return_value = [{"name": "code__nexus-a1b2c3d4"}]

    runner = CliRunner()
    with (
        patch("nexus.commands.search_cmd._t3", return_value=mock_t3),
        patch("nexus.commands.search_cmd.search_cross_corpus", return_value=[]),
        patch("nexus.commands.search_cmd.search_ripgrep", side_effect=fake_rg),
        patch("nexus.commands.search_cmd.load_config",
              return_value={"embeddings": {"rerankerModel": "rerank-2.5"}}),
    ):
        result = runner.invoke(
            _cli_main(),
            ["search", "query", "--hybrid", "--corpus", "code__nexus-a1b2c3d4",
             "--no-rerank"],
        )
    assert result.exit_code == 0, result.output
    names = [p.name for p in rg_calls]
    assert "nexus-a1b2c3d4.cache" in names
    assert "other-e5f6g7h8.cache" not in names


# ── CLI integration: multiple rg hits same file ──────────────────────────────


def test_multiple_rg_hits_same_file_boost_once(cli_env, mock_t3):
    vector = _sr(id="v1", distance=0.9, collection="code__repo-abcd1234",
                 file_path="/repo/match.py")
    result = _run_hybrid_cli(
        cli_env, mock_t3,
        vectors=[vector],
        rg_hits=[_rg_dict(line=10), _rg_dict(line=20, content="hello again", frecency=0.4)],
        extra_args=["--json"],
        cache_lines="/repo/match.py:10:hello\n/repo/match.py:20:hello again\n",
        config_extra={"search": {"hybrid_default": False}},
    )
    assert result.exit_code == 0, result.output
    data = json.loads(result.output)
    assert len(data) == 1
    assert data[0].get("source_path") == "/repo/match.py"


# ── CLI integration: hybrid_default config ───────────────────────────────────


@pytest.mark.parametrize("hybrid_default,expect_rg_called", [(True, True), (False, False)])
def test_hybrid_default_config(cli_env, mock_t3, hybrid_default, expect_rg_called):
    (cli_env / "repo-abcd1234.cache").write_text("/repo/match.py:10:hello\n")
    rg_calls: list[int] = []

    def fake_rg(query, cache_path, *, n_results=50, fixed_strings=True, timeout=10):
        rg_calls.append(1)
        return []

    runner = CliRunner()
    with (
        patch("nexus.commands.search_cmd._t3", return_value=mock_t3),
        patch("nexus.commands.search_cmd.search_cross_corpus", return_value=[]),
        patch("nexus.commands.search_cmd.search_ripgrep", side_effect=fake_rg),
        patch("nexus.commands.search_cmd.load_config",
              return_value={"embeddings": {"rerankerModel": "rerank-2.5"},
                            "search": {"hybrid_default": hybrid_default}}),
    ):
        # No --hybrid flag — relies on config
        result = runner.invoke(
            _cli_main(),
            ["search", "query", "--corpus", "code", "--no-rerank"],
        )
    assert result.exit_code == 0, result.output
    assert (len(rg_calls) >= 1) == expect_rg_called


# ── CLI integration: rg-only promotion ───────────────────────────────────────


def test_rg_only_files_promoted_with_penalty(cli_env, mock_t3):
    vector = _sr(id="v1", distance=0.9, collection="code__repo-abcd1234",
                 file_path="/repo/vector_file.py")
    result = _run_hybrid_cli(
        cli_env, mock_t3,
        vectors=[vector],
        rg_hits=[_rg_dict(file_path="/repo/rg_only.py", line=5, content="exact match")],
        extra_args=["--json"],
        cache_lines="/repo/rg_only.py:5:exact match\n",
        config_extra={"search": {"hybrid_default": False}},
    )
    assert result.exit_code == 0, result.output
    data = json.loads(result.output)
    paths = [r.get("source_path", r.get("file_path")) for r in data]
    assert "/repo/rg_only.py" in paths
    assert "/repo/vector_file.py" in paths


def test_rg_only_deduped_per_file(cli_env, mock_t3):
    result = _run_hybrid_cli(
        cli_env, mock_t3,
        rg_hits=[
            _rg_dict(file_path="/repo/rg_only.py", line=5, content="match1"),
            _rg_dict(file_path="/repo/rg_only.py", line=10, content="match2", frecency=0.4),
        ],
        extra_args=["--json"],
        cache_lines="/repo/rg_only.py:5:match1\n/repo/rg_only.py:10:match2\n",
        config_extra={"search": {"hybrid_default": False}},
    )
    assert result.exit_code == 0, result.output
    lines = result.output.splitlines()
    json_start = next(i for i, ln in enumerate(lines) if ln.strip() == "[")
    data = json.loads("\n".join(lines[json_start:]))
    rg_only = [r for r in data if r.get("file_path") == "/repo/rg_only.py"]
    assert len(rg_only) == 1


# ── CLI integration: rg_matched_lines metadata ──────────────────────────────


def test_boosted_result_has_rg_matched_lines(cli_env, mock_t3):
    vector = _sr(id="v1", distance=0.9, collection="code__repo-abcd1234",
                 file_path="/repo/match.py")
    result = _run_hybrid_cli(
        cli_env, mock_t3,
        vectors=[vector],
        rg_hits=[_rg_dict(line=10), _rg_dict(line=25, content="world", frecency=0.4)],
        extra_args=["--json"],
        cache_lines="/repo/match.py:10:hello\n/repo/match.py:25:world\n",
        config_extra={"search": {"hybrid_default": False}},
    )
    assert result.exit_code == 0, result.output
    data = json.loads(result.output)
    assert len(data) == 1
    assert data[0].get("rg_matched_lines") == [10, 25]
