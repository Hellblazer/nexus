# SPDX-License-Identifier: AGPL-3.0-or-later
"""Per-session capability census append at SessionEnd (nexus-h33x8.3).

Delivery is Tier B: this module is imported only from the fully
detached grandchild path in :mod:`nexus._session_end_launcher`
(``_run_session_end_synchronously`` -> ``_write_capability_census``),
never at that launcher's top level -- the pre-fork budget invariant
documented there is untouched by this module's existence.

VISIBILITY IS SETTLED BY SOURCE, not experiment (bead nexus-h33x8.3,
2026-08-01 comment): the grandchild's stdio is redirected to
``/dev/null`` before this module is ever imported, so anything this
module does is PROVABLY INVISIBLE on screen. The durable JSONL append
below is therefore the PRIMARY artifact, not a fallback -- readable
later via ``nx census capability --session <id>`` or a direct read of
the log.

A parent-side (post-fork) VISIBLE line was considered and rejected on
cost grounds, not skipped for convenience: a full per-session capability
census re-walks every transcript file for the session (main +
subagent-*.jsonl), and that cost is DATA-DEPENDENT and effectively
unbounded (measured 2026-08-20 against this repo's own real
``~/.claude/projects/-Users-hal-hildebrand-git-nexus`` transcripts:
~0.21s for a 70-subagent-file/6k-tool-call session, ~0.50s for an
88MB/62-file session -- both comfortably inside the SessionEnd hook's
10s budget today, but the cost scales with session size with no cap,
unlike ``_print_service_tier_summary``'s single hard-2.0s-timeout HTTP
read). Re-running that walk a second time in the parent, synchronously,
purely for an on-screen nicety, was judged not "cheap" in the sense the
bead requires -- so per the bead's own escape hatch ("if it can't be
done cheaply, skip the visible line entirely -- JSONL alone satisfies
the bead"), there is no visible line. All measurement happens exactly
once, in the grandchild, off the hook-timeout critical path entirely.

Reuses nexus-h33x8.1's shipped transcript-parsing machinery
(:mod:`nexus.census`) rather than re-implementing it -- see
``census_corpus``/``census_session_dispatches``.

FUTURE-READER WARNING (code-review Significant, fix pass 2026-08-20): as
of this writing NOTHING reads ``capability_census.jsonl`` -- confirmed by
grep across ``src/``/``tests/``/``conexus/``, not assumed (nexus-h33x8.3
fix-pass-3 dev notes). That "no reader yet" state is exactly where a time
bomb hides: whoever eventually writes the first reader (a
``nx census capability --from-log`` variant, a longitudinal-trend command,
whatever it turns out to be) MUST merge the rotated ``.1`` generation
(oldest-first) with the live file, or it will silently see only the
post-rotation slice and half its history will look like it never
happened. Do not discover this the hard way -- :func:`nexus.routing_stats
._iter_records` already solved the identical problem for
``routing_log.jsonl`` (same rotation scheme, same two-file merge, oldest-
first); copy that pattern, don't reinvent it.

ROTATION, NOT TRIM-IN-PLACE (Sam-directed fix pass, 2026-08-20): the log
grows without bound otherwise. Rewriting the file in place to keep only
the newest N lines is a foot-cannon for a MULTI-WRITER append log like
this one -- a concurrent SessionEnd appender from another Claude Code
session can interleave a read-modify-write with its own line-atomic
append (clobbering that append), and a crash partway through the
rewrite loses the file outright. Rotation by atomic rename
(``os.replace``/``Path.replace``, atomic on POSIX) never reads the
file's content at all: at every instant the path either names the
pre-rotation file or nothing, never a half-written intermediate. See
:func:`_rotate_log_if_oversized` for the mechanics -- do NOT
"simplify" this back into a rewrite; that is the exact class of bug
this design avoids.
"""
from __future__ import annotations

import datetime
import json
import os
import pathlib
from typing import Any

#: Filename for the durable per-session capability census log, alongside
#: ``routing_log.jsonl`` -- same directory in the common case, but NOT the
#: same precedent: ``nexus.routing_stats.default_log_path`` hardcodes
#: ``Path.home()`` and only honours ``NX_ROUTING_LOG_PATH``, while this
#: log resolves through :func:`nexus.config.nexus_config_dir` and honours
#: ``NEXUS_CONFIG_DIR`` (the bead's explicit requirement) -- the two
#: diverge whenever ``NEXUS_CONFIG_DIR`` is set (sandboxes, tests).
_LOG_FILENAME = "capability_census.jsonl"

