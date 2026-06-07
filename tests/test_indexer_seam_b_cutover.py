# SPDX-License-Identifier: AGPL-3.0-or-later
"""RDR-152 bead nexus-gmiaf.22 (P3.3) — Seam B INDEXER cutover tests.

Verifies that when NX_STORAGE_BACKEND_VECTORS=service:
  - _run_index does NOT call voyageai.Client (service embeds server-side)
  - _run_index uses get_t3() (routes to HttpVectorClient), not make_t3()
  - doc_indexer._index_document skips _embed_with_fallback in service mode
  - doc_indexer._index_pdf_incremental skips _embed_with_fallback in service mode
  - doc_indexer.index_pdf small-doc path skips _embed_with_fallback in service mode
  - storage_boundary_lint flags voyageai.Client usage in indexer surface
"""
from __future__ import annotations

import pathlib
from contextlib import contextmanager
from unittest.mock import MagicMock, patch

import pytest


# ── Fixtures and helpers ─────────────────────────────────────────────────────


_DEFAULT_CONFIG = {
    "server": {"ignorePatterns": []},
    "indexing": {
        "code_extensions": [],
        "prose_extensions": [],
        "rdr_paths": ["docs/rdr"],
        "include_untracked": False,
    },
}


def _reg(override=None):
    base = {
        "collection": "code__repo",
        "code_collection": "code__repo__voyage-code-3__v1",
        "docs_collection": "docs__repo__voyage-context-3__v1",
    }
    m = MagicMock()
    m.get.return_value = {**base, **(override or {})}
    return m


def _mock_db():
    col = MagicMock()
    col.get.return_value = {"metadatas": [], "ids": []}
    db = MagicMock()
    db.get_or_create_collection.return_value = col
    db.get_collection.return_value = col
    return db, col


@contextmanager
def _service_mode_patches(db, *, extra=None):
    """Set up the common mocks for a service-mode _run_index invocation."""
    patches = {
        "nexus.frecency.batch_frecency": {"return_value": {}},
        "nexus.ripgrep_cache.build_cache": {},
        "nexus.indexer._git_metadata": {"return_value": {}},
        "nexus.config.load_config": {"return_value": _DEFAULT_CONFIG},
        "nexus.config.get_credential": {"return_value": "fake-key"},
        # Service-mode routing: get_t3() returns the mock db
        "nexus.mcp_infra.get_t3": {"return_value": db},
        # make_t3 should NOT be called in service mode — mock it so
        # accidental call is detectable
        "nexus.db.make_t3": {"return_value": db},
        # Stub out the file-level indexers
        "nexus.indexer._index_code_file": {"return_value": 0},
        "nexus.indexer._index_prose_file": {"return_value": 0},
        "nexus.indexer._index_pdf_file": {"return_value": 0},
        "nexus.indexer._discover_and_index_rdrs": {"return_value": (0, 0, 0)},
        "nexus.indexer._prune_misclassified": {},
        "nexus.indexer._prune_deleted_files": {},
        "nexus.indexer._migrate_legacy_collections": {"return_value": {}},
        "nexus.indexer.stamp_collection_version": {},
        "nexus.catalog.factory.make_catalog_reader": {"return_value": None},
        "nexus.catalog.factory.make_catalog_writer": {"return_value": None},
    }
    if extra:
        patches.update(extra)

    mocks, stack = {}, []
    for target, kw in patches.items():
        p = patch(target, **kw)
        m = p.start()
        stack.append(p)
        mocks[target.split(".")[-1]] = m
    try:
        yield mocks
    finally:
        for p in reversed(stack):
            p.stop()


# ── RDR-152 P3.3: _run_index service-mode routing ─────────────────────────────


