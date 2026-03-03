"""T1: indexer.py — status transitions, error path, credential skip, hidden file filter."""
import subprocess
from pathlib import Path
from unittest.mock import MagicMock, call, patch

import pytest
from voyageai.object.embeddings import EmbeddingsObject

from nexus.indexer import CredentialsMissingError, index_repository


@pytest.fixture
def registry():
    mock = MagicMock()
    mock.get.return_value = {
        "collection": "code__repo",
        "code_collection": "code__repo",
        "docs_collection": "docs__repo",
        "status": "registered",
    }
    return mock


# Default config mock that includes the indexing section
_DEFAULT_CONFIG = {
    "server": {"ignorePatterns": []},
    "indexing": {"code_extensions": [], "prose_extensions": [], "rdr_paths": ["docs/rdr"], "include_untracked": False},
}


def test_index_sets_indexing_then_ready(tmp_path: Path, registry) -> None:
    """Status transitions: registered → indexing → ready."""
    repo = tmp_path / "repo"
    repo.mkdir()

    with patch("nexus.indexer._run_index"):
        index_repository(repo, registry)

    calls = registry.update.call_args_list
    assert any(c == call(repo, status="indexing") for c in calls)
    assert any(c == call(repo, status="ready") for c in calls)


def test_index_sets_error_on_failure(tmp_path: Path, registry) -> None:
    """If _run_index raises, status is set to 'error' and exception re-raised."""
    repo = tmp_path / "repo"
    repo.mkdir()

    with patch("nexus.indexer._run_index", side_effect=RuntimeError("boom")):
        with pytest.raises(RuntimeError, match="boom"):
            index_repository(repo, registry)

    calls = registry.update.call_args_list
    assert any(c == call(repo, status="indexing") for c in calls)
    assert any(c == call(repo, status="error") for c in calls)
    # 'ready' must NOT appear
    assert not any(c == call(repo, status="ready") for c in calls)


def test_run_index_raises_credentials_missing_without_credentials(
    tmp_path: Path, monkeypatch
) -> None:
    """Without VOYAGE_API_KEY, _run_index raises CredentialsMissingError (not silently skips)."""
    from nexus.indexer import _run_index

    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "hello.py").write_text("print('hi')\n")

    registry = MagicMock()
    registry.get.return_value = {
        "collection": "code__repo",
        "code_collection": "code__repo",
        "docs_collection": "docs__repo",
    }

    monkeypatch.delenv("VOYAGE_API_KEY", raising=False)
    monkeypatch.delenv("CHROMA_API_KEY", raising=False)

    with patch("nexus.frecency.batch_frecency", return_value={}):
        with patch("nexus.ripgrep_cache.build_cache"):
            with patch("nexus.config.load_config", return_value=_DEFAULT_CONFIG):
                with patch("nexus.db.make_t3") as mock_make_t3:
                    with pytest.raises(CredentialsMissingError):
                        _run_index(repo, registry)

    mock_make_t3.assert_not_called()


def test_index_sets_pending_credentials_when_missing(tmp_path: Path, registry) -> None:
    """When credentials are absent, status is 'pending_credentials' (not 'ready' or 'error')."""
    repo = tmp_path / "repo"
    repo.mkdir()

    with patch("nexus.indexer._run_index", side_effect=CredentialsMissingError("no creds")):
        with pytest.raises(CredentialsMissingError):
            index_repository(repo, registry)

    calls = registry.update.call_args_list
    assert any(c == call(repo, status="indexing") for c in calls)
    assert any(c == call(repo, status="pending_credentials") for c in calls)
    assert not any(c == call(repo, status="ready") for c in calls)
    assert not any(c == call(repo, status="error") for c in calls)


def test_cache_path_includes_repo_hash(tmp_path: Path, monkeypatch) -> None:
    """Cache filenames include a path hash so two repos named 'myproject' don't collide."""
    from nexus.indexer import _run_index

    repo_a = tmp_path / "myproject"
    repo_b = tmp_path / "other" / "myproject"
    repo_a.mkdir()
    repo_b.mkdir(parents=True)

    seen_paths: list[Path] = []

    def fake_build_cache(repo: Path, cache_path: Path, scored: list) -> None:
        seen_paths.append(cache_path)

    registry = MagicMock()
    registry.get.return_value = {
        "collection": "code__myproject",
        "code_collection": "code__myproject",
        "docs_collection": "docs__myproject",
    }

    monkeypatch.delenv("VOYAGE_API_KEY", raising=False)
    monkeypatch.delenv("CHROMA_API_KEY", raising=False)

    with patch("nexus.frecency.batch_frecency", return_value={}):
        with patch("nexus.ripgrep_cache.build_cache", side_effect=fake_build_cache):
            with patch("nexus.config.load_config", return_value=_DEFAULT_CONFIG):
                with pytest.raises(CredentialsMissingError):
                    _run_index(repo_a, registry)
                with pytest.raises(CredentialsMissingError):
                    _run_index(repo_b, registry)

    assert len(seen_paths) == 2
    assert seen_paths[0] != seen_paths[1], "Cache paths for same-name repos must differ"
    assert seen_paths[0].name != seen_paths[1].name


