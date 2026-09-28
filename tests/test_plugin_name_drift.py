# SPDX-License-Identifier: AGPL-3.0-or-later
"""nexus-mkj6u: plugin-name drift detection.

The 2026-05-23 rename moved the Claude Code plugin name from ``nx``
to ``conexus``. Claude Code does NOT auto-uninstall renamed plugins;
a user's local cache at ``~/.claude/plugins/cache/nexus-plugins/nx/...``
survives the marketplace.json rename until they uninstall + reinstall.

Two surfaces detect this:

1. ``check_version_compatibility`` (mcp_infra.py) — fires every MCP
   session startup, logs ``plugin_name_mismatch`` to structlog.
2. ``_check_plugin_name`` (health.py) — surfaces in ``nx doctor`` as
   a non-fatal warning with the uninstall/install commands.
"""
from __future__ import annotations

import json
import logging
from pathlib import Path

import pytest


# ── _check_plugin_name (health.py) ───────────────────────────────────────────


def _plant_plugin_manifest(tmp_path: Path, name: str, version: str = "4.34.5") -> Path:
    """Write a fake plugin.json into a tmp CLAUDE_PLUGIN_ROOT layout."""
    plugin_root = tmp_path / "plugin_cache" / "nexus-plugins" / name / version
    (plugin_root / ".claude-plugin").mkdir(parents=True)
    manifest = plugin_root / ".claude-plugin" / "plugin.json"
    manifest.write_text(json.dumps({
        "name": name,
        "version": version,
        "description": "test fixture",
    }))
    return plugin_root


def test_check_plugin_name_warns_on_old_nx(monkeypatch, tmp_path):
    """An installed ``nx`` plugin against the conexus-expecting CLI fires
    a non-fatal warning naming both /plugin install and /reload-plugins."""
    plugin_root = _plant_plugin_manifest(tmp_path, name="nx")
    monkeypatch.setenv("CLAUDE_PLUGIN_ROOT", str(plugin_root))

    from nexus.health import _check_plugin_name

    results = _check_plugin_name()
    assert len(results) == 1
    r = results[0]
    assert r.ok is False
    assert r.fatal is False
    assert "nx@nexus-plugins" in r.detail or "renamed" in r.detail
    suggestions = " ".join(r.fix_suggestions)
    # nexus-qocnk: installing conexus does not remove nx, whose hooks stay.
    assert r.fix_suggestions[0] == "/plugin uninstall nx@nexus-plugins"
    assert "/plugin install conexus@nexus-plugins" in suggestions
    assert "/reload-plugins" in suggestions


def test_check_plugin_name_silent_when_conexus_installed(monkeypatch, tmp_path):
    """The expected ``conexus`` plugin is installed → no warning."""
    plugin_root = _plant_plugin_manifest(tmp_path, name="conexus")
    monkeypatch.setenv("CLAUDE_PLUGIN_ROOT", str(plugin_root))

    from nexus.health import _check_plugin_name

    assert _check_plugin_name() == []


def test_check_plugin_name_silent_when_no_claude_plugin_root(monkeypatch):
    """CLI-only invocation (no Claude Code in the loop) — no warning."""
    monkeypatch.delenv("CLAUDE_PLUGIN_ROOT", raising=False)
    from nexus.health import _check_plugin_name
    assert _check_plugin_name() == []


def test_check_plugin_name_silent_when_manifest_missing(monkeypatch, tmp_path):
    """CLAUDE_PLUGIN_ROOT is set but plugin.json doesn't exist."""
    monkeypatch.setenv("CLAUDE_PLUGIN_ROOT", str(tmp_path))
    from nexus.health import _check_plugin_name
    assert _check_plugin_name() == []


# ── check_version_compatibility (mcp_infra.py) ───────────────────────────────


