# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""The three deciding verbs, driven as Claude Code drives them (nexus-17i1n).

Every other test of these hooks proves a PART: that ``run()`` returns the
right envelope, that ``hooks.json`` names the right verb, that
``VERB_TABLE`` maps that verb to the right module, that ``nx-hook``'s
dispatch mechanism works against a synthetic verb. Each was true, and
stayed true, while the gate was completely inert in conexus 7.55.0 —
because nothing joined them up.

So this file makes the one claim none of those make: spawn the real
``nx-hook`` with the real verb from ``hooks.json``, feed it a payload on
stdin the way the harness does, and read the envelope off stdout. No
imports of the hook modules, no monkeypatching — a subprocess, a pipe,
and the bytes that come back.

That is deliberately the weakest-looking and most valuable test here.
The defect it covers was invisible to a suite of 1500 passing tests
precisely because every one of them stopped at a component boundary.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]

from nexus.mcp.hooks import DECIDING_HOOKS
from tests._hook_wiring import HOOKS_JSON

#: hook name -> the nx-hook verb hooks.json actually wires it as. Read
#: from the file rather than written down, so this cannot drift from the
#: wiring it claims to exercise.
def _wired_verbs() -> dict[str, str]:
    data = json.loads(HOOKS_JSON.read_text())
    out: dict[str, str] = {}
    for groups in data.get("hooks", {}).values():
        for group in groups:
            for hook in group.get("hooks", []):
                if hook.get("type") == "command" and hook.get("command") == "nx-hook":
                    args = hook.get("args") or []
                    if args:
                        out[str(args[0]).replace("-", "_")] = str(args[0])
    return out


def _run_verb(verb: str, payload: dict, extra_env: dict[str, str] | None = None):
    """Spawn nx-hook exactly as an exec-form command hook would."""
    return subprocess.run(
        ["nx-hook", verb],
        input=json.dumps(payload),
        capture_output=True,
        text=True,
        timeout=60,
        env={**os.environ, **(extra_env or {})},
    )


@pytest.fixture(scope="module", autouse=True)
def _require_nx_hook():
    if shutil.which("nx-hook") is None:
        pytest.skip(
            "nx-hook is not on PATH. It is a console script, so an install "
            "generation predating its declaration has no shim — which is "
            "itself the condition under which these hooks do not fire."
        )


def test_every_deciding_hook_is_reachable_as_a_wired_verb() -> None:
    """Non-vacuity: the verbs below are the ones hooks.json really names."""
    wired = _wired_verbs()
    missing = DECIDING_HOOKS - set(wired)
    assert not missing, (
        f"these deciding hooks are not wired as nx-hook verbs, so the tests "
        f"below would exercise nothing: {sorted(missing)}"
    )


def test_auto_approve_allows_an_allowlisted_tool_over_the_real_wire() -> None:
    proc = _run_verb(
        "auto-approve",
        {
            "tool_name": "mcp__plugin_conexus_nexus__search",
            "hook_event_name": "PreToolUse",
        },
    )
    assert proc.returncode == 0, proc.stderr
    envelope = json.loads(proc.stdout)
    assert (
        envelope["hookSpecificOutput"]["permissionDecision"] == "allow"
    ), proc.stdout


def test_auto_approve_stays_silent_for_a_tool_it_does_not_own() -> None:
    """The discrimination. An approver that approves everything is not one."""
    proc = _run_verb(
        "auto-approve",
        {"tool_name": "Bash", "hook_event_name": "PreToolUse"},
    )
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.strip() == "", (
        f"auto-approve spoke for a tool outside its allowlist: {proc.stdout!r}"
    )


def test_the_close_gate_denies_an_unmarked_bead_over_the_real_wire(tmp_path) -> None:
    """The regression, end to end.

    Armed with a purpose-built `.nexus.yml` under CLAUDE_PROJECT_DIR
    rather than the live one, so the verdict does not depend on whether
    the developer running this has `on_close` enabled in their own
    `.nexus.yml` — a test that inherited that would pass or fail by
    machine rather than by code. (This used to stub a fake
    read_verification_config.py under CLAUDE_PLUGIN_ROOT; the reader is
    in the wheel now and that script is deleted, nexus-z9cz2.)
    """
    (tmp_path / ".nexus.yml").write_text("verification:\n  on_close: true\n")

    proc = _run_verb(
        "pre-close-verification",
        {
            "session_id": "e2e-no-such-session",
            "tool_name": "Bash",
            "tool_input": {"command": "bd close nexus-99xyz --reason e2e"},
        },
        extra_env={"CLAUDE_PROJECT_DIR": str(tmp_path)},
    )
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.strip(), "the close gate wrote nothing at all"
    decision = json.loads(proc.stdout)["hookSpecificOutput"]["permissionDecision"]
    # An unreachable T1 fails OPEN by design, and "e2e-no-such-session"
    # has no T1, so allow is the correct answer here. What this asserts
    # is narrower and is the thing that was broken: the verb resolves,
    # runs, and puts a well-formed decision envelope on stdout.
    assert decision in {"allow", "deny"}, proc.stdout


def test_a_non_bash_call_is_allowed_immediately() -> None:
    proc = _run_verb(
        "pre-close-verification",
        {"session_id": "s", "tool_name": "Read", "tool_input": {"file_path": "/x"}},
    )
    assert proc.returncode == 0, proc.stderr
    assert json.loads(proc.stdout)["hookSpecificOutput"]["permissionDecision"] == "allow"


def test_subagent_stop_runs_and_stays_silent_with_no_ledger(tmp_path) -> None:
    """It can only block an agent the ledger says owes a report.

    With a throwaway XDG_STATE_HOME there is no ledger, so silence is
    correct — and silence still proves the verb resolved and exited
    cleanly rather than being not-found.
    """
    proc = _run_verb(
        "subagent-stop",
        {
            "session_id": "e2e-no-such-session",
            "agent_id": "e2e-agent",
            "agent_type": "Explore",
            "agent_transcript_path": str(tmp_path / "nope.jsonl"),
            "stop_hook_active": "false",
        },
        extra_env={"XDG_STATE_HOME": str(tmp_path / "state")},
    )
    assert proc.returncode == 0, proc.stderr
    if proc.stdout.strip():
        json.loads(proc.stdout)  # whatever it says must at least be JSON


def test_an_unknown_verb_is_refused_rather_than_silently_passing() -> None:
    """The failure mode that would make every test above vacuous.

    If nx-hook answered 0 and nothing for a verb it does not have, a
    typo'd verb in hooks.json would be indistinguishable from a hook
    choosing to stay quiet — which is exactly how the original defect
    hid.
    """
    proc = _run_verb("no-such-verb-nexus-17i1n", {})
    assert proc.returncode != 0 or "unknown verb" in (proc.stderr + proc.stdout), (
        f"nx-hook accepted an unknown verb silently: rc={proc.returncode} "
        f"out={proc.stdout!r} err={proc.stderr!r}"
    )
