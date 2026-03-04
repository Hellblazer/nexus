# SPDX-License-Identifier: AGPL-3.0-or-later
"""T2: commands/hooks.py — nx hooks install/uninstall/status command group."""
import os
import stat
from pathlib import Path
from unittest.mock import patch

import pytest
from click.testing import CliRunner

from nexus.cli import main

HOOK_NAMES = ("post-commit", "post-merge", "post-rewrite")
SENTINEL_BEGIN = "# >>> nexus managed begin >>>"
SENTINEL_END = "# <<< nexus managed end <<<"


# ── fixtures ──────────────────────────────────────────────────────────────────


@pytest.fixture
def runner() -> CliRunner:
    return CliRunner()


@pytest.fixture
def fake_repo(tmp_path: Path) -> Path:
    """A fake git repo directory with .git/hooks."""
    repo = tmp_path / "myrepo"
    repo.mkdir()
    hooks_dir = repo / ".git" / "hooks"
    hooks_dir.mkdir(parents=True)
    return repo


def _mock_git(repo: Path, git_common_dir: str | None = None, hooks_path: str | None = None):
    """Return a patcher context that stubs out subprocess calls for git config."""

    def _run(cmd, *, cwd=None, capture_output=False, text=False, timeout=None, **kw):
        import subprocess

        class Res:
            stdout = ""
            stderr = ""
            returncode = 0

        r = Res()
        if cmd[:2] == ["git", "rev-parse"] and "--git-common-dir" in cmd:
            r.stdout = git_common_dir or str(repo / ".git")
        elif cmd[:3] == ["git", "config", "core.hooksPath"]:
            if hooks_path:
                r.stdout = hooks_path
            else:
                r.returncode = 1  # not set
        else:
            # propagate real git calls
            import subprocess as sp

            return sp.run(cmd, cwd=cwd, capture_output=capture_output, text=text, timeout=timeout)
        return r

    return patch("nexus.commands.hooks.subprocess.run", side_effect=_run)


# ── install: fresh repo ───────────────────────────────────────────────────────


def test_install_creates_three_hooks(runner: CliRunner, fake_repo: Path) -> None:
    """nx hooks install creates post-commit, post-merge, post-rewrite."""
    with _mock_git(fake_repo):
        result = runner.invoke(main, ["hooks", "install", str(fake_repo)])

    assert result.exit_code == 0, result.output
    hooks_dir = fake_repo / ".git" / "hooks"
    for name in HOOK_NAMES:
        hf = hooks_dir / name
        assert hf.exists(), f"{name} not created"
        assert SENTINEL_BEGIN in hf.read_text()
        assert SENTINEL_END in hf.read_text()
        assert "--on-locked=skip" in hf.read_text()


def test_install_sets_executable_bit(runner: CliRunner, fake_repo: Path) -> None:
    """Newly created hook files are executable."""
    with _mock_git(fake_repo):
        runner.invoke(main, ["hooks", "install", str(fake_repo)])

    hooks_dir = fake_repo / ".git" / "hooks"
    for name in HOOK_NAMES:
        mode = (hooks_dir / name).stat().st_mode
        assert mode & stat.S_IXUSR, f"{name} not executable"


def test_install_output_shows_created(runner: CliRunner, fake_repo: Path) -> None:
    """Install output shows 'created' for fresh hooks."""
    with _mock_git(fake_repo):
        result = runner.invoke(main, ["hooks", "install", str(fake_repo)])

    assert "created" in result.output
    assert result.exit_code == 0


# ── install: coexistence with existing hooks ─────────────────────────────────


def test_install_appends_to_existing_hook(runner: CliRunner, fake_repo: Path) -> None:
    """install appends nexus stanza without overwriting existing hook content."""
    hooks_dir = fake_repo / ".git" / "hooks"
    existing = hooks_dir / "post-commit"
    existing.write_text("#!/bin/sh\necho 'existing hook'\n")

    with _mock_git(fake_repo):
        result = runner.invoke(main, ["hooks", "install", str(fake_repo)])

    assert result.exit_code == 0
    content = existing.read_text()
    assert "existing hook" in content, "Existing content must be preserved"
    assert SENTINEL_BEGIN in content, "Nexus stanza must be added"
    assert "appended" in result.output


