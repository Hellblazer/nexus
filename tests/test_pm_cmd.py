# SPDX-License-Identifier: AGPL-3.0-or-later
"""CLI-layer tests for nx pm subcommands.

Tests the Click wiring in nexus.commands.pm — every subcommand is exercised
through CliRunner against nexus.cli.main, with business-logic functions mocked.
"""
from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest
from click.testing import CliRunner

from nexus.cli import main


@pytest.fixture
def runner() -> CliRunner:
    return CliRunner()


# ── Helpers ──────────────────────────────────────────────────────────────────

def _patch_infer(project: str = "testrepo"):
    """Return a patch context manager that fixes _infer_project to *project*."""
    return patch("nexus.commands.pm._infer_project", return_value=project)


def _patch_db():
    """Return a patch that replaces T2Database with a context-manager mock."""
    mock_db = MagicMock()
    mock_db.__enter__ = MagicMock(return_value=mock_db)
    mock_db.__exit__ = MagicMock(return_value=False)
    return patch("nexus.commands.pm.T2Database", return_value=mock_db), mock_db


def _patch_config(archive_ttl: int = 90):
    """Return a patch for load_config returning a config dict with pm.archiveTtl."""
    return patch(
        "nexus.commands.pm.load_config",
        return_value={"pm": {"archiveTtl": archive_ttl}},
    )


# ── init ─────────────────────────────────────────────────────────────────────


def test_pm_init_default_project(runner: CliRunner) -> None:
    """init with no --project flag infers project from git."""
    db_patch, mock_db = _patch_db()
    with db_patch, _patch_infer("myrepo"), patch("nexus.commands.pm.pm_init") as mock_init:
        result = runner.invoke(main, ["pm", "init"])

    assert result.exit_code == 0, result.output
    mock_init.assert_called_once_with(mock_db, project="myrepo")
    assert "myrepo" in result.output
    assert "4 standard docs" in result.output


def test_pm_init_explicit_project(runner: CliRunner) -> None:
    """init with --project flag uses the provided name, not _infer_project."""
    db_patch, mock_db = _patch_db()
    with db_patch, patch("nexus.commands.pm.pm_init") as mock_init:
        result = runner.invoke(main, ["pm", "init", "--project", "custom-proj"])

    assert result.exit_code == 0, result.output
    mock_init.assert_called_once_with(mock_db, project="custom-proj")
    assert "custom-proj" in result.output


# ── resume ───────────────────────────────────────────────────────────────────


def test_pm_resume_prints_continuation(runner: CliRunner) -> None:
    """resume outputs the continuation content returned by pm_resume."""
    db_patch, mock_db = _patch_db()
    with (
        db_patch,
        _patch_infer("proj"),
        patch("nexus.commands.pm.pm_resume", return_value="# Continuation\nHello world.") as mock_resume,
    ):
        result = runner.invoke(main, ["pm", "resume"])

    assert result.exit_code == 0, result.output
    mock_resume.assert_called_once_with(mock_db, project="proj")
    assert "Hello world." in result.output


def test_pm_resume_no_content_shows_error(runner: CliRunner) -> None:
    """resume when pm_resume returns None raises a ClickException (exit code 1)."""
    db_patch, _ = _patch_db()
    with db_patch, _patch_infer("missing"), patch("nexus.commands.pm.pm_resume", return_value=None):
        result = runner.invoke(main, ["pm", "resume"])

    assert result.exit_code != 0
    assert "missing" in result.output
    assert "nx pm init" in result.output


# ── status ───────────────────────────────────────────────────────────────────


def test_pm_status_shows_phase_and_blockers(runner: CliRunner) -> None:
    """status output includes phase, agent, and blocker lines."""
    status_dict = {
        "phase": 2,
        "agent": "java-developer",
        "blockers": ["waiting on creds", "CI broken"],
    }
    db_patch, mock_db = _patch_db()
    with db_patch, _patch_infer("proj"), patch("nexus.commands.pm.pm_status", return_value=status_dict):
        result = runner.invoke(main, ["pm", "status"])

    assert result.exit_code == 0, result.output
    assert "Phase   : 2" in result.output
    assert "java-developer" in result.output
    assert "Blockers:" in result.output
    assert "1. waiting on creds" in result.output
    assert "2. CI broken" in result.output


def test_pm_status_no_blockers(runner: CliRunner) -> None:
    """status when blockers list is empty shows 'Blockers: none'."""
    status_dict = {
        "phase": 1,
        "agent": None,
        "blockers": [],
    }
    db_patch, _ = _patch_db()
    with db_patch, _patch_infer("proj"), patch("nexus.commands.pm.pm_status", return_value=status_dict):
        result = runner.invoke(main, ["pm", "status"])

    assert result.exit_code == 0, result.output
    assert "Blockers: none" in result.output
    assert "(none)" in result.output  # agent is None


