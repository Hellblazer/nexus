# SPDX-License-Identifier: AGPL-3.0-or-later
"""Tests for nx collection info and verify command enhancements.

Bead nexus-sg7: verify --deep embedding spot-check
Bead nexus-3q6: info with embedding model + last-indexed
Bead nexus-azsh: collection reindex command
"""
from __future__ import annotations

from unittest.mock import MagicMock, call, patch

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
    with patch("nexus.commands.collection._t3", return_value=mock_db):
        result = runner.invoke(main, ["collection", "list"])
    assert result.exit_code == 0
    assert "No collections" in result.output


def test_list_shows_names_and_counts(runner: CliRunner, env_creds) -> None:
    mock_db = MagicMock()
    mock_db.list_collections.return_value = [
        {"name": "code__myrepo", "count": 42},
        {"name": "knowledge__topic", "count": 7},
    ]
    with patch("nexus.commands.collection._t3", return_value=mock_db):
        result = runner.invoke(main, ["collection", "list"])
    assert result.exit_code == 0
    assert "code__myrepo" in result.output
    assert "42" in result.output
    assert "knowledge__topic" in result.output


# ── info not found ───────────────────────────────────────────────────────────


def test_info_not_found(runner: CliRunner, env_creds) -> None:
    mock_db = MagicMock()
    mock_db.list_collections.return_value = []
    with patch("nexus.commands.collection._t3", return_value=mock_db):
        result = runner.invoke(main, ["collection", "info", "no_such"])
    assert result.exit_code != 0
    assert "not found" in result.output.lower()


# ── delete ───────────────────────────────────────────────────────────────────


def test_delete_with_yes_flag(runner: CliRunner, env_creds) -> None:
    mock_db = MagicMock()
    with patch("nexus.commands.collection._t3", return_value=mock_db):
        result = runner.invoke(main, ["collection", "delete", "old", "--yes"])
    assert result.exit_code == 0
    assert "Deleted" in result.output
    mock_db.delete_collection.assert_called_once_with("old")


def test_delete_aborts_without_confirmation(runner: CliRunner, env_creds) -> None:
    mock_db = MagicMock()
    with patch("nexus.commands.collection._t3", return_value=mock_db):
        result = runner.invoke(main, ["collection", "delete", "old"], input="n\n")
    assert result.exit_code != 0
    mock_db.delete_collection.assert_not_called()


def test_delete_confirm_alias(runner: CliRunner, env_creds) -> None:
    """--confirm is accepted as alias for --yes."""
    mock_db = MagicMock()
    with patch("nexus.commands.collection._t3", return_value=mock_db):
        result = runner.invoke(main, ["collection", "delete", "c", "--confirm"])
    assert result.exit_code == 0
    mock_db.delete_collection.assert_called_once()


# ── verify not found ─────────────────────────────────────────────────────────


def test_verify_not_found(runner: CliRunner, env_creds) -> None:
    mock_db = MagicMock()
    mock_db.list_collections.return_value = []
    with patch("nexus.commands.collection._t3", return_value=mock_db):
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
    """verify --deep: verify_collection_deep is called and health is reported."""
    from nexus.db.t3 import VerifyResult

    mock_db = MagicMock()
    mock_db.list_collections.return_value = [
        {"name": "knowledge__test", "count": 5},
    ]
    mock_result = VerifyResult(
        status="healthy", doc_count=5, probe_doc_id="doc1", distance=0.1, metric="l2"
    )

    with patch("nexus.commands.collection._t3", return_value=mock_db), \
         patch("nexus.db.t3.verify_collection_deep", return_value=mock_result) as mock_vcd:
        result = runner.invoke(
            main, ["collection", "verify", "knowledge__test", "--deep"]
        )

    assert result.exit_code == 0, result.output
    mock_vcd.assert_called_once_with(mock_db, "knowledge__test")
    # Output should confirm health
    assert "OK" in result.output or "health" in result.output.lower()


def test_verify_deep_empty_collection_warns_but_exits_zero(
    runner: CliRunner, env_creds
) -> None:
    """verify --deep on empty collection: skipped status exits 0."""
    from nexus.db.t3 import VerifyResult

    mock_db = MagicMock()
    mock_db.list_collections.return_value = [
        {"name": "knowledge__empty", "count": 0},
    ]
    mock_result = VerifyResult(status="skipped", doc_count=0)

    with patch("nexus.commands.collection._t3", return_value=mock_db), \
         patch("nexus.db.t3.verify_collection_deep", return_value=mock_result):
        result = runner.invoke(
            main, ["collection", "verify", "knowledge__empty", "--deep"]
        )

    assert result.exit_code == 0, result.output
    # Should report skipped / too few
    assert "skipped" in result.output.lower() or "0" in result.output


