# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""SessionStart and SessionEnd hook logic for Claude Code integration."""
from __future__ import annotations

import json
import os
import sqlite3
import subprocess
from pathlib import Path

import structlog

from nexus.session import (
    generate_session_id,
    write_claude_session_id,
)


_log = structlog.get_logger()

# -- Helpers ------------------------------------------------------------------

def _default_db_path() -> Path:
    # RDR-128 P3: no longer opens T2 directly (session_end_flush routes its
    # writes through the daemon via mcp_infra.t2_index_write). Retained as
    # the config-dir-isolation canary asserted by
    # test_config_dir_isolation.TestT2IsolatedUnderOverride.
    from nexus.config import nexus_config_dir  # noqa: PLC0415 — deferred import; rare/branch-local path or circular-dep / startup-cost avoidance

    return nexus_config_dir() / "memory.db"


def _open_t1():
    """Open the process's T1 store for the SessionEnd flush, never honoring
    the shared-scope escape hatch.

    nexus-6a19f: ``NX_T1_ALLOW_SHARED_FALLBACK=1`` (see
    :func:`nexus.db.t1.get_t1_database`) exists so a caller with no usable
    lease for an explicit session id can opt back into the shared
    CLI-dedicated scope for READING/WRITING markers (e.g.
    ``conexus/hooks/scripts/pre_close_verification_hook.sh``). It must
    NEVER extend to :func:`session_end_flush`'s ``t1.clear()`` call below --
    clearing the shared scope on every lease-less SessionEnd would wipe
    every OTHER session's markers accumulated there (the nexus-6a19f
    Significant-1 finding: this was a live, undocumented, destructive bug
    pre-f7xyq). Force the flag off for the duration of this call regardless
    of what the ambient environment carries, so an explicit-but-unleased
    session id always takes the fail-loud branch here -- ``t1`` stays
    ``None``, and the ``if t1 is not None: t1.clear()`` guard below never
    fires for it.
    """
    from nexus.db.t1 import get_t1_database  # noqa: PLC0415 — deferred import; rare/branch-local path or circular-dep / startup-cost avoidance

    prev = os.environ.pop("NX_T1_ALLOW_SHARED_FALLBACK", None)
    try:
        return get_t1_database()
    finally:
        if prev is not None:
            os.environ["NX_T1_ALLOW_SHARED_FALLBACK"] = prev


def _infer_repo() -> str:
    """Detect current repo name from git, or fall back to cwd name."""
    try:
        result = subprocess.run(
            ["git", "rev-parse", "--show-toplevel"],
            capture_output=True, text=True, check=True, timeout=10,
        )
        return Path(result.stdout.strip()).name
    except Exception as exc:  # noqa: BLE001 — best-effort; error surfaced via log/echo, must not crash caller
        _log.debug("infer_repo_git_failed", error=str(exc))
        return Path.cwd().name


#: SessionStart ``source`` values that mean "the transcript now has a
#: DIFFERENT session id than whatever a live MCP server sibling last
#: sampled at spawn" (nexus-d76vc). ``startup`` spawns fresh MCP servers
#: (nothing frozen yet, nothing to hand off) and ``compact`` keeps the
#: SAME session id (no divergence) -- neither writes a marker. ``fork``
#: (``--fork-session``) is a real Claude Code source value too but is
#: deliberately NOT included here: the bead's design of record enumerates
#: clear/resume only, and widening to fork is scope this bead did not
#: size or test -- tracked as a follow-up, not silently folded in.
_T1_HANDOFF_SOURCES = frozenset({"clear", "resume"})


def _write_t1_handoff_markers(new_session_id: str) -> None:
    """nexus-d76vc: hand THIS conversation's new session id to any live
    MCP server sibling of the current claude process, so its frozen T1
    scope can follow across ``/clear``/``/resume`` instead of staying
    pinned to the pre-event session (the nexus-aj564 split-brain).

    Ancestry authentication (MUST-HOLD rn3wo.1) happens entirely via
    *selection*: :func:`~nexus.session.find_mcp_sibling_pids` only ever
    returns pids that are LIVE, IMMEDIATE children of this hook's own
    claude ancestor -- there is no separate "verify" step because a
    marker is never written for a pid that was not found this way, so a
    concurrent session's hook (walking its OWN, necessarily different,
    claude ancestor) can structurally never enumerate this session's MCP
    pids. The watcher side (``nexus.mcp.core``) independently re-derives
    its own ancestor and re-checks anyway -- defense in depth, never
    trusting the marker file alone from either direction.

    Best-effort: any failure (no `ps`, no siblings, disk error) is
    logged at debug and swallowed -- a SessionStart hook must never fail
    the session over a T1-scope convenience feature. Every non-T1 tool,
    and T1 itself via the frozen pre-existing scope, keeps working either
    way.
    """
    try:
        from nexus.config import nexus_config_dir  # noqa: PLC0415 — deferred import; rare/branch-local path
        from nexus.daemon.t1_handoff import write_handoff_marker  # noqa: PLC0415 — deferred import; rare/branch-local path
        from nexus.session import (  # noqa: PLC0415 — deferred import; rare/branch-local path
            find_immediate_claude_pid,
            find_mcp_sibling_pids,
        )

        claude_pid = find_immediate_claude_pid()
        if claude_pid <= 0:
            return
        mcp_pids = find_mcp_sibling_pids(claude_pid)
        if not mcp_pids:
            return
        config_dir = nexus_config_dir()
        for mcp_pid in mcp_pids:
            write_handoff_marker(
                mcp_pid,
                new_session_id=new_session_id,
                claude_pid=claude_pid,
                config_dir=config_dir,
            )
    except Exception as exc:  # noqa: BLE001 — best-effort; hook must never crash session-start over a T1-scope convenience feature
        _log.debug("t1_handoff_marker_write_failed", error=str(exc))


