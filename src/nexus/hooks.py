# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""SessionStart and SessionEnd hook logic for Claude Code integration."""
from __future__ import annotations

import fcntl
import json
import os
import shutil
import sqlite3
import subprocess
from pathlib import Path

import structlog

from nexus.db.t2 import T2Database
from nexus.session import (
    SESSIONS_DIR,
    _ppid_of,
    find_ancestor_session,
    find_claude_root_pid,
    find_session_by_id,
    generate_session_id,
    spawn_t1_watchdog,
    start_t1_server,
    stop_t1_server,
    sweep_stale_sessions,
    write_claude_session_id,
    write_session_record,
    write_session_record_by_id,
)


_log = structlog.get_logger()

# -- Helpers ------------------------------------------------------------------

def _default_db_path() -> Path:
    from nexus.config import nexus_config_dir

    return nexus_config_dir() / "memory.db"


def _open_t2() -> T2Database:
    return T2Database(_default_db_path())


def _open_t1():
    from nexus.db.t1 import T1Database
    return T1Database()


def _mcp_owns_t1_enabled() -> bool:
    """Return True when nx-mcp owns the chroma lifecycle (RDR-094 Phase 4).

    Default ON as of conexus 4.12.0. ``NEXUS_MCP_OWNS_T1=0`` (or
    ``false`` / ``no`` / ``off``) is the emergency opt-out; absence
    of the env var means ON.
    """
    raw = os.environ.get("NEXUS_MCP_OWNS_T1", "").strip().lower()
    if raw in ("0", "false", "no", "off"):
        return False
    # Default ON: empty string OR explicit truthy values.
    return True


def _infer_repo() -> str:
    """Detect current repo name from git, or fall back to cwd name."""
    try:
        result = subprocess.run(
            ["git", "rev-parse", "--show-toplevel"],
            capture_output=True, text=True, check=True, timeout=10,
        )
        return Path(result.stdout.strip()).name
    except Exception as exc:
        _log.debug("infer_repo_git_failed", error=str(exc))
        return Path.cwd().name


# -- SessionStart -------------------------------------------------------------

