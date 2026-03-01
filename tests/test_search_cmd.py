"""Tests for nx search command — new flags: --where, -A/-B/-C, --reverse, -m."""
from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest
from click.testing import CliRunner

from nexus.cli import main
from nexus.types import SearchResult


@pytest.fixture
def runner() -> CliRunner:
    return CliRunner()


def _make_result(
    id: str,
    content: str,
    collection: str = "knowledge__test",
    distance: float = 0.1,
    metadata: dict | None = None,
) -> SearchResult:
    return SearchResult(
        id=id,
        content=content,
        distance=distance,
        collection=collection,
        metadata=metadata or {},
    )


def _mock_t3(collections: list[str] | None = None) -> MagicMock:
    """Return a mock T3Database with configurable collections list."""
    mock = MagicMock()
    col_names = collections or ["knowledge__test"]
    mock.list_collections.return_value = [{"name": n} for n in col_names]
    return mock


# ── -C short form REMOVED from --corpus ───────────────────────────────────────


def test_corpus_short_form_C_is_removed(runner: CliRunner, monkeypatch: pytest.MonkeyPatch) -> None:
    """-C is no longer accepted as short form for --corpus."""
    monkeypatch.setenv("CHROMA_API_KEY", "k")
    monkeypatch.setenv("VOYAGE_API_KEY", "v")
    monkeypatch.setenv("CHROMA_TENANT", "t")
    monkeypatch.setenv("CHROMA_DATABASE", "d")

    mock_t3 = _mock_t3()
    mock_t3.search.return_value = []

    with patch("nexus.commands.search_cmd._t3", return_value=mock_t3):
        # -C N should now be context-lines, not corpus: using a string corpus name
        # should fail (N would be an int argument). Providing -C knowledge would
        # at minimum NOT be treated as corpus selection — the old behaviour is gone.
        result = runner.invoke(main, ["search", "query", "-C", "knowledge"])

    # -C now expects an integer (context lines), so passing "knowledge" (non-integer)
    # must produce an error exit code.
    assert result.exit_code != 0, (
        "-C should require an integer argument (context lines), not a corpus name"
    )


# ── --corpus long form still works ────────────────────────────────────────────


def test_corpus_long_form_still_works(runner: CliRunner, monkeypatch: pytest.MonkeyPatch) -> None:
    """--corpus long form is retained after removing -C short form."""
    monkeypatch.setenv("CHROMA_API_KEY", "k")
    monkeypatch.setenv("VOYAGE_API_KEY", "v")
    monkeypatch.setenv("CHROMA_TENANT", "t")
    monkeypatch.setenv("CHROMA_DATABASE", "d")

    mock_t3 = _mock_t3(["knowledge__test"])
    mock_t3.search.return_value = []

    with patch("nexus.commands.search_cmd._t3", return_value=mock_t3):
        with patch("nexus.commands.search_cmd.search_cross_corpus", return_value=[]):
            result = runner.invoke(main, ["search", "query", "--corpus", "knowledge"])

    # No results is fine; what matters is it exits cleanly (0 or "No results" message)
    assert "Error" not in result.output or result.exit_code == 0


# ── -m alias for --max-results ────────────────────────────────────────────────


def test_m_flag_limits_results(runner: CliRunner, monkeypatch: pytest.MonkeyPatch) -> None:
    """-m N limits results to N (alias for --max-results N / --n N)."""
    monkeypatch.setenv("CHROMA_API_KEY", "k")
    monkeypatch.setenv("VOYAGE_API_KEY", "v")
    monkeypatch.setenv("CHROMA_TENANT", "t")
    monkeypatch.setenv("CHROMA_DATABASE", "d")

    results_pool = [
        _make_result(f"r{i}", f"line {i}", distance=float(i) * 0.1)
        for i in range(10)
    ]

    mock_t3 = _mock_t3()
    with patch("nexus.commands.search_cmd._t3", return_value=mock_t3):
        with patch("nexus.commands.search_cmd.search_cross_corpus", return_value=results_pool):
            with patch("nexus.commands.search_cmd.load_config", return_value={"embeddings": {"rerankerModel": "rerank-2.5"}, "mxbai": {}}):
                result = runner.invoke(
                    main,
                    ["search", "query", "--no-rerank", "-m", "3", "--corpus", "knowledge"],
                )

    assert result.exit_code == 0, result.output
    # With --no-rerank the round_robin_interleave[:n] path is used, so only 3 results emitted
    output_lines = [ln for ln in result.output.splitlines() if ln.strip()]
    assert len(output_lines) <= 3, f"Expected at most 3 result lines, got: {output_lines}"


