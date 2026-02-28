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


# ── list ─────────────────────────────────────────────────────────────────────


def test_list_empty(runner: CliRunner, env_creds) -> None:
    """Empty cloud returns 'No collections' message."""
    mock_db = MagicMock()
    mock_db.list_collections.return_value = []
    with patch("nexus.commands.collection.t3_knowledge", return_value=mock_db), \
         patch("nexus.commands.collection.t3_code", return_value=mock_db), \
         patch("nexus.commands.collection.t3_docs", return_value=mock_db), \
         patch("nexus.commands.collection.t3_rdr", return_value=mock_db):
        result = runner.invoke(main, ["collection", "list"])
    assert result.exit_code == 0
    assert "No collections" in result.output


def test_list_shows_names_and_counts(runner: CliRunner, env_creds) -> None:
    mock_db = MagicMock()
    mock_db.list_collections.return_value = [
        {"name": "code__myrepo", "count": 42},
        {"name": "knowledge__topic", "count": 7},
    ]
    with patch("nexus.commands.collection.t3_knowledge", return_value=mock_db):
        result = runner.invoke(main, ["collection", "list", "--type", "knowledge"])
    assert result.exit_code == 0
    assert "code__myrepo" in result.output
    assert "42" in result.output
    assert "knowledge__topic" in result.output


# ── info not found ───────────────────────────────────────────────────────────


def test_info_not_found(runner: CliRunner, env_creds) -> None:
    mock_db = MagicMock()
    mock_db.list_collections.return_value = []
    with patch("nexus.commands.collection.t3_knowledge", return_value=mock_db):
        result = runner.invoke(main, ["collection", "info", "no_such"])
    assert result.exit_code != 0
    assert "not found" in result.output.lower()


# ── delete ───────────────────────────────────────────────────────────────────


def test_delete_with_yes_flag(runner: CliRunner, env_creds) -> None:
    mock_db = MagicMock()
    with patch("nexus.commands.collection.t3_knowledge_local", return_value=mock_db):
        result = runner.invoke(main, ["collection", "delete", "old", "--yes"])
    assert result.exit_code == 0
    assert "Deleted" in result.output
    mock_db.delete_collection.assert_called_once_with("old")


def test_delete_aborts_without_confirmation(runner: CliRunner, env_creds) -> None:
    mock_db = MagicMock()
    with patch("nexus.commands.collection.t3_knowledge_local", return_value=mock_db):
        result = runner.invoke(main, ["collection", "delete", "old"], input="n\n")
    assert result.exit_code != 0
    mock_db.delete_collection.assert_not_called()


def test_delete_confirm_alias(runner: CliRunner, env_creds) -> None:
    """--confirm is accepted as alias for --yes."""
    mock_db = MagicMock()
    with patch("nexus.commands.collection.t3_knowledge_local", return_value=mock_db):
        result = runner.invoke(main, ["collection", "delete", "c", "--confirm"])
    assert result.exit_code == 0
    mock_db.delete_collection.assert_called_once()


# ── verify not found ─────────────────────────────────────────────────────────


def test_verify_not_found(runner: CliRunner, env_creds) -> None:
    mock_db = MagicMock()
    mock_db.list_collections.return_value = []
    with patch("nexus.commands.collection.t3_knowledge", return_value=mock_db):
        result = runner.invoke(main, ["collection", "verify", "missing"])
    assert result.exit_code != 0
    assert "not found" in result.output.lower()


# ── nexus-sg7: verify --deep ──────────────────────────────────────────────────


