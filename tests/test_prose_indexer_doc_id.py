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


def test_prose_indexer_writes_doc_id_into_t3_chunk_metadata(
    prose_repo: Path,
    registry: RepoRegistry,
    local_t3: T3Database,
    catalog_env: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """First-pass indexing of a fresh prose corpus must populate ``doc_id``
    in every chunk's T3 metadata, matching the catalog tumbler the
    orchestrator registered before per-file indexing.

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

    # Group chunks by the prose file they came from
    by_source: dict[str, list[dict]] = {}
    for meta in result["metadatas"]:
        by_source.setdefault(meta["source_path"], []).append(meta)

    assert by_source, "expected metadatas to carry source_path"

    # Resolve the catalog owner for this repo
    owner_row = cat._db.execute(
        "SELECT tumbler_prefix FROM owners LIMIT 1"
    ).fetchone()
    assert owner_row is not None, "expected catalog owner registered by indexer"
    owner_t = Tumbler.parse(owner_row[0])

    md_seen = False
    rst_seen = False
    for source_path, metas in by_source.items():
        rel_path = str(Path(source_path).relative_to(prose_repo))
        entry = cat.by_file_path(owner_t, rel_path)
        assert entry is not None, (
            f"catalog has no entry for {rel_path!r} — "
            "pre-index registration should have created one"
        )
        expected_doc_id = str(entry.tumbler)
        for m in metas:
            assert m.get("doc_id") == expected_doc_id, (
                f"chunk for {rel_path} carries doc_id={m.get('doc_id')!r}, "
                f"expected {expected_doc_id!r} (catalog tumbler)"
            )
        if rel_path.endswith(".md"):
            md_seen = True
        if rel_path.endswith(".rst"):
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
