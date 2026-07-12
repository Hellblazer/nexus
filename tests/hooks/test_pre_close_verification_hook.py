# SPDX-License-Identifier: AGPL-3.0-or-later
"""Tests for the PreToolUse close verification hook script.

The hook is advisory-only — checks for review scratch marker but never
blocks. No test execution.
"""
from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

import pytest

SCRIPT = (
    Path(__file__).resolve().parents[2]
    / "conexus"
    / "hooks"
    / "scripts"
    / "pre_close_verification_hook.sh"
)

_SAFE_PATH = "/usr/bin:/bin"

import shutil as _shutil

_PYTHON3 = _shutil.which("python3") or ""
if _PYTHON3:
    _SAFE_PATH = str(Path(_PYTHON3).parent) + ":" + _SAFE_PATH


def _make_payload(
    tool_name: str = "Bash",
    command: str = "bd close nexus-4yit",
) -> str:
    return json.dumps({
        "session_id": "test-session",
        "hook_event_name": "PreToolUse",
        "tool_name": tool_name,
        "tool_input": {"command": command},
    })


@pytest.fixture
def mock_config_env(tmp_path):
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


def _get_decision(parsed: dict) -> str:
    return parsed.get("hookSpecificOutput", {}).get("permissionDecision", "")


def _get_context(parsed: dict) -> str:
    return parsed.get("hookSpecificOutput", {}).get("additionalContext", "")


class TestPreCloseVerificationHook:
    """PreToolUse close hook — advisory only, never blocks."""

    def test_script_exists_and_is_executable(self) -> None:
        assert SCRIPT.exists()
        assert os.access(SCRIPT, os.X_OK)

    def test_exits_zero_always(self) -> None:
        assert _run_hook(_make_payload()).returncode == 0

    def test_outputs_valid_json(self) -> None:
        parsed = json.loads(_run_hook(_make_payload()).stdout)
        assert "hookSpecificOutput" in parsed

    def test_never_denies(self, mock_config_env) -> None:
        """Even with on_close=True, decision is always allow."""
        env = mock_config_env({"on_close": True})
        result = _run_hook(_make_payload(), env_overrides={"PATH": _SAFE_PATH, **env})
        assert _get_decision(json.loads(result.stdout)) == "allow"

    def test_fast_noop_non_bash_tool(self) -> None:
        result = _run_hook(_make_payload(tool_name="Write"))
        assert _get_decision(json.loads(result.stdout)) == "allow"

    def test_fast_noop_non_matching_bash(self) -> None:
        result = _run_hook(_make_payload(command="ls -la"))
        assert _get_decision(json.loads(result.stdout)) == "allow"

    def test_fast_noop_bd_list(self) -> None:
        result = _run_hook(_make_payload(command="bd list --status=in_progress"))
        assert _get_decision(json.loads(result.stdout)) == "allow"

    def test_allow_when_on_close_false(self, mock_config_env) -> None:
        env = mock_config_env({"on_close": False})
        result = _run_hook(_make_payload(), env_overrides={"PATH": _SAFE_PATH, **env})
        assert _get_decision(json.loads(result.stdout)) == "allow"

    def test_allow_when_config_reader_fails(self) -> None:
        result = _run_hook(
            _make_payload(),
            env_overrides={"CLAUDE_PLUGIN_ROOT": "/nonexistent/path"},
        )
        assert _get_decision(json.loads(result.stdout)) == "allow"

    def test_bd_done_pattern_matches(self, mock_config_env) -> None:
        env = mock_config_env({"on_close": True})
        result = _run_hook(
            _make_payload(command="bd done nexus-xyz"),
            env_overrides={"PATH": _SAFE_PATH, **env},
        )
        assert _get_decision(json.loads(result.stdout)) == "allow"

    def test_graceful_empty_stdin(self) -> None:
        result = _run_hook("")
        assert _get_decision(json.loads(result.stdout)) == "allow"


