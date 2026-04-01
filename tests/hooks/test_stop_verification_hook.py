# SPDX-License-Identifier: AGPL-3.0-or-later
"""Tests for the Stop verification hook script.

The Stop hook is advisory-only — it warns about uncommitted changes and open
beads but never blocks. Hard enforcement is the PreToolUse close gate's job.
"""
from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[2] / "nx" / "hooks" / "scripts" / "stop_verification_hook.sh"

_MINIMAL_PATH = "/usr/bin:/bin"


def _make_payload(stop_hook_active: bool = False) -> str:
    return json.dumps({
        "session_id": "test-session",
        "hook_event_name": "Stop",
        "stop_hook_active": stop_hook_active,
    })


def _run_hook(
    stdin: str = "",
    *,
    env_overrides: dict[str, str] | None = None,
    cwd: str | Path | None = None,
) -> subprocess.CompletedProcess[str]:
    env = {
        **os.environ,
        "PATH": os.environ.get("PATH", ""),
        **(env_overrides or {}),
    }
    if not stdin:
        stdin = _make_payload()
    return subprocess.run(
        ["bash", str(SCRIPT)],
        input=stdin,
        capture_output=True,
        text=True,
        timeout=30,
        env=env,
        cwd=cwd,
    )


def _init_git_repo(path: Path) -> None:
    subprocess.run(["git", "init", str(path)], capture_output=True, check=True)
    subprocess.run(["git", "-C", str(path), "config", "user.email", "test@test.com"], capture_output=True, check=True)
    subprocess.run(["git", "-C", str(path), "config", "user.name", "Test"], capture_output=True, check=True)
    (path / "README.md").write_text("init\n")
    subprocess.run(["git", "-C", str(path), "add", "."], capture_output=True, check=True)
    subprocess.run(["git", "-C", str(path), "commit", "-m", "init"], capture_output=True, check=True)


@pytest.fixture
def mock_config_env(tmp_path_factory):
    plugin_root = tmp_path_factory.mktemp("plugin_root")
    scripts_dir = plugin_root / "hooks" / "scripts"
    scripts_dir.mkdir(parents=True)

    def _make(config: dict) -> dict[str, str]:
        script = scripts_dir / "read_verification_config.py"
        config_json = json.dumps(config)
        script.write_text(f"import json; print({repr(config_json)})\n")
        return {"CLAUDE_PLUGIN_ROOT": str(plugin_root)}

    return _make


@pytest.fixture
def clean_git_repo(tmp_path):
    _init_git_repo(tmp_path)
    return tmp_path


@pytest.fixture
def dirty_git_repo(tmp_path):
    _init_git_repo(tmp_path)
    (tmp_path / "README.md").write_text("modified\n")
    return tmp_path


class TestStopVerificationHook:
    """Stop verification hook — advisory only, never blocks."""

    def test_script_exists_and_is_executable(self) -> None:
        assert SCRIPT.exists()
        assert os.access(SCRIPT, os.X_OK)

    def test_exits_zero_always(self) -> None:
        assert _run_hook().returncode == 0

    def test_outputs_valid_json(self, mock_config_env) -> None:
        env = mock_config_env({"on_stop": False})
        parsed = json.loads(_run_hook(env_overrides=env).stdout)
        assert parsed["decision"] == "approve"

    def test_approve_when_on_stop_false(self, mock_config_env) -> None:
        env = mock_config_env({"on_stop": False})
        assert json.loads(_run_hook(env_overrides=env).stdout)["decision"] == "approve"

    def test_approve_when_config_reader_fails(self) -> None:
        result = _run_hook(env_overrides={"CLAUDE_PLUGIN_ROOT": "/nonexistent"})
        assert json.loads(result.stdout)["decision"] == "approve"

    def test_never_blocks(self, mock_config_env, dirty_git_repo) -> None:
        """Even with uncommitted changes, decision is always approve."""
        env = {**mock_config_env({"on_stop": True}), "PATH": _MINIMAL_PATH}
        result = _run_hook(
            stdin=_make_payload(),
            env_overrides=env,
            cwd=dirty_git_repo,
        )
        assert json.loads(result.stdout)["decision"] == "approve"

    def test_warns_on_uncommitted_changes(self, mock_config_env, dirty_git_repo) -> None:
        env = {**mock_config_env({"on_stop": True}), "PATH": _MINIMAL_PATH}
        result = _run_hook(
            stdin=_make_payload(),
            env_overrides=env,
            cwd=dirty_git_repo,
        )
        parsed = json.loads(result.stdout)
        assert parsed["decision"] == "approve"
        assert "uncommitted" in parsed.get("reason", "").lower()

    def test_no_warning_when_clean(self, mock_config_env, clean_git_repo) -> None:
        env = {**mock_config_env({"on_stop": True}), "PATH": _MINIMAL_PATH}
        result = _run_hook(
            stdin=_make_payload(),
            env_overrides=env,
            cwd=clean_git_repo,
        )
        parsed = json.loads(result.stdout)
        assert parsed["decision"] == "approve"
        assert "reason" not in parsed or not parsed.get("reason")

    def test_graceful_empty_stdin(self, mock_config_env) -> None:
        env = mock_config_env({"on_stop": False})
        result = _run_hook(stdin="", env_overrides=env)
        assert json.loads(result.stdout)["decision"] == "approve"
