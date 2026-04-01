# SPDX-License-Identifier: AGPL-3.0-or-later
"""Tests for the PreToolUse close verification hook script."""
from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

import pytest

SCRIPT = (
    Path(__file__).resolve().parents[2]
    / "nx"
    / "hooks"
    / "scripts"
    / "pre_close_verification_hook.sh"
)

# Minimal PATH to avoid invoking real bd/nx in tests
_SAFE_PATH = "/usr/bin:/bin"

# Prepend python3 dir so the config reader script can run
import shutil as _shutil

_PYTHON3 = _shutil.which("python3") or ""
if _PYTHON3:
    _SAFE_PATH = str(Path(_PYTHON3).parent) + ":" + _SAFE_PATH


def _make_payload(
    tool_name: str = "Bash",
    command: str = "bd close nexus-4yit",
) -> str:
    return json.dumps(
        {
            "session_id": "test-session",
            "hook_event_name": "PreToolUse",
            "tool_name": tool_name,
            "tool_input": {"command": command},
        }
    )


@pytest.fixture
def mock_config_env(tmp_path):
    """Factory: create CLAUDE_PLUGIN_ROOT with a fake read_verification_config.py."""

    def _make(config: dict) -> dict[str, str]:
        scripts_dir = tmp_path / "hooks" / "scripts"
        scripts_dir.mkdir(parents=True)
        script = scripts_dir / "read_verification_config.py"
        config_json = json.dumps(config)
        script.write_text(f"print({repr(config_json)})\n")
        return {"CLAUDE_PLUGIN_ROOT": str(tmp_path)}

    return _make


