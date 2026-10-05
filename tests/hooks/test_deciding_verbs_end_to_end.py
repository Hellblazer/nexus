# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""The deciding verbs, driven as Claude Code drives them (nexus-17i1n).

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
from tests._hook_wiring import HOOKS_JSON, REPO_ROOT, command_verb, launcher_script_args

#: hook name -> the nx-hook verb hooks.json actually wires it as. Read
#: from the file rather than written down, so this cannot drift from the
#: wiring it claims to exercise.
def _wired_verbs() -> dict[str, str]:
    data = json.loads(HOOKS_JSON.read_text())
    out: dict[str, str] = {}
    for groups in data.get("hooks", {}).values():
        for group in groups:
            for hook in group.get("hooks", []):
                verb = command_verb(hook)
                if verb:
                    out[verb.replace("-", "_")] = verb
    return out


def _wired_argv(verb: str) -> list[str]:
    """The argv hooks.json runs for *verb*: the ``uv``-launched nx-hook shim
    when that is how it is wired (nexus-rcoze, nexus-efk2h), so the real path
    is uv, then the shim, then nx-hook."""
    data = json.loads(HOOKS_JSON.read_text())
    for groups in data.get("hooks", {}).values():
        for group in groups:
            for hook in group.get("hooks", []):
                if command_verb(hook) == verb and launcher_script_args(hook):
                    root = str(REPO_ROOT / "conexus")
                    return [
                        hook["command"],
                        *(a.replace("${CLAUDE_PLUGIN_ROOT}", root) for a in hook["args"]),
                    ]
    return ["nx-hook", verb]


def _run_verb(verb: str, payload: dict, extra_env: dict[str, str] | None = None):
    """Spawn the verb exactly as its exec-form hooks.json entry would."""
    return subprocess.run(
        _wired_argv(verb),
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