def session_start(claude_session_id: str | None = None) -> str:
    """Execute the SessionStart hook.

    1. Sweep stale orphaned server processes from previous sessions.
    2. Walk the PPID chain: if an ancestor session exists, adopt it (child agent).
       Otherwise start a new ChromaDB server and write a session record.
    3. Print recent T2 memory summary.

    Returns the output string to be printed.
    """
    # Sweep orphaned server processes from previous crashed sessions. Also
    # handles the migration: legacy numeric-stem session files written by
    # the old PID-keyed scheme are reaped unconditionally here.
    sweep_stale_sessions(SESSIONS_DIR)
    # RDR-094 Phase 3: sweep orphan nx_t1_* tmpdirs that have no live
    # session record and are older than 24h. The 24h cutoff protects
    # in-flight tmpdirs (mkdtemp -> write_session_record_by_id has a
    # small window; legitimate tmpdirs from active sessions never reach
    # the cutoff because their record reaches the filter first).
    try:
        from nexus.session import sweep_orphan_tmpdirs
        sweep_orphan_tmpdirs(SESSIONS_DIR)
    except Exception as exc:
        _log.debug("sweep_orphan_tmpdirs_failed", error=str(exc))

    # Resolve session_id with this precedence:
    #   1. ``NX_SESSION_ID`` env  — we're a nested subprocess our parent
    #      already populated. Inherit the parent's UUID so shell tools
    #      that look up by ``current_session`` still find the parent's
    #      record.
    #   2. ``claude_session_id`` from stdin — top-level Claude session.
    #      The hook payload (commands/hook.py) carries the canonical
    #      conversation UUID Claude Code generated.
    #   3. A fresh UUID — fallback for invocations outside Claude Code.
    inherited = os.environ.get("NX_SESSION_ID", "").strip() or None
    session_id = inherited or claude_session_id or generate_session_id()

    # Honor NEXUS_SKIP_T1, set by ``claude_dispatch`` (and any caller
    # of ``claude -p`` where T1 inheritance is undesirable) so short-
    # lived operator subprocesses don't pay the chroma startup cost
    # or pollute session bookkeeping. The T1 client (``db/t1.py``)
    # falls back to EphemeralClient when this env is set.
    #
    # NEXUS_MCP_OWNS_T1 (RDR-094 Phase 4, default-on as of 4.12.0)
    # moves chroma spawn to the nx-mcp lifespan. The hook leaves chroma
    # to the MCP server; we still run the sweep and write
    # ``current_session`` so other tools (legacy CLI usage, subagent
    # T1 inheritance) keep working. Set ``NEXUS_MCP_OWNS_T1=0`` (or
    # false / no / off) as the emergency opt-out.
    skip_t1 = os.environ.get("NEXUS_SKIP_T1", "").strip().lower() in ("1", "true", "yes")
    mcp_owns_t1 = _mcp_owns_t1_enabled()
    if mcp_owns_t1:
        skip_t1 = True

    if not skip_t1:
        # Acquire an exclusive lock before the find+write sequence to prevent
        # two sibling agents (same UUID, both see record-absent simultaneously)
        # from each starting their own ChromaDB server and orphaning the first.
        SESSIONS_DIR.mkdir(parents=True, exist_ok=True, mode=0o700)
        _session_lock = SESSIONS_DIR / "session.lock"
        from nexus.indexer import _clear_stale_lock
        _clear_stale_lock(_session_lock)
        lock_fd = os.open(str(_session_lock), os.O_CREAT | os.O_WRONLY | os.O_TRUNC, 0o600)
        try:
            os.write(lock_fd, str(os.getpid()).encode())
            os.fsync(lock_fd)
            fcntl.flock(lock_fd, fcntl.LOCK_EX)
            # UUID-keyed lookup: T1 is scoped to a Claude conversation, not a
            # terminal session. Two ``claude`` invocations in the same shell
            # get distinct UUIDs → distinct session files → distinct T1
            # servers. Subagents within one conversation share via the
            # ``current_session`` flat file written below.
            existing = find_session_by_id(SESSIONS_DIR, session_id)
            if existing is None:
                try:
                    host, port, server_pid, tmpdir = start_t1_server()
                    # nexus-99jb Layer 1: spawn a detached watchdog that
                    # watches the Claude Code root PID + the chroma PID
                    # and triggers graceful shutdown when Claude Code
                    # disappears. Independent of SessionEnd firing, so
                    # covers /exit (#17885) and Hook cancelled (#41577).
                    claude_root_pid = find_claude_root_pid()
                    session_file = SESSIONS_DIR / f"{session_id}.session"
                    watchdog_pid = spawn_t1_watchdog(
                        claude_pid=claude_root_pid,
                        chroma_pid=server_pid,
                        session_file=session_file,
                        tmpdir=tmpdir,
                    ) if claude_root_pid else 0
                    write_session_record_by_id(
                        SESSIONS_DIR, session_id, host, port, server_pid,
                        tmpdir, claude_root_pid=claude_root_pid,
                        watchdog_pid=watchdog_pid,
                    )
                except Exception as exc:
                    _log.warning(
                        "session_start: T1 server unavailable; T1 will be local-only",
                        error=str(exc),
                    )
        finally:
            os.close(lock_fd)  # closing the fd releases the flock automatically

    # Persist the UUID via ``current_session`` flat file — only when this is
    # a TOP-LEVEL session. Nested subprocesses (operator ``claude -p`` calls,
    # subagents) inherit ``NX_SESSION_ID`` from their parent's env and must
    # leave the parent's pointer alone. Without this guard, a nested
    # subprocess's SessionStart would stomp the flat file with its own
    # transient UUID — typically pointing at no on-disk session record at
    # all (because skip_t1 was set) — and the parent's shell-side
    # ``nx scratch`` / ``nx memory`` would then fall back to EphemeralClient
    # for the rest of the conversation.
    if not inherited:
        write_claude_session_id(session_id)

    # T2 memory context is surfaced by session_start_hook.py (via t2_prefix_scan.py)
    # which provides multi-namespace, snippet-enriched output. No duplication here.
    return f"Nexus ready. T1 scratch initialized (session: {session_id})."


# -- SessionEnd ---------------------------------------------------------------