def test_run_index_skips_hidden_files(tmp_path: Path, monkeypatch) -> None:
    """Files inside hidden directories (e.g. .git/) are excluded."""
    from nexus.indexer import _run_index

    repo = tmp_path / "repo"
    repo.mkdir()
    hidden_dir = repo / ".git"
    hidden_dir.mkdir()
    (hidden_dir / "config").write_text("git config\n")
    (repo / "main.py").write_text("x = 1\n")

    registry = MagicMock()
    registry.get.return_value = {
        "collection": "code__repo",
        "code_collection": "code__repo",
        "docs_collection": "docs__repo",
    }

    monkeypatch.delenv("VOYAGE_API_KEY", raising=False)
    monkeypatch.delenv("CHROMA_API_KEY", raising=False)

    # Capture paths that make it into the scored list via build_cache
    seen_paths: list[Path] = []

    def fake_build_cache(repo_: Path, cache_path: Path, scored: list) -> None:
        seen_paths.extend(f for _, f in scored)

    with patch("nexus.frecency.batch_frecency", return_value={}):
        with patch("nexus.ripgrep_cache.build_cache", side_effect=fake_build_cache):
            with patch("nexus.config.load_config", return_value=_DEFAULT_CONFIG):
                with pytest.raises(CredentialsMissingError):
                    _run_index(repo, registry)

    assert all(".git" not in str(p) for p in seen_paths), f"Hidden files were not filtered: {seen_paths}"
    assert any("main.py" in str(p) for p in seen_paths)


def test_run_index_source_path_is_absolute(tmp_path: Path) -> None:
    """source_path in chunk metadata must be an absolute path, not relative to repo root."""
    from nexus.indexer import _run_index

    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "main.py").write_text("x = 1\n")

    registry = MagicMock()
    registry.get.return_value = {
        "collection": "code__repo",
        "code_collection": "code__repo",
        "docs_collection": "docs__repo",
    }

    captured_metadatas: list = []

    mock_col = MagicMock()
    mock_col.get.return_value = {"metadatas": [], "ids": []}

    mock_db = MagicMock()
    mock_db.get_or_create_collection.return_value = mock_col

    def capture_upsert(collection_name, ids, documents, embeddings, metadatas):
        captured_metadatas.extend(metadatas)

    mock_db.upsert_chunks_with_embeddings.side_effect = capture_upsert

    fake_chunk = {
        "line_start": 1, "line_end": 1, "text": "x = 1",
        "chunk_index": 0, "chunk_count": 1,
        "ast_chunked": False, "filename": "main.py", "file_extension": ".py",
    }

    mock_voyage_result = MagicMock(spec=EmbeddingsObject)
    mock_voyage_result.embeddings = [[0.1, 0.2, 0.3]]

    mock_voyage_client = MagicMock()
    mock_voyage_client.embed.return_value = mock_voyage_result

    with patch("nexus.frecency.batch_frecency", return_value={}):
        with patch("nexus.ripgrep_cache.build_cache"):
            with patch("nexus.indexer._git_metadata", return_value={}):
                with patch("nexus.config.load_config", return_value=_DEFAULT_CONFIG):
                    with patch("nexus.config.get_credential", return_value="fake-key"):
                        with patch("nexus.db.make_t3", return_value=mock_db):
                            with patch("nexus.chunker.chunk_file", return_value=[fake_chunk]):
                                with patch("voyageai.Client", return_value=mock_voyage_client):
                                    _run_index(repo, registry)

    assert captured_metadatas, "Expected upsert_chunks_with_embeddings to be called for main.py"
    source_path = captured_metadatas[0]["source_path"]
    assert Path(source_path).is_absolute(), f"source_path must be absolute; got {source_path!r}"
    assert source_path == str(repo / "main.py")


def test_run_index_skips_unchanged_content_hash(tmp_path: Path) -> None:
    """Files whose content_hash is already in T3 must be skipped (no upsert_chunks call)."""
    import hashlib
    from nexus.indexer import _run_index

    repo = tmp_path / "repo"
    repo.mkdir()
    content = "x = 1\n"
    (repo / "main.py").write_text(content)
    content_hash = hashlib.sha256(content.encode()).hexdigest()

    registry = MagicMock()
    registry.get.return_value = {
        "collection": "code__repo",
        "code_collection": "code__repo",
        "docs_collection": "docs__repo",
    }

    mock_col = MagicMock()
    # Simulate file already indexed with the same content_hash AND same embedding model
    mock_col.get.return_value = {
        "metadatas": [{"content_hash": content_hash, "embedding_model": "voyage-code-3"}],
        "ids": [],
    }

    mock_db = MagicMock()
    mock_db.get_or_create_collection.return_value = mock_col

    with patch("nexus.frecency.batch_frecency", return_value={}):
        with patch("nexus.ripgrep_cache.build_cache"):
            with patch("nexus.indexer._git_metadata", return_value={}):
                with patch("nexus.config.load_config", return_value=_DEFAULT_CONFIG):
                    with patch("nexus.config.get_credential", return_value="fake-key"):
                        with patch("nexus.db.make_t3", return_value=mock_db):
                            with patch("voyageai.Client"):
                                _run_index(repo, registry)

    mock_db.upsert_chunks_with_embeddings.assert_not_called()


