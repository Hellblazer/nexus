"""T3: commands/index.py — nx index code registration and indexing."""
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


def test_index_code_registers_and_indexes(runner: CliRunner, index_home: Path) -> None:
    """nx index code <path> registers the repo and calls index_repository."""
    repo = index_home / "myrepo"
    repo.mkdir()

    mock_reg = MagicMock()
    mock_reg.get.return_value = None  # not yet registered

    with patch("nexus.commands.index._registry", return_value=mock_reg):
        with patch("nexus.indexer.index_repository") as mock_index:
            result = runner.invoke(main, ["index", "code", str(repo)])

    assert result.exit_code == 0
    mock_reg.add.assert_called_once()
    mock_index.assert_called_once()
    assert "Registered" in result.output
    assert "Done" in result.output


def test_index_code_idempotent_when_already_registered(runner: CliRunner, index_home: Path) -> None:
    """If repo is already registered, skip add() and just re-index."""
    repo = index_home / "myrepo"
    repo.mkdir()

    mock_reg = MagicMock()
    mock_reg.get.return_value = {"collection": "code__myrepo"}  # already registered

    with patch("nexus.commands.index._registry", return_value=mock_reg):
        with patch("nexus.indexer.index_repository") as mock_index:
            result = runner.invoke(main, ["index", "code", str(repo)])

    assert result.exit_code == 0
    mock_reg.add.assert_not_called()
    mock_index.assert_called_once()
    assert "Registered" not in result.output


def test_index_code_invalid_path(runner: CliRunner, index_home: Path) -> None:
    """Non-existent path produces a non-zero exit code."""
    result = runner.invoke(main, ["index", "code", str(index_home / "nonexistent")])
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
