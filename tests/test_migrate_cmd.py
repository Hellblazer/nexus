# SPDX-License-Identifier: AGPL-3.0-or-later
"""nx migrate t3 — T3 store migration command tests (P15 from RDR-004).

All tests follow RED → verify fail → GREEN discipline.
"""
from pathlib import Path
from unittest.mock import MagicMock, call, patch

import pytest
from click.testing import CliRunner

from nexus.cli import main


@pytest.fixture
def runner() -> CliRunner:
    return CliRunner()


def _make_source_col(name: str, doc_count: int) -> MagicMock:
    """Build a mock ChromaDB Collection with `name` and pre-populated docs."""
    ids = [f"{name}_id_{i}" for i in range(doc_count)]
    docs = [f"doc content {i}" for i in range(doc_count)]
    metas = [{"key": f"val_{i}"} for i in range(doc_count)]
    embeddings = [[float(i)] * 4 for i in range(doc_count)]
    col = MagicMock()
    col.name = name
    col.get.return_value = {
        "ids": ids,
        "documents": docs,
        "metadatas": metas,
        "embeddings": embeddings,
    }
    col.count.return_value = doc_count
    return col


def _make_source_db(collections: list[MagicMock]) -> MagicMock:
    """Build a mock T3Database whose list_collections returns the given cols."""
    db = MagicMock()
    db.list_collections.return_value = [{"name": c.name} for c in collections]
    col_map = {c.name: c for c in collections}
    db._client.get_collection.side_effect = lambda name: col_map[name]
    return db


def _make_dest_db() -> MagicMock:
    """Build a mock destination T3Database."""
    db = MagicMock()
    dest_col = MagicMock()
    dest_col.count.return_value = 0  # starts empty
    db.get_or_create_collection.return_value = dest_col
    return db


# ── P15-a: code__ routing ──────────────────────────────────────────────────────

def test_migrate_t3_routes_code_collections_to_code_store(runner: CliRunner) -> None:
    """code__* collections in source are upserted into the code store."""
    code_col = _make_source_col("code__myrepo", 2)
    source_db = _make_source_db([code_col])
    dest_code = _make_dest_db()
    dest_docs = _make_dest_db()
    dest_rdr = _make_dest_db()
    dest_knowledge = _make_dest_db()

    with patch("nexus.commands.migrate._open_source_db", return_value=source_db), \
         patch("nexus.commands.migrate._open_dest_db",
               side_effect=lambda key: {
                   "code_path": dest_code, "docs_path": dest_docs,
                   "rdr_path": dest_rdr, "knowledge_path": dest_knowledge,
               }[key]):
        result = runner.invoke(main, ["migrate", "t3"])

    assert result.exit_code == 0, result.output
    dest_code.get_or_create_collection.assert_called_with("code__myrepo")
    dest_docs.get_or_create_collection.assert_not_called()
    dest_rdr.get_or_create_collection.assert_not_called()


# ── P15-b: docs__ routing ─────────────────────────────────────────────────────

def test_migrate_t3_routes_docs_collections_to_docs_store(runner: CliRunner) -> None:
    """docs__* collections in source are upserted into the docs store."""
    docs_col = _make_source_col("docs__corpus1", 3)
    source_db = _make_source_db([docs_col])
    dest_code = _make_dest_db()
    dest_docs = _make_dest_db()
    dest_rdr = _make_dest_db()
    dest_knowledge = _make_dest_db()

    with patch("nexus.commands.migrate._open_source_db", return_value=source_db), \
         patch("nexus.commands.migrate._open_dest_db",
               side_effect=lambda key: {
                   "code_path": dest_code, "docs_path": dest_docs,
                   "rdr_path": dest_rdr, "knowledge_path": dest_knowledge,
               }[key]):
        result = runner.invoke(main, ["migrate", "t3"])

    assert result.exit_code == 0, result.output
    dest_docs.get_or_create_collection.assert_called_with("docs__corpus1")
    dest_code.get_or_create_collection.assert_not_called()


# ── P15-c: rdr__ routing ──────────────────────────────────────────────────────

def test_migrate_t3_routes_rdr_collections_to_rdr_store(runner: CliRunner) -> None:
    """rdr__* collections in source are upserted into the rdr store."""
    rdr_col = _make_source_col("rdr__nexus-abc12345", 1)
    source_db = _make_source_db([rdr_col])
    dest_code = _make_dest_db()
    dest_docs = _make_dest_db()
    dest_rdr = _make_dest_db()
    dest_knowledge = _make_dest_db()

    with patch("nexus.commands.migrate._open_source_db", return_value=source_db), \
         patch("nexus.commands.migrate._open_dest_db",
               side_effect=lambda key: {
                   "code_path": dest_code, "docs_path": dest_docs,
                   "rdr_path": dest_rdr, "knowledge_path": dest_knowledge,
               }[key]):
        result = runner.invoke(main, ["migrate", "t3"])

    assert result.exit_code == 0, result.output
    dest_rdr.get_or_create_collection.assert_called_with("rdr__nexus-abc12345")
    dest_code.get_or_create_collection.assert_not_called()


# ── P15-d: knowledge__ routing ───────────────────────────────────────────────

