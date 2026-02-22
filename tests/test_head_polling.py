"""AC3: HEAD polling — detect hash change, trigger re-index, skip if indexing."""
from pathlib import Path
from unittest.mock import MagicMock, call, patch

import pytest

from nexus.polling import check_and_reindex


@pytest.fixture
def registry():
    mock = MagicMock()
    return mock


def test_no_change_skips_reindex(registry) -> None:
    """If HEAD hash unchanged, re-index is not triggered."""
    repo = Path("/repo")
    registry.get.return_value = {
        "head_hash": "abc123",
        "status": "ready",
        "collection": "code__repo",
    }

    with patch("nexus.polling._current_head", return_value="abc123"):
        with patch("nexus.polling.index_repo") as mock_index:
            check_and_reindex(repo, registry)

    mock_index.assert_not_called()


def test_head_changed_triggers_reindex(registry) -> None:
    """If HEAD hash changed, re-index is triggered and registry updated."""
    repo = Path("/repo")
    registry.get.return_value = {
        "head_hash": "old_hash",
        "status": "ready",
        "collection": "code__repo",
    }

    with patch("nexus.polling._current_head", return_value="new_hash"):
        with patch("nexus.polling.index_repo") as mock_index:
            check_and_reindex(repo, registry)

    mock_index.assert_called_once_with(repo, registry)
    registry.update.assert_called_once_with(repo, head_hash="new_hash")


def test_indexing_status_skips_reindex(registry) -> None:
    """If repo status is 'indexing', skip even if HEAD changed."""
    repo = Path("/repo")
    registry.get.return_value = {
        "head_hash": "old_hash",
        "status": "indexing",
        "collection": "code__repo",
    }

    with patch("nexus.polling._current_head", return_value="new_hash"):
        with patch("nexus.polling.index_repo") as mock_index:
            check_and_reindex(repo, registry)

    mock_index.assert_not_called()
    registry.update.assert_not_called()


def test_index_failure_still_updates_head_hash(registry) -> None:
    """When index_repo raises, head_hash is still updated to prevent infinite retry loops."""
    repo = Path("/repo")
    registry.get.return_value = {
        "head_hash": "old_hash",
        "status": "ready",
        "collection": "code__repo",
    }

    with patch("nexus.polling._current_head", return_value="new_hash"):
        with patch("nexus.polling.index_repo", side_effect=RuntimeError("index failed")):
            with pytest.raises(RuntimeError, match="index failed"):
                check_and_reindex(repo, registry)

    registry.update.assert_called_once_with(repo, head_hash="new_hash")


def test_missing_repo_in_registry_skips(registry) -> None:
    """If registry.get returns None, skip gracefully."""
    repo = Path("/repo")
    registry.get.return_value = None

    with patch("nexus.polling._current_head", return_value="abc123"):
        with patch("nexus.polling.index_repo") as mock_index:
            check_and_reindex(repo, registry)

    mock_index.assert_not_called()
