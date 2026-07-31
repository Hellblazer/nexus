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


def test_run_index_batch_flush_forwards_force_re_embed(tmp_path, monkeypatch):
    """RDR-181 §Approach step 3: the ChunkBatcher flush closure defined
    inside _run_index (the batched cross-file write path for code/prose/pdf
    chunks, duoak 2C) must forward --force to force_re_embed on the
    HttpVectorClient upsert. Without this, a forced reindex whose chunks
    land via the shared batcher (rather than the per-file oversize-fallback
    path) would silently keep the server-side embed-skip — the same gap
    the per-file fallback fix closes, but for the dominant batched path.
    """
    from nexus.db.http_vector_client import HttpVectorClient
    from nexus.indexer import _run_index

    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "hello.py").write_text("x = 1\n")
    reg = _reg()

    monkeypatch.setenv("NX_STORAGE_BACKEND_VECTORS", "service")
    monkeypatch.setenv("NX_LOCAL", "0")
    monkeypatch.setenv("VOYAGE_API_KEY", "fake")
    monkeypatch.setenv("CHROMA_API_KEY", "fake")

    db = MagicMock(spec=HttpVectorClient)
    captured: dict = {}

    class _CapturingBatcher:
        def __init__(self, *, flush, **_kw):
            captured["flush"] = flush

        def add(self, *_a, **_kw):
            return False  # never staged — the per-file indexers are stubbed anyway

        def drain(self, on_progress=None) -> int:
            # nexus-uizok: drain grew an on_progress callback + flush count.
            return 0

        @property
        def pending_summary(self) -> dict:
            # nexus-uizok drain-phase contract: nothing staged, nothing in flight.
            return {"chunks": 0, "collections": 0, "in_flight": 0}

        @property
        def failed_files(self) -> dict:
            return {}

        @property
        def stats(self) -> dict:
            return {"flushes": 0.0, "flush_seconds": 0.0}

    with _service_mode_patches(db), \
         patch("nexus.chunk_batcher.ChunkBatcher", _CapturingBatcher):
        _run_index(repo, reg, force=True)

    assert "flush" in captured, (
        "ChunkBatcher must be constructed when db is an HttpVectorClient"
    )
    captured["flush"]("code__repo__voyage-code-3__v1", ["id1"], ["doc1"], [{"m": 1}])
    db.upsert_chunks_with_embeddings.assert_called_once_with(
        collection_name="code__repo__voyage-code-3__v1",
        ids=["id1"], documents=["doc1"],
        embeddings=[[]],
        metadatas=[{"m": 1}],
        force_re_embed=True,
    )


def test_run_index_batch_flush_force_false_omits_force_re_embed(tmp_path, monkeypatch):
    """Mirror of the above with force=False (the common re-index case):
    the flush closure must still pass force_re_embed=False explicitly
    (not force_re_embed missing) — the callee treats both the same, but
    the closure's own contract is to forward whatever --force resolved to."""
    from nexus.db.http_vector_client import HttpVectorClient
    from nexus.indexer import _run_index

    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "hello.py").write_text("x = 1\n")
    reg = _reg()

    monkeypatch.setenv("NX_STORAGE_BACKEND_VECTORS", "service")
    monkeypatch.setenv("NX_LOCAL", "0")
    monkeypatch.setenv("VOYAGE_API_KEY", "fake")
    monkeypatch.setenv("CHROMA_API_KEY", "fake")

    db = MagicMock(spec=HttpVectorClient)
    captured: dict = {}

    class _CapturingBatcher:
        def __init__(self, *, flush, **_kw):
            captured["flush"] = flush

        def add(self, *_a, **_kw):
            return False

        def drain(self, on_progress=None) -> int:
            # nexus-uizok: drain grew an on_progress callback + flush count.
            return 0

        @property
        def pending_summary(self) -> dict:
            # nexus-uizok drain-phase contract: nothing staged, nothing in flight.
            return {"chunks": 0, "collections": 0, "in_flight": 0}

        @property
        def failed_files(self) -> dict:
            return {}

        @property
        def stats(self) -> dict:
            return {"flushes": 0.0, "flush_seconds": 0.0}

    with _service_mode_patches(db), \
         patch("nexus.chunk_batcher.ChunkBatcher", _CapturingBatcher):
        _run_index(repo, reg, force=False)

    captured["flush"]("code__repo__voyage-code-3__v1", ["id1"], ["doc1"], [{"m": 1}])
    db.upsert_chunks_with_embeddings.assert_called_once_with(
        collection_name="code__repo__voyage-code-3__v1",
        ids=["id1"], documents=["doc1"],
        embeddings=[[]],
        metadatas=[{"m": 1}],
        force_re_embed=False,
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
        # make_t3 must NOT have been called — it would create a split-brain
        # write to daemon-Chroma while search reads service-Chroma
        mocks["make_t3"].assert_not_called()


def test_run_index_non_service_mode_uses_make_t3(tmp_path, monkeypatch):
    """With the explicit chroma opt-out, the legacy make_t3() path is used.
    (nexus-tawx0: service mode is the DEFAULT now; unset == service.)"""
    from nexus.indexer import _run_index

    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "hello.py").write_text("x = 1\n")
    reg = _reg()

    monkeypatch.setenv("NX_STORAGE_BACKEND_VECTORS", "chroma")
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
         patch("nexus.doc_indexer._vector_with_retry", side_effect=lambda fn, **kw: fn(**kw)), \
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
         patch("nexus.doc_indexer._vector_with_retry", side_effect=lambda fn, **kw: fn(**kw)), \
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


