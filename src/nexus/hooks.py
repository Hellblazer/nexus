# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""SessionStart and SessionEnd hook logic for Claude Code integration."""
from __future__ import annotations

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
    except Exception:
        return Path.cwd().name


# -- SessionStart -------------------------------------------------------------

def session_start(claude_session_id: str | None = None) -> str:
    """Execute the SessionStart hook.

    1. Sweep stale orphaned server processes from previous sessions.
    2. Walk the PPID chain: if an ancestor session exists, adopt it (child agent).
       Otherwise start a new ChromaDB server and write a session record.
    3. Detect PM project via T2 query.
    4. If PM: inject computed PM resume.
       Else: print recent memory summary.

    Returns the output string to be printed.
    """
    # Sweep orphaned server processes from previous crashed sessions.
    sweep_stale_sessions(SESSIONS_DIR)

    # Check for an existing ancestor session (child-agent scenario).
    ancestor = find_ancestor_session(SESSIONS_DIR)
    if ancestor:
        session_id = ancestor["session_id"]
    else:
        # Root session: generate ID and start the ChromaDB server.
        session_id = claude_session_id or generate_session_id()
        ppid = os.getppid()
        try:
            host, port, server_pid, tmpdir = start_t1_server()
            write_session_record(SESSIONS_DIR, ppid, session_id, host, port, server_pid, tmpdir)
        except Exception as exc:
            _log.warning(
                "session_start: T1 server unavailable; T1 will be local-only",
                error=str(exc),
            )

    # Keep writing the flat file for any external tooling that reads it.
    write_claude_session_id(session_id)

    lines: list[str] = [f"Nexus ready. T1 scratch initialized (session: {session_id})."]

    repo = _infer_repo()
    try:
        with _open_t2() as db:
            from nexus.pm import pm_resume
            blockers_row = db.get(project=repo, title="BLOCKERS.md")
            is_pm = blockers_row is not None and "pm" in (blockers_row.get("tags") or "")
            if is_pm:
                content = pm_resume(db, project=repo)
                if content:
                    lines.append(content)
            else:
                entries = db.list_entries(project=repo)[:10]
                if entries:
                    lines.append(f"Recent memory ({repo}, last {len(entries)} entries):")
                    for e in entries:
                        lines.append(f"  - {e['title']} ({e.get('agent') or '-'}, {e.get('timestamp', '')[:10]})")
                else:
                    lines.append(f"No memory entries for '{repo}'.")
    except (sqlite3.Error, OSError):
        lines.append("(memory unavailable)")

    return "\n".join(lines)


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
    own_record: dict | None = None
    if own_file.exists():
        try:
            r = json.loads(own_file.read_text())
            if isinstance(r, dict) and "session_id" in r:
                own_record = r
        except (json.JSONDecodeError, OSError):
            pass

    # For T1 flush, use own record if available; otherwise walk the chain
    # (child-agent scenario where the parent's file is further up).
    flush_record = own_record or find_ancestor_session(SESSIONS_DIR)

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
            pass
        tmpdir = own_record.get("tmpdir", "")
        if tmpdir:
            shutil.rmtree(tmpdir, ignore_errors=True)

    return f"Session ended. Flushed {flushed} scratch entries. Expired {expired} memory entries."