def test_install_idempotent(runner: CliRunner, fake_repo: Path) -> None:
    """Running install twice does not add the stanza twice."""
    with _mock_git(fake_repo):
        runner.invoke(main, ["hooks", "install", str(fake_repo)])
        result = runner.invoke(main, ["hooks", "install", str(fake_repo)])

    assert result.exit_code == 0
    hooks_dir = fake_repo / ".git" / "hooks"
    for name in HOOK_NAMES:
        content = (hooks_dir / name).read_text()
        assert content.count(SENTINEL_BEGIN) == 1, f"{name}: stanza duplicated"


# ── install: core.hooksPath ───────────────────────────────────────────────────


def test_install_respects_core_hooks_path(runner: CliRunner, fake_repo: Path, tmp_path: Path) -> None:
    """install uses core.hooksPath when configured."""
    custom_dir = tmp_path / "custom_hooks"
    custom_dir.mkdir()

    with _mock_git(fake_repo, hooks_path=str(custom_dir)):
        result = runner.invoke(main, ["hooks", "install", str(fake_repo)])

    assert result.exit_code == 0
    for name in HOOK_NAMES:
        assert (custom_dir / name).exists(), f"{name} not in custom_dir"
    # Default .git/hooks should be empty
    assert not (fake_repo / ".git" / "hooks" / "post-commit").exists()


def test_install_warns_on_non_writable_hooks_path(
    runner: CliRunner, fake_repo: Path, tmp_path: Path
) -> None:
    """install warns clearly when core.hooksPath is not writable."""
    locked_dir = tmp_path / "locked_hooks"
    locked_dir.mkdir()
    locked_dir.chmod(0o555)  # read+exec only

    try:
        with _mock_git(fake_repo, hooks_path=str(locked_dir)):
            result = runner.invoke(main, ["hooks", "install", str(fake_repo)])

        assert result.exit_code != 0 or "warning" in result.output.lower() or "not writable" in result.output.lower()
    finally:
        locked_dir.chmod(0o755)  # restore for cleanup


# ── install: worktree ─────────────────────────────────────────────────────────


def test_install_worktree_uses_main_repo_hooks(
    runner: CliRunner, fake_repo: Path, tmp_path: Path
) -> None:
    """Hooks are installed into the main repo's hooks dir, not a worktree gitlink."""
    # Simulate a worktree where git-common-dir points to the main repo
    worktree_dir = tmp_path / "worktrees" / "feature"
    worktree_dir.mkdir(parents=True)

    # git-common-dir returns the main repo's .git path
    main_git = fake_repo / ".git"

    with _mock_git(fake_repo, git_common_dir=str(main_git)):
        result = runner.invoke(main, ["hooks", "install", str(fake_repo)])

    assert result.exit_code == 0
    hooks_dir = fake_repo / ".git" / "hooks"
    assert (hooks_dir / "post-commit").exists()


# ── uninstall ─────────────────────────────────────────────────────────────────


def test_uninstall_removes_owned_hooks(runner: CliRunner, fake_repo: Path) -> None:
    """Uninstall removes nexus-owned hook files entirely."""
    with _mock_git(fake_repo):
        runner.invoke(main, ["hooks", "install", str(fake_repo)])

    with _mock_git(fake_repo):
        result = runner.invoke(main, ["hooks", "uninstall", str(fake_repo)])

    assert result.exit_code == 0
    hooks_dir = fake_repo / ".git" / "hooks"
    for name in HOOK_NAMES:
        assert not (hooks_dir / name).exists(), f"{name} should be deleted"
    assert "removed" in result.output


def test_uninstall_preserves_existing_content(runner: CliRunner, fake_repo: Path) -> None:
    """Uninstall removes only the nexus stanza; other hook content remains."""
    hooks_dir = fake_repo / ".git" / "hooks"
    existing = hooks_dir / "post-commit"
    existing.write_text("#!/bin/sh\necho 'keep me'\n")

    with _mock_git(fake_repo):
        runner.invoke(main, ["hooks", "install", str(fake_repo)])
        result = runner.invoke(main, ["hooks", "uninstall", str(fake_repo)])

    assert result.exit_code == 0
    content = existing.read_text()
    assert "keep me" in content, "Existing content must be preserved"
    assert SENTINEL_BEGIN not in content, "Nexus stanza must be removed"


def test_uninstall_idempotent(runner: CliRunner, fake_repo: Path) -> None:
    """Calling uninstall when no hooks are installed exits cleanly."""
    with _mock_git(fake_repo):
        result = runner.invoke(main, ["hooks", "uninstall", str(fake_repo)])

    assert result.exit_code == 0


# ── status ────────────────────────────────────────────────────────────────────