def _resolve_session_records() -> tuple[str | None, dict | None, Path | None, dict | None]:
    """Resolve session id + owned record + flush record for the SessionEnd path.

    Shared between ``session_end_flush`` and the chroma-stop block in
    ``session_end``. Returns ``(session_id, own_record, own_file, flush_record)``.
    """
    session_id = os.environ.get("NX_SESSION_ID")
    if not session_id:
        from nexus.session import read_claude_session_id
        session_id = read_claude_session_id()

    own_record: dict | None = None
    own_file: Path | None = None
    if session_id:
        own_file = SESSIONS_DIR / f"{session_id}.session"
        # Safe read: the file is written once at session_start by this
        # process and only deleted later in this function -- no external
        # writer can modify it between the exists() check and read_text().
        if own_file.exists():
            try:
                r = json.loads(own_file.read_text())
                if isinstance(r, dict) and "session_id" in r:
                    own_record = r
            except (json.JSONDecodeError, OSError) as exc:
                _log.debug(
                    "session_end_own_record_corrupt",
                    path=str(own_file),
                    error=str(exc),
                )

    flush_record = own_record or find_session_by_id(SESSIONS_DIR, session_id)
    return session_id, own_record, own_file, flush_record


def session_end_flush() -> str:
    """Run the storage-only portion of SessionEnd: T1 flush + T2 expire.

    Splits cleanly out of ``session_end`` so Phase 4 (RDR-094) can wire
    hooks.json directly at this entry point: it does no chroma teardown,
    so it cannot race the MCP server's own lifespan/atexit/signal
    cleanup. ``session_end`` keeps the chroma-stop block for the
    feature-flag-off rollout window and is the legacy entry point.

    Importantly: this function is fork-safe. It only opens T1/T2
    SQLite handles (each call gets a fresh connection) and does not
    touch any module-level state acquired before fork. Phase C
    (nexus-l828) imports it post-fork in the launcher's grandchild.
    """
    _, _, _, flush_record = _resolve_session_records()
    if flush_record is None:
        _log.warning(
            "session_end: no T1 session record found; flagged scratch entries may not have been flushed"
        )

    flushed = 0
    expired = 0

    try:
        with _open_t2() as db:
            if flush_record:
                t1 = _open_t1()
                for entry in t1.flagged_entries():
                    db.put(
                        project=entry["flush_project"],
                        title=entry["flush_title"],
                        content=entry["content"],
                        tags=entry.get("tags", ""),
                        ttl=None,
                    )
                    flushed += 1
                # Clear only after all entries are flushed (T2.put is idempotent).
                t1.clear()

            expired = db.expire()
    except (sqlite3.Error, OSError) as exc:
        _log.warning("session_end: storage error during flush/expire", error=str(exc))

    return f"Session ended. Flushed {flushed} scratch entries. Expired {expired} memory entries."


def session_end() -> str:
    """Execute the SessionEnd hook (legacy entry point).

    Calls :func:`session_end_flush` for storage work, then -- when this
    process owns the session record AND ``NEXUS_MCP_OWNS_T1`` is not
    set -- stops the ChromaDB server and removes the session record +
    tmpdir.

    The ``NEXUS_MCP_OWNS_T1`` gate is the in-place mitigation from
    PR #300: when the MCP server owns chroma, hook-side teardown
    races the lifespan/atexit/signal-handler cleanup. Phase C
    (nexus-l828) swaps hooks.json's internal call to
    :func:`session_end_flush` directly so the gate becomes redundant;
    it stays here for the flag-off rollout window.
    """
    _, own_record, own_file, _ = _resolve_session_records()
    summary = session_end_flush()

    mcp_owns_t1 = _mcp_owns_t1_enabled()
    if own_record and own_file is not None and not mcp_owns_t1:
        server_pid = own_record.get("server_pid", 0)
        if server_pid:
            stop_t1_server(server_pid)
        try:
            own_file.unlink(missing_ok=True)
        except OSError:
            pass  # intentional: best-effort session file deletion
        tmpdir = own_record.get("tmpdir", "")
        if tmpdir:
            shutil.rmtree(tmpdir, ignore_errors=True)

    return summary
