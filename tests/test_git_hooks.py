# SPDX-License-Identifier: AGPL-3.0-or-later
import re
import stat
from pathlib import Path
from unittest.mock import patch

import click
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
    # nexus-8g79.10 (V2): subprocess.run call sites are inside
    # nexus._git_hooks_meta (git_common_dir + effective_hooks_dir);
    # commands/hooks.py uses them via re-export. Patch the lower-
    # layer module that actually owns the call.
    return patch("nexus._git_hooks_meta.subprocess.run", side_effect=_run)


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
        # pgrep guard added 2026-05-23 (nexus-mkj6u): belt-and-suspenders
        # with --on-locked=skip; catches the multi-commit pile-up race
        # that flock-based locking lost on the open()+truncate+flock window.
        for token in (
            "index.log", "disown", "--on-locked=skip", "nx index repo",
            "pgrep -f", "exit 0",
        ):
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


# ── hook update (nexus-mkj6u shakeout) ────────────────────────────────────────


class TestUpdate:
    """``nx hooks update`` refreshes nexus-managed stanzas to the current
    template — for users whose existing post-commit stanza pre-dates a
    fix like the 2026-05-23 pgrep guard."""

    def _write_legacy_stanza(self, hook_file):
        """Simulate a pre-pgrep-guard stanza on disk."""
        legacy = (
            f"#!/bin/sh\n"
            f"{SENTINEL_BEGIN}\n"
            'nx index repo "$(git rev-parse --show-toplevel)" --on-locked=skip \\\n'
            '  >> "$HOME/.config/nexus/index.log" 2>&1 &\n'
            "disown\n"
            f"{SENTINEL_END}\n"
        )
        hook_file.write_text(legacy)

    def test_refreshes_legacy_stanza_to_current(self, runner, fake_repo):
        from nexus.cli import main
        hd = _hooks_dir(fake_repo)
        hd.mkdir(parents=True, exist_ok=True)
        legacy_file = hd / "post-commit"
        self._write_legacy_stanza(legacy_file)
        assert "pgrep" not in legacy_file.read_text()

        with _mock_git(fake_repo):
            result = runner.invoke(main, ["hooks", "update", str(fake_repo)])
        assert result.exit_code == 0, result.output
        new_content = legacy_file.read_text()
        assert "pgrep -f" in new_content
        assert "exit 0" in new_content
        # Single sentinel block (no duplication).
        assert new_content.count(SENTINEL_BEGIN) == 1

    def test_skips_unmanaged_hook_files(self, runner, fake_repo):
        from nexus.cli import main
        hd = _hooks_dir(fake_repo)
        hd.mkdir(parents=True, exist_ok=True)
        unmanaged = hd / "post-commit"
        unmanaged.write_text("#!/bin/sh\necho hi\n")  # no SENTINEL

        with _mock_git(fake_repo):
            result = runner.invoke(main, ["hooks", "update", str(fake_repo)])
        assert result.exit_code == 0
        assert unmanaged.read_text() == "#!/bin/sh\necho hi\n"
        assert "unmanaged" in result.output

    def test_skips_not_installed(self, runner, fake_repo):
        from nexus.cli import main
        # No hook files at all
        with _mock_git(fake_repo):
            result = runner.invoke(main, ["hooks", "update", str(fake_repo)])
        assert result.exit_code == 0
        assert "not installed" in result.output

    def test_preserves_appended_content(self, runner, fake_repo):
        from nexus.cli import main
        hd = _hooks_dir(fake_repo)
        hd.mkdir(parents=True, exist_ok=True)
        legacy_file = hd / "post-commit"
        legacy_stanza = (
            f"{SENTINEL_BEGIN}\n"
            'nx index repo "$(git rev-parse --show-toplevel)" --on-locked=skip \\\n'
            '  >> "$HOME/.config/nexus/index.log" 2>&1 &\n'
            "disown\n"
            f"{SENTINEL_END}\n"
        )
        appended = "#!/bin/sh\necho 'pre-existing user logic'\n" + legacy_stanza
        legacy_file.write_text(appended)

        with _mock_git(fake_repo):
            result = runner.invoke(main, ["hooks", "update", str(fake_repo)])
        assert result.exit_code == 0
        new_content = legacy_file.read_text()
        # User logic preserved
        assert "echo 'pre-existing user logic'" in new_content
        # New stanza body present
        assert "pgrep -f" in new_content
        # Single sentinel block
        assert new_content.count(SENTINEL_BEGIN) == 1