def test_verify_without_deep_preserves_existing_behavior(
    runner: CliRunner, env_creds
) -> None:
    """verify without --deep: existing behavior preserved (count shown)."""
    mock_db = MagicMock()
    mock_db.list_collections.return_value = [
        {"name": "knowledge__test", "count": 42},
    ]

    with patch("nexus.commands.collection.t3_knowledge", return_value=mock_db):
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

    with patch("nexus.commands.collection.t3_knowledge", return_value=mock_db):
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

    with patch("nexus.commands.collection.t3_knowledge", return_value=mock_db):
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

    with patch("nexus.commands.collection.t3_knowledge", return_value=mock_db):
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
    """info shows voyage-code-3 index model and voyage-4 query model for code__ collections."""
    mock_db = MagicMock()
    mock_db.list_collections.return_value = [
        {"name": "code__nexus", "count": 1247},
    ]
    mock_db.collection_info.return_value = {"count": 1247, "metadata": {}}
    mock_col = MagicMock()
    mock_col.get.return_value = {"ids": [], "metadatas": []}
    mock_db._client.get_collection.return_value = mock_col

    with patch("nexus.commands.collection.t3_code", return_value=mock_db):
        result = runner.invoke(main, ["collection", "info", "code__nexus", "--type", "code"])

    assert result.exit_code == 0, result.output
    assert "voyage-code-3" in result.output  # index model
    assert "voyage-4" in result.output        # query model


def test_info_shows_embedding_model_for_knowledge_collection(
    runner: CliRunner, env_creds
) -> None:
    """info shows voyage-4 for knowledge__ collections."""
    mock_db = MagicMock()
    mock_db.list_collections.return_value = [
        {"name": "knowledge__research", "count": 88},
    ]
    mock_db.collection_info.return_value = {"count": 88, "metadata": {}}
    mock_col = MagicMock()
    mock_col.get.return_value = {"ids": [], "metadatas": []}
    mock_db._client.get_collection.return_value = mock_col

    with patch("nexus.commands.collection.t3_knowledge", return_value=mock_db):
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
    mock_db.collection_info.return_value = {"count": 3, "metadata": {}}
    mock_col = MagicMock()
    mock_col.get.return_value = {
        "ids": ["a", "b", "c"],
        "metadatas": [
            {"indexed_at": "2026-02-20T08:00:00+00:00", "title": "doc1"},
            {"indexed_at": "2026-02-22T10:23:45+00:00", "title": "doc2"},
            {"indexed_at": "2026-02-21T12:00:00+00:00", "title": "doc3"},
        ],
    }
    mock_db._client.get_collection.return_value = mock_col

    with patch("nexus.commands.collection.t3_knowledge", return_value=mock_db):
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
    mock_db.collection_info.return_value = {"count": 2, "metadata": {}}
    mock_col = MagicMock()
    mock_col.get.return_value = {
        "ids": ["x", "y"],
        "metadatas": [
            {"title": "doc_without_ts"},
            {"title": "another_without_ts"},
        ],
    }
    mock_db._client.get_collection.return_value = mock_col

    with patch("nexus.commands.collection.t3_knowledge", return_value=mock_db):
        result = runner.invoke(main, ["collection", "info", "knowledge__legacy"])

    assert result.exit_code == 0, result.output
    assert "unknown" in result.output.lower()


# ── nexus-pjsc.4: --type flag + multi-store list ──────────────────────────────


def test_list_type_knowledge_routes_to_knowledge_store(runner: CliRunner) -> None:
    """list --type knowledge uses t3_knowledge() only."""
    mock_db = MagicMock()
    mock_db.list_collections.return_value = [{"name": "knowledge__x", "count": 3}]
    with patch("nexus.commands.collection.t3_knowledge", return_value=mock_db):
        result = runner.invoke(main, ["collection", "list", "--type", "knowledge"])
    assert result.exit_code == 0, result.output
    assert "knowledge__x" in result.output


def test_list_no_type_enumerates_all_four_stores(runner: CliRunner) -> None:
    """list (no --type) calls all 4 store factories and merges results."""
    def make_mock(names):
        m = MagicMock()
        m.list_collections.return_value = [{"name": n, "count": 1} for n in names]
        return m

    mock_code = make_mock(["code__a"])
    mock_docs = make_mock(["docs__b"])
    mock_rdr = make_mock(["rdr__c"])
    mock_know = make_mock(["knowledge__d"])

    with patch("nexus.commands.collection.t3_code", return_value=mock_code), \
         patch("nexus.commands.collection.t3_docs", return_value=mock_docs), \
         patch("nexus.commands.collection.t3_rdr", return_value=mock_rdr), \
         patch("nexus.commands.collection.t3_knowledge", return_value=mock_know):
        result = runner.invoke(main, ["collection", "list"])

    assert result.exit_code == 0, result.output
    assert "code__a" in result.output
    assert "docs__b" in result.output
    assert "rdr__c" in result.output
    assert "knowledge__d" in result.output


