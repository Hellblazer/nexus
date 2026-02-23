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
    if raw := os.environ.get("NX_SESSION_PID"):
        try:
            return int(raw)
        except ValueError:
            pass  # fall through to getsid
    # os.getsid is POSIX-only; raises AttributeError on Windows.
    # Acceptable since Nexus targets macOS/Linux (Claude Code environments).
    return os.getsid(0)


def session_file_path(ppid: int | None = None) -> Path:
    """Return the session file path keyed by *ppid* (or the stable anchor)."""
    pid = ppid if ppid is not None else _stable_pid()
    return Path.home() / ".config" / "nexus" / "sessions" / f"{pid}.session"


def write_session_file(session_id: str, ppid: int | None = None) -> Path:
    """Write *session_id* to the session file. Returns the path.

    Uses ``os.open()`` with mode 0o600 from the first byte to avoid a
    TOCTOU race between ``write_text()`` and ``chmod()``.
    """
    path = session_file_path(ppid)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd = os.open(str(path), os.O_CREAT | os.O_WRONLY | os.O_TRUNC, 0o600)
    try:
        os.write(fd, session_id.encode())
    finally:
        os.close(fd)
    return path


def read_session_id(ppid: int | None = None) -> str | None:
    """Read and return the session ID from the session file, or None."""
    try:
        text = session_file_path(ppid).read_text().strip()
        return text or None
    except OSError:
        return None
