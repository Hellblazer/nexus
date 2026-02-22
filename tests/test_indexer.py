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
            with patch("nexus.db.t3.T3Database") as mock_t3:
                with pytest.raises(CredentialsMissingError):
                    _run_index(repo, registry)

    mock_t3.assert_not_called()


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
