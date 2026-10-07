# SPDX-License-Identifier: AGPL-3.0-or-later
"""Every uv-launched conexus hook entry runs, exactly as hooks.json declares it.

nexus-efk2h (RDR-224 Phase 4): the plugin-resident hooks were launched as
``python3``, which is not on PATH on stock Windows, so they never fired there.
They now launch through ``uv tool run ... --python >=3.12 python`` (the argv is
``tests/_hook_wiring.UV_LAUNCHER_ARGV``), which carries the 3.12 floor. ``tests/test_hooks_json_shape_lint.py``
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


# -- the known blocking failure (RDR-224 review finding B, nexus-f9bgu.36) -----------

#: Events on which Claude Code treats a hook's exit 2 as BLOCKING: the tool call,
#: the permission request, or the prompt is refused.
_BLOCKING_EVENTS = {"PreToolUse", "PermissionRequest", "UserPromptSubmit"}


def _no_interpreter_env(tmp_path) -> dict[str, str]:
    """uv alone on PATH, an empty managed-Python directory, downloads off, offline:
    a box where uv can find no interpreter and cannot fetch one."""
    bindir = tmp_path / "bin"
    bindir.mkdir()
    (bindir / "uv").symlink_to(shutil.which("uv"))
    pydir = tmp_path / "py"
    pydir.mkdir()
    return {
        "PATH": str(bindir),
        "HOME": str(tmp_path),
        "UV_PYTHON_INSTALL_DIR": str(pydir),
        "UV_PYTHON_DOWNLOADS": "never",
        "UV_OFFLINE": "1",
        "UV_CACHE_DIR": str(tmp_path / "cache"),
    }


def test_uv_with_no_interpreter_exits_2_which_blocks_five_entries(tmp_path) -> None:
    """MEASURED, not fixed: hooks.json launches in exec form (``args`` set, so no
    shell), and an exec-form entry cannot map a launcher failure to a non-blocking
    code. With no Python 3.12+ findable and none fetchable (offline, proxied,
    air-gapped), ``uv run`` exits 2; Claude Code reads exit 2 as a block on
    PreToolUse, PermissionRequest and UserPromptSubmit. The test pins that premise
    so a uv release that changes the code, or a hooks.json change that moves an
    entry across the blocking line, is noticed; conexus/README.md documents the
    remedy and the finding's record (nexus_rdr/224-phase4-fix-A-B) the options."""
    env = _no_interpreter_env(tmp_path)
    entries = _launcher_entries()
    blocking = [(e, h) for e, h in entries if e in _BLOCKING_EVENTS]
    assert len(blocking) == 5, [(e, _script(h)) for e, h in blocking]
    # The plugin root is a copy outside the repo: `--directory <plugin root>` makes uv
    # discover interpreters from there, and the repo's own .venv above conexus/ would
    # supply one.
    root = tmp_path / "plugin"
    shutil.copytree(REPO_ROOT / "conexus" / "hooks", root / "hooks")
    for event, hook in entries:
        argv = [
            hook["command"],
            *(a.replace("${CLAUDE_PLUGIN_ROOT}", str(root)) for a in hook["args"]),
        ]
        proc = subprocess.run(
            argv, input="{}", capture_output=True, text=True, timeout=60,
            env=env, cwd=str(tmp_path),
        )
        assert proc.returncode == 2, (event, _script(hook), proc.returncode, proc.stderr)
        assert "No interpreter found" in proc.stderr, (event, _script(hook), proc.stderr)


def test_the_readme_documents_the_blocking_failure_and_its_remedy() -> None:
    text = (REPO_ROOT / "conexus" / "README.md").read_text()
    assert "exits 2" in text and "uv python install 3.12" in text, (
        "conexus/README.md must say that uv exits 2 when it finds no Python and cannot "
        "fetch one, that this blocks, and the remedy"
    )