#: Byte cap that triggers rotation (~1 MiB). A capability-census record
#: runs a few hundred bytes; years of one-record-per-SessionEnd headroom
#: fit comfortably under this before the FIRST rotation ever fires. A
#: byte ``stat()`` is O(1) -- checking this on every write is deliberately
#: cheap, unlike a line count (O(n), would require reading the whole file).
_LOG_ROTATION_MAX_BYTES = 1_048_576


def _rotate_log_if_oversized(log_path: pathlib.Path) -> None:
    """Rotate ``log_path`` to ``<name>.1`` via atomic rename if it has
    grown past :data:`_LOG_ROTATION_MAX_BYTES`. Never a read-modify-write
    -- see the module docstring's ROTATION, NOT TRIM-IN-PLACE section for
    why.

    Exactly one older generation is retained: any existing ``.1`` is
    CLOBBERED by ``os.replace`` (POSIX rename semantics), never pushed to
    ``.2`` -- bounding total on-disk size at roughly 2x the cap ONCE
    rotation has run at least once. The FIRST rotation is an exception to
    that bound: a pre-existing file already over the cap when this code
    first ships (e.g. this project's own real ``routing_log.jsonl``,
    observed at ~5MB against a 1 MiB cap) lands in ``.1`` WHOLE, not
    truncated to the cap -- and persists at that size until the NEXT
    rotation clobbers it. Steady-state is bounded; the one-time
    first-rotation transient is not.

    TOCTOU DOUBLE-ROTATION CLOBBER (code-review Critical, nexus-g3jw6,
    fix pass 2026-08-20) -- why this function takes a lock at all: two
    concurrent rotators, P1 and P2, can BOTH observe the file oversize
    (P2's observation stale by the time it acts). P1 rotates the real
    history into ``.1`` and reappends a small live file. If P2 then
    blindly replays its stale decision, its rename SUCCEEDS AGAIN (the
    live path exists again) and CLOBBERS P1's real ``.1`` with P1's
    small reappended content -- silent, irreversible history loss, not
    the benign "someone else already rotated it" FileNotFoundError case
    below. FIX: a non-blocking advisory lock on a sidecar
    ``<name>.rotate.lock`` serializes the {re-stat, os.replace}
    critical section across rotators. Appends stay completely lock-free
    (unchanged, single ``fh.write()`` in the caller) -- only the RARE
    rotation path pays any lock cost, and only once oversize was
    observed at all. Losing the lock race (``BlockingIOError``) means
    someone else is rotating right now -- skip entirely, do not block
    or retry. Winning the lock means re-stat under it: if the file is no
    longer oversize (someone else already rotated, e.g. P1 finished
    first), skip -- the earlier, unlocked stat() that triggered this
    call may be stale, but the DECISION to actually rename is always
    made with fresh data. This eliminates the stale-observation rename
    by construction, not merely narrows its window -- a bare re-stat
    immediately before ``os.replace`` WITHOUT the lock would still let
    two rotators interleave their re-stat and replace calls.

    A concurrent rotation race that manifests as ``FileNotFoundError``
    on the rename itself (another process's rotation completed between
    OUR re-stat-under-the-lock and OUR own replace -- possible only if
    that other process is not participating in this same lock, e.g. a
    pre-upgrade version of this code) is still tolerated silently: the
    file is rotated either way, which is what this function promises.
    Any OTHER failure (permission error, disk error, lock file
    unopenable, ...) is the caller's responsibility to isolate; this
    function does not swallow those itself, so a genuinely unexpected
    failure stays diagnosable rather than silently absorbed at two
    different layers.
    """
    try:
        size = log_path.stat().st_size
    except FileNotFoundError:
        return  # nothing to rotate

    if size < _LOG_ROTATION_MAX_BYTES:
        return  # cheap, lock-free common case: not even apparently oversize

    # Apparently oversize (per a possibly-stale stat()) -- escalate to the
    # serialized, re-checked critical section. Everything from here down
    # runs at most once per rotation event across all processes that
    # honor this lock.
    from nexus._locking import lock_file, unlock_file  # noqa: PLC0415 — deferred; only needed on the rare rotation path

    lock_path = log_path.with_name(log_path.name + ".rotate.lock")
    try:
        lock_fd = os.open(str(lock_path), os.O_RDWR | os.O_CREAT, 0o644)
    except OSError:
        return  # can't even open the lockfile -- best-effort, skip rotation
    lock_file_obj = os.fdopen(lock_fd, "r+")
    try:
        try:
            lock_file(lock_file_obj, blocking=False)
        except BlockingIOError:
            # Someone else is inside the rotation critical section right
            # now -- skip entirely rather than wait or race them.
            return

        # RE-CHECK under the lock: the stat() above may be stale (another
        # rotator may have already rotated -- and even reappended -- since
        # then). Only rotate if STILL oversize right now.
        try:
            size = log_path.stat().st_size
        except FileNotFoundError:
            return  # nothing left to rotate
        if size < _LOG_ROTATION_MAX_BYTES:
            return  # a fresher rotator already handled it

        rotated = log_path.with_name(log_path.name + ".1")
        try:
            os.replace(log_path, rotated)
        except FileNotFoundError:
            # Another (non-participating) process removed/rotated the
            # file between our re-stat and our rename -- rotated either
            # way.
            pass
    finally:
        try:
            unlock_file(lock_file_obj)
        except OSError:
            pass
        lock_file_obj.close()