def test_run_index_reindexes_when_embedding_model_changed(tmp_path: Path) -> None:
    """Files with matching content_hash but outdated embedding_model must be re-embedded."""
    import hashlib
    from nexus.indexer import _run_index

    repo = tmp_path / "repo"
    repo.mkdir()
    content = "x = 1\n"
    (repo / "main.py").write_text(content)
    content_hash = hashlib.sha256(content.encode()).hexdigest()

    registry = MagicMock()
    registry.get.return_value = {
        "collection": "code__repo",
        "code_collection": "code__repo",
        "docs_collection": "docs__repo",
    }

    mock_col = MagicMock()
    # Same content_hash but stale embedding model (voyage-4 from old collection-EF path)
    mock_col.get.return_value = {
        "metadatas": [{"content_hash": content_hash, "embedding_model": "voyage-4"}],
        "ids": [],
    }

    mock_db = MagicMock()
    mock_db.get_or_create_collection.return_value = mock_col

    mock_voyage_result = MagicMock(spec=EmbeddingsObject)
    mock_voyage_result.embeddings = [[0.1, 0.2, 0.3]]

    mock_voyage_client = MagicMock()
    mock_voyage_client.embed.return_value = mock_voyage_result

    fake_chunk = {
        "line_start": 1, "line_end": 1, "text": "x = 1",
        "chunk_index": 0, "chunk_count": 1,
        "ast_chunked": False, "filename": "main.py", "file_extension": ".py",
    }

    with patch("nexus.frecency.batch_frecency", return_value={}):
        with patch("nexus.ripgrep_cache.build_cache"):
            with patch("nexus.indexer._git_metadata", return_value={}):
                with patch("nexus.config.load_config", return_value=_DEFAULT_CONFIG):
                    with patch("nexus.config.get_credential", return_value="fake-key"):
                        with patch("nexus.db.make_t3", return_value=mock_db):
                            with patch("nexus.chunker.chunk_file", return_value=[fake_chunk]):
                                with patch("voyageai.Client", return_value=mock_voyage_client):
                                    _run_index(repo, registry)

    # Must re-embed even though content_hash matches, because embedding_model differs
    mock_db.upsert_chunks_with_embeddings.assert_called_once()


# ── _run_index_frecency_only ──────────────────────────────────────────────────

def test_frecency_only_updates_frecency_score(tmp_path: Path) -> None:
    """_run_index_frecency_only updates frecency_score on existing chunks."""
    from nexus.indexer import _run_index_frecency_only

    repo = tmp_path / "repo"
    repo.mkdir()
    src_file = repo / "main.py"
    src_file.write_text("x = 1\n")

    registry = MagicMock()
    registry.get.return_value = {
        "collection": "code__repo",
        "code_collection": "code__repo",
        "docs_collection": "docs__repo",
    }

    old_meta = {"frecency_score": 0.1, "source_path": str(src_file), "title": "main.py:1-1"}
    mock_col = MagicMock()
    mock_col.get.return_value = {"ids": ["chunk-1"], "metadatas": [old_meta]}

    mock_db = MagicMock()
    mock_db.get_or_create_collection.return_value = mock_col

    with patch("nexus.frecency.batch_frecency", return_value={src_file: 0.75}):
        with patch("nexus.config.get_credential", return_value="fake-key"):
            with patch("nexus.db.make_t3", return_value=mock_db):
                _run_index_frecency_only(repo, registry)

    # Called at least once (may be called for both code__ and docs__ collections)
    assert mock_db.update_chunks.call_count >= 1
    # Verify the first call has correct data
    call_kwargs = mock_db.update_chunks.call_args_list[0].kwargs
    assert call_kwargs["ids"] == ["chunk-1"]
    assert call_kwargs["metadatas"][0]["frecency_score"] == 0.75
    # Other metadata fields must be preserved
    assert call_kwargs["metadatas"][0]["title"] == "main.py:1-1"


def test_frecency_only_skips_unindexed_files(tmp_path: Path) -> None:
    """_run_index_frecency_only skips files with no existing indexed chunks."""
    from nexus.indexer import _run_index_frecency_only

    repo = tmp_path / "repo"
    repo.mkdir()
    src_file = repo / "new_file.py"
    src_file.write_text("y = 2\n")

    registry = MagicMock()
    registry.get.return_value = {
        "collection": "code__repo",
        "code_collection": "code__repo",
        "docs_collection": "docs__repo",
    }

    mock_col = MagicMock()
    # No existing chunks for this file
    mock_col.get.return_value = {"ids": [], "metadatas": []}

    mock_db = MagicMock()
    mock_db.get_or_create_collection.return_value = mock_col

    with patch("nexus.frecency.batch_frecency", return_value={src_file: 0.5}):
        with patch("nexus.config.get_credential", return_value="fake-key"):
            with patch("nexus.db.make_t3", return_value=mock_db):
                _run_index_frecency_only(repo, registry)

    mock_db.update_chunks.assert_not_called()


def test_frecency_only_raises_credentials_missing(tmp_path: Path, monkeypatch) -> None:
    """_run_index_frecency_only raises CredentialsMissingError when T3 keys are absent."""
    from nexus.indexer import _run_index_frecency_only

    repo = tmp_path / "repo"
    repo.mkdir()

    registry = MagicMock()
    registry.get.return_value = {
        "collection": "code__repo",
        "code_collection": "code__repo",
        "docs_collection": "docs__repo",
    }

    monkeypatch.delenv("VOYAGE_API_KEY", raising=False)
    monkeypatch.delenv("CHROMA_API_KEY", raising=False)

    with pytest.raises(CredentialsMissingError):
        _run_index_frecency_only(repo, registry)


# ── F1: UnicodeDecodeError / OSError debug logging ───────────────────────────

