# SPDX-License-Identifier: AGPL-3.0-or-later
"""Tests for the Stop verification hook, ``nexus.hooks.stop_verification``.

The Stop hook is advisory-only — it warns about uncommitted changes and open
beads but never blocks. Hard enforcement is the PreToolUse close gate's job.

RDR-215 bead nexus-q02nx.13 ported this hook from
``conexus/hooks/scripts/stop_verification_hook.sh``; bead .21 re-declared its
``hooks.json`` entry to the ``hook_stop_verification`` mcp_tool, so the bash
script no longer runs in production and this file drives the Python module
only (nexus-q02nx.21).
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

_MINIMAL_PATH = "/usr/bin:/bin"


def _make_payload(stop_hook_active: bool = False) -> str:
    return json.dumps({
        "session_id": "test-session",
        "hook_event_name": "Stop",
        "stop_hook_active": stop_hook_active,
    })


#: A child process, not an in-process call: these tests vary ``cwd`` and the
#: environment per case, and the hook reads both at call time. Driving it in
#: process would mean ``os.chdir`` and ``os.environ`` mutation, which are
#: process-global -- the same hazard that made the ledger's contention seams
#: environment variables rather than an in-process seam.
_PY_DRIVER = """
import json, sys
from nexus._hook_runtime._io import never_fail
from nexus.hooks import stop_verification

raw = sys.stdin.read()
try:
    payload = json.loads(raw) if raw.strip() else None
except Exception:
    payload = None
if not isinstance(payload, dict):
    payload = None
result = never_fail(lambda: stop_verification.run(payload), "stop_verification")
if result.stdout is not None:
    sys.stdout.write(result.stdout + "\\n")
sys.exit(result.exit_code)
"""


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
        [sys.executable, "-c", _PY_DRIVER],
        input=stdin,
        capture_output=True,
        text=True,
        timeout=60,
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
    """A real `.nexus.yml` the hook will really read.

    This used to write a FAKE ``read_verification_config.py`` under a
    stub ``CLAUDE_PLUGIN_ROOT`` and let the hook spawn it. Bead
    nexus-b5ugt ported that reader into the wheel
    (``nexus.hooks.verification_config``), so there is no script to
    substitute and no plugin root to point at -- which was the point:
    the hook reached a plugin script, an mcp_tool server has no usable
    ``CLAUDE_PLUGIN_ROOT``, and the config therefore read ``{}`` forever.

    Writing the real file and pointing ``CLAUDE_PROJECT_DIR`` at it is a
    stronger test than the stub was. The stub asserted that the hook
    would faithfully relay whatever JSON a script printed; this asserts
    that the hook reads the user's actual config format.
    """
    def _make(config: dict) -> dict[str, str]:
        project = tmp_path_factory.mktemp("project")
        body = "verification:\n" + "".join(
            f"  {k}: {json.dumps(v)}\n" for k, v in config.items()
        )
        (project / ".nexus.yml").write_text(body)
        return {"CLAUDE_PROJECT_DIR": str(project)}

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

    def test_exits_zero_always(self) -> None:
        assert _run_hook().returncode == 0

    def test_outputs_valid_json(self, mock_config_env) -> None:
        env = mock_config_env({"on_stop": False})
        parsed = json.loads(_run_hook(env_overrides=env).stdout)
        assert parsed["decision"] == "approve"

    def test_approve_when_on_stop_false(self, mock_config_env) -> None:
        env = mock_config_env({"on_stop": False})
        assert json.loads(_run_hook(env_overrides=env).stdout)["decision"] == "approve"

    def test_approve_when_there_is_no_config_at_all(self, tmp_path) -> None:
        """No `.nexus.yml` anywhere means DEFAULTS, and defaults approve.

        cwd is a bare temp directory, not a git checkout, on purpose:
        ``find_project_dir`` falls through CLAUDE_PROJECT_DIR to the cwd
        and then to the cwd's git COMMON dir, so running this from
        inside a worktree would find the primary checkout's real config
        and silently test the opposite case.
        """
        result = _run_hook(
            env_overrides={"CLAUDE_PROJECT_DIR": str(tmp_path)},
            cwd=tmp_path,
        )
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
