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
    generate_session_id,
    start_t1_server,
    stop_t1_server,
    sweep_stale_sessions,
    write_claude_session_id,
    write_session_record,
)


_log = structlog.get_logger()

# -- Helpers ------------------------------------------------------------------

def _default_db_path() -> Path:
    return Path.home() / ".config" / "nexus" / "memory.db"


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
    # Sweep orphaned server processes from previous crashed sessions.
    sweep_stale_sessions(SESSIONS_DIR)

    # Initialize session_id before the lock block so it is always bound even if
    # flock or find_ancestor_session raises an unexpected exception.
    session_id = claude_session_id or generate_session_id()

    # Acquire an exclusive lock before the find+write sequence to prevent two
    # sibling agents (same parent PID, both see ancestor=None simultaneously)
    # from each starting their own ChromaDB server and orphaning the first.
    SESSIONS_DIR.mkdir(parents=True, exist_ok=True, mode=0o700)
    lock_fd = os.open(str(SESSIONS_DIR / "session.lock"), os.O_CREAT | os.O_WRONLY, 0o600)
    try:
        fcntl.flock(lock_fd, fcntl.LOCK_EX)
        ancestor = find_ancestor_session(SESSIONS_DIR)
        if ancestor:
            session_id = ancestor["session_id"]
        else:
            # Root session: start the ChromaDB server with the pre-generated ID.
            # Key the session file to the grandparent (Claude Code's PID) rather
            # than the immediate parent (a transient shell subprocess that dies
            # as soon as the hook exits).  This ensures subsequent Bash calls from
            # the same Claude Code process share the same T1 session.
            _direct_ppid = os.getppid()
            ppid = _ppid_of(_direct_ppid) or _direct_ppid
            try:
                host, port, server_pid, tmpdir = start_t1_server()
                write_session_record(SESSIONS_DIR, ppid, session_id, host, port, server_pid, tmpdir)
            except Exception as exc:
                _log.warning(
                    "session_start: T1 server unavailable; T1 will be local-only",
                    error=str(exc),
                )
    finally:
        os.close(lock_fd)  # closing the fd releases the flock automatically

    # Keep writing the flat file for any external tooling that reads it.
    write_claude_session_id(session_id)

    # T2 memory context is surfaced by session_start_hook.py (via t2_prefix_scan.py)
    # which provides multi-namespace, snippet-enriched output. No duplication here.
    return f"Nexus ready. T1 scratch initialized (session: {session_id})."


# -- SessionEnd ---------------------------------------------------------------

def session_end() -> str:
    """Execute the SessionEnd hook.

    1. Check whether this process owns the session (its parent wrote the record).
    2. Flush flagged T1 entries to T2 (using whatever session is reachable via
       PPID chain — works for both parent and child agents).
    3. Run T2 expire().
    4. If owner: stop the ChromaDB server and delete the session record + tmpdir.
       Child agents skip the server-stop step.

    Returns a summary string.
    """
    ppid = os.getppid()
    own_file = SESSIONS_DIR / f"{ppid}.session"

    # Determine if this process owns the session (wrote the record at start).
    # own_file.read_text() is safe here: the file is written once at session_start
    # by this process and is only deleted later in this function — no external
    # writer can modify it between the exists() check and read_text().
    own_record: dict | None = None
    if own_file.exists():
        try:
            r = json.loads(own_file.read_text())
            if isinstance(r, dict) and "session_id" in r:
                own_record = r
        except (json.JSONDecodeError, OSError) as exc:
            _log.debug("session_end_own_record_corrupt", path=str(own_file), error=str(exc))

    # For T1 flush, use own record if available; otherwise walk the chain
    # (child-agent scenario where the parent's file is further up).
    flush_record = own_record or find_ancestor_session(SESSIONS_DIR)
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

    # Stop server and clean up only if this process owns the session.
    if own_record:
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

    return f"Session ended. Flushed {flushed} scratch entries. Expired {expired} memory entries."
