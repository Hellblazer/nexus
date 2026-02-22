# SPDX-License-Identifier: AGPL-3.0-or-later
import os
from pathlib import Path
from uuid import uuid4


def generate_session_id() -> str:
    """Return a new UUID4 session ID string."""
    return str(uuid4())


def session_file_path(ppid: int | None = None) -> Path:
    """Return the PID-scoped session file path.

    The hook writes this file using its own ``os.getppid()`` (the Claude Code
    process PID). ``nx`` subcommands read it using their own ``os.getppid()``
    (the shell process PID). For direct invocation from Claude Code both values
    are the same.
    """
    pid = ppid if ppid is not None else os.getppid()
    return Path.home() / ".config" / "nexus" / "sessions" / f"{pid}.session"


def write_session_file(session_id: str, ppid: int | None = None) -> Path:
    """Write *session_id* to the PID-scoped session file. Returns the path."""
    path = session_file_path(ppid)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(session_id)
    return path


def read_session_id(ppid: int | None = None) -> str | None:
    """Read and return the session ID from the PID-scoped file, or None."""
    try:
        text = session_file_path(ppid).read_text().strip()
        return text or None
    except FileNotFoundError:
        return None
