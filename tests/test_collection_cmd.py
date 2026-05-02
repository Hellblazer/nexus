# SPDX-License-Identifier: AGPL-3.0-or-later
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
    monkeypatch.setenv("CHROMA_API_KEY", "test-chroma-key")
    monkeypatch.setenv("VOYAGE_API_KEY", "test-voyage-key")
    monkeypatch.setenv("CHROMA_TENANT", "test-tenant")
    monkeypatch.setenv("CHROMA_DATABASE", "test-db")


@pytest.fixture
def mock_db():
    return MagicMock()


def _invoke(runner, mock_db, args, **kwargs):
    with patch("nexus.commands.collection._t3", return_value=mock_db):
        return runner.invoke(main, ["collection", *args], **kwargs)


# ── list ────────────────────────────────────────────────────────────────────


def test_list_empty(runner, env_creds, mock_db) -> None:
    mock_db.list_collections.return_value = []
    result = _invoke(runner, mock_db, ["list"])
    assert result.exit_code == 0
    assert "No collections" in result.output


def test_list_shows_names_and_counts(runner, env_creds, mock_db) -> None:
    mock_db.list_collections.return_value = [
        {"name": "code__myrepo", "count": 42},
        {"name": "knowledge__topic", "count": 7},
    ]
    result = _invoke(runner, mock_db, ["list"])
    assert result.exit_code == 0
    assert "code__myrepo" in result.output
    assert "42" in result.output
    assert "knowledge__topic" in result.output


# ── info ────────────────────────────────────────────────────────────────────


def test_info_not_found(runner, env_creds, mock_db) -> None:
    mock_db.list_collections.return_value = []
    result = _invoke(runner, mock_db, ["info", "no_such"])
    assert result.exit_code != 0
    assert "not found" in result.output.lower()


def _mock_db_for_info(mock_db, name, count, metadatas):
    mock_db.list_collections.return_value = [{"name": name, "count": count}]
    mock_db.collection_info.return_value = {"count": count, "metadata": {}}
    mock_col = MagicMock()
    mock_col.get.return_value = {
        "ids": [f"id{i}" for i in range(len(metadatas))],
        "metadatas": metadatas,
    }
    mock_db.get_or_create_collection.return_value = mock_col


@pytest.mark.parametrize("col_name,expected_model", [
    ("code__nexus", "voyage-code-3"),
    ("knowledge__research", "voyage-context-3"),
])
def test_info_shows_embedding_model(runner, env_creds, mock_db, col_name, expected_model) -> None:
    _mock_db_for_info(mock_db, col_name, 42, [{}])
    result = _invoke(runner, mock_db, ["info", col_name])
    assert result.exit_code == 0, result.output
    assert expected_model in result.output


def test_info_shows_last_indexed_when_metadata_exists(runner, env_creds, mock_db) -> None:
    _mock_db_for_info(mock_db, "knowledge__test", 3, [
        {"indexed_at": "2026-02-20T08:00:00+00:00"},
        {"indexed_at": "2026-02-22T10:23:45+00:00"},
        {"indexed_at": "2026-02-21T12:00:00+00:00"},
    ])
    result = _invoke(runner, mock_db, ["info", "knowledge__test"])
    assert result.exit_code == 0, result.output
    assert "2026-02-22T10:23:45+00:00" in result.output


def test_info_shows_unknown_when_no_indexed_at(runner, env_creds, mock_db) -> None:
    _mock_db_for_info(mock_db, "knowledge__legacy", 2, [
        {"title": "doc_without_ts"},
        {"title": "another_without_ts"},
    ])
    result = _invoke(runner, mock_db, ["info", "knowledge__legacy"])
    assert result.exit_code == 0, result.output
    assert "unknown" in result.output.lower()


# ── delete ──────────────────────────────────────────────────────────────────


@pytest.mark.parametrize("flag", ["--yes", "--confirm"])
def test_delete_with_confirmation_flag(runner, env_creds, mock_db, flag) -> None:
    result = _invoke(runner, mock_db, ["delete", "old", flag])
    assert result.exit_code == 0
    assert "Deleted" in result.output
    mock_db.delete_collection.assert_called_once()


def test_delete_aborts_without_confirmation(runner, env_creds, mock_db) -> None:
    result = _invoke(runner, mock_db, ["delete", "old"], input="n\n")
    assert result.exit_code != 0
    mock_db.delete_collection.assert_not_called()


