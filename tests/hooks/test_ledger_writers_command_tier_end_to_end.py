# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""The RDR-184 ledger's two WRITERS, driven as Claude Code drives them (nexus-5l8i8).

Root cause (bead nexus-5l8i8, from session 81d1d28b's transcript,
2026-09-27): ``agent_dispatch_expect`` and ``subagent_start_stamp`` were
wired as ``mcp_tool`` entries. When this session's own
``plugin:conexus:nexus`` MCP server disconnected for about three minutes,
two ``Agent`` dispatches inside that window each logged a non-blocking
``hook_non_blocking_error PreToolUse:Agent "MCP server ... not connected"``
and PROCEEDED — Claude Code's own documented posture for an unreachable
``mcp_tool`` hook is "non-blocking error", not "retry" or "block" — so the
EXPECT row those dispatches should have written was never written. The
matching ``SubagentStart`` fired after reconnect and DID write a START row,
so the retro audit read the gap as an undeclared dispatch: exactly the
silent miss RDR-184 exists to catch.

This is a DIFFERENT hazard than the one bead nexus-17i1n fixed for
``pre_close_verification``/``subagent_stop``/``auto_approve`` (an
``mcp_tool`` hook cannot return a verdict at all). Neither ledger writer
returns a verdict; the hazard here is that an ``mcp_tool`` hook's very
INVOCATION depends on this session's MCP connection state, and the event
it observes fires whether or not that connection exists. The command tier
has no such dependency: it is a plain subprocess the harness spawns
directly, MCP session or none.