def test_run_index_logs_skipped_binary_files(tmp_path: Path) -> None:
    """Binary files that fail read_text() are skipped with a debug log."""
    from nexus.indexer import _run_index

    repo = tmp_path / "repo"
    repo.mkdir()
    # A valid text file that will be indexed normally
    (repo / "main.py").write_text("x = 1\n")
    # A binary file that will fail utf-8 decoding
    (repo / "image.bin").write_bytes(b"\x80\x81\x82\x83\xff\xfe")

    registry = MagicMock()
    registry.get.return_value = {
        "collection": "code__repo",
        "code_collection": "code__repo",
        "docs_collection": "docs__repo",
    }

    mock_col = MagicMock()
    mock_col.get.return_value = {"metadatas": [], "ids": []}

    mock_db = MagicMock()
    mock_db.get_or_create_collection.return_value = mock_col

    fake_chunk = {
        "line_start": 1, "line_end": 1, "text": "x = 1",
        "chunk_index": 0, "chunk_count": 1,
        "ast_chunked": False, "filename": "main.py", "file_extension": ".py",
    }

    mock_voyage_result = MagicMock(spec=EmbeddingsObject)
    mock_voyage_result.embeddings = [[0.1, 0.2, 0.3]]
    mock_voyage_client = MagicMock()
    mock_voyage_client.embed.return_value = mock_voyage_result

    with patch("nexus.frecency.batch_frecency", return_value={}):
        with patch("nexus.ripgrep_cache.build_cache"):
            with patch("nexus.indexer._git_metadata", return_value={}):
                with patch("nexus.config.load_config", return_value=_DEFAULT_CONFIG):
                    with patch("nexus.config.get_credential", return_value="fake-key"):
                        with patch("nexus.db.make_t3", return_value=mock_db):
                            with patch("nexus.chunker.chunk_file", return_value=[fake_chunk]):
                                with patch("voyageai.Client", return_value=mock_voyage_client):
                                    with patch("nexus.indexer._log") as mock_log:
                                        _run_index(repo, registry)

    # Verify debug was called for the skipped binary file
    debug_calls = mock_log.debug.call_args_list
    skipped_calls = [c for c in debug_calls if "skipped non-text file" in str(c)]
    assert skipped_calls, (
        f"Expected debug log for skipped binary file, got calls: {debug_calls}"
    )


# ── F2: empty chunks debug logging ──────────────────────────────────────────

def test_run_index_logs_empty_chunks(tmp_path: Path) -> None:
    """Files producing no chunks are skipped with a debug log."""
    from nexus.indexer import _run_index

    repo = tmp_path / "repo"
    repo.mkdir()
    # A file with only whitespace — chunker returns empty list
    (repo / "empty.py").write_text("   \n\n   \n")

    registry = MagicMock()
    registry.get.return_value = {
        "collection": "code__repo",
        "code_collection": "code__repo",
        "docs_collection": "docs__repo",
    }

    mock_col = MagicMock()
    mock_col.get.return_value = {"metadatas": [], "ids": []}

    mock_db = MagicMock()
    mock_db.get_or_create_collection.return_value = mock_col

    with patch("nexus.frecency.batch_frecency", return_value={}):
        with patch("nexus.ripgrep_cache.build_cache"):
            with patch("nexus.indexer._git_metadata", return_value={}):
                with patch("nexus.config.load_config", return_value=_DEFAULT_CONFIG):
                    with patch("nexus.config.get_credential", return_value="fake-key"):
                        with patch("nexus.db.make_t3", return_value=mock_db):
                            with patch("nexus.chunker.chunk_file", return_value=[]):
                                with patch("voyageai.Client"):
                                    with patch("nexus.indexer._log") as mock_log:
                                        _run_index(repo, registry)

    # Verify debug was called for the empty-chunks file
    debug_calls = mock_log.debug.call_args_list
    empty_calls = [c for c in debug_calls if "skipped file with no chunks" in str(c)]
    assert empty_calls, (
        f"Expected debug log for empty chunks file, got calls: {debug_calls}"
    )


# ── Content-class routing tests ──────────────────────────────────────────────


def _make_collection_tracking_db():
    """Create a mock DB that tracks upsert calls by collection name."""
    upserts_by_collection: dict[str, list] = {}
    cols_by_name: dict[str, MagicMock] = {}

    def get_or_create(name):
        if name not in cols_by_name:
            col = MagicMock()
            col.get.return_value = {"metadatas": [], "ids": []}
            cols_by_name[name] = col
        return cols_by_name[name]

    def capture_upsert(collection_name, ids, documents, embeddings, metadatas):
        upserts_by_collection.setdefault(collection_name, []).extend(metadatas)

    mock_db = MagicMock()
    mock_db.get_or_create_collection.side_effect = get_or_create
    mock_db.upsert_chunks_with_embeddings.side_effect = capture_upsert
    return mock_db, upserts_by_collection, cols_by_name


def _registry_with_dual_collections():
    """Create a registry mock with both code_collection and docs_collection."""
    registry = MagicMock()
    registry.get.return_value = {
        "collection": "code__repo",
        "code_collection": "code__repo",
        "docs_collection": "docs__repo",
    }
    return registry


def test_run_index_routes_prose_to_docs_collection(tmp_path: Path) -> None:
    """Markdown files should be indexed into the docs__ collection, not code__."""
    from nexus.indexer import _run_index

    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "README.md").write_text("# Hello\n\nThis is a README with enough content to chunk.\n")

    registry = _registry_with_dual_collections()
    mock_db, upserts, cols = _make_collection_tracking_db()

    mock_embed_result = (
        [[0.1] * 10],  # embeddings
        "voyage-context-3",  # actual model
    )

    with patch("nexus.frecency.batch_frecency", return_value={}):
        with patch("nexus.ripgrep_cache.build_cache"):
            with patch("nexus.indexer._git_metadata", return_value={}):
                with patch("nexus.config.load_config", return_value=_DEFAULT_CONFIG):
                    with patch("nexus.config.get_credential", return_value="fake-key"):
                        with patch("nexus.db.make_t3", return_value=mock_db):
                            with patch("voyageai.Client"):
                                with patch("nexus.doc_indexer._embed_with_fallback", return_value=mock_embed_result):
                                    _run_index(repo, registry)

    # docs__repo should have received chunks
    assert "docs__repo" in upserts, f"Expected docs__repo to receive chunks, got: {list(upserts.keys())}"
    assert all(m["category"] == "prose" for m in upserts["docs__repo"])
    # code__repo should NOT have received any chunks
    assert "code__repo" not in upserts, f"code__repo should not have chunks for .md files"


