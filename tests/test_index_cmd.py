"""T3: commands/index.py — nx index repo registration and indexing."""
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from click.testing import CliRunner

from nexus.cli import main


@pytest.fixture
def runner() -> CliRunner:
    return CliRunner()


@pytest.fixture
def index_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("HOME", str(tmp_path))
    return tmp_path


def test_index_repo_registers_and_indexes(runner: CliRunner, index_home: Path) -> None:
    """nx index repo <path> registers the repo and calls index_repository."""
    repo = index_home / "myrepo"
    repo.mkdir()

    mock_reg = MagicMock()
    mock_reg.get.return_value = None  # not yet registered

    with patch("nexus.commands.index._registry", return_value=mock_reg):
        with patch("nexus.indexer.index_repository") as mock_index:
            result = runner.invoke(main, ["index", "repo", str(repo)])

    assert result.exit_code == 0
    mock_reg.add.assert_called_once()
    mock_index.assert_called_once()
    assert "Registered" in result.output
    assert "Done" in result.output


def test_index_repo_idempotent_when_already_registered(runner: CliRunner, index_home: Path) -> None:
    """If repo is already registered, skip add() and just re-index."""
    repo = index_home / "myrepo"
    repo.mkdir()

    mock_reg = MagicMock()
    mock_reg.get.return_value = {"collection": "code__myrepo"}  # already registered

    with patch("nexus.commands.index._registry", return_value=mock_reg):
        with patch("nexus.indexer.index_repository") as mock_index:
            result = runner.invoke(main, ["index", "repo", str(repo)])

    assert result.exit_code == 0
    mock_reg.add.assert_not_called()
    mock_index.assert_called_once()
    assert "Registered" not in result.output


def test_index_repo_invalid_path(runner: CliRunner, index_home: Path) -> None:
    """Non-existent path produces a non-zero exit code."""
    result = runner.invoke(main, ["index", "repo", str(index_home / "nonexistent")])
    assert result.exit_code != 0


# ── nx index pdf ──────────────────────────────────────────────────────────────

def test_index_pdf_command_indexes_file(runner: CliRunner, index_home: Path) -> None:
    """nx index pdf <path> calls index_pdf and reports chunk count."""
    pdf = index_home / "doc.pdf"
    pdf.write_bytes(b"fake pdf")

    with patch("nexus.doc_indexer.index_pdf", return_value=3) as mock_index:
        result = runner.invoke(main, ["index", "pdf", str(pdf)])

    assert result.exit_code == 0, result.output
    mock_index.assert_called_once()
    assert "3" in result.output


def test_index_pdf_nonexistent_path_fails(runner: CliRunner, index_home: Path) -> None:
    """Non-existent PDF path produces a non-zero exit code."""
    result = runner.invoke(main, ["index", "pdf", str(index_home / "missing.pdf")])
    assert result.exit_code != 0


# ── nx index md ───────────────────────────────────────────────────────────────

def test_index_md_command_indexes_file(runner: CliRunner, index_home: Path) -> None:
    """nx index md <path> calls index_markdown and reports chunk count."""
    md = index_home / "doc.md"
    md.write_text("# Hello\n\nWorld.\n")

    with patch("nexus.doc_indexer.index_markdown", return_value=2) as mock_index:
        result = runner.invoke(main, ["index", "md", str(md)])

    assert result.exit_code == 0, result.output
    mock_index.assert_called_once()
    assert "2" in result.output


def test_index_md_nonexistent_path_fails(runner: CliRunner, index_home: Path) -> None:
    """Non-existent markdown path produces a non-zero exit code."""
    result = runner.invoke(main, ["index", "md", str(index_home / "missing.md")])
    assert result.exit_code != 0


# ── --frecency-only flag ──────────────────────────────────────────────────────

def test_index_repo_frecency_only_flag_passed_through(runner: CliRunner, index_home: Path) -> None:
    """nx index repo <path> --frecency-only passes frecency_only=True to index_repository."""
    repo = index_home / "myrepo"
    repo.mkdir()

    mock_reg = MagicMock()
    mock_reg.get.return_value = {"collection": "code__myrepo"}  # already registered

    with patch("nexus.commands.index._registry", return_value=mock_reg):
        with patch("nexus.indexer.index_repository") as mock_index:
            result = runner.invoke(main, ["index", "repo", str(repo), "--frecency-only"])

    assert result.exit_code == 0, result.output
    mock_index.assert_called_once()
    _, call_kwargs = mock_index.call_args
    assert call_kwargs.get("frecency_only") is True
    assert "frecency" in result.output.lower()


def test_index_repo_default_is_full_index(runner: CliRunner, index_home: Path) -> None:
    """nx index repo <path> without --frecency-only passes frecency_only=False."""
    repo = index_home / "myrepo"
    repo.mkdir()

    mock_reg = MagicMock()
    mock_reg.get.return_value = {"collection": "code__myrepo"}

    with patch("nexus.commands.index._registry", return_value=mock_reg):
        with patch("nexus.indexer.index_repository") as mock_index:
            with patch("nexus.commands.index._detect_large_files", return_value=[]):
                result = runner.invoke(main, ["index", "repo", str(repo)])

    assert result.exit_code == 0, result.output
    mock_index.assert_called_once()
    _, call_kwargs = mock_index.call_args
    assert call_kwargs.get("frecency_only") is False