def test_migrate_t3_routes_knowledge_collections_to_knowledge_store(runner: CliRunner) -> None:
    """knowledge__* collections in source are upserted into the knowledge store."""
    k_col = _make_source_col("knowledge__topic", 5)
    source_db = _make_source_db([k_col])
    dest_code = _make_dest_db()
    dest_docs = _make_dest_db()
    dest_rdr = _make_dest_db()
    dest_knowledge = _make_dest_db()

    with patch("nexus.commands.migrate._open_source_db", return_value=source_db), \
         patch("nexus.commands.migrate._open_dest_db",
               side_effect=lambda key: {
                   "code_path": dest_code, "docs_path": dest_docs,
                   "rdr_path": dest_rdr, "knowledge_path": dest_knowledge,
               }[key]):
        result = runner.invoke(main, ["migrate", "t3"])

    assert result.exit_code == 0, result.output
    dest_knowledge.get_or_create_collection.assert_called_with("knowledge__topic")
    dest_code.get_or_create_collection.assert_not_called()


# ── P15-e: idempotency — skip when counts match ───────────────────────────────

def test_migrate_t3_skips_collection_when_dest_count_matches(runner: CliRunner) -> None:
    """When destination collection already has same doc count, migration skips upsert."""
    k_col = _make_source_col("knowledge__notes", 4)
    source_db = _make_source_db([k_col])
    dest_code = _make_dest_db()
    dest_docs = _make_dest_db()
    dest_rdr = _make_dest_db()
    dest_knowledge = _make_dest_db()
    # Destination already has same count
    dest_col_existing = MagicMock()
    dest_col_existing.count.return_value = 4
    dest_knowledge.get_or_create_collection.return_value = dest_col_existing

    with patch("nexus.commands.migrate._open_source_db", return_value=source_db), \
         patch("nexus.commands.migrate._open_dest_db",
               side_effect=lambda key: {
                   "code_path": dest_code, "docs_path": dest_docs,
                   "rdr_path": dest_rdr, "knowledge_path": dest_knowledge,
               }[key]):
        result = runner.invoke(main, ["migrate", "t3"])

    assert result.exit_code == 0, result.output
    # Upsert should NOT have been called — counts match
    dest_col_existing.upsert.assert_not_called()


# ── P15-f: upsert when count differs ──────────────────────────────────────────

def test_migrate_t3_upserts_when_dest_count_differs(runner: CliRunner) -> None:
    """When destination has fewer docs than source, migration upserts all source docs."""
    k_col = _make_source_col("knowledge__notes", 4)
    source_db = _make_source_db([k_col])
    dest_code = _make_dest_db()
    dest_docs = _make_dest_db()
    dest_rdr = _make_dest_db()
    dest_knowledge = _make_dest_db()
    dest_col = MagicMock()
    dest_col.count.return_value = 2  # partial migration
    dest_knowledge.get_or_create_collection.return_value = dest_col

    with patch("nexus.commands.migrate._open_source_db", return_value=source_db), \
         patch("nexus.commands.migrate._open_dest_db",
               side_effect=lambda key: {
                   "code_path": dest_code, "docs_path": dest_docs,
                   "rdr_path": dest_rdr, "knowledge_path": dest_knowledge,
               }[key]):
        result = runner.invoke(main, ["migrate", "t3"])

    assert result.exit_code == 0, result.output
    dest_col.upsert.assert_called_once()
    call_kwargs = dest_col.upsert.call_args.kwargs
    assert call_kwargs["ids"] == k_col.get.return_value["ids"]


# ── P15-g: unknown prefix → knowledge store ──────────────────────────────────

def test_migrate_t3_unknown_prefix_goes_to_knowledge_store(runner: CliRunner) -> None:
    """Collections with unrecognised prefix are routed to knowledge store."""
    unknown_col = _make_source_col("custom__stuff", 1)
    source_db = _make_source_db([unknown_col])
    dest_code = _make_dest_db()
    dest_docs = _make_dest_db()
    dest_rdr = _make_dest_db()
    dest_knowledge = _make_dest_db()

    with patch("nexus.commands.migrate._open_source_db", return_value=source_db), \
         patch("nexus.commands.migrate._open_dest_db",
               side_effect=lambda key: {
                   "code_path": dest_code, "docs_path": dest_docs,
                   "rdr_path": dest_rdr, "knowledge_path": dest_knowledge,
               }[key]):
        result = runner.invoke(main, ["migrate", "t3"])

    assert result.exit_code == 0, result.output
    dest_knowledge.get_or_create_collection.assert_called_with("custom__stuff")
    dest_code.get_or_create_collection.assert_not_called()


# ── S3: empty source guard ────────────────────────────────────────────────────

def test_migrate_t3_empty_source_exits_cleanly(runner: CliRunner) -> None:
    """Empty source store exits 0 with informative message; no dest stores are opened."""
    source_db = MagicMock()
    source_db.list_collections.return_value = []
    mock_open_dest = MagicMock()

    with patch("nexus.commands.migrate._open_source_db", return_value=source_db), \
         patch("nexus.commands.migrate._open_dest_db", mock_open_dest):
        result = runner.invoke(main, ["migrate", "t3"])

    assert result.exit_code == 0, result.output
    assert "empty" in result.output.lower() or "nothing" in result.output.lower()
    mock_open_dest.assert_not_called()
