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
from unittest.mock import patch

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


def _patch_t3(t3: T3Database):
    """Patch make_t3 and credential checks to use local EphemeralClient."""
    return (
        patch("nexus.db.make_t3", return_value=t3),
        patch("nexus.config.get_credential", side_effect=lambda k: "test-key"),
    )


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


# ── CLI: nx index code ─────────────────────────────────────────────────────────

def test_cli_index_code_registers_and_indexes(
    mini_repo: Path, tmp_path: Path, local_t3: T3Database,
    monkeypatch: pytest.MonkeyPatch
) -> None:
    """nx index code <path> registers the repo and indexes it end-to-end."""
    from click.testing import CliRunner
    from nexus.cli import main

    monkeypatch.setenv("HOME", str(tmp_path))
    runner = CliRunner()

    with patch("nexus.db.make_t3", return_value=local_t3), \
         patch("nexus.config.get_credential", side_effect=lambda k: "test-key"):
        result = runner.invoke(main, ["index", "code", str(mini_repo)])

    assert result.exit_code == 0, result.output
    assert "Done" in result.output


def test_cli_index_then_search_pipeline(
    mini_repo: Path, tmp_path: Path, local_t3: T3Database,
    monkeypatch: pytest.MonkeyPatch
) -> None:
    """nx index code → nx search: full CLI pipeline returns results."""
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

    with patch("nexus.db.make_t3", return_value=local_t3), \
         patch("nexus.config.get_credential", side_effect=lambda k: "test-key"), \
         patch("nexus.commands.search_cmd._t3", return_value=local_t3):

        index_result = runner.invoke(main, ["index", "code", str(mini_repo)])
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
    """nx index code --frecency-only completes without error after a full index."""
    from click.testing import CliRunner
    from nexus.cli import main

    monkeypatch.setenv("HOME", str(tmp_path))
    runner = CliRunner()

    with patch("nexus.db.make_t3", return_value=local_t3), \
         patch("nexus.config.get_credential", side_effect=lambda k: "test-key"):
        runner.invoke(main, ["index", "code", str(mini_repo)])
        result = runner.invoke(main, ["index", "code", str(mini_repo), "--frecency-only"])

    assert result.exit_code == 0, result.output
    assert "Done" in result.output
