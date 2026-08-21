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
        monkeypatch.setenv("NEXUS_CONFIG_DIR", str(cfg))
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


# ── doctor dead-owner rendering (nexus-7kl32 / nexus-9t86i) ────────────────


class TestDoctorDeadOwnerRendering:
    """nexus-7kl32: a registered repo owner whose root no longer exists on
    disk must never render as a green ``ok=True`` 'could not check' — that
    is exactly the self-admitted-vacuity class nexus-9t86i named (a check
    that could not read state must never render ✓). Evidence:
    shakedown-2026-08-04/S3-doctor.json — 24 of 25 signal-free greens were
    this exact line shape."""

    def _seed_registry(self, monkeypatch, tmp_path, repo_path, *, cfg_name="nx_config_dead_owner"):
        """RepoRegistry expects ``{repos: {<path>: {...}}}`` JSON shape."""
        import json
        cfg = tmp_path / cfg_name
        cfg.mkdir(exist_ok=True)
        monkeypatch.setenv("NEXUS_CONFIG_DIR", str(cfg))
        registry_path = cfg / "repos.json"
        registry_path.write_text(json.dumps({"repos": {str(repo_path): {}}}))

    def test_vanished_owner_root_is_not_rendered_ok(self, tmp_path, monkeypatch):
        # Deliberately never created — mirrors the bench-index sandbox /
        # probe-checkout population that produced the S3 evidence. No
        # catalog is seeded, so this exercises the legacy repos.json-only
        # attribution branch (the catalog-owned branch has its own test
        # below, per code-review finding 3 — population mismatch).
        dead_repo = tmp_path / "bench-index-XXXXXX.deadbeef" / "benchidx-w2"
        self._seed_registry(monkeypatch, tmp_path, dead_repo)

        from nexus.health import _check_git_hooks
        results = _check_git_hooks()

        hooks_results = [r for r in results if r.label == "git hooks"]
        assert hooks_results, "expected a git-hooks result for the registered dead owner"
        r = hooks_results[0]
        assert r.ok is False, (
            f"dead owner root rendered ok=True — the exact nexus-9t86i "
            f"vacuity class (detail={r.detail!r})"
        )
        assert r.warn is True, "dead-owner signal must be a soft warning, not a hard failure"
        assert r.fatal is False, "a dead owner must not flip nx doctor's exit code"
        assert "no longer exist" in r.detail

    def test_vanished_legacy_only_owner_suggests_removing_from_registry(
        self, tmp_path, monkeypatch
    ):
        """code-review finding 3 (population mismatch): a dead owner whose
        ONLY registration is the legacy repos.json file is not visible to
        `nx catalog owners --census` (catalog owners only) — pointing at
        that verb here would relocate the exact misleading-rendering bug
        this bead exists to eliminate. The detail must say so, and the
        actual remedy (editing repos.json) differs from the catalog case."""
        dead_repo = tmp_path / "probe-rich-deadbeef" / "rich"
        self._seed_registry(monkeypatch, tmp_path, dead_repo)

        from nexus.health import _check_git_hooks
        results = _check_git_hooks()
        r = next(x for x in results if x.label == "git hooks")
        assert "not covered by" in r.detail
        assert "legacy repos.json" in r.detail
        assert any("repos.json" in s for s in r.fix_suggestions), r.fix_suggestions
        assert not any("census" in s for s in r.fix_suggestions), (
            "a legacy-only dead owner must not be told to run a verb that "
            f"cannot see it: {r.fix_suggestions}"
        )

    def test_vanished_catalog_owner_fix_suggestion_points_at_census(
        self, tmp_path, monkeypatch
    ):
        """The catalog-owned counterpart of the legacy-only test above:
        when the dead owner IS a catalog row, `nx catalog owners --census`
        genuinely covers it (same list_repos_dual_with_catalog_roots round
        trip this check uses for attribution), so the suggestion should say
        so. nexus-cw262: the mutation arm now exists (--execute deactivate)
        and is named UNCONDITIONALLY only when the connected engine is
        confirmed capable — this fake carries a ``deactivated_at`` key
        (round-3 critique T2 21467 Significant-2's capability signal),
        simulating a cw262-deployed engine. The pre-cw262-engine case (no
        such key) is covered separately below."""
        dead_repo = tmp_path / "bench-index-XXXXXX.catalog-owned" / "w2"
        self._seed_registry(monkeypatch, tmp_path, dead_repo)

        class _FakeCatalogReader:
            def list_owners_by_type(self, owner_type):
                if owner_type != "repo":
                    return []
                return [{
                    "tumbler_prefix": "1.99", "repo_root": str(dead_repo),
                    "deactivated_at": None,
                }]

        monkeypatch.setattr(
            "nexus.catalog.factory.make_catalog_reader",
            lambda: _FakeCatalogReader(),
        )

        from nexus.health import _check_git_hooks
        results = _check_git_hooks()
        r = next(x for x in results if x.label == "git hooks")
        assert "legacy repos.json" not in r.detail
        assert any(
            "owners" in s and "census" in s for s in r.fix_suggestions
        ), r.fix_suggestions
        assert any(
            "--execute deactivate --no-dry-run --confirm" in s
            and "nexus-cw262" in s
            for s in r.fix_suggestions
        ), (
            f"a CONFIRMED-capable engine must name the mutation arm "
            f"unconditionally: {r.fix_suggestions}"
        )

    def test_vanished_catalog_owner_engine_predates_route_gives_honest_wording(
        self, tmp_path, monkeypatch
    ):
        """nexus-cw262 round-3 critique (T2 21467 Significant-2): the live
        cloud engine at authorship time genuinely predates the
        owner-deactivate route. A fake owner dict with NO ``deactivated_at``
        key (the wire shape a pre-cw262 engine actually returns) must make
        the fix_suggestion say the route requires an engine upgrade — never
        claim --execute deactivate works unconditionally against an engine
        that cannot serve it."""
        dead_repo = tmp_path / "bench-index-XXXXXX.pre-cw262-engine" / "w2"
        self._seed_registry(monkeypatch, tmp_path, dead_repo)

        class _FakeCatalogReaderNoCapability:
            def list_owners_by_type(self, owner_type):
                if owner_type != "repo":
                    return []
                # No "deactivated_at" key at all -- the pre-cw262 wire shape.
                return [{"tumbler_prefix": "1.99", "repo_root": str(dead_repo)}]

        monkeypatch.setattr(
            "nexus.catalog.factory.make_catalog_reader",
            lambda: _FakeCatalogReaderNoCapability(),
        )

        from nexus.health import _check_git_hooks
        results = _check_git_hooks()
        r = next(x for x in results if x.label == "git hooks")
        assert any(
            "requires an engine build" in s and "not yet deployed" in s
            for s in r.fix_suggestions
        ), (
            f"an engine confirmed NOT to carry the route must get honest "
            f"wording, not a claim the mutation arm works: {r.fix_suggestions}"
        )
        assert not any(
            "--execute deactivate --no-dry-run --confirm" in s
            for s in r.fix_suggestions
        ), (
            f"must never claim the destructive command works against an "
            f"engine confirmed to lack the route: {r.fix_suggestions}"
        )

    def test_exists_probe_raising_os_error_degrades_instead_of_crashing(
        self, tmp_path, monkeypatch
    ):
        """code-review IMPORTANT: repo_path.exists() itself can raise (e.g.
        a permission-denied path component). Before this fix that would
        propagate out of _check_git_hooks uncaught — a brand-new crash
        surface introduced by the very fix meant to make doctor more
        honest. Must degrade to the generic could-not-check branch instead."""
        present_repo = tmp_path / "locked-parent" / "repo"
        self._seed_registry(monkeypatch, tmp_path, present_repo)

        from pathlib import Path as _Path
        real_exists = _Path.exists

        def _raise(self):
            if self == present_repo:
                raise PermissionError("simulated permission denied")
            return real_exists(self)

        # The probe itself (effective_hooks_dir) must also fail so we reach
        # the except-branch under test; anything raises here.
        def _boom(*a, **kw):
            raise RuntimeError("simulated probe failure")

        monkeypatch.setattr("nexus._git_hooks_meta.effective_hooks_dir", _boom)
        monkeypatch.setattr(_Path, "exists", _raise)

        from nexus.health import _check_git_hooks
        results = _check_git_hooks()  # must not raise
        r = next(x for x in results if x.label == "git hooks")
        assert r.ok is False
        assert r.warn is True
        assert "could not check" in r.detail
        assert "simulated probe failure" in r.detail

    def test_doctor_exit_code_unaffected_by_dead_owner(self, tmp_path, monkeypatch):
        """RDR-129 B4: a soft warning never flips nx doctor's failed flag —
        the bead is explicit that exit semantics for healthy owners (and,
        by the same non-fatal contract, for dead ones) must not change."""
        dead_repo = tmp_path / "u8n4r_probe2" / "edgeD" / "primary"
        self._seed_registry(monkeypatch, tmp_path, dead_repo)

        from nexus.health import _check_git_hooks, format_health_for_cli
        results = _check_git_hooks()
        _, failed = format_health_for_cli(results, local_mode=True)
        assert failed is False

    def test_other_probe_failure_also_downgraded_honestly(self, tmp_path, monkeypatch):
        """A probe failure that is NOT a vanished root (e.g. a permission
        error, or any other exception surfaced by the git subprocess calls)
        must still degrade to an honest signal, not silently to ok=True —
        the fix is a rendering-policy change, not a special case scoped
        only to the vanished-path branch."""
        present_repo = tmp_path / "present-but-broken"
        present_repo.mkdir()
        self._seed_registry(monkeypatch, tmp_path, present_repo)

        def _raise(*a, **kw):
            raise RuntimeError("simulated non-path probe failure")

        monkeypatch.setattr("nexus._git_hooks_meta.effective_hooks_dir", _raise)

        from nexus.health import _check_git_hooks
        results = _check_git_hooks()
        r = next(x for x in results if x.label == "git hooks")
        assert r.ok is False
        assert r.warn is True
        assert "could not check" in r.detail
        assert "simulated non-path probe failure" in r.detail

    def test_healthy_owner_still_renders_ok(self, runner, fake_repo, monkeypatch, tmp_path):
        """Kill control: a real, still-installed repo must be untouched by
        this fix — the honest-rendering change must not regress the happy
        path nx doctor exercises on every healthy owner."""
        _install(runner, fake_repo)
        self._seed_registry(monkeypatch, tmp_path, fake_repo, cfg_name="nx_config_healthy_owner")

        with _mock_git(fake_repo):
            from nexus.health import _check_git_hooks
            results = _check_git_hooks()
        r = next(x for x in results if x.label == "git hooks")
        assert r.ok is True
        assert "could not check" not in r.detail

    def test_catalog_owner_attribution_reuses_the_walk_round_trip(self, tmp_path, monkeypatch):
        """nexus-cw262 round-2 critique (T2 21456 moderate finding): the
        attribution set (catalog_repo_roots) used to come from an
        INDEPENDENT second cat.list_owners_by_type("repo") call, separate
        from the one list_repos_dual already made to build the walk list —
        a transient failure of just the second call silently misattributed
        a genuinely catalog-owned dead owner as a legacy repos.json entry.
        Fixed by list_repos_dual_with_catalog_roots: ONE round trip serves
        both. This pins the non-regression: exactly one call, not two."""
        dead_repo = tmp_path / "bench-index-XXXXXX.single-trip" / "w2"
        self._seed_registry(monkeypatch, tmp_path, dead_repo)

        calls = {"n": 0}

        class _CountingCatalogReader:
            def list_owners_by_type(self, owner_type):
                calls["n"] += 1
                if owner_type != "repo":
                    return []
                return [{"tumbler_prefix": "1.99", "repo_root": str(dead_repo)}]

        monkeypatch.setattr(
            "nexus.catalog.factory.make_catalog_reader",
            lambda: _CountingCatalogReader(),
        )

        from nexus.health import _check_git_hooks
        results = _check_git_hooks()
        r = next(x for x in results if x.label == "git hooks")

        # Non-vacuity: the attribution still resolves correctly (catalog
        # branch, not legacy) — proves the single call actually served ALL
        # THREE consumers (walk list, attribution set, capability signal —
        # round 3), not that attribution silently degraded to empty.
        # This fake's owner dict carries no "deactivated_at" key, so the
        # capability signal reads "unavailable" and the wording is the
        # honest engine-floor variant (dedicated coverage: test_vanished_
        # catalog_owner_engine_predates_route_gives_honest_wording) — this
        # test only pins the round-trip count and the census mention.
        assert "legacy repos.json" not in r.detail
        joined = " ".join(r.fix_suggestions)
        assert "census" in joined and "nexus-cw262" in joined, r.fix_suggestions
        assert calls["n"] == 1, (
            f"list_owners_by_type called {calls['n']} times — expected exactly 1 "
            "(the walk list and the attribution set must share one round trip)"
        )

    def test_catalog_reader_construction_failure_degrades_without_crashing(
        self, tmp_path, monkeypatch
    ):
        """The cat=None pre-init guard (round-2 critique T2 21456, noted gap:
        no test in the fix round exercised this path). If make_catalog_reader
        itself raises (e.g. StorageModeFlagError for a stranded
        NX_STORAGE_BACKEND_CATALOG=sqlite install), the outer except in
        _check_git_hooks must catch it, degrade repos/catalog_repo_roots to
        empty (registry-only), and never crash `nx doctor` with an
        UnboundLocalError or any other propagated exception."""
        from nexus.db.storage_mode import StorageModeFlagError

        present_repo = tmp_path / "reader-construction-failure"
        present_repo.mkdir()
        self._seed_registry(monkeypatch, tmp_path, present_repo)

        def _boom():
            raise StorageModeFlagError("simulated stranded sqlite catalog flag")

        monkeypatch.setattr("nexus.catalog.factory.make_catalog_reader", _boom)

        from nexus.health import _check_git_hooks
        results = _check_git_hooks()  # must not raise

        # repos degraded to empty (list_repos_dual_with_catalog_roots's own
        # exception path never ran -- make_catalog_reader raised BEFORE it
        # could be called, so the outer except in _check_git_hooks is what
        # catches this, not the inner best-effort try inside repos.py).
        no_repos = [r for r in results if r.label == "git hooks" and "no repos registered" in r.detail]
        assert no_repos, (
            f"expected the 'no repos registered' fallback when catalog-reader "
            f"construction itself raises: {[r.detail for r in results if r.label == 'git hooks']}"
        )
