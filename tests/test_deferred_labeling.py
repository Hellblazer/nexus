# SPDX-License-Identifier: AGPL-3.0-or-later
"""Deferred topic labeling (nexus-qqc1v): the post-index taxonomy chain
spawns a DETACHED ``nx taxonomy label`` process instead of labeling
inline — 81.2s of Claude-haiku cosmetics measured off the indexing wall
(2026-07-04 attrib run). Labels appear minutes later on their own; the
CLI exits without waiting.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

from nexus.commands.index import _spawn_deferred_labeling
from nexus.util.process_group import isolation_popen_kwargs


class TestSpawnDeferredLabeling:
    def test_log_dir_honors_nexus_config_dir_not_real_home(
        self, monkeypatch, tmp_path
    ) -> None:
        """nexus-pfuns: ``log_dir`` used to be a hardcoded ``Path.home() /
        ".config" / "nexus" / "logs"``, blind to NEXUS_CONFIG_DIR entirely
        -- the test below's own ``monkeypatch.setenv("NEXUS_CONFIG_DIR",
        ...)`` was a silent no-op against it, so every run appended real
        bytes to Sam's actual
        ~/.config/nexus/logs/deferred_labeling.log."""
        real_log = Path.home() / ".config" / "nexus" / "logs" / "deferred_labeling.log"
        real_stat_before = real_log.stat() if real_log.exists() else None

        def fake_popen(cmd, **kw):
            class P:
                pid = 4242
            return P()

        monkeypatch.setattr(subprocess, "Popen", fake_popen)
        monkeypatch.setenv("NEXUS_CONFIG_DIR", str(tmp_path))
        assert _spawn_deferred_labeling() is True

        isolated_log = tmp_path / "logs" / "deferred_labeling.log"
        assert isolated_log.exists()
        if real_stat_before is not None:
            real_stat_after = real_log.stat()
            assert real_stat_after.st_mtime == real_stat_before.st_mtime
            assert real_stat_after.st_size == real_stat_before.st_size

    def test_spawns_detached_nx_taxonomy_label(self, monkeypatch, tmp_path) -> None:
        calls: list[dict] = []

        def fake_popen(cmd, **kw):
            calls.append({"cmd": cmd, **kw})
            class P:  # noqa: D401
                pid = 4242
            return P()

        monkeypatch.setattr(subprocess, "Popen", fake_popen)
        monkeypatch.setenv("NEXUS_CONFIG_DIR", str(tmp_path))
        ok = _spawn_deferred_labeling()
        assert ok is True
        assert len(calls) == 1
        cmd = calls[0]["cmd"]
        # runs the module entry with the same interpreter — survives
        # PATH-less environments (launchd lesson, nexus-n8sbw)
        assert cmd[1:] == ["-m", "nexus.cli", "taxonomy", "label"]
        # detached: own session on POSIX, own process group on Windows (where
        # ``nx uninstall`` stops this run with a CTRL_BREAK that only reaches a
        # group leader), no inherited stdio pipes back to us
        expected = isolation_popen_kwargs()
        assert expected, "the isolation kwargs are never empty"
        assert {k: calls[0].get(k) for k in expected} == expected
        assert calls[0]["stdin"] == subprocess.DEVNULL
        # nexus-7xzc1: the pid is recorded where `nx uninstall` stops the run
        from nexus.daemon.installer import DEFERRED_LABELING_PID_NAME

        assert (tmp_path / DEFERRED_LABELING_PID_NAME).read_text().strip() == "4242"

    def test_the_run_starts_in_the_stable_cwd_and_is_handed_the_callers_directory(
        self, monkeypatch, tmp_path
    ) -> None:
        """nexus-x3m8e: the run outlives the session whose worktree launched it,
        so it must not inherit that directory (a deleted cwd breaks every
        getcwd() in it); but ``nx taxonomy label`` reads the per-repo
        ``.nexus.yml`` through ``load_config()``, so the caller's directory
        travels as NEXUS_REPO_ROOT, which ``load_config`` reads in place of the cwd."""
        import os

        calls: list[dict] = []

        def fake_popen(cmd, **kw):
            calls.append(kw)

            class P:
                pid = 4242

            return P()

        launch = tmp_path / "session-worktree"
        launch.mkdir()
        monkeypatch.chdir(launch)
        monkeypatch.delenv("NEXUS_REPO_ROOT", raising=False)
        monkeypatch.setattr(subprocess, "Popen", fake_popen)
        monkeypatch.setenv("NEXUS_CONFIG_DIR", str(tmp_path / "cfg"))
        assert _spawn_deferred_labeling() is True
        assert calls[0]["cwd"] == os.path.abspath(os.sep)
        assert calls[0]["env"]["NEXUS_REPO_ROOT"] == os.getcwd()
        assert calls[0]["start_new_session"] is True or "creationflags" in calls[0]

    def test_spawn_failure_returns_false_never_raises(self, monkeypatch) -> None:
        def boom(cmd, **kw):
            raise OSError("no fork for you")

        monkeypatch.setattr(subprocess, "Popen", boom)
        assert _spawn_deferred_labeling() is False
