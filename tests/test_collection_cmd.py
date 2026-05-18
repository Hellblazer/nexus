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


def test_reindex_treats_phase3_chunk_with_chash_only_as_reindexable(
    runner, env_creds, mock_db, tmp_path,
) -> None:
    """nexus-vn48 (RDR-108 Phase 4 review D-M1): RDR-108 Phase 3
    chunks have neither source_path NOR doc_id in metadata, only
    chunk_text_hash. The safety check must resolve chash -> doc_id
    via the catalog manifest then use the existing doc_id branch.
    Pre-fix the chunk landed in ``sourceless`` and triggered the
    all-sourceless refuse path on every Phase-3 corpus.
    """
    doc_file = tmp_path / "doc.md"
    doc_file.write_text("# Doc\ncontent")

    mock_db.collection_info.return_value = {"count": 1, "metadata": {}}
    mock_col = MagicMock()
    chash = "a" * 64
    # Phase-3 chunk: only chunk_text_hash; no source_path or doc_id.
    mock_col.get.return_value = {
        "ids": ["chunk-phase3"],
        "metadatas": [{"chunk_text_hash": chash}],
    }
    mock_db.get_or_create_collection.return_value = mock_col
    from nexus.db.t3 import VerifyResult
    mock_db.delete_collection.return_value = None
    after_col = MagicMock()
    after_col.count.return_value = 1
    mock_db.get_or_create_collection.side_effect = [mock_col, after_col]
    vr = VerifyResult(status="healthy", doc_count=1, probe_doc_id="x", distance=0.05, metric="l2")

    # Stub Catalog.docs_for_chashes to return the manifest mapping.
    # Catalog is lazy-imported inside reindex_cmd (`from nexus.catalog
    # import Catalog`); patch the source module's attribute so the
    # lazy import resolves to our stub.
    fake_cat = MagicMock()
    fake_cat.docs_for_chashes.return_value = {chash: ["ART-PHASE3"]}

    import nexus.catalog as _cat_mod
    with patch(
        "nexus.commands.collection._doc_id_to_file_path",
        return_value=str(doc_file),
    ), \
         patch("nexus.commands.collection._t3", return_value=mock_db), \
         patch.object(_cat_mod.Catalog, "is_initialized", return_value=True), \
         patch.object(_cat_mod, "Catalog", return_value=fake_cat), \
         patch("nexus.doc_indexer.index_markdown", return_value=1), \
         patch("nexus.db.t3.verify_collection_deep", return_value=vr):
        result = runner.invoke(
            main, ["collection", "reindex", "docs__test", "--force"],
        )
    # Must NOT hit the all-sourceless refuse branch.
    assert "refusing to reindex" not in result.output.lower(), result.output
    mock_db.delete_collection.assert_called_once_with("docs__test")
    # Verify the manifest path actually fired.
    fake_cat.docs_for_chashes.assert_called_once()


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


# ── GH #369: reindex post-processing ────────────────────────────────────────


def test_run_collection_postprocessing_skips_empty_collection_list() -> None:
    """The helper is a no-op on empty input so callers can route
    unconditionally without an outer ``if collections:`` guard.
    """
    from nexus.commands.index import run_collection_postprocessing

    # Should not raise, even without any T2/T3 access setup.
    run_collection_postprocessing([], repo_path=None, quiet=True)


def test_run_collection_postprocessing_swallows_t2_t3_errors() -> None:
    """The chain's outer try/except wraps T3/T2 setup so a missing
    SQLite path (e.g. fresh install with no memory.db yet) is logged
    and silently skipped rather than killing the caller.
    """
    from unittest.mock import patch

    from nexus.commands.index import run_collection_postprocessing

    # nexus-cj1a: after uar6, ``run_collection_postprocessing`` calls
    # ``mcp_infra.get_t3`` (not the legacy ``nexus.db.make_t3``). The
    # prior patch target was vacuous — the test passed because the
    # patch never intercepted the call, not because the chain swallowed
    # the exception. Patch the actual seam.
    with patch("nexus.mcp_infra.get_t3", side_effect=RuntimeError("boom")):
        # Must NOT raise; the chain logs and falls through.
        run_collection_postprocessing(
            ["docs__regression"], repo_path=None, quiet=True,
        )


