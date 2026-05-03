# SPDX-License-Identifier: AGPL-3.0-or-later
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

_CORPUS_FILES = [
    "src/nexus/ttl.py",
    "src/nexus/corpus.py",
    "src/nexus/types.py",
    "src/nexus/session.py",
]
_NEXUS_ROOT = Path(__file__).parent.parent

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

_RDR_FILES = {
    "docs/rdr/ADR-001-storage-tiers.md": (
        "---\ntitle: Storage Tier Architecture\nstatus: accepted\n---\n\n"
        "# ADR-001: Storage Tier Architecture\n\n"
        "## Decision\n\nWe use three storage tiers for different persistence needs.\n"
        "## Consequences\n\nMore complexity but better separation of concerns.\n"
    ),
}


def _git_init(repo: Path, msg: str = "Initial commit") -> None:
    for cmd in (
        ["git", "init", "-b", "main"],
        ["git", "config", "user.email", "test@nexus"],
        ["git", "config", "user.name", "Nexus Test"],
        ["git", "add", "."],
        ["git", "commit", "-m", msg],
    ):
        subprocess.run(cmd, cwd=repo, check=True, capture_output=True)


# ── Fixtures ──────────────────────────────────────────────────────────────────

@pytest.fixture(scope="module")
def mini_repo(tmp_path_factory: pytest.TempPathFactory) -> Path:
    repo = tmp_path_factory.mktemp("nexus-mini")
    for rel in _CORPUS_FILES:
        dest = repo / rel
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(_NEXUS_ROOT / rel, dest)
    _git_init(repo, "Initial corpus commit")
    return repo


@pytest.fixture
def local_t3() -> T3Database:
    return T3Database(
        _client=chromadb.EphemeralClient(), _ef_override=DefaultEmbeddingFunction()
    )


@pytest.fixture
def registry(tmp_path: Path, mini_repo: Path) -> RepoRegistry:
    reg = RepoRegistry(tmp_path / "repos.json")
    reg.add(mini_repo)
    return reg


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


@pytest.fixture(scope="module")
def rich_repo(tmp_path_factory: pytest.TempPathFactory) -> Path:
    import pymupdf as _fitz
    repo = tmp_path_factory.mktemp("nexus-rich")
    for rel in _CORPUS_FILES:
        dest = repo / rel
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(_NEXUS_ROOT / rel, dest)
    for rel, content in {**_PROSE_FILES, **_RDR_FILES}.items():
        dest = repo / rel
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_text(content, encoding="utf-8")
    pdf_doc = _fitz.open()
    page = pdf_doc.new_page()
    page.insert_text((72, 100), "Hello World. This is a test document for PDF ingest.", fontsize=12)
    pdf_doc.set_metadata({"title": "Test Document", "author": "Test Author"})
    (repo / "docs" / "test.pdf").write_bytes(pdf_doc.tobytes())
    pdf_doc.close()
    _git_init(repo, "Initial rich corpus commit")
    return repo


@pytest.fixture
def rich_registry(tmp_path: Path, rich_repo: Path) -> RepoRegistry:
    reg = RepoRegistry(tmp_path / "repos.json")
    reg.add(rich_repo)
    return reg


# ── Helpers ───────────────────────────────────────────────────────────────────

def _index(repo: Path, registry: RepoRegistry, t3: T3Database, **kw) -> None:
    from nexus.indexer import index_repository
    with patch("nexus.db.make_t3", return_value=t3), \
         patch("nexus.config.get_credential", side_effect=lambda k: "test-key"):
        index_repository(repo, registry, **kw)


def _get_sources(t3: T3Database, col_name: str) -> set[str]:
    """RDR-102 D2: source_path was removed from the chunk schema. The
    chunk's ``title`` is constructed as ``"{file.relative_to(repo)}:
    chunk-{i}"`` for code (code_indexer.py:393) and prose
    (prose_indexer.py:96), so it still carries the filename — sufficient
    for the ``any(filename in p for p in sources)`` membership pattern
    these tests use to verify file-to-collection routing."""
    col = t3.get_or_create_collection(col_name)
    return {m.get("title", "") for m in col.get(include=["metadatas"])["metadatas"]}


