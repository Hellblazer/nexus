# SPDX-License-Identifier: AGPL-3.0-or-later
"""Integration tests for the verification hook pipeline."""
from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

import pytest

HOOKS_DIR = Path(__file__).resolve().parents[2] / "nx" / "hooks" / "scripts"
HOOKS_JSON = Path(__file__).resolve().parents[2] / "nx" / "hooks" / "hooks.json"
STOP_HOOK = HOOKS_DIR / "stop_verification_hook.sh"
CLOSE_HOOK = HOOKS_DIR / "pre_close_verification_hook.sh"
CONFIG_READER = HOOKS_DIR / "read_verification_config.py"

# Minimal PATH with python3 but without bd/nx
_PYTHON3 = subprocess.run(
    ["which", "python3"], capture_output=True, text=True
).stdout.strip()
_MINIMAL_PATH = f"{os.path.dirname(_PYTHON3)}:/usr/bin:/bin"


def _run_hook(
    script: Path,
    stdin: str,
    *,
    env_overrides: dict[str, str] | None = None,
    cwd: str | Path | None = None,
) -> subprocess.CompletedProcess[str]:
    env = {
        "PATH": _MINIMAL_PATH,
        "HOME": os.environ.get("HOME", "/tmp"),
        **(env_overrides or {}),
    }
    return subprocess.run(
        ["bash", str(script)],
        input=stdin,
        capture_output=True,
        text=True,
        timeout=30,
        env=env,
        cwd=cwd,
    )


def _init_git_repo(path: Path) -> None:
    subprocess.run(["git", "init", str(path)], capture_output=True, check=True)
    subprocess.run(
        ["git", "-C", str(path), "config", "user.email", "test@test.com"],
        capture_output=True, check=True,
    )
    subprocess.run(
        ["git", "-C", str(path), "config", "user.name", "Test"],
        capture_output=True, check=True,
    )
    (path / "README.md").write_text("init\n")
    subprocess.run(["git", "-C", str(path), "add", "."], capture_output=True, check=True)
    subprocess.run(
        ["git", "-C", str(path), "commit", "-m", "init"],
        capture_output=True, check=True,
    )


@pytest.fixture
def mock_plugin_root(tmp_path_factory: pytest.TempPathFactory):
    """Create a mock CLAUDE_PLUGIN_ROOT with a configurable read_verification_config.py."""
    root = tmp_path_factory.mktemp("plugin")
    scripts_dir = root / "hooks" / "scripts"
    scripts_dir.mkdir(parents=True)

    def _make(config: dict) -> dict[str, str]:
        config_json = json.dumps(config)
        script = scripts_dir / "read_verification_config.py"
        script.write_text(
            f"import json; print({repr(config_json)})"
        )
        return {"CLAUDE_PLUGIN_ROOT": str(root)}

    return _make


# ---------------------------------------------------------------------------
# hooks.json structure tests
# ---------------------------------------------------------------------------


class TestHooksJsonStructure:
    """Verify hooks.json registration is correct."""

    def test_hooks_json_is_valid_json(self) -> None:
        data = json.loads(HOOKS_JSON.read_text())
        assert "hooks" in data

    def test_hooks_json_has_stop_event(self) -> None:
        data = json.loads(HOOKS_JSON.read_text())
        assert "Stop" in data["hooks"]

    def test_hooks_json_has_pretooluse_event(self) -> None:
        data = json.loads(HOOKS_JSON.read_text())
        assert "PreToolUse" in data["hooks"]

    def test_hooks_json_stop_timeout(self) -> None:
        data = json.loads(HOOKS_JSON.read_text())
        stop_hooks = data["hooks"]["Stop"]
        hook = stop_hooks[0]["hooks"][0]
        assert hook["timeout"] == 180

    def test_hooks_json_pretooluse_timeout(self) -> None:
        data = json.loads(HOOKS_JSON.read_text())
        pre_hooks = data["hooks"]["PreToolUse"]
        hook = pre_hooks[0]["hooks"][0]
        assert hook["timeout"] == 300

    def test_hooks_json_pretooluse_matcher_is_bash(self) -> None:
        data = json.loads(HOOKS_JSON.read_text())
        pre_hooks = data["hooks"]["PreToolUse"]
        assert pre_hooks[0]["matcher"] == "Bash"

    def test_hooks_json_existing_hooks_unchanged(self) -> None:
        data = json.loads(HOOKS_JSON.read_text())
        hooks = data["hooks"]
        assert "SessionStart" in hooks
        assert "PostCompact" in hooks
        assert "StopFailure" in hooks
        assert "SubagentStart" in hooks

    def test_hooks_json_references_valid_scripts(self) -> None:
        """All hook commands referencing hooks/scripts/ point to existing files."""
        data = json.loads(HOOKS_JSON.read_text())
        for event_name, event_hooks in data["hooks"].items():
            for hook_group in event_hooks:
                for hook in hook_group.get("hooks", []):
                    cmd = hook.get("command", "")
                    if "hooks/scripts/" in cmd:
                        script_name = cmd.split("hooks/scripts/")[-1].split()[0]
                        script_path = HOOKS_DIR / script_name
                        assert script_path.exists(), (
                            f"{event_name} references missing script: {script_path}"
                        )


# ---------------------------------------------------------------------------
# Script existence and permissions
# ---------------------------------------------------------------------------


