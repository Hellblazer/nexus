# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""The tuple-watch session-marker contract (RDR-211 nexus-rplay.24).

Rehomed out of the former CLI mailbox-watch loop module ahead of its
deletion (RDR-211 nexus-rplay.14, a later bead in the same RDR): these five
functions are the part of that module OTHER modules import at runtime --
``nexus.tuple_directory.resolve_default_from`` (``_read_session_marker``,
for ``mailbox_send``'s default ``from``) and ``nexus.hooks``'s ``/clear``/
``/resume`` SessionStart handoff (``record_clear_and_write_session_marker``,
via ``_write_tuple_watch_session_marker``) -- so they did not leave with the
watcher loop when it was deleted.

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

**On-disk paths are UNCHANGED (byte-identical)** from the former watcher
module, because files already on disk are read by those names. The drain
hook used to carry its own literal copies of them, as a plugin script that
could not import this package; since nexus-t9klx it is
:mod:`nexus.hooks.mailbox_drain` and calls :func:`cleared_record_path`
itself, so this module is the only spelling.

The former watcher module re-exported these five names as thin
pass-throughs while it still existed, so every importer kept working
unchanged across the move; RDR-211 nexus-rplay.14 deleted that module (and
its re-export) outright once nothing else needed it.
"""
from __future__ import annotations

import os
from pathlib import Path

import structlog

_log = structlog.get_logger(__name__)

#: Matches the former watcher module's own ``_STATE_SUBDIR`` literal, kept
#: here as a separate copy rather than a shared import: that module's other
#: uses of the constant (the probe cursor, the per-address lock, the
#: address-registration directory) were the deleted WATCHER's own state;
#: this module owns only the marker/cleared-record pair.
_STATE_SUBDIR = "tuple-watch"


def session_marker_path(state_dir: Path, claude_pid: int) -> Path:
    """``<state_dir>/tuple-watch/session.<claude_pid>``: the per-claude-pid
    session marker (nexus-6konb.12, MM-3.4 fix 1). Keyed on the CLAUDE
    ancestor pid, never a session id -- the whole point is to tell a
    reader that resolved an OLDER session id for this pid that a NEWER
    one now exists for the same conversation, so the pid has to be the
    stable half of the pair (see :func:`write_session_marker`).
    """
    return state_dir / _STATE_SUBDIR / f"session.{claude_pid}"


def write_session_marker(state_dir: Path, claude_pid: int, session_id: str) -> None:
    """Best-effort, atomic marker naming the NEW session id for
    *claude_pid* (nexus-6konb.12, MM-3.4 fix 1).

    Originally replaced the model-dependent TaskStop rule the deleted
    Monitor-arm instruction used to carry: that rule could not work after
    ``/clear`` in the first place, because the fresh conversation it would
    run in has no memory of the OLD Monitor's harness task id -- there was
    never a way for a genuinely new context to discover it. This marker
    sidesteps the discovery problem instead of solving it: a reader checks
    its OWN pid's marker, not a task id nothing hands it. RDR-211
    nexus-rplay.14 deleted the watcher that read this marker for its own
    self-stop; the marker's remaining readers are
    ``nexus.tuple_directory.resolve_default_from`` (``mailbox_send``'s
    default ``from``) and this module's own
    :func:`record_clear_and_write_session_marker`.

    Reuses the nexus-d76vc T1-handoff pattern (:mod:`nexus.daemon.t1_handoff`):
    the writer (``nexus.hooks.session_start``, on every SessionStart source)
    and a reader each independently derive the SAME claude_pid via
    :func:`nexus.session.find_immediate_claude_pid`, walking process
    ancestry from wherever they happen to run up to the first ``claude*``
    process. The pid is never passed between them -- it is recomputed on
    both sides.

    Called only from a best-effort caller (see
    ``nexus.hooks._write_tuple_watch_session_marker``): a failure here is
    never a reason to fail SessionStart over it.
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
    :mod:`nexus.hooks.mailbox_drain`. *session_id* is the NEW
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
