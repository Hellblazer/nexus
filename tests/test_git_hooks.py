# SPDX-License-Identifier: AGPL-3.0-or-later
import re
import stat
from pathlib import Path
from unittest.mock import patch

import pytest
from click.testing import CliRunner

from nexus.cli import main

HOOK_NAMES = ("post-commit", "post-merge", "post-rewrite")
SENTINEL_BEGIN = "# >>> nexus managed begin >>>"
SENTINEL_END = "# <<< nexus managed end <<<"


@pytest.fixture
def runner() -> CliRunner:
    return CliRunner()


@pytest.fixture
def fake_repo(tmp_path) -> Path:
    repo = tmp_path / "myrepo"
    repo.mkdir()
    (repo / ".git" / "hooks").mkdir(parents=True)
    return repo


def _mock_git(repo: Path, git_common_dir: str | None = None, hooks_path: str | None = None):
    def _run(cmd, *, cwd=None, capture_output=False, text=False, timeout=None, **kw):
        import subprocess as sp
        class Res:
            stdout = ""; stderr = ""; returncode = 0
        r = Res()
        if cmd[:2] == ["git", "rev-parse"] and "--git-common-dir" in cmd:
            r.stdout = git_common_dir or str(repo / ".git")
        elif cmd[:3] == ["git", "config", "core.hooksPath"]:
            if hooks_path: r.stdout = hooks_path
            else: r.returncode = 1
        else:
            return sp.run(cmd, cwd=cwd, capture_output=capture_output, text=text, timeout=timeout)
        return r
    return patch("nexus.commands.hooks.subprocess.run", side_effect=_run)


def _install(runner, repo):
    with _mock_git(repo):
        return runner.invoke(main, ["hooks", "install", str(repo)])


def _hooks_dir(repo):
    return repo / ".git" / "hooks"


# ── install ──────────────────────────────────────────────────────────────────

class TestInstall:
    def test_creates_three_hooks(self, runner, fake_repo):
        result = _install(runner, fake_repo)
        assert result.exit_code == 0
        for name in HOOK_NAMES:
            content = (_hooks_dir(fake_repo) / name).read_text()
            assert SENTINEL_BEGIN in content and "--on-locked=skip" in content

    def test_sets_executable_bit(self, runner, fake_repo):
        _install(runner, fake_repo)
        for name in HOOK_NAMES:
            assert (_hooks_dir(fake_repo) / name).stat().st_mode & stat.S_IXUSR

    def test_output_shows_created(self, runner, fake_repo):
        result = _install(runner, fake_repo)
        assert "created" in result.output

    def test_appends_to_existing(self, runner, fake_repo):
        existing = _hooks_dir(fake_repo) / "post-commit"
        existing.write_text("#!/bin/sh\necho 'existing hook'\n")
        result = _install(runner, fake_repo)
        assert result.exit_code == 0
        content = existing.read_text()
        assert "existing hook" in content and SENTINEL_BEGIN in content
        assert "appended" in result.output

    def test_idempotent(self, runner, fake_repo):
        _install(runner, fake_repo)
        _install(runner, fake_repo)
        for name in HOOK_NAMES:
            assert (_hooks_dir(fake_repo) / name).read_text().count(SENTINEL_BEGIN) == 1

    def test_respects_core_hooks_path(self, runner, fake_repo, tmp_path):
        custom_dir = tmp_path / "custom_hooks"
        custom_dir.mkdir()
        with _mock_git(fake_repo, hooks_path=str(custom_dir)):
            result = runner.invoke(main, ["hooks", "install", str(fake_repo)])
        assert result.exit_code == 0
        for name in HOOK_NAMES:
            assert (custom_dir / name).exists()
        assert not (_hooks_dir(fake_repo) / "post-commit").exists()

    def test_warns_non_writable_hooks_path(self, runner, fake_repo, tmp_path):
        locked = tmp_path / "locked_hooks"
        locked.mkdir()
        locked.chmod(0o555)
        try:
            with _mock_git(fake_repo, hooks_path=str(locked)):
                result = runner.invoke(main, ["hooks", "install", str(fake_repo)])
            assert result.exit_code != 0 or "warning" in result.output.lower() or "not writable" in result.output.lower()
        finally:
            locked.chmod(0o755)

    def test_worktree_uses_main_repo_hooks(self, runner, fake_repo, tmp_path):
        (tmp_path / "worktrees" / "feature").mkdir(parents=True)
        with _mock_git(fake_repo, git_common_dir=str(fake_repo / ".git")):
            result = runner.invoke(main, ["hooks", "install", str(fake_repo)])
        assert result.exit_code == 0 and (_hooks_dir(fake_repo) / "post-commit").exists()


