# SPDX-License-Identifier: AGPL-3.0-or-later
"""nx migrate t3 — unit tests for T3 migration logic."""
from unittest.mock import MagicMock, call, patch

import pytest
from click.testing import CliRunner

from nexus.cli import main


# ── Helpers ──────────────────────────────────────────────────────────────────

def _make_source_col(name: str, docs: list[str], embeddings: list) -> MagicMock:
    """Build a mock ChromaDB collection for the source store."""
    col = MagicMock()
    col.name = name
    col.count.return_value = len(docs)
    col.get.return_value = {
        "ids": [f"id-{i}" for i in range(len(docs))],
        "documents": docs,
        "embeddings": embeddings,
        "metadatas": [{} for _ in docs],
    }
    return col


# ── P9: code routing ──────────────────────────────────────────────────────────

def test_migrate_routes_code_collection_to_code_store() -> None:
    """code__repo from source ends up in dest via get_or_create_collection."""
    embs = [[0.1, 0.2], [0.3, 0.4]]
    col = _make_source_col("code__repo", ["d1", "d2"], embs)

    source = MagicMock()
    source.list_collections.return_value = [col]
    source.get_collection.return_value = col

    dest = MagicMock()
    dest.collection_exists.return_value = False
    dest_col = MagicMock()
    dest.get_or_create_collection.return_value = dest_col

    from nexus.commands.migrate import migrate_t3_collections

    result = migrate_t3_collections(source, dest)

    dest.get_or_create_collection.assert_called_once_with("code__repo")
    dest_col.upsert.assert_called_once()
    assert result["code__repo"] == 2


# ── P10: docs routing ─────────────────────────────────────────────────────────

def test_migrate_routes_docs_collection_to_docs_store() -> None:
    """docs__corpus from source ends up in dest via get_or_create_collection."""
    embs = [[0.5, 0.6]]
    col = _make_source_col("docs__corpus", ["doc1"], embs)

    source = MagicMock()
    source.list_collections.return_value = [col]
    source.get_collection.return_value = col

    dest = MagicMock()
    dest.collection_exists.return_value = False
    dest_col = MagicMock()
    dest.get_or_create_collection.return_value = dest_col

    from nexus.commands.migrate import migrate_t3_collections

    result = migrate_t3_collections(source, dest)

    dest.get_or_create_collection.assert_called_once_with("docs__corpus")
    dest_col.upsert.assert_called_once()
    assert result["docs__corpus"] == 1


# ── P11: idempotency ──────────────────────────────────────────────────────────

def test_migrate_is_idempotent_when_counts_match() -> None:
    """When dest count equals source count, collection is skipped entirely."""
    col = _make_source_col("knowledge__sec", ["a", "b", "c"], [[0.1]] * 3)

    source = MagicMock()
    source.list_collections.return_value = [col]
    source.get_collection.return_value = col

    dest = MagicMock()
    dest.collection_exists.return_value = True
    dest.collection_info.return_value = {"count": 3}  # same as source
    dest_col = MagicMock()
    dest.get_or_create_collection.return_value = dest_col

    from nexus.commands.migrate import migrate_t3_collections

    result = migrate_t3_collections(source, dest)

    # Must not create or upsert to dest collection when counts match
    dest.get_or_create_collection.assert_not_called()
    dest_col.upsert.assert_not_called()
    assert result.get("knowledge__sec", 0) == 0


# ── P12: embeddings verbatim ──────────────────────────────────────────────────

def test_migrate_copies_embeddings_verbatim() -> None:
    """Embeddings from source are passed to dest upsert unchanged (no re-embedding)."""
    embeddings = [[0.11, 0.22, 0.33], [0.44, 0.55, 0.66]]
    col = _make_source_col("knowledge__notes", ["doc a", "doc b"], embeddings)

    source = MagicMock()
    source.list_collections.return_value = [col]
    source.get_collection.return_value = col

    dest = MagicMock()
    dest.collection_exists.return_value = False
    dest_col = MagicMock()
    dest.get_or_create_collection.return_value = dest_col

    from nexus.commands.migrate import migrate_t3_collections

    migrate_t3_collections(source, dest)

    upsert_call = dest_col.upsert.call_args
    # Embeddings must be passed verbatim
    assert upsert_call.kwargs.get("embeddings") == embeddings


# ── CLI smoke test ────────────────────────────────────────────────────────────

def test_migrate_t3_missing_credentials_exits_cleanly() -> None:
    """nx migrate t3 exits with error message when credentials are missing."""
    runner = CliRunner()
    with patch("nexus.commands.migrate.get_credential", return_value=None):
        result = runner.invoke(main, ["migrate", "t3"])
    assert result.exit_code != 0
    assert "Error" in result.output