# ── verify ──────────────────────────────────────────────────────────────────


def test_verify_not_found(runner, env_creds, mock_db) -> None:
    mock_db.list_collections.return_value = []
    result = _invoke(runner, mock_db, ["verify", "missing"])
    assert result.exit_code != 0
    assert "not found" in result.output.lower()


def test_verify_without_deep(runner, env_creds, mock_db) -> None:
    mock_db.list_collections.return_value = [{"name": "knowledge__test", "count": 42}]
    result = _invoke(runner, mock_db, ["verify", "knowledge__test"])
    assert result.exit_code == 0, result.output
    assert "42" in result.output
    assert "OK" in result.output
    mock_db.search.assert_not_called()


def _verify_deep(runner, mock_db, col_name, count, verify_result, verify_side_effect=None):
    from nexus.db.t3 import VerifyResult
    mock_db.list_collections.return_value = [{"name": col_name, "count": count}]
    patch_kwargs = {"return_value": verify_result} if verify_side_effect is None else {"side_effect": verify_side_effect}
    with patch("nexus.commands.collection._t3", return_value=mock_db), \
         patch("nexus.db.t3.verify_collection_deep", **patch_kwargs) as mock_vcd:
        result = runner.invoke(main, ["collection", "verify", col_name, "--deep"])
    return result, mock_vcd


def test_verify_deep_healthy(runner, env_creds, mock_db) -> None:
    from nexus.db.t3 import VerifyResult
    vr = VerifyResult(status="healthy", doc_count=5, probe_doc_id="doc1", distance=0.1, metric="l2")
    result, mock_vcd = _verify_deep(runner, mock_db, "knowledge__test", 5, vr)
    assert result.exit_code == 0, result.output
    mock_vcd.assert_called_once_with(mock_db, "knowledge__test")
    assert "OK" in result.output or "health" in result.output.lower()


def test_verify_deep_empty_skipped(runner, env_creds, mock_db) -> None:
    from nexus.db.t3 import VerifyResult
    vr = VerifyResult(status="skipped", doc_count=0)
    result, _ = _verify_deep(runner, mock_db, "knowledge__empty", 0, vr)
    assert result.exit_code == 0, result.output
    assert "skipped" in result.output.lower() or "0" in result.output


def test_verify_deep_error_exits_one(runner, env_creds, mock_db) -> None:
    result, _ = _verify_deep(
        runner, mock_db, "knowledge__broken", 3, None,
        verify_side_effect=RuntimeError("embedding service unavailable"),
    )
    assert result.exit_code != 0
    assert "embedding service unavailable" in result.output or "Error" in result.output


# ── reindex ─────────────────────────────────────────────────────────────────


def test_reindex_not_found(runner, env_creds, mock_db) -> None:
    mock_db.collection_info.side_effect = KeyError("Collection not found: 'nonexistent__col'")
    result = _invoke(runner, mock_db, ["reindex", "nonexistent__col"])
    assert result.exit_code != 0
    assert "not found" in result.output.lower() or "error" in result.output.lower()


def _setup_reindex_mock(mock_db, metadatas_check, metadatas_batch, before_count=1, after_count=1):
    from nexus.db.t3 import VerifyResult
    mock_db.collection_info.side_effect = [
        {"count": before_count, "metadata": {}},
        {"count": after_count, "metadata": {}},
    ]
    mock_col = MagicMock()
    mock_col.get.side_effect = [
        {"ids": [f"id{i}" for i in range(len(metadatas_check))], "metadatas": metadatas_check},
        {"ids": [f"id{i}" for i in range(len(metadatas_batch))], "metadatas": metadatas_batch},
    ]
    mock_db.get_or_create_collection.return_value = mock_col
    mock_db.delete_collection.return_value = None
    return VerifyResult(status="skipped", doc_count=after_count)


def test_reindex_aborts_on_sourceless(runner, env_creds, mock_db) -> None:
    mock_db.collection_info.return_value = {"count": 3, "metadata": {}}
    mock_col = MagicMock()
    mock_col.get.return_value = {
        "ids": ["a", "b", "c"],
        "metadatas": [{"source_path": "/some/file.md"}, {}, {"source_path": ""}],
    }
    mock_db.get_or_create_collection.return_value = mock_col
    result = _invoke(runner, mock_db, ["reindex", "knowledge__test"])
    assert result.exit_code != 0
    assert any(w in result.output.lower() for w in ("sourceless", "source_path", "force", "lost"))