def test_list_type_code_routes_to_code_store(runner: CliRunner) -> None:
    """list --type code uses t3_code() only."""
    mock_db = MagicMock()
    mock_db.list_collections.return_value = [{"name": "code__myrepo", "count": 99}]
    with patch("nexus.commands.collection.t3_code", return_value=mock_db):
        result = runner.invoke(main, ["collection", "list", "--type", "code"])
    assert result.exit_code == 0, result.output
    assert "code__myrepo" in result.output


def test_info_default_uses_knowledge_store(runner: CliRunner) -> None:
    """info without --type uses t3_knowledge()."""
    mock_db = MagicMock()
    mock_db.list_collections.return_value = [{"name": "knowledge__test", "count": 5}]
    mock_db.collection_info.return_value = {"count": 5, "metadata": {}}
    mock_col = MagicMock()
    mock_col.get.return_value = {"ids": [], "metadatas": []}
    mock_db.get_or_create_collection.return_value = mock_col
    with patch("nexus.commands.collection.t3_knowledge", return_value=mock_db):
        result = runner.invoke(main, ["collection", "info", "knowledge__test"])
    assert result.exit_code == 0, result.output
    mock_db.list_collections.assert_called_once()


def test_info_type_code_routes_to_code_store(runner: CliRunner) -> None:
    """info --type code uses t3_code()."""
    mock_db = MagicMock()
    mock_db.list_collections.return_value = [{"name": "code__nexus", "count": 10}]
    mock_db.collection_info.return_value = {"count": 10, "metadata": {}}
    mock_col = MagicMock()
    mock_col.get.return_value = {"ids": [], "metadatas": []}
    mock_db.get_or_create_collection.return_value = mock_col
    with patch("nexus.commands.collection.t3_code", return_value=mock_db):
        result = runner.invoke(main, ["collection", "info", "code__nexus", "--type", "code"])
    assert result.exit_code == 0, result.output
    mock_db.list_collections.assert_called_once()


def test_delete_default_uses_knowledge_store(runner: CliRunner) -> None:
    """delete without --type uses t3_knowledge_local() (no voyage_api_key needed)."""
    mock_db = MagicMock()
    with patch("nexus.commands.collection.t3_knowledge_local", return_value=mock_db):
        result = runner.invoke(main, ["collection", "delete", "knowledge__old", "--yes"])
    assert result.exit_code == 0, result.output
    mock_db.delete_collection.assert_called_once_with("knowledge__old")


def test_delete_type_rdr_routes_to_rdr_store(runner: CliRunner) -> None:
    """delete --type rdr uses t3_rdr_local() (no voyage_api_key needed)."""
    mock_db = MagicMock()
    with patch("nexus.commands.collection.t3_rdr_local", return_value=mock_db):
        result = runner.invoke(main, ["collection", "delete", "rdr__old", "--yes", "--type", "rdr"])
    assert result.exit_code == 0, result.output
    mock_db.delete_collection.assert_called_once_with("rdr__old")


def test_verify_default_uses_knowledge_store(runner: CliRunner) -> None:
    """verify without --type uses t3_knowledge()."""
    mock_db = MagicMock()
    mock_db.list_collections.return_value = [{"name": "knowledge__chk", "count": 2}]
    with patch("nexus.commands.collection.t3_knowledge", return_value=mock_db):
        result = runner.invoke(main, ["collection", "verify", "knowledge__chk"])
    assert result.exit_code == 0, result.output
    mock_db.list_collections.assert_called_once()


def test_verify_type_docs_routes_to_docs_store(runner: CliRunner) -> None:
    """verify --type docs uses t3_docs()."""
    mock_db = MagicMock()
    mock_db.list_collections.return_value = [{"name": "docs__corpus", "count": 7}]
    with patch("nexus.commands.collection.t3_docs", return_value=mock_db):
        result = runner.invoke(main, ["collection", "verify", "docs__corpus", "--type", "docs"])
    assert result.exit_code == 0, result.output
    mock_db.list_collections.assert_called_once()


