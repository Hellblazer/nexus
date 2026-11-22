# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""A dispatched agent as work its identity owns (RDR-253 Step 6 part 1, nexus-hczdo).

SubagentStart registers ``kind=agent`` with ref ``agent:<agent id>`` (:func:`register_start`, called by
:mod:`nexus.hooks.subagent_start`); SubagentStop removes the row (:func:`register_stop`, called by
:mod:`nexus.hooks.subagent_stop_t1_nudge` on every stop, nudged or not). Register and remove only, once each: no write
per step. The row is what the restart's ready check and the compaction checkpoint read, so a session that dispatched agents
and is asked to restart is told they would be cut off.

The stop forgets the row and does not mark it done: the ready check ignores a done row, and a done row per dispatch would
fill the identity's 200-row limit by agents alone. For the same reason neither hook purges at the limit
(``purge=False``): SubagentStart has 10 seconds, and the purge is up to ~200 sequential engine calls. A row whose stop never
came (the session was killed) reads stale at the next ready check (:func:`nexus.identity.work.process_stale_why`: it
predates its holder's start) and is purged by a registrar that has time.

An event with no ``agent_id`` or no ``agent_type`` writes nothing: the harness's own internal stops carry no type and
``hooks.json`` never spawns the stop verb for them, so a start for one would leave a row nothing ends.

The stop that fails its first write retries once (:data:`STOP_RETRY_TIMEOUT_S`), and every stop that leaves the row
behind logs it (``subagent_work_stop_failed``; :func:`nexus.identity.work.forget_own` logs a process with no identity as
``identity_work_forget_no_identity``). A row no stop removed is closed by
the reap (:mod:`nexus.identity.work_reap`) at the next SessionStart or restart ready check, which logs it as a missed end.

Never raises and never waits long: :func:`nexus.identity.work.register_own` writes best effort with a 2 s engine timeout, an
engine that does not know the kind (0.1.198 answers ``bad_kind``; this process then stops asking) or has no identity to
write for is a False and one log line, and the dispatch or the stop goes on. A success logs one structured line
(``subagent_work_registered``, ``subagent_work_forgotten``), the count of agent rows the measure reads. A hook's process exits
at once, so no owner record is written.
"""
from __future__ import annotations

from typing import Any

import structlog

__all__ = ["ref_of", "register_start", "register_stop"]

_log = structlog.get_logger(__name__)

#: Seconds the stop's one retry may take, inside the hook's 10 s budget (``hooks.json``) after the first 2 s call.
STOP_RETRY_TIMEOUT_S = 5.0


def ref_of(agent_id: str) -> str:
    return f"agent:{agent_id}"


def _ids(payload: Any) -> tuple[str, str] | None:
    data = payload if isinstance(payload, dict) else {}
    agent_id, agent_type = data.get("agent_id"), data.get("agent_type")
    if isinstance(agent_id, str) and agent_id and isinstance(agent_type, str) and agent_type:
        return agent_id, agent_type
    return None


def register_start(payload: Any) -> bool:
    """Register the agent running. True when a row was written."""
    ids = _ids(payload)
    if ids is None:
        return False
    try:
        from nexus.identity import work  # noqa: PLC0415 — deferred: a stop that labels nothing pays nothing

        done = bool(work.register_own("agent", ref_of(ids[0]), "running", purge=False))
    except Exception as exc:  # noqa: BLE001 — a label never blocks a dispatch
        _log.warning("subagent_work_label_failed", state="running", error=f"{type(exc).__name__}: {exc}")
        return False
    if done:
        _log.info("subagent_work_registered", agent_id=ids[0], agent_type=ids[1])
    return done


def register_stop(payload: Any) -> bool:
    """Remove the agent's row. True when it is gone."""
    ids = _ids(payload)
    if ids is None:
        return False
    try:
        from nexus.identity import work  # noqa: PLC0415

        done = bool(work.forget_own("agent", ref_of(ids[0])))
        if not done:
            # One retry with the rest of the hook's budget (10 s in hooks.json): the first call's 2 s bound is what a cold
            # engine connection or a busy cloud edge misses, and a missed stop leaves the row for the reap.
            done = bool(work.forget_own("agent", ref_of(ids[0]), timeout=STOP_RETRY_TIMEOUT_S))
    except Exception as exc:  # noqa: BLE001 — a label never costs a stop
        _log.warning("subagent_work_label_failed", state="forget", error=f"{type(exc).__name__}: {exc}")
        return False
    if done:
        _log.info("subagent_work_forgotten", agent_id=ids[0], agent_type=ids[1])
    else:
        _log.warning("subagent_work_stop_failed", agent_id=ids[0], agent_type=ids[1])
    return done
