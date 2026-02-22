# SPDX-License-Identifier: AGPL-3.0-or-later
import os
from pathlib import Path
from uuid import uuid4


def generate_session_id() -> str:
    """Return a new UUID4 session ID string."""
    return str(uuid4())


def _stable_pid() -> int:
    """Return a stable process-group anchor for session file naming.

    Lookup order:
    1. ``NX_SESSION_PID`` env var — allows callers to pin a specific PID.
    2. Process session leader (``os.getsid(0)``) — stable across all commands
       in the same interactive terminal session, whether invoked from the
       SessionStart hook or directly from the CLI.
    """
    if env_pid := os.environ.get("NX_SESSION_PID"):
        return int(env_pid)
    return os.getsid(0)


def session_file_path(ppid: int | None = None) -> Path:
    """Return the session file path keyed by *ppid* (or the stable anchor)."""
    pid = ppid if ppid is not None else _stable_pid()
    return Path.home() / ".config" / "nexus" / "sessions" / f"{pid}.session"


def write_session_file(session_id: str, ppid: int | None = None) -> Path:
    """Write *session_id* to the session file. Returns the path."""
    path = session_file_path(ppid)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(session_id)
    return path


def read_session_id(ppid: int | None = None) -> str | None:
    """Read and return the session ID from the session file, or None."""
    try:
        text = session_file_path(ppid).read_text().strip()
        return text or None
    except FileNotFoundError:
        return None