def test_collections_from_registry_info_filters_excluded() -> None:
    """``taxonomy.local_exclude_collections`` patterns hide registry
    collections from the post-processing chain. Empty registry
    info returns ``[]``.
    """
    from nexus.commands.index import _collections_from_registry_info

    # Empty info -> empty list.
    assert _collections_from_registry_info({}) == []

    # A typical post-RDR-103 registry entry returns its
    # collection + docs_collection pair.
    info = {
        "collection": "code__myrepo__voyage-code-3__v1",
        "docs_collection": "docs__myrepo__voyage-context-3__v1",
    }
    out = _collections_from_registry_info(info)
    # Local-mode default config excludes ``code__*`` from the taxonomy
    # post-processing chain (taxonomy.local_exclude_collections =
    # ["code__*"]); the docs__ collection always surfaces. Cloud-mode
    # CI runs would see both, but unit tests run in local mode.
    assert "docs__myrepo__voyage-context-3__v1" in out


def test_collections_from_registry_info_prefers_conformant_code_collection() -> None:
    """nexus-cxg9: pre-RDR-103 entries keep the legacy non-conformant
    ``collection`` alias even after the conformant ``code_collection``
    is set. The post-pass must enumerate the conformant name only —
    enumerating the alias triggers ``collection_not_found`` every run.
    """
    from nexus.commands.index import _collections_from_registry_info

    info = {
        "collection": "code__nexus-571b8edd",  # legacy alias, no T3 collection
        "code_collection": "code__1-2188__voyage-code-3__v1",
        "docs_collection": "docs__1-2188__voyage-context-3__v1",
        "rdr_collection": "rdr__1-2188__voyage-context-3__v1",
    }
    out = _collections_from_registry_info(info)
    assert "code__nexus-571b8edd" not in out, (
        "legacy non-conformant alias must not be enumerated when a "
        "conformant code_collection is present"
    )
    # Cloud-mode passthrough: rdr/docs always; code__ filtered in local mode only.
    assert "docs__1-2188__voyage-context-3__v1" in out
    assert "rdr__1-2188__voyage-context-3__v1" in out


def test_collections_from_registry_info_dedupes() -> None:
    """nexus-cxg9: when the legacy ``collection`` field happens to equal
    ``code_collection`` (post-RDR-103 fresh registrations), the result
    must not contain duplicates.
    """
    from nexus.commands.index import _collections_from_registry_info

    name = "code__myrepo__voyage-code-3__v1"
    info = {"collection": name, "code_collection": name,
            "docs_collection": "docs__myrepo__voyage-context-3__v1"}
    out = _collections_from_registry_info(info)
    assert len(out) == len(set(out))


def test_run_collection_postprocessing_does_not_pass_alias_through(monkeypatch):
    """Review #757: prove the warning suppression end-to-end. The alias
    must never reach ``_discover_taxonomy`` (which is where the
    ``collection_not_found`` warning would fire). Patches
    ``_discover_taxonomy`` to capture call args; asserts the legacy
    non-conformant name never shows up."""
    from unittest.mock import MagicMock
    import nexus.commands.index as index_mod

    captured: list[str] = []

    def _capture(collection_name, taxonomy, chroma_client, *, force=False):
        captured.append(collection_name)
        return 0

    monkeypatch.setattr(index_mod, "_discover_taxonomy", _capture)

    fake_t3 = MagicMock()
    fake_t3._client = MagicMock()
    # nexus-cj1a: after uar6, ``run_collection_postprocessing`` calls
    # ``mcp_infra.get_t3`` (not the legacy ``nexus.db.make_t3``).
    # Patch the live seam; the prior patches at ``index_mod.make_t3``
    # / ``nexus.db.make_t3`` were vestigial (the call site no longer
    # routes through either name) and only worked because
    # ``_discover_taxonomy`` is patched directly above.
    import nexus.mcp_infra as _mcp_mod
    monkeypatch.setattr(_mcp_mod, "get_t3", lambda: fake_t3)

    info = {
        "collection": "code__nexus-571b8edd",  # legacy non-conformant alias
        "code_collection": "code__1-2188__voyage-code-3__v1",
        "docs_collection": "docs__1-2188__voyage-context-3__v1",
    }
    collections = index_mod._collections_from_registry_info(info)
    index_mod.run_collection_postprocessing(collections, repo_path=None, quiet=True)

    assert "code__nexus-571b8edd" not in captured, (
        "legacy non-conformant alias leaked to _discover_taxonomy — the "
        "collection_not_found warning would fire on every post-pass"
    )


