# SPDX-License-Identifier: AGPL-3.0-or-later
"""E2E tests for the code-indexing pipeline and HEAD-polling logic.

No API keys required — T3 is backed by EphemeralClient + local ONNX model.
A small subset of Nexus's own source code is used as the test corpus so the
chunker, AST parser, frecency scoring, and search engine all exercise real
code paths rather than synthetic fixtures.
"""
from __future__ import annotations

import hashlib
import shutil
import subprocess
from pathlib import Path
from unittest.mock import MagicMock, patch

import chromadb
import pytest
from chromadb.utils.embedding_functions import DefaultEmbeddingFunction

from nexus.db.t3 import T3Database
from nexus.registry import RepoRegistry


# ── Corpus definition ─────────────────────────────────────────────────────────

# Small Nexus source files — together they cover TTL parsing, corpus naming,
# type definitions, and session logic.  300 lines total; fast to index.
_CORPUS_FILES = [
    "src/nexus/ttl.py",
    "src/nexus/corpus.py",
    "src/nexus/types.py",
    "src/nexus/session.py",
]

_NEXUS_ROOT = Path(__file__).parent.parent

# Prose files for the rich_repo fixture — markdown + config content
_PROSE_FILES = {
    "README.md": (
        "# Nexus\n\nNexus is a semantic search and knowledge management system.\n\n"
        "## Features\n\n- Semantic search across code repositories\n"
        "- Persistent memory across sessions\n- Three storage tiers\n"
    ),
    "docs/architecture.md": (
        "# Architecture\n\n## Overview\n\nNexus uses a three-tier storage model.\n\n"
        "### T1 — Session Scratch\n\nEphemeral ChromaDB with DefaultEmbeddingFunction.\n\n"
        "### T3 — Permanent Knowledge\n\nChromaDB Cloud with Voyage AI embeddings.\n"
    ),
    "config.yaml": (
        "server:\n  port: 7890\n  headPollInterval: 10\n"
        "embeddings:\n  rerankerModel: rerank-2.5\n"
    ),
}

# RDR (Record of Design Rationale) files — under docs/rdr/
_RDR_FILES = {
    "docs/rdr/ADR-001-storage-tiers.md": (
        "---\ntitle: Storage Tier Architecture\nstatus: accepted\n---\n\n"
        "# ADR-001: Storage Tier Architecture\n\n"
        "## Decision\n\nWe use three storage tiers for different persistence needs.\n"
        "## Consequences\n\nMore complexity but better separation of concerns.\n"
    ),
}


# ── Fixtures ──────────────────────────────────────────────────────────────────