def test_check_version_compatibility_logs_plugin_name_mismatch(monkeypatch, tmp_path, capsys):
    """Stale ``nx`` plugin against current CLI → structlog warning at
    every MCP startup. Catches the rename-not-yet-completed case.

    structlog by default routes to sys.stdout (the ConsoleRenderer),
    bypassing Python's logging module entirely. Use capsys, not caplog.
    """
    plugin_root = _plant_plugin_manifest(tmp_path, name="nx")
    monkeypatch.setenv("CLAUDE_PLUGIN_ROOT", str(plugin_root))

    # Avoid the T2-daemon path (it would log unrelated warnings).
    monkeypatch.setattr(
        "nexus.mcp_infra.default_db_path",
        lambda: Path("/nonexistent.db"),
    )

    from nexus.mcp_infra import check_version_compatibility
    check_version_compatibility()

    out, err = capsys.readouterr()
    captured = out + err
    assert "plugin_name_mismatch" in captured, (
        f"expected plugin_name_mismatch warning in stdout/stderr; got: {captured!r}"
    )
    # Actionable hint: uninstall the old plugin (nexus-qocnk: it keeps its
    # own hooks), install the new one, reload.
    assert "/plugin uninstall nx@nexus-plugins" in captured
    assert captured.index("/plugin uninstall nx@nexus-plugins") < captured.index(
        "/plugin install conexus@nexus-plugins")
    assert "/reload-plugins" in captured


def test_check_version_compatibility_silent_when_name_matches(monkeypatch, tmp_path, capsys):
    """Correct plugin name installed → no plugin_name_mismatch warning."""
    plugin_root = _plant_plugin_manifest(tmp_path, name="conexus")
    monkeypatch.setenv("CLAUDE_PLUGIN_ROOT", str(plugin_root))
    monkeypatch.setattr(
        "nexus.mcp_infra.default_db_path",
        lambda: Path("/nonexistent.db"),
    )

    from nexus.mcp_infra import check_version_compatibility
    check_version_compatibility()

    out, err = capsys.readouterr()
    captured = out + err
    assert "plugin_name_mismatch" not in captured


# ── _parse_version (mcp_infra.py) ────────────────────────────────────────────
# PORTED from tests/test_migrations.py::TestParseVersion in RDR-158 P4 Stage 4
# (nexus-i711w): the helper was REHOMED from the deleted nexus.db.migrations
# into nexus.mcp_infra, whose plugin↔CLI drift check is its surviving consumer.


class TestParseVersion:
    def test_normal_version(self) -> None:
        from nexus.mcp_infra import _parse_version

        assert _parse_version("4.1.2") == (4, 1, 2)

    def test_zero_version(self) -> None:
        from nexus.mcp_infra import _parse_version

        assert _parse_version("0.0.0") == (0, 0, 0)

    def test_prerelease_fallback(self) -> None:
        from nexus.mcp_infra import _parse_version

        assert _parse_version("1.0.0rc1") == (0, 0, 0)

    def test_empty_string_fallback(self) -> None:
        from nexus.mcp_infra import _parse_version

        assert _parse_version("") == (0, 0, 0)

    def test_two_part_version_normalized(self) -> None:
        from nexus.mcp_infra import _parse_version

        assert _parse_version("3.7") == (3, 7, 0)

    def test_single_part_version_normalized(self) -> None:
        from nexus.mcp_infra import _parse_version

        assert _parse_version("5") == (5, 0, 0)

    def test_ordering(self) -> None:
        from nexus.mcp_infra import _parse_version

        assert _parse_version("1.10.0") > _parse_version("1.9.0")
        assert _parse_version("2.0.0") > _parse_version("1.99.99")
        assert _parse_version("4.1.2") == _parse_version("4.1.2")


# nexus-qocnk: nx doctor flags a retired plugin still in Claude Code's plugin
# registry, whichever plugin the session runs (the rename check above only
# sees the running plugin's own manifest).


def _registry(tmp_path: Path, keys: list[str]) -> Path:
    import json

    path = tmp_path / "installed_plugins.json"
    path.write_text(json.dumps({"version": 2, "plugins": {
        k: [{"scope": "user", "version": "4.34.5", "installPath": "/x"}] for k in keys}}))
    return path


def test_doctor_flags_a_retired_nx_plugin_still_installed(tmp_path):
    from nexus.health import _check_retired_plugin_installed

    reg = _registry(tmp_path, ["conexus@nexus-plugins", "nx@nexus-plugins"])
    [row] = _check_retired_plugin_installed(reg)
    assert row.ok is False and row.warn is True
    assert "nx@nexus-plugins" in row.detail
    assert row.fix_suggestions[0] == "/plugin uninstall nx@nexus-plugins"


def test_doctor_retired_plugin_row_is_ok_without_one(tmp_path):
    from nexus.health import _check_retired_plugin_installed

    [row] = _check_retired_plugin_installed(_registry(tmp_path, ["conexus@nexus-plugins"]))
    assert row.ok is True
    [row] = _check_retired_plugin_installed(tmp_path / "absent.json")
    assert row.ok is True, "a box with no registry is not applicable, not a failure"
