# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""nexus-dg7i6.4: SessionStart re-hosts ``bd prime`` when the beads plugin is off.

The beads Claude Code plugin's one load-bearing contribution to a beads repo
is its SessionStart/PreCompact ``bd prime`` hook (repo ``.beads/PRIME.md``
plus beads' own memories). nexus disables the plugin per project to take its
28 skill-listing entries out of the model's view (T3 1.11.758), so
``session_context`` prints ``bd prime`` itself, but only when the plugin is
effectively disabled: a user who keeps the plugin must not get PRIME twice.

Effective enablement follows Claude Code's settings precedence for
``enabledPlugins``: project ``.claude/settings.local.json`` over project
``.claude/settings.json`` over the user's ``settings.json``.
"""
from __future__ import annotations

import json
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
        monkeypatch.setenv("CLAUDE_PROJECT_DIR", str(proj))
        monkeypatch.setenv("HOME", str(tmp_path / "home"))
        monkeypatch.setattr(session_context, "which", lambda cmd: cmd == "bd")
        calls: list = []

        def _fake(args, timeout, cwd=None):
            calls.append(args)
            return prime if args == ["bd", "prime"] else ready

        monkeypatch.setattr(session_context, "run_command", _fake)
        return session_context.run(None).stdout or "", calls

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

    def test_failed_prime_is_silent_and_keeps_other_sections(self, monkeypatch, tmp_path: Path) -> None:
        out, calls = self._run(monkeypatch, tmp_path, beads_dir=True, plugin_on=False, prime=None)
        assert ["bd", "prime"] in calls
        assert "## nx Capabilities" in out

    def test_prime_skipped_when_bd_ready_failed(self, monkeypatch, tmp_path: Path) -> None:
        out, calls = self._run(monkeypatch, tmp_path, beads_dir=True, plugin_on=False, ready=None)
        assert ["bd", "prime"] not in calls
        assert self.PRIME not in out