def test_verify_deep_search_raises_exits_one(
    runner: CliRunner, env_creds
) -> None:
    """verify --deep when verify_collection_deep raises: exits 1 with error message."""
    mock_db = MagicMock()
    mock_db.list_collections.return_value = [
        {"name": "knowledge__broken", "count": 3},
    ]

    with patch("nexus.commands.collection._t3", return_value=mock_db), \
         patch("nexus.db.t3.verify_collection_deep",
               side_effect=RuntimeError("embedding service unavailable")):
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
    # Mock get_or_create_collection to return a collection with no indexed_at in metadata
    mock_col = MagicMock()
    mock_col.get.return_value = {"ids": [], "metadatas": []}
    mock_db.get_or_create_collection.return_value = mock_col

    with patch("nexus.commands.collection._t3", return_value=mock_db):
        result = runner.invoke(main, ["collection", "info", "code__nexus"])

    assert result.exit_code == 0, result.output
    assert "voyage-code-3" in result.output  # index model
    assert "voyage-4" in result.output        # query model


def test_info_shows_embedding_model_for_knowledge_collection(
    runner: CliRunner, env_creds
) -> None:
    """info shows voyage-context-3 for knowledge__ collections (CCE)."""
    mock_db = MagicMock()
    mock_db.list_collections.return_value = [
        {"name": "knowledge__research", "count": 88},
    ]
    mock_db.collection_info.return_value = {"count": 88, "metadata": {}}
    mock_col = MagicMock()
    mock_col.get.return_value = {"ids": [], "metadatas": []}
    mock_db.get_or_create_collection.return_value = mock_col

    with patch("nexus.commands.collection._t3", return_value=mock_db):
        result = runner.invoke(main, ["collection", "info", "knowledge__research"])

    assert result.exit_code == 0, result.output
    assert "voyage-context-3" in result.output


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
    mock_db.get_or_create_collection.return_value = mock_col

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
    mock_db.collection_info.return_value = {"count": 2, "metadata": {}}
    mock_col = MagicMock()
    mock_col.get.return_value = {
        "ids": ["x", "y"],
        "metadatas": [
            {"title": "doc_without_ts"},
            {"title": "another_without_ts"},
        ],
    }
    mock_db.get_or_create_collection.return_value = mock_col

    with patch("nexus.commands.collection._t3", return_value=mock_db):
        result = runner.invoke(main, ["collection", "info", "knowledge__legacy"])

    assert result.exit_code == 0, result.output
    assert "unknown" in result.output.lower()


# ── nexus-azsh: collection reindex ───────────────────────────────────────────


def test_reindex_not_found(runner: CliRunner, env_creds) -> None:
    """Reindex of nonexistent collection fails with error."""
    mock_db = MagicMock()
    mock_db.collection_info.side_effect = KeyError("Collection not found: 'nonexistent__col'")
    with patch("nexus.commands.collection._t3", return_value=mock_db):
        result = runner.invoke(main, ["collection", "reindex", "nonexistent__col"])
    assert result.exit_code != 0
    assert "not found" in result.output.lower() or "error" in result.output.lower()


def test_reindex_aborts_on_sourceless_entries(runner: CliRunner, env_creds) -> None:
    """Reindex aborts for collections with entries missing source_path unless --force."""
    mock_db = MagicMock()
    mock_db.collection_info.return_value = {"count": 3, "metadata": {}}

    mock_col = MagicMock()
    mock_col.get.return_value = {
        "ids": ["a", "b", "c"],
        "metadatas": [
            {"source_path": "/some/file.md"},
            {},  # missing source_path
            {"source_path": ""},  # empty counts as sourceless
        ],
    }
    mock_db.get_or_create_collection.return_value = mock_col

    with patch("nexus.commands.collection._t3", return_value=mock_db):
        result = runner.invoke(main, ["collection", "reindex", "knowledge__test"])

    assert result.exit_code != 0
    output = result.output.lower()
    assert any(word in output for word in ("sourceless", "source_path", "force", "lost"))


def test_reindex_force_proceeds_with_sourceless_entries(runner: CliRunner, env_creds, tmp_path) -> None:
    """With --force, reindex proceeds even with sourceless entries."""
    from nexus.db.t3 import VerifyResult

    doc_file = tmp_path / "doc.md"
    doc_file.write_text("# Doc\ncontent")

    mock_db = MagicMock()
    mock_db.collection_info.side_effect = [
        {"count": 2, "metadata": {}},  # before_count
        {"count": 1, "metadata": {}},  # after_count
    ]

    mock_col = MagicMock()
    mock_col.get.side_effect = [
        # sourceless check (limit=100)
        {
            "ids": ["a", "b"],
            "metadatas": [
                {"source_path": str(doc_file)},
                {},  # sourceless — would abort without --force
            ],
        },
        # pagination batch (limit=300, offset=0) — <300 so loop ends
        {
            "ids": ["a"],
            "metadatas": [{"source_path": str(doc_file)}],
        },
    ]
    mock_db.get_or_create_collection.return_value = mock_col
    mock_db.delete_collection.return_value = None

    mock_verify = VerifyResult(status="skipped", doc_count=1)

    with patch("nexus.commands.collection._t3", return_value=mock_db), \
         patch("nexus.doc_indexer.index_markdown", return_value=1), \
         patch("nexus.db.t3.verify_collection_deep", return_value=mock_verify):
        result = runner.invoke(main, ["collection", "reindex", "knowledge__test", "--force"])

    assert result.exit_code == 0, result.output
    mock_db.delete_collection.assert_called_once_with("knowledge__test")