# ── uninstall ────────────────────────────────────────────────────────────────

class TestUninstall:
    def test_removes_owned_hooks(self, runner, fake_repo):
        _install(runner, fake_repo)
        with _mock_git(fake_repo):
            result = runner.invoke(main, ["hooks", "uninstall", str(fake_repo)])
        assert result.exit_code == 0 and "removed" in result.output
        for name in HOOK_NAMES:
            assert not (_hooks_dir(fake_repo) / name).exists()

    def test_preserves_existing_content(self, runner, fake_repo):
        existing = _hooks_dir(fake_repo) / "post-commit"
        existing.write_text("#!/bin/sh\necho 'keep me'\n")
        _install(runner, fake_repo)
        with _mock_git(fake_repo):
            runner.invoke(main, ["hooks", "uninstall", str(fake_repo)])
        content = existing.read_text()
        assert "keep me" in content and SENTINEL_BEGIN not in content

    def test_idempotent(self, runner, fake_repo):
        with _mock_git(fake_repo):
            result = runner.invoke(main, ["hooks", "uninstall", str(fake_repo)])
        assert result.exit_code == 0


# ── status ───────────────────────────────────────────────────────────────────

class TestStatus:
    @pytest.mark.parametrize("setup,expect", [
        ("none", "not installed"),
        ("owned", "owned"),
        ("appended", "appended"),
        ("unmanaged", "unmanaged"),
    ])
    def test_status_states(self, runner, fake_repo, setup, expect):
        hd = _hooks_dir(fake_repo)
        if setup == "owned":
            _install(runner, fake_repo)
        elif setup == "appended":
            (hd / "post-commit").write_text("#!/bin/sh\necho 'pre-existing'\n")
            _install(runner, fake_repo)
        elif setup == "unmanaged":
            (hd / "post-commit").write_text("#!/bin/sh\necho 'third-party'\n")
        with _mock_git(fake_repo):
            result = runner.invoke(main, ["hooks", "status", str(fake_repo)])
        assert result.exit_code == 0 and expect in result.output

    def test_reports_hooks_directory(self, runner, fake_repo):
        with _mock_git(fake_repo):
            result = runner.invoke(main, ["hooks", "status", str(fake_repo)])
        assert ".git" in result.output or "hooks" in result.output

    def test_core_hooks_path_in_output(self, runner, fake_repo, tmp_path):
        custom = tmp_path / "shared_hooks"
        custom.mkdir()
        with _mock_git(fake_repo, hooks_path=str(custom)):
            result = runner.invoke(main, ["hooks", "status", str(fake_repo)])
        assert str(custom) in result.output or "shared_hooks" in result.output


# ── hook script content ──────────────────────────────────────────────────────

class TestHookContent:
    def test_required_elements(self, runner, fake_repo):
        _install(runner, fake_repo)
        content = (_hooks_dir(fake_repo) / "post-commit").read_text()
        for token in ("index.log", "disown", "--on-locked=skip", "nx index repo"):
            assert token in content

    def test_stanza_identical_in_owned_and_appended(self, runner, fake_repo):
        _install(runner, fake_repo)
        hd = _hooks_dir(fake_repo)
        owned = (hd / "post-commit").read_text()
        for name in HOOK_NAMES:
            (hd / name).unlink(missing_ok=True)
        (hd / "post-commit").write_text("#!/bin/sh\necho 'pre'\n")
        _install(runner, fake_repo)
        appended = (hd / "post-commit").read_text()

        def extract(text):
            m = re.search(rf"{re.escape(SENTINEL_BEGIN)}.*?{re.escape(SENTINEL_END)}", text, re.DOTALL)
            return m.group(0) if m else ""

        assert extract(owned) == extract(appended)