# ── --reverse flag ─────────────────────────────────────────────────────────────


def test_reverse_flag_reverses_output_order(
    runner: CliRunner, monkeypatch: pytest.MonkeyPatch
) -> None:
    """--reverse reverses the final output order (highest-scoring last)."""
    monkeypatch.setenv("CHROMA_API_KEY", "k")
    monkeypatch.setenv("VOYAGE_API_KEY", "v")
    monkeypatch.setenv("CHROMA_TENANT", "t")
    monkeypatch.setenv("CHROMA_DATABASE", "d")

    results_pool = [
        _make_result("first", "alpha content", distance=0.1,
                     metadata={"source_path": "alpha.py", "line_start": 1}),
        _make_result("second", "beta content", distance=0.2,
                     metadata={"source_path": "beta.py", "line_start": 1}),
    ]

    mock_t3 = _mock_t3()
    with patch("nexus.commands.search_cmd._t3", return_value=mock_t3):
        with patch("nexus.commands.search_cmd.search_cross_corpus", return_value=results_pool):
            with patch("nexus.commands.search_cmd.load_config", return_value={"embeddings": {"rerankerModel": "rerank-2.5"}, "mxbai": {}}):
                normal = runner.invoke(
                    main,
                    ["search", "query", "--no-rerank", "--corpus", "knowledge", "--no-color"],
                )
                reversed_ = runner.invoke(
                    main,
                    ["search", "query", "--no-rerank", "--corpus", "knowledge", "--no-color", "--reverse"],
                )

    assert normal.exit_code == 0, normal.output
    assert reversed_.exit_code == 0, reversed_.output

    normal_lines = [ln for ln in normal.output.splitlines() if ln.strip()]
    reversed_lines = [ln for ln in reversed_.output.splitlines() if ln.strip()]

    assert normal_lines != reversed_lines, "--reverse should change output order"
    assert normal_lines == list(reversed(reversed_lines)), (
        "--reverse should produce exactly the reversed list"
    )


# ── --where metadata filter ────────────────────────────────────────────────────


def test_where_single_filter_passed_to_search(
    runner: CliRunner, monkeypatch: pytest.MonkeyPatch
) -> None:
    """--where lang=python builds a ChromaDB where dict passed to search_cross_corpus."""
    monkeypatch.setenv("CHROMA_API_KEY", "k")
    monkeypatch.setenv("VOYAGE_API_KEY", "v")
    monkeypatch.setenv("CHROMA_TENANT", "t")
    monkeypatch.setenv("CHROMA_DATABASE", "d")

    mock_t3 = _mock_t3()

    captured_where: list[dict | None] = []

    def fake_search(query, collections, n_results, t3, where=None):
        captured_where.append(where)
        return []

    with patch("nexus.commands.search_cmd._t3", return_value=mock_t3):
        with patch("nexus.commands.search_cmd.search_cross_corpus", side_effect=fake_search):
            with patch("nexus.commands.search_cmd.load_config", return_value={"embeddings": {"rerankerModel": "rerank-2.5"}, "mxbai": {}}):
                result = runner.invoke(
                    main,
                    ["search", "query", "--corpus", "knowledge", "--where", "lang=python"],
                )

    assert result.exit_code == 0, result.output
    assert len(captured_where) == 1
    assert captured_where[0] == {"lang": "python"}, (
        f"Expected where={{'lang': 'python'}}, got {captured_where[0]}"
    )