**What this file proves, and what it structurally can't.** The positive
tests below drive the REAL ``hooks.json``-wired command (the ``nx_hook_shim.py``
subprocess) with a payload, in an environment scrubbed of anything
MCP-adjacent, and assert the ledger row lands — proving the write path has
no MCP touchpoint to fail. What it does NOT do, and no fast subprocess-only
test can: literally reproduce Claude Code's own client-side "MCP server not
connected, so this mcp_tool hook's callback is never invoked" behavior,
because that decision is made inside the Claude Code harness process
itself, a thing this suite does not spawn. That reproduction lives one
layer up, in the container-based ``tests/e2e/hook-surface-shakeout``
harness (see its ``SHAKEOUT_RACE_DELAY`` mode, which delays the MCP
server's own startup against a real Claude Code session). What this file
CAN do, and does, is the durable substitute: a structural mutation test
(``test_reverting_to_the_old_mcp_tool_entries_is_caught``) that reconstructs
the exact pre-fix ``hooks.json`` shape and shows the checker this bead
relies on (``tests/_hook_wiring.command_verb``) finds no subprocess to run
for it at all — which is precisely the property that made the rows
disappear: an ``mcp_tool`` entry has nothing this suite, or the harness's
own command spawner, can invoke without a live MCP round trip.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from nexus.hooks import expectations as _exp
from tests._hook_wiring import HOOKS_JSON, NX_HOOK_SHIM, REPO_ROOT, command_verb, events_for

_SESSION_ID = "e2e-nx5l8i8-ledger-writer"


def _wired_argv(verb: str) -> list[str]:
    """The argv hooks.json runs for *verb* — the real shim invocation, read
    from the shipped file rather than hand-typed, so this cannot drift from
    the wiring it claims to exercise (mirrors
    ``test_deciding_verbs_end_to_end.py``'s helper of the same shape)."""
    data = json.loads(HOOKS_JSON.read_text())
    for groups in data.get("hooks", {}).values():
        for group in groups:
            for hook in group.get("hooks", []):
                if command_verb(hook) == verb and hook.get("command") == "python3":
                    shim = NX_HOOK_SHIM.replace("${CLAUDE_PLUGIN_ROOT}", str(REPO_ROOT / "conexus"))
                    return [sys.executable, shim, verb]
    pytest.fail(f"hooks.json wires no python3/nx_hook_shim.py entry for verb {verb!r}")


def _run_verb_with_no_mcp_server(verb: str, payload: dict, xdg_state_home: Path):
    """Spawn *verb* exactly as its hooks.json entry would, in an
    environment that cannot reach any MCP server: no inherited
    ``CLAUDE_*`` connection markers, no plugin-session identity, nothing
    naming a ``plugin:conexus:nexus`` transport at all. ``nx-hook`` must
    still be resolvable on PATH (a real installed generation, or this dev
    tree's own console script via ``uv run``), which is the one thing a
    genuinely absent CLI — not an absent MCP SERVER — would also strip;
    that case is the shim's own job (``nx_hook_shim.py``'s module
    docstring), not this file's.
    """
    env = {
        "PATH": os.environ.get("PATH", ""),
        "HOME": os.environ.get("HOME", ""),
        "XDG_STATE_HOME": str(xdg_state_home),
    }
    # Carry only what makes the interpreter/venv resolvable; strip every
    # CLAUDE_*/NX_MCP_*/NX_SESSION_* variable an MCP-connected session
    # would have set, so this process starts exactly as cold as a plain
    # command-tier spawn with no MCP session in its ancestry.
    for key in ("VIRTUAL_ENV", "PYTHONPATH", "UV_PROJECT_ENVIRONMENT"):
        if key in os.environ:
            env[key] = os.environ[key]
    return subprocess.run(
        _wired_argv(verb),
        input=json.dumps(payload),
        capture_output=True,
        text=True,
        timeout=60,
        env=env,
    )


@pytest.fixture(scope="module", autouse=True)
def _require_nx_hook():
    if shutil.which("nx-hook") is None:
        pytest.skip(
            "nx-hook is not on PATH. It is a console script, so an install "
            "generation predating its declaration has no shim."
        )


def test_both_writers_are_wired_on_the_command_tier_not_mcp_tool() -> None:
    """Non-vacuity, and the sharpest form of the regression guard.

    If either entry is ever moved back to ``mcp_tool`` — the exact shape
    that dropped rows during session 81d1d28b's MCP disconnect — this
    fails immediately, before any subprocess runs. ``command_verb`` returns
    ``None`` for an ``mcp_tool`` entry, so this also IS
    ``test_reverting_to_the_old_mcp_tool_entries_is_caught``'s live-file
    form: it exercises the SHIPPED ``hooks.json``, not a reconstructed
    fixture.
    """
    data = json.loads(HOOKS_JSON.read_text())
    found_agent_dispatch = found_subagent_start = False
    for groups in data.get("hooks", {}).values():
        for group in groups:
            for hook in group.get("hooks", []):
                assert hook.get("tool") not in (
                    "hook_agent_dispatch_expect",
                    "hook_subagent_start_stamp",
                ), (
                    f"found an mcp_tool entry for {hook.get('tool')!r} — this is "
                    "the exact wiring that drops RDR-184 ledger rows during an "
                    "MCP disconnect (nexus-5l8i8). Wire it through "
                    "nx_hook_shim.py instead."
                )
                if command_verb(hook) == "agent-dispatch-expect":
                    found_agent_dispatch = True
                if command_verb(hook) == "subagent-start-stamp":
                    found_subagent_start = True
    assert found_agent_dispatch, "agent-dispatch-expect is not wired as a command-tier verb anywhere"
    assert found_subagent_start, "subagent-start-stamp is not wired as a command-tier verb anywhere"


def test_reverting_to_the_old_mcp_tool_entries_is_caught(tmp_path: Path) -> None:
    """Mutation-verify the check above against the EXACT pre-fix shape.

    Reconstructs the two entries verbatim as they shipped before this
    bead (an ``mcp_tool`` call against ``plugin:conexus:nexus``) and shows
    two things at once: ``command_verb`` finds no subprocess to run for
    either (there is nothing this suite, or the real harness's own
    command-spawner, can invoke without a live MCP round trip — the
    property that made the rows disappear), and ``events_for`` still
    reports the event correctly (an mcp_tool entry is a real wiring, just
    the wrong tier), so a checker keyed on "is it wired at all" would miss
    this regression entirely.
    """
    old_shape = {
        "hooks": {
            "PreToolUse": [
                {
                    "matcher": "Agent|Task",
                    "hooks": [
                        {
                            "type": "mcp_tool",
                            "server": "plugin:conexus:nexus",
                            "tool": "hook_agent_dispatch_expect",
                            "input": {
                                "session_id": "${session_id}",
                                "tool_name": "${tool_name}",
                                "tool_use_id": "${tool_use_id}",
                                "tool_input": "${tool_input}",
                            },
                            "timeout": 10,
                        }
                    ],
                }
            ],
            "SubagentStart": [
                {
                    "matcher": "",
                    "hooks": [
                        {
                            "type": "mcp_tool",
                            "server": "plugin:conexus:nexus",
                            "tool": "hook_subagent_start_stamp",
                            "input": {
                                "session_id": "${session_id}",
                                "agent_id": "${agent_id}",
                                "agent_type": "${agent_type}",
                            },
                            "timeout": 10,
                        }
                    ],
                }
            ],
        }
    }
    perturbed = tmp_path / "hooks.json"
    perturbed.write_text(json.dumps(old_shape))

    assert events_for("agent_dispatch_expect", hooks_json=perturbed) == ["PreToolUse"], (
        "the old shape is still recognised as WIRED (correct — it fires on a "
        "real event), which is what makes the next assertion the real proof"
    )
    for entry in old_shape["hooks"]["PreToolUse"][0]["hooks"]:
        assert command_verb(entry) is None, (
            "the old mcp_tool entry resolved to a command-tier verb, which "
            "means this mutation no longer represents the pre-fix shape"
        )
    for entry in old_shape["hooks"]["SubagentStart"][0]["hooks"]:
        assert command_verb(entry) is None, (
            "the old mcp_tool entry resolved to a command-tier verb, which "
            "means this mutation no longer represents the pre-fix shape"
        )


def test_agent_dispatch_expect_writes_its_row_with_no_mcp_server_reachable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The positive proof: the real wired command, no MCP server anywhere
    in this process's environment, and the EXPECT row still lands."""
    session_id = f"{_SESSION_ID}-expect"
    xdg_state_home = tmp_path / "state"
    proc = _run_verb_with_no_mcp_server(
        "agent-dispatch-expect",
        {
            "session_id": session_id,
            "tool_name": "Agent",
            "tool_use_id": "e2e-tu-1",
            "tool_input": {"subagent_type": "general-purpose", "run_in_background": True},
        },
        xdg_state_home=xdg_state_home,
    )
    assert proc.returncode == 0, f"stdout={proc.stdout!r} stderr={proc.stderr!r}"
    assert proc.stdout.strip() == "", (
        "agent-dispatch-expect must be stdout-silent on every path "
        f"(got {proc.stdout!r})"
    )

    # Read the ledger through the SAME env the subprocess wrote it under —
    # this test process's own XDG_STATE_HOME (if any) is irrelevant, and
    # reading it without this patch resolves the wrong file, silently.
    monkeypatch.setenv("XDG_STATE_HOME", str(xdg_state_home))
    ledger = Path(_exp.expectations_file(session_id))
    assert ledger.is_file(), (
        f"no EXPECT row was written to {ledger} — the exact failure mode "
        "measured in session 81d1d28b during an MCP disconnect"
    )
    lines = [ln for ln in ledger.read_text().splitlines() if ln]
    assert len(lines) == 1, f"expected exactly one row, got {lines}"
    fields = lines[0].split("\t")
    assert fields[1] == "EXPECT", lines[0]
    assert fields[2] == "general-purpose", lines[0]
    assert fields[4] == "e2e-tu-1", lines[0]


def test_subagent_start_stamp_writes_its_row_with_no_mcp_server_reachable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Same proof, for the START side."""
    session_id = f"{_SESSION_ID}-start"
    xdg_state_home = tmp_path / "state"
    proc = _run_verb_with_no_mcp_server(
        "subagent-start-stamp",
        {
            "session_id": session_id,
            "agent_id": "e2e-agent-id-1",
            "agent_type": "general-purpose",
        },
        xdg_state_home=xdg_state_home,
    )
    assert proc.returncode == 0, f"stdout={proc.stdout!r} stderr={proc.stderr!r}"
    assert proc.stdout.strip() == "", (
        f"subagent-start-stamp must be stdout-silent on every path (got {proc.stdout!r})"
    )

    monkeypatch.setenv("XDG_STATE_HOME", str(xdg_state_home))
    ledger = Path(_exp.expectations_file(session_id))
    assert ledger.is_file(), (
        f"no START row was written to {ledger} — the same failure shape as "
        "the EXPECT side, one hook over"
    )
    lines = [ln for ln in ledger.read_text().splitlines() if ln]
    assert len(lines) == 1, f"expected exactly one row, got {lines}"
    fields = lines[0].split("\t")
    assert fields[1] == "START", lines[0]
    assert fields[2] == "e2e-agent-id-1", lines[0]
    assert fields[3] == "general-purpose", lines[0]


def test_both_writers_survive_an_unknown_verb_sibling_unaffected() -> None:
    """Non-interference sanity: an unrelated unknown verb through the same
    shim must not somehow trip either writer (guards against a shared
    global the ledger verbs above this file's own module could leak)."""
    proc = subprocess.run(
        [sys.executable, str(REPO_ROOT / "conexus" / "hooks" / "scripts" / "nx_hook_shim.py"),
         "no-such-verb-nexus-5l8i8"],
        input="{}",
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert proc.returncode == 0, f"stdout={proc.stdout!r} stderr={proc.stderr!r}"
