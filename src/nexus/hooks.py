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
    find_session_by_id,
    generate_session_id,
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

    # Initialize session_id before the lock block so it is always bound even
    # if flock or find_session_by_id raises an unexpected exception. The
    # claude_session_id arrives via stdin from the SessionStart hook payload
    # (commands/hook.py) — that's the canonical Claude conversation UUID.
    session_id = claude_session_id or generate_session_id()

    # Honor NEXUS_SKIP_T1 — set by ``claude_dispatch`` (and any caller of
    # ``claude -p`` where T1 inheritance is undesirable) so short-lived
    # operator subprocesses don't pay the chroma startup cost or pollute
    # session bookkeeping. The T1 client (``db/t1.py``) falls back to
    # EphemeralClient when no server record is found, so skipping the
    # server start here yields the right "no T1" semantics automatically.
    skip_t1 = os.environ.get("NEXUS_SKIP_T1", "").strip().lower() in ("1", "true", "yes")

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
                    write_session_record_by_id(
                        SESSIONS_DIR, session_id, host, port, server_pid, tmpdir
                    )
                except Exception as exc:
                    _log.warning(
                        "session_start: T1 server unavailable; T1 will be local-only",
                        error=str(exc),
                    )
        finally:
            os.close(lock_fd)  # closing the fd releases the flock automatically

    # Persist the UUID for cross-tree inheritance via the ``current_session``
    # flat file. This is the actual mechanism that propagates the session ID
    # across Claude Code → Bash tool → nx command boundaries (subagent
    # SessionStart hooks read the file, find the parent's UUID, and adopt
    # the parent's T1 server). Always written, even when skip_t1 is set, so
    # a subagent invoked from a skip-T1 parent can still cohere on its own
    # session if it later writes a server record.
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
    # Resolve the Claude conversation UUID from the env var the SessionStart
    # hook exported, or from the legacy ``current_session`` flat file as
    # fallback for processes launched outside the SessionStart's child tree.
    session_id = os.environ.get("NX_SESSION_ID")
    if not session_id:
        from nexus.session import read_claude_session_id
        session_id = read_claude_session_id()

    own_record: dict | None = None
    own_file: Path | None = None
    if session_id:
        own_file = SESSIONS_DIR / f"{session_id}.session"
        # Safe read: the file is written once at session_start by this
        # process and only deleted later in this function — no external
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

    # For T1 flush: prefer the owned record; otherwise look up by UUID
    # (child-agent scenario where the env var was inherited).
    flush_record = own_record or find_session_by_id(SESSIONS_DIR, session_id)
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
    if own_record and own_file is not None:
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