@pytest.fixture(scope="module")
def mini_repo(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """A real git repo containing a subset of Nexus source files.

    Module-scoped so the git init + commit runs once per test session.
    """
    repo = tmp_path_factory.mktemp("nexus-mini")
    for rel in _CORPUS_FILES:
        src = _NEXUS_ROOT / rel
        dest = repo / rel
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, dest)

    subprocess.run(["git", "init", "-b", "main"], cwd=repo, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.email", "test@nexus"], cwd=repo, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.name", "Nexus Test"], cwd=repo, check=True, capture_output=True)
    subprocess.run(["git", "add", "."], cwd=repo, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", "Initial corpus commit"], cwd=repo, check=True, capture_output=True)
    return repo


@pytest.fixture
def local_t3() -> T3Database:
    """Fresh EphemeralClient T3Database per test — no API keys needed."""
    client = chromadb.EphemeralClient()
    ef = DefaultEmbeddingFunction()
    return T3Database(_client=client, _ef_override=ef)


@pytest.fixture
def registry(tmp_path: Path, mini_repo: Path) -> RepoRegistry:
    """RepoRegistry in a temp file with mini_repo pre-registered."""
    reg = RepoRegistry(tmp_path / "repos.json")
    reg.add(mini_repo)
    return reg


@pytest.fixture(autouse=True)
def mock_voyage_client():
    """Patch voyageai.Client so E2E tests run without API keys.

    Mocks both embed() and contextualized_embed() so that:
    - Code indexing (direct embed via voyage-code-3) works
    - Prose/PDF indexing (CCE via _embed_with_fallback / voyage-context-3) works

    Uses DefaultEmbeddingFunction (ONNX MiniLM-L6) to produce embeddings so
    that index vectors and query vectors are in the same space as the local_t3
    fixture's _ef_override — keeping semantic search meaningful in unit tests.
    """
    ef = DefaultEmbeddingFunction()

    mock_client = MagicMock()

    def fake_embed(texts, model, input_type="document"):
        result = MagicMock()
        result.embeddings = ef(texts)
        return result

    def fake_contextualized_embed(inputs, model, input_type="document"):
        # inputs is a list of lists: [[chunk1, chunk2, ...]]
        # contextualized_embed returns result.results[i].embeddings
        result = MagicMock()
        batch_result = MagicMock()
        batch_result.embeddings = ef(inputs[0])
        result.results = [batch_result]
        return result

    mock_client.embed.side_effect = fake_embed
    mock_client.contextualized_embed.side_effect = fake_contextualized_embed

    with patch("voyageai.Client", return_value=mock_client):
        yield mock_client


@pytest.fixture(scope="module")
def rich_repo(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """A real git repo with code, prose, and RDR files.

    Module-scoped: git init + commit runs once per test session.
    Contains:
      - Code files (from _CORPUS_FILES): .py source files
      - Prose files (from _PROSE_FILES): .md and .yaml files
      - RDR files (from _RDR_FILES): ADR markdown under docs/rdr/
    """
    repo = tmp_path_factory.mktemp("nexus-rich")

    # Code files — copied from the real Nexus source
    for rel in _CORPUS_FILES:
        src = _NEXUS_ROOT / rel
        dest = repo / rel
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, dest)

    # Prose files — synthetic content
    for rel, content in _PROSE_FILES.items():
        dest = repo / rel
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_text(content, encoding="utf-8")

    # RDR files — synthetic ADR content
    for rel, content in _RDR_FILES.items():
        dest = repo / rel
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_text(content, encoding="utf-8")

    subprocess.run(["git", "init", "-b", "main"], cwd=repo, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.email", "test@nexus"], cwd=repo, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.name", "Nexus Test"], cwd=repo, check=True, capture_output=True)
    subprocess.run(["git", "add", "."], cwd=repo, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", "Initial rich corpus commit"], cwd=repo, check=True, capture_output=True)
    return repo


@pytest.fixture
def rich_registry(tmp_path: Path, rich_repo: Path) -> RepoRegistry:
    """RepoRegistry in a temp file with rich_repo pre-registered."""
    reg = RepoRegistry(tmp_path / "repos.json")
    reg.add(rich_repo)
    return reg


# ── Indexer pipeline tests ─────────────────────────────────────────────────────

def test_index_creates_chunks_in_t3(
    mini_repo: Path, registry: RepoRegistry, local_t3: T3Database
) -> None:
    """index_repository() chunks corpus files and stores them in T3."""
    from nexus.indexer import index_repository

    with patch("nexus.db.make_t3", return_value=local_t3), \
         patch("nexus.config.get_credential", side_effect=lambda k: "test-key"):
        index_repository(mini_repo, registry)

    col_name = registry.get(mini_repo)["collection"]
    col = local_t3.get_or_create_collection(col_name)
    assert col.count() > 0, "Expected at least one chunk in T3 after indexing"


def test_index_status_transitions_to_ready(
    mini_repo: Path, registry: RepoRegistry, local_t3: T3Database
) -> None:
    """Registry status goes registered → ready after a successful index."""
    from nexus.indexer import index_repository

    assert registry.get(mini_repo)["status"] == "registered"

    with patch("nexus.db.make_t3", return_value=local_t3), \
         patch("nexus.config.get_credential", side_effect=lambda k: "test-key"):
        index_repository(mini_repo, registry)

    assert registry.get(mini_repo)["status"] == "ready"


def test_index_chunks_carry_source_path_metadata(
    mini_repo: Path, registry: RepoRegistry, local_t3: T3Database
) -> None:
    """Every chunk has a source_path pointing at the original file."""
    from nexus.indexer import index_repository

    with patch("nexus.db.make_t3", return_value=local_t3), \
         patch("nexus.config.get_credential", side_effect=lambda k: "test-key"):
        index_repository(mini_repo, registry)

    col_name = registry.get(mini_repo)["collection"]
    col = local_t3.get_or_create_collection(col_name)
    result = col.get(include=["metadatas"])
    source_paths = {m.get("source_path", "") for m in result["metadatas"]}

    assert any("ttl.py" in p for p in source_paths), f"ttl.py missing from: {source_paths}"
    assert any("session.py" in p for p in source_paths), f"session.py missing from: {source_paths}"


def test_index_staleness_check_skips_unchanged_files(
    mini_repo: Path, registry: RepoRegistry, local_t3: T3Database
) -> None:
    """Second index run skips files whose content_hash is unchanged."""
    from nexus.indexer import index_repository

    with patch("nexus.db.make_t3", return_value=local_t3), \
         patch("nexus.config.get_credential", side_effect=lambda k: "test-key"):
        index_repository(mini_repo, registry)
        col_name = registry.get(mini_repo)["collection"]
        col = local_t3.get_or_create_collection(col_name)
        count_after_first = col.count()

        index_repository(mini_repo, registry)
        count_after_second = col.count()

    assert count_after_second == count_after_first, (
        f"Expected no new chunks on re-index of unchanged files "
        f"(first={count_after_first}, second={count_after_second})"
    )


def test_index_frecency_only_preserves_chunk_count(
    mini_repo: Path, registry: RepoRegistry, local_t3: T3Database
) -> None:
    """--frecency-only reindex updates scores without adding or removing chunks."""
    from nexus.indexer import index_repository

    with patch("nexus.db.make_t3", return_value=local_t3), \
         patch("nexus.config.get_credential", side_effect=lambda k: "test-key"):
        index_repository(mini_repo, registry)
        col_name = registry.get(mini_repo)["collection"]
        col = local_t3.get_or_create_collection(col_name)
        count_before = col.count()

        index_repository(mini_repo, registry, frecency_only=True)

    assert col.count() == count_before


# ── Smart indexing: dual collection tests ────────────────────────────────────

def _run_smart_index(repo: Path, registry: RepoRegistry, local_t3: T3Database) -> None:
    """Helper: run index_repository with standard test patches."""
    from nexus.indexer import index_repository

    with patch("nexus.db.make_t3", return_value=local_t3), \
         patch("nexus.config.get_credential", side_effect=lambda k: "test-key"):
        index_repository(repo, registry)


def test_smart_index_creates_both_collections(
    rich_repo: Path, rich_registry: RepoRegistry, local_t3: T3Database,
) -> None:
    """index_repository creates both code__ and docs__ collections with chunks."""
    _run_smart_index(rich_repo, rich_registry, local_t3)

    info = rich_registry.get(rich_repo)
    code_col = local_t3.get_or_create_collection(info["code_collection"])
    docs_col = local_t3.get_or_create_collection(info["docs_collection"])

    assert code_col.count() > 0, "Expected chunks in code__ collection"
    assert docs_col.count() > 0, "Expected chunks in docs__ collection"


def test_smart_index_code_files_in_code_collection(
    rich_repo: Path, rich_registry: RepoRegistry, local_t3: T3Database,
) -> None:
    """.py files should be in code__, NOT in docs__."""
    _run_smart_index(rich_repo, rich_registry, local_t3)

    info = rich_registry.get(rich_repo)
    code_col = local_t3.get_or_create_collection(info["code_collection"])
    docs_col = local_t3.get_or_create_collection(info["docs_collection"])

    code_result = code_col.get(include=["metadatas"])
    code_sources = {m.get("source_path", "") for m in code_result["metadatas"]}
    assert any("ttl.py" in p for p in code_sources), (
        f"ttl.py should be in code__ collection; got: {code_sources}"
    )

    docs_result = docs_col.get(include=["metadatas"])
    docs_sources = {m.get("source_path", "") for m in docs_result["metadatas"]}
    assert not any(".py" in p for p in docs_sources), (
        f".py files should NOT be in docs__ collection; found: "
        f"{[p for p in docs_sources if '.py' in p]}"
    )


def test_smart_index_prose_files_in_docs_collection(
    rich_repo: Path, rich_registry: RepoRegistry, local_t3: T3Database,
) -> None:
    """.md and .yaml files should be in docs__, NOT in code__."""
    _run_smart_index(rich_repo, rich_registry, local_t3)

    info = rich_registry.get(rich_repo)
    code_col = local_t3.get_or_create_collection(info["code_collection"])
    docs_col = local_t3.get_or_create_collection(info["docs_collection"])

    docs_result = docs_col.get(include=["metadatas"])
    docs_sources = {m.get("source_path", "") for m in docs_result["metadatas"]}
    assert any("README.md" in p for p in docs_sources), (
        f"README.md should be in docs__ collection; got: {docs_sources}"
    )
    assert any("architecture.md" in p for p in docs_sources), (
        f"architecture.md should be in docs__ collection; got: {docs_sources}"
    )
    assert any("config.yaml" in p for p in docs_sources), (
        f"config.yaml should be in docs__ collection; got: {docs_sources}"
    )

    code_result = code_col.get(include=["metadatas"])
    code_sources = {m.get("source_path", "") for m in code_result["metadatas"]}
    assert not any("README.md" in p for p in code_sources), (
        "README.md should NOT be in code__ collection"
    )


def test_smart_index_rdr_excluded_from_docs(
    rich_repo: Path, rich_registry: RepoRegistry, local_t3: T3Database,
) -> None:
    """RDR files should NOT be in the main docs__<repo> collection."""
    _run_smart_index(rich_repo, rich_registry, local_t3)

    info = rich_registry.get(rich_repo)
    docs_col = local_t3.get_or_create_collection(info["docs_collection"])

    docs_result = docs_col.get(include=["metadatas"])
    docs_sources = {m.get("source_path", "") for m in docs_result["metadatas"]}
    assert not any("ADR-001" in p for p in docs_sources), (
        f"RDR files should NOT be in docs__<repo> collection; "
        f"found: {[p for p in docs_sources if 'ADR-001' in p]}"
    )


def test_smart_index_rdr_in_rdr_collection(
    rich_repo: Path, rich_registry: RepoRegistry, local_t3: T3Database,
) -> None:
    """RDR files should be indexed into rdr__<basename>-<hash8>."""
    _run_smart_index(rich_repo, rich_registry, local_t3)

    path_hash = hashlib.sha256(str(rich_repo).encode()).hexdigest()[:8]
    rdr_col_name = f"rdr__{rich_repo.name}-{path_hash}"
    rdr_col = local_t3.get_or_create_collection(rdr_col_name)
    assert rdr_col.count() > 0, "Expected RDR chunks in rdr__ collection"

    result = rdr_col.get(include=["metadatas"])
    source_paths = {m.get("source_path", "") for m in result["metadatas"]}
    assert any("ADR-001" in p for p in source_paths), (
        f"ADR-001 should be in rdr__ collection; got: {source_paths}"
    )


def test_smart_index_search_code_query(
    rich_repo: Path, rich_registry: RepoRegistry, local_t3: T3Database,
) -> None:
    """Searching code__ returns code files for a code-related query."""
    _run_smart_index(rich_repo, rich_registry, local_t3)

    info = rich_registry.get(rich_repo)
    results = local_t3.search(
        "parse TTL days weeks permanent", [info["code_collection"]], n_results=5,
    )
    assert len(results) > 0, "Expected code search results"
    source_paths = [r.get("source_path", "") for r in results]
    assert any("ttl.py" in p for p in source_paths), (
        f"Expected ttl.py in code search results; got: {source_paths}"
    )


def test_smart_index_search_prose_query(
    rich_repo: Path, rich_registry: RepoRegistry, local_t3: T3Database,
) -> None:
    """Searching docs__ returns prose files for a prose-related query."""
    _run_smart_index(rich_repo, rich_registry, local_t3)

    info = rich_registry.get(rich_repo)
    results = local_t3.search(
        "semantic search knowledge management features", [info["docs_collection"]], n_results=5,
    )
    assert len(results) > 0, "Expected prose search results"
    source_paths = [r.get("source_path", "") for r in results]
    assert any("README.md" in p for p in source_paths), (
        f"Expected README.md in prose search results; got: {source_paths}"
    )


def test_smart_index_embedding_model_metadata(
    rich_repo: Path, rich_registry: RepoRegistry, local_t3: T3Database,
) -> None:
    """Code chunks record voyage-code-3; docs chunks record voyage-context-3 or voyage-4."""
    _run_smart_index(rich_repo, rich_registry, local_t3)

    info = rich_registry.get(rich_repo)

    # Check code collection
    code_col = local_t3.get_or_create_collection(info["code_collection"])
    code_result = code_col.get(include=["metadatas"])
    code_models = {m.get("embedding_model", "") for m in code_result["metadatas"]}
    assert "voyage-code-3" in code_models, (
        f"Code chunks should use voyage-code-3; got: {code_models}"
    )

    # Check docs collection — CCE uses voyage-context-3, fallback uses voyage-4
    docs_col = local_t3.get_or_create_collection(info["docs_collection"])
    docs_result = docs_col.get(include=["metadatas"])
    docs_models = {m.get("embedding_model", "") for m in docs_result["metadatas"]}
    allowed = {"voyage-context-3", "voyage-4"}
    assert docs_models <= allowed, (
        f"Docs chunks should use voyage-context-3 or voyage-4; got: {docs_models}"
    )
    assert len(docs_models) > 0, "Expected at least one embedding model in docs chunks"


def test_smart_index_staleness_check(
    rich_repo: Path, rich_registry: RepoRegistry, local_t3: T3Database,
) -> None:
    """Second index skips unchanged files in both code__ and docs__ collections."""
    _run_smart_index(rich_repo, rich_registry, local_t3)

    info = rich_registry.get(rich_repo)
    code_col = local_t3.get_or_create_collection(info["code_collection"])
    docs_col = local_t3.get_or_create_collection(info["docs_collection"])
    code_count_1 = code_col.count()
    docs_count_1 = docs_col.count()

    # Re-run indexing — should be a no-op
    _run_smart_index(rich_repo, rich_registry, local_t3)

    assert code_col.count() == code_count_1, (
        f"Code chunk count changed on re-index: {code_count_1} -> {code_col.count()}"
    )
    assert docs_col.count() == docs_count_1, (
        f"Docs chunk count changed on re-index: {docs_count_1} -> {docs_col.count()}"
    )


def test_smart_index_git_metadata_present(
    rich_repo: Path, rich_registry: RepoRegistry, local_t3: T3Database,
) -> None:
    """Chunks in both collections carry git_commit_hash, git_branch, source_path."""
    _run_smart_index(rich_repo, rich_registry, local_t3)

    info = rich_registry.get(rich_repo)

    for col_name in (info["code_collection"], info["docs_collection"]):
        col = local_t3.get_or_create_collection(col_name)
        result = col.get(include=["metadatas"])
        assert len(result["metadatas"]) > 0, f"No chunks in {col_name}"

        sample = result["metadatas"][0]
        assert sample.get("git_commit_hash"), (
            f"git_commit_hash missing in {col_name}: {sample}"
        )
        assert sample.get("git_branch"), (
            f"git_branch missing in {col_name}: {sample}"
        )
        assert sample.get("source_path"), (
            f"source_path missing in {col_name}: {sample}"
        )


# ── Migration test ──────────────────────────────────────────────────────────

def test_migration_moves_prose_from_code_to_docs(
    rich_repo: Path, tmp_path: Path, local_t3: T3Database,
) -> None:
    """Simulates old indexer (all in code__), then runs new indexer to verify migration.

    The _prune_misclassified step should remove prose chunks from code__ and
    the new indexer should place prose into docs__.

    Note: chromadb.EphemeralClient is a singleton — all instances share state.
    The test verifies by ID that the specific fake chunk is pruned.
    """
    from nexus.indexer import index_repository

    reg = RepoRegistry(tmp_path / "repos.json")
    reg.add(rich_repo)
    info = reg.get(rich_repo)
    code_col_name = info["code_collection"]
    docs_col_name = info["docs_collection"]

    # Manually insert a prose file's chunk into code__ (simulating old behavior)
    code_col = local_t3.get_or_create_collection(code_col_name)
    readme_path = str(rich_repo / "README.md")
    code_col.add(
        ids=["fake-prose-in-code"],
        documents=["Nexus is a semantic search system"],
        metadatas=[{
            "source_path": readme_path,
            "content_hash": "old-hash",
            "embedding_model": "voyage-code-3",
            "store_type": "code",
        }],
    )
    # Verify the fake chunk was inserted (by specific ID, not total count)
    pre_result = code_col.get(ids=["fake-prose-in-code"], include=["metadatas"])
    assert len(pre_result["ids"]) == 1, "Fake prose chunk must exist before migration"

    # Run the new unified indexer
    with patch("nexus.db.make_t3", return_value=local_t3), \
         patch("nexus.config.get_credential", side_effect=lambda k: "test-key"):
        index_repository(rich_repo, reg)

    # The fake prose chunk should have been pruned from code__
    code_result = code_col.get(include=["metadatas"])
    code_sources = {m.get("source_path", "") for m in code_result["metadatas"]}
    assert readme_path not in code_sources, (
        "README.md should be pruned from code__ after reclassification"
    )

    # Specifically, the fake ID should be gone
    post_fake = code_col.get(ids=["fake-prose-in-code"], include=[])
    assert len(post_fake["ids"]) == 0, (
        "The fake-prose-in-code chunk should have been pruned from code__"
    )

    # README.md should now be in docs__
    docs_col = local_t3.get_or_create_collection(docs_col_name)
    docs_result = docs_col.get(include=["metadatas"])
    docs_sources = {m.get("source_path", "") for m in docs_result["metadatas"]}
    assert any("README.md" in p for p in docs_sources), (
        f"README.md should be in docs__ after migration; got: {docs_sources}"
    )


# ── Index → search pipeline ───────────────────────────────────────────────────

def test_index_then_search_ttl_content(
    mini_repo: Path, registry: RepoRegistry, local_t3: T3Database
) -> None:
    """Index corpus, then search for TTL-related content — ttl.py should rank."""
    from nexus.indexer import index_repository

    with patch("nexus.db.make_t3", return_value=local_t3), \
         patch("nexus.config.get_credential", side_effect=lambda k: "test-key"):
        index_repository(mini_repo, registry)

    col_name = registry.get(mini_repo)["collection"]
    results = local_t3.search("parse TTL days weeks permanent", [col_name], n_results=5)

    assert len(results) > 0, "Expected search results after indexing"
    source_paths = [r.get("source_path", "") for r in results]
    assert any("ttl.py" in p for p in source_paths), (
        f"Expected ttl.py in top results for TTL query; got: {source_paths}"
    )


def test_index_then_search_session_content(
    mini_repo: Path, registry: RepoRegistry, local_t3: T3Database
) -> None:
    """Search for session/getsid content — session.py should be in results."""
    from nexus.indexer import index_repository

    with patch("nexus.db.make_t3", return_value=local_t3), \
         patch("nexus.config.get_credential", side_effect=lambda k: "test-key"):
        index_repository(mini_repo, registry)

    col_name = registry.get(mini_repo)["collection"]
    results = local_t3.search("session identifier process group ID", [col_name], n_results=5)

    assert len(results) > 0
    source_paths = [r.get("source_path", "") for r in results]
    assert any("session.py" in p for p in source_paths), (
        f"Expected session.py in top results; got: {source_paths}"
    )


def test_index_then_search_corpus_naming(
    mini_repo: Path, registry: RepoRegistry, local_t3: T3Database
) -> None:
    """Search for collection naming logic — corpus.py should appear."""
    from nexus.indexer import index_repository

    with patch("nexus.db.make_t3", return_value=local_t3), \
         patch("nexus.config.get_credential", side_effect=lambda k: "test-key"):
        index_repository(mini_repo, registry)

    col_name = registry.get(mini_repo)["collection"]
    results = local_t3.search("collection name prefix code docs knowledge", [col_name], n_results=5)

    assert len(results) > 0
    source_paths = [r.get("source_path", "") for r in results]
    assert any("corpus.py" in p for p in source_paths), (
        f"Expected corpus.py in top results; got: {source_paths}"
    )


# ── HEAD polling tests ─────────────────────────────────────────────────────────

def test_polling_triggers_index_when_head_differs(
    mini_repo: Path, registry: RepoRegistry
) -> None:
    """check_and_reindex() triggers indexing when stored head_hash is stale."""
    from nexus.polling import check_and_reindex

    assert registry.get(mini_repo)["head_hash"] == ""

    indexed: list[Path] = []
    with patch("nexus.polling.index_repo", side_effect=lambda r, reg: indexed.append(r)):
        check_and_reindex(mini_repo, registry)

    assert mini_repo in indexed, "Expected indexing to be triggered"


def test_polling_skips_when_head_unchanged(
    mini_repo: Path, registry: RepoRegistry
) -> None:
    """check_and_reindex() does not re-index when HEAD matches stored hash."""
    from nexus.polling import check_and_reindex

    current_head = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=mini_repo,
        capture_output=True, text=True, check=True,
    ).stdout.strip()
    registry.update(mini_repo, head_hash=current_head)

    indexed: list[Path] = []
    with patch("nexus.polling.index_repo", side_effect=lambda r, reg: indexed.append(r)):
        check_and_reindex(mini_repo, registry)

    assert indexed == [], "Expected no indexing when HEAD is current"


