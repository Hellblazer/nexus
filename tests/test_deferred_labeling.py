# SPDX-License-Identifier: AGPL-3.0-or-later
"""Deferred topic labeling (nexus-qqc1v): the post-index taxonomy chain
spawns a DETACHED ``nx taxonomy label`` process instead of labeling
inline — 81.2s of Claude-haiku cosmetics measured off the indexing wall
(2026-07-04 attrib run). Labels appear minutes later on their own; the
CLI exits without waiting.
"""

from __future__ import annotations

import subprocess

from nexus.commands.index import _spawn_deferred_labeling


class TestSpawnDeferredLabeling:
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
        # detached: new session, no inherited stdio pipes back to us
        assert calls[0]["start_new_session"] is True
        assert calls[0]["stdin"] == subprocess.DEVNULL

    def test_spawn_failure_returns_false_never_raises(self, monkeypatch) -> None:
        def boom(cmd, **kw):
            raise OSError("no fork for you")

        monkeypatch.setattr(subprocess, "Popen", boom)
        assert _spawn_deferred_labeling() is False