def test_run_index_routes_code_to_code_collection(tmp_path: Path) -> None:
    """Python files should be indexed into the code__ collection, not docs__."""
    from nexus.indexer import _run_index

    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "main.py").write_text("x = 1\n")

    registry = _registry_with_dual_collections()
    mock_db, upserts, cols = _make_collection_tracking_db()

    fake_chunk = {
        "line_start": 1, "line_end": 1, "text": "x = 1",
        "chunk_index": 0, "chunk_count": 1,
        "ast_chunked": False, "filename": "main.py", "file_extension": ".py",
    }

    mock_voyage_result = MagicMock(spec=EmbeddingsObject)
    mock_voyage_result.embeddings = [[0.1, 0.2, 0.3]]
    mock_voyage_client = MagicMock()
    mock_voyage_client.embed.return_value = mock_voyage_result

    with patch("nexus.frecency.batch_frecency", return_value={}):
        with patch("nexus.ripgrep_cache.build_cache"):
            with patch("nexus.indexer._git_metadata", return_value={}):
                with patch("nexus.config.load_config", return_value=_DEFAULT_CONFIG):
                    with patch("nexus.config.get_credential", return_value="fake-key"):
                        with patch("nexus.db.make_t3", return_value=mock_db):
                            with patch("nexus.chunker.chunk_file", return_value=[fake_chunk]):
                                with patch("voyageai.Client", return_value=mock_voyage_client):
                                    _run_index(repo, registry)

    # code__repo should have received chunks
    assert "code__repo" in upserts, f"Expected code__repo to receive chunks, got: {list(upserts.keys())}"
    assert all(m["category"] == "code" for m in upserts["code__repo"])
    # docs__repo should NOT have received any chunks
    assert "docs__repo" not in upserts, f"docs__repo should not have chunks for .py files"


def test_run_index_excludes_rdr_paths_from_docs(tmp_path: Path) -> None:
    """Files under rdr_paths should not be indexed into docs__."""
    from nexus.indexer import _run_index

    repo = tmp_path / "repo"
    repo.mkdir()
    # Create a regular markdown file (should be indexed in docs__)
    (repo / "README.md").write_text("# README\n\nProject description here.\n")
    # Create an RDR file (should NOT be indexed in docs__)
    rdr_dir = repo / "docs" / "rdr"
    rdr_dir.mkdir(parents=True)
    (rdr_dir / "ADR-001.md").write_text("# ADR-001\n\nArchitecture decision.\n")

    registry = _registry_with_dual_collections()
    mock_db, upserts, cols = _make_collection_tracking_db()

    mock_embed_result = (
        [[0.1] * 10],
        "voyage-context-3",
    )

    # Config with rdr_paths pointing to docs/rdr
    config_with_rdr = {
        "server": {"ignorePatterns": []},
        "indexing": {"code_extensions": [], "prose_extensions": [], "rdr_paths": ["docs/rdr"]},
    }

    with patch("nexus.frecency.batch_frecency", return_value={}):
        with patch("nexus.ripgrep_cache.build_cache"):
            with patch("nexus.indexer._git_metadata", return_value={}):
                with patch("nexus.config.load_config", return_value=config_with_rdr):
                    with patch("nexus.config.get_credential", return_value="fake-key"):
                        with patch("nexus.db.make_t3", return_value=mock_db):
                            with patch("voyageai.Client"):
                                with patch("nexus.doc_indexer._embed_with_fallback", return_value=mock_embed_result):
                                    with patch("nexus.doc_indexer.batch_index_markdowns") as mock_batch:
                                        _run_index(repo, registry)

    # docs__repo should have README but NOT ADR-001
    if "docs__repo" in upserts:
        source_paths = [m["source_path"] for m in upserts["docs__repo"]]
        assert any("README.md" in p for p in source_paths), "README.md should be in docs__repo"
        assert not any("ADR-001" in p for p in source_paths), "ADR-001 should NOT be in docs__repo"
    # batch_index_markdowns should have been called for the RDR files
    mock_batch.assert_called_once()
    rdr_call_paths = [str(p) for p in mock_batch.call_args[0][0]]
    assert any("ADR-001.md" in p for p in rdr_call_paths), "ADR-001.md should be in batch_index_markdowns call"