def _run_hook(
    stdin: str,
    *,
    env_overrides: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    env = {
        **os.environ,
        "PATH": _SAFE_PATH,
        **(env_overrides or {}),
    }
    return subprocess.run(
        ["bash", str(SCRIPT)],
        input=stdin,
        capture_output=True,
        text=True,
        timeout=30,
        env=env,
    )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _parse(result: subprocess.CompletedProcess[str]) -> dict:
    """Return parsed JSON from stdout."""
    return json.loads(result.stdout.strip())


def _get_decision(parsed: dict) -> str:
    """Extract permissionDecision from PreToolUse hookSpecificOutput."""
    return parsed.get("hookSpecificOutput", {}).get("permissionDecision", "")


def _get_reason(parsed: dict) -> str:
    """Extract the reason/context string from PreToolUse output."""
    hso = parsed.get("hookSpecificOutput", {})
    return hso.get("permissionDecisionReason", "") or hso.get("additionalContext", "")


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestPreCloseVerificationHook:
    """PreToolUse close verification hook tests."""

    # --- Basic invariants ---

    def test_script_exists_and_is_executable(self) -> None:
        assert SCRIPT.exists(), f"Script not found: {SCRIPT}"
        assert os.access(SCRIPT, os.X_OK), f"Script not executable: {SCRIPT}"

    def test_exits_zero_always(self) -> None:
        result = _run_hook(_make_payload())
        assert result.returncode == 0

    def test_outputs_valid_json_with_hookspecificoutput(self) -> None:
        result = _run_hook(_make_payload())
        parsed = _parse(result)
        assert "hookSpecificOutput" in parsed
        decision = _get_decision(parsed)
        assert decision in ("allow", "deny", "ask")

    # --- Fast no-ops (tool_name check) ---

    def test_fast_noop_non_bash_tool(self) -> None:
        """Non-Bash tool → allow immediately without reading config."""
        result = _run_hook(
            _make_payload(tool_name="Write", command="bd close nexus-4yit"),
        )
        assert result.returncode == 0
        assert _get_decision(_parse(result)) == "allow"

    def test_fast_noop_non_matching_bash(self) -> None:
        """Bash command that doesn't match bd close/done → allow."""
        result = _run_hook(_make_payload(command="ls -la"))
        assert result.returncode == 0
        assert _get_decision(_parse(result)) == "allow"

    def test_fast_noop_bd_list(self) -> None:
        """bd list is NOT close/done → allow."""
        result = _run_hook(_make_payload(command="bd list --status=in_progress"))
        assert result.returncode == 0
        assert _get_decision(_parse(result)) == "allow"

    # --- Config guard ---

    def test_allow_when_on_close_false(self, mock_config_env) -> None:
        """on_close=False → allow without running tests."""
        env = mock_config_env({"on_close": False, "test_command": "false"})
        result = _run_hook(
            _make_payload(),
            env_overrides={"PATH": _SAFE_PATH, **env},
        )
        assert result.returncode == 0
        assert _get_decision(_parse(result)) == "allow"

    def test_allow_when_config_reader_fails(self) -> None:
        """Missing CLAUDE_PLUGIN_ROOT → config reader fails → allow (safe default)."""
        result = _run_hook(
            _make_payload(),
            env_overrides={"CLAUDE_PLUGIN_ROOT": "/nonexistent/path"},
        )
        assert result.returncode == 0
        assert _get_decision(_parse(result)) == "allow"

    # --- Layer 2a: mechanical gate ---

    def test_denies_on_test_failure(self, mock_config_env) -> None:
        """on_close=True, test_command='false' → deny."""
        env = mock_config_env({"on_close": True, "test_command": "false", "test_timeout": 10})
        result = _run_hook(
            _make_payload(),
            env_overrides={"PATH": _SAFE_PATH, **env},
        )
        assert result.returncode == 0
        parsed = _parse(result)
        assert _get_decision(parsed) == "deny"
        assert "Tests failing" in _get_reason(parsed)

    def test_allows_on_test_pass(self, mock_config_env) -> None:
        """on_close=True, test_command='true' → allow."""
        env = mock_config_env({"on_close": True, "test_command": "true", "test_timeout": 10})
        result = _run_hook(
            _make_payload(),
            env_overrides={"PATH": _SAFE_PATH, **env},
        )
        assert result.returncode == 0
        assert _get_decision(_parse(result)) == "allow"

    def test_bd_done_pattern_matches(self, mock_config_env) -> None:
        """'bd done <id>' should trigger the gate (not fast-noop)."""
        env = mock_config_env({"on_close": True, "test_command": "true", "test_timeout": 10})
        result = _run_hook(
            _make_payload(command="bd done nexus-xyz"),
            env_overrides={"PATH": _SAFE_PATH, **env},
        )
        assert result.returncode == 0
        assert _get_decision(_parse(result)) == "allow"

    def test_command_not_found_is_advisory(self, mock_config_env) -> None:
        """test_command not found → allow with advisory, not deny."""
        env = mock_config_env(
            {
                "on_close": True,
                "test_command": "nonexistent_cmd_xyz_999",
                "test_timeout": 10,
            }
        )
        result = _run_hook(
            _make_payload(),
            env_overrides={"PATH": _SAFE_PATH, **env},
        )
        assert result.returncode == 0
        parsed = _parse(result)
        assert _get_decision(parsed) == "allow"
        assert "ADVISORY" in _get_reason(parsed)

    def test_empty_test_command_advisory(self, mock_config_env) -> None:
        """on_close=True but test_command='' → allow with advisory."""
        env = mock_config_env({"on_close": True, "test_command": "", "test_timeout": 10})
        result = _run_hook(
            _make_payload(),
            env_overrides={"PATH": _SAFE_PATH, **env},
        )
        assert result.returncode == 0
        parsed = _parse(result)
        assert _get_decision(parsed) == "allow"
        assert "ADVISORY" in _get_reason(parsed)

    # --- Edge cases ---

    def test_graceful_empty_stdin(self) -> None:
        """Empty stdin → allow (no crash)."""
        result = _run_hook("")
        assert result.returncode == 0
        assert _get_decision(_parse(result)) == "allow"

    def test_bd_close_multiple_ids(self, mock_config_env) -> None:
        """'bd close id1 id2' → extracts first bead ID (id1) and proceeds."""
        env = mock_config_env({"on_close": True, "test_command": "true", "test_timeout": 10})
        result = _run_hook(
            _make_payload(command="bd close id1 id2"),
            env_overrides={"PATH": _SAFE_PATH, **env},
        )
        assert result.returncode == 0
        assert _get_decision(_parse(result)) == "allow"
