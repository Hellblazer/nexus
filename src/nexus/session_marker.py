# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""The tuple-watch session-marker contract (RDR-211 nexus-rplay.24).

Rehomed out of ``nexus.tuple_watch`` ahead of that module's deletion (the
``nx tuple watch`` loop itself, a later RDR-211 bead): these five functions
are the part of ``tuple_watch.py`` that OTHER modules import at runtime --
``nexus.tuple_directory.resolve_default_from`` (``_read_session_marker``,
for ``mailbox_send``'s default ``from``) and ``nexus.hooks``'s ``/clear``/
``/resume`` SessionStart handoff (``record_clear_and_write_session_marker``,
via ``_write_tuple_watch_session_marker``) -- so they cannot leave with the
watcher loop.

**Why here, not ``nexus.session`` or ``nexus.db.t1``:** ``nexus.session``
already owns CLI/session-id identity (``resolve_active_session_id``,
``find_immediate_claude_pid``, the ``current_session`` flat file), but has
no notion of a per-claude-pid watcher marker or an RDR-208 drain record --
folding this in would conflate two unrelated "session" concepts and make
that already-large module another thing the watcher-deletion bead has to
touch. ``nexus.db.t1``'s lease helpers (``publish_t1_session_lease`` /
``read_t1_session_lease``) are a structurally different mechanism: a
PG/HTTP-backed T1 session lease reached through the engine, not a flat
file on disk. Neither is the right layer, so this module is a small,
single-purpose home for exactly the marker/cleared-record pair.

**On-disk paths are UNCHANGED (byte-identical)** from ``tuple_watch.py``:
``conexus/hooks/scripts/mailbox_drain.py`` is a plugin script that cannot
import this package, so it keeps its own literal copies of the
``tuple-watch`` directory name and the ``session.<pid>`` /
``cleared.<session_id>`` file-name shapes, pinned against drift by
``tests/hooks/test_mailbox_drain_hook.py`` (which compares those literals
against ``nexus.tuple_watch.session_marker_path`` /
``.cleared_record_path`` directly) and by this module's own
``tests/test_session_marker.py::TestPathsMatchTheMailboxDrainHookLiterals``.
Moving this contract must never move those strings.

