# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""Closing the work rows whose work has ended (RDR-253 Step 6, nexus-hczdo: the work-row lifecycle).

Every registrar closes its own row when its work ends (:mod:`nexus.identity.work`). This module is the backstop for an end
that never wrote: a SIGKILL, a restart with an agent in flight, a hook killed at its budget, an engine call that timed out
at the end. It closes a ``running`` row only on a witness about the work itself (:func:`reap_why`), the way its registrar
would have closed it, so no session marks rows by hand and no reader counts them. Callers: SessionStart for the session's
own identity (:mod:`nexus.hooks.work_reap_child`), ``nx identity work list --reap`` and the restart script's ready check.

The witnesses. A wait for a session the identity no longer holds. The liveness probe of
:func:`nexus.identity.work.process_stale_why`: the process is gone, the row is a ghost of an earlier holder, or nobody wrote
it for 12 hours. For an ``agent`` row, which has no process of its own, the harness's record of the agent's end: Claude Code
writes a ``<task-notification>`` naming the agent id and a terminal ``<status>`` into the holder session's transcript when a
background agent stops (:func:`ended_agents`). A notification older than the row's last write is not counted, so an agent
resumed after it ended (its SubagentStart writes the row again) is not closed.

What it never closes: a ``worktree`` row (a path that is not a directory here may be another host's, and the row is the
ownership record ``may-stop`` reads), a ``tmux`` row, and any row whose only fault is that its identity is not alive (a
crashed session's lab run can still run on its host).

The engine does not fence ``identity_work`` writes (a label, not a fenced family), so a reader with no identity of its own
may close another identity's rows; it closes only what a witness says has ended.
"""
from __future__ import annotations

import json
import os
import re
import time
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import structlog

from nexus.identity.bind_client import IdentityApiError, IdentityUnavailableError
from nexus.identity.work import (
    DONE,
    RUNNING,
    Work,
    _PROBE_REF,
    _epoch,
    _process_gone,
    holder_facts,
    of,
    process_stale_why,
    read_owner,
    wait_superseded,
)

__all__ = ["FORGOTTEN_AT_END", "REAPED_KINDS", "ended_agents", "reap", "reap_why"]

_log = structlog.get_logger(__name__)

#: The kinds the reap closes: those whose rows end with their work. A worktree or a tmux server outlives its session.
REAPED_KINDS: tuple[str, ...] = ("wait", "labrun", "agent", "watcher")

#: The kinds whose end leaves nothing to show: forgotten, as the SubagentStop hook forgets an agent's row. Every other reaped
#: kind is marked ``done``, as its registrar marks it.
FORGOTTEN_AT_END: tuple[str, ...] = ("agent",)

#: The statuses of a ``<task-notification>`` that say the task is over. Seen in transcripts: completed, failed; the rest are
#: the harness's other terminal words, accepted so a stopped agent counts too.
TERMINAL_STATUSES: frozenset[str] = frozenset({"completed", "failed", "killed", "stopped", "cancelled", "canceled", "error"})

_NOTICE = re.compile(r"<task-id>(?P<id>[A-Za-z0-9_-]+)</task-id>.*?<status>(?P<status>[a-z_]+)</status>", re.S)


def _claude_projects() -> Path:
    base = os.environ.get("CLAUDE_CONFIG_DIR", "").strip()
    return (Path(base) if base else Path.home() / ".claude") / "projects"


def ended_agents(session_id: str | None, projects: Path | None = None) -> dict[str, float]:
    """``{agent id: epoch seconds of its latest terminal task-notification}`` from the transcript of *session_id*
    (``<projects>/*/<session id>.jsonl``). Empty when there is no session, no transcript, or it cannot be read: no witness,
    never an error. Only lines that carry a task notification are parsed."""
    if not session_id:
        return {}
    root = projects if projects is not None else _claude_projects()
    out: dict[str, float] = {}
    try:
        paths = list(root.glob(f"*/{session_id}.jsonl"))
    except OSError:
        return {}
    for path in paths:
        try:
            with path.open("r", encoding="utf-8", errors="replace") as fh:
                for line in fh:
                    if "<task-notification>" not in line:
                        continue
                    try:
                        entry = json.loads(line)
                    except ValueError:
                        continue
                    at = _epoch(entry.get("timestamp")) if isinstance(entry, Mapping) else None
                    text = json.dumps(entry.get("content", "")) if isinstance(entry, Mapping) else ""
                    text = text.replace("\\n", "\n")
                    for m in _NOTICE.finditer(text):
                        if m["status"] in TERMINAL_STATUSES and at is not None:
                            out[m["id"]] = max(out.get(m["id"], 0.0), at)
        except OSError:
            continue
    return out


def reap_why(env: Any, identity: str, w: Work, *, session: str | None, holder_started: float | None,
             ended: Mapping[str, float] | None = None) -> str | None:
    """Why a running row is safe to close for its registrar, or None (see the module docstring for the witnesses).
    *ended* is :func:`ended_agents` of the holder's session."""
    if w.state != RUNNING or w.kind not in REAPED_KINDS:
        return None
    if wait_superseded({"kind": w.kind, "ref": w.ref}, session) and not _vouched(env, identity, w):
        # A process that still runs and is the one recorded is the authority (a /branch window can show the holder's
        # session lagging the registry): only a wait nothing vouches for is closed for naming an earlier session.
        return "a wait from an earlier session"
    if w.kind == "agent" and ended:
        agent_id = w.ref.split(":", 1)[1] if w.ref.startswith("agent:") else w.ref
        at, written = ended.get(agent_id), _epoch(w.updated_at)
        if at is not None and (written is None or at >= written):
            return "its task notification says it ended"
    return process_stale_why(env, identity, w, holder_started=holder_started)


def _vouched(env: Any, identity: str, w: Work) -> bool:
    """A process on this host runs and is the one the row's probe ref or owner record names."""
    match = _PROBE_REF.match(w.ref)
    if match:
        judged, gone = _process_gone(env.procs, env.host, match["host"], int(match["pid"]), start=None,
                                     began=float(match["started"]))
        if judged:
            return not gone
    record = read_owner(env.identity_dir, identity, w.kind, w.ref)
    if record is not None:
        start = record.get("start")
        judged, gone = _process_gone(env.procs, env.host, str(record.get("host") or ""), int(record["pid"]),
                                     start=start if isinstance(start, str) else None, began=None)
        return judged and not gone
    return False


def reap(env: Any, identity: str, *, projects: Path | None = None, deadline_s: float | None = None) -> list[dict[str, str]]:
    """Close every running row of *identity* whose work has ended by :func:`reap_why`: forget an agent row, mark any other
    ``done``. The backstop for an end-of-life write that never came, so no session marks rows by hand and no reader counts
    them. Each closed row is logged, and the call logs one ``identity_work_reaped`` line with the count; a count above zero
    is a lead to an end-of-life path that did not write (or an agent that was in flight across a restart). Returns
    ``[{kind, ref, why, closed}]``. A row whose close fails is left as it was and logged (``identity_work_reap_failed``),
    never raised. *projects* is the Claude transcripts root (a test's stand-in). *deadline_s* bounds the whole call (the
    SessionStart child has no alarm on Windows): the rows not reached are left for the next reader.

    The rows are judged on one read, then read again just before any write: a row whose ``updated_at`` changed in between
    was written by its registrar again (a resumed ``nx ci wait`` on the same ref) and is left alone."""
    until = time.monotonic() + deadline_s if deadline_s is not None else None
    session, started = holder_facts(env.client, identity)
    rows = of(env.client, identity)
    ended = ended_agents(session, projects) if any(w.kind == "agent" and w.state == RUNNING for w in rows) else {}
    judged = [(w, why) for w in rows
              if (why := reap_why(env, identity, w, session=session, holder_started=started, ended=ended)) is not None]
    closed: list[dict[str, str]] = []
    if not judged:
        _log.info("identity_work_reaped", identity=identity, count=0, kinds=[])
        return closed
    fresh = {(w.kind, w.ref): (w.state, w.updated_at) for w in of(env.client, identity)}
    for w, why in judged:
        if until is not None and time.monotonic() > until:
            _log.warning("identity_work_reap_deadline", identity=identity, left=len(judged) - len(closed))
            break
        if fresh.get((w.kind, w.ref)) != (w.state, w.updated_at):
            continue
        action = "forgotten" if w.kind in FORGOTTEN_AT_END else DONE
        try:
            if action == DONE:
                env.client.put_work(identity, w.kind, w.ref, DONE)
            else:
                env.client.remove_work(identity, w.kind, w.ref)
        except (IdentityApiError, IdentityUnavailableError) as exc:
            _log.warning("identity_work_reap_failed", identity=identity, kind=w.kind, ref=w.ref, error=str(exc))
            continue
        closed.append({"kind": w.kind, "ref": w.ref, "why": why, "closed": action})
        _log.info("identity_work_reaped_row", identity=identity, kind=w.kind, ref=w.ref, why=why, closed=action)
    _log.info("identity_work_reaped", identity=identity, count=len(closed), kinds=sorted({c["kind"] for c in closed}))
    return closed
