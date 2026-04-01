# SPDX-License-Identifier: AGPL-3.0-or-later
"""Tests for the Stop verification hook script."""
from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[2] / "nx" / "hooks" / "scripts" / "stop_verification_hook.sh"


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
    """Create a mock CLAUDE_PLUGIN_ROOT with a fake read_verification_config.py.

    Uses tmp_path_factory so the plugin root is always a separate directory from
    any git repo fixtures (which also use tmp_path), preventing cross-contamination.
    """
    plugin_root = tmp_path_factory.mktemp("plugin_root")
    scripts_dir = plugin_root / "hooks" / "scripts"
    scripts_dir.mkdir(parents=True)

    def _make(config: dict) -> dict[str, str]:
        script = scripts_dir / "read_verification_config.py"
        # Embed as a JSON string literal — avoids Python True/False mismatch
        config_json = json.dumps(config)
        script.write_text(
            f"import json; print({repr(config_json)})\n"
        )
        return {"CLAUDE_PLUGIN_ROOT": str(plugin_root)}

    return _make


@pytest.fixture
def clean_git_repo(tmp_path):
    """A clean git repo with one commit and no uncommitted changes."""
    _init_git_repo(tmp_path)
    return tmp_path


@pytest.fixture
def dirty_git_repo(tmp_path):
    """A git repo with an uncommitted modification."""
    _init_git_repo(tmp_path)
    (tmp_path / "README.md").write_text("modified\n")
    return tmp_path


# Path that has neither git nor bd nor any test tooling
_MINIMAL_PATH = "/usr/bin:/bin"