# ── block ────────────────────────────────────────────────────────────────────


def test_pm_block_adds_blocker(runner: CliRunner) -> None:
    """block subcommand passes the blocker text to pm_block."""
    db_patch, mock_db = _patch_db()
    with db_patch, _patch_infer("proj"), patch("nexus.commands.pm.pm_block") as mock_block:
        result = runner.invoke(main, ["pm", "block", "waiting on credentials"])

    assert result.exit_code == 0, result.output
    mock_block.assert_called_once_with(mock_db, project="proj", blocker="waiting on credentials")
    assert "waiting on credentials" in result.output


# ── unblock ──────────────────────────────────────────────────────────────────


def test_pm_unblock_removes_blocker(runner: CliRunner) -> None:
    """unblock subcommand passes the line number to pm_unblock."""
    db_patch, mock_db = _patch_db()
    with db_patch, _patch_infer("proj"), patch("nexus.commands.pm.pm_unblock") as mock_unblock:
        result = runner.invoke(main, ["pm", "unblock", "2"])

    assert result.exit_code == 0, result.output
    mock_unblock.assert_called_once_with(mock_db, project="proj", line=2)
    assert "2" in result.output
    assert "removed" in result.output.lower()


# ── phase next ───────────────────────────────────────────────────────────────


def test_pm_phase_next_advances(runner: CliRunner) -> None:
    """phase next subcommand calls pm_phase_next and reports the new phase."""
    db_patch, mock_db = _patch_db()
    with db_patch, _patch_infer("proj"), patch("nexus.commands.pm.pm_phase_next", return_value=3) as mock_pn:
        result = runner.invoke(main, ["pm", "phase", "next"])

    assert result.exit_code == 0, result.output
    mock_pn.assert_called_once_with(mock_db, project="proj")
    assert "3" in result.output


# ── search ───────────────────────────────────────────────────────────────────


def test_pm_search_with_results(runner: CliRunner) -> None:
    """search displays formatted results when pm_search returns matches."""
    results = [
        {
            "id": 42,
            "project": "myrepo",
            "title": "BLOCKERS.md",
            "timestamp": "2026-02-22T10:00:00",
            "content": "Decided to use ChromaDB for storage.",
        },
    ]
    db_patch, mock_db = _patch_db()
    with db_patch, patch("nexus.commands.pm.pm_search", return_value=results) as mock_search:
        result = runner.invoke(main, ["pm", "search", "ChromaDB"])

    assert result.exit_code == 0, result.output
    mock_search.assert_called_once_with(mock_db, query="ChromaDB", project=None)
    assert "[42]" in result.output
    assert "myrepo/BLOCKERS.md" in result.output
    assert "ChromaDB" in result.output


def test_pm_search_no_results(runner: CliRunner) -> None:
    """search with no results shows 'No results found.' message."""
    db_patch, _ = _patch_db()
    with db_patch, patch("nexus.commands.pm.pm_search", return_value=[]):
        result = runner.invoke(main, ["pm", "search", "nonexistent"])

    assert result.exit_code == 0, result.output
    assert "No results found." in result.output


# ── archive ──────────────────────────────────────────────────────────────────


def test_pm_archive_success(runner: CliRunner) -> None:
    """archive subcommand calls pm_archive and reports success."""
    db_patch, mock_db = _patch_db()
    with db_patch, _patch_infer("proj"), _patch_config(90), patch("nexus.commands.pm.pm_archive") as mock_archive:
        result = runner.invoke(main, ["pm", "archive"])

    assert result.exit_code == 0, result.output
    mock_archive.assert_called_once_with(mock_db, project="proj", status="completed", archive_ttl=90)
    assert "Archived" in result.output
    assert "proj" in result.output


def test_pm_archive_error_shows_click_exception(runner: CliRunner) -> None:
    """archive when RuntimeError is raised shows a ClickException (exit code 1)."""
    db_patch, _ = _patch_db()
    with (
        db_patch,
        _patch_infer("proj"),
        _patch_config(90),
        patch("nexus.commands.pm.pm_archive", side_effect=RuntimeError("API error")),
    ):
        result = runner.invoke(main, ["pm", "archive"])

    assert result.exit_code != 0
    assert "Archive failed" in result.output
    assert "API error" in result.output


