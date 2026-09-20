# SPDX-License-Identifier: AGPL-3.0-or-later
"""Registration-wiring tests for the SubagentStart expectations-stamp hook
(RDR-184 .16 wiring, bead nexus-ccs9v.16 — the START-row dispatch record
the declaration-completeness retro audit diffs against EXPECT rows).

RDR-215 bead nexus-q02nx.21 ported the hook's own behaviour to
``nexus.hooks.subagent_start_stamp`` and deleted
``conexus/hooks/scripts/subagent-start-stamp.sh`` and
``conexus/hooks/scripts/subagent-stop.sh``; that coverage now lives in
``tests/hooks/test_subagent_start_stamp_module.py`` and
``tests/hooks/test_subagent_stop_module.py``. What survives here is the
structural wiring check that has nothing to do with the script bodies:
that the two ``mcp_tool`` entries are registered exactly once each,
between ``conexus/hooks/hooks.json`` and ``.claude/settings.json``.
``.claude/settings.json`` names this file and test by path in its own
``_comment``, so both stay put.

The hooks are looked up by IDENTITY rather than by declaration form
(``tests/_hook_wiring.py``): ``subagent_stop`` came back to the command
tier at bead nexus-17i1n, and a check that counts ``mcp_tool`` names
would have read that as zero registrations and passed — a count of
nothing, from a test whose whole job is to count.
"""
from __future__ import annotations

import json
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]

from tests._hook_wiring import events_for  # noqa: E402

#: The two orchestration hooks, by hook name. Each is registered on the
#: tool tier and one of them is WIRED on the command tier; the lookups
#: below span both, so this list does not care which.
_ORCHESTRATION_TOOLS = ("hook_subagent_start_stamp", "hook_subagent_stop")


def test_registered_in_plugin_hooks_json() -> None:
    assert "SubagentStart" in events_for("subagent_start_stamp"), (
        "subagent_start_stamp not registered under SubagentStart"
    )


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

    Behaviour is unchanged by the RDR-215 port: both hooks still default
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
    for tool in _ORCHESTRATION_TOOLS:
        offenders = [c for c in project_cmds if tool in c]
        assert not offenders, (
            f"{tool} is registered in BOTH conexus/hooks/hooks.json and "
            f".claude/settings.json; both fire, so every row it writes is "
            f"doubled and the .11 census is corrupted. Remove the project-"
            f"settings copy — the plugin already ships it. Found: {offenders}"
        )

    for tool in _ORCHESTRATION_TOOLS:
        wirings = events_for(tool.removeprefix("hook_"))
        assert len(wirings) == 1, (
            f"{tool} must be registered exactly once in the plugin; "
            f"found {len(wirings)} on {wirings}"
        )
