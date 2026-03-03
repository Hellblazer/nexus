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
            result = runner.invoke(main, ["index", "repo", str(repo)])

    assert result.exit_code == 0, result.output
    mock_index.assert_called_once()
    _, call_kwargs = mock_index.call_args
    assert call_kwargs.get("frecency_only") is False


# ── --force flag ──────────────────────────────────────────────────────────────


def test_index_repo_force_flag_passed_through(runner: CliRunner, index_home: Path) -> None:
    """nx index repo <path> --force passes force=True to index_repository."""
    repo = index_home / "myrepo"
    repo.mkdir()

    mock_reg = MagicMock()
    mock_reg.get.return_value = {"collection": "code__myrepo"}

    with patch("nexus.commands.index._registry", return_value=mock_reg):
        with patch("nexus.indexer.index_repository") as mock_index:
            result = runner.invoke(main, ["index", "repo", str(repo), "--force"])

    assert result.exit_code == 0, result.output
    mock_index.assert_called_once()
    _, call_kwargs = mock_index.call_args
    assert call_kwargs.get("force") is True


def test_index_repo_force_frecency_mutual_exclusion(runner: CliRunner, index_home: Path) -> None:
    """--force and --frecency-only are mutually exclusive on nx index repo."""
    repo = index_home / "myrepo"
    repo.mkdir()

    mock_reg = MagicMock()
    mock_reg.get.return_value = {"collection": "code__myrepo"}

    with patch("nexus.commands.index._registry", return_value=mock_reg):
        result = runner.invoke(main, ["index", "repo", str(repo), "--force", "--frecency-only"])

    assert result.exit_code != 0
    assert "mutually exclusive" in result.output.lower()


def test_index_repo_force_output_message(runner: CliRunner, index_home: Path) -> None:
    """nx index repo <path> --force prints 'Force-indexing' instead of 'Indexing'."""
    repo = index_home / "myrepo"
    repo.mkdir()

    mock_reg = MagicMock()
    mock_reg.get.return_value = {"collection": "code__myrepo"}

    with patch("nexus.commands.index._registry", return_value=mock_reg):
        with patch("nexus.indexer.index_repository"):
            result = runner.invoke(main, ["index", "repo", str(repo), "--force"])

    assert result.exit_code == 0, result.output
    assert "Force-indexing" in result.output


def test_index_pdf_force_flag(runner: CliRunner, index_home: Path) -> None:
    """nx index pdf <path> --force passes force=True to index_pdf."""
    pdf = index_home / "doc.pdf"
    pdf.write_bytes(b"fake pdf")

    with patch("nexus.doc_indexer.index_pdf", return_value=5) as mock_index:
        result = runner.invoke(main, ["index", "pdf", str(pdf), "--force"])

    assert result.exit_code == 0, result.output
    mock_index.assert_called_once()
    _, call_kwargs = mock_index.call_args
    assert call_kwargs.get("force") is True


def test_index_pdf_force_dry_run_mutual_exclusion(runner: CliRunner, index_home: Path) -> None:
    """--force and --dry-run are mutually exclusive on nx index pdf."""
    pdf = index_home / "doc.pdf"
    pdf.write_bytes(b"fake pdf")

    result = runner.invoke(main, ["index", "pdf", str(pdf), "--force", "--dry-run"])

    assert result.exit_code != 0
    assert "mutually exclusive" in result.output.lower()


def test_index_md_force_flag(runner: CliRunner, index_home: Path) -> None:
    """nx index md <path> --force passes force=True to index_markdown."""
    md = index_home / "doc.md"
    md.write_text("# Hello\n\nWorld.\n")

    with patch("nexus.doc_indexer.index_markdown", return_value=2) as mock_index:
        result = runner.invoke(main, ["index", "md", str(md), "--force"])

    assert result.exit_code == 0, result.output
    mock_index.assert_called_once()
    _, call_kwargs = mock_index.call_args
    assert call_kwargs.get("force") is True
