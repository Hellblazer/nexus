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
    """Two invocations for one agent_id must yield exactly one START row."""
    _run(_payload(), tmp_path, mode="observe")
    _run(_payload(), tmp_path, mode="observe")
    content = _expfile(tmp_path).read_text()
    assert content.count(f"\tSTART\t{AGENT_ID}\t") == 1


def test_idempotent_under_CONCURRENT_invocation(tmp_path: Path) -> None:
    """The shape that actually broke (nexus-3h0u6).

    The sequential test above passed throughout, while production wrote
    duplicate START rows with identical timestamps — because the guard was a
    check-then-append and the two registrations fired at the same instant.
    Sequential invocation cannot reproduce a TOCTOU, so it certified nothing
    about the case the guard existed for. This one launches the invocations
    simultaneously.
    """
    import concurrent.futures

    payload = _payload()
    with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
        futures = [
            pool.submit(_run, payload, tmp_path, mode="observe") for _ in range(8)
        ]
        for f in futures:
            assert f.result().returncode == 0

    content = _expfile(tmp_path).read_text()
    assert content.count(f"\tSTART\t{AGENT_ID}\t") == 1, (
        f"8 concurrent stamps must compose to ONE row, got:\n{content}"
    )


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


def test_orchestration_hooks_are_registered_exactly_once() -> None:
    """ONE registration surface, mechanically enforced (nexus-3h0u6).

    RDR-184 .16 armed repo sessions from .claude/settings.json as interim
    instrumentation, explicitly "without waiting for a plugin release", and
    relied on the stamp's per-agent_id idempotence to make the two surfaces
    "compose to one row". They did not: the guard was racy, and once the
    plugin release shipped (conexus 6.14.0 registers both hooks) every
    hook-written START and REPORTED row was duplicated — inflating the
    nexus-ccs9v.11 census 2x on hook rows against 1x EXPECT rows, which is
    precisely the measurement that bead exists to take.

    The stamp is now genuinely atomic, but single-registration is the real
    guarantee, because REPORTED has no idempotence at all and legitimately
    repeats across multiple stops — nothing could dedupe it per event. So
    the invariant is enforced here rather than left to convention.

    Behaviour is unchanged by removal: both scripts default
    NX_ORCH_STOP_GUARD to block internally (the P1.G flip, .15), and
    NX_ORCH_STOP_GUARD=off still opts out per session.
    """
    settings = json.loads((REPO_ROOT / ".claude" / "settings.json").read_text())
    project_cmds = [
        h.get("command", "")
        for event in settings.get("hooks", {}).values()
        for entry in event
        for h in entry.get("hooks", [])
    ]
    for script in ("subagent-start-stamp.sh", "subagent-stop.sh"):
        offenders = [c for c in project_cmds if script in c]
        assert not offenders, (
            f"{script} is registered in BOTH conexus/hooks/hooks.json and "
            f".claude/settings.json; both fire, so every row it writes is "
            f"doubled and the .11 census is corrupted. Remove the project-"
            f"settings copy — the plugin already ships it. Found: {offenders}"
        )

    hooks = json.loads((REPO_ROOT / "conexus" / "hooks" / "hooks.json").read_text())
    plugin_cmds = [
        h.get("command", "")
        for event in hooks["hooks"].values()
        for entry in event
        for h in entry.get("hooks", [])
    ]
    for script in ("subagent-start-stamp.sh", "subagent-stop.sh"):
        assert sum(script in c for c in plugin_cmds) == 1, (
            f"{script} must be registered exactly once in the plugin"
        )
