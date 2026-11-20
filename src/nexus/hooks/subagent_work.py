# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""A dispatched agent as work its identity owns (RDR-253 Step 6 part 1, nexus-hczdo).

SubagentStart registers ``kind=agent`` with ref ``agent:<agent id>`` (:func:`register_start`, called by
:mod:`nexus.hooks.subagent_start`); SubagentStop marks it ``done`` (:func:`mark_done`, called by
:mod:`nexus.hooks.subagent_stop_t1_nudge` when the agent is really finished). Register and done only: no write per step.
The row is what the restart's ready check and the compaction checkpoint read, so a session that dispatched agents and is
asked to restart is told they would be cut off.

An event with no ``agent_id`` or no ``agent_type`` writes nothing: the harness's own internal stops carry no type and
``hooks.json`` never spawns the stop verb for them, so a start for one would leave a row nothing ends.

Never raises and never waits long: :func:`nexus.identity.work.register_own` writes best effort with a 2 s engine timeout, an
engine that does not know the kind (0.1.198 answers ``bad_kind``) or has no identity to write for is a False and one log
line, and the dispatch or the stop goes on. A hook's process exits at once, so no owner record is written: a row whose
SubagentStop never came (the session was killed) reads stale through its identity, or after the row TTL.
"""
from __future__ import annotations

from typing import Any

import structlog

__all__ = ["mark_done", "ref_of", "register_start"]

_log = structlog.get_logger(__name__)


def ref_of(agent_id: str) -> str:
    return f"agent:{agent_id}"


def _label(payload: Any, state: str) -> bool:
    data = payload if isinstance(payload, dict) else {}
    agent_id, agent_type = data.get("agent_id"), data.get("agent_type")
    if not (isinstance(agent_id, str) and agent_id and isinstance(agent_type, str) and agent_type):
        return False
    try:
        from nexus.identity import work  # noqa: PLC0415 — deferred: a stop that labels nothing pays nothing

        return bool(work.register_own("agent", ref_of(agent_id), state))
    except Exception as exc:  # noqa: BLE001 — a label never blocks a dispatch or a stop
        _log.warning("subagent_work_label_failed", state=state, error=f"{type(exc).__name__}: {exc}")
        return False


def register_start(payload: Any) -> bool:
    """Register the agent running. True when a row was written."""
    return _label(payload, "running")


def mark_done(payload: Any) -> bool:
    """Mark the agent done. True when a row was written."""
    return _label(payload, "done")