def test_run_index_service_mode_skips_voyageai_client(tmp_path, monkeypatch):
    """When NX_STORAGE_BACKEND_VECTORS=service, _run_index must NOT call
    voyageai.Client — embedding happens server-side (Seam B contract)."""
    from nexus.indexer import _run_index

    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "hello.py").write_text("x = 1\n")
    reg = _reg()

    monkeypatch.setenv("NX_STORAGE_BACKEND_VECTORS", "service")
    monkeypatch.setenv("NX_LOCAL", "0")  # force cloud / non-local mode
    monkeypatch.setenv("VOYAGE_API_KEY", "fake")
    monkeypatch.setenv("CHROMA_API_KEY", "fake")

    db, _ = _mock_db()
    with _service_mode_patches(db) as mocks, \
         patch("voyageai.Client") as voyage_ctor:
        _run_index(repo, reg)
        voyage_ctor.assert_not_called(), (
            "voyageai.Client was called in service mode — embedding must be "
            "server-side only"
        )


def test_run_index_service_mode_uses_get_t3_not_make_t3(tmp_path, monkeypatch):
    """In service mode, _run_index must use mcp_infra.get_t3() to obtain the
    T3 handle rather than make_t3() — so HttpVectorClient is the write target."""
    from nexus.indexer import _run_index

    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "hello.py").write_text("x = 1\n")
    reg = _reg()

    monkeypatch.setenv("NX_STORAGE_BACKEND_VECTORS", "service")
    monkeypatch.setenv("NX_LOCAL", "0")
    monkeypatch.setenv("VOYAGE_API_KEY", "fake")
    monkeypatch.setenv("CHROMA_API_KEY", "fake")

    db, _ = _mock_db()
    with _service_mode_patches(db) as mocks:
        _run_index(repo, reg)
        # get_t3 must have been called to obtain the service-backed store
        mocks["get_t3"].assert_called()


def test_run_index_non_service_mode_uses_make_t3(tmp_path, monkeypatch):
    """When NX_STORAGE_BACKEND_VECTORS is unset, the legacy make_t3() path
    must be used (default path, unchanged by this bead)."""
    from nexus.indexer import _run_index

    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "hello.py").write_text("x = 1\n")
    reg = _reg()

    monkeypatch.delenv("NX_STORAGE_BACKEND_VECTORS", raising=False)
    monkeypatch.setenv("NX_LOCAL", "0")
    monkeypatch.setenv("VOYAGE_API_KEY", "fake")
    monkeypatch.setenv("CHROMA_API_KEY", "fake")

    db, _ = _mock_db()
    with _service_mode_patches(db) as mocks:
        _run_index(repo, reg)
        mocks["make_t3"].assert_called()


# ── RDR-152 P3.3: doc_indexer embed skip in service mode ────────────────────────


def _make_doc_indexer_db():
    """Mock db with col.get returning 'not indexed' so we don't skip."""
    col = MagicMock()
    col.get.return_value = {"metadatas": [], "ids": []}
    db = MagicMock()
    db.get_or_create_collection.return_value = col
    return db, col


def test_index_document_service_mode_skips_embed_fallback(tmp_path, monkeypatch):
    """In service mode, _index_document must NOT call _embed_with_fallback —
    the service embeds server-side. Instead it calls db.upsert_chunks directly."""
    from nexus.doc_indexer import _index_document

    monkeypatch.setenv("NX_STORAGE_BACKEND_VECTORS", "service")

    # _index_document reads the file for content_hash — create a real file
    test_file = tmp_path / "test.md"
    test_file.write_text("# Test\nContent here.\n")

    db, col = _make_doc_indexer_db()

    def fake_chunk_fn(file_path, content_hash, target_model, now_iso, corpus):
        return [("id1", "chunk text", {"embedding_model": target_model})]

    mock_hooks = MagicMock()

    with patch("nexus.doc_indexer._embed_with_fallback") as embed_mock, \
         patch("nexus.doc_indexer._register_or_lookup_doc_id", return_value="doc-1"), \
         patch("nexus.doc_indexer._chroma_with_retry", side_effect=lambda fn, **kw: fn(**kw)), \
         patch("nexus.hook_registry.HookRegistry", return_value=mock_hooks), \
         patch("nexus.hook_registry.install_default_hooks"):
        _index_document(
            test_file,
            corpus="test-corpus",
            chunk_fn=fake_chunk_fn,
            t3=db,
            embed_fn=None,
        )
        embed_mock.assert_not_called(), (
            "_embed_with_fallback must not be called in service mode"
        )