# -- SessionStart -------------------------------------------------------------

def session_start(claude_session_id: str | None = None, source: str | None = None) -> str:
    """Execute the SessionStart hook.

    Resolves the session UUID and persists it to ``current_session``
    so cross-process tools (shell ``nx scratch``, doctor diagnostics,
    SessionEnd flush) can look it up. Nested subprocesses (operator
    ``claude -p`` calls, subagents) inherit ``NX_SESSION_ID`` from
    their parent's env; their SessionStart must leave the parent's
    pointer alone so the parent's shell-side tools stay in sync.

    Chroma lifecycle is owned by the MCP server's FastMCP lifespan
    (RDR-105 P4) and is no fixture of this hook. Multi-writer record
    machinery (``sessions/<uuid>.session``, sweep, reconcile) was
    deleted in P4; the hook does session-id propagation only.

    ``source`` (nexus-d76vc, RDR-105 aj564 follow-up): the Claude Code
    SessionStart ``source`` field (``startup``/``resume``/``clear``/
    ``compact``/``fork``). On ``resume``/``clear`` -- the two sources
    where the transcript's session id changes out from under a live,
    already-frozen MCP server -- writes a T1 handoff marker for each MCP
    server sibling so its FastMCP lifespan can re-lease onto the new
    session id (see :func:`_write_t1_handoff_markers`). ``startup``
    spawns brand-new MCP servers (nothing frozen yet) and ``compact``
    keeps the same session id (no divergence); neither writes a marker.
    """
    # Resolve session_id with this precedence:
    #   1. ``NX_SESSION_ID`` env: nested subprocess that inherited the
    #      parent's UUID. Leave ``current_session`` untouched.
    #   2. ``claude_session_id`` from stdin: top-level Claude session.
    #   3. Fresh UUID: fallback for invocations outside Claude Code.
    inherited = os.environ.get("NX_SESSION_ID", "").strip() or None
    session_id = inherited or claude_session_id or generate_session_id()

    if not inherited:
        write_claude_session_id(session_id)

    if source in _T1_HANDOFF_SOURCES and session_id and session_id != "unknown":
        _write_t1_handoff_markers(session_id)

    # nexus-gff3g: do NOT claim "T1 scratch initialized" here. This hook only
    # records the session-id; T1 chroma is owned by the MCP server's FastMCP
    # lifespan (RDR-105 P4), which may key its lease on a DIFFERENT session-id
    # (its NX_SESSION_ID) than the one written above. Claiming initialization
    # masked exactly that divergence during the 5.10.x T1-scratch failure.
    # RDR-155 P4b: the substrate-migration bridge notice (nexus-0rwwv) died
    # with the migration machinery; stranded pre-PG installs are redirected
    # to the LAST_MIGRATION_CAPABLE release by the stranded-install detector.
    return f"Nexus ready (session: {session_id})."


# -- SessionEnd ---------------------------------------------------------------


