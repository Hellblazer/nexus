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
