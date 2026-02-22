"""T1: indexer.py — status transitions, error path, credential skip, hidden file filter."""
from pathlib import Path
from unittest.mock import MagicMock, call, patch

import pytest

from nexus.indexer import CredentialsMissingError, index_repository


@pytest.fixture
def registry():
    mock = MagicMock()
    mock.get.return_value = {"collection": "code__repo", "status": "registered"}
    return mock


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
    registry.get.return_value = {"collection": "code__repo"}

    monkeypatch.delenv("VOYAGE_API_KEY", raising=False)
    monkeypatch.delenv("CHROMA_API_KEY", raising=False)

    with patch("nexus.frecency.batch_frecency", return_value={}):
        with patch("nexus.ripgrep_cache.build_cache"):
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
    registry.get.return_value = {"collection": "code__myproject"}

    monkeypatch.delenv("VOYAGE_API_KEY", raising=False)
    monkeypatch.delenv("CHROMA_API_KEY", raising=False)

    with patch("nexus.frecency.batch_frecency", return_value={}):
        with patch("nexus.ripgrep_cache.build_cache", side_effect=fake_build_cache):
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
    registry.get.return_value = {"collection": "code__repo"}

    monkeypatch.delenv("VOYAGE_API_KEY", raising=False)
    monkeypatch.delenv("CHROMA_API_KEY", raising=False)

    # Capture paths that make it into the scored list via build_cache
    seen_paths: list[Path] = []

    def fake_build_cache(repo_: Path, cache_path: Path, scored: list) -> None:
        seen_paths.extend(f for _, f in scored)

    with patch("nexus.frecency.batch_frecency", return_value={}):
        with patch("nexus.ripgrep_cache.build_cache", side_effect=fake_build_cache):
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
    registry.get.return_value = {"collection": "code__repo"}

    captured_metadatas: list = []

    mock_col = MagicMock()
    mock_col.get.return_value = {"metadatas": []}  # no existing data → proceed to upsert

    mock_db = MagicMock()
    mock_db.get_or_create_collection.return_value = mock_col

    def capture_upsert(collection, ids, documents, metadatas):
        captured_metadatas.extend(metadatas)

    mock_db.upsert_chunks.side_effect = capture_upsert

    fake_chunk = {
        "line_start": 1, "line_end": 1, "text": "x = 1",
        "chunk_index": 0, "chunk_count": 1,
        "ast_chunked": False, "filename": "main.py", "file_extension": ".py",
    }

    with patch("nexus.frecency.batch_frecency", return_value={}):
        with patch("nexus.ripgrep_cache.build_cache"):
            with patch("nexus.indexer._git_metadata", return_value={}):
                with patch("nexus.config.load_config", return_value={"server": {"ignorePatterns": []}}):
                    with patch("nexus.config.get_credential", return_value="fake-key"):
                        with patch("nexus.db.make_t3", return_value=mock_db):
                            with patch("nexus.chunker.chunk_file", return_value=[fake_chunk]):
                                _run_index(repo, registry)

    assert captured_metadatas, "Expected upsert_chunks to be called for main.py"
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
    registry.get.return_value = {"collection": "code__repo"}

    mock_col = MagicMock()
    # Simulate file already indexed with the same content_hash
    mock_col.get.return_value = {"metadatas": [{"content_hash": content_hash}]}

    mock_db = MagicMock()
    mock_db.get_or_create_collection.return_value = mock_col

    with patch("nexus.frecency.batch_frecency", return_value={}):
        with patch("nexus.ripgrep_cache.build_cache"):
            with patch("nexus.indexer._git_metadata", return_value={}):
                with patch("nexus.config.load_config", return_value={"server": {"ignorePatterns": []}}):
                    with patch("nexus.config.get_credential", return_value="fake-key"):
                        with patch("nexus.db.make_t3", return_value=mock_db):
                            _run_index(repo, registry)

    mock_db.upsert_chunks.assert_not_called()


# ── _run_index_frecency_only ──────────────────────────────────────────────────

def test_frecency_only_updates_frecency_score(tmp_path: Path) -> None:
    """_run_index_frecency_only updates frecency_score on existing chunks."""
    from nexus.indexer import _run_index_frecency_only

    repo = tmp_path / "repo"
    repo.mkdir()
    src_file = repo / "main.py"
    src_file.write_text("x = 1\n")

    registry = MagicMock()
    registry.get.return_value = {"collection": "code__repo"}

    old_meta = {"frecency_score": 0.1, "source_path": str(src_file), "title": "main.py:1-1"}
    mock_col = MagicMock()
    mock_col.get.return_value = {"ids": ["chunk-1"], "metadatas": [old_meta]}

    mock_db = MagicMock()
    mock_db.get_or_create_collection.return_value = mock_col

    with patch("nexus.frecency.batch_frecency", return_value={src_file: 0.75}):
        with patch("nexus.config.get_credential", return_value="fake-key"):
            with patch("nexus.db.make_t3", return_value=mock_db):
                _run_index_frecency_only(repo, registry)

    mock_db.update_chunks.assert_called_once()
    call_kwargs = mock_db.update_chunks.call_args.kwargs
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
    registry.get.return_value = {"collection": "code__repo"}

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
    registry.get.return_value = {"collection": "code__repo"}

    monkeypatch.delenv("VOYAGE_API_KEY", raising=False)
    monkeypatch.delenv("CHROMA_API_KEY", raising=False)

    with pytest.raises(CredentialsMissingError):
        _run_index_frecency_only(repo, registry)