``nexus.tuple_watch`` re-exports these five names as thin pass-throughs, so
its own remaining code (the watcher self-stop check in ``run_watch``) and
every test importing them from there keep working unchanged until the
watcher-deletion bead removes that module outright.
"""
from __future__ import annotations

import os
from pathlib import Path

import structlog

_log = structlog.get_logger(__name__)

#: Matches ``nexus.tuple_watch``'s own ``_STATE_SUBDIR`` literal, kept here
#: as a separate copy rather than a shared import: tuple_watch.py's other
#: uses of that constant (the probe cursor, the per-address lock, the
#: address-registration directory) are the WATCHER's own state and stay in
#: that module; this module owns only the marker/cleared-record pair.
_STATE_SUBDIR = "tuple-watch"


def session_marker_path(state_dir: Path, claude_pid: int) -> Path:
    """``<state_dir>/tuple-watch/session.<claude_pid>``: the stale-watcher
    self-stop marker (nexus-6konb.12, MM-3.4 fix 1). Keyed on the CLAUDE
    ancestor pid, never a session id -- the whole point is to tell a
    watcher spawned under an OLDER session that a NEWER one now exists
    for the same conversation, so the pid has to be the stable half of
    the pair (see :func:`write_session_marker`).
    """
    return state_dir / _STATE_SUBDIR / f"session.{claude_pid}"


def write_session_marker(state_dir: Path, claude_pid: int, session_id: str) -> None:
    """Best-effort, atomic marker naming the NEW session id for
    *claude_pid* (nexus-6konb.12, MM-3.4 fix 1).

    Replaces the model-dependent TaskStop rule the SessionStart arm
    instruction used to carry (:mod:`nexus.mailbox_arm`): that rule could
    not work after ``/clear`` in the first place, because the fresh
    conversation it would run in has no memory of the OLD Monitor's
    harness task id -- there was never a way for a genuinely new context
    to discover it. This marker sidesteps the discovery problem instead
    of solving it: the watcher checks its OWN pid's marker, not a task
    id nothing hands it.

    Reuses the nexus-d76vc T1-handoff pattern (:mod:`nexus.daemon.t1_handoff`):
    the writer (``nexus.hooks.session_start``, on ``/clear``/``/resume``)
    and the reader (``nexus.tuple_watch.run_watch``, from inside the
    Monitor's own shell) each independently derive the SAME claude_pid
    via :func:`nexus.session.find_immediate_claude_pid`, walking process
    ancestry from wherever they happen to run up to the first ``claude*``
    process. The pid is never passed between them -- it is recomputed on
    both sides, which is what lets a watcher spawned minutes earlier,
    from a different shell, still find the right file.

    Called only from a best-effort caller (see
    ``nexus.hooks._write_tuple_watch_session_marker``): a failure here
    only means a stale watcher keeps running and holding its lock a
    little longer, never a reason to fail SessionStart over it.
    """
    path = session_marker_path(state_dir, claude_pid)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.parent / f"{path.name}.{os.getpid()}.tmp"
        tmp.write_text(session_id, encoding="utf-8")
        tmp.replace(path)
    except OSError as e:  # pragma: no cover — best-effort, disk-failure path
        _log.debug("tuple_watch_session_marker_write_failed", claude_pid=claude_pid, error=str(e))


def _read_session_marker(state_dir: Path, claude_pid: int) -> str | None:
    """Read the marker :func:`write_session_marker` writes, or ``None`` for
    anything short of a clean non-empty read -- missing file, unreadable,
    or empty are the overwhelmingly common per-cycle case (no ``/clear``
    or ``/resume`` happened since this watcher spawned) and must be
    indistinguishable from each other: a stop decision is made only on an
    actual, different session id, never inferred from an absent or
    unreadable file.
    """
    path = session_marker_path(state_dir, claude_pid)
    try:
        return path.read_text(encoding="utf-8").strip() or None
    except OSError:
        return None


def cleared_record_path(state_dir: Path, session_id: str) -> Path:
    """``<state_dir>/tuple-watch/cleared.<session_id>``: the one-time drain
    record RDR-208 Phase 2 Step 3 hands to
    ``conexus/hooks/scripts/mailbox_drain.py``. *session_id* is the NEW
    session's id -- the drain reads its OWN session id's record, never a
    prior one -- and the file's content is the mailbox(es) a ``/clear``
    stranded: one bare session id per line. See
    :func:`record_clear_and_write_session_marker`.
    """
    return state_dir / _STATE_SUBDIR / f"cleared.{session_id}"


def record_clear_and_write_session_marker(
    state_dir: Path, claude_pid: int, new_session_id: str, *, record_clear: bool,
) -> None:
    """Write *claude_pid*'s session marker for *new_session_id* and, when
    *record_clear* is true, record the mailbox(es) a ``/clear`` just
    stranded (RDR-208 Phase 2 Step 3).

    The previous marker is read BEFORE :func:`write_session_marker`
    overwrites it -- the ordering the RDR's ``/clear`` design requires, so
    the previous session id is never lost to the very write that would
    otherwise erase it. No record is written when there was no previous
    marker, or it already names *new_session_id* (nothing was stranded).

    Chained clears (a second ``/clear`` before the first's drain has run)
    carry every earlier id forward into the new record and remove the old
    one: with S1 -> S2 -> S3 and no prompt in between, ``cleared.S3`` ends
    up naming both S2 and S1, and ``cleared.S2`` -- which no live session
    can answer to any more, so nothing would ever read it again except the
    drain's 7-day prune -- is deleted rather than left to strand its own
    record of S1 unreachably.

    Called only from ``nexus.hooks._write_tuple_watch_session_marker``,
    whose docstring carries the "never fail SessionStart" contract this
    relies on: every failure here is caught there. This function itself
    swallows an ``OSError`` from the record write specifically (matching
    :func:`write_session_marker`'s own best-effort contract just above),
    since ``write_session_marker`` already succeeded or failed on its own
    by the time the record write is attempted.
    """
    previous_id = _read_session_marker(state_dir, claude_pid) if record_clear else None
    write_session_marker(state_dir, claude_pid, new_session_id)
    if not record_clear or not previous_id or previous_id == new_session_id:
        return

    ids = [previous_id]
    old_record = cleared_record_path(state_dir, previous_id)
    old_ids: list[str] = []
    try:
        old_ids = [
            line.strip()
            for line in old_record.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]
    except OSError:
        pass
    for old_id in old_ids:
        if old_id not in ids:
            ids.append(old_id)

    new_record = cleared_record_path(state_dir, new_session_id)
    try:
        new_record.parent.mkdir(parents=True, exist_ok=True)
        tmp = new_record.parent / f"{new_record.name}.{os.getpid()}.tmp"
        tmp.write_text("\n".join(ids) + "\n", encoding="utf-8")
        tmp.replace(new_record)
    except OSError as e:  # pragma: no cover — best-effort, disk-failure path
        _log.debug(
            "tuple_watch_cleared_record_write_failed",
            new_session_id=new_session_id, error=str(e),
        )
        return

    if old_ids:
        try:
            old_record.unlink()
        except OSError:
            pass


__all__ = [
    "_read_session_marker",
    "cleared_record_path",
    "record_clear_and_write_session_marker",
    "session_marker_path",
    "write_session_marker",
]