class TestStopVerificationHook:
    """Stop verification hook script tests."""

    def test_script_exists_and_is_executable(self) -> None:
        assert SCRIPT.exists(), f"Script not found: {SCRIPT}"
        assert os.access(SCRIPT, os.X_OK), f"Script not executable: {SCRIPT}"

    def test_exits_zero_always(self) -> None:
        result = _run_hook()
        assert result.returncode == 0

    def test_outputs_valid_json(self, mock_config_env) -> None:
        env = mock_config_env({"on_stop": False})
        result = _run_hook(env_overrides=env)
        assert result.returncode == 0
        parsed = json.loads(result.stdout)
        assert "decision" in parsed

    def test_approve_when_on_stop_false(self, mock_config_env) -> None:
        env = mock_config_env({"on_stop": False})
        result = _run_hook(env_overrides=env)
        parsed = json.loads(result.stdout)
        assert parsed["decision"] == "approve"

    def test_approve_when_config_reader_fails(self) -> None:
        """When CLAUDE_PLUGIN_ROOT points nowhere, safe default is approve."""
        result = _run_hook(env_overrides={"CLAUDE_PLUGIN_ROOT": "/nonexistent/path/xyz"})
        assert result.returncode == 0
        parsed = json.loads(result.stdout)
        assert parsed["decision"] == "approve"

    def test_first_pass_blocks_on_uncommitted_changes(self, mock_config_env, dirty_git_repo) -> None:
        """Uncommitted changes in git repo → block on first pass."""
        env = {
            **mock_config_env({
                "on_stop": True,
                "test_command": "",
                "test_timeout": 10,
            }),
            # Remove bd from PATH to avoid false open-bead failures
            "PATH": _MINIMAL_PATH,
        }
        result = _run_hook(
            stdin=_make_payload(stop_hook_active=False),
            env_overrides=env,
            cwd=dirty_git_repo,
        )
        assert result.returncode == 0
        parsed = json.loads(result.stdout)
        assert parsed["decision"] == "block"
        assert "uncommitted" in parsed["reason"].lower()

    def test_first_pass_approve_when_clean(self, mock_config_env, clean_git_repo) -> None:
        """Clean git repo + passing test command → approve."""
        env = {
            **mock_config_env({
                "on_stop": True,
                "test_command": "true",
                "test_timeout": 10,
            }),
            "PATH": f"/usr/bin:/bin:{os.environ.get('PATH', '')}",
        }
        result = _run_hook(
            stdin=_make_payload(stop_hook_active=False),
            env_overrides=env,
            cwd=clean_git_repo,
        )
        assert result.returncode == 0
        parsed = json.loads(result.stdout)
        assert parsed["decision"] == "approve"

    def test_first_pass_blocks_on_test_failure(self, mock_config_env, clean_git_repo) -> None:
        """test_command that always fails → block on first pass."""
        env = {
            **mock_config_env({
                "on_stop": True,
                "test_command": "false",
                "test_timeout": 10,
            }),
            "PATH": _MINIMAL_PATH,
        }
        result = _run_hook(
            stdin=_make_payload(stop_hook_active=False),
            env_overrides=env,
            cwd=clean_git_repo,
        )
        assert result.returncode == 0
        parsed = json.loads(result.stdout)
        assert parsed["decision"] == "block"
        assert "test" in parsed["reason"].lower() or "exit" in parsed["reason"].lower()

    def test_retry_lets_test_failures_through(self, mock_config_env, clean_git_repo) -> None:
        """On retry (stop_hook_active=true), test failures → approve with warning."""
        env = {
            **mock_config_env({
                "on_stop": True,
                "test_command": "false",
                "test_timeout": 10,
            }),
            "PATH": _MINIMAL_PATH,
        }
        result = _run_hook(
            stdin=_make_payload(stop_hook_active=True),
            env_overrides=env,
            cwd=clean_git_repo,
        )
        assert result.returncode == 0
        parsed = json.loads(result.stdout)
        assert parsed["decision"] == "approve"
        assert "warning" in parsed.get("reason", "").upper() or "TESTS FAILING" in parsed.get("reason", "")

    def test_retry_blocks_mechanical_failures(self, mock_config_env, dirty_git_repo) -> None:
        """On retry, git uncommitted changes still block."""
        env = {
            **mock_config_env({
                "on_stop": True,
                "test_command": "",
                "test_timeout": 10,
            }),
            "PATH": _MINIMAL_PATH,
        }
        result = _run_hook(
            stdin=_make_payload(stop_hook_active=True),
            env_overrides=env,
            cwd=dirty_git_repo,
        )
        assert result.returncode == 0
        parsed = json.loads(result.stdout)
        assert parsed["decision"] == "block"

    def test_command_not_found_is_advisory(self, mock_config_env, clean_git_repo) -> None:
        """Unknown test command (exit 127) → approve with advisory, never block."""
        env = {
            **mock_config_env({
                "on_stop": True,
                "test_command": "nonexistent_command_xyz_999",
                "test_timeout": 10,
            }),
            "PATH": _MINIMAL_PATH,
        }
        result = _run_hook(
            stdin=_make_payload(stop_hook_active=False),
            env_overrides=env,
            cwd=clean_git_repo,
        )
        assert result.returncode == 0
        parsed = json.loads(result.stdout)
        assert parsed["decision"] == "approve"

    def test_timeout_is_advisory(self, mock_config_env, clean_git_repo) -> None:
        """Test command that times out → approve with advisory, never block."""
        env = {
            **mock_config_env({
                "on_stop": True,
                "test_command": "sleep 999",
                "test_timeout": 1,
            }),
            "PATH": f"/usr/bin:/bin:{os.environ.get('PATH', '')}",
        }
        result = _run_hook(
            stdin=_make_payload(stop_hook_active=False),
            env_overrides=env,
            cwd=clean_git_repo,
        )
        assert result.returncode == 0
        parsed = json.loads(result.stdout)
        assert parsed["decision"] == "approve"

    def test_empty_test_command_skips_test_check(self, mock_config_env, clean_git_repo) -> None:
        """Empty test_command → skip test check entirely, approve with advisory."""
        env = {
            **mock_config_env({
                "on_stop": True,
                "test_command": "",
                "test_timeout": 10,
            }),
            "PATH": _MINIMAL_PATH,
        }
        result = _run_hook(
            stdin=_make_payload(stop_hook_active=False),
            env_overrides=env,
            cwd=clean_git_repo,
        )
        assert result.returncode == 0
        parsed = json.loads(result.stdout)
        # Advisory-only failures → approve
        assert parsed["decision"] == "approve"

    def test_graceful_empty_stdin(self, mock_config_env) -> None:
        """Empty stdin → safe default (approve)."""
        env = mock_config_env({"on_stop": False})
        result = _run_hook(stdin="", env_overrides=env)
        assert result.returncode == 0
        parsed = json.loads(result.stdout)
        assert parsed["decision"] == "approve"