# ── GH #451: --corpus flag for nx index repo ────────────────────────────────


def test_index_repo_cmd_help_shows_corpus_flag(runner) -> None:
    """GH #451: ``nx index repo --help`` advertises the new ``--corpus``
    choice flag for routing prose to ``knowledge__`` instead of ``docs__``.
    """
    from nexus.commands.index import index_repo_cmd

    result = runner.invoke(index_repo_cmd, ["--help"])
    assert result.exit_code == 0
    assert "--corpus" in result.output
    assert "knowledge" in result.output


def test_corpus_knowledge_rewrites_docs_collection(tmp_path, monkeypatch) -> None:
    """GH #451: ``--corpus knowledge`` mutates the registry's
    ``docs_collection`` field so the indexer routes prose to
    ``knowledge__<owner>__...`` instead of ``docs__<owner>__...``.

    Pure registry-level test; the indexer call is mocked so we
    isolate the routing decision from the embed pipeline.
    """
    import json
    from unittest.mock import patch

    from click.testing import CliRunner
    from nexus.commands.index import index_repo_cmd

    # Set up an isolated config dir so the registry path is sandboxed.
    monkeypatch.setenv("NEXUS_CONFIG_DIR", str(tmp_path))
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / ".git").mkdir()  # so _git_ls_files won't crash; not strictly
    (repo / "README.md").write_text("# repo\n")

    runner = CliRunner()
    with patch("nexus.indexer.index_repository", return_value={}) as _idx:
        result = runner.invoke(
            index_repo_cmd,
            [str(repo), "--corpus", "knowledge", "--no-taxonomy"],
            catch_exceptions=False,
        )

    # The registry should now hold a knowledge__ docs_collection.
    repos_json = tmp_path / "repos.json"
    assert repos_json.exists(), "registry never written"
    data = json.loads(repos_json.read_text())
    entry = data["repos"][str(repo.resolve())]
    assert entry["docs_collection"].startswith("knowledge__"), (
        f"--corpus knowledge did not rewrite docs_collection; "
        f"registry holds {entry['docs_collection']!r}"
    )
    assert entry["code_collection"].startswith("code__"), (
        "code_collection must NOT be touched by --corpus knowledge"
    )
    # Output mentions the routing decision.
    assert "knowledge__" in result.output


def test_corpus_default_keeps_docs_collection(tmp_path, monkeypatch) -> None:
    """GH #451: omitting ``--corpus`` (or passing ``--corpus docs``)
    is the default; prose continues to route to ``docs__``.
    """
    import json
    from unittest.mock import patch

    from click.testing import CliRunner
    from nexus.commands.index import index_repo_cmd

    monkeypatch.setenv("NEXUS_CONFIG_DIR", str(tmp_path))
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / ".git").mkdir()
    (repo / "README.md").write_text("# repo\n")

    runner = CliRunner()
    with patch("nexus.indexer.index_repository", return_value={}):
        runner.invoke(
            index_repo_cmd, [str(repo), "--no-taxonomy"], catch_exceptions=False,
        )

    repos_json = tmp_path / "repos.json"
    data = json.loads(repos_json.read_text())
    entry = data["repos"][str(repo.resolve())]
    assert entry["docs_collection"].startswith("docs__"), (
        f"default routing changed; got {entry['docs_collection']!r}"
    )


# ── re-embed (nexus-bw65) ──────────────────────────────────────────────────


def test_reembed_dry_run_reports_count(runner, env_creds, tmp_path) -> None:
    """nexus-bw65: --dry-run (default) reports the chunk count that
    would be re-embedded without making any writes. No Voyage call."""
    import chromadb
    import uuid

    coll_name = f"knowledge__rem_{uuid.uuid4().hex[:12]}"
    client = chromadb.EphemeralClient()
    col = client.get_or_create_collection(coll_name)
    col.add(
        ids=["c1", "c2"], documents=["doc one", "doc two"],
        metadatas=[
            {"content_hash": "h1", "embedding_model": "voyage-3"},
            {"content_hash": "h2", "embedding_model": "voyage-3"},
        ],
    )

    fake_db = MagicMock()
    fake_db._client = client
    fake_db.get_collection = lambda name: client.get_collection(name)

    result = _invoke(
        runner, fake_db,
        ["re-embed", coll_name, "--to", "voyage-code-3"],
    )
    assert result.exit_code == 0, result.output
    assert "dry-run" in result.output
    assert "2" in result.output