class TestSessionIdExport:
    """nexus-36q84: the hook is detached from any live nx-mcp process and
    cannot rely on env-var inheritance from a parent Claude session. It
    must extract ``session_id`` from its own stdin JSON payload (present
    on every hook invocation per the standard hook contract — see
    ``_make_payload``) and export it as ``NX_SESSION_ID`` before invoking
    ``nx scratch list``, so the CLI resolves the CORRECT session's T1 data
    instead of falling through to the machine-wide (and possibly
    clobbered-by-a-sibling-session) ``current_session`` flat file.
    """

    @staticmethod
    def _make_fake_nx(tmp_path: Path) -> Path:
        """A fake `nx` on PATH that logs the NX_SESSION_ID it observed
        for every invocation, then emits harmless scratch-list-shaped
        output so the hook's downstream grep checks don't blow up."""
        fake_bin = tmp_path / "fakebin"
        fake_bin.mkdir()
        nx_script = fake_bin / "nx"
        nx_script.write_text(
            "#!/bin/bash\n"
            'echo "NX_SESSION_ID=${NX_SESSION_ID:-<unset>}" >> "$NX_CALL_LOG"\n'
            'echo "no scratch entries"\n'
            "exit 0\n"
        )
        nx_script.chmod(0o755)
        return fake_bin

    def test_exports_session_id_from_stdin_payload_for_review_check(
        self, tmp_path, mock_config_env
    ) -> None:
        fake_bin = self._make_fake_nx(tmp_path)
        log_file = tmp_path / "nx_calls.log"
        env = mock_config_env({"on_close": True})

        result = _run_hook(
            _make_payload(command="bd close nexus-4yit"),
            env_overrides={
                "PATH": f"{fake_bin}:{_SAFE_PATH}",
                "NX_CALL_LOG": str(log_file),
                **env,
            },
        )

        assert result.returncode == 0
        log_contents = log_file.read_text() if log_file.exists() else ""
        # _make_payload's default session_id is "test-session".
        assert "NX_SESSION_ID=test-session" in log_contents, log_contents

    def test_exports_session_id_from_stdin_payload_for_rdr_close_check(
        self, tmp_path
    ) -> None:
        """The `bd create` / rdr-close-active branch also calls
        `nx scratch list` (to look up the active-close marker) — it must
        see the same exported NX_SESSION_ID."""
        fake_bin = self._make_fake_nx(tmp_path)
        log_file = tmp_path / "nx_calls.log"

        payload = json.dumps({
            "session_id": "rdr-close-session",
            "hook_event_name": "PreToolUse",
            "tool_name": "Bash",
            "tool_input": {"command": "bd create --title foo --description bar"},
        })

        result = _run_hook(
            payload,
            env_overrides={
                "PATH": f"{fake_bin}:{_SAFE_PATH}",
                "NX_CALL_LOG": str(log_file),
            },
        )

        assert result.returncode == 0
        log_contents = log_file.read_text() if log_file.exists() else ""
        assert "NX_SESSION_ID=rdr-close-session" in log_contents, log_contents

    def test_missing_session_id_in_payload_preserves_ambient_env(
        self, tmp_path, mock_config_env
    ) -> None:
        """Defensive: if the stdin payload has no session_id field, the
        hook must NOT clobber a legitimate pre-existing NX_SESSION_ID
        with an empty value."""
        fake_bin = self._make_fake_nx(tmp_path)
        log_file = tmp_path / "nx_calls.log"
        env = mock_config_env({"on_close": True})

        payload = json.dumps({
            "hook_event_name": "PreToolUse",
            "tool_name": "Bash",
            "tool_input": {"command": "bd close nexus-4yit"},
        })

        result = _run_hook(
            payload,
            env_overrides={
                "PATH": f"{fake_bin}:{_SAFE_PATH}",
                "NX_CALL_LOG": str(log_file),
                "NX_SESSION_ID": "pre-existing-ambient-value",
                **env,
            },
        )

        assert result.returncode == 0
        log_contents = log_file.read_text() if log_file.exists() else ""
        assert "NX_SESSION_ID=pre-existing-ambient-value" in log_contents, log_contents