# ── Phase 3: --chunk-size and large-file warning ──────────────────────────────

def test_chunk_size_option_passed_to_index_repository(runner: CliRunner, index_home: Path) -> None:
    """--chunk-size N passes chunk_lines=N to index_repository."""
    repo = index_home / "myrepo"
    repo.mkdir()

    mock_reg = MagicMock()
    mock_reg.get.return_value = {"collection": "code__myrepo"}

    with patch("nexus.commands.index._registry", return_value=mock_reg):
        with patch("nexus.indexer.index_repository") as mock_index:
            with patch("nexus.commands.index._detect_large_files", return_value=[]):
                result = runner.invoke(main, ["index", "repo", str(repo), "--chunk-size", "80"])

    assert result.exit_code == 0, result.output
    _, call_kwargs = mock_index.call_args
    assert call_kwargs.get("chunk_lines") == 80


def test_chunk_size_default_is_none(runner: CliRunner, index_home: Path) -> None:
    """Without --chunk-size, chunk_lines defaults to None (use module default)."""
    repo = index_home / "myrepo"
    repo.mkdir()

    mock_reg = MagicMock()
    mock_reg.get.return_value = {"collection": "code__myrepo"}

    with patch("nexus.commands.index._registry", return_value=mock_reg):
        with patch("nexus.indexer.index_repository") as mock_index:
            with patch("nexus.commands.index._detect_large_files", return_value=[]):
                result = runner.invoke(main, ["index", "repo", str(repo)])

    assert result.exit_code == 0, result.output
    _, call_kwargs = mock_index.call_args
    assert call_kwargs.get("chunk_lines") is None


def test_large_file_warning_printed_when_large_files_present(
    runner: CliRunner, index_home: Path
) -> None:
    """Warning is printed to stderr when large code files are detected."""
    repo = index_home / "myrepo"
    repo.mkdir()

    mock_reg = MagicMock()
    mock_reg.get.return_value = {"collection": "code__myrepo"}
    large_path = repo / "huge.py"

    with patch("nexus.commands.index._registry", return_value=mock_reg):
        with patch("nexus.indexer.index_repository"):
            with patch(
                "nexus.commands.index._detect_large_files",
                return_value=[(5000, large_path)],
            ):
                result = runner.invoke(main, ["index", "repo", str(repo)], )

    assert result.exit_code == 0, result.output
    assert "Warning" in result.output
    assert "chunk" in result.output.lower()


def test_no_chunk_warning_flag_suppresses_warning(
    runner: CliRunner, index_home: Path
) -> None:
    """--no-chunk-warning suppresses the large-file warning."""
    repo = index_home / "myrepo"
    repo.mkdir()

    mock_reg = MagicMock()
    mock_reg.get.return_value = {"collection": "code__myrepo"}
    large_path = repo / "huge.py"

    with patch("nexus.commands.index._registry", return_value=mock_reg):
        with patch("nexus.indexer.index_repository"):
            with patch(
                "nexus.commands.index._detect_large_files",
                return_value=[(5000, large_path)],
            ):
                result = runner.invoke(
                    main,
                    ["index", "repo", str(repo), "--no-chunk-warning"],
                )

    assert result.exit_code == 0, result.output
    assert "Warning" not in result.output


def test_no_warning_when_no_large_files(runner: CliRunner, index_home: Path) -> None:
    """No warning is printed when all code files are within the threshold."""
    repo = index_home / "myrepo"
    repo.mkdir()

    mock_reg = MagicMock()
    mock_reg.get.return_value = {"collection": "code__myrepo"}

    with patch("nexus.commands.index._registry", return_value=mock_reg):
        with patch("nexus.indexer.index_repository"):
            with patch("nexus.commands.index._detect_large_files", return_value=[]):
                result = runner.invoke(main, ["index", "repo", str(repo)], )

    assert result.exit_code == 0, result.output
    assert "Warning" not in result.output


def test_detect_large_files_returns_large_code_files(index_home: Path) -> None:
    """_detect_large_files identifies code files exceeding threshold * chunk_lines."""
    from nexus.commands.index import _detect_large_files

    repo = index_home / "myrepo"
    repo.mkdir()

    # Create a large code file (line_count > 30 * 10 = 300)
    large = repo / "big.py"
    large.write_text("\n".join(f"x = {i}" for i in range(350)))

    # Create a small code file
    small = repo / "small.py"
    small.write_text("x = 1\n")

    results = _detect_large_files(repo, chunk_lines=10, threshold=30)
    large_paths = [p for _, p in results]

    assert large in large_paths
    assert small not in large_paths


def test_detect_large_files_ignores_non_code_files(index_home: Path) -> None:
    """_detect_large_files skips non-code files (Markdown, JSON, etc.)."""
    from nexus.commands.index import _detect_large_files

    repo = index_home / "myrepo"
    repo.mkdir()

    md_file = repo / "README.md"
    md_file.write_text("\n".join(f"line {i}" for i in range(500)))

    results = _detect_large_files(repo, chunk_lines=10, threshold=30)
    paths = [p for _, p in results]

    assert md_file not in paths
