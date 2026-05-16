# SPDX-License-Identifier: AGPL-3.0-or-later
"""Plugin/wheel version-compat guard for orb_bridge_*.py scripts (nexus-yeu8).

The nx plugin scripts embed an expected ``BRIDGE_API_VERSION``; the wheel
exports its current value via ``nexus.cockpit.hook_bridge``. On mismatch the
helper logs an ERROR-level structlog event and returns False so the script
can exit 0 without writing any tuple. On match the helper returns True.

Tests:
- BRIDGE_API_VERSION is exported and an int.
- check_bridge_api_version returns True on match (no warn/error log).
- check_bridge_api_version returns False on mismatch and logs ERROR with both versions.
- A bridge script with a stale embedded version exits 0 and does NOT call emit().
- A bridge script with the matching embedded version proceeds to call emit().
"""

from __future__ import annotations

import subprocess
import sys
import textwrap
from pathlib import Path

import pytest
import structlog
from structlog.testing import capture_logs

from nexus.cockpit import hook_bridge


def test_bridge_api_version_exported_as_int() -> None:
    assert isinstance(hook_bridge.BRIDGE_API_VERSION, int)
    assert hook_bridge.BRIDGE_API_VERSION == 1


def test_check_bridge_api_version_match_returns_true() -> None:
    structlog.configure()  # reset to default
    with capture_logs() as logs:
        assert hook_bridge.check_bridge_api_version(hook_bridge.BRIDGE_API_VERSION) is True
    # No error or warning logs on match
    assert not any(
        entry.get("log_level") in ("error", "warning")
        and entry.get("event") == "hook_bridge_version_mismatch"
        for entry in logs
    )


def test_check_bridge_api_version_mismatch_returns_false_and_logs_error() -> None:
    structlog.configure()
    wrong = hook_bridge.BRIDGE_API_VERSION + 99
    with capture_logs() as logs:
        result = hook_bridge.check_bridge_api_version(wrong)
    assert result is False
    matching = [e for e in logs if e.get("event") == "hook_bridge_version_mismatch"]
    assert len(matching) == 1
    entry = matching[0]
    assert entry["log_level"] == "error"
    assert entry["expected"] == wrong
    assert entry["actual"] == hook_bridge.BRIDGE_API_VERSION


# ---------------------------------------------------------------------------
# All seven shipped scripts must embed the current BRIDGE_API_VERSION.
# ---------------------------------------------------------------------------

_SCRIPTS = [
    "orb_bridge_pretooluse.py",
    "orb_bridge_posttooluse.py",
    "orb_bridge_stop.py",
    "orb_bridge_subagent_stop.py",
    "orb_bridge_user_prompt_submit.py",
    "orb_bridge_session.py",
    "orb_bridge_notification.py",
]


def _scripts_dir() -> Path:
    # tests/cockpit/test_bridge_version_compat.py -> repo root -> nx/hooks/scripts
    return Path(__file__).resolve().parents[2] / "nx" / "hooks" / "scripts"


@pytest.mark.parametrize("script_name", _SCRIPTS)
def test_each_script_embeds_current_bridge_api_version(script_name: str) -> None:
    text = (_scripts_dir() / script_name).read_text()
    expected_literal = f"EXPECTED_BRIDGE_API_VERSION = {hook_bridge.BRIDGE_API_VERSION}"
    assert expected_literal in text, (
        f"{script_name} must embed EXPECTED_BRIDGE_API_VERSION = "
        f"{hook_bridge.BRIDGE_API_VERSION}"
    )
    # And must call the helper
    assert "check_bridge_api_version" in text


@pytest.mark.parametrize("script_name", _SCRIPTS)
def test_script_exits_0_on_version_mismatch_without_emitting(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, script_name: str
) -> None:
    """Force a mismatch via a sitecustomize shim, run the script, assert no emit()."""
    # Shim that monkeypatches the wheel's BRIDGE_API_VERSION to a different value
    # and replaces emit() with a sentinel that writes to a marker file.
    marker = tmp_path / "emit_called"
    sitecustomize = tmp_path / "sitecustomize.py"
    sitecustomize.write_text(
        textwrap.dedent(
            f"""
            from nexus.cockpit import hook_bridge as _hb
            _hb.BRIDGE_API_VERSION = _hb.BRIDGE_API_VERSION + 99

            def _fake_emit(*a, **kw):
                open({str(marker)!r}, "w").write("called")

            _hb.emit = _fake_emit
            """
        )
    )

    env = {
        **dict(__import__("os").environ),
        "PYTHONPATH": f"{tmp_path}:" + dict(__import__("os").environ).get("PYTHONPATH", ""),
        # Force CLAUDECODE on so emit would otherwise be attempted
        "CLAUDECODE": "1",
    }
    script = _scripts_dir() / script_name
    proc = subprocess.run(
        [sys.executable, str(script)],
        input="{}",
        capture_output=True,
        text=True,
        env=env,
        timeout=15,
    )
    assert proc.returncode == 0, f"stderr={proc.stderr}"
    assert not marker.exists(), (
        f"emit() was called despite version mismatch (script={script_name}, "
        f"stderr={proc.stderr})"
    )


def test_script_proceeds_on_version_match(tmp_path: Path) -> None:
    """When versions match, the script calls emit() (verified via shim)."""
    marker = tmp_path / "emit_called"
    sitecustomize = tmp_path / "sitecustomize.py"
    sitecustomize.write_text(
        textwrap.dedent(
            f"""
            from nexus.cockpit import hook_bridge as _hb

            def _fake_emit(*a, **kw):
                open({str(marker)!r}, "w").write("called")

            def _fake_output(*a, **kw):
                return None

            _hb.emit = _fake_emit
            _hb.output_for_hook = _fake_output
            """
        )
    )
    env = {
        **dict(__import__("os").environ),
        "PYTHONPATH": f"{tmp_path}:" + dict(__import__("os").environ).get("PYTHONPATH", ""),
        "CLAUDECODE": "1",
    }
    script = _scripts_dir() / "orb_bridge_notification.py"
    proc = subprocess.run(
        [sys.executable, str(script)],
        input="{}",
        capture_output=True,
        text=True,
        env=env,
        timeout=15,
    )
    assert proc.returncode == 0, f"stderr={proc.stderr}"
    assert marker.exists(), f"emit() not invoked on version match; stderr={proc.stderr}"