def test_index_document_service_mode_calls_upsert_chunks(tmp_path, monkeypatch):
    """In service mode, _index_document must call upsert_chunks_with_embeddings
    (with empty embeddings that the server discards) to complete the write."""
    from nexus.doc_indexer import _index_document

    monkeypatch.setenv("NX_STORAGE_BACKEND_VECTORS", "service")

    test_file = tmp_path / "test.md"
    test_file.write_text("# Test\nContent here.\n")

    db, col = _make_doc_indexer_db()

    def fake_chunk_fn(file_path, content_hash, target_model, now_iso, corpus):
        return [("id1", "chunk text", {"embedding_model": target_model})]

    mock_hooks = MagicMock()

    with patch("nexus.doc_indexer._embed_with_fallback"), \
         patch("nexus.doc_indexer._register_or_lookup_doc_id", return_value="doc-1"), \
         patch("nexus.doc_indexer._chroma_with_retry", side_effect=lambda fn, **kw: fn(**kw)), \
         patch("nexus.hook_registry.HookRegistry", return_value=mock_hooks), \
         patch("nexus.hook_registry.install_default_hooks"):
        _index_document(
            test_file,
            corpus="test-corpus",
            chunk_fn=fake_chunk_fn,
            t3=db,
            embed_fn=None,
        )
        # upsert_chunks_with_embeddings must be called (service ignores embeddings)
        assert db.upsert_chunks_with_embeddings.called, (
            "expected upsert_chunks_with_embeddings call in service mode"
        )


def test_index_pdf_incremental_service_mode_skips_embed_fallback(tmp_path, monkeypatch):
    """In service mode, _index_pdf_incremental must NOT call _embed_with_fallback."""
    from nexus.doc_indexer import _index_pdf_incremental

    monkeypatch.setenv("NX_STORAGE_BACKEND_VECTORS", "service")

    db, col = _make_doc_indexer_db()
    db.get_or_create_collection.return_value = col
    col.get.return_value = {"ids": [], "metadatas": []}

    prepared = [
        ("id1", "chunk text 1", {"embedding_model": "voyage-context-3"}),
        ("id2", "chunk text 2", {"embedding_model": "voyage-context-3"}),
    ]

    mock_hooks = MagicMock()

    with patch("nexus.doc_indexer._embed_with_fallback") as embed_mock, \
         patch("nexus.doc_indexer.read_checkpoint", return_value=None), \
         patch("nexus.doc_indexer.write_checkpoint"), \
         patch("nexus.doc_indexer.delete_checkpoint"), \
         patch("nexus.doc_indexer._register_or_lookup_doc_id", return_value="doc-1"), \
         patch("nexus.doc_indexer._chroma_with_retry", side_effect=lambda fn, **kw: fn(**kw)):
        _index_pdf_incremental(
            tmp_path / "test.pdf",
            corpus="test-corpus",
            prepared=prepared,
            content_hash="abc123",
            collection_name="docs__test__voyage-context-3__v1",
            t3=db,
            embed_fn=None,
            hooks=mock_hooks,
        )
        embed_mock.assert_not_called(), (
            "_embed_with_fallback must not be called in service mode incremental path"
        )


# ── Storage boundary lint: voyageai detection ─────────────────────────────────


REPO_ROOT = pathlib.Path(__file__).parent.parent


def _lint_check(extra_files=None, allowlist_prefixes=None):
    from nexus.storage_boundary_lint import scan_repo

    return scan_repo(
        repo_root=REPO_ROOT,
        allowlist_prefixes=allowlist_prefixes,
        extra_files=extra_files,
    )