def test_run_index_returns_rdr_stats(tmp_path: Path) -> None:
    """_run_index returns a dict with rdr_indexed / rdr_current / rdr_failed counts."""
    from nexus.indexer import _run_index

    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "README.md").write_text("# README\n")
    rdr_dir = repo / "docs" / "rdr"
    rdr_dir.mkdir(parents=True)
    (rdr_dir / "001-decision.md").write_text("# Decision\n")
    (rdr_dir / "002-decision.md").write_text("# Decision 2\n")

    registry = _registry_with_dual_collections()
    mock_db, _, _ = _make_collection_tracking_db()
    config_with_rdr = {
        "server": {"ignorePatterns": []},
        "indexing": {"code_extensions": [], "prose_extensions": [], "rdr_paths": ["docs/rdr"]},
    }
    # batch_index_markdowns returns: 1 indexed, 1 skipped (already current), 0 failed
    mock_results = {
        str(rdr_dir / "001-decision.md"): "indexed",
        str(rdr_dir / "002-decision.md"): "skipped",
    }

    with patch("nexus.frecency.batch_frecency", return_value={}):
        with patch("nexus.ripgrep_cache.build_cache"):
            with patch("nexus.indexer._git_metadata", return_value={}):
                with patch("nexus.config.load_config", return_value=config_with_rdr):
                    with patch("nexus.config.get_credential", return_value="fake-key"):
                        with patch("nexus.db.make_t3", return_value=mock_db):
                            with patch("voyageai.Client"):
                                with patch("nexus.doc_indexer.batch_index_markdowns", return_value=mock_results):
                                    stats = _run_index(repo, registry)

    assert stats["rdr_indexed"] == 1
    assert stats["rdr_current"] == 1
    assert stats["rdr_failed"] == 0


def test_index_repo_cmd_shows_rdr_summary(tmp_path: Path) -> None:
    """nx index repo prints an RDR summary line when RDR documents exist."""
    from click.testing import CliRunner

    from nexus.cli import main
    from nexus.registry import RepoRegistry

    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "README.md").write_text("# README\n")
    rdr_dir = repo / "docs" / "rdr"
    rdr_dir.mkdir(parents=True)
    (rdr_dir / "001-decision.md").write_text("# Decision\n")

    registry_path = tmp_path / "repos.json"
    mock_results = {str(rdr_dir / "001-decision.md"): "indexed"}
    mock_stats = {"rdr_indexed": 1, "rdr_current": 0, "rdr_failed": 0}

    runner = CliRunner()
    with patch("nexus.commands.index._registry_path", return_value=registry_path):
        with patch("nexus.indexer.index_repository", return_value=mock_stats):
            result = runner.invoke(main, ["index", "repo", str(repo)])

    assert result.exit_code == 0, result.output
    assert "RDR documents" in result.output
    assert "1 indexed" in result.output


def test_index_repo_cmd_no_rdr_summary_when_no_rdrs(tmp_path: Path) -> None:
    """nx index repo omits the RDR summary line when no RDR documents are found."""
    from click.testing import CliRunner

    from nexus.cli import main

    repo = tmp_path / "repo"
    repo.mkdir()

    registry_path = tmp_path / "repos.json"
    mock_stats: dict = {"rdr_indexed": 0, "rdr_current": 0, "rdr_failed": 0}

    runner = CliRunner()
    with patch("nexus.commands.index._registry_path", return_value=registry_path):
        with patch("nexus.indexer.index_repository", return_value=mock_stats):
            result = runner.invoke(main, ["index", "repo", str(repo)])

    assert result.exit_code == 0, result.output
    assert "RDR documents" not in result.output


def test_run_index_mixed_repo(tmp_path: Path) -> None:
    """A repo with both code and prose files routes each to the correct collection."""
    from nexus.indexer import _run_index

    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "main.py").write_text("print('hello')\n")
    (repo / "README.md").write_text("# Project\n\nA simple project.\n")
    (repo / "notes.txt").write_text("Some notes about the project.\n")

    registry = _registry_with_dual_collections()
    mock_db, upserts, cols = _make_collection_tracking_db()

    fake_chunk = {
        "line_start": 1, "line_end": 1, "text": "print('hello')",
        "chunk_index": 0, "chunk_count": 1,
        "ast_chunked": False, "filename": "main.py", "file_extension": ".py",
    }

    mock_voyage_result = MagicMock(spec=EmbeddingsObject)
    mock_voyage_result.embeddings = [[0.1, 0.2, 0.3]]
    mock_voyage_client = MagicMock()
    mock_voyage_client.embed.return_value = mock_voyage_result

    mock_embed_result = ([[0.1] * 10], "voyage-context-3")

    with patch("nexus.frecency.batch_frecency", return_value={}):
        with patch("nexus.ripgrep_cache.build_cache"):
            with patch("nexus.indexer._git_metadata", return_value={}):
                with patch("nexus.config.load_config", return_value=_DEFAULT_CONFIG):
                    with patch("nexus.config.get_credential", return_value="fake-key"):
                        with patch("nexus.db.make_t3", return_value=mock_db):
                            with patch("nexus.chunker.chunk_file", return_value=[fake_chunk]):
                                with patch("voyageai.Client", return_value=mock_voyage_client):
                                    with patch("nexus.doc_indexer._embed_with_fallback", return_value=mock_embed_result):
                                        _run_index(repo, registry)

    # code__repo should have main.py
    assert "code__repo" in upserts
    code_paths = {m["source_path"] for m in upserts["code__repo"]}
    assert any("main.py" in p for p in code_paths)

    # docs__repo should have README.md and notes.txt
    assert "docs__repo" in upserts
    docs_paths = {m["source_path"] for m in upserts["docs__repo"]}
    assert any("README.md" in p for p in docs_paths)
    assert any("notes.txt" in p for p in docs_paths)