def test_reindex_refuses_when_all_entries_sourceless(
    runner, env_creds, mock_db,
) -> None:
    """GitHub #367: when every entry is store_put-only (no source_path),
    --force must NOT bypass the safety check. The operation has no source
    to re-index from and would destroy all data with no recovery path.

    The fix routes the user to ``nx collection delete`` for an explicit
    delete and refuses regardless of --force. Mirrors the failure mode
    that lost 28 entries across knowledge__knowledge / knowledge__prd-
    orchestrate / knowledge__conductor-orchestrate / taxonomy__centroids
    when a user ran ``nx collection reindex --force`` during an embedding
    model migration."""
    mock_db.collection_info.return_value = {"count": 23, "metadata": {}}
    mock_col = MagicMock()
    # All 23 entries lack source_path — pure store_put population.
    mock_col.get.return_value = {
        "ids": [f"id{i}" for i in range(23)],
        "metadatas": [{} for _ in range(23)],
    }
    mock_db.get_or_create_collection.return_value = mock_col

    # Plain reindex must refuse.
    result = _invoke(runner, mock_db, ["reindex", "knowledge__knowledge"])
    assert result.exit_code != 0
    assert "refusing to reindex" in result.output.lower()
    assert "nx collection delete" in result.output
    assert "#367" in result.output

    # --force must ALSO refuse (the bug was --force bypassing the safety
    # check when there's nothing to force).
    result = _invoke(
        runner, mock_db, ["reindex", "knowledge__knowledge", "--force"],
    )
    assert result.exit_code != 0
    assert "refusing to reindex" in result.output.lower()
    assert "--force does not bypass" in result.output

    # Critical assertion: delete_collection was NEVER called. The bug
    # deleted first and reported 0 chunks after.
    mock_db.delete_collection.assert_not_called()


def test_reindex_treats_doc_id_only_chunk_as_reindexable(
    runner, env_creds, mock_db, tmp_path,
) -> None:
    """nexus-7b5n: post-prune chunks lack source_path but carry
    doc_id. The safety check must resolve doc_id → catalog file_path
    and treat the chunk as reindexable, not sourceless.

    WITH TEETH: a regression that drops the doc_id branch lands the
    chunk in ``sourceless`` and triggers the all-sourceless refuse
    path even though the catalog has a backing source.
    """
    doc_file = tmp_path / "doc.md"
    doc_file.write_text("# Doc\ncontent")

    mock_db.collection_info.return_value = {"count": 1, "metadata": {}}
    mock_col = MagicMock()
    # Chunk has only doc_id (post-prune shape); source_path absent.
    mock_col.get.return_value = {
        "ids": ["chunk-0"],
        "metadatas": [{"doc_id": "ART-deadbeef"}],
    }
    mock_db.get_or_create_collection.return_value = mock_col
    from nexus.db.t3 import VerifyResult
    mock_db.delete_collection.return_value = None
    after_col = MagicMock()
    after_col.count.return_value = 1
    mock_db.get_or_create_collection.side_effect = [mock_col, after_col]
    vr = VerifyResult(status="healthy", doc_count=1, probe_doc_id="x", distance=0.05, metric="l2")

    with patch(
        "nexus.commands.collection._doc_id_to_file_path",
        return_value=str(doc_file),
    ), \
         patch("nexus.commands.collection._t3", return_value=mock_db), \
         patch("nexus.doc_indexer.index_markdown", return_value=1), \
         patch("nexus.db.t3.verify_collection_deep", return_value=vr):
        result = runner.invoke(
            main, ["collection", "reindex", "docs__test", "--force"],
        )
    # Must NOT hit the all-sourceless refuse branch; reindex proceeds.
    assert "refusing to reindex" not in result.output.lower()
    mock_db.delete_collection.assert_called_once_with("docs__test")