def test_polling_skips_when_status_is_indexing(
    mini_repo: Path, registry: RepoRegistry
) -> None:
    """check_and_reindex() does not trigger when status is 'indexing'."""
    from nexus.polling import check_and_reindex

    registry.update(mini_repo, status="indexing")

    indexed: list[Path] = []
    with patch("nexus.polling.index_repo", side_effect=lambda r, reg: indexed.append(r)):
        check_and_reindex(mini_repo, registry)

    assert indexed == []


def test_polling_records_head_hash_after_successful_index(
    mini_repo: Path, registry: RepoRegistry
) -> None:
    """After a successful reindex, head_hash is updated to the current HEAD."""
    from nexus.polling import check_and_reindex

    with patch("nexus.polling.index_repo"):  # no-op index
        check_and_reindex(mini_repo, registry)

    entry = registry.get(mini_repo)
    assert entry["head_hash"] != "", "head_hash must be recorded after indexing"


def test_polling_does_not_record_head_hash_on_credentials_missing(
    mini_repo: Path, registry: RepoRegistry
) -> None:
    """When CredentialsMissingError is raised, head_hash stays empty (allows retry)."""
    from nexus.errors import CredentialsMissingError
    from nexus.polling import check_and_reindex

    def _raise(repo, reg):
        raise CredentialsMissingError("no creds")

    with patch("nexus.polling.index_repo", side_effect=_raise):
        check_and_reindex(mini_repo, registry)

    assert registry.get(mini_repo)["head_hash"] == "", (
        "head_hash must stay empty on CredentialsMissingError so polling retries"
    )


