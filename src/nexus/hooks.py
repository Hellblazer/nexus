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
    generate_session_id,
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

    return f"Nexus ready. T1 scratch initialized (session: {session_id})."


# -- SessionEnd ---------------------------------------------------------------


def session_end_flush() -> str:
    """Run the storage-only portion of SessionEnd: T1 flush + T2 expire.

    Fork-safe: each call opens fresh T1/T2 handles and does not touch
    module-level state acquired pre-fork. Constructs ``T1Database()``
    so any flagged scratch entries can be flushed; if T1 cannot be
    resolved (no live MCP, no addr file, no isolation flag), the
    constructor's fail-loud raise surfaces the gap and the flush is
    skipped.

    Known race window
        On stdio transport the SessionEnd hook fires when stdin EOFs,
        which is the same event that drives the MCP server's lifespan
        ``async finally`` to unlink ``~/.config/nexus/t1_addr.<pid>``
        and stop chroma. The launcher daemonizes ``session_end_flush``
        in a grandchild, but if the lifespan finally wins the race the
        grandchild's ``T1Database()`` walks the PPID chain, finds no
        addr file, and raises ``T1ServerNotFoundError``. The
        ``except`` below catches the raise and logs
        ``session_end_flush_t1_unavailable``; flagged entries are then
        silently dropped. Best-effort flush is the documented contract;
        a future improvement would be for the lifespan to drain the
        flagged-entries queue itself before unlinking the addr file.
    """
    flushed = 0
    expired = 0

    try:
        with _open_t2() as db:
            try:
                t1 = _open_t1()
            except Exception as exc:
                _log.warning(
                    "session_end_flush_t1_unavailable",
                    error=str(exc),
                    message="flagged scratch entries were not flushed",
                )
                t1 = None
            if t1 is not None:
                for entry in t1.flagged_entries():
                    db.put(
                        project=entry["flush_project"],
                        title=entry["flush_title"],
                        content=entry["content"],
                        tags=entry.get("tags", ""),
                        ttl=None,
                    )
                    flushed += 1
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
