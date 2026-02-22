# SPDX-License-Identifier: AGPL-3.0-or-later
"""Tests for nx collection info and verify command enhancements.

Bead nexus-sg7: verify --deep embedding spot-check
Bead nexus-3q6: info with embedding model + last-indexed
"""
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


# ── nexus-sg7: verify --deep ──────────────────────────────────────────────────


def test_verify_without_deep_preserves_existing_behavior(
    runner: CliRunner, env_creds
) -> None:
    """verify without --deep: existing behavior preserved (count shown)."""
    mock_db = MagicMock()
    mock_db.list_collections.return_value = [
        {"name": "knowledge__test", "count": 42},
    ]

    with patch("nexus.commands.collection._t3", return_value=mock_db):
        result = runner.invoke(main, ["collection", "verify", "knowledge__test"])

    assert result.exit_code == 0, result.output
    assert "42" in result.output
    assert "OK" in result.output
    # search should NOT be called for shallow verify
    mock_db.search.assert_not_called()


def test_verify_deep_calls_search_and_reports_health(
    runner: CliRunner, env_creds
) -> None:
    """verify --deep: search is called and health is reported."""
    mock_db = MagicMock()
    mock_db.list_collections.return_value = [
        {"name": "knowledge__test", "count": 5},
    ]
    mock_db.search.return_value = [
        {"id": "doc1", "content": "some text", "distance": 0.1}
    ]

    with patch("nexus.commands.collection._t3", return_value=mock_db):
        result = runner.invoke(
            main, ["collection", "verify", "knowledge__test", "--deep"]
        )

    assert result.exit_code == 0, result.output
    mock_db.search.assert_called_once()
    call_kwargs = mock_db.search.call_args
    # Verify it queried the correct collection
    assert "knowledge__test" in str(call_kwargs)
    # Output should confirm health
    assert "OK" in result.output or "health" in result.output.lower()


def test_verify_deep_empty_collection_warns_but_exits_zero(
    runner: CliRunner, env_creds
) -> None:
    """verify --deep on empty collection: warns but exits 0."""
    mock_db = MagicMock()
    mock_db.list_collections.return_value = [
        {"name": "knowledge__empty", "count": 0},
    ]

    with patch("nexus.commands.collection._t3", return_value=mock_db):
        result = runner.invoke(
            main, ["collection", "verify", "knowledge__empty", "--deep"]
        )

    assert result.exit_code == 0, result.output
    # Should warn about empty collection
    assert "empty" in result.output.lower() or "warning" in result.output.lower() or "0" in result.output
    # search should NOT be called for empty collection
    mock_db.search.assert_not_called()


def test_verify_deep_search_raises_exits_one(
    runner: CliRunner, env_creds
) -> None:
    """verify --deep when search raises: exits 1 with error message."""
    mock_db = MagicMock()
    mock_db.list_collections.return_value = [
        {"name": "knowledge__broken", "count": 3},
    ]
    mock_db.search.side_effect = RuntimeError("embedding service unavailable")

    with patch("nexus.commands.collection._t3", return_value=mock_db):
        result = runner.invoke(
            main, ["collection", "verify", "knowledge__broken", "--deep"]
        )

    assert result.exit_code != 0
    # Error message should be in output
    assert "embedding service unavailable" in result.output or "Error" in result.output


# ── nexus-3q6: info with embedding model + last-indexed ───────────────────────


def test_info_shows_embedding_model_for_code_collection(
    runner: CliRunner, env_creds
) -> None:
    """info shows voyage-code-3 for code__ collections."""
    mock_db = MagicMock()
    mock_db.list_collections.return_value = [
        {"name": "code__nexus", "count": 1247},
    ]
    # Mock get_collection to return a collection with no indexed_at in metadata
    mock_col = MagicMock()
    mock_col.get.return_value = {"ids": [], "metadatas": []}
    mock_db._client = MagicMock()
    mock_db._client.get_collection.return_value = mock_col

    with patch("nexus.commands.collection._t3", return_value=mock_db):
        result = runner.invoke(main, ["collection", "info", "code__nexus"])

    assert result.exit_code == 0, result.output
    assert "voyage-code-3" in result.output


def test_info_shows_embedding_model_for_knowledge_collection(
    runner: CliRunner, env_creds
) -> None:
    """info shows voyage-4 for knowledge__ collections."""
    mock_db = MagicMock()
    mock_db.list_collections.return_value = [
        {"name": "knowledge__research", "count": 88},
    ]
    mock_col = MagicMock()
    mock_col.get.return_value = {"ids": [], "metadatas": []}
    mock_db._client = MagicMock()
    mock_db._client.get_collection.return_value = mock_col

    with patch("nexus.commands.collection._t3", return_value=mock_db):
        result = runner.invoke(main, ["collection", "info", "knowledge__research"])

    assert result.exit_code == 0, result.output
    assert "voyage-4" in result.output


def test_info_shows_last_indexed_when_metadata_exists(
    runner: CliRunner, env_creds
) -> None:
    """info shows most recent indexed_at timestamp when it exists in metadata."""
    mock_db = MagicMock()
    mock_db.list_collections.return_value = [
        {"name": "knowledge__test", "count": 3},
    ]
    mock_col = MagicMock()
    mock_col.get.return_value = {
        "ids": ["a", "b", "c"],
        "metadatas": [
            {"indexed_at": "2026-02-20T08:00:00+00:00", "title": "doc1"},
            {"indexed_at": "2026-02-22T10:23:45+00:00", "title": "doc2"},
            {"indexed_at": "2026-02-21T12:00:00+00:00", "title": "doc3"},
        ],
    }
    mock_db._client = MagicMock()
    mock_db._client.get_collection.return_value = mock_col

    with patch("nexus.commands.collection._t3", return_value=mock_db):
        result = runner.invoke(main, ["collection", "info", "knowledge__test"])

    assert result.exit_code == 0, result.output
    # Should show the most recent indexed_at
    assert "2026-02-22T10:23:45+00:00" in result.output


def test_info_shows_unknown_when_no_indexed_at_metadata(
    runner: CliRunner, env_creds
) -> None:
    """info shows 'unknown' when no indexed_at metadata exists."""
    mock_db = MagicMock()
    mock_db.list_collections.return_value = [
        {"name": "knowledge__legacy", "count": 2},
    ]
    mock_col = MagicMock()
    mock_col.get.return_value = {
        "ids": ["x", "y"],
        "metadatas": [
            {"title": "doc_without_ts"},
            {"title": "another_without_ts"},
        ],
    }
    mock_db._client = MagicMock()
    mock_db._client.get_collection.return_value = mock_col

    with patch("nexus.commands.collection._t3", return_value=mock_db):
        result = runner.invoke(main, ["collection", "info", "knowledge__legacy"])

    assert result.exit_code == 0, result.output
    assert "unknown" in result.output.lower()