def test_index_document_service_mode_t3_none_no_credentials_error(tmp_path, monkeypatch):
    """CLI deployment path: _index_document with t3=None in service mode must NOT
    raise CredentialsMissingError even when no Voyage/Chroma creds are set.

    This is the deployment-blocking scenario identified by the substantive critic:
    a production service-mode node has no Voyage/Chroma creds by design (the
    service embeds), but doc_indexer's old guard fired is_local_mode() first,
    then the credential check, before service mode was tested. The fixed guard
    must check is_vector_service_mode() FIRST so the credential gate is bypassed
    entirely in service mode.
    """
    from nexus.doc_indexer import _index_document, _markdown_chunks

    monkeypatch.setenv("NX_STORAGE_BACKEND_VECTORS", "service")
    monkeypatch.delenv("NX_LOCAL", raising=False)
    monkeypatch.delenv("VOYAGE_API_KEY", raising=False)
    monkeypatch.delenv("NX_VOYAGE_API_KEY", raising=False)

    test_file = tmp_path / "test.md"
    test_file.write_text("# Test\nContent here.\n")

    db, col = _make_doc_indexer_db()
    mock_get_t3 = MagicMock(return_value=db)
    mock_make_t3 = MagicMock(return_value=db)
    mock_hooks = MagicMock()

    def fake_chunk_fn(file_path, content_hash, target_model, now_iso, corpus):
        return [("id1", "chunk text", {"embedding_model": target_model})]

    with patch("nexus.mcp_infra.get_t3", mock_get_t3), \
         patch("nexus.doc_indexer.make_t3", mock_make_t3), \
         patch("nexus.doc_indexer._embed_with_fallback") as embed_mock, \
         patch("nexus.doc_indexer._make_local_embed_fn") as local_embed_mock, \
         patch("nexus.doc_indexer._register_or_lookup_doc_id", return_value="doc-1"), \
         patch("nexus.doc_indexer._vector_with_retry", side_effect=lambda fn, **kw: fn(**kw)), \
         patch("nexus.hook_registry.HookRegistry", return_value=mock_hooks), \
         patch("nexus.hook_registry.install_default_hooks"):
        # Must NOT raise CredentialsMissingError or any credential-related error
        count = _index_document(
            test_file,
            corpus="test-corpus",
            chunk_fn=fake_chunk_fn,
            t3=None,      # CLI path: forces get_t3() routing
            embed_fn=None,
        )

    assert count > 0, f"Expected at least 1 chunk indexed, got {count}"
    # get_t3() must have been called (service-mode routing)
    mock_get_t3.assert_called()
    # make_t3() must NOT have been called (split-brain prevention)
    mock_make_t3.assert_not_called()
    # No Python embed must have fired
    embed_mock.assert_not_called()
    local_embed_mock.assert_not_called()


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
         patch("nexus.doc_indexer._vector_with_retry", side_effect=lambda fn, **kw: fn(**kw)):
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


