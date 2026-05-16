# SPDX-License-Identifier: AGPL-3.0-or-later
"""Tests for ``nx doctor --check-bridge`` (RDR-111 + nexus-y1xc + nexus-1xip).

Coverage:
- plugin/wheel version skew (nexus-y1xc): mismatch produces a soft-warn line.
- plugin/wheel version match: produces a green check line.
- daemon-mode tuples.db refusal (nexus-1xip): under ``NX_STORAGE_MODE=daemon``
  the readback is skipped with a hint, not opened directly.
- direct mode: the readback runs against the on-disk tuples.db.
"""
from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest
from click.testing import CliRunner

from nexus.commands import doctor as doctor_cmd


def _write_plugin_manifest(plugin_root: Path, version: str) -> None:
    manifest_dir = plugin_root / ".claude-plugin"
    manifest_dir.mkdir(parents=True, exist_ok=True)
    (manifest_dir / "plugin.json").write_text(json.dumps({"name": "nx", "version": version}))


def _stub_bridge_scripts(plugin_root: Path) -> None:
    scripts = plugin_root / "hooks" / "scripts"
    scripts.mkdir(parents=True)
    for name in (
        "orb_bridge_pretooluse.py",
        "orb_bridge_posttooluse.py",
        "orb_bridge_stop.py",
        "orb_bridge_subagent_stop.py",
        "orb_bridge_user_prompt_submit.py",
        "orb_bridge_session.py",
        "orb_bridge_notification.py",
    ):
        (scripts / name).write_text("# stub\n")


def _make_tuples_db(path: Path) -> None:
    """Create a tuples.db with the minimum schema for the readback."""
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path))
    conn.execute(
        """\
        CREATE TABLE tuples (
            id TEXT PRIMARY KEY,
            subspace TEXT,
            template_name TEXT,
            content TEXT,
            dimensions_json TEXT,
            embed_text TEXT,
            created_at REAL
        )
        """
    )
    conn.commit()
    conn.close()


def test_plugin_wheel_version_match_reports_ok(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """When plugin manifest version matches the installed wheel, check passes."""
    plugin_root = tmp_path / "plugin"
    _stub_bridge_scripts(plugin_root)

    # Match: read the actual wheel version and write it into the manifest.
    from importlib.metadata import version as _pkg_version

    wheel_version = _pkg_version("conexus")
    _write_plugin_manifest(plugin_root, wheel_version)

    monkeypatch.setenv("CLAUDE_PLUGIN_ROOT", str(plugin_root))
    monkeypatch.setenv("HOME", str(tmp_path))  # isolate ~/.config/nexus/tuples.db

    runner = CliRunner()
    result = runner.invoke(doctor_cmd.doctor_cmd, ["--check-bridge"])

    assert result.exit_code == 0, result.output
    # Plugin/wheel version line is present and green.
    assert "plugin/wheel version" in result.output
    assert wheel_version in result.output


def test_plugin_wheel_version_skew_reports_warning(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """When plugin manifest version != wheel version, check emits a skew warning."""
    plugin_root = tmp_path / "plugin"
    _stub_bridge_scripts(plugin_root)
    _write_plugin_manifest(plugin_root, "0.0.0-stale")

    monkeypatch.setenv("CLAUDE_PLUGIN_ROOT", str(plugin_root))
    monkeypatch.setenv("HOME", str(tmp_path))

    runner = CliRunner()
    result = runner.invoke(doctor_cmd.doctor_cmd, ["--check-bridge"])

    assert result.exit_code == 0, result.output
    assert "plugin/wheel version" in result.output
    assert "skew" in result.output
    assert "0.0.0-stale" in result.output


def test_check_bridge_under_daemon_mode_skips_tuples_readback(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """nexus-1xip: with NX_STORAGE_MODE=daemon, the recent-tuples readback
    is skipped with a hint, not opened via direct sqlite3.connect."""
    plugin_root = tmp_path / "plugin"
    _stub_bridge_scripts(plugin_root)
    _write_plugin_manifest(plugin_root, "0.0.0-stale")  # avoid wheel-skew noise

    # Create a tuples.db at the path the check looks for so the readback
    # branch is actually reachable absent the daemon-mode gate.
    fake_home = tmp_path / "home"
    fake_tuples = fake_home / ".config" / "nexus" / "tuples.db"
    _make_tuples_db(fake_tuples)

    monkeypatch.setenv("CLAUDE_PLUGIN_ROOT", str(plugin_root))
    monkeypatch.setenv("HOME", str(fake_home))
    monkeypatch.setenv("NX_STORAGE_MODE", "daemon")

    runner = CliRunner()
    result = runner.invoke(doctor_cmd.doctor_cmd, ["--check-bridge"])

    assert result.exit_code == 0, result.output
    # The skip message must appear and mention the daemon-mode hint.
    assert "recent hook events" in result.output
    assert "skipped" in result.output.lower() or "daemon" in result.output.lower()
    # The direct-mode "tuple(s) in the last 24h" branch must NOT fire.
    assert "tuple(s) in the last 24h" not in result.output


def test_check_bridge_direct_mode_opens_tuples_readback(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """In direct mode (no NX_STORAGE_MODE), the readback runs against tuples.db."""
    plugin_root = tmp_path / "plugin"
    _stub_bridge_scripts(plugin_root)
    _write_plugin_manifest(plugin_root, "0.0.0-stale")

    fake_home = tmp_path / "home"
    fake_tuples = fake_home / ".config" / "nexus" / "tuples.db"
    _make_tuples_db(fake_tuples)

    monkeypatch.setenv("CLAUDE_PLUGIN_ROOT", str(plugin_root))
    monkeypatch.setenv("HOME", str(fake_home))
    monkeypatch.delenv("NX_STORAGE_MODE", raising=False)

    runner = CliRunner()
    result = runner.invoke(doctor_cmd.doctor_cmd, ["--check-bridge"])

    assert result.exit_code == 0, result.output
    # In direct mode the readback runs; with an empty tuples table the
    # output names the 24h window.
    assert "recent hook events" in result.output
    assert "24h" in result.output