def capability_census_log_path() -> pathlib.Path:
    """Durable JSONL path for the per-session capability census.

    Resolves through :func:`nexus.config.nexus_config_dir` (honours
    ``NEXUS_CONFIG_DIR``) rather than hardcoding ``Path.home()`` --
    the bead is explicit that the config dir must be resolved properly.
    """
    from nexus.config import nexus_config_dir  # noqa: PLC0415 — deferred; only needed here

    return nexus_config_dir() / _LOG_FILENAME


def _now_iso() -> str:
    return datetime.datetime.now(datetime.timezone.utc).isoformat()


def _blindspot_record(session_id: str, reason: str) -> dict[str, Any]:
    """A BLINDSPOT record: explicit marker, no zeroed counts.

    Verification 3 (nexus-h33x8.3): a session whose transcript is
    unreadable/absent at end-of-session must never be reported as a
    clean zero -- that would be indistinguishable from a session that
    genuinely used nothing.
    """
    return {
        "session_id": session_id,
        "timestamp": _now_iso(),
        "blindspot": True,
        "unmeasurable_reason": reason,
    }


def _zero_record(session_id: str) -> dict[str, Any]:
    """A real, measured all-zero record -- not a blindspot.

    code-review Important #1 (fix pass, 2026-08-20): ``census_session``'s
    own precedence chain (``nexus/census.py``) treats "readable, parsed
    cleanly, but genuinely zero tool_use blocks of any kind" as
    ``UNMEASURABLE_NO_TOOL_USE`` -- a MEASUREMENT-machinery label, not a
    measurement FAILURE. A session that truly used nothing is exactly
    the ``skills=0 nx_answer=0 ...`` line the bead's own example shows;
    collapsing it into the same blindspot bucket as an unreadable or
    missing transcript would make the zero indistinguishable from a
    measurement gap, defeating the whole point of the BLINDSPOT marker.
    Built directly from ``CAPABILITIES`` rather than via
    ``CorpusCensus.total_calls`` -- the session's own orchestrator/
    subagent dicts ARE empty in this branch by construction, but stating
    the zeros explicitly here is not incidental on that.
    """
    from nexus.census import CAPABILITIES  # noqa: PLC0415 — deferred; only needed here

    return {
        "session_id": session_id,
        "timestamp": _now_iso(),
        "blindspot": False,
        "capabilities": dict.fromkeys(CAPABILITIES, 0),
        "dispatches": 0,
        "total_calls": 0,
    }


