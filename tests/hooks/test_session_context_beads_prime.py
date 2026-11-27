# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""nexus-dg7i6.4: ``bd prime`` is re-hosted when the beads plugin is off.

The beads Claude Code plugin's one load-bearing contribution to a beads repo
is its SessionStart/PreCompact ``bd prime`` hook (repo ``.beads/PRIME.md``
plus beads' own memories). nexus disables the plugin per project to take its
28 skill-listing entries out of the model's view (T3 1.11.758), so
``session_context`` renders ``bd prime`` itself, but only when the plugin is
effectively disabled: a user who keeps the plugin must not get PRIME twice.

Since the SessionStart progressive-disclosure change the ready list is on demand (the ``beads`` topic of ``nx brief``,
:func:`nexus.hooks.session_context._render_beads`), but the primer stays at session start where the plugin is off: it is that
project's only primer (marginalia's gate, S2b), printed by ``session_context.run`` through ``_render_bd_prime``.
"""
from __future__ import annotations

import json
import types
from pathlib import Path

import pytest

from nexus import beads_prime
from nexus.hooks import session_context

KEY = "beads@beads-marketplace"


def _settings(path: Path, enabled: dict | None, raw: str | None = None) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(raw if raw is not None else json.dumps({"enabledPlugins": enabled}), encoding="utf-8")


@pytest.mark.parametrize(
    ("user", "project", "local", "expected"),
    [
        ({KEY: True}, None, None, True),
        ({KEY: True}, {KEY: False}, None, False),
        ({KEY: True}, {KEY: False}, {KEY: True}, True),
        ({KEY: True}, {KEY: True}, {KEY: False}, False),
        ({"beads@a": True}, {"beads@b": False}, None, True),
        (None, {KEY: True}, None, True),
        ({"conexus@nexus-plugins": True}, None, None, False),
        (None, None, None, False),
    ],
)
def test_effective_enablement_follows_settings_precedence(
    tmp_path: Path, user, project, local, expected
) -> None:
    config, proj = tmp_path / "config", tmp_path / "proj"
    proj.mkdir()
    if user is not None:
        _settings(config / "settings.json", user)
    if project is not None:
        _settings(proj / ".claude" / "settings.json", project)
    if local is not None:
        _settings(proj / ".claude" / "settings.local.json", local)
    assert beads_prime.beads_plugin_enabled(proj, claude_config_dir=config) is expected


def test_malformed_settings_file_counts_as_absent(tmp_path: Path) -> None:
    config, proj = tmp_path / "config", tmp_path / "proj"
    proj.mkdir()
    _settings(config / "settings.json", {KEY: True})
    _settings(proj / ".claude" / "settings.json", None, raw="{not json")
    assert beads_prime.beads_plugin_enabled(proj, claude_config_dir=config) is True


def test_plugin_key_from_any_marketplace_counts(tmp_path: Path) -> None:
    config, proj = tmp_path / "config", tmp_path / "proj"
    proj.mkdir()
    _settings(config / "settings.json", {"beads@some-fork": True})
    assert beads_prime.beads_plugin_enabled(proj, claude_config_dir=config) is True


def test_enabled_key_for_an_uninstalled_plugin_counts_as_off(tmp_path: Path) -> None:
    config, proj = tmp_path / "config", tmp_path / "proj"
    proj.mkdir()
    _settings(config / "settings.json", {KEY: True})
    record = config / "plugins" / "installed_plugins.json"
    record.parent.mkdir(parents=True)
    record.write_text(json.dumps({"version": 2, "plugins": {"conexus@nexus-plugins": []}}), encoding="utf-8")
    assert beads_prime.beads_plugin_enabled(proj, claude_config_dir=config) is False
    record.write_text(json.dumps({"version": 2, "plugins": {KEY: []}}), encoding="utf-8")
    assert beads_prime.beads_plugin_enabled(proj, claude_config_dir=config) is True


class TestSessionContextPrime:
    PRIME = "# beads usage\nVerbs: `bd ready` — ✓"

    def _run(self, monkeypatch, tmp_path: Path, *, beads_dir: bool, plugin_on: bool,
             prime: str | None = PRIME, ready: str | None = "") -> tuple[str, list]:
        proj = tmp_path / "project"
        proj.mkdir()
        if beads_dir:
            (proj / ".beads").mkdir()
        config = tmp_path / "config"
        _settings(config / "settings.json", {KEY: plugin_on})
        monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(config))
        monkeypatch.setenv("HOME", str(tmp_path / "home"))
        monkeypatch.setattr(session_context, "which", lambda cmd: cmd == "bd")
        calls: list = []

        def _fake(args, timeout, cwd=None):
            calls.append(args)
            return prime

        def _fake_spawn(args, timeout, cwd=None):
            # `bd ready` goes through _spawn (it needs the exit code and stderr, GH #1689): None is a failed bd, "" a clean empty list
            calls.append(args)
            if ready is None:
                return types.SimpleNamespace(returncode=1, stdout="", stderr="Error: dolt server unreachable"), ""
            return types.SimpleNamespace(returncode=0, stdout=ready, stderr=""), ""

        monkeypatch.setattr(session_context, "run_command", _fake)
        monkeypatch.setattr(session_context, "_spawn", _fake_spawn)
        return "\n".join(session_context._render_beads(proj)), calls

    def test_prints_prime_when_plugin_is_off(self, monkeypatch, tmp_path: Path) -> None:
        out, calls = self._run(monkeypatch, tmp_path, beads_dir=True, plugin_on=False)
        assert ["bd", "prime"] in calls
        assert self.PRIME in out

    def test_silent_when_plugin_is_on(self, monkeypatch, tmp_path: Path) -> None:
        out, calls = self._run(monkeypatch, tmp_path, beads_dir=True, plugin_on=True)
        assert ["bd", "prime"] not in calls
        assert self.PRIME not in out

    def test_silent_outside_a_beads_repo(self, monkeypatch, tmp_path: Path) -> None:
        out, calls = self._run(monkeypatch, tmp_path, beads_dir=False, plugin_on=False)
        assert ["bd", "prime"] not in calls
        assert self.PRIME not in out

    def test_failed_prime_is_silent_and_keeps_the_ready_beads(self, monkeypatch, tmp_path: Path) -> None:
        out, calls = self._run(monkeypatch, tmp_path, beads_dir=True, plugin_on=False, prime=None,
                               ready="○ nexus-abc123 ● P1 [bug] a ready bead")
        assert ["bd", "prime"] in calls
        assert "## Ready Beads" in out and "a ready bead" in out

    def test_prime_skipped_when_bd_ready_failed(self, monkeypatch, tmp_path: Path) -> None:
        out, calls = self._run(monkeypatch, tmp_path, beads_dir=True, plugin_on=False, ready=None)
        assert ["bd", "prime"] not in calls
        assert self.PRIME not in out
        assert "exit 1" in out and "dolt server unreachable" in out, "a failed bd ready is said, not silent (GH #1689)"

    def test_session_start_prints_the_primer_where_the_plugin_is_off_but_never_runs_bd_ready(self, monkeypatch, tmp_path: Path) -> None:
        """marginalia's gate (S2b): ``bd prime`` is that project's only primer, so it stays at session start; the ready list
        is the on-demand half."""
        proj = tmp_path / "project"
        (proj / ".beads").mkdir(parents=True)
        monkeypatch.setenv("CLAUDE_PROJECT_DIR", str(proj))
        monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "claude"))
        monkeypatch.setenv("NEXUS_CONFIG_DIR", str(tmp_path / "config"))
        monkeypatch.chdir(proj)
        calls: list = []

        def fake(args, timeout, cwd=None):
            calls.append(list(args))
            return self.PRIME if args == ["bd", "prime"] else None

        monkeypatch.setattr(session_context, "run_command", fake)
        assert self.PRIME in (session_context.run(None).stdout or "")
        assert ["bd", "ready"] not in calls