# ── CLI: nx index repo ─────────────────────────────────────────────────────────

def test_cli_index_repo_registers_and_indexes(
    mini_repo: Path, tmp_path: Path, local_t3: T3Database,
    monkeypatch: pytest.MonkeyPatch
) -> None:
    """nx index repo <path> registers the repo and indexes it end-to-end."""
    from click.testing import CliRunner
    from nexus.cli import main

    monkeypatch.setenv("HOME", str(tmp_path))
    runner = CliRunner()

    with patch("nexus.db.make_t3", return_value=local_t3), \
         patch("nexus.config.get_credential", side_effect=lambda k: "test-key"):
        result = runner.invoke(main, ["index", "repo", str(mini_repo)])

    assert result.exit_code == 0, result.output
    assert "Done" in result.output


def test_cli_index_then_search_pipeline(
    mini_repo: Path, tmp_path: Path, local_t3: T3Database,
    monkeypatch: pytest.MonkeyPatch
) -> None:
    """nx index repo → nx search: full CLI pipeline returns results."""
    from click.testing import CliRunner
    from nexus.cli import main

    monkeypatch.setenv("HOME", str(tmp_path))
    runner = CliRunner()

    col_name = (
        "code__"
        + mini_repo.name
        + "-"
        + hashlib.sha256(str(mini_repo).encode()).hexdigest()[:8]
    )

    _re = RuntimeError("not configured")
    with patch("nexus.db.make_t3", return_value=local_t3), \
         patch("nexus.config.get_credential", side_effect=lambda k: "test-key"), \
         patch("nexus.commands.search_cmd.t3_knowledge", return_value=local_t3), \
         patch("nexus.commands.search_cmd.t3_code", side_effect=_re), \
         patch("nexus.commands.search_cmd.t3_docs", side_effect=_re), \
         patch("nexus.commands.search_cmd.t3_rdr", side_effect=_re):

        index_result = runner.invoke(main, ["index", "repo", str(mini_repo)])
        assert index_result.exit_code == 0, index_result.output

        search_result = runner.invoke(main, [
            "search", "parse TTL expiry days",
            "--corpus", col_name,
            "--n", "5",
        ])

    assert search_result.exit_code == 0, search_result.output
    assert len(search_result.output.strip()) > 0


def test_cli_index_frecency_only_flag(
    mini_repo: Path, tmp_path: Path, local_t3: T3Database,
    monkeypatch: pytest.MonkeyPatch
) -> None:
    """nx index repo --frecency-only completes without error after a full index."""
    from click.testing import CliRunner
    from nexus.cli import main

    monkeypatch.setenv("HOME", str(tmp_path))
    runner = CliRunner()

    with patch("nexus.db.make_t3", return_value=local_t3), \
         patch("nexus.config.get_credential", side_effect=lambda k: "test-key"):
        runner.invoke(main, ["index", "repo", str(mini_repo)])
        result = runner.invoke(main, ["index", "repo", str(mini_repo), "--frecency-only"])

    assert result.exit_code == 0, result.output
    assert "Done" in result.output
