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
    a non-fatal warning naming the uninstall/install commands."""
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
    assert "/plugin uninstall nx@nexus-plugins" in suggestions
    assert "/plugin install conexus@nexus-plugins" in suggestions


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
    # Actionable hint surfaces
    assert "/plugin uninstall nx@nexus-plugins" in captured
    assert "/plugin install conexus@nexus-plugins" in captured


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