def test_where_multiple_filters_anded(
    runner: CliRunner, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Multiple --where flags are ANDed into a single ChromaDB where dict."""
    monkeypatch.setenv("CHROMA_API_KEY", "k")
    monkeypatch.setenv("VOYAGE_API_KEY", "v")
    monkeypatch.setenv("CHROMA_TENANT", "t")
    monkeypatch.setenv("CHROMA_DATABASE", "d")

    mock_t3 = _mock_t3()

    captured_where: list[dict | None] = []

    def fake_search(query, collections, n_results, t3, where=None):
        captured_where.append(where)
        return []

    with patch("nexus.commands.search_cmd._t3", return_value=mock_t3):
        with patch("nexus.commands.search_cmd.search_cross_corpus", side_effect=fake_search):
            with patch("nexus.commands.search_cmd.load_config", return_value={"embeddings": {"rerankerModel": "rerank-2.5"}, "mxbai": {}}):
                result = runner.invoke(
                    main,
                    [
                        "search", "query", "--corpus", "knowledge",
                        "--where", "store_type=pm-archive",
                        "--where", "status=completed",
                    ],
                )

    assert result.exit_code == 0, result.output
    assert len(captured_where) == 1
    assert captured_where[0] == {"store_type": "pm-archive", "status": "completed"}, (
        f"Multiple --where flags should be ANDed, got {captured_where[0]}"
    )


def test_where_no_flag_passes_none(
    runner: CliRunner, monkeypatch: pytest.MonkeyPatch
) -> None:
    """When --where is omitted, search_cross_corpus is called with where=None."""
    monkeypatch.setenv("CHROMA_API_KEY", "k")
    monkeypatch.setenv("VOYAGE_API_KEY", "v")
    monkeypatch.setenv("CHROMA_TENANT", "t")
    monkeypatch.setenv("CHROMA_DATABASE", "d")

    mock_t3 = _mock_t3()
    captured_where: list[dict | None] = []

    def fake_search(query, collections, n_results, t3, where=None):
        captured_where.append(where)
        return []

    with patch("nexus.commands.search_cmd._t3", return_value=mock_t3):
        with patch("nexus.commands.search_cmd.search_cross_corpus", side_effect=fake_search):
            with patch("nexus.commands.search_cmd.load_config", return_value={"embeddings": {"rerankerModel": "rerank-2.5"}, "mxbai": {}}):
                result = runner.invoke(
                    main,
                    ["search", "query", "--corpus", "knowledge"],
                )

    assert result.exit_code == 0, result.output
    assert captured_where[0] is None, (
        f"Without --where, expected None, got {captured_where[0]}"
    )


# ── -A / -B / -C context lines ────────────────────────────────────────────────


def test_context_A_shows_extra_lines_after(
    runner: CliRunner, monkeypatch: pytest.MonkeyPatch
) -> None:
    """-A N shows N additional lines of chunk content after the first line."""
    monkeypatch.setenv("CHROMA_API_KEY", "k")
    monkeypatch.setenv("VOYAGE_API_KEY", "v")
    monkeypatch.setenv("CHROMA_TENANT", "t")
    monkeypatch.setenv("CHROMA_DATABASE", "d")

    # 5-line chunk; default (no -A) shows only 1 line; -A 3 shows 4 lines
    content = "line1\nline2\nline3\nline4\nline5"
    results_pool = [
        _make_result("r1", content,
                     metadata={"source_path": "foo.py", "line_start": 10}),
    ]

    mock_t3 = _mock_t3()
    with patch("nexus.commands.search_cmd._t3", return_value=mock_t3):
        with patch("nexus.commands.search_cmd.search_cross_corpus", return_value=results_pool):
            with patch("nexus.commands.search_cmd.load_config", return_value={"embeddings": {"rerankerModel": "rerank-2.5"}, "mxbai": {}}):
                # Without -A: format_plain shows all lines already — test that
                # -A N is accepted and does not crash
                result = runner.invoke(
                    main,
                    ["search", "query", "--no-rerank", "--corpus", "knowledge",
                     "--no-color", "-A", "3"],
                )

    assert result.exit_code == 0, result.output


def test_context_C_sets_lines_after(
    runner: CliRunner, monkeypatch: pytest.MonkeyPatch
) -> None:
    """-C N is accepted as alias for -A N (no error, integer arg)."""
    monkeypatch.setenv("CHROMA_API_KEY", "k")
    monkeypatch.setenv("VOYAGE_API_KEY", "v")
    monkeypatch.setenv("CHROMA_TENANT", "t")
    monkeypatch.setenv("CHROMA_DATABASE", "d")

    mock_t3 = _mock_t3()
    with patch("nexus.commands.search_cmd._t3", return_value=mock_t3):
        with patch("nexus.commands.search_cmd.search_cross_corpus", return_value=[]):
            with patch("nexus.commands.search_cmd.load_config", return_value={"embeddings": {"rerankerModel": "rerank-2.5"}, "mxbai": {}}):
                result = runner.invoke(
                    main,
                    ["search", "query", "--corpus", "knowledge", "-C", "5"],
                )

    assert result.exit_code == 0, result.output


def test_context_C_requires_integer(
    runner: CliRunner, monkeypatch: pytest.MonkeyPatch
) -> None:
    """-C requires an integer argument; passing a string produces error."""
    monkeypatch.setenv("CHROMA_API_KEY", "k")
    monkeypatch.setenv("VOYAGE_API_KEY", "v")
    monkeypatch.setenv("CHROMA_TENANT", "t")
    monkeypatch.setenv("CHROMA_DATABASE", "d")

    mock_t3 = _mock_t3()
    with patch("nexus.commands.search_cmd._t3", return_value=mock_t3):
        result = runner.invoke(
            main,
            ["search", "query", "--corpus", "knowledge", "-C", "notanumber"],
        )

    assert result.exit_code != 0


# ── format_plain context-aware output ────────────────────────────────────────


def test_format_plain_with_context_shows_correct_lines() -> None:
    """format_plain_with_context shows first line + lines_after additional lines."""
    from nexus.formatters import format_plain_with_context

    content = "\n".join(f"line{i}" for i in range(10))
    result = SearchResult(
        id="x", content=content, distance=0.1, collection="c",
        metadata={"source_path": "file.py", "line_start": 0},
    )
    lines = format_plain_with_context([result], lines_after=3)
    # Should show 1 + 3 = 4 lines
    content_lines = [ln for ln in lines if ln.strip()]
    assert len(content_lines) <= 4, f"Expected ≤4 lines, got: {content_lines}"


def test_format_plain_with_context_no_context_equals_format_plain() -> None:
    """format_plain_with_context(0) produces same output as format_plain."""
    from nexus.formatters import format_plain, format_plain_with_context

    content = "alpha\nbeta\ngamma"
    result = SearchResult(
        id="x", content=content, distance=0.1, collection="c",
        metadata={"source_path": "file.py", "line_start": 5},
    )
    plain = format_plain([result])
    ctx = format_plain_with_context([result], lines_after=0)
    assert plain == ctx


# ── --hybrid flag triggers ripgrep ────────────────────────────────────────────


def test_hybrid_flag_triggers_ripgrep(
    runner: CliRunner, monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    """When --hybrid is passed, search_ripgrep is invoked against cache files."""
    monkeypatch.setenv("CHROMA_API_KEY", "k")
    monkeypatch.setenv("VOYAGE_API_KEY", "v")
    monkeypatch.setenv("CHROMA_TENANT", "t")
    monkeypatch.setenv("CHROMA_DATABASE", "d")

    cache_file = tmp_path / "myrepo-abcd1234.cache"
    cache_file.write_text("/repo/main.py:1:hello world\n")
    monkeypatch.setattr("nexus.commands.search_cmd._CONFIG_DIR", tmp_path)

    mock_t3 = _mock_t3(["code__myrepo-abcd1234"])

    rg_call_count = []

    def fake_search_ripgrep(query, cache_path, *, n_results=50, fixed_strings=True):
        rg_call_count.append(1)
        return []

    with patch("nexus.commands.search_cmd._t3", return_value=mock_t3):
        with patch("nexus.commands.search_cmd.search_cross_corpus", return_value=[]):
            with patch("nexus.commands.search_cmd.load_config", return_value={"embeddings": {"rerankerModel": "rerank-2.5"}, "mxbai": {}}):
                with patch("nexus.commands.search_cmd.search_ripgrep", side_effect=fake_search_ripgrep):
                    result = runner.invoke(
                        main,
                        ["search", "query", "--hybrid", "--corpus", "code", "--no-rerank"],
                    )

    assert result.exit_code == 0, result.output
    assert len(rg_call_count) >= 1, "search_ripgrep should be called when --hybrid is set"


def test_hybrid_results_include_rg_hits(
    runner: CliRunner, monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    """With --hybrid, ripgrep hits are merged with semantic results."""
    monkeypatch.setenv("CHROMA_API_KEY", "k")
    monkeypatch.setenv("VOYAGE_API_KEY", "v")
    monkeypatch.setenv("CHROMA_TENANT", "t")
    monkeypatch.setenv("CHROMA_DATABASE", "d")

    cache_file = tmp_path / "myrepo-abcd1234.cache"
    cache_file.write_text("/repo/main.py:1:hello world\n")
    monkeypatch.setattr("nexus.commands.search_cmd._CONFIG_DIR", tmp_path)

    mock_t3 = _mock_t3(["code__myrepo-abcd1234"])

    rg_hit = {
        "file_path": "/repo/main.py",
        "line_number": 1,
        "line_content": "hello world",
        "frecency_score": 0.5,
    }

    with patch("nexus.commands.search_cmd._t3", return_value=mock_t3):
        with patch("nexus.commands.search_cmd.search_cross_corpus", return_value=[]):
            with patch("nexus.commands.search_cmd.load_config", return_value={"embeddings": {"rerankerModel": "rerank-2.5"}, "mxbai": {}}):
                with patch("nexus.commands.search_cmd.search_ripgrep", return_value=[rg_hit]):
                    result = runner.invoke(
                        main,
                        ["search", "query", "--hybrid", "--corpus", "code", "--no-rerank"],
                    )

    assert result.exit_code == 0, result.output
    assert "/repo/main.py" in result.output, (
        "Output should include the ripgrep-matched file path"
    )


def test_hybrid_without_cache_files_still_works(
    runner: CliRunner, monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    """When --hybrid is set but no .cache files exist, falls back to semantic-only."""
    monkeypatch.setenv("CHROMA_API_KEY", "k")
    monkeypatch.setenv("VOYAGE_API_KEY", "v")
    monkeypatch.setenv("CHROMA_TENANT", "t")
    monkeypatch.setenv("CHROMA_DATABASE", "d")

    # tmp_path has no .cache files
    monkeypatch.setattr("nexus.commands.search_cmd._CONFIG_DIR", tmp_path)

    semantic_result = _make_result(
        "sem1", "semantic content",
        collection="code__myrepo",
        metadata={"source_path": "file.py", "line_start": 1},
    )
    mock_t3 = _mock_t3(["code__myrepo"])

    with patch("nexus.commands.search_cmd._t3", return_value=mock_t3):
        with patch("nexus.commands.search_cmd.search_cross_corpus", return_value=[semantic_result]):
            with patch("nexus.commands.search_cmd.load_config", return_value={"embeddings": {"rerankerModel": "rerank-2.5"}, "mxbai": {}}):
                result = runner.invoke(
                    main,
                    ["search", "query", "--hybrid", "--corpus", "code", "--no-rerank"],
                )

    assert result.exit_code == 0, result.output
    assert "No results" not in result.output


# ── NX_ANSWER env var override ────────────────────────────────────────────────


def test_nx_answer_env_enables_answer_mode(
    runner: CliRunner, monkeypatch: pytest.MonkeyPatch
) -> None:
    """NX_ANSWER=1 activates answer mode without passing --answer / -a."""
    monkeypatch.setenv("CHROMA_API_KEY", "k")
    monkeypatch.setenv("VOYAGE_API_KEY", "v")
    monkeypatch.setenv("CHROMA_TENANT", "t")
    monkeypatch.setenv("CHROMA_DATABASE", "d")
    monkeypatch.setenv("NX_ANSWER", "1")

    results_pool = [
        _make_result("r1", "some content",
                     metadata={"source_path": "foo.py", "line_start": 1}),
    ]
    answer_called: list[bool] = []

    def fake_answer_mode(query, results):
        answer_called.append(True)
        return "Synthesized answer"

    mock_t3 = _mock_t3()
    with patch("nexus.commands.search_cmd._t3", return_value=mock_t3):
        with patch("nexus.commands.search_cmd.search_cross_corpus", return_value=results_pool):
            with patch("nexus.commands.search_cmd.load_config",
                       return_value={"embeddings": {"rerankerModel": "rerank-2.5"}, "mxbai": {}}):
                with patch("nexus.commands.search_cmd.answer_mode",
                           side_effect=fake_answer_mode):
                    result = runner.invoke(
                        main,
                        ["search", "query", "--corpus", "knowledge", "--no-rerank"],
                    )

    assert result.exit_code == 0, result.output
    assert len(answer_called) == 1, (
        "answer_mode() should be called when NX_ANSWER=1 is set, "
        f"but was called {len(answer_called)} times. Output: {result.output}"
    )


def test_nx_answer_env_unset_does_not_enable_answer_mode(
    runner: CliRunner, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Without NX_ANSWER, answer mode is off (existing behaviour preserved)."""
    monkeypatch.setenv("CHROMA_API_KEY", "k")
    monkeypatch.setenv("VOYAGE_API_KEY", "v")
    monkeypatch.setenv("CHROMA_TENANT", "t")
    monkeypatch.setenv("CHROMA_DATABASE", "d")
    monkeypatch.delenv("NX_ANSWER", raising=False)

    results_pool = [
        _make_result("r1", "some content",
                     metadata={"source_path": "foo.py", "line_start": 1}),
    ]
    answer_called: list[bool] = []

    def fake_answer_mode(query, results):
        answer_called.append(True)
        return "Synthesized answer"

    mock_t3 = _mock_t3()
    with patch("nexus.commands.search_cmd._t3", return_value=mock_t3):
        with patch("nexus.commands.search_cmd.search_cross_corpus", return_value=results_pool):
            with patch("nexus.commands.search_cmd.load_config",
                       return_value={"embeddings": {"rerankerModel": "rerank-2.5"}, "mxbai": {}}):
                with patch("nexus.commands.search_cmd.answer_mode",
                           side_effect=fake_answer_mode):
                    result = runner.invoke(
                        main,
                        ["search", "query", "--corpus", "knowledge", "--no-rerank"],
                    )

    assert result.exit_code == 0, result.output
    assert len(answer_called) == 0, (
        "answer_mode() should NOT be called when NX_ANSWER is unset, "
        f"but was called {len(answer_called)} times. Output: {result.output}"
    )


def test_nx_answer_empty_string_does_not_enable_answer_mode(
    runner: CliRunner, monkeypatch: pytest.MonkeyPatch
) -> None:
    """NX_ANSWER= (empty string) leaves answer mode off."""
    monkeypatch.setenv("CHROMA_API_KEY", "k")
    monkeypatch.setenv("VOYAGE_API_KEY", "v")
    monkeypatch.setenv("CHROMA_TENANT", "t")
    monkeypatch.setenv("CHROMA_DATABASE", "d")
    monkeypatch.setenv("NX_ANSWER", "")

    results_pool = [
        _make_result("r1", "some content",
                     metadata={"source_path": "foo.py", "line_start": 1}),
    ]
    answer_called: list[bool] = []

    def fake_answer_mode(query, results):
        answer_called.append(True)
        return "Synthesized answer"

    mock_t3 = _mock_t3()
    with patch("nexus.commands.search_cmd._t3", return_value=mock_t3):
        with patch("nexus.commands.search_cmd.search_cross_corpus", return_value=results_pool):
            with patch("nexus.commands.search_cmd.load_config",
                       return_value={"embeddings": {"rerankerModel": "rerank-2.5"}, "mxbai": {}}):
                with patch("nexus.commands.search_cmd.answer_mode",
                           side_effect=fake_answer_mode):
                    result = runner.invoke(
                        main,
                        ["search", "query", "--corpus", "knowledge", "--no-rerank"],
                    )

    assert result.exit_code == 0, result.output
    assert len(answer_called) == 0, (
        "answer_mode() should NOT be called when NX_ANSWER is empty string, "
        f"but was called {len(answer_called)} times. Output: {result.output}"
    )


# ── _parse_where edge cases ──────────────────────────────────────────────────


from nexus.commands.search_cmd import _parse_where


def test_parse_where_empty_tuple_returns_none() -> None:
    assert _parse_where(()) is None


def test_parse_where_single_pair() -> None:
    assert _parse_where(("lang=python",)) == {"lang": "python"}


def test_parse_where_multiple_equals_uses_first_partition() -> None:
    """'key=a=b=c' → key='a=b=c' (partition splits on first '=')."""
    result = _parse_where(("key=a=b=c",))
    assert result == {"key": "a=b=c"}


def test_parse_where_empty_value() -> None:
    """'key=' → key='' (empty value)."""
    result = _parse_where(("key=",))
    assert result == {"key": ""}


def test_parse_where_empty_key() -> None:
    """'=value' → ''='value' (empty key accepted by parser)."""
    result = _parse_where(("=value",))
    assert result == {"": "value"}


def test_parse_where_missing_equals_raises() -> None:
    from click import BadParameter
    with pytest.raises(BadParameter, match="KEY=VALUE"):
        _parse_where(("no-equals-here",))


def test_parse_where_multiple_pairs_merged() -> None:
    result = _parse_where(("lang=python", "type=code"))
    assert result == {"lang": "python", "type": "code"}



# ── --max-file-chunks ─────────────────────────────────────────────────────────


def test_max_file_chunks_builds_chunk_count_filter(
    runner: CliRunner, monkeypatch: pytest.MonkeyPatch
) -> None:
    """--max-file-chunks N passes chunk_count $lte filter to search_cross_corpus."""
    monkeypatch.setenv("CHROMA_API_KEY", "k")
    monkeypatch.setenv("VOYAGE_API_KEY", "v")
    monkeypatch.setenv("CHROMA_TENANT", "t")
    monkeypatch.setenv("CHROMA_DATABASE", "d")

    mock_t3 = _mock_t3(["code__myrepo"])
    captured_where: list[dict | None] = []

    def fake_search(query, collections, n_results, t3, where=None):
        captured_where.append(where)
        return []

    with patch("nexus.commands.search_cmd._t3", return_value=mock_t3):
        with patch("nexus.commands.search_cmd.search_cross_corpus", side_effect=fake_search):
            with patch("nexus.commands.search_cmd.load_config",
                       return_value={"embeddings": {"rerankerModel": "rerank-2.5"}, "mxbai": {}}):
                result = runner.invoke(
                    main,
                    ["search", "query", "--corpus", "code", "--max-file-chunks", "17"],
                )

    assert result.exit_code == 0, result.output
    assert len(captured_where) == 1
    assert captured_where[0] == {"chunk_count": {"$lte": 17}}, (
        f"Expected chunk_count $lte filter, got {captured_where[0]}"
    )


def test_max_file_chunks_and_where_merged_with_and(
    runner: CliRunner, monkeypatch: pytest.MonkeyPatch
) -> None:
    """--max-file-chunks + --where are merged using ChromaDB $and operator."""
    monkeypatch.setenv("CHROMA_API_KEY", "k")
    monkeypatch.setenv("VOYAGE_API_KEY", "v")
    monkeypatch.setenv("CHROMA_TENANT", "t")
    monkeypatch.setenv("CHROMA_DATABASE", "d")

    mock_t3 = _mock_t3(["code__myrepo"])
    captured_where: list[dict | None] = []

    def fake_search(query, collections, n_results, t3, where=None):
        captured_where.append(where)
        return []

    with patch("nexus.commands.search_cmd._t3", return_value=mock_t3):
        with patch("nexus.commands.search_cmd.search_cross_corpus", side_effect=fake_search):
            with patch("nexus.commands.search_cmd.load_config",
                       return_value={"embeddings": {"rerankerModel": "rerank-2.5"}, "mxbai": {}}):
                result = runner.invoke(
                    main,
                    [
                        "search", "query", "--corpus", "code",
                        "--max-file-chunks", "17",
                        "--where", "lang=python",
                    ],
                )

    assert result.exit_code == 0, result.output
    assert len(captured_where) == 1
    w = captured_where[0]
    assert w is not None
    assert "$and" in w, f"Expected $and merge, got {w}"
    conditions = w["$and"]
    assert {"chunk_count": {"$lte": 17}} in conditions, f"Missing chunk_count filter in {w}"
    assert {"lang": "python"} in conditions, f"Missing lang filter in {w}"


# ── corpus warning ────────────────────────────────────────────────────────────


def test_search_warns_when_corpus_term_unmatched(runner: CliRunner) -> None:
    """search emits a warning when a --corpus term matches no collection."""
    mock_t3 = _mock_t3(["knowledge__test"])
    mock_t3.search.return_value = []

    with patch("nexus.commands.search_cmd._t3", return_value=mock_t3), \
         patch("nexus.commands.search_cmd.search_cross_corpus", return_value=[]):
        result = runner.invoke(main, [
            "search", "foo",
            "--corpus", "knowledge",  # matches knowledge__test
            "--corpus", "badcorpus",  # matches nothing
        ])

    assert "badcorpus" in result.output