def _lint_check(extra_files=None, allowlist_prefixes=None,
                voyageai_client_allowlist=None):
    from nexus.storage_boundary_lint import scan_repo

    return scan_repo(
        repo_root=REPO_ROOT,
        allowlist_prefixes=allowlist_prefixes,
        extra_files=extra_files,
        voyageai_client_allowlist=voyageai_client_allowlist,
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


def test_voyageai_client_epsilon_token_does_not_suppress(tmp_path):
    """RDR-186 P4 inversion: the retired per-line epsilon-allow token no
    longer suppresses a voyageai.Client violation — only a named entry in
    VOYAGEAI_CLIENT_ALLOWLIST does."""
    target = tmp_path / "legacy_indexer.py"
    target.write_text(
        "import voyageai\n"
        "def embed_legacy(texts, api_key):\n"
        "    client = voyageai.Client(api_key=api_key)  # epsilon-allow: Phase-4 deletion target, legacy non-service path\n"
        "    return client.embed(texts)\n"
    )
    result = _lint_check(extra_files=[target])
    matched = [v for v in result.violations if v.file == str(target)]
    assert len(matched) == 1, (
        f"the retired epsilon-allow token must not suppress: {matched}"
    )


def test_voyageai_client_named_allowlist_exempts(tmp_path):
    """A voyageai.Client site covered by a named per-file budget is counted
    into ``voyageai_allowlisted_count`` instead of violating."""
    from nexus.storage_boundary_lint import VOYAGEAI_CLIENT_ALLOWLIST

    base = _lint_check().voyageai_allowlisted_count
    target = tmp_path / "named_legacy_indexer.py"
    target.write_text(
        "import voyageai\n"
        "def embed_legacy(texts, api_key):\n"
        "    client = voyageai.Client(api_key=api_key)\n"
        "    return client.embed(texts)\n"
    )
    result = _lint_check(
        extra_files=[target],
        voyageai_client_allowlist={**VOYAGEAI_CLIENT_ALLOWLIST, str(target): 1},
    )
    matched = [v for v in result.violations if v.file == str(target)]
    assert matched == []
    assert result.voyageai_allowlisted_count == base + 1


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
    """The lint's baseline metrics stay stable with voyageai in the BANLIST.

    RDR-186 P4 terminal shape: the numbers DERIVE from the named allowlists
    in storage_boundary_lint.py instead of being pasted literals kept in
    lockstep with test_storage_boundary_lint's copy (the drift class the
    old hand-bumped history here repeatedly demonstrated — see git history
    of this test for the 25 -> ... -> 2 connect ledger narrative).
    """
    from nexus.storage_boundary_lint import (
        SQLITE_CONNECT_ALLOWLIST,
        T2DATABASE_CONSTRUCTION_ALLOWLIST,
    )

    result = _lint_check()
    assert result.total_violations == 0, (
        f"Baseline violation count changed after voyageai lint extension: "
        f"{[(v.file, v.line, v.symbol) for v in result.violations]}"
    )
    assert result.sqlite_allowlisted_connects == sum(
        SQLITE_CONNECT_ALLOWLIST.values()
    ), (
        f"sqlite allowlisted-connect ledger stale: "
        f"{result.sqlite_allowlisted_connects}"
    )
    assert result.t2database_constructions == sum(
        T2DATABASE_CONSTRUCTION_ALLOWLIST.values()
    ), (
        f"t2database_constructions ledger stale: "
        f"{result.t2database_constructions}"
    )


def test_voyageai_allowlisted_count_ratchet():
    """voyageai_allowlisted_count must equal the named-allowlist sum — 3
    after the Seam B cutover:
    - indexer.py (cloud/non-service legacy path)
    - doc_indexer.py (_embed_with_fallback legacy path)
    - commands/collection.py (re-embed CLI utility)

    A new legacy Voyage call cannot be self-granted any more (the per-line
    escape token is retired, RDR-186 P4): it would need a reviewed
    VOYAGEAI_CLIENT_ALLOWLIST entry, and this exact-equality assertion
    keeps the ledger honest in both directions.
    """
    from nexus.storage_boundary_lint import VOYAGEAI_CLIENT_ALLOWLIST

    result = _lint_check()
    assert result.voyageai_allowlisted_count == sum(
        VOYAGEAI_CLIENT_ALLOWLIST.values()
    ), (
        f"voyageai_allowlisted_count ({result.voyageai_allowlisted_count}) "
        f"!= named allowlist sum ({sum(VOYAGEAI_CLIENT_ALLOWLIST.values())}). "
        f"A Phase-4 deletion target moved — update VOYAGEAI_CLIENT_ALLOWLIST "
        f"to match reality (downward when a legacy path dies)."
    )
    assert sum(VOYAGEAI_CLIENT_ALLOWLIST.values()) == 3