# ── Basic indexer pipeline ────────────────────────────────────────────────────

def test_index_creates_chunks_in_t3(
    mini_repo: Path, registry: RepoRegistry, local_t3: T3Database
) -> None:
    _index(mini_repo, registry, local_t3)
    col = local_t3.get_or_create_collection(registry.get(mini_repo)["collection"])
    assert col.count() > 0

def test_index_status_transitions_to_ready(
    mini_repo: Path, registry: RepoRegistry, local_t3: T3Database
) -> None:
    assert registry.get(mini_repo)["status"] == "registered"
    _index(mini_repo, registry, local_t3)
    assert registry.get(mini_repo)["status"] == "ready"


@pytest.mark.parametrize("expected_file", ["ttl.py", "session.py"])
def test_index_chunks_carry_source_path(
    mini_repo: Path, registry: RepoRegistry, local_t3: T3Database, expected_file: str
) -> None:
    _index(mini_repo, registry, local_t3)
    sources = _get_sources(local_t3, registry.get(mini_repo)["collection"])
    assert any(expected_file in p for p in sources), f"{expected_file} missing from: {sources}"


def test_index_staleness_skips_unchanged(
    mini_repo: Path, registry: RepoRegistry, local_t3: T3Database
) -> None:
    _index(mini_repo, registry, local_t3)
    col = local_t3.get_or_create_collection(registry.get(mini_repo)["collection"])
    count1 = col.count()
    _index(mini_repo, registry, local_t3)
    assert col.count() == count1


def test_index_frecency_only_preserves_count(
    mini_repo: Path, registry: RepoRegistry, local_t3: T3Database
) -> None:
    _index(mini_repo, registry, local_t3)
    col = local_t3.get_or_create_collection(registry.get(mini_repo)["collection"])
    count = col.count()
    _index(mini_repo, registry, local_t3, frecency_only=True)
    assert col.count() == count


# ── Smart indexing: dual collection ───────────────────────────────────────────

def test_smart_index_creates_both_collections(
    rich_repo: Path, rich_registry: RepoRegistry, local_t3: T3Database,
) -> None:
    _index(rich_repo, rich_registry, local_t3)
    info = rich_registry.get(rich_repo)
    for key in ("code_collection", "docs_collection"):
        col = local_t3.get_or_create_collection(info[key])
        assert col.count() > 0, f"No chunks in {key}"


@pytest.mark.parametrize("file,expected_col,excluded_col", [
    ("ttl.py", "code_collection", "docs_collection"),
    ("README.md", "docs_collection", "code_collection"),
    ("architecture.md", "docs_collection", None),
])
def test_smart_index_file_routing(
    rich_repo: Path, rich_registry: RepoRegistry, local_t3: T3Database,
    file: str, expected_col: str, excluded_col: str | None,
) -> None:
    _index(rich_repo, rich_registry, local_t3)
    info = rich_registry.get(rich_repo)
    sources = _get_sources(local_t3, info[expected_col])
    assert any(file in p for p in sources), f"{file} not in {expected_col}: {sources}"
    if excluded_col:
        excl_sources = _get_sources(local_t3, info[excluded_col])
        assert not any(file in p for p in excl_sources), f"{file} should not be in {excluded_col}"


def test_smart_index_config_yaml_excluded(
    rich_repo: Path, rich_registry: RepoRegistry, local_t3: T3Database,
) -> None:
    _index(rich_repo, rich_registry, local_t3)
    info = rich_registry.get(rich_repo)
    docs_sources = _get_sources(local_t3, info["docs_collection"])
    assert not any("config.yaml" in p for p in docs_sources)


def test_smart_index_py_excluded_from_docs(
    rich_repo: Path, rich_registry: RepoRegistry, local_t3: T3Database,
) -> None:
    _index(rich_repo, rich_registry, local_t3)
    info = rich_registry.get(rich_repo)
    docs_sources = _get_sources(local_t3, info["docs_collection"])
    assert not any(".py" in p for p in docs_sources)


