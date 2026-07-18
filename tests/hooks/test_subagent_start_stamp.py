# SPDX-License-Identifier: AGPL-3.0-or-later
"""Tests for the SubagentStart expectations-stamp hook (RDR-184 .16 wiring,
bead nexus-ccs9v.16 — the START-row dispatch record the declaration-
completeness retro audit diffs against EXPECT rows).

Contract: mode-gated (writes ONLY when NX_ORCH_STOP_GUARD is observe|block),
idempotent per agent_id (plugin + project-settings double registration must
compose to one row), and STDOUT-SILENT (SubagentStart stdout injects context
into the spawned subagent — the stamp must never add context).
"""
from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPT = REPO_ROOT / "conexus" / "hooks" / "scripts" / "subagent-start-stamp.sh"

SESSION = "sess-stamp"
AGENT_ID = "aworker-s-1234567890abcdef"


def _payload(**overrides) -> str:
    base = {
        "session_id": SESSION,
        "hook_event_name": "SubagentStart",
        "agent_id": AGENT_ID,
        "agent_type": "worker-s",
        "prompt_id": "p1",
    }
    base.update(overrides)
    return json.dumps(base)


def _run(stdin: str, tmp_path: Path, *, mode: str | None) -> subprocess.CompletedProcess[str]:
    env = {**os.environ, "XDG_STATE_HOME": str(tmp_path / "state")}
    env.pop("NX_ORCH_STOP_GUARD", None)
    if mode is not None:
        env["NX_ORCH_STOP_GUARD"] = mode
    return subprocess.run(
        ["bash", str(SCRIPT)],
        input=stdin, capture_output=True, text=True, timeout=30, env=env,
    )


def _expfile(tmp_path: Path) -> Path:
    return tmp_path / "state" / "nexus" / "orchestration" / f"{SESSION}.expectations"


def test_unset_mode_stamps(tmp_path: Path) -> None:
    """DEFAULT-ON (P1.G flipped 2026-07-17): unset mode stamps, matching
    subagent-stop.sh's block default."""
    proc = _run(_payload(), tmp_path, mode=None)
    assert proc.returncode == 0
    assert proc.stdout == ""
    assert f"\tSTART\t{AGENT_ID}\t" in _expfile(tmp_path).read_text()


def test_explicit_off_writes_nothing(tmp_path: Path) -> None:
    proc = _run(_payload(), tmp_path, mode="off")
    assert proc.returncode == 0
    assert proc.stdout == ""
    assert not _expfile(tmp_path).exists()


def test_observe_mode_stamps_start_row(tmp_path: Path) -> None:
    proc = _run(_payload(), tmp_path, mode="observe")
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout == "", "stamp leaked stdout — SubagentStart stdout injects subagent context"
    content = _expfile(tmp_path).read_text()
    assert f"\tSTART\t{AGENT_ID}\tworker-s\n" in content


def test_block_mode_stamps_too(tmp_path: Path) -> None:
    proc = _run(_payload(), tmp_path, mode="block")
    assert proc.returncode == 0
    assert f"\tSTART\t{AGENT_ID}\t" in _expfile(tmp_path).read_text()


def test_idempotent_per_agent_id(tmp_path: Path) -> None:
    """Plugin hooks.json + project settings.json can BOTH register the stamp
    in a repo session — two invocations must yield exactly one START row."""
    _run(_payload(), tmp_path, mode="observe")
    _run(_payload(), tmp_path, mode="observe")
    content = _expfile(tmp_path).read_text()
    assert content.count(f"\tSTART\t{AGENT_ID}\t") == 1


def test_distinct_agents_each_get_a_row(tmp_path: Path) -> None:
    _run(_payload(), tmp_path, mode="observe")
    _run(_payload(agent_id="aother-1234", agent_type="other"), tmp_path, mode="observe")
    content = _expfile(tmp_path).read_text()
    assert content.count("\tSTART\t") == 2


def test_junk_stdin_writes_nothing(tmp_path: Path) -> None:
    proc = _run("not json {", tmp_path, mode="observe")
    assert proc.returncode == 0
    assert proc.stdout == ""
    assert not _expfile(tmp_path).exists()


def test_registered_in_plugin_hooks_json() -> None:
    hooks = json.loads((REPO_ROOT / "conexus" / "hooks" / "hooks.json").read_text())
    commands = [
        h["command"]
        for entry in hooks["hooks"].get("SubagentStart", [])
        for h in entry.get("hooks", [])
    ]
    assert any("subagent-start-stamp.sh" in c for c in commands)


def test_project_settings_arm_block_by_default() -> None:
    """Option-(b) instrumentation (bead .11): repo sessions run the stop
    hook + stamp from project settings, defaulting to block since the
    P1.G flip (env can override), via $CLAUDE_PROJECT_DIR."""
    settings = json.loads((REPO_ROOT / ".claude" / "settings.json").read_text())
    stop_cmds = [
        h["command"]
        for entry in settings["hooks"]["SubagentStop"]
        for h in entry["hooks"]
    ]
    start_cmds = [
        h["command"]
        for entry in settings["hooks"]["SubagentStart"]
        for h in entry["hooks"]
    ]
    assert any(
        "subagent-stop.sh" in c and "${NX_ORCH_STOP_GUARD:-block}" in c and "$CLAUDE_PROJECT_DIR" in c
        for c in stop_cmds
    )
    assert any(
        "subagent-start-stamp.sh" in c and "${NX_ORCH_STOP_GUARD:-block}" in c and "$CLAUDE_PROJECT_DIR" in c
        for c in start_cmds
    )
