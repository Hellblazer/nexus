# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""WITH TEETH: prose_indexer must write the catalog tumbler as ``doc_id``
into T3 chunk metadata at chunk-write time (RDR-101 Phase 3 PR δ Stage B.1).

Without the pre-index resolver wiring, ``index_prose_file`` calls
``make_chunk_metadata`` with no ``doc_id`` argument, the schema funnel
drops the (empty) field via ``normalize`` Step 4c, and chunks land in T3
with no back-reference to the catalog Document. The catalog doctor's
``--t3-doc-id-coverage`` check would then read 0% on freshly-indexed
corpora — the gap PR δ Stage A's schema gate alone cannot close.

Reverting the wiring (resolver -> ctx.doc_id_resolver -> prose_indexer's
``make_chunk_metadata`` ``doc_id=`` argument) breaks the test
deterministically.
"""
from __future__ import annotations

import subprocess
from pathlib import Path
from unittest.mock import MagicMock, patch

import chromadb
import pytest
from chromadb.utils.embedding_functions import DefaultEmbeddingFunction

from nexus.catalog.catalog import Catalog
from nexus.catalog.tumbler import Tumbler
from nexus.db.t3 import T3Database
from nexus.registry import RepoRegistry


def _git(repo: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True)


@pytest.fixture(autouse=True)
def git_identity(monkeypatch: pytest.MonkeyPatch) -> None:
    for k, v in [
        ("GIT_AUTHOR_NAME", "Test"),
        ("GIT_AUTHOR_EMAIL", "test@test.invalid"),
        ("GIT_COMMITTER_NAME", "Test"),
        ("GIT_COMMITTER_EMAIL", "test@test.invalid"),
    ]:
        monkeypatch.setenv(k, v)


@pytest.fixture
def prose_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "prose-repo"
    repo.mkdir()
    (repo / "README.md").write_text(
        "# Hello\n\nThis is a markdown file.\n\n"
        "## First Section\n\nFirst section body.\n\n"
        "## Second Section\n\nSecond section body.\n",
        encoding="utf-8",
    )
    # Non-markdown prose: ``.rst`` is classified as PROSE (not in the
    # SKIP set, not code), exercising prose_indexer's line-chunk branch.
    (repo / "guide.rst").write_text(
        "Plain Prose Guide\n"
        "=================\n\n"
        "First line of plain prose.\n"
        "Second line of plain prose.\n"
        "Third line for chunking.\n",
        encoding="utf-8",
    )
    _git(repo, "init", "-b", "main")
    _git(repo, "config", "user.email", "test@nexus")
    _git(repo, "config", "user.name", "Nexus Test")
    _git(repo, "add", ".")
    _git(repo, "commit", "-m", "Initial commit")
    return repo


@pytest.fixture
def local_t3() -> T3Database:
    return T3Database(
        _client=chromadb.EphemeralClient(),
        _ef_override=DefaultEmbeddingFunction(),
    )


@pytest.fixture
def registry(tmp_path: Path, prose_repo: Path) -> RepoRegistry:
    reg = RepoRegistry(tmp_path / "repos.json")
    reg.add(prose_repo)
    return reg


@pytest.fixture
def catalog_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    catalog_dir = tmp_path / "catalog"
    monkeypatch.setenv("NEXUS_CATALOG_PATH", str(catalog_dir))
    Catalog.init(catalog_dir)
    return catalog_dir


@pytest.fixture(autouse=True)
def mock_voyage_client():
    """Local-mode test: voyageai client is never called, but
    `voyageai.Client` may still be constructed by the orchestrator."""
    ef = DefaultEmbeddingFunction()
    mock_client = MagicMock()

    def fake_embed(texts, model, input_type="document"):
        r = MagicMock()
        r.embeddings = ef(texts)
        return r

    def fake_contextualized_embed(inputs, model, input_type="document"):
        r = MagicMock()
        br = MagicMock()
        br.embeddings = ef(inputs[0])
        r.results = [br]
        return r

    mock_client.embed.side_effect = fake_embed
    mock_client.contextualized_embed.side_effect = fake_contextualized_embed
    with patch("voyageai.Client", return_value=mock_client):
        yield mock_client


def _do_index(repo: Path, registry: RepoRegistry, t3: T3Database, monkeypatch) -> None:
    from nexus.indexer import index_repository

    monkeypatch.setenv("NX_LOCAL", "1")
    with patch("nexus.db.make_t3", return_value=t3), \
         patch("nexus.config.get_credential", side_effect=lambda k: "test-key"):
        index_repository(repo, registry, force=False)


def test_prose_indexer_writes_manifest_rows_for_each_document(
    prose_repo: Path,
    registry: RepoRegistry,
    local_t3: T3Database,
    catalog_env: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """RDR-108 Phase 3 (nexus-bdag) retired ``doc_id`` from chunk metadata
    in favour of the catalog ``document_chunks`` manifest. First-pass
    indexing of a fresh prose corpus must therefore populate manifest
    rows for every registered Document instead of stamping doc_id on
    every chunk.

    Covers both the markdown and non-markdown branches of
    ``prose_indexer.index_prose_file``.
    """
    _do_index(prose_repo, registry, local_t3, monkeypatch)

    cat = Catalog(catalog_env, catalog_env / ".catalog.db")
    info = registry.get(prose_repo)
    assert info is not None
    docs_collection = info.get("docs_collection")
    assert docs_collection, "registry should record docs_collection after indexing"

    docs_col = local_t3.get_collection(docs_collection)
    result = docs_col.get(include=["metadatas"])
    assert result["ids"], "expected at least one prose chunk in T3 docs collection"

    # Phase 3: chunks must NOT carry doc_id any more (catalog manifest
    # is authoritative).
    for meta in result["metadatas"]:
        assert "doc_id" not in meta, (
            f"Phase 3: chunk metadata must not carry doc_id; got {meta!r}"
        )
        assert "chunk_index" not in meta
        assert "chunk_count" not in meta

    # Each registered Document in the docs collection must have a manifest.
    documents = cat._db.execute(
        "SELECT tumbler, file_path FROM documents "
        "WHERE physical_collection = ?",
        (docs_collection,),
    ).fetchall()
    assert documents, "expected catalog Documents for the docs collection"

    md_seen = False
    rst_seen = False
    for row in documents:
        tumbler = row[0]
        file_path = row[1] or ""
        manifest_rows = cat.get_manifest(tumbler)
        assert manifest_rows, (
            f"manifest_write_batch_hook must populate document_chunks "
            f"for doc_id={tumbler!r} (file_path={file_path!r})"
        )
        # nexus-zq79: documents.chunk_count must stay in sync with
        # the manifest (it's a denormalised cache; cache-invalidation
        # bug regression test).
        entry = cat.resolve(Tumbler.parse(tumbler))
        assert entry is not None and entry.chunk_count == len(manifest_rows), (
            f"chunk_count={entry.chunk_count if entry else None} != "
            f"manifest_size={len(manifest_rows)} for doc_id={tumbler!r}"
        )
        if file_path.endswith(".md"):
            md_seen = True
        if file_path.endswith(".rst"):
            rst_seen = True

    assert md_seen, "markdown branch (SemanticMarkdownChunker) was not exercised"
    assert rst_seen, "non-markdown branch (line_chunk) was not exercised"


def test_prose_indexer_doc_id_absent_when_catalog_uninitialized(
    prose_repo: Path,
    registry: RepoRegistry,
    local_t3: T3Database,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When no catalog exists at NEXUS_CATALOG_PATH, ``index_prose_file``
    must still succeed and emit chunks WITHOUT ``doc_id`` (the schema
    drops empty doc_id at the funnel — see metadata_schema.normalize
    Step 4c).

    Guards against the resolver wiring crashing on absent catalogs:
    the orchestrator must build a no-op resolver in that case so the
    prose path stays oblivious to catalog presence.
    """
    monkeypatch.setenv("NEXUS_CATALOG_PATH", str(tmp_path / "no-catalog"))
    # Note: catalog directory is intentionally NOT initialized.

    _do_index(prose_repo, registry, local_t3, monkeypatch)

    info = registry.get(prose_repo)
    assert info is not None
    docs_collection = info.get("docs_collection")
    assert docs_collection
    docs_col = local_t3.get_collection(docs_collection)
    result = docs_col.get(include=["metadatas"])
    assert result["ids"], "indexer should still write chunks when catalog absent"

    for meta in result["metadatas"]:
        assert "doc_id" not in meta, (
            "doc_id must be dropped (normalize Step 4c) when no catalog "
            "entry exists; saw doc_id=%r" % meta.get("doc_id")
        )