def session_end_flush() -> str:
    """Run the storage-only portion of SessionEnd: T1 flush + T2 expire.

    Fork-safe: each call opens fresh T1/T2 handles and does not touch
    module-level state acquired pre-fork. Constructs ``T1Database()``
    so any flagged scratch entries can be flushed; if T1 cannot be
    resolved (no live MCP, no addr file, no isolation flag), the
    constructor's fail-loud raise surfaces the gap and the flush is
    skipped.

    ``T1ServerNotFoundError`` frequency (nexus-6a19f, updated from the
    original "known race window" framing below): since nexus-f7xyq this
    raise is no longer a narrow teardown-race case -- it fires for the
    ROUTINE split-brain scenario where this detached grandchild's
    resolvable session id (``NX_SESSION_ID`` inherited via ``fork()``, or
    the current transcript's ``CLAUDE_CODE_SESSION_ID``) has no fresh
    published T1 lease, which is common whenever the owning MCP process
    was spawned for a DIFFERENT (earlier or divergent) session than the
    one this hook invocation resolves. This is expected, not exceptional:
    a best-effort flush that skips under session-id divergence is the
    correct, session-isolation-safe behavior -- the alternative (silently
    reading/writing a different session's shared scope, the pre-f7xyq
    bug) was worse. ``_open_t1()`` additionally forces
    ``NX_T1_ALLOW_SHARED_FALLBACK`` off for this call (see its own
    docstring) so this path can never be talked into the shared-scope
    ``clear()`` below via that escape hatch.

    Known race window (the original, narrower case this docstring used to
    describe exclusively)
        On stdio transport the SessionEnd hook fires when stdin EOFs,
        which is the same event that drives the MCP server's lifespan
        ``async finally`` to relinquish its T1 lease record
        (``~/.config/nexus/t1_addr.<session_id>``) and stop chroma
        (RDR-149 P4). The launcher daemonizes ``session_end_flush``
        in a grandchild, but if the lifespan finally wins the race the
        grandchild's ``T1Database()`` resolves the session-id, finds no
        live lease, and raises ``T1ServerNotFoundError``. The
        ``except`` below catches the raise and logs
        ``session_end_flush_t1_unavailable``; flagged entries are then
        silently dropped. Best-effort flush is the documented contract;
        a future improvement would be for the lifespan to drain the
        flagged-entries queue itself before unlinking the addr file.
    """
    flushed = 0
    expired = 0

    try:
        try:
            t1 = _open_t1()
        except Exception as exc:  # noqa: BLE001 — best-effort; error surfaced via log/echo, must not crash caller
            _log.warning(
                "session_end_flush_t1_unavailable",
                error=str(exc),
                message="flagged scratch entries were not flushed",
            )
            t1 = None
        # T1 access is process-local; snapshot the flagged entries here so
        # only the T2 writes cross the daemon boundary below.
        entries = list(t1.flagged_entries()) if t1 is not None else []

        def _flush_and_expire(db):
            n = 0
            for entry in entries:
                db.memory.put(
                    project=entry["flush_project"],
                    title=entry["flush_title"],
                    content=entry["content"],
                    tags=entry.get("tags", ""),
                    ttl=None,
                )
                n += 1
            # Flushed entries are permanent (ttl=None); the expire sweep
            # below only reaps already-expired rows, so flush-then-expire
            # ordering is safe.
            return n, db.expire()

        # RDR-128 P3 (nexus-sbxbe.3): route the flush + TTL sweep through the
        # T2 daemon so the detached SessionEnd grandchild does not open
        # memory.db directly and contend on its single WAL writer lock.
        # t2_index_write falls back to a direct T2Database when the daemon
        # is unreachable (the grandchild can outlive the MCP lifespan).
        from nexus.mcp_infra import t2_index_write  # noqa: PLC0415 — deferred import; rare/branch-local path or circular-dep / startup-cost avoidance
        flushed, expired = t2_index_write(_flush_and_expire)
        # nexus-6a19f: `t1 is None` here covers every case where
        # `_open_t1()` raised -- including, since nexus-f7xyq, an explicit
        # session id with no usable lease. That is load-bearing, not
        # incidental: pre-f7xyq, that same case returned the SHARED
        # CLI-dedicated store (the fallback identity every bare `nx
        # scratch` invocation on the machine reads/writes), and clear()
        # below would have wiped that ENTIRE shared scope -- every
        # session's markers, not just this one's -- on every lease-less
        # SessionEnd. `_open_t1()` forces `NX_T1_ALLOW_SHARED_FALLBACK` off
        # for its call, so that escape hatch can never route this
        # particular clear() at the shared scope either. Only ever call
        # clear() on a store this process can prove is scoped to ITS OWN
        # session (`t1 is not None` means USE_INHERITED, USE_LEASED, or the
        # genuinely-bare CLI-dedicated mint -- never the fail-loud branch).
        if t1 is not None:
            t1.clear()
    except (sqlite3.Error, OSError) as exc:
        _log.warning("session_end: storage error during flush/expire", error=str(exc))

    return f"Session ended. Flushed {flushed} scratch entries. Expired {expired} memory entries."


def session_end() -> str:
    """Execute the SessionEnd hook.

    Thin wrapper around :func:`session_end_flush`. nx-mcp owns chroma
    teardown via its FastMCP lifespan + signal handler + atexit chain
    (RDR-094 Phase 4, unconditional as of 4.13.0); the watchdog is the
    safety net if all three of those paths fail. The hook does T1
    flush + T2 expire only.
    """
    return session_end_flush()