def test_pm_archive_value_error_shows_click_exception(runner: CliRunner) -> None:
    """archive when ValueError is raised shows a ClickException (exit code 1)."""
    db_patch, _ = _patch_db()
    with (
        db_patch,
        _patch_infer("proj"),
        _patch_config(90),
        patch("nexus.commands.pm.pm_archive", side_effect=ValueError("No PM docs found")),
    ):
        result = runner.invoke(main, ["pm", "archive"])

    assert result.exit_code != 0
    assert "Archive failed" in result.output
    assert "No PM docs found" in result.output


def test_pm_archive_custom_status(runner: CliRunner) -> None:
    """archive --status paused passes 'paused' to pm_archive."""
    db_patch, mock_db = _patch_db()
    with db_patch, _patch_infer("proj"), _patch_config(90), patch("nexus.commands.pm.pm_archive") as mock_archive:
        result = runner.invoke(main, ["pm", "archive", "--status", "paused"])

    assert result.exit_code == 0, result.output
    mock_archive.assert_called_once_with(mock_db, project="proj", status="paused", archive_ttl=90)


# ── expire ───────────────────────────────────────────────────────────────────


def test_pm_expire(runner: CliRunner) -> None:
    """expire subcommand calls db.expire() and reports the count."""
    db_patch, mock_db = _patch_db()
    mock_db.expire.return_value = 5
    with db_patch:
        result = runner.invoke(main, ["pm", "expire"])

    assert result.exit_code == 0, result.output
    mock_db.expire.assert_called_once()
    assert "5" in result.output
    assert "entries" in result.output


def test_pm_expire_singular(runner: CliRunner) -> None:
    """expire with count=1 uses singular 'entry' instead of 'entries'."""
    db_patch, mock_db = _patch_db()
    mock_db.expire.return_value = 1
    with db_patch:
        result = runner.invoke(main, ["pm", "expire"])

    assert result.exit_code == 0, result.output
    assert "1 entry" in result.output
    # Should not say "1 entries"
    assert "1 entries" not in result.output


# ── promote ──────────────────────────────────────────────────────────────────


def test_pm_promote_success(runner: CliRunner) -> None:
    """promote subcommand calls pm_promote and prints the returned doc ID."""
    db_patch, mock_db = _patch_db()
    mock_t3 = MagicMock()
    with (
        db_patch,
        _patch_infer("proj"),
        patch("nexus.commands.store._t3", return_value=mock_t3),
        patch("nexus.commands.pm.pm_promote", return_value="abc123def456") as mock_promote,
    ):
        result = runner.invoke(main, ["pm", "promote", "METHODOLOGY.md"])

    assert result.exit_code == 0, result.output
    mock_promote.assert_called_once_with(
        db_t2=mock_db,
        db_t3=mock_t3,
        project="proj",
        title="METHODOLOGY.md",
        collection="knowledge__pm__proj",
        ttl_days=0,
    )
    assert "abc123def456" in result.output


def test_pm_promote_key_error(runner: CliRunner) -> None:
    """promote with invalid title raises ClickException (exit code 1)."""
    db_patch, _ = _patch_db()
    with (
        db_patch,
        _patch_infer("proj"),
        patch("nexus.commands.store._t3"),
        patch("nexus.commands.pm.pm_promote", side_effect=KeyError("'MISSING.md' not found")),
    ):
        result = runner.invoke(main, ["pm", "promote", "MISSING.md"])

    assert result.exit_code != 0
    assert "MISSING.md" in result.output


def test_pm_promote_custom_collection_and_ttl(runner: CliRunner) -> None:
    """promote --collection and --ttl are forwarded to pm_promote."""
    db_patch, mock_db = _patch_db()
    mock_t3 = MagicMock()
    with (
        db_patch,
        _patch_infer("proj"),
        patch("nexus.commands.store._t3", return_value=mock_t3),
        patch("nexus.commands.pm.pm_promote", return_value="someid") as mock_promote,
    ):
        result = runner.invoke(
            main,
            ["pm", "promote", "BLOCKERS.md", "--collection", "knowledge__custom", "--ttl", "30"],
        )

    assert result.exit_code == 0, result.output
    mock_promote.assert_called_once_with(
        db_t2=mock_db,
        db_t3=mock_t3,
        project="proj",
        title="BLOCKERS.md",
        collection="knowledge__custom",
        ttl_days=30,
    )


# ── reference ────────────────────────────────────────────────────────────────