def test_status_not_installed(runner: CliRunner, fake_repo: Path) -> None:
    """Status shows 'not installed' when hooks are absent."""
    with _mock_git(fake_repo):
        result = runner.invoke(main, ["hooks", "status", str(fake_repo)])

    assert result.exit_code == 0
    assert "not installed" in result.output


def test_status_managed_owned(runner: CliRunner, fake_repo: Path) -> None:
    """Status shows 'owned' for freshly installed (nexus-created) hooks."""
    with _mock_git(fake_repo):
        runner.invoke(main, ["hooks", "install", str(fake_repo)])
        result = runner.invoke(main, ["hooks", "status", str(fake_repo)])

    assert result.exit_code == 0
    assert "owned" in result.output


def test_status_managed_appended(runner: CliRunner, fake_repo: Path) -> None:
    """Status shows 'appended' when nexus stanza was added to existing hook."""
    hooks_dir = fake_repo / ".git" / "hooks"
    (hooks_dir / "post-commit").write_text("#!/bin/sh\necho 'pre-existing'\n")

    with _mock_git(fake_repo):
        runner.invoke(main, ["hooks", "install", str(fake_repo)])
        result = runner.invoke(main, ["hooks", "status", str(fake_repo)])

    assert result.exit_code == 0
    assert "appended" in result.output


def test_status_unmanaged(runner: CliRunner, fake_repo: Path) -> None:
    """Status shows 'unmanaged' for hook files without the nexus sentinel."""
    hooks_dir = fake_repo / ".git" / "hooks"
    (hooks_dir / "post-commit").write_text("#!/bin/sh\necho 'third-party'\n")

    with _mock_git(fake_repo):
        result = runner.invoke(main, ["hooks", "status", str(fake_repo)])

    assert result.exit_code == 0
    assert "unmanaged" in result.output


def test_status_reports_hooks_directory(runner: CliRunner, fake_repo: Path) -> None:
    """Status output includes the resolved hooks directory path."""
    with _mock_git(fake_repo):
        result = runner.invoke(main, ["hooks", "status", str(fake_repo)])

    assert result.exit_code == 0
    # hooks dir path should be in output
    assert ".git" in result.output or "hooks" in result.output


def test_status_core_hooks_path(runner: CliRunner, fake_repo: Path, tmp_path: Path) -> None:
    """Status reports the correct path when core.hooksPath is set."""
    custom_dir = tmp_path / "shared_hooks"
    custom_dir.mkdir()

    with _mock_git(fake_repo, hooks_path=str(custom_dir)):
        result = runner.invoke(main, ["hooks", "status", str(fake_repo)])

    assert result.exit_code == 0
    assert str(custom_dir) in result.output or "shared_hooks" in result.output


# ── hook script content ───────────────────────────────────────────────────────


def test_hook_script_contains_required_elements(runner: CliRunner, fake_repo: Path) -> None:
    """Hook script includes log redirect, disown, and --on-locked=skip."""
    with _mock_git(fake_repo):
        runner.invoke(main, ["hooks", "install", str(fake_repo)])

    hook = fake_repo / ".git" / "hooks" / "post-commit"
    content = hook.read_text()
    assert "index.log" in content
    assert "disown" in content
    assert "--on-locked=skip" in content
    assert "nx index repo" in content


def test_hook_stanza_is_identical_in_owned_and_appended(runner: CliRunner, fake_repo: Path) -> None:
    """The nexus stanza is the same whether the hook is created fresh or appended."""
    hooks_dir = fake_repo / ".git" / "hooks"

    # Fresh install
    with _mock_git(fake_repo):
        runner.invoke(main, ["hooks", "install", str(fake_repo)])
    owned_content = (hooks_dir / "post-commit").read_text()

    # Remove and install with existing file
    for name in HOOK_NAMES:
        (hooks_dir / name).unlink(missing_ok=True)
    (hooks_dir / "post-commit").write_text("#!/bin/sh\necho 'pre'\n")

    with _mock_git(fake_repo):
        runner.invoke(main, ["hooks", "install", str(fake_repo)])
    appended_content = (hooks_dir / "post-commit").read_text()

    # Extract stanza from each
    def extract_stanza(text: str) -> str:
        import re

        m = re.search(
            rf"{re.escape(SENTINEL_BEGIN)}.*?{re.escape(SENTINEL_END)}",
            text,
            re.DOTALL,
        )
        return m.group(0) if m else ""

    assert extract_stanza(owned_content) == extract_stanza(appended_content)