class TestUpdateAll:
    """``nx hooks update-all`` (and the ``nx upgrade`` hook) refreshes every
    nexus-managed hook across all registered repos in one sweep."""

    def _legacy_stanza_file(self, hook_file: Path) -> None:
        hook_file.write_text(
            f"#!/bin/sh\n{SENTINEL_BEGIN}\n"
            'nx index repo "$(git rev-parse --show-toplevel)" --on-locked=skip \\\n'
            '  >> "$HOME/.config/nexus/index.log" 2>&1 &\n'
            f"disown\n{SENTINEL_END}\n"
        )

    def _make_repo(self, tmp_path: Path, name: str) -> Path:
        repo = tmp_path / name
        (repo / ".git" / "hooks").mkdir(parents=True)
        return repo

    def test_refreshes_all_managed_repos(self, runner, tmp_path, monkeypatch):
        from nexus.cli import main

        repo_a = self._make_repo(tmp_path, "repo_a")
        repo_b = self._make_repo(tmp_path, "repo_b")
        for repo in (repo_a, repo_b):
            self._legacy_stanza_file(repo / ".git" / "hooks" / "post-commit")

        monkeypatch.setattr(
            "nexus.commands.hooks._iter_managed_repo_roots",
            lambda: [repo_a, repo_b],
        )
        monkeypatch.setattr(
            "nexus.commands.hooks._effective_hooks_dir",
            lambda repo: repo / ".git" / "hooks",
        )

        result = runner.invoke(main, ["hooks", "update-all"])
        assert result.exit_code == 0, result.output
        for repo in (repo_a, repo_b):
            content = (repo / ".git" / "hooks" / "post-commit").read_text()
            assert "pgrep -f" in content
            assert content.count(SENTINEL_BEGIN) == 1
        assert "2 repo(s)" in result.output

    def test_skips_unmanaged_and_absent(self, runner, tmp_path, monkeypatch):
        from nexus.cli import main

        managed = self._make_repo(tmp_path, "managed")
        unmanaged = self._make_repo(tmp_path, "unmanaged")
        self._legacy_stanza_file(managed / ".git" / "hooks" / "post-commit")
        (unmanaged / ".git" / "hooks" / "post-commit").write_text(
            "#!/bin/sh\necho hi\n"
        )

        monkeypatch.setattr(
            "nexus.commands.hooks._iter_managed_repo_roots",
            lambda: [managed, unmanaged],
        )
        monkeypatch.setattr(
            "nexus.commands.hooks._effective_hooks_dir",
            lambda repo: repo / ".git" / "hooks",
        )

        result = runner.invoke(main, ["hooks", "update-all"])
        assert result.exit_code == 0, result.output
        # Unmanaged file untouched.
        assert (
            unmanaged / ".git" / "hooks" / "post-commit"
        ).read_text() == "#!/bin/sh\necho hi\n"
        assert "1 repo(s)" in result.output

    def test_one_bad_repo_does_not_abort_sweep(self, runner, tmp_path, monkeypatch):
        from nexus.cli import main

        good = self._make_repo(tmp_path, "good")
        bad = tmp_path / "bad"  # no .git → effective_hooks_dir raises
        bad.mkdir()
        self._legacy_stanza_file(good / ".git" / "hooks" / "post-commit")

        def _hooks_dir(repo: Path):
            if repo == bad:
                raise click.ClickException("not a git repo")
            return repo / ".git" / "hooks"

        monkeypatch.setattr(
            "nexus.commands.hooks._iter_managed_repo_roots",
            lambda: [bad, good],
        )
        monkeypatch.setattr(
            "nexus.commands.hooks._effective_hooks_dir", _hooks_dir
        )

        result = runner.invoke(main, ["hooks", "update-all"])
        assert result.exit_code == 0, result.output
        # Good repo still refreshed despite bad repo earlier in the list.
        assert "pgrep -f" in (
            good / ".git" / "hooks" / "post-commit"
        ).read_text()
        assert "1 repo(s) skipped" in result.output

    def test_no_managed_hooks_anywhere(self, runner, monkeypatch):
        from nexus.cli import main

        monkeypatch.setattr(
            "nexus.commands.hooks._iter_managed_repo_roots", lambda: []
        )
        result = runner.invoke(main, ["hooks", "update-all"])
        assert result.exit_code == 0, result.output
        assert "No nexus-managed hooks" in result.output


# ── doctor stanza-drift check ──────────────────────────────────────────────────


class TestDoctorStanzaDrift:
    """nexus-mkj6u: nx doctor surfaces drift between installed stanza and
    current template, with a fix suggestion pointing at ``nx hooks update``."""

    def _seed_registry(self, monkeypatch, tmp_path, repo):
        """RepoRegistry expects ``{repos: {<path>: {...}}}`` JSON shape."""
        import json
        cfg = tmp_path / "nx_config_drift"
        cfg.mkdir(exist_ok=True)
        monkeypatch.setattr("nexus.config.nexus_config_dir", lambda: cfg)
        registry_path = cfg / "repos.json"
        registry_path.write_text(json.dumps({"repos": {str(repo): {}}}))

    def test_drift_detected_when_legacy_stanza_installed(self, fake_repo, monkeypatch, tmp_path):
        self._seed_registry(monkeypatch, tmp_path, fake_repo)

        # Write a legacy stanza (no pgrep) to fake_repo's post-commit
        hd = _hooks_dir(fake_repo)
        hd.mkdir(parents=True, exist_ok=True)
        (hd / "post-commit").write_text(
            f"#!/bin/sh\n{SENTINEL_BEGIN}\n"
            'nx index repo "$(git rev-parse --show-toplevel)" --on-locked=skip \\\n'
            '  >> "$HOME/.config/nexus/index.log" 2>&1 &\n'
            "disown\n"
            f"{SENTINEL_END}\n"
        )

        with _mock_git(fake_repo):
            from nexus.health import _check_git_hooks
            results = _check_git_hooks()
        drift = [r for r in results if "stanza drift" in r.label.lower()]
        assert drift, f"expected a stanza-drift warning, got: {[r.label for r in results]}"
        r = drift[0]
        assert r.ok is False
        assert any("nx hooks update" in s for s in r.fix_suggestions)

    def test_no_drift_when_stanza_matches_template(self, runner, fake_repo, monkeypatch, tmp_path):
        # Install fresh hooks (matches current template by definition)
        _install(runner, fake_repo)
        self._seed_registry(monkeypatch, tmp_path, fake_repo)

        with _mock_git(fake_repo):
            from nexus.health import _check_git_hooks
            results = _check_git_hooks()
        drift = [r for r in results if "stanza drift" in r.label.lower()]
        assert drift == [], (
            f"expected no drift warning (fresh install matches template), "
            f"got: {[(r.label, r.detail) for r in drift]}"
        )