def test_reindex_rdr_collection(runner: CliRunner, env_creds, tmp_path) -> None:
    """Reindex of rdr__ collection uses batch_index_markdowns, not index_markdown."""
    from nexus.db.t3 import VerifyResult

    rdr_file = tmp_path / "rdr-001.md"
    rdr_file.write_text("# RDR 001\ncontent here")

    mock_db = MagicMock()
    mock_db.collection_info.side_effect = [
        {"count": 1, "metadata": {}},  # before_count
        {"count": 1, "metadata": {}},  # after_count
    ]

    mock_col = MagicMock()
    mock_col.get.side_effect = [
        # sourceless check
        {"ids": ["a"], "metadatas": [{"source_path": str(rdr_file)}]},
        # pagination batch
        {"ids": ["a"], "metadatas": [{"source_path": str(rdr_file)}]},
    ]
    mock_db.get_or_create_collection.return_value = mock_col
    mock_db.delete_collection.return_value = None

    mock_verify = VerifyResult(status="skipped", doc_count=1)

    with patch("nexus.commands.collection._t3", return_value=mock_db), \
         patch("nexus.doc_indexer.batch_index_markdowns", return_value={str(rdr_file): "indexed"}) as mock_batch, \
         patch("nexus.db.t3.verify_collection_deep", return_value=mock_verify):
        result = runner.invoke(main, ["collection", "reindex", "rdr__nexus"])

    assert result.exit_code == 0, result.output
    mock_batch.assert_called_once()
    _, kwargs = mock_batch.call_args
    assert kwargs.get("collection_name") == "rdr__nexus"
    assert kwargs.get("force") is True


def test_reindex_runs_verify_after(runner: CliRunner, env_creds, tmp_path) -> None:
    """Reindex runs verify_collection_deep after re-indexing."""
    from nexus.db.t3 import VerifyResult

    doc_file = tmp_path / "doc.md"
    doc_file.write_text("# Doc\ncontent")

    mock_db = MagicMock()
    mock_db.collection_info.side_effect = [
        {"count": 2, "metadata": {}},  # before_count
        {"count": 2, "metadata": {}},  # after_count
    ]

    mock_col = MagicMock()
    mock_col.get.side_effect = [
        # sourceless check
        {"ids": ["a"], "metadatas": [{"source_path": str(doc_file)}]},
        # pagination
        {"ids": ["a"], "metadatas": [{"source_path": str(doc_file)}]},
    ]
    mock_db.get_or_create_collection.return_value = mock_col
    mock_db.delete_collection.return_value = None

    mock_verify = VerifyResult(status="healthy", doc_count=2, probe_doc_id="x", distance=0.05, metric="l2")

    with patch("nexus.commands.collection._t3", return_value=mock_db), \
         patch("nexus.doc_indexer.index_markdown", return_value=1), \
         patch("nexus.db.t3.verify_collection_deep", return_value=mock_verify) as mock_vcd:
        result = runner.invoke(main, ["collection", "reindex", "docs__corpus"])

    assert result.exit_code == 0, result.output
    mock_vcd.assert_called_once_with(mock_db, "docs__corpus")
    assert "2" in result.output  # before/after count reported


def test_reindex_warns_on_missing_source_files(runner: CliRunner, env_creds) -> None:
    """Reindex warns about source files that no longer exist on disk."""
    from nexus.db.t3 import VerifyResult

    mock_db = MagicMock()
    mock_db.collection_info.side_effect = [
        {"count": 1, "metadata": {}},
        {"count": 0, "metadata": {}},
    ]

    missing_path = "/nonexistent/path/that/does/not/exist.md"
    mock_col = MagicMock()
    mock_col.get.side_effect = [
        # sourceless check
        {"ids": ["a"], "metadatas": [{"source_path": missing_path}]},
        # pagination
        {"ids": ["a"], "metadatas": [{"source_path": missing_path}]},
    ]
    mock_db.get_or_create_collection.return_value = mock_col
    mock_db.delete_collection.return_value = None

    mock_verify = VerifyResult(status="skipped", doc_count=0)

    with patch("nexus.commands.collection._t3", return_value=mock_db), \
         patch("nexus.db.t3.verify_collection_deep", return_value=mock_verify):
        result = runner.invoke(main, ["collection", "reindex", "docs__corpus"])

    assert result.exit_code == 0, result.output
    output = result.output.lower()
    assert any(word in output for word in ("not found", "missing", "warning"))
