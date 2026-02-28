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
    db.get_collection_raw.side_effect = lambda name: col_map[name]
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


# ── I2: _open_source_db must not fall back to CloudClient ────────────────────


def test_open_source_db_raises_when_no_path_configured() -> None:
    """I2: _open_source_db raises ClickException when chromadb.path is empty.

    Post-migration, there is no legacy path — falling back silently to
    CloudClient would be a misleading and risky default.
    """
    import click
    from nexus.commands.migrate import _open_source_db

    with patch("nexus.config.load_config", return_value={"chromadb": {}}):
        with pytest.raises(click.ClickException, match="chromadb.path"):
            _open_source_db()


# ── S3: empty source guard ────────────────────────────────────────────────────

# ── I2: source_col.get() must be paginated ────────────────────────────────────

def test_migrate_t3_col_get_uses_limit(runner: CliRunner) -> None:
    """I2: source_col.get() must use limit= to avoid OOM on large collections.

    Without a limit, get() on a large collection loads all docs into memory
    at once.  The migration must paginate with limit=5000.
    """
    small_col = _make_source_col("knowledge__small", 3)
    source_db = _make_source_db([small_col])
    dest_knowledge = _make_dest_db()
    dest_code = _make_dest_db()
    dest_docs = _make_dest_db()
    dest_rdr = _make_dest_db()

    with patch("nexus.commands.migrate._open_source_db", return_value=source_db), \
         patch("nexus.commands.migrate._open_dest_db",
               side_effect=lambda key: {
                   "code_path": dest_code, "docs_path": dest_docs,
                   "rdr_path": dest_rdr, "knowledge_path": dest_knowledge,
               }[key]):
        result = runner.invoke(main, ["migrate", "t3"])

    assert result.exit_code == 0, result.output
    # get() must have been called with a limit= to avoid unbounded fetches
    call_kwargs = small_col.get.call_args.kwargs
    assert "limit" in call_kwargs, "source_col.get() must pass limit= (pagination guard)"
    assert call_kwargs["limit"] == 5000


# ── C5: upsert must be per-page, not accumulated ──────────────────────────────


def _make_paginated_source_col(name: str, n_pages: int, page_size: int = 5000) -> MagicMock:
    """Source collection that returns `n_pages` full pages then an empty page."""
    def _page(offset: int, limit: int, **_kwargs) -> dict:
        start = offset
        end = min(start + limit, n_pages * page_size)
        ids = [f"{name}_id_{i}" for i in range(start, end)]
        return {
            "ids": ids,
            "documents": [f"doc {i}" for i in range(start, end)],
            "embeddings": [[float(i)] for i in range(start, end)],
            "metadatas": [{"k": str(i)} for i in range(start, end)],
        }

    col = MagicMock()
    col.name = name
    col.get.side_effect = lambda include, limit, offset: _page(offset, limit)
    col.count.return_value = n_pages * page_size
    return col


def test_migrate_t3_upserts_per_page_not_accumulated(runner: CliRunner) -> None:
    """C5: migrate_t3_cmd calls dest_col.upsert() once per page, not once for all pages."""
    # 2 full pages of 5000 + partial final page
    source_col = _make_paginated_source_col("code__repo", n_pages=2, page_size=5000)
    # Adjust: last page has fewer items to signal end
    page_size = 5000
    calls: list[dict] = []

    def _page(include, limit, offset):
        start = offset
        if start >= 2 * page_size:
            return {"ids": [], "documents": [], "embeddings": [], "metadatas": []}
        end = min(start + limit, 2 * page_size + 3)  # partial third "page" won't exist
        end = min(start + limit, 2 * page_size)
        ids = [f"id_{i}" for i in range(start, end)]
        return {
            "ids": ids,
            "documents": [f"d{i}" for i in range(start, end)],
            "embeddings": [[float(i)] for i in range(start, end)],
            "metadatas": [{"k": str(i)} for i in range(start, end)],
        }

    source_col = MagicMock()
    source_col.name = "code__repo"
    source_col.get.side_effect = lambda include, limit, offset: _page(include, limit, offset)
    source_col.count.return_value = 2 * page_size

    source_db = MagicMock()
    source_db.list_collections.return_value = [{"name": "code__repo"}]
    source_db.get_collection_raw.return_value = source_col

    dest_col = MagicMock()
    dest_col.count.return_value = 0
    dest_db = MagicMock()
    dest_db.get_or_create_collection.return_value = dest_col

    with patch("nexus.commands.migrate._open_source_db", return_value=source_db), \
         patch("nexus.commands.migrate._open_dest_db", return_value=dest_db):
        result = runner.invoke(main, ["migrate", "t3"])

    assert result.exit_code == 0, result.output
    # Each page must be upserted immediately — 2 separate upsert calls
    assert dest_col.upsert.call_count == 2, (
        f"Expected 2 per-page upserts, got {dest_col.upsert.call_count}"
    )


