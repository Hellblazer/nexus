# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""WITH TEETH: code_indexer must write the catalog tumbler as ``doc_id``
into T3 chunk metadata at chunk-write time (RDR-101 Phase 3 PR δ Stage B.2).

Mirrors ``test_prose_indexer_doc_id.py`` for the code path. Without the
resolver wiring, ``index_code_file`` calls ``make_chunk_metadata`` with
no ``doc_id`` argument, the schema funnel drops the empty field via
``normalize`` Step 4c, and code chunks land in T3 with no
back-reference to the catalog Document.

Reverting the wiring (resolver -> ctx.doc_id_resolver -> code_indexer's
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
def code_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "code-repo"
    repo.mkdir()
    (repo / "module.py").write_text(
        '"""Sample module for code-indexer doc_id test."""\n'
        "\n"
        "def add(a: int, b: int) -> int:\n"
        '    """Return a + b."""\n'
        "    return a + b\n"
        "\n"
        "\n"
        "class Calculator:\n"
        '    """Tiny calculator class so the indexer chunks structured code."""\n'
        "\n"
        "    def multiply(self, a: int, b: int) -> int:\n"
        "        return a * b\n"
        "\n"
        "    def divide(self, a: int, b: int) -> float:\n"
        '        if b == 0:\n'
        '            raise ValueError("zero division")\n'
        "        return a / b\n",
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
def registry(tmp_path: Path, code_repo: Path) -> RepoRegistry:
    reg = RepoRegistry(tmp_path / "repos.json")
    reg.add(code_repo)
    return reg


@pytest.fixture
def catalog_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    catalog_dir = tmp_path / "catalog"
    monkeypatch.setenv("NEXUS_CATALOG_PATH", str(catalog_dir))
    Catalog.init(catalog_dir)
    return catalog_dir


@pytest.fixture(autouse=True)
def mock_voyage_client():
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


def test_code_indexer_writes_manifest_rows_for_each_document(
    code_repo: Path,
    registry: RepoRegistry,
    local_t3: T3Database,
    catalog_env: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """RDR-108 Phase 3 (nexus-bdag) retired ``doc_id`` from chunk metadata
    in favour of the catalog ``document_chunks`` manifest. First-pass
    indexing of a fresh code corpus must populate manifest rows for
    every registered Document, not stamp doc_id on every chunk.
    """
    _do_index(code_repo, registry, local_t3, monkeypatch)

    cat = Catalog(catalog_env, catalog_env / ".catalog.db")
    info = registry.get(code_repo)
    assert info is not None
    code_collection = info.get("code_collection") or info["collection"]
    assert code_collection, "registry should record code_collection after indexing"

    code_col = local_t3.get_collection(code_collection)
    result = code_col.get(include=["metadatas"])
    assert result["ids"], "expected at least one code chunk in T3 code collection"

    # Phase 3: chunks must NOT carry doc_id any more.
    for meta in result["metadatas"]:
        assert "doc_id" not in meta, (
            f"Phase 3: chunk metadata must not carry doc_id; got {meta!r}"
        )
        assert "chunk_index" not in meta
        assert "chunk_count" not in meta

    documents = cat._db.execute(
        "SELECT tumbler, file_path FROM documents "
        "WHERE physical_collection = ?",
        (code_collection,),
    ).fetchall()
    assert documents, "expected catalog Documents for the code collection"
    for row in documents:
        tumbler = row[0]
        file_path = row[1] or ""
        manifest_rows = cat.get_manifest(tumbler)
        assert manifest_rows, (
            f"manifest_write_batch_hook must populate document_chunks "
            f"for doc_id={tumbler!r} (file_path={file_path!r})"
        )


def test_code_indexer_doc_id_absent_when_catalog_uninitialized(
    code_repo: Path,
    registry: RepoRegistry,
    local_t3: T3Database,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When no catalog exists, ``index_code_file`` must still succeed
    and emit chunks WITHOUT ``doc_id`` (schema drops empty doc_id at
    the funnel - see metadata_schema.normalize Step 4c).
    """
    monkeypatch.setenv("NEXUS_CATALOG_PATH", str(tmp_path / "no-catalog"))

    _do_index(code_repo, registry, local_t3, monkeypatch)

    info = registry.get(code_repo)
    assert info is not None
    code_collection = info.get("code_collection") or info["collection"]
    assert code_collection
    code_col = local_t3.get_collection(code_collection)
    result = code_col.get(include=["metadatas"])
    assert result["ids"], "indexer should still write chunks when catalog absent"

    for meta in result["metadatas"]:
        assert "doc_id" not in meta, (
            "doc_id must be dropped (normalize Step 4c) when no catalog "
            "entry exists; saw doc_id=%r" % meta.get("doc_id")
        )