def test_run_index_prune_deleted_files(tmp_path: Path) -> None:
    """Chunks for files no longer in the repo should be pruned from both collections."""
    from nexus.indexer import _prune_deleted_files

    # Simulate: only file_a.py is current; file_b.py was deleted
    all_current = {"/repo/file_a.py"}

    mock_col = MagicMock()
    mock_col.get.return_value = {
        "ids": ["chunk-a1", "chunk-b1", "chunk-b2"],
        "metadatas": [
            {"source_path": "/repo/file_a.py"},
            {"source_path": "/repo/file_b.py"},
            {"source_path": "/repo/file_b.py"},
        ],
    }

    mock_db = MagicMock()
    mock_db.get_or_create_collection.return_value = mock_col

    _prune_deleted_files("code__repo", "docs__repo", all_current, mock_db)

    # Should delete chunks for file_b.py from both collections
    delete_calls = mock_col.delete.call_args_list
    assert len(delete_calls) == 2  # once for code__repo, once for docs__repo
    for dc in delete_calls:
        deleted_ids = dc.kwargs.get("ids") or dc[1].get("ids") if dc[1] else dc[0][0]
        # chunk-b1 and chunk-b2 should be in the deleted set
        if isinstance(deleted_ids, list):
            assert "chunk-a1" not in deleted_ids
            assert "chunk-b1" in deleted_ids or "chunk-b2" in deleted_ids


def test_run_index_prune_misclassified(tmp_path: Path) -> None:
    """Chunks in the wrong collection should be removed after reclassification."""
    from nexus.indexer import _prune_misclassified

    repo = tmp_path / "repo"
    repo.mkdir()

    code_files = [repo / "main.py"]
    prose_files = [repo / "README.md"]  # was previously in code__
    pdf_files: list[Path] = []

    # Mock: docs collection has a chunk for main.py (misclassified)
    mock_code_col = MagicMock()
    mock_code_col.get.return_value = {"ids": []}  # README.md not in code__ (clean)

    mock_docs_col = MagicMock()
    mock_docs_col.get.return_value = {"ids": ["stale-chunk-1"]}  # main.py in docs__ (wrong)

    cols = {"code__repo": mock_code_col, "docs__repo": mock_docs_col}
    mock_db = MagicMock()
    mock_db.get_or_create_collection.side_effect = lambda name: cols[name]

    _prune_misclassified(repo, "code__repo", "docs__repo", code_files, prose_files, pdf_files, mock_db)

    # main.py chunk should be deleted from docs__repo
    mock_docs_col.delete.assert_called_once_with(ids=["stale-chunk-1"])


def test_registry_c2_fallback(tmp_path: Path) -> None:
    """When registry lacks docs_collection, the deterministic naming function is used."""
    from nexus.indexer import _run_index
    from nexus.registry import _docs_collection_name

    repo = tmp_path / "repo"
    repo.mkdir()

    # Registry without docs_collection key
    registry = MagicMock()
    registry.get.return_value = {
        "collection": "code__repo",
        "code_collection": "code__repo",
        # No docs_collection key
    }

    expected_docs = _docs_collection_name(repo)
    collection_names_used: list[str] = []

    mock_col = MagicMock()
    mock_col.get.return_value = {"metadatas": [], "ids": []}
    mock_db = MagicMock()

    def track_get_or_create(name):
        collection_names_used.append(name)
        return mock_col

    mock_db.get_or_create_collection.side_effect = track_get_or_create

    with patch("nexus.frecency.batch_frecency", return_value={}):
        with patch("nexus.ripgrep_cache.build_cache"):
            with patch("nexus.indexer._git_metadata", return_value={}):
                with patch("nexus.config.load_config", return_value=_DEFAULT_CONFIG):
                    with patch("nexus.config.get_credential", return_value="fake-key"):
                        with patch("nexus.db.make_t3", return_value=mock_db):
                            with patch("voyageai.Client"):
                                _run_index(repo, registry)

    # The deterministic docs collection name should be used
    assert expected_docs in collection_names_used, (
        f"Expected {expected_docs} in {collection_names_used}"
    )


# ── _git_ls_files tests ──────────────────────────────────────────────────────