class TestScriptPermissions:
    """Verify all hook scripts exist and are executable."""

    def test_stop_hook_exists_and_executable(self) -> None:
        assert STOP_HOOK.exists()
        assert os.access(STOP_HOOK, os.X_OK)

    def test_close_hook_exists_and_executable(self) -> None:
        assert CLOSE_HOOK.exists()
        assert os.access(CLOSE_HOOK, os.X_OK)

    def test_config_reader_exists(self) -> None:
        assert CONFIG_READER.exists()


# ---------------------------------------------------------------------------
# End-to-end pipeline tests
# ---------------------------------------------------------------------------


class TestStopHookPipeline:
    """End-to-end tests for the Stop verification hook (advisory only)."""

    def test_on_stop_false_passes_through(self, mock_plugin_root) -> None:
        env = mock_plugin_root({"on_stop": False})
        payload = json.dumps({"hook_event_name": "Stop", "stop_hook_active": False})
        result = _run_hook(STOP_HOOK, payload, env_overrides=env)
        assert result.returncode == 0
        assert json.loads(result.stdout)["decision"] == "approve"

    def test_on_stop_true_clean_repo(self, tmp_path, mock_plugin_root) -> None:
        _init_git_repo(tmp_path)
        env = mock_plugin_root({"on_stop": True})
        payload = json.dumps({"hook_event_name": "Stop", "stop_hook_active": False})
        result = _run_hook(STOP_HOOK, payload, env_overrides=env, cwd=tmp_path)
        assert result.returncode == 0
        assert json.loads(result.stdout)["decision"] == "approve"

    def test_warns_on_uncommitted_changes(self, tmp_path, mock_plugin_root) -> None:
        _init_git_repo(tmp_path)
        (tmp_path / "README.md").write_text("modified\n")
        env = mock_plugin_root({"on_stop": True})
        payload = json.dumps({"hook_event_name": "Stop", "stop_hook_active": False})
        result = _run_hook(STOP_HOOK, payload, env_overrides=env, cwd=tmp_path)
        assert result.returncode == 0
        output = json.loads(result.stdout)
        assert output["decision"] == "approve"
        assert "uncommitted" in output.get("reason", "").lower()


class TestCloseHookPipeline:
    """End-to-end tests for the PreToolUse close verification hook."""

    @staticmethod
    def _get_decision(output: dict) -> str:
        return output.get("hookSpecificOutput", {}).get("permissionDecision", "")

    def test_non_bash_tool_fast_noop(self) -> None:
        payload = json.dumps({
            "hook_event_name": "PreToolUse",
            "tool_name": "Write",
            "tool_input": {"file_path": "/tmp/x.txt", "content": "x"},
        })
        result = _run_hook(CLOSE_HOOK, payload)
        assert result.returncode == 0
        assert self._get_decision(json.loads(result.stdout)) == "allow"

    def test_non_matching_bash_fast_noop(self) -> None:
        payload = json.dumps({
            "hook_event_name": "PreToolUse",
            "tool_name": "Bash",
            "tool_input": {"command": "ls -la"},
        })
        result = _run_hook(CLOSE_HOOK, payload)
        assert result.returncode == 0
        assert self._get_decision(json.loads(result.stdout)) == "allow"

    def test_on_close_true_denies_failing_tests(self, mock_plugin_root) -> None:
        env = mock_plugin_root({"on_close": True, "test_command": "false", "test_timeout": 10})
        payload = json.dumps({
            "hook_event_name": "PreToolUse",
            "tool_name": "Bash",
            "tool_input": {"command": "bd close nexus-test"},
        })
        result = _run_hook(CLOSE_HOOK, payload, env_overrides=env)
        assert result.returncode == 0
        assert self._get_decision(json.loads(result.stdout)) == "deny"

    def test_on_close_true_allows_passing_tests(self, mock_plugin_root) -> None:
        env = mock_plugin_root({"on_close": True, "test_command": "true", "test_timeout": 10})
        payload = json.dumps({
            "hook_event_name": "PreToolUse",
            "tool_name": "Bash",
            "tool_input": {"command": "bd close nexus-test"},
        })
        result = _run_hook(CLOSE_HOOK, payload, env_overrides=env)
        assert result.returncode == 0
        assert self._get_decision(json.loads(result.stdout)) == "allow"

    def test_bd_done_triggers_check(self, mock_plugin_root) -> None:
        env = mock_plugin_root({"on_close": True, "test_command": "true", "test_timeout": 10})
        payload = json.dumps({
            "hook_event_name": "PreToolUse",
            "tool_name": "Bash",
            "tool_input": {"command": "bd done nexus-test"},
        })
        result = _run_hook(CLOSE_HOOK, payload, env_overrides=env)
        assert result.returncode == 0
        assert self._get_decision(json.loads(result.stdout)) == "allow"

    def test_on_close_false_passes_through(self, mock_plugin_root) -> None:
        env = mock_plugin_root({"on_close": False, "test_command": "false"})
        payload = json.dumps({
            "hook_event_name": "PreToolUse",
            "tool_name": "Bash",
            "tool_input": {"command": "bd close nexus-test"},
        })
        result = _run_hook(CLOSE_HOOK, payload, env_overrides=env)
        assert result.returncode == 0
        assert self._get_decision(json.loads(result.stdout)) == "allow"