def build_capability_census_record(
    project_dir: pathlib.Path, session_id: str,
) -> dict[str, Any]:
    """Build one census record for ``session_id``.

    Reuses :func:`nexus.census.census_corpus` (scoped to the single
    session) for the roll-up math and BLINDSPOT/measurability
    determination, and :func:`nexus.census.census_session_dispatches`
    for the recognized-dispatch count -- both nexus-h33x8.1/.2 machinery,
    not reimplemented here.

    Counts only, never verdicts: this record carries per-capability
    call counts and a dispatch count, nothing that says "you should
    have used X" (bead nexus-h33x8.3).
    """
    from nexus.census import (  # noqa: PLC0415 — deferred; only needed here
        CAPABILITIES,
        UNMEASURABLE_NO_TOOL_USE,
        census_corpus,
        census_session_dispatches,
    )

    result = census_corpus(project_dir, session=session_id)

    if result.scope_error:
        return _blindspot_record(session_id, result.scope_error)
    if not result.sessions:
        reason = (
            result.unmeasurable[0].unmeasurable_reason
            if result.unmeasurable and result.unmeasurable[0].unmeasurable_reason
            else "unknown"
        )
        if reason == UNMEASURABLE_NO_TOOL_USE:
            return _zero_record(session_id)
        return _blindspot_record(session_id, reason)

    capabilities = {cap: result.total_calls(cap) for cap in CAPABILITIES}

    # code-review suggestion #2 (fix pass, 2026-08-20): the ``.measurable``
    # guard this used to carry was dead code, not defensive -- provably so.
    # census_session_dispatches's OWN precedence chain sets
    # unmeasurable_reason=None the moment total_tool_use_blocks > 0, and
    # every unmeasurable branch (MISSING/UNREADABLE/EMPTY/UNPARSEABLE/
    # NO_TOOL_USE) implies zero tool_use blocks were successfully parsed,
    # which implies zero Agent-dispatch blocks among them -- so
    # ``dispatch_census.dispatches`` is already ``[]`` in exactly the cases
    # the guard existed to catch. Removed rather than kept as inert
    # ceremony that looked like real degradation handling.
    dispatch_census = census_session_dispatches(project_dir, session_id)
    dispatches = len(dispatch_census.dispatches)

    return {
        "session_id": session_id,
        "timestamp": _now_iso(),
        "blindspot": False,
        "capabilities": capabilities,
        "dispatches": dispatches,
        "total_calls": sum(capabilities.values()),
    }


def write_session_capability_census(session_id: str | None = None) -> dict[str, Any] | None:
    """Append one capability-census record to the durable JSONL log.

    Returns the record written, or ``None`` when no session id resolves
    (nothing meaningful to census -- mirrors
    ``_print_service_tier_summary``'s own silent no-op in that case).

    Does NOT swallow exceptions raised while measuring or writing --
    that is the caller's job
    (``_session_end_launcher._write_capability_census``), which wraps
    this call and logs failures via structlog so a census bug can never
    break SessionEnd cleanup.
    """
    from nexus.census import default_project_dir  # noqa: PLC0415 — deferred; only needed here
    from nexus.session import resolve_active_session_id  # noqa: PLC0415 — deferred; only needed here

    sid = session_id or resolve_active_session_id()
    if not sid:
        return None

    project_dir = default_project_dir()
    record = build_capability_census_record(project_dir, sid)

    log_path = capability_census_log_path()
    log_path.parent.mkdir(parents=True, exist_ok=True)
    try:
        _rotate_log_if_oversized(log_path)
    except Exception as exc:  # noqa: BLE001 — rotation is best-effort; the durable record below is not
        # A rotation failure (anything beyond the FileNotFoundError race
        # _rotate_log_if_oversized already tolerates internally -- e.g. a
        # permission error on the rename, or on opening the rotate lock
        # file) must never prevent the append. code-review Important #4
        # (fix pass, 2026-08-20): a bare swallow here contradicted
        # _rotate_log_if_oversized's own "stays diagnosable" promise --
        # log it at debug level (matches
        # ``_session_end_launcher._write_capability_census``'s identical
        # discipline for its analogous failure case) so an environment
        # whose rotation silently fails forever is still debuggable from
        # the logs, not just from a growing file someone eventually
        # notices.
        try:
            import structlog  # noqa: PLC0415 — deferred; only needed on this rare failure path

            structlog.get_logger(__name__).debug(
                "capability_census_rotation_failed",
                error=str(exc),
            )
        except Exception:  # noqa: BLE001 — even the debug log is best-effort
            pass
    with log_path.open("a", encoding="utf-8") as fh:
        # code-review Important #2 (fix pass, 2026-08-20): ONE fh.write()
        # call with the complete line (payload + newline), matching the
        # codebase's own precedent for this exact append-log problem
        # (conexus/hooks/scripts/routing/_lib.py::log_routing_event).
        # Concurrent SessionEnd appenders from multiple Claude Code
        # sessions can hit this file near-simultaneously; two separate
        # write() calls relied on CPython's buffering happening to
        # coalesce them into one OS-level write, an implementation
        # detail rather than a guarantee against interleaved partial
        # lines from another process.
        fh.write(json.dumps(record, sort_keys=True) + "\n")
    return record