def test_reindex_treats_doc_id_with_no_catalog_entry_as_sourceless(
    runner, env_creds, mock_db,
) -> None:
    """When the chunk carries doc_id but the catalog has no entry
    (catalog gap / pre-Phase-3 chunks), the safety check falls back
    to the sourceless path so the operator runs synthesize-log first.
    """
    mock_db.collection_info.return_value = {"count": 1, "metadata": {}}
    mock_col = MagicMock()
    mock_col.get.return_value = {
        "ids": ["chunk-orphan"],
        "metadatas": [{"doc_id": "ART-orphan"}],
    }
    mock_db.get_or_create_collection.return_value = mock_col

    with patch(
        "nexus.commands.collection._doc_id_to_file_path",
        return_value="",
    ), \
         patch("nexus.commands.collection._t3", return_value=mock_db):
        result = runner.invoke(
            main, ["collection", "reindex", "docs__orphan", "--force"],
        )
    # All chunks resolve to "" file_path → all-sourceless refuse path.
    assert result.exit_code != 0
    assert "refusing to reindex" in result.output.lower()
    mock_db.delete_collection.assert_not_called()


def test_reindex_force_proceeds(runner, env_creds, mock_db, tmp_path) -> None:
    doc_file = tmp_path / "doc.md"
    doc_file.write_text("# Doc\ncontent")
    vr = _setup_reindex_mock(
        mock_db,
        [{"source_path": str(doc_file)}, {}],
        [{"source_path": str(doc_file)}],
        before_count=2, after_count=1,
    )
    with patch("nexus.commands.collection._t3", return_value=mock_db), \
         patch("nexus.doc_indexer.index_markdown", return_value=1), \
         patch("nexus.db.t3.verify_collection_deep", return_value=vr):
        result = runner.invoke(main, ["collection", "reindex", "knowledge__test", "--force"])
    assert result.exit_code == 0, result.output
    mock_db.delete_collection.assert_called_once_with("knowledge__test")


def test_reindex_rdr_uses_batch(runner, env_creds, mock_db, tmp_path) -> None:
    rdr_file = tmp_path / "rdr-001.md"
    rdr_file.write_text("# RDR 001\ncontent here")
    metas = [{"source_path": str(rdr_file)}]
    vr = _setup_reindex_mock(mock_db, metas, metas)
    with patch("nexus.commands.collection._t3", return_value=mock_db), \
         patch("nexus.doc_indexer.batch_index_markdowns", return_value={str(rdr_file): "indexed"}) as mock_batch, \
         patch("nexus.db.t3.verify_collection_deep", return_value=vr):
        result = runner.invoke(main, ["collection", "reindex", "rdr__nexus"])
    assert result.exit_code == 0, result.output
    mock_batch.assert_called_once()
    _, kwargs = mock_batch.call_args
    assert kwargs.get("collection_name") == "rdr__nexus"
    assert kwargs.get("force") is True


def test_reindex_runs_verify_after(runner, env_creds, mock_db, tmp_path) -> None:
    doc_file = tmp_path / "doc.md"
    doc_file.write_text("# Doc\ncontent")
    from nexus.db.t3 import VerifyResult
    metas = [{"source_path": str(doc_file)}]
    vr = VerifyResult(status="healthy", doc_count=2, probe_doc_id="x", distance=0.05, metric="l2")
    _setup_reindex_mock(mock_db, metas, metas, before_count=2, after_count=2)
    with patch("nexus.commands.collection._t3", return_value=mock_db), \
         patch("nexus.doc_indexer.index_markdown", return_value=1), \
         patch("nexus.db.t3.verify_collection_deep", return_value=vr) as mock_vcd:
        result = runner.invoke(main, ["collection", "reindex", "docs__corpus"])
    assert result.exit_code == 0, result.output
    mock_vcd.assert_called_once_with(mock_db, "docs__corpus")
    assert "2" in result.output


def test_reindex_warns_on_missing_source_files(runner, env_creds, mock_db) -> None:
    from nexus.db.t3 import VerifyResult
    missing = "/nonexistent/path/that/does/not/exist.md"
    metas = [{"source_path": missing}]
    vr = VerifyResult(status="skipped", doc_count=0)
    _setup_reindex_mock(mock_db, metas, metas, after_count=0)
    with patch("nexus.commands.collection._t3", return_value=mock_db), \
         patch("nexus.db.t3.verify_collection_deep", return_value=vr):
        result = runner.invoke(main, ["collection", "reindex", "docs__corpus"])
    assert result.exit_code == 0, result.output
    assert any(w in result.output.lower() for w in ("not found", "missing", "warning"))
