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

from nexus.db.t2 import T2Database
from nexus.session import (
    SESSIONS_DIR,
    find_session_by_id,
    generate_session_id,
    sweep_stale_sessions,
    write_claude_session_id,
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

    # nx-mcp owns chroma's lifecycle (RDR-094 Phase 4, unconditional as
    # of 4.13.0). The hook no longer spawns chroma or the watchdog; the
    # MCP server does both via its FastMCP lifespan. We only persist the
    # session UUID below so child agents and shell-side tools can find
    # the parent's record. ``NEXUS_SKIP_T1`` is still honoured downstream
    # by ``T1Database.__init__`` (db/t1.py) for the stateless-operator
    # path; the hook itself does no T1 work.

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
    """Execute the SessionEnd hook.

    Thin wrapper around :func:`session_end_flush`. nx-mcp owns chroma
    teardown via its FastMCP lifespan + signal handler + atexit chain
    (RDR-094 Phase 4, unconditional as of 4.13.0); the watchdog is the
    safety net if all three of those paths fail. The hook does T1
    flush + T2 expire only.
    """
    return session_end_flush()