# ── C2: info_cmd must not call get_or_create_collection ──────────────────────


def test_info_does_not_call_get_or_create_collection(
    runner: CliRunner, env_creds
) -> None:
    """C2: info_cmd uses _client.get_collection, not get_or_create_collection."""
    mock_db = MagicMock()
    mock_db.list_collections.return_value = [{"name": "knowledge__test", "count": 5}]
    mock_db.collection_info.return_value = {}
    mock_col = MagicMock()
    mock_col.get.return_value = {"ids": [], "metadatas": []}
    mock_db._client.get_collection.return_value = mock_col

    with patch("nexus.commands.collection.t3_knowledge", return_value=mock_db):
        result = runner.invoke(main, ["collection", "info", "knowledge__test"])

    assert result.exit_code == 0, result.output
    mock_db.get_or_create_collection.assert_not_called()
    mock_db._client.get_collection.assert_called_once_with("knowledge__test")


# ── S3: info_cmd caps metadata fetch with limit ───────────────────────────────


def test_info_fetches_metadata_with_limit(runner: CliRunner, env_creds) -> None:
    """S3: col.get() in info_cmd must pass limit= to prevent unbounded fetches."""
    mock_db = MagicMock()
    mock_db.list_collections.return_value = [{"name": "knowledge__test", "count": 5}]
    mock_db.collection_info.return_value = {}
    mock_col = MagicMock()
    mock_col.get.return_value = {"ids": [], "metadatas": []}
    mock_db._client.get_collection.return_value = mock_col

    with patch("nexus.commands.collection.t3_knowledge", return_value=mock_db):
        result = runner.invoke(main, ["collection", "info", "knowledge__test"])

    assert result.exit_code == 0, result.output
    assert mock_col.get.called, "col.get() should have been called"
    call_kwargs = mock_col.get.call_args.kwargs
    assert "limit" in call_kwargs, "col.get() must pass limit= to avoid unbounded fetches"


# ── I4: info/delete/verify infer store from collection name prefix ────────────


def test_info_infers_code_store_from_prefix(runner: CliRunner, env_creds) -> None:
    """I4: info code__repo without --type infers code store from prefix."""
    mock_code_db = MagicMock()
    mock_code_db.list_collections.return_value = [{"name": "code__repo", "count": 10}]
    mock_code_db.collection_info.return_value = {}
    mock_col = MagicMock()
    mock_col.get.return_value = {"ids": [], "metadatas": []}
    mock_code_db._client.get_collection.return_value = mock_col
    mock_knowledge_db = MagicMock()

    with patch("nexus.commands.collection.t3_code", return_value=mock_code_db) as m_code, \
         patch("nexus.commands.collection.t3_knowledge", return_value=mock_knowledge_db) as m_k:
        result = runner.invoke(main, ["collection", "info", "code__repo"])

    assert result.exit_code == 0, result.output
    m_code.assert_called_once()
    m_k.assert_not_called()


def test_info_infers_docs_store_from_prefix(runner: CliRunner, env_creds) -> None:
    """I4: info docs__corpus without --type infers docs store from prefix."""
    mock_docs_db = MagicMock()
    mock_docs_db.list_collections.return_value = [{"name": "docs__corpus", "count": 7}]
    mock_docs_db.collection_info.return_value = {}
    mock_col = MagicMock()
    mock_col.get.return_value = {"ids": [], "metadatas": []}
    mock_docs_db._client.get_collection.return_value = mock_col
    mock_knowledge_db = MagicMock()

    with patch("nexus.commands.collection.t3_docs", return_value=mock_docs_db) as m_docs, \
         patch("nexus.commands.collection.t3_knowledge", return_value=mock_knowledge_db) as m_k:
        result = runner.invoke(main, ["collection", "info", "docs__corpus"])

    assert result.exit_code == 0, result.output
    m_docs.assert_called_once()
    m_k.assert_not_called()