def test_smart_index_rdr_routing(
    rich_repo: Path, rich_registry: RepoRegistry, local_t3: T3Database,
) -> None:
    """RDR-102 D2: source_path is gone from chunks. The frontmatter
    title for ADR-001-storage-tiers.md is "Storage Tier Architecture"
    (overrides the filename-derived stem), so the chunk's ``title``
    field doesn't carry "ADR-001". Verify routing via the chunk's
    ``section_title`` (the H1 "ADR-001: Storage Tier Architecture"
    propagates as section_title for all chunks under it) AND by
    asserting the rdr collection has chunks while docs does not.
    """
    _index(rich_repo, rich_registry, local_t3)
    info = rich_registry.get(rich_repo)
    docs_col = local_t3.get_or_create_collection(info["docs_collection"])
    docs_sections = {
        m.get("section_title", "")
        for m in docs_col.get(include=["metadatas"])["metadatas"]
    }
    assert not any("ADR-001" in s for s in docs_sections), (
        f"ADR-001 must not appear in docs__ section_titles: {docs_sections}"
    )

    path_hash = hashlib.sha256(str(rich_repo).encode()).hexdigest()[:8]
    rdr_col_name = f"rdr__{rich_repo.name}-{path_hash}"
    rdr_col = local_t3.get_or_create_collection(rdr_col_name)
    rdr_metas = rdr_col.get(include=["metadatas"])["metadatas"]
    assert rdr_metas, f"expected ADR-001 chunks in {rdr_col_name}; got none"
    rdr_sections = {m.get("section_title", "") for m in rdr_metas}
    assert any("ADR-001" in s for s in rdr_sections), (
        f"ADR-001 H1 must surface as section_title in rdr__: {rdr_sections}"
    )


@pytest.mark.parametrize("query,corpus_key,expected_file", [
    ("parse TTL days weeks permanent", "code_collection", "ttl.py"),
    ("semantic search knowledge management features", "docs_collection", "README.md"),
])
def test_smart_index_search(
    rich_repo: Path, rich_registry: RepoRegistry, local_t3: T3Database,
    query: str, corpus_key: str, expected_file: str,
) -> None:
    _index(rich_repo, rich_registry, local_t3)
    info = rich_registry.get(rich_repo)
    results = local_t3.search(query, [info[corpus_key]], n_results=5)
    assert len(results) > 0
    # RDR-102 D2: search results no longer carry source_path (filtered
    # by normalize() at write time). Title carries "{relpath}:chunk-{i}".
    sources = [r.get("title", "") for r in results]
    assert any(expected_file in p for p in sources), f"{expected_file} not in: {sources}"


def test_smart_index_embedding_model_metadata(
    rich_repo: Path, rich_registry: RepoRegistry, local_t3: T3Database,
) -> None:
    _index(rich_repo, rich_registry, local_t3)
    info = rich_registry.get(rich_repo)
    code_col = local_t3.get_or_create_collection(info["code_collection"])
    code_models = {m.get("embedding_model", "") for m in code_col.get(include=["metadatas"])["metadatas"]}
    assert "voyage-code-3" in code_models
    docs_col = local_t3.get_or_create_collection(info["docs_collection"])
    docs_models = {m.get("embedding_model", "") for m in docs_col.get(include=["metadatas"])["metadatas"]}
    assert docs_models <= {"voyage-context-3"} and docs_models


def test_smart_index_staleness_check(
    rich_repo: Path, rich_registry: RepoRegistry, local_t3: T3Database,
) -> None:
    _index(rich_repo, rich_registry, local_t3)
    info = rich_registry.get(rich_repo)
    counts = {}
    for key in ("code_collection", "docs_collection"):
        counts[key] = local_t3.get_or_create_collection(info[key]).count()
    _index(rich_repo, rich_registry, local_t3)
    for key, c in counts.items():
        assert local_t3.get_or_create_collection(info[key]).count() == c


