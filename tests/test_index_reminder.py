# SPDX-License-Identifier: AGPL-3.0-or-later
"""Tests for nx index repo hooks-install reminder (Phase 3 RDR-018)."""
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from click.testing import CliRunner

from nexus.cli import main


@pytest.fixture
def runner() -> CliRunner:
    return CliRunner()


@pytest.fixture
def index_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("HOME", str(tmp_path))
    return tmp_path


def _make_repo(base: Path) -> Path:
    """Create a minimal fake repo directory and return it."""
    repo = base / "myrepo"
    repo.mkdir()
    return repo


def _mock_reg(already_registered: bool = True) -> MagicMock:
    mock = MagicMock()
    mock.get.return_value = {"collection": "code__myrepo"} if already_registered else None
    return mock


# ── Reminder IS shown when no hook files contain the sentinel ──────────────────

def test_reminder_shown_when_no_hooks_installed(runner: CliRunner, index_home: Path, tmp_path: Path) -> None:
    """Reminder is printed when none of the 3 hook files contain the nexus sentinel."""
    repo = _make_repo(index_home)
    hooks_dir = tmp_path / "hooks"
    hooks_dir.mkdir()
    # No hook files at all — sentinel is absent

    with (
        patch("nexus.commands.index._registry", return_value=_mock_reg()),
        patch("nexus.indexer.index_repository", return_value={}),
        patch("nexus.commands.hooks._effective_hooks_dir", return_value=hooks_dir),
    ):
        result = runner.invoke(main, ["index", "repo", str(repo)])

    assert result.exit_code == 0, result.output
    assert "nx hooks install" in result.output
    assert "auto-index" in result.output


def test_reminder_shown_when_hook_files_exist_without_sentinel(
    runner: CliRunner, index_home: Path, tmp_path: Path
) -> None:
    """Reminder is printed when hook files exist but none contain the nexus sentinel."""
    repo = _make_repo(index_home)
    hooks_dir = tmp_path / "hooks"
    hooks_dir.mkdir()
    # Create hook files without the sentinel
    for name in ("post-commit", "post-merge", "post-rewrite"):
        (hooks_dir / name).write_text("#!/bin/sh\necho hello\n")

    with (
        patch("nexus.commands.index._registry", return_value=_mock_reg()),
        patch("nexus.indexer.index_repository", return_value={}),
        patch("nexus.commands.hooks._effective_hooks_dir", return_value=hooks_dir),
    ):
        result = runner.invoke(main, ["index", "repo", str(repo)])

    assert result.exit_code == 0, result.output
    assert "nx hooks install" in result.output


# ── Reminder is NOT shown when at least one hook contains the sentinel ─────────

def test_reminder_suppressed_when_post_commit_managed(
    runner: CliRunner, index_home: Path, tmp_path: Path
) -> None:
    """Reminder is suppressed when post-commit contains the nexus sentinel."""
    from nexus.commands.hooks import SENTINEL_BEGIN

    repo = _make_repo(index_home)
    hooks_dir = tmp_path / "hooks"
    hooks_dir.mkdir()
    (hooks_dir / "post-commit").write_text(f"#!/bin/sh\n{SENTINEL_BEGIN}\nnx index repo .\n")

    with (
        patch("nexus.commands.index._registry", return_value=_mock_reg()),
        patch("nexus.indexer.index_repository", return_value={}),
        patch("nexus.commands.hooks._effective_hooks_dir", return_value=hooks_dir),
    ):
        result = runner.invoke(main, ["index", "repo", str(repo)])

    assert result.exit_code == 0, result.output
    assert "nx hooks install" not in result.output


def test_reminder_suppressed_when_post_merge_managed(
    runner: CliRunner, index_home: Path, tmp_path: Path
) -> None:
    """Reminder is suppressed when post-merge contains the nexus sentinel."""
    from nexus.commands.hooks import SENTINEL_BEGIN

    repo = _make_repo(index_home)
    hooks_dir = tmp_path / "hooks"
    hooks_dir.mkdir()
    # Only post-merge has the sentinel; that's sufficient
    (hooks_dir / "post-merge").write_text(f"#!/bin/sh\n{SENTINEL_BEGIN}\nnx index repo .\n")

    with (
        patch("nexus.commands.index._registry", return_value=_mock_reg()),
        patch("nexus.indexer.index_repository", return_value={}),
        patch("nexus.commands.hooks._effective_hooks_dir", return_value=hooks_dir),
    ):
        result = runner.invoke(main, ["index", "repo", str(repo)])

    assert result.exit_code == 0, result.output
    assert "nx hooks install" not in result.output