def test_info_infers_rdr_store_from_prefix(runner: CliRunner, env_creds) -> None:
    """I4: info rdr__nexus without --type infers rdr store from prefix."""
    mock_rdr_db = MagicMock()
    mock_rdr_db.list_collections.return_value = [{"name": "rdr__nexus-abc12345", "count": 3}]
    mock_rdr_db.collection_info.return_value = {}
    mock_col = MagicMock()
    mock_col.get.return_value = {"ids": [], "metadatas": []}
    mock_rdr_db._client.get_collection.return_value = mock_col
    mock_knowledge_db = MagicMock()

    with patch("nexus.commands.collection.t3_rdr", return_value=mock_rdr_db) as m_rdr, \
         patch("nexus.commands.collection.t3_knowledge", return_value=mock_knowledge_db) as m_k:
        result = runner.invoke(main, ["collection", "info", "rdr__nexus-abc12345"])

    assert result.exit_code == 0, result.output
    m_rdr.assert_called_once()
    m_k.assert_not_called()


def test_delete_infers_code_store_from_prefix(runner: CliRunner, env_creds) -> None:
    """I4: delete code__repo without --type infers code store from prefix (uses local variant)."""
    mock_code_db = MagicMock()
    mock_knowledge_db = MagicMock()

    with patch("nexus.commands.collection.t3_code_local", return_value=mock_code_db) as m_code, \
         patch("nexus.commands.collection.t3_knowledge_local", return_value=mock_knowledge_db) as m_k:
        result = runner.invoke(main, ["collection", "delete", "code__repo", "--yes"])

    assert result.exit_code == 0, result.output
    m_code.assert_called_once()
    m_k.assert_not_called()


# ── I8: delete must not require voyage_api_key ───────────────────────────────


def test_delete_cmd_does_not_require_voyage_api_key_code(
    runner: CliRunner, monkeypatch: pytest.MonkeyPatch
) -> None:
    """I8: 'nx collection delete code__repo' succeeds without voyage_api_key."""
    monkeypatch.delenv("VOYAGE_API_KEY", raising=False)
    mock_db = MagicMock()

    with patch("nexus.commands.collection.t3_code_local", return_value=mock_db):
        result = runner.invoke(main, ["collection", "delete", "--yes", "code__repo"])

    assert result.exit_code == 0, result.output
    mock_db.delete_collection.assert_called_once_with("code__repo")


def test_delete_cmd_does_not_require_voyage_api_key_rdr(
    runner: CliRunner, monkeypatch: pytest.MonkeyPatch
) -> None:
    """I8: 'nx collection delete rdr__nexus' succeeds without voyage_api_key."""
    monkeypatch.delenv("VOYAGE_API_KEY", raising=False)
    mock_db = MagicMock()

    with patch("nexus.commands.collection.t3_rdr_local", return_value=mock_db):
        result = runner.invoke(main, ["collection", "delete", "--yes", "rdr__nexus"])

    assert result.exit_code == 0, result.output
    mock_db.delete_collection.assert_called_once_with("rdr__nexus")


def test_delete_cmd_does_not_require_voyage_api_key_knowledge(
    runner: CliRunner, monkeypatch: pytest.MonkeyPatch
) -> None:
    """I8: 'nx collection delete knowledge__topic' succeeds without voyage_api_key."""
    monkeypatch.delenv("VOYAGE_API_KEY", raising=False)
    mock_db = MagicMock()

    with patch("nexus.commands.collection.t3_knowledge_local", return_value=mock_db):
        result = runner.invoke(main, ["collection", "delete", "--yes", "knowledge__topic"])

    assert result.exit_code == 0, result.output
    mock_db.delete_collection.assert_called_once_with("knowledge__topic")


def test_verify_infers_code_store_from_prefix(runner: CliRunner, env_creds) -> None:
    """I4: verify code__repo without --type infers code store from prefix."""
    mock_code_db = MagicMock()
    mock_code_db.list_collections.return_value = [{"name": "code__repo", "count": 5}]
    mock_knowledge_db = MagicMock()

    with patch("nexus.commands.collection.t3_code", return_value=mock_code_db) as m_code, \
         patch("nexus.commands.collection.t3_knowledge", return_value=mock_knowledge_db) as m_k:
        result = runner.invoke(main, ["collection", "verify", "code__repo"])

    assert result.exit_code == 0, result.output
    m_code.assert_called_once()
    m_k.assert_not_called()