@pytest.mark.parametrize("meta_key", ["commit", "branch"])
def test_smart_index_git_metadata(
    rich_repo: Path, rich_registry: RepoRegistry, local_t3: T3Database,
    meta_key: str,
) -> None:
    """Git provenance is consolidated into the ``git_meta`` JSON blob
    (nexus-40t). RDR-102 D2 removed ``source_path`` from the schema —
    the parametrize set is reduced to the git_meta sub-keys, which
    are the actual subject of this test."""
    import json as _json

    _index(rich_repo, rich_registry, local_t3)
    info = rich_registry.get(rich_repo)
    for col_key in ("code_collection", "docs_collection"):
        col = local_t3.get_or_create_collection(info[col_key])
        sample = col.get(include=["metadatas"])["metadatas"][0]
        git_blob = sample.get("git_meta")
        assert git_blob, f"git_meta missing in {col_key}: {sample}"
        decoded = _json.loads(git_blob)
        assert decoded.get(meta_key), (
            f"git_meta.{meta_key} missing in {col_key}: {decoded}"
        )


# ── Migration ─────────────────────────────────────────────────────────────────

def test_migration_moves_prose_from_code_to_docs(
    rich_repo: Path, tmp_path: Path, local_t3: T3Database,
) -> None:
    """RDR-102 D2 update: ``_prune_misclassified`` in ``indexer.py``
    keys on ``doc_id`` (the post-Phase-A canonical identity) when the
    file is in the catalog hook's ``file_to_doc_id`` map. With Phase
    B removing source_path from the chunk schema, the doc_id-keyed
    lookup is the only path that can find a misclassified chunk; the
    legacy source_path fallback returns nothing because chunks no
    longer carry source_path.

    Test flow:
      1. Index once — populates the catalog with the canonical
         README.md tumbler.
      2. Read the tumbler from the catalog.
      3. Seed a bad chunk into code__ with that tumbler as doc_id.
      4. Re-index — the doc_id-keyed _prune_misclassified must find
         and remove the seed chunk because README.md is in
         file_to_doc_id (catalog hook re-runs every index).
    """
    from nexus.catalog import reset_cache
    from nexus.catalog.catalog import Catalog
    from nexus.config import catalog_path

    # _catalog_hook returns early when the catalog is not initialized
    # at NEXUS_CATALOG_PATH. Initialize it so the hook can register
    # README.md and the indexer threads its tumbler into file_to_doc_id.
    cat_path = catalog_path()
    Catalog.init(cat_path)
    reset_cache()

    reg = RepoRegistry(tmp_path / "repos.json")
    reg.add(rich_repo)
    info = reg.get(rich_repo)

    # Step 1 — first index populates the catalog with README's tumbler.
    _index(rich_repo, reg, local_t3)

    # Step 2 — read the tumbler that _catalog_hook assigned.
    reset_cache()
    cat = Catalog(cat_path, cat_path / ".catalog.db")
    row = cat._db.execute(
        "SELECT tumbler FROM documents WHERE file_path = ?",
        ("README.md",),
    ).fetchone()
    cat._db.close()
    assert row is not None, (
        "expected catalog to have README.md after first index"
    )
    readme_tumbler = row[0]

    # Step 3 — seed a bad chunk with README's doc_id into code__.
    code_col = local_t3.get_or_create_collection(info["code_collection"])
    code_col.add(
        ids=["fake-prose-in-code"],
        documents=["Nexus is a semantic search system"],
        metadatas=[{
            "doc_id": readme_tumbler,
            "title": "README.md:chunk-0",
            "content_hash": "old-hash",
            "embedding_model": "voyage-code-3", "store_type": "code",
        }],
    )
    assert len(code_col.get(ids=["fake-prose-in-code"])["ids"]) == 1

    # Step 4 — re-index triggers _prune_misclassified, which uses
    # the doc_id-keyed where-filter to find the seed chunk and remove
    # it from code__ (README.md classifies as prose, so its doc_id
    # in code__ is misclassified).
    _index(rich_repo, reg, local_t3)

    assert len(code_col.get(ids=["fake-prose-in-code"])["ids"]) == 0, (
        "migration must prune the seed chunk via the doc_id-keyed "
        "_prune_misclassified path"
    )
    assert any("README.md" in p for p in _get_sources(local_t3, info["docs_collection"]))


