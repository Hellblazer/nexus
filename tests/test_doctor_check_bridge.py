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


# ---------------------------------------------------------------------------
# RDR-114 Step 3 (nexus-6bad): operator-surfacing for the fail-closed policy
# ---------------------------------------------------------------------------


def _write_daemon_log(fake_home: Path, lines: list[str]) -> Path:
    """Write a daemon.log under the fake HOME's nexus config dir, return path."""
    log_dir = fake_home / ".config" / "nexus" / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    log_path = log_dir / "daemon.log"
    log_path.write_text("\n".join(lines) + "\n")
    return log_path


def test_check_bridge_reports_operator_override_when_opt_in_set(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """nexus-6bad: NX_BRIDGE_ALLOW_DIRECT_FALLBACK=1 is surfaced as an
    operator override so the override does not silently linger."""
    plugin_root = tmp_path / "plugin"
    _stub_bridge_scripts(plugin_root)
    _write_plugin_manifest(plugin_root, "0.0.0-stale")
    fake_home = tmp_path / "home"
    fake_home.mkdir()

    monkeypatch.setenv("CLAUDE_PLUGIN_ROOT", str(plugin_root))
    monkeypatch.setenv("HOME", str(fake_home))
    monkeypatch.setenv("NX_BRIDGE_ALLOW_DIRECT_FALLBACK", "1")
    monkeypatch.delenv("NX_BRIDGE_DISABLE", raising=False)

    runner = CliRunner()
    result = runner.invoke(doctor_cmd.doctor_cmd, ["--check-bridge"])

    assert result.exit_code == 0, result.output
    assert "NX_BRIDGE_ALLOW_DIRECT_FALLBACK" in result.output
    out_lower = result.output.lower()
    assert "override" in out_lower or "fail-open" in out_lower or "direct" in out_lower


def test_check_bridge_default_reports_fail_closed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """nexus-6bad: with the opt-in unset, doctor reports the shipped
    fail-closed default."""
    plugin_root = tmp_path / "plugin"
    _stub_bridge_scripts(plugin_root)
    _write_plugin_manifest(plugin_root, "0.0.0-stale")
    fake_home = tmp_path / "home"
    fake_home.mkdir()

    monkeypatch.setenv("CLAUDE_PLUGIN_ROOT", str(plugin_root))
    monkeypatch.setenv("HOME", str(fake_home))
    monkeypatch.delenv("NX_BRIDGE_ALLOW_DIRECT_FALLBACK", raising=False)
    monkeypatch.delenv("NX_BRIDGE_DISABLE", raising=False)

    runner = CliRunner()
    result = runner.invoke(doctor_cmd.doctor_cmd, ["--check-bridge"])

    assert result.exit_code == 0, result.output
    # Default fail-closed should be visible as a green check line.
    out_lower = result.output.lower()
    assert "fail-closed" in out_lower or "default" in out_lower


def test_check_bridge_warns_on_conflicting_env_combination(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """nexus-6bad: BOTH NX_BRIDGE_DISABLE=1 AND
    NX_BRIDGE_ALLOW_DIRECT_FALLBACK=1 -> doctor warns that DISABLE
    exits first and ALLOW_DIRECT_FALLBACK has no effect."""
    plugin_root = tmp_path / "plugin"
    _stub_bridge_scripts(plugin_root)
    _write_plugin_manifest(plugin_root, "0.0.0-stale")
    fake_home = tmp_path / "home"
    fake_home.mkdir()

    monkeypatch.setenv("CLAUDE_PLUGIN_ROOT", str(plugin_root))
    monkeypatch.setenv("HOME", str(fake_home))
    monkeypatch.setenv("NX_BRIDGE_DISABLE", "1")
    monkeypatch.setenv("NX_BRIDGE_ALLOW_DIRECT_FALLBACK", "1")

    runner = CliRunner()
    result = runner.invoke(doctor_cmd.doctor_cmd, ["--check-bridge"])

    assert result.exit_code == 0, result.output
    out_lower = result.output.lower()
    # The warning must name both envs and explain the interaction.
    assert "nx_bridge_disable" in out_lower
    assert "nx_bridge_allow_direct_fallback" in out_lower
    assert "no effect" in out_lower or "exits first" in out_lower or "conflict" in out_lower


def test_check_bridge_surfaces_recent_drop_events_from_log(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """nexus-6bad: hook_bridge_emit_drop_rpc_failed events from
    daemon.log within the last 24h are surfaced with a count."""
    plugin_root = tmp_path / "plugin"
    _stub_bridge_scripts(plugin_root)
    _write_plugin_manifest(plugin_root, "0.0.0-stale")
    fake_home = tmp_path / "home"
    fake_home.mkdir()

    # Three drop events in the last hour, plus one stale one outside
    # the 24h window. Format matches logging_setup.py's
    # RotatingFileHandler default: "%(asctime)s %(name)s %(levelname)s %(message)s".
    # The message is structlog's KeyValueRenderer output.
    import datetime as _dt
    now = _dt.datetime.now(_dt.timezone.utc)
    recent_ts = now.isoformat()
    stale_ts = (now - _dt.timedelta(hours=48)).isoformat()
    log_lines = [
        f"{recent_ts[:19].replace('T', ' ')},123 nexus.cockpit.hook_bridge WARNING "
        f"event=hook_bridge_emit_drop_rpc_failed hook_type=PreToolUse "
        f"subspace=hook_events/tool_call_intent error=ConnectionRefusedError "
        f"timestamp={recent_ts} level=warning",
        f"{recent_ts[:19].replace('T', ' ')},234 nexus.cockpit.hook_bridge WARNING "
        f"event=hook_bridge_emit_drop_rpc_failed hook_type=PostToolUse "
        f"subspace=hook_events/tool_call_completed error=RpcTimeoutError "
        f"timestamp={recent_ts} level=warning",
        f"{recent_ts[:19].replace('T', ' ')},345 nexus.cockpit.hook_bridge WARNING "
        f"event=hook_bridge_emit_drop_rpc_failed hook_type=Stop "
        f"subspace=hook_events/assistant_turn_ended error=ConnectionRefusedError "
        f"timestamp={recent_ts} level=warning",
        # Stale (older than 24h) — must NOT count.
        f"{stale_ts[:19].replace('T', ' ')},456 nexus.cockpit.hook_bridge WARNING "
        f"event=hook_bridge_emit_drop_rpc_failed hook_type=Stop "
        f"subspace=hook_events/assistant_turn_ended error=ConnectionRefusedError "
        f"timestamp={stale_ts} level=warning",
    ]
    _write_daemon_log(fake_home, log_lines)

    monkeypatch.setenv("CLAUDE_PLUGIN_ROOT", str(plugin_root))
    monkeypatch.setenv("HOME", str(fake_home))
    monkeypatch.delenv("NX_BRIDGE_ALLOW_DIRECT_FALLBACK", raising=False)
    monkeypatch.delenv("NX_BRIDGE_DISABLE", raising=False)

    runner = CliRunner()
    result = runner.invoke(doctor_cmd.doctor_cmd, ["--check-bridge"])

    assert result.exit_code == 0, result.output
    out = result.output
    # The recent drops line must be present and report N=3 (stale event
    # is excluded by the 24h window).
    assert "recent bridge drops" in out.lower() or "bridge drops" in out.lower()
    assert "3" in out


def test_check_bridge_no_recent_drops_reports_clean(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """When no drop events exist (no log file or zero matching lines),
    doctor reports a clean 'no recent drops' line."""
    plugin_root = tmp_path / "plugin"
    _stub_bridge_scripts(plugin_root)
    _write_plugin_manifest(plugin_root, "0.0.0-stale")
    fake_home = tmp_path / "home"
    fake_home.mkdir()

    monkeypatch.setenv("CLAUDE_PLUGIN_ROOT", str(plugin_root))
    monkeypatch.setenv("HOME", str(fake_home))
    monkeypatch.delenv("NX_BRIDGE_ALLOW_DIRECT_FALLBACK", raising=False)
    monkeypatch.delenv("NX_BRIDGE_DISABLE", raising=False)

    runner = CliRunner()
    result = runner.invoke(doctor_cmd.doctor_cmd, ["--check-bridge"])

    assert result.exit_code == 0, result.output
    out_lower = result.output.lower()
    # Clean state surfaced (either via a "no drops" line or implicit pass).
    assert "no recent" in out_lower or "0 drops" in out_lower or "bridge drops" in out_lower
