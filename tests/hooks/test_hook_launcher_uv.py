# SPDX-License-Identifier: AGPL-3.0-or-later
"""Every uv-launched conexus hook entry runs, exactly as hooks.json declares it.

nexus-efk2h (RDR-224 Phase 4): the plugin-resident hooks were launched as
``python3``, which is not on PATH on stock Windows, so they never fired there.
They now launch through ``uv run --no-project --no-config --quiet``, and each
script's PEP 723 block carries the 3.12 floor. ``tests/test_hooks_json_shape_lint.py``
pins the SHAPE and the blocks; this file
runs the real argv, with the plugin root substituted, and checks the outcome:

* the scripts really ran under an interpreter that satisfies their 3.12 floor
  (below it a script prints "requires Python 3.12+" and exits 1, which Claude
  Code treats as a non-blocking error, so a governance gate would fail open);
* a deny stays a deny and an allow stays an allow through the launcher, with
  stdin and stdout passed through byte for byte.

The version-lockstep entry is not run: it dispatches a detached upgrade.
tests/hooks/test_lockstep_survives_cli_skew.py covers its shape and imports.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess

import pytest

from tests._hook_wiring import HOOKS_JSON, REPO_ROOT, launcher_script_args

_PLUGIN_ROOT = str(REPO_ROOT / "conexus")

_CRED_READ = {
    "hook_event_name": "PreToolUse",
    "tool_name": "Bash",
    "tool_input": {"command": "cat ~/.claude/.credentials.json"},
    "session_id": "launcher-test",
    "cwd": "/tmp",
}


def _launcher_entries() -> list[tuple[str, dict]]:
    data = json.loads(HOOKS_JSON.read_text())
    return [
        (event, hook)
        for event, groups in data["hooks"].items()
        for group in groups
        for hook in group.get("hooks", [])
        if launcher_script_args(hook)
    ]


def _argv(hook: dict) -> list[str]:
    return [
        hook["command"],
        *(a.replace("${CLAUDE_PLUGIN_ROOT}", _PLUGIN_ROOT) for a in hook["args"]),
    ]


def _run(hook: dict, payload: dict) -> subprocess.CompletedProcess[str]:
    # A scrubbed env: no VIRTUAL_ENV, no PYTHON*, only what a hook has anyway.
    env = {"PATH": os.environ["PATH"], "HOME": os.environ.get("HOME", "")}
    return subprocess.run(
        _argv(hook), input=json.dumps(payload), capture_output=True, text=True,
        timeout=120, env=env, cwd="/tmp",
    )


def _script(hook: dict) -> str:
    return os.path.basename(launcher_script_args(hook)[0])


@pytest.fixture(scope="module", autouse=True)
def _require_uv() -> None:
    assert shutil.which("uv"), "uv is not on PATH; it is the launcher under test, so this is a failure, not a skip"


def test_the_walk_finds_the_launcher_entries() -> None:
    """Non-vacuity: seven entries launch through uv (four scripts, three shim)."""
    entries = _launcher_entries()
    assert len(entries) == 7, [(e, _script(h)) for e, h in entries]
    assert {_script(h) for _, h in entries} == {
        "version_lockstep_hook.py",
        "nx_hook_shim.py",
        "subagent_git_write_requires_orchestrator.py",
        "credential_print_guard.py",
        "mailbox_drain.py",
    }


def test_the_credential_guard_still_denies_through_the_launcher() -> None:
    (hook,) = [h for _, h in _launcher_entries() if _script(h) == "credential_print_guard.py"]
    proc = _run(hook, _CRED_READ)
    assert proc.returncode == 0, proc.stderr
    assert "requires Python 3.12" not in proc.stderr
    verdict = json.loads(proc.stdout)["hookSpecificOutput"]
    assert verdict["permissionDecision"] == "deny", proc.stdout


@pytest.mark.parametrize(
    "script",
    ["subagent_git_write_requires_orchestrator.py", "credential_print_guard.py"],
)
def test_the_bash_gates_run_and_allow_a_benign_command(script: str) -> None:
    (hook,) = [h for _, h in _launcher_entries() if _script(h) == script]
    payload = {**_CRED_READ, "tool_input": {"command": "ls"}}
    proc = _run(hook, payload)
    assert proc.returncode == 0, proc.stderr
    assert "requires Python 3.12" not in proc.stderr
    assert "deny" not in proc.stdout


def test_the_mailbox_drain_runs_under_the_floor_and_fails_open() -> None:
    (hook,) = [h for _, h in _launcher_entries() if _script(h) == "mailbox_drain.py"]
    proc = _run(
        hook,
        {"hook_event_name": "UserPromptSubmit", "prompt": "hi", "session_id": "launcher-test", "cwd": "/tmp"},
    )
    assert proc.returncode == 0, proc.stderr
    assert "requires Python 3.12" not in proc.stderr


@pytest.mark.skipif(shutil.which("nx-hook") is None, reason="needs the installed nx-hook the shim wraps")
@pytest.mark.parametrize("event", ["PreToolUse", "PermissionRequest"])
def test_auto_approve_allows_through_uv_and_the_shim(event: str) -> None:
    (hook,) = [
        h for e, h in _launcher_entries()
        if e == event and launcher_script_args(h)[-1] == "auto-approve"
    ]
    proc = _run(
        hook,
        {"hook_event_name": event, "tool_name": "mcp__plugin_conexus_nexus__search", "session_id": "launcher-test"},
    )
    assert proc.returncode == 0, proc.stderr
    assert "allow" in proc.stdout, proc.stdout