# ── Index-then-search (mini_repo) ────────────────────────────────────────────

@pytest.mark.parametrize("query,expected_file", [
    ("parse TTL days weeks permanent", "ttl.py"),
    ("session identifier process group ID", "session.py"),
    ("collection name prefix code docs knowledge", "corpus.py"),
])
def test_index_then_search(
    mini_repo: Path, registry: RepoRegistry, local_t3: T3Database,
    query: str, expected_file: str,
) -> None:
    _index(mini_repo, registry, local_t3)
    col = registry.get(mini_repo)["collection"]
    results = local_t3.search(query, [col], n_results=5)
    assert len(results) > 0
    # RDR-102 D2: source_path is gone; title carries the filename via
    # the "{relpath}:chunk-{i}" pattern (code_indexer.py:393).
    sources = [r.get("title", "") for r in results]
    assert any(expected_file in p for p in sources), f"{expected_file} not in: {sources}"


# ── CLI tests ─────────────────────────────────────────────────────────────────


def test_cli_index_repo(
    mini_repo: Path, tmp_path: Path, local_t3: T3Database, monkeypatch: pytest.MonkeyPatch,
) -> None:
    from click.testing import CliRunner
    from nexus.cli import main
    monkeypatch.setenv("HOME", str(tmp_path))
    runner = CliRunner()
    with patch("nexus.db.make_t3", return_value=local_t3), \
         patch("nexus.config.get_credential", side_effect=lambda k: "test-key"):
        result = runner.invoke(main, ["index", "repo", str(mini_repo)])
    assert result.exit_code == 0
    assert "Done" in result.output


def test_cli_index_then_search(
    mini_repo: Path, tmp_path: Path, local_t3: T3Database, monkeypatch: pytest.MonkeyPatch,
) -> None:
    from click.testing import CliRunner
    from nexus.cli import main
    monkeypatch.setenv("HOME", str(tmp_path))
    runner = CliRunner()
    col_name = f"code__{mini_repo.name}-{hashlib.sha256(str(mini_repo).encode()).hexdigest()[:8]}"
    with patch("nexus.db.make_t3", return_value=local_t3), \
         patch("nexus.config.get_credential", side_effect=lambda k: "test-key"), \
         patch("nexus.commands.search_cmd._t3", return_value=local_t3):
        idx = runner.invoke(main, ["index", "repo", str(mini_repo)])
        assert idx.exit_code == 0
        search = runner.invoke(main, ["search", "parse TTL expiry days", "--corpus", col_name, "--n", "5"])
    assert search.exit_code == 0
    assert len(search.output.strip()) > 0


def test_cli_index_frecency_only(
    mini_repo: Path, tmp_path: Path, local_t3: T3Database, monkeypatch: pytest.MonkeyPatch,
) -> None:
    from click.testing import CliRunner
    from nexus.cli import main
    monkeypatch.setenv("HOME", str(tmp_path))
    runner = CliRunner()
    with patch("nexus.db.make_t3", return_value=local_t3), \
         patch("nexus.config.get_credential", side_effect=lambda k: "test-key"):
        runner.invoke(main, ["index", "repo", str(mini_repo)])
        result = runner.invoke(main, ["index", "repo", str(mini_repo), "--frecency-only"])
    assert result.exit_code == 0
    assert "Done" in result.output


# ── AC-E5: PDF routing ───────────────────────────────────────────────────────

def test_index_repository_pdf_routing(
    rich_repo: Path, rich_registry: RepoRegistry, local_t3: T3Database,
) -> None:
    from nexus.registry import _docs_collection_name
    _index(rich_repo, rich_registry, local_t3)
    docs_col = _docs_collection_name(rich_repo)
    results = local_t3.search("Hello World test document PDF ingest", [docs_col], n_results=5)
    pdf_results = [r for r in results if r.get("store_type") == "pdf"]
    assert pdf_results, f"No PDF chunks; store_types: {[r.get('store_type') for r in results]}"
    assert isinstance(pdf_results[0]["title"], str)
