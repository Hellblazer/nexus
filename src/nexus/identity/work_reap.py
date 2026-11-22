# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""Closing the work rows whose work has ended (RDR-253 Step 6, nexus-hczdo: the work-row lifecycle).

Every registrar closes its own row when its work ends (:mod:`nexus.identity.work`). This module is the backstop for an end
that never wrote: a SIGKILL, a restart or compaction with an agent in flight, an engine call that timed out at the end. It
closes a ``running`` row only on a witness about the work itself (:func:`reap_why`), the way its registrar would have
closed it, so no session marks rows by hand and no reader counts them. Callers: SessionStart for the session's own
identity (:mod:`nexus.hooks.work_reap_child`), ``nx identity work list --reap`` and the restart script's ready check.

The engine does not fence ``identity_work`` writes (a label, not a fenced family), so a reader with no identity of its own
may close another identity's rows; it closes only what the probe already reads as ended.
"""
from __future__ import annotations

from typing import Any

import structlog

from nexus.identity.bind_client import IdentityApiError, IdentityUnavailableError
from nexus.identity.work import (
    DONE,
    RUNNING,
    Work,
    holder_facts,
    of,
    process_stale_why,
    wait_superseded,
    worktree_gone,
)

__all__ = ["FORGOTTEN_AT_END", "reap", "reap_why"]

_log = structlog.get_logger(__name__)


#: The kinds whose end leaves nothing to show: a stale row of one is forgotten, as its own end forgets it (an agent's stop,
#: a removed worktree). Every other kind is closed ``done``, as its registrar closes it.
FORGOTTEN_AT_END: tuple[str, ...] = ("agent", "worktree")


def reap_why(env: Any, identity: str, w: Work, *, session: str | None, holder_started: float | None) -> str | None:
    """Why a running row is safe to close for its registrar, or None. Only a witness about the work itself counts: its
    worktree is gone (:func:`worktree_gone`), it is a wait for a session the identity no longer holds
    (:func:`wait_superseded`), or the probe reads its process gone, the row a ghost of an earlier holder, or unwritten for
    :data:`ROW_TTL_S` (:func:`process_stale_why`). "The identity is not alive" alone never reaps: a lab run of a crashed
    session can still run on its host, and a seat's worktree rows are its ownership when the seat is bound again."""
    if w.state != RUNNING:
        return None
    row = {"kind": w.kind, "ref": w.ref}
    if worktree_gone(row):
        return "the worktree is gone"
    if wait_superseded(row, session):
        return "a wait from an earlier session"
    return process_stale_why(env, identity, w, holder_started=holder_started)


def reap(env: Any, identity: str) -> list[dict[str, str]]:
    """Close every running row of *identity* whose work has ended by :func:`reap_why`: forget an agent or worktree row
    (:data:`FORGOTTEN_AT_END`), mark any other ``done``. The backstop for an end-of-life write that never came (a SIGKILL,
    a restart with an agent in flight, an engine call that timed out at the end), so no session marks rows by hand and no
    reader counts them. Each closed row is logged, and the call logs one ``identity_work_reaped`` line with the count: a
    count above zero names an end-of-life path that did not write. Returns ``[{kind, ref, why, closed}]``. A row whose
    close fails is left as it was and logged (``identity_work_reap_failed``), never raised."""
    session, started = holder_facts(env.client, identity)
    closed: list[dict[str, str]] = []
    for w in of(env.client, identity):
        why = reap_why(env, identity, w, session=session, holder_started=started)
        if why is None:
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