def test_git_ls_files_returns_tracked_files(tmp_path: Path) -> None:
    """_git_ls_files returns only tracked files, not .gitignored ones."""
    from nexus.indexer import _git_ls_files

    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "tracked.py").write_text("x = 1\n")
    (repo / ".env").write_text("SECRET=abc\n")
    (repo / ".gitignore").write_text(".env\n")

    subprocess.run(["git", "init", "-b", "main"], cwd=repo, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.email", "t@t"], cwd=repo, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.name", "T"], cwd=repo, check=True, capture_output=True)
    subprocess.run(["git", "add", "."], cwd=repo, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", "init"], cwd=repo, check=True, capture_output=True)

    files = _git_ls_files(repo)
    file_names = {f.name for f in files}
    assert "tracked.py" in file_names
    assert ".gitignore" in file_names
    assert ".env" not in file_names, ".env should be gitignored"


def test_git_ls_files_with_untracked(tmp_path: Path) -> None:
    """include_untracked=True also returns untracked-but-not-ignored files."""
    from nexus.indexer import _git_ls_files

    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "tracked.py").write_text("x = 1\n")
    (repo / ".gitignore").write_text(".env\n")

    subprocess.run(["git", "init", "-b", "main"], cwd=repo, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.email", "t@t"], cwd=repo, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.name", "T"], cwd=repo, check=True, capture_output=True)
    subprocess.run(["git", "add", "tracked.py", ".gitignore"], cwd=repo, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", "init"], cwd=repo, check=True, capture_output=True)

    # Create untracked (not ignored) file
    (repo / "new_file.py").write_text("y = 2\n")
    # Create ignored file
    (repo / ".env").write_text("SECRET=abc\n")

    # Without include_untracked
    files = _git_ls_files(repo, include_untracked=False)
    file_names = {f.name for f in files}
    assert "new_file.py" not in file_names

    # With include_untracked
    files = _git_ls_files(repo, include_untracked=True)
    file_names = {f.name for f in files}
    assert "new_file.py" in file_names
    assert ".env" not in file_names, ".env should still be gitignored"


def test_git_ls_files_fallback_on_non_git_dir(tmp_path: Path) -> None:
    """_git_ls_files returns empty list for non-git directories (triggers fallback)."""
    from nexus.indexer import _git_ls_files

    non_git = tmp_path / "not-a-repo"
    non_git.mkdir()
    (non_git / "file.py").write_text("x = 1\n")

    files = _git_ls_files(non_git)
    assert files == [], "Non-git directory should return empty list for fallback"


# ── DEFAULT_IGNORE / _should_ignore ──────────────────────────────────────────

def test_should_ignore_lock_files() -> None:
    """*.lock files are ignored by default (uv.lock, yarn.lock, Gemfile.lock, etc.)."""
    from nexus.indexer import DEFAULT_IGNORE, _should_ignore

    for name in ("uv.lock", "yarn.lock", "poetry.lock", "Gemfile.lock", "Cargo.lock"):
        assert _should_ignore(Path(name), DEFAULT_IGNORE), f"{name} should be ignored"


def test_should_ignore_go_sum() -> None:
    """go.sum is ignored by default."""
    from nexus.indexer import DEFAULT_IGNORE, _should_ignore

    assert _should_ignore(Path("go.sum"), DEFAULT_IGNORE)


def test_should_ignore_lock_files_in_subdirectory() -> None:
    """*.lock files are ignored regardless of directory depth."""
    from nexus.indexer import DEFAULT_IGNORE, _should_ignore

    assert _should_ignore(Path("subdir/uv.lock"), DEFAULT_IGNORE)
    assert _should_ignore(Path("a/b/c/yarn.lock"), DEFAULT_IGNORE)


def test_should_not_ignore_regular_files() -> None:
    """Normal source files are not caught by the lock/sum patterns."""
    from nexus.indexer import DEFAULT_IGNORE, _should_ignore

    for name in ("main.py", "README.md", "pyproject.toml", "go.mod"):
        assert not _should_ignore(Path(name), DEFAULT_IGNORE), f"{name} should not be ignored"


# ── empty-string filtering ────────────────────────────────────────────────────

def _make_voyage_mock(n_embeddings: int) -> MagicMock:
    mock_result = MagicMock(spec=EmbeddingsObject)
    mock_result.embeddings = [[float(i)] * 3 for i in range(n_embeddings)]
    mock_voyage = MagicMock()
    mock_voyage.embed.return_value = mock_result
    return mock_voyage


def test_index_code_file_skips_empty_text_chunks(tmp_path: Path) -> None:
    """Chunks with empty text are silently filtered before embedding.

    Regression test for: voyageai.error.InvalidRequestError: Input cannot contain
    empty strings or empty lists.
    """
    from nexus.indexer import _index_code_file

    repo = tmp_path / "repo"
    repo.mkdir()
    file_ = repo / "main.py"
    file_.write_text("x = 1\n")

    mock_col = MagicMock()
    mock_col.get.return_value = {"metadatas": [], "ids": []}
    mock_db = MagicMock()

    # chunker returns two chunks: one valid, one empty
    chunks = [
        {"line_start": 1, "line_end": 1, "text": "x = 1",
         "chunk_index": 0, "chunk_count": 2, "ast_chunked": False,
         "filename": "main.py", "file_extension": ".py"},
        {"line_start": 2, "line_end": 2, "text": "",   # <-- empty
         "chunk_index": 1, "chunk_count": 2, "ast_chunked": False,
         "filename": "main.py", "file_extension": ".py"},
    ]
    mock_voyage = _make_voyage_mock(1)  # only 1 valid chunk after filtering

    with patch("nexus.chunker.chunk_file", return_value=chunks):
        result = _index_code_file(
            file_, repo, "code__repo", "voyage-code-3",
            mock_col, mock_db, mock_voyage,
            git_meta={}, now_iso="2026-01-01T00:00:00", score=1.0,
        )

    assert result is True
    # embed must have been called with only the non-empty chunk
    assert mock_voyage.embed.called
    call_args = mock_voyage.embed.call_args
    texts = call_args[1].get("texts") or call_args[0][0]
    assert "" not in texts
    assert len(texts) == 1, "Only 1 non-empty chunk should be embedded"
    assert "x = 1" in texts[0], "Chunk content must appear in embed text (may be prefixed)"


def test_index_code_file_returns_false_when_all_chunks_empty(tmp_path: Path) -> None:
    """If every chunk has empty text, _index_code_file returns False without calling embed."""
    from nexus.indexer import _index_code_file

    repo = tmp_path / "repo"
    repo.mkdir()
    file_ = repo / "empty.py"
    file_.write_text("\n\n\n")

    mock_col = MagicMock()
    mock_col.get.return_value = {"metadatas": [], "ids": []}
    mock_db = MagicMock()

    empty_chunks = [
        {"line_start": i, "line_end": i, "text": "",
         "chunk_index": i, "chunk_count": 3, "ast_chunked": False,
         "filename": "empty.py", "file_extension": ".py"}
        for i in range(3)
    ]
    mock_voyage = _make_voyage_mock(0)

    with patch("nexus.chunker.chunk_file", return_value=empty_chunks):
        result = _index_code_file(
            file_, repo, "code__repo", "voyage-code-3",
            mock_col, mock_db, mock_voyage,
            git_meta={}, now_iso="2026-01-01T00:00:00", score=1.0,
        )

    assert result is False
    mock_voyage.embed.assert_not_called()
    mock_db.upsert_chunks_with_embeddings.assert_not_called()