def test_pm_reference_with_results(runner: CliRunner) -> None:
    """reference subcommand displays formatted archive results."""
    results = [
        {
            "project": "myrepo",
            "status": "completed",
            "archived_at": "2026-02-20T12:00:00",
            "content": "Summary of important decisions made.",
        },
    ]
    db_patch, mock_db = _patch_db()
    with db_patch, patch("nexus.commands.pm.pm_reference", return_value=results) as mock_ref:
        result = runner.invoke(main, ["pm", "reference", "auth decisions"])

    assert result.exit_code == 0, result.output
    mock_ref.assert_called_once_with(mock_db, query="auth decisions")
    assert "[myrepo]" in result.output
    assert "status=completed" in result.output
    assert "2026-02-20T12:00:00" in result.output
    assert "Summary of important decisions" in result.output


def test_pm_reference_no_results(runner: CliRunner) -> None:
    """reference with no results shows 'No archived syntheses found.'"""
    db_patch, _ = _patch_db()
    with db_patch, patch("nexus.commands.pm.pm_reference", return_value=[]):
        result = runner.invoke(main, ["pm", "reference", "nonexistent topic"])

    assert result.exit_code == 0, result.output
    assert "No archived syntheses found." in result.output


def test_pm_reference_no_query_prompts(runner: CliRunner) -> None:
    """reference with no query argument prompts the user for input."""
    db_patch, mock_db = _patch_db()
    with db_patch, patch("nexus.commands.pm.pm_reference", return_value=[]) as mock_ref:
        result = runner.invoke(main, ["pm", "reference"], input="my search query\n")

    assert result.exit_code == 0, result.output
    mock_ref.assert_called_once_with(mock_db, query="my search query")


# ── restore ──────────────────────────────────────────────────────────────────


def test_pm_restore_success(runner: CliRunner) -> None:
    """restore subcommand calls pm_restore and reports success."""
    db_patch, mock_db = _patch_db()
    with db_patch, patch("nexus.commands.pm.pm_restore") as mock_restore:
        result = runner.invoke(main, ["pm", "restore", "myrepo"])

    assert result.exit_code == 0, result.output
    mock_restore.assert_called_once_with(mock_db, project="myrepo")
    assert "Restored" in result.output
    assert "myrepo" in result.output


def test_pm_restore_error(runner: CliRunner) -> None:
    """restore when RuntimeError is raised shows a ClickException (exit code 1)."""
    db_patch, _ = _patch_db()
    with db_patch, patch("nexus.commands.pm.pm_restore", side_effect=RuntimeError("fully expired")):
        result = runner.invoke(main, ["pm", "restore", "oldrepo"])

    assert result.exit_code != 0
    assert "fully expired" in result.output


# ── close ────────────────────────────────────────────────────────────────────


def test_pm_close_success(runner: CliRunner) -> None:
    """close subcommand delegates to archive with status='completed'."""
    db_patch, mock_db = _patch_db()
    with db_patch, _patch_infer("proj"), _patch_config(90), patch("nexus.commands.pm.pm_archive") as mock_archive:
        result = runner.invoke(main, ["pm", "close"])

    assert result.exit_code == 0, result.output
    mock_archive.assert_called_once_with(mock_db, project="proj", status="completed", archive_ttl=90)
    assert "Archived" in result.output
    assert "proj" in result.output


def test_pm_close_error(runner: CliRunner) -> None:
    """close when RuntimeError is raised shows a ClickException (via archive delegation)."""
    db_patch, _ = _patch_db()
    with (
        db_patch,
        _patch_infer("proj"),
        _patch_config(90),
        patch("nexus.commands.pm.pm_archive", side_effect=RuntimeError("T3 unreachable")),
    ):
        result = runner.invoke(main, ["pm", "close"])

    assert result.exit_code != 0
    assert "Archive failed" in result.output
    assert "T3 unreachable" in result.output


# ── _infer_project ───────────────────────────────────────────────────────────


def test_infer_project_from_git() -> None:
    """_infer_project uses git rev-parse to derive the repo name."""
    from nexus.commands.pm import _infer_project

    mock_result = MagicMock()
    mock_result.stdout = "/home/user/my-awesome-repo\n"

    with patch("subprocess.run", return_value=mock_result) as mock_run:
        name = _infer_project()

    assert name == "my-awesome-repo"
    mock_run.assert_called_once()
    assert mock_run.call_args.args[0] == ["git", "rev-parse", "--show-toplevel"]


def test_infer_project_fallback_to_cwd() -> None:
    """_infer_project falls back to cwd name when git command fails."""
    from pathlib import PurePosixPath

    from nexus.commands.pm import _infer_project

    fake_cwd = PurePosixPath("/tmp/fallback-dir")
    with patch("subprocess.run", side_effect=FileNotFoundError("git not found")):
        with patch("pathlib.Path.cwd", return_value=fake_cwd):
            name = _infer_project()

    assert name == "fallback-dir"