# ── I7: _open_source_db and _open_dest_db must resolve paths ──────────────────


def test_open_source_db_resolves_dotdot_in_path() -> None:
    """I7: _open_source_db normalises '..' path components via Path.resolve()."""
    from nexus.commands.migrate import _open_source_db

    dotdot_path = "/tmp/nexus_migrate_test/a/../legacy_store"

    with patch("nexus.config.load_config",
               return_value={"chromadb": {"path": dotdot_path}}), \
         patch("chromadb.PersistentClient") as mock_pc, \
         patch("chromadb.utils.embedding_functions.DefaultEmbeddingFunction"):
        try:
            _open_source_db()
        except Exception:
            pass

    assert mock_pc.called, "PersistentClient was never called"
    actual = mock_pc.call_args.kwargs.get("path", mock_pc.call_args.args[0] if mock_pc.call_args.args else "")
    assert ".." not in actual, f"Path not resolved — '..' still present: {actual!r}"


def test_open_dest_db_resolves_dotdot_in_path() -> None:
    """I7: _open_dest_db normalises '..' path components via Path.resolve()."""
    from nexus.commands.migrate import _open_dest_db

    dotdot_path = "/tmp/nexus_migrate_test/a/../dest_store"

    with patch("nexus.config.load_config",
               return_value={"chromadb": {"code_path": dotdot_path}}), \
         patch("chromadb.PersistentClient") as mock_pc, \
         patch("chromadb.utils.embedding_functions.DefaultEmbeddingFunction"):
        try:
            _open_dest_db("code_path")
        except Exception:
            pass

    assert mock_pc.called, "PersistentClient was never called"
    actual = mock_pc.call_args.kwargs.get("path", mock_pc.call_args.args[0] if mock_pc.call_args.args else "")
    assert ".." not in actual, f"Path not resolved — '..' still present: {actual!r}"


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


# ── S2: _PREFIX_TO_STORE in migrate.py must be a tuple (not dict) ─────────────


def test_migrate_prefix_to_store_is_tuple() -> None:
    """S2: _PREFIX_TO_STORE in migrate.py must be tuple[tuple[str,str],...] for
    type consistency with collection.py — both consumers of STORE_PREFIX_MAP."""
    from nexus.commands.migrate import _PREFIX_TO_STORE

    assert isinstance(_PREFIX_TO_STORE, tuple), (
        f"_PREFIX_TO_STORE must be tuple, got {type(_PREFIX_TO_STORE).__name__}; "
        "both collection.py and migrate.py should use the same type"
    )
    for item in _PREFIX_TO_STORE:
        assert isinstance(item, tuple) and len(item) == 2, (
            f"each entry must be a (prefix, store) tuple, got {item!r}"
        )


# ── S3: upsert failure during migration must propagate, not be silently swallowed


def test_migrate_t3_upsert_failure_propagates_exception(
    runner: CliRunner,
) -> None:
    """S3: if dest_col.upsert() raises during migration, the exception must
    propagate (non-zero exit) — partial migration is detectable on retry."""
    source_col = _make_source_col("knowledge__test", 2)
    source_col.get.return_value = {
        "ids": ["a", "b"],
        "documents": ["doc a", "doc b"],
        "embeddings": [[0.1], [0.2]],
        "metadatas": [{}, {}],
    }
    source_db = MagicMock()
    source_db.list_collections.return_value = [{"name": "knowledge__test"}]
    source_db.get_collection_raw.return_value = source_col

    dest_col = MagicMock()
    dest_col.count.return_value = 0
    dest_col.upsert.side_effect = RuntimeError("disk full")
    dest_db = MagicMock()
    dest_db.get_or_create_collection.return_value = dest_col

    with patch("nexus.commands.migrate._open_source_db", return_value=source_db), \
         patch("nexus.commands.migrate._open_dest_db", return_value=dest_db):
        result = runner.invoke(main, ["migrate", "t3"])

    assert result.exit_code != 0, (
        "upsert failure must propagate as non-zero exit code, not be swallowed"
    )
