# SPDX-License-Identifier: AGPL-3.0-or-later
"""nx migrate t3 — unit tests for T3 migration logic."""
from unittest.mock import MagicMock, patch

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
    dest.collection_info.side_effect = KeyError("not found")
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
    dest.collection_info.side_effect = KeyError("not found")
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
    dest.collection_info.side_effect = KeyError("not found")
    dest_col = MagicMock()
    dest.get_or_create_collection.return_value = dest_col

    from nexus.commands.migrate import migrate_t3_collections

    migrate_t3_collections(source, dest)

    upsert_call = dest_col.upsert.call_args
    # Embeddings must be passed verbatim
    assert upsert_call.kwargs.get("embeddings") == embeddings


# ── P13: auto-create databases ───────────────────────────────────────────────

def test_ensure_databases_creates_four_databases() -> None:
    """ensure_databases calls create_database for each of the four store types."""
    from nexus.commands.migrate import ensure_databases
    from nexus.db.t3 import _STORE_TYPES

    admin = MagicMock()
    ensure_databases(admin, tenant="my-tenant", base="nexus")

    created = [c.args[0] for c in admin.create_database.call_args_list]
    assert set(created) == {f"nexus_{t}" for t in _STORE_TYPES}
    for c in admin.create_database.call_args_list:
        assert c.kwargs.get("tenant") == "my-tenant"


def test_ensure_databases_ignores_already_exists() -> None:
    """ensure_databases silently ignores UniqueConstraintError (database already exists)."""
    from chromadb.errors import UniqueConstraintError
    from nexus.commands.migrate import ensure_databases

    admin = MagicMock()
    admin.create_database.side_effect = UniqueConstraintError()
    # Should not raise
    ensure_databases(admin, tenant="my-tenant", base="nexus")


# ── P14: pagination ───────────────────────────────────────────────────────────

def test_migrate_paginates_large_collections() -> None:
    """Collections with more than _PAGE_SIZE docs are fetched in multiple pages."""
    from nexus.commands.migrate import migrate_t3_collections, _PAGE_SIZE

    total_docs = _PAGE_SIZE + 500  # one full page + partial page

    col = MagicMock()
    col.name = "knowledge__big"
    col.count.return_value = total_docs

    first_page = {
        "ids": [f"id-{i}" for i in range(_PAGE_SIZE)],
        "documents": ["doc"] * _PAGE_SIZE,
        "embeddings": [[0.1]] * _PAGE_SIZE,
        "metadatas": [{}] * _PAGE_SIZE,
    }
    second_page = {
        "ids": [f"id-{_PAGE_SIZE + i}" for i in range(500)],
        "documents": ["doc"] * 500,
        "embeddings": [[0.1]] * 500,
        "metadatas": [{}] * 500,
    }
    col.get.side_effect = [first_page, second_page]

    source = MagicMock()
    source.list_collections.return_value = [col]
    source.get_collection.return_value = col

    dest = MagicMock()
    dest.collection_info.side_effect = KeyError("not found")
    dest_col = MagicMock()
    dest.get_or_create_collection.return_value = dest_col

    result = migrate_t3_collections(source, dest)

    # get() called twice: first full page then remainder
    assert col.get.call_count == 2
    first_call, second_call = col.get.call_args_list
    assert first_call.kwargs["limit"] == _PAGE_SIZE
    assert first_call.kwargs["offset"] == 0
    assert second_call.kwargs["limit"] == 500
    assert second_call.kwargs["offset"] == _PAGE_SIZE

    # upsert called twice (once per page)
    assert dest_col.upsert.call_count == 2
    assert result["knowledge__big"] == total_docs


# ── P15: per-collection exception handling ────────────────────────────────────

def test_migrate_continues_after_per_collection_failure() -> None:
    """A failure on one collection is recorded as -1 and migration continues."""
    good_col = _make_source_col("knowledge__good", ["doc"], [[0.1]])

    bad_col = MagicMock()
    bad_col.name = "knowledge__bad"
    bad_col.count.return_value = 1

    source = MagicMock()
    source.list_collections.return_value = [bad_col, good_col]
    source.get_collection.side_effect = lambda name: bad_col if name == "knowledge__bad" else good_col

    dest = MagicMock()
    dest.collection_info.side_effect = KeyError("not found")
    dest_col_good = MagicMock()

    def get_or_create(name):
        if name == "knowledge__bad":
            raise RuntimeError("upsert validation error")
        return dest_col_good

    dest.get_or_create_collection.side_effect = get_or_create

    from nexus.commands.migrate import migrate_t3_collections

    result = migrate_t3_collections(source, dest)

    assert result["knowledge__bad"] == -1
    assert result["knowledge__good"] == 1