def test_reminder_suppressed_when_post_rewrite_managed(
    runner: CliRunner, index_home: Path, tmp_path: Path
) -> None:
    """Reminder is suppressed when post-rewrite contains the nexus sentinel."""
    from nexus.commands.hooks import SENTINEL_BEGIN

    repo = _make_repo(index_home)
    hooks_dir = tmp_path / "hooks"
    hooks_dir.mkdir()
    (hooks_dir / "post-rewrite").write_text(f"#!/bin/sh\n{SENTINEL_BEGIN}\nnx index repo .\n")

    with (
        patch("nexus.commands.index._registry", return_value=_mock_reg()),
        patch("nexus.indexer.index_repository", return_value={}),
        patch("nexus.commands.hooks._effective_hooks_dir", return_value=hooks_dir),
    ):
        result = runner.invoke(main, ["index", "repo", str(repo)])

    assert result.exit_code == 0, result.output
    assert "nx hooks install" not in result.output


# ── Reminder is NOT shown on --frecency-only runs ─────────────────────────────

def test_reminder_suppressed_on_frecency_only(
    runner: CliRunner, index_home: Path, tmp_path: Path
) -> None:
    """Reminder is NOT printed when --frecency-only is used (content not changed)."""
    repo = _make_repo(index_home)
    hooks_dir = tmp_path / "hooks"
    hooks_dir.mkdir()
    # No hooks installed — would normally show reminder

    with (
        patch("nexus.commands.index._registry", return_value=_mock_reg()),
        patch("nexus.indexer.index_repository", return_value={}),
        patch("nexus.commands.hooks._effective_hooks_dir", return_value=hooks_dir),
    ):
        result = runner.invoke(main, ["index", "repo", str(repo), "--frecency-only"])

    assert result.exit_code == 0, result.output
    assert "nx hooks install" not in result.output


# ── Hook detection errors are silently suppressed ─────────────────────────────

def test_hook_detection_error_does_not_break_indexing(
    runner: CliRunner, index_home: Path
) -> None:
    """If _effective_hooks_dir raises, indexing still completes successfully."""
    import click

    repo = _make_repo(index_home)

    with (
        patch("nexus.commands.index._registry", return_value=_mock_reg()),
        patch("nexus.indexer.index_repository", return_value={}),
        patch(
            "nexus.commands.hooks._effective_hooks_dir",
            side_effect=click.ClickException("Not a git repository"),
        ),
    ):
        result = runner.invoke(main, ["index", "repo", str(repo)])

    assert result.exit_code == 0, result.output
    assert "Done" in result.output


def test_hook_detection_generic_error_does_not_break_indexing(
    runner: CliRunner, index_home: Path
) -> None:
    """If hook detection raises any unexpected exception, indexing still completes."""
    repo = _make_repo(index_home)

    with (
        patch("nexus.commands.index._registry", return_value=_mock_reg()),
        patch("nexus.indexer.index_repository", return_value={}),
        patch(
            "nexus.commands.hooks._effective_hooks_dir",
            side_effect=RuntimeError("unexpected failure"),
        ),
    ):
        result = runner.invoke(main, ["index", "repo", str(repo)])

    assert result.exit_code == 0, result.output
    assert "Done" in result.output


# ── Reminder text exact match ─────────────────────────────────────────────────

def test_reminder_exact_text(runner: CliRunner, index_home: Path, tmp_path: Path) -> None:
    """The reminder contains the exact canonical text."""
    repo = _make_repo(index_home)
    hooks_dir = tmp_path / "hooks"
    hooks_dir.mkdir()

    with (
        patch("nexus.commands.index._registry", return_value=_mock_reg()),
        patch("nexus.indexer.index_repository", return_value={}),
        patch("nexus.commands.hooks._effective_hooks_dir", return_value=hooks_dir),
    ):
        result = runner.invoke(main, ["index", "repo", str(repo)])

    assert result.exit_code == 0, result.output
    assert (
        "Tip: run `nx hooks install` to auto-index this repo on every commit."
        in result.output
    )


# ── _effective_hooks_dir is used (worktree/core.hooksPath aware) ───────────────

def test_effective_hooks_dir_is_called_with_repo_path(
    runner: CliRunner, index_home: Path, tmp_path: Path
) -> None:
    """_effective_hooks_dir is invoked with the resolved repo path."""
    repo = _make_repo(index_home)
    hooks_dir = tmp_path / "hooks"
    hooks_dir.mkdir()
    mock_hdir = MagicMock(return_value=hooks_dir)

    with (
        patch("nexus.commands.index._registry", return_value=_mock_reg()),
        patch("nexus.indexer.index_repository", return_value={}),
        patch("nexus.commands.hooks._effective_hooks_dir", mock_hdir),
    ):
        result = runner.invoke(main, ["index", "repo", str(repo)])

    assert result.exit_code == 0, result.output
    mock_hdir.assert_called_once_with(repo.resolve())
