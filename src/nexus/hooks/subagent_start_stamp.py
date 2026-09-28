# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""The SubagentStart ledger stamp (RDR-215 bead nexus-q02nx.11).

Port of ``conexus/hooks/scripts/subagent-start-stamp.sh`` (contract map
row 5). Records the framework-assigned ``agent_id`` against its
``agent_type`` as a ``START`` row.

**Non-load-bearing backfill, deliberately.** The SubagentStart payload
cannot classify background-ness (cc-validation scenario 27), so this row
never decides whether an agent owes a report -- that is
``expectations_owes_report``'s job, keyed on type. What it gives is the
cross-check the retro audit and census need: an agent that STARTED with no
matching EXPECT credit is an undeclared dispatch, and that comparison is
only possible because this row exists.

**STDOUT-SILENT by contract**, like its sibling writer. It appends to the
ledger and says nothing.

**A SendMessage resume writes ``RESUMED``, not ``START`` (nexus-xxvv3).**
After a ``/clear``, a background worktree agent resumed by SendMessage
re-fires this hook under a NEW session id, as ``general-purpose`` (the
harness drops the original type), with no PreToolUse Agent dispatch to
have written an EXPECT row here first. Before appending anything, this
hook checks whether ``agent_id`` already has a real ``START`` row in
SOME OTHER session's ledger (:func:`nexus.hooks.expectations.expectations_find_resume_origin`)
-- if so, it is a resume of an already-declared dispatch, and the row
written is ``RESUMED`` (:func:`nexus.hooks.expectations.expectations_resume`)
so the retro audit credits it against its original dispatch instead of
flagging it undeclared and spending this session's own EXPECT credit for
whatever type it re-STARTed as.

**A bug class the port removes for free.** The bash decodes three fields
with ``IFS=$'\\t' read``, and tab is IFS whitespace, so a genuinely empty
middle field COLLAPSES and the remaining values shift left --
``agent-dispatch-expect.sh`` hit exactly this and switched to ``\\x1f``,
noting in its own header that this script "is currently benign only by
luck". Decoding from the payload dict directly, as here, cannot shift a
field at all. That is a real correctness improvement rather than a
behaviour change to hide, and it is why the differential below feeds an
empty ``agent_type`` to both implementations.
"""
from __future__ import annotations

import os
import time
from pathlib import Path

from nexus._hook_runtime._config import stop_guard_mode
from nexus._hook_runtime._io import HookResult
from nexus.hooks import expectations as _exp

__all__ = ["run"]

_SCRUB = str.maketrans({"\t": " ", "\n": " ", "\r": " "})
_LOCK_TRIES = 10

#: The harness's own collapsed ``agent_type`` for a SendMessage-resumed
#: dispatch (nexus-xxvv3 IMPORTANT-2, code review). Gates the cross-session
#: resume scan so an ORDINARY dispatch with a real declared type (the vast
#: majority of traffic: ``conexus:developer``, ``worktree-developer``, etc)
#: never pays it -- only a first-time START reporting exactly this type
#: does. Approximate, not exact: an ordinary FRESH dispatch with no
#: specific subagent_type also reports as ``general-purpose``, so the scan
#: still runs for that common case too -- correctly finding nothing, at the
#: cost this module's own docstring already measures. No narrower signal is
#: available in the SubagentStart payload to distinguish the two (the
#: payload does not carry a "resumed" flag; see
#: ``expectations_find_resume_origin``'s own docstring for the failure
#: modes this approximation still leaves open).
_RESUMABLE_AGENT_TYPE = "general-purpose"


def _already_stamped(file: str, agent_id: str) -> bool:
    """True iff this agent already has a START or RESUMED row.

    Stamp-at-most-once. A duplicate START is far less harmful than a
    duplicate EXPECT -- the readers dedupe it by agent id and it inflates
    no credit pool -- but a second row still muddies the census's own
    counts, so it is refused where it can be seen cheaply. RESUMED
    (nexus-xxvv3) is included because it is this function's OTHER row
    shape for the same "already handled this agent_id" fact -- a resumed
    dispatch never gets both a START and a RESUMED row for the same id.
    """
    try:
        text = Path(file).read_text()
    except OSError:
        return False
    for line in text.split("\n"):
        row = line.split("\t")
        if len(row) > 2 and row[1] in ("START", "RESUMED") and row[2] == agent_id:
            return True
    return False


def _acquire(lockdir: str) -> bool:
    """Bounded, best-effort. Correctness does not rest on it: the worst
    case is a duplicate row the readers already collapse, and a missing
    row would be worse than a duplicate one."""
    for _ in range(_LOCK_TRIES):
        try:
            os.mkdir(lockdir)
            return True
        except FileExistsError:
            time.sleep(0.1)
        except OSError:
            return False
    return False


def run(payload: dict | None) -> HookResult:
    """Append this subagent's START row. Always silent, always exit 0."""
    if stop_guard_mode() not in ("observe", "block"):
        return HookResult()

    data = payload or {}
    session_id = str(data.get("session_id") or "").translate(_SCRUB)
    agent_id = str(data.get("agent_id") or "").translate(_SCRUB)
    agent_type = str(data.get("agent_type") or "").translate(_SCRUB)

    # All three are required. The bash exits 0 here without a diagnostic,
    # and that silence is right: SubagentStart fires for dispatches this
    # ledger does not track, so a missing field is an ordinary event
    # rather than an anomaly worth a line in the operator's stderr.
    if not session_id or not agent_id or not agent_type:
        return HookResult()

    try:
        file = _exp.expectations_file(session_id)
    except _exp.ExpectationsUsageError:
        return HookResult()

    lockdir = f"{file}.stamp.lock"
    held = _acquire(lockdir)
    try:
        if not _already_stamped(file, agent_id):
            # nexus-xxvv3: a SendMessage-resumed background worktree agent
            # re-fires SubagentStart under a NEW session id, as
            # general-purpose (the harness drops the original type), with
            # no preceding PreToolUse Agent dispatch to write an EXPECT
            # row here. If this agent_id already has a real START
            # elsewhere, this is that resume, not a fresh dispatch -- write
            # RESUMED instead of START so the retro audit credits it
            # against its ORIGINAL dispatch rather than flagging it
            # UNDECLARED and consuming this session's credit for whatever
            # it re-STARTed as.
            #
            # Gated on agent_type (nexus-xxvv3 IMPORTANT-2, code review):
            # the scan is not free, so it only runs for the ONE type the
            # harness is known to report for a resume -- see
            # _RESUMABLE_AGENT_TYPE's own comment for what this does and
            # does not distinguish.
            origin = (
                _exp.expectations_find_resume_origin(session_id, agent_id)
                if agent_type == _RESUMABLE_AGENT_TYPE
                else None
            )
            try:
                if origin is not None:
                    origin_session_id, original_agent_type = origin
                    _exp.expectations_resume(
                        session_id, agent_id, agent_type,
                        origin_session_id, original_agent_type,
                    )
                else:
                    _exp.expectations_start(session_id, agent_id, agent_type)
            except _exp.ExpectationsUsageError:
                return HookResult()
    finally:
        if held:
            try:
                os.rmdir(lockdir)
            except OSError:
                pass
    return HookResult()