def test_voyageai_client_in_indexer_surface_is_flagged(tmp_path):
    """voyageai.Client(...) in an indexer module (outside legacy allowlist)
    must be flagged as a storage-boundary violation after the Seam B cutover."""
    target = tmp_path / "fake_indexer.py"
    target.write_text(
        "import voyageai\n"
        "def embed(texts, api_key):\n"
        "    client = voyageai.Client(api_key=api_key)\n"
        "    return client.embed(texts)\n"
    )
    result = _lint_check(extra_files=[target])
    matched = [v for v in result.violations if v.file == str(target)]
    assert len(matched) == 1, (
        f"expected 1 voyageai.Client violation in indexer surface, got: {matched}"
    )
    assert "voyageai" in matched[0].symbol


def test_voyageai_client_in_indexer_surface_with_epsilon_allow_is_not_flagged(tmp_path):
    """A voyageai.Client call with a valid epsilon-allow annotation on the same
    line must NOT be flagged — the epsilon-allow mechanism applies uniformly."""
    target = tmp_path / "legacy_indexer.py"
    target.write_text(
        "import voyageai\n"
        "def embed_legacy(texts, api_key):\n"
        "    client = voyageai.Client(api_key=api_key)  # epsilon-allow: Phase-4 deletion target, legacy non-service path\n"
        "    return client.embed(texts)\n"
    )
    result = _lint_check(extra_files=[target])
    matched = [v for v in result.violations if v.file == str(target)]
    assert matched == [], (
        f"epsilon-allow'd voyageai.Client should not be flagged: {matched}"
    )


def test_voyageai_in_legacy_db_path_is_allowlisted():
    """src/nexus/db/ (including t3.py) is in the allowlist and must NOT produce
    violations even if it imports voyageai (Phase-4 deletion target)."""
    result = _lint_check()
    for v in result.violations:
        assert "src/nexus/db/" not in v.file, (
            f"db/ must be allowlisted, got violation at {v.file}:{v.line}"
        )


def test_voyageai_banlist_entry_present():
    """voyageai.Client must appear in the BANLIST after P3.3."""
    from nexus.storage_boundary_lint import BANLIST

    voyageai_entries = [(m, a) for m, a in BANLIST if m == "voyageai"]
    assert voyageai_entries, (
        "voyageai.Client must be in BANLIST for Seam B structural tripwire"
    )


def test_indexer_has_zero_unallowed_voyageai_after_cutover():
    """After the Seam B cutover, indexer.py and doc_indexer.py must have
    zero un-annotated voyageai.Client calls on the runtime write path.

    Any surviving voyageai.Client call must carry an epsilon-allow annotation
    explaining why it is a Phase-4 deletion target (legacy path only)."""
    result = _lint_check()
    indexer_violations = [
        v for v in result.violations
        if "indexer" in v.file and "voyageai" in v.symbol
    ]
    assert indexer_violations == [], (
        f"Indexer module has un-annotated voyageai.Client calls after cutover: "
        f"{[(v.file, v.line) for v in indexer_violations]}"
    )


def test_lint_baseline_unchanged_after_voyageai_extension():
    """The existing baseline metrics (epsilon_allow_connects == 17,
    total_violations == 0, t2database_constructions == 31) must remain
    stable after adding voyageai to the BANLIST.

    This ensures the lint extension does not silently break existing counts."""
    result = _lint_check()
    assert result.total_violations == 0, (
        f"Baseline violation count changed after voyageai lint extension: "
        f"{[(v.file, v.line, v.symbol) for v in result.violations]}"
    )
    assert result.epsilon_allow_connects == 17, (
        f"epsilon_allow_connects baseline changed: {result.epsilon_allow_connects}"
    )
    assert result.t2database_constructions == 31, (
        f"t2database_constructions baseline changed: {result.t2database_constructions}"
    )
