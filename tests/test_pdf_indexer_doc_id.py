# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""WITH TEETH: PDF indexing must write the catalog tumbler as ``doc_id``
into T3 chunk metadata at chunk-write time (RDR-101 Phase 3 PR δ Stage B.3).

Mirrors B.1 (prose) and B.2 (code) for the PDF path. Stage B.3 also closes
a separate gap discovered during B.1: PDFs were never registered in the
catalog at all (``indexed_for_catalog`` only contained code + prose + RDR
files). This test exercises both fixes together — PDFs land in the
catalog AND PDF chunks carry doc_id back to that catalog entry.

Reverting either half of the fix breaks the test:
- removing ``(f, "pdf", docs_collection)`` from the pre-index registration
  list -> catalog entry missing -> ``cat.by_file_path(...)`` returns None
- removing ``doc_id=catalog_doc_id`` from the augmented metadata
  -> chunk doc_id is empty -> normalize Step 4c drops it
"""
from __future__ import annotations

import shutil
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
def pdf_repo(tmp_path: Path, simple_pdf: Path) -> Path:
    repo = tmp_path / "pdf-repo"
    repo.mkdir()
    shutil.copy2(simple_pdf, repo / "doc.pdf")
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
def registry(tmp_path: Path, pdf_repo: Path) -> RepoRegistry:
    reg = RepoRegistry(tmp_path / "repos.json")
    reg.add(pdf_repo)
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
    ``voyageai.Client`` may still be constructed by the orchestrator."""
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


def _local_embed(chunks, model, api_key, input_type="document", timeout=120.0, on_progress=None):
    """Shape-#2 embed adapter (returns embeddings, model)."""
    ef = DefaultEmbeddingFunction()
    return ef(chunks), model


def _do_index(repo: Path, registry: RepoRegistry, t3: T3Database, monkeypatch) -> None:
    from nexus.indexer import index_repository

    monkeypatch.setenv("NX_LOCAL", "1")
    with patch("nexus.db.make_t3", return_value=t3), \
         patch("nexus.config.get_credential", side_effect=lambda k: "test-key"), \
         patch("nexus.doc_indexer._embed_with_fallback", side_effect=_local_embed):
        index_repository(repo, registry, force=False)


def test_pdf_indexer_writes_doc_id_into_t3_chunk_metadata(
    pdf_repo: Path,
    registry: RepoRegistry,
    local_t3: T3Database,
    catalog_env: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """First-pass indexing of a fresh PDF corpus must:
      1) Register the PDF in the catalog (was: skipped — PDFs missing from
         ``indexed_for_catalog`` pre-Stage-B.3).
      2) Populate ``doc_id`` in every PDF chunk's T3 metadata, matching
         the catalog tumbler.
    """
    _do_index(pdf_repo, registry, local_t3, monkeypatch)

    cat = Catalog(catalog_env, catalog_env / ".catalog.db")
    info = registry.get(pdf_repo)
    assert info is not None
    docs_collection = info.get("docs_collection")
    assert docs_collection, "registry should record docs_collection after indexing"

    docs_col = local_t3.get_collection(docs_collection)
    result = docs_col.get(include=["metadatas"])
    assert result["ids"], "expected at least one PDF chunk in T3 docs collection"

    # Filter to PDF chunks (the docs__ collection holds prose + PDF mixed).
    pdf_metas = [m for m in result["metadatas"] if m.get("content_type") == "pdf"]
    assert pdf_metas, "expected at least one chunk with content_type='pdf'"

    # Resolve the catalog owner and the PDF's catalog entry.
    owner_row = cat._db.execute(
        "SELECT tumbler_prefix FROM owners LIMIT 1"
    ).fetchone()
    assert owner_row is not None, "expected catalog owner registered by indexer"
    owner_t = Tumbler.parse(owner_row[0])

    pdf_entry = cat.by_file_path(owner_t, "doc.pdf")
    assert pdf_entry is not None, (
        "catalog has no entry for doc.pdf - PDFs must be in "
        "``indexed_for_catalog`` so the orchestrator's pre-index "
        "registration creates a tumbler for them."
    )
    assert pdf_entry.content_type == "pdf", (
        f"expected catalog content_type='pdf', got {pdf_entry.content_type!r}"
    )

    expected_doc_id = str(pdf_entry.tumbler)
    for m in pdf_metas:
        assert m.get("doc_id") == expected_doc_id, (
            f"PDF chunk carries doc_id={m.get('doc_id')!r}, "
            f"expected {expected_doc_id!r} (catalog tumbler)"
        )


def test_pdf_indexer_doc_id_absent_when_catalog_uninitialized(
    pdf_repo: Path,
    registry: RepoRegistry,
    local_t3: T3Database,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When no catalog exists, PDF indexing must still succeed and emit
    chunks WITHOUT ``doc_id`` (schema drops empty doc_id at the funnel).
    """
    monkeypatch.setenv("NEXUS_CATALOG_PATH", str(tmp_path / "no-catalog"))

    _do_index(pdf_repo, registry, local_t3, monkeypatch)

    info = registry.get(pdf_repo)
    assert info is not None
    docs_collection = info.get("docs_collection")
    assert docs_collection
    docs_col = local_t3.get_collection(docs_collection)
    result = docs_col.get(include=["metadatas"])
    assert result["ids"], "indexer should still write chunks when catalog absent"

    pdf_metas = [m for m in result["metadatas"] if m.get("content_type") == "pdf"]
    assert pdf_metas, "expected at least one PDF chunk"

    for m in pdf_metas:
        assert "doc_id" not in m, (
            "doc_id must be dropped (normalize Step 4c) when no catalog "
            "entry exists; saw doc_id=%r" % m.get("doc_id")
        )
