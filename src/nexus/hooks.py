# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""SessionStart and SessionEnd hook logic for Claude Code integration."""
from __future__ import annotations

import subprocess
from pathlib import Path

from nexus.db.t2 import T2Database
from nexus.session import generate_session_id, session_file_path, write_session_file


# ── Helpers ───────────────────────────────────────────────────────────────────

def _default_db_path() -> Path:
    return Path.home() / ".config" / "nexus" / "memory.db"


def _open_t2() -> T2Database:
    return T2Database(_default_db_path())


def _open_t1(session_id: str):
    from nexus.db.t1 import T1Database
    return T1Database(session_id=session_id)


def _infer_repo() -> str:
    """Detect current repo name from git, or fall back to cwd name."""
    try:
        result = subprocess.run(
            ["git", "rev-parse", "--show-toplevel"],
            capture_output=True, text=True, check=True,
        )
        return Path(result.stdout.strip()).name
    except Exception:
        return Path.cwd().name


# ── SessionStart ──────────────────────────────────────────────────────────────

def session_start() -> str:
    """Execute the SessionStart hook.

    1. Generate UUID4 session ID and write to getsid(0)-scoped session file.
    2. Detect PM project via T2 query.
    3. If PM: inject CONTINUATION.md (≤2000 chars).
       Else: print recent memory summary (≤10 entries × 500 chars).

    Returns the output string to be printed.
    """
    session_id = generate_session_id()
    write_session_file(session_id)

    lines: list[str] = [f"Nexus ready. T1 scratch initialized (session: {session_id})."]

    repo = _infer_repo()
    db = _open_t2()

    # PM detection: T2 SQL query for {repo}_pm CONTINUATION.md
    pm_row = db.get(project=f"{repo}_pm", title="CONTINUATION.md")
    if pm_row is not None:
        content = (pm_row.get("content") or "")[:2000]
        lines.append(content)
    else:
        # Non-PM: recent memory summary
        entries = db.list_entries(project=repo)[:10]
        if entries:
            lines.append(f"Recent memory ({repo}, last {len(entries)} entries):")
            for e in entries:
                lines.append(f"  - {e['title']} ({e.get('agent') or '-'}, {e.get('timestamp', '')[:10]})")
        else:
            lines.append(f"No memory entries for '{repo}'.")

    return "\n".join(lines)


# ── SessionEnd ────────────────────────────────────────────────────────────────

def session_end() -> str:
    """Execute the SessionEnd hook.

    1. Flush flagged T1 entries to T2.
    2. Clear T1 session entries.
    3. Run T2 expire.
    4. Remove the session file.

    Returns a summary string.
    """
    session_file = session_file_path()

    # Read session ID so we can open T1
    try:
        session_id = session_file.read_text().strip() or None
    except FileNotFoundError:
        session_id = None

    db = _open_t2()
    flushed = 0

    if session_id:
        t1 = _open_t1(session_id)
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

    # Remove session file
    try:
        session_file.unlink()
    except FileNotFoundError:
        pass

    parts = [f"Session ended. Flushed {flushed} scratch entries. Expired {expired} memory entries."]
    return "\n".join(parts)