# ── P16: _cloud_admin_client Settings wiring ─────────────────────────────────

def test_cloud_admin_client_calls_admin_client_with_cloud_settings() -> None:
    """_cloud_admin_client passes all seven cloud-wired Settings fields to chromadb.AdminClient.

    All seven fields are version-sensitive (verified against chromadb 0.6.x).
    If any assertion fails after a chromadb upgrade, review the Settings wiring in
    _cloud_admin_client() against the new chromadb.CloudClient internals.
    """
    import chromadb as real_chromadb
    from nexus.commands.migrate import _cloud_admin_client

    with patch.object(real_chromadb, "AdminClient", return_value=MagicMock()) as mock_admin:
        _cloud_admin_client("my-api-key")

    mock_admin.assert_called_once()
    s = mock_admin.call_args[0][0]

    assert s.chroma_api_impl == "chromadb.api.fastapi.FastAPI"
    assert s.chroma_server_host == "api.trychroma.com"
    assert s.chroma_server_http_port == 443
    assert s.chroma_server_ssl_enabled is True
    assert s.chroma_client_auth_provider == (
        "chromadb.auth.token_authn.TokenAuthClientProvider"
    )
    assert s.chroma_client_auth_credentials == "my-api-key"
    assert s.chroma_overwrite_singleton_tenant_database_access_from_auth is True


# ── CLI smoke test ────────────────────────────────────────────────────────────

def test_migrate_t3_missing_credentials_exits_cleanly() -> None:
    """nx migrate t3 exits with error message when credentials are missing."""
    runner = CliRunner()
    with patch("nexus.commands.migrate.get_credential", return_value=None):
        result = runner.invoke(main, ["migrate", "t3"])
    assert result.exit_code != 0
    assert "Error" in result.output


def test_migrate_t3_ensure_databases_called_before_make_t3() -> None:
    """ensure_databases is called before make_t3 — auto-create before connect."""
    call_order: list[str] = []

    def mock_ensure(*_args, **_kwargs):
        call_order.append("ensure_databases")
        return {}

    def mock_make_t3():
        call_order.append("make_t3")
        return MagicMock()

    runner = CliRunner()

    source_mock = MagicMock()
    source_mock.list_collections.return_value = []

    import chromadb as real_chromadb

    with (
        patch("nexus.commands.migrate.get_credential", return_value="fake-val"),
        patch.object(real_chromadb, "CloudClient", return_value=source_mock),
        patch("nexus.commands.migrate._cloud_admin_client", return_value=MagicMock()),
        patch("nexus.commands.migrate.ensure_databases", side_effect=mock_ensure),
        # Patching nexus.commands.migrate.make_t3 because make_t3 is now imported at
        # module level in migrate.py.  If the import location changes, update this target.
        patch("nexus.commands.migrate.make_t3", side_effect=mock_make_t3),
    ):
        runner.invoke(main, ["migrate", "t3"])

    assert "ensure_databases" in call_order, "ensure_databases was not called"
    assert "make_t3" in call_order, "make_t3 was not called"
    ensure_idx = call_order.index("ensure_databases")
    make_t3_idx = call_order.index("make_t3")
    assert ensure_idx < make_t3_idx, "ensure_databases must be called before make_t3"


# ── P17: ensure_databases permission error is non-fatal ───────────────────────

def test_migrate_t3_continues_when_ensure_databases_permission_denied() -> None:
    """When ensure_databases raises (e.g. Chroma Cloud permission denied),
    the command prints a warning and proceeds to make_t3()."""
    import chromadb as real_chromadb

    source_mock = MagicMock()
    source_mock.list_collections.return_value = []

    make_t3_called: list[bool] = []

    def mock_make_t3():
        make_t3_called.append(True)
        return MagicMock()

    runner = CliRunner()
    with (
        patch("nexus.commands.migrate.get_credential", return_value="fake-val"),
        patch.object(real_chromadb, "CloudClient", return_value=source_mock),
        patch("nexus.commands.migrate._cloud_admin_client", return_value=MagicMock()),
        patch(
            "nexus.commands.migrate.ensure_databases",
            side_effect=Exception("Permission denied."),
        ),
        patch("nexus.commands.migrate.make_t3", side_effect=mock_make_t3),
    ):
        result = runner.invoke(main, ["migrate", "t3"])

    # Warning printed, NOT a hard failure
    assert "Warning" in result.output
    assert "permission denied" in result.output.lower()
    # Migration proceeds to make_t3
    assert make_t3_called, "make_t3 was not called after permission denied"
