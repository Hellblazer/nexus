"""CLI-layer tests for nx store, nx search, and nx collection commands."""
from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest
from click.testing import CliRunner

from nexus.cli import main


@pytest.fixture
def runner() -> CliRunner:
    return CliRunner()


@pytest.fixture
def env_creds(monkeypatch: pytest.MonkeyPatch) -> None:
    """Set required credential env vars."""
    monkeypatch.setenv("CHROMA_API_KEY", "test-chroma-key")
    monkeypatch.setenv("VOYAGE_API_KEY", "test-voyage-key")
    monkeypatch.setenv("CHROMA_TENANT", "test-tenant")
    monkeypatch.setenv("CHROMA_DATABASE", "test-db")


# ── _t3() factory error paths ─────────────────────────────────────────────────

def test_store_put_missing_chroma_api_key(
    runner: CliRunner, monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    monkeypatch.delenv("CHROMA_API_KEY", raising=False)
    monkeypatch.setenv("VOYAGE_API_KEY", "vk")
    monkeypatch.setenv("CHROMA_TENANT", "t")
    monkeypatch.setenv("CHROMA_DATABASE", "d")
    src = tmp_path / "f.txt"
    src.write_text("content")

    result = runner.invoke(main, ["store", "put", str(src)])

    assert result.exit_code != 0
    assert "CHROMA_API_KEY" in result.output


def test_store_put_missing_voyage_api_key(
    runner: CliRunner, monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    monkeypatch.setenv("CHROMA_API_KEY", "ck")
    monkeypatch.delenv("VOYAGE_API_KEY", raising=False)
    monkeypatch.setenv("CHROMA_TENANT", "t")
    monkeypatch.setenv("CHROMA_DATABASE", "d")
    src = tmp_path / "f.txt"
    src.write_text("content")

    result = runner.invoke(main, ["store", "put", str(src)])

    assert result.exit_code != 0
    assert "VOYAGE_API_KEY" in result.output


def test_store_put_missing_tenant(
    runner: CliRunner, monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    monkeypatch.setenv("CHROMA_API_KEY", "ck")
    monkeypatch.setenv("VOYAGE_API_KEY", "vk")
    monkeypatch.delenv("CHROMA_TENANT", raising=False)
    monkeypatch.delenv("CHROMA_DATABASE", raising=False)
    src = tmp_path / "f.txt"
    src.write_text("content")

    result = runner.invoke(main, ["store", "put", str(src)])

    assert result.exit_code != 0
    assert "tenant" in result.output.lower() or "database" in result.output.lower()


# ── nx store put ──────────────────────────────────────────────────────────────

def test_store_put_stdin_requires_title(
    runner: CliRunner, env_creds, monkeypatch: pytest.MonkeyPatch
) -> None:
    """C1: stdin input without --title is rejected with a clear error."""
    with patch("nexus.commands.store._t3"):
        result = runner.invoke(main, ["store", "put", "-"], input="some content")

    assert result.exit_code != 0
    assert "--title" in result.output


def test_store_put_stdin_with_title_succeeds(
    runner: CliRunner, env_creds
) -> None:
    """stdin input with --title stores the document."""
    mock_db = MagicMock()
    mock_db.put.return_value = "doc-id-abc"

    with patch("nexus.commands.store._t3", return_value=mock_db):
        result = runner.invoke(
            main, ["store", "put", "-", "--title", "my-title.md"], input="content here"
        )

    assert result.exit_code == 0
    assert "doc-id-abc" in result.output
    mock_db.put.assert_called_once()
    call_kwargs = mock_db.put.call_args.kwargs
    assert call_kwargs["title"] == "my-title.md"
    assert call_kwargs["content"] == "content here"


def test_store_put_file_uses_filename_as_title(
    runner: CliRunner, env_creds, tmp_path
) -> None:
    """File store uses the filename as the default title."""
    src = tmp_path / "analysis.md"
    src.write_text("finding: important")
    mock_db = MagicMock()
    mock_db.put.return_value = "doc-id-xyz"

    with patch("nexus.commands.store._t3", return_value=mock_db):
        result = runner.invoke(main, ["store", "put", str(src)])

    assert result.exit_code == 0
    assert "doc-id-xyz" in result.output
    call_kwargs = mock_db.put.call_args.kwargs
    assert call_kwargs["title"] == "analysis.md"


def test_store_put_file_not_found(
    runner: CliRunner, env_creds
) -> None:
    with patch("nexus.commands.store._t3"):
        result = runner.invoke(main, ["store", "put", "/no/such/file.txt"])

    assert result.exit_code != 0
    assert "not found" in result.output.lower() or "File not found" in result.output


def test_store_put_invalid_ttl_shows_error(
    runner: CliRunner, env_creds, tmp_path
) -> None:
    """Invalid TTL format is rejected with a clear CLI error (not a traceback)."""
    src = tmp_path / "f.txt"
    src.write_text("content")

    with patch("nexus.commands.store._t3"):
        result = runner.invoke(main, ["store", "put", str(src), "--ttl", "5z"])

    assert result.exit_code != 0
    assert "5z" in result.output


def test_store_expire_reports_count(runner: CliRunner, env_creds) -> None:
    mock_db = MagicMock()
    mock_db.expire.return_value = 3

    with patch("nexus.commands.store._t3", return_value=mock_db):
        result = runner.invoke(main, ["store", "expire"])

    assert result.exit_code == 0
    assert "3" in result.output


# ── nx collection ─────────────────────────────────────────────────────────────

def test_collection_list_empty(runner: CliRunner, env_creds) -> None:
    mock_db = MagicMock()
    mock_db.list_collections.return_value = []

    with patch("nexus.commands.collection._t3", return_value=mock_db):
        result = runner.invoke(main, ["collection", "list"])

    assert result.exit_code == 0
    assert "No collections" in result.output


def test_collection_list_shows_names_and_counts(runner: CliRunner, env_creds) -> None:
    mock_db = MagicMock()
    mock_db.list_collections.return_value = [
        {"name": "code__myrepo", "count": 42},
        {"name": "knowledge__sec", "count": 7},
    ]

    with patch("nexus.commands.collection._t3", return_value=mock_db):
        result = runner.invoke(main, ["collection", "list"])

    assert result.exit_code == 0
    assert "code__myrepo" in result.output
    assert "42" in result.output
    assert "knowledge__sec" in result.output
    assert "7" in result.output


def test_collection_info_not_found(runner: CliRunner, env_creds) -> None:
    mock_db = MagicMock()
    mock_db.list_collections.return_value = []

    with patch("nexus.commands.collection._t3", return_value=mock_db):
        result = runner.invoke(main, ["collection", "info", "no-such-collection"])

    assert result.exit_code != 0
    assert "not found" in result.output.lower()


def test_collection_verify_not_found(runner: CliRunner, env_creds) -> None:
    mock_db = MagicMock()
    mock_db.list_collections.return_value = []

    with patch("nexus.commands.collection._t3", return_value=mock_db):
        result = runner.invoke(main, ["collection", "verify", "missing"])

    assert result.exit_code != 0
    assert "not found" in result.output.lower()


# ── nx search ────────────────────────────────────────────────────────────────

def test_search_no_matching_corpus(runner: CliRunner, env_creds) -> None:
    mock_db = MagicMock()
    mock_db.list_collections.return_value = []

    with patch("nexus.commands.search_cmd._t3", return_value=mock_db):
        result = runner.invoke(main, ["search", "my query", "--corpus", "code"])

    assert result.exit_code == 0
    assert "No matching collections" in result.output


def test_search_no_results(runner: CliRunner, env_creds) -> None:
    mock_db = MagicMock()
    mock_db.list_collections.return_value = [{"name": "knowledge__sec", "count": 5}]
    mock_db.search.return_value = []

    with patch("nexus.commands.search_cmd._t3", return_value=mock_db):
        result = runner.invoke(main, ["search", "my query", "--corpus", "knowledge"])

    assert result.exit_code == 0
    assert "No results" in result.output


def test_search_displays_results(runner: CliRunner, env_creds) -> None:
    mock_db = MagicMock()
    mock_db.list_collections.return_value = [{"name": "knowledge__sec", "count": 2}]
    mock_db.search.return_value = [
        {
            "id": "abc12345-0000-0000-0000-000000000000",
            "content": "security finding here",
            "distance": 0.123,
            "title": "sec.md",
            "tags": "security",
            "source_path": "./sec.md",
            "line_start": 1,
        }
    ]

    with patch("nexus.commands.search_cmd._t3", return_value=mock_db):
        result = runner.invoke(main, ["search", "security", "--corpus", "knowledge"])

    assert result.exit_code == 0
    assert "security finding here" in result.output


# ── nexus-ani: collection delete --yes flag ───────────────────────────────────

def test_collection_delete_yes_skips_prompt(runner: CliRunner, env_creds) -> None:
    """--yes flag skips interactive confirmation."""
    mock_db = MagicMock()

    with patch("nexus.commands.collection._t3", return_value=mock_db):
        result = runner.invoke(main, ["collection", "delete", "knowledge__test", "--yes"])

    assert result.exit_code == 0, result.output
    mock_db.delete_collection.assert_called_once_with("knowledge__test")
    assert "Deleted" in result.output


def test_collection_delete_without_yes_prompts(runner: CliRunner, env_creds) -> None:
    """Without --yes, a confirmation prompt is shown (user declines via 'n')."""
    mock_db = MagicMock()

    with patch("nexus.commands.collection._t3", return_value=mock_db):
        result = runner.invoke(main, ["collection", "delete", "knowledge__test"], input="n\n")

    # User said no → aborted, collection NOT deleted
    mock_db.delete_collection.assert_not_called()


def test_collection_delete_confirm_flag_no_longer_exists(runner: CliRunner, env_creds) -> None:
    """--confirm flag was removed (renamed to --yes); using it is an error."""
    mock_db = MagicMock()

    with patch("nexus.commands.collection._t3", return_value=mock_db):
        result = runner.invoke(main, ["collection", "delete", "knowledge__test", "--confirm"])

    assert result.exit_code != 0  # no such option