def test_reembed_no_dry_run_writes_via_voyage(
    runner, env_creds, tmp_path, monkeypatch,
) -> None:
    """nexus-bw65: --no-dry-run --yes embeds via Voyage and upserts the
    new vectors. Chunk ids + document text + metadata preserved;
    metadata.embedding_model stamped to the target model.
    """
    import chromadb
    import uuid

    coll_name = f"knowledge__rem_{uuid.uuid4().hex[:12]}"
    client = chromadb.EphemeralClient()
    col = client.get_or_create_collection(coll_name)
    col.add(
        ids=["c1", "c2"], documents=["doc one", "doc two"],
        metadatas=[
            {"content_hash": "h1", "embedding_model": "voyage-3"},
            {"content_hash": "h2", "embedding_model": "voyage-3"},
        ],
    )

    fake_db = MagicMock()
    fake_db._client = client
    fake_db.get_collection = lambda name: client.get_collection(name)

    upsert_calls: list[dict] = []

    def _capture_upsert(**kw):
        upsert_calls.append(kw)

    fake_db.upsert_chunks_with_embeddings.side_effect = _capture_upsert

    fake_embed_result = MagicMock()
    fake_embed_result.embeddings = [[0.5] * 1024, [0.7] * 1024]
    fake_voyage_client = MagicMock()
    fake_voyage_client.embed.return_value = fake_embed_result

    with patch("voyageai.Client", return_value=fake_voyage_client):
        result = _invoke(
            runner, fake_db,
            ["re-embed", coll_name, "--to", "voyage-code-3",
             "--no-dry-run", "--yes"],
        )
    assert result.exit_code == 0, result.output
    assert "re-embedded 2" in result.output
    assert len(upsert_calls) == 1
    call = upsert_calls[0]
    assert call["collection_name"] == coll_name
    assert call["ids"] == ["c1", "c2"]
    assert call["documents"] == ["doc one", "doc two"]
    # embedding_model stamped to target model on every row.
    assert all(
        m["embedding_model"] == "voyage-code-3" for m in call["metadatas"]
    )
    # Voyage embed called with target model.
    fake_voyage_client.embed.assert_called_once()
    assert fake_voyage_client.embed.call_args.kwargs["model"] == "voyage-code-3"


def test_reembed_rejects_cce_model(runner, env_creds) -> None:
    """nexus-bw65: voyage-context-3 (CCE) is not supported; click.Choice
    rejects at parse time."""
    result = _invoke(
        runner, MagicMock(),
        ["re-embed", "knowledge__any", "--to", "voyage-context-3"],
    )
    assert result.exit_code != 0
    assert "voyage-context-3" in result.output or "Invalid value" in result.output


def test_reembed_skips_empty_documents(
    runner, env_creds, monkeypatch,
) -> None:
    """nexus-bw65: chunks with empty / whitespace-only document text are
    skipped (Voyage rejects empty strings). Counted under ``skipped``."""
    import chromadb
    import uuid

    coll_name = f"knowledge__rem_{uuid.uuid4().hex[:12]}"
    client = chromadb.EphemeralClient()
    col = client.get_or_create_collection(coll_name)
    col.add(
        ids=["good", "blank"],
        documents=["real content", "   "],
        metadatas=[
            {"content_hash": "h1", "embedding_model": "voyage-3"},
            {"content_hash": "h2", "embedding_model": "voyage-3"},
        ],
    )

    fake_db = MagicMock()
    fake_db._client = client
    fake_db.get_collection = lambda name: client.get_collection(name)

    fake_embed_result = MagicMock()
    fake_embed_result.embeddings = [[0.5] * 1024]
    fake_voyage_client = MagicMock()
    fake_voyage_client.embed.return_value = fake_embed_result

    with patch("voyageai.Client", return_value=fake_voyage_client):
        result = _invoke(
            runner, fake_db,
            ["re-embed", coll_name, "--to", "voyage-3",
             "--no-dry-run", "--yes"],
        )
    assert result.exit_code == 0, result.output
    assert "re-embedded 1" in result.output
    assert "skipped 1" in result.output
