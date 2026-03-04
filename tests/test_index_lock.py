# SPDX-License-Identifier: AGPL-3.0-or-later
"""T1: index_repository per-repo file lock, on_locked flag, and head_hash update."""
import fcntl
import threading
from pathlib import Path
from unittest.mock import MagicMock, call, patch

import pytest
from click.testing import CliRunner

from nexus.cli import main
from nexus.indexer import index_repository


# ── fixtures ──────────────────────────────────────────────────────────────────


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


@pytest.fixture
def runner() -> CliRunner:
    return CliRunner()


@pytest.fixture
def lock_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Redirect HOME so lock files land in tmp_path."""
    monkeypatch.setenv("HOME", str(tmp_path))
    return tmp_path


# ── lock path helper ──────────────────────────────────────────────────────────


def test_repo_lock_path_is_stable(tmp_path: Path) -> None:
    """_repo_lock_path returns a consistent path for the same repo."""
    from nexus.indexer import _repo_lock_path

    p1 = _repo_lock_path(tmp_path)
    p2 = _repo_lock_path(tmp_path)
    assert p1 == p2
    assert p1.suffix == ".lock"
    assert "locks" in str(p1)


# ── lock held during indexing ─────────────────────────────────────────────────


def test_lock_is_held_during_indexing(tmp_path: Path, registry, lock_home: Path) -> None:
    """Non-blocking lock attempt fails while index_repository holds the lock."""
    from nexus.indexer import _repo_lock_path

    lock_path = _repo_lock_path(tmp_path)
    lock_held_during_run: list[bool] = []

    def _fake_run_index(*args, **kwargs):
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        with open(lock_path, "w") as probe:
            try:
                fcntl.flock(probe, fcntl.LOCK_EX | fcntl.LOCK_NB)
                lock_held_during_run.append(False)
                fcntl.flock(probe, fcntl.LOCK_UN)
            except BlockingIOError:
                lock_held_during_run.append(True)
        return {}

    with patch("nexus.indexer._run_index", side_effect=_fake_run_index):
        with patch("nexus.indexer._current_head", return_value="abc123"):
            index_repository(tmp_path, registry)

    assert lock_held_during_run == [True], "Lock must be held while _run_index executes"


# ── on_locked=skip ────────────────────────────────────────────────────────────


def test_on_locked_skip_returns_empty_when_locked(tmp_path: Path, registry, lock_home: Path) -> None:
    """on_locked=skip returns {} immediately without running when lock is held."""
    from nexus.indexer import _repo_lock_path

    lock_path = _repo_lock_path(tmp_path)
    lock_path.parent.mkdir(parents=True, exist_ok=True)

    with open(lock_path, "w") as holder:
        fcntl.flock(holder, fcntl.LOCK_EX)

        with patch("nexus.indexer._run_index") as mock_run:
            result = index_repository(tmp_path, registry, on_locked="skip")

    assert result == {}
    mock_run.assert_not_called()
    # Status should NOT have been set to "indexing"
    assert not any(c == call(tmp_path, status="indexing") for c in registry.update.call_args_list)


def test_on_locked_skip_runs_when_unlocked(tmp_path: Path, registry, lock_home: Path) -> None:
    """on_locked=skip acquires the lock and runs when not contested."""
    with patch("nexus.indexer._run_index", return_value={"rdr_indexed": 0}) as mock_run:
        with patch("nexus.indexer._current_head", return_value="abc123"):
            result = index_repository(tmp_path, registry, on_locked="skip")

    mock_run.assert_called_once()
    assert result == {"rdr_indexed": 0}


# ── on_locked=wait ────────────────────────────────────────────────────────────


def test_on_locked_wait_blocks_then_runs(tmp_path: Path, registry, lock_home: Path) -> None:
    """on_locked=wait blocks while lock is held, then runs after release."""
    from nexus.indexer import _repo_lock_path

    lock_path = _repo_lock_path(tmp_path)
    lock_path.parent.mkdir(parents=True, exist_ok=True)

    ran_event = threading.Event()
    lock_released = threading.Event()

    def _fake_run(*args, **kwargs):
        ran_event.set()
        return {}

    def run_indexer():
        with patch("nexus.indexer._run_index", side_effect=_fake_run):
            with patch("nexus.indexer._current_head", return_value="aaa"):
                index_repository(tmp_path, registry, on_locked="wait")

    # Hold the lock, start indexer in background, verify it blocks, then release
    with open(lock_path, "w") as holder:
        fcntl.flock(holder, fcntl.LOCK_EX)

        t = threading.Thread(target=run_indexer, daemon=True)
        t.start()

        # Should still be blocked after 200 ms
        assert not ran_event.wait(timeout=0.3), "Indexer should be blocked while lock is held"

    # Lock released by exiting `with` block — indexer should now run
    assert ran_event.wait(timeout=5.0), "Indexer should run after lock is released"
    t.join(timeout=5.0)


# ── frecency_only bypasses lock ───────────────────────────────────────────────


def test_frecency_only_bypasses_lock(tmp_path: Path, registry, lock_home: Path) -> None:
    """frecency_only=True runs even when the lock file would be contested."""
    from nexus.indexer import _repo_lock_path

    lock_path = _repo_lock_path(tmp_path)
    lock_path.parent.mkdir(parents=True, exist_ok=True)

    with open(lock_path, "w") as holder:
        fcntl.flock(holder, fcntl.LOCK_EX)

        with patch("nexus.indexer._run_index_frecency_only") as mock_frec:
            result = index_repository(tmp_path, registry, frecency_only=True)

    mock_frec.assert_called_once()
    assert result == {}


# ── head_hash update ──────────────────────────────────────────────────────────


def test_head_hash_updated_after_full_index(tmp_path: Path, registry, lock_home: Path) -> None:
    """head_hash is updated in registry after a successful full-index run."""
    with patch("nexus.indexer._run_index", return_value={}):
        with patch("nexus.indexer._current_head", return_value="deadbeef") as mock_head:
            index_repository(tmp_path, registry)

    mock_head.assert_called_once_with(tmp_path)
    assert call(tmp_path, head_hash="deadbeef") in registry.update.call_args_list


def _has_head_hash_call(registry: MagicMock) -> bool:
    """Return True if any registry.update call includes a head_hash keyword arg."""
    return any(
        "head_hash" in (c.kwargs if hasattr(c, "kwargs") else {})
        for c in registry.update.call_args_list
    )


def test_head_hash_not_updated_on_frecency_only(tmp_path: Path, registry, lock_home: Path) -> None:
    """head_hash is NOT updated when frecency_only=True."""
    with patch("nexus.indexer._run_index_frecency_only"):
        with patch("nexus.indexer._current_head") as mock_head:
            index_repository(tmp_path, registry, frecency_only=True)

    mock_head.assert_not_called()
    assert not _has_head_hash_call(registry)


def test_head_hash_not_updated_on_credentials_error(tmp_path: Path, registry, lock_home: Path) -> None:
    """head_hash is NOT updated when indexing fails with CredentialsMissingError."""
    from nexus.indexer import CredentialsMissingError

    with patch("nexus.indexer._run_index", side_effect=CredentialsMissingError("no creds")):
        with patch("nexus.indexer._current_head", return_value="abc"):
            with pytest.raises(CredentialsMissingError):
                index_repository(tmp_path, registry)

    assert not _has_head_hash_call(registry)


def test_head_hash_not_updated_on_other_error(tmp_path: Path, registry, lock_home: Path) -> None:
    """head_hash is NOT updated when indexing raises any other exception."""
    with patch("nexus.indexer._run_index", side_effect=RuntimeError("boom")):
        with patch("nexus.indexer._current_head", return_value="abc"):
            with pytest.raises(RuntimeError):
                index_repository(tmp_path, registry)

    assert not _has_head_hash_call(registry)


# ── lock released after indexing ─────────────────────────────────────────────


def test_lock_released_after_indexing(tmp_path: Path, registry, lock_home: Path) -> None:
    """Lock is released after index_repository returns (success or failure)."""
    from nexus.indexer import _repo_lock_path

    with patch("nexus.indexer._run_index", return_value={}):
        with patch("nexus.indexer._current_head", return_value="abc"):
            index_repository(tmp_path, registry)

    # Should be able to acquire the lock immediately after
    lock_path = _repo_lock_path(tmp_path)
    with open(lock_path, "w") as f:
        fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)  # must not raise
        fcntl.flock(f, fcntl.LOCK_UN)


def test_lock_released_on_exception(tmp_path: Path, registry, lock_home: Path) -> None:
    """Lock is released even when indexing raises."""
    from nexus.indexer import _repo_lock_path

    with patch("nexus.indexer._run_index", side_effect=RuntimeError("crash")):
        with patch("nexus.indexer._current_head", return_value="abc"):
            with pytest.raises(RuntimeError):
                index_repository(tmp_path, registry)

    lock_path = _repo_lock_path(tmp_path)
    with open(lock_path, "w") as f:
        fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)  # must not raise
        fcntl.flock(f, fcntl.LOCK_UN)


# ── CLI --on-locked option ────────────────────────────────────────────────────


def test_cli_on_locked_default_is_wait(runner: CliRunner, lock_home: Path) -> None:
    """nx index repo uses on_locked='wait' by default."""
    repo = lock_home / "myrepo"
    repo.mkdir()

    mock_reg = MagicMock()
    mock_reg.get.return_value = {"collection": "code__myrepo"}

    with patch("nexus.commands.index._registry", return_value=mock_reg):
        with patch("nexus.indexer.index_repository") as mock_idx:
            mock_idx.return_value = {}
            result = runner.invoke(main, ["index", "repo", str(repo)])

    assert result.exit_code == 0
    _, kwargs = mock_idx.call_args
    assert kwargs.get("on_locked", "wait") == "wait"


def test_cli_on_locked_skip(runner: CliRunner, lock_home: Path) -> None:
    """nx index repo --on-locked=skip passes on_locked='skip'."""
    repo = lock_home / "myrepo"
    repo.mkdir()

    mock_reg = MagicMock()
    mock_reg.get.return_value = {"collection": "code__myrepo"}

    with patch("nexus.commands.index._registry", return_value=mock_reg):
        with patch("nexus.indexer.index_repository") as mock_idx:
            mock_idx.return_value = {}
            result = runner.invoke(main, ["index", "repo", str(repo), "--on-locked=skip"])

    assert result.exit_code == 0
    _, kwargs = mock_idx.call_args
    assert kwargs["on_locked"] == "skip"


def test_cli_on_locked_invalid_value(runner: CliRunner, lock_home: Path) -> None:
    """nx index repo --on-locked=bad rejects invalid values."""
    repo = lock_home / "myrepo"
    repo.mkdir()

    with patch("nexus.commands.index._registry", return_value=MagicMock()):
        result = runner.invoke(main, ["index", "repo", str(repo), "--on-locked=bad"])

    assert result.exit_code != 0
