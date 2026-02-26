# SPDX-License-Identifier: AGPL-3.0-or-later
import os
from pathlib import Path
from uuid import uuid4

# Flat file written by SessionStart hook with the Claude session ID.
# Shared by all Bash subprocesses within one Claude Code conversation.
# os.getsid(0) is NOT used: Claude Code spawns each Bash(...) call in its own
# process session, making getsid different per invocation.
CLAUDE_SESSION_FILE = Path.home() / ".config" / "nexus" / "current_session"


def generate_session_id() -> str:
    """Return a new UUID4 session ID string."""
    return str(uuid4())


def write_claude_session_id(session_id: str) -> None:
    """Write the Claude session ID to the stable flat file (mode 0o600)."""
    CLAUDE_SESSION_FILE.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd = os.open(str(CLAUDE_SESSION_FILE), os.O_CREAT | os.O_WRONLY | os.O_TRUNC, 0o600)
    try:
        os.write(fd, session_id.encode())
    finally:
        os.close(fd)


def read_claude_session_id() -> str | None:
    """Read the Claude session ID from the flat file, or None if not set."""
    try:
        text = CLAUDE_SESSION_FILE.read_text().strip()
        return text or None
    except OSError:
        return None


def _stable_pid() -> int:
    """Return a stable process-group anchor for legacy session file naming.

    Lookup order:
    1. ``NX_SESSION_PID`` env var — allows callers to pin a specific PID.
    2. Process session leader (``os.getsid(0)``).

    Note: os.getsid(0) is unreliable across Claude Code Bash subprocesses.
    New code should use read_claude_session_id() / write_claude_session_id()
    instead of session_file_path() / write_session_file() / read_session_id().
    """
    if raw := os.environ.get("NX_SESSION_PID"):
        try:
            return int(raw)
        except ValueError:
            pass
    return os.getsid(0)


def session_file_path(ppid: int | None = None) -> Path:
    """Return the legacy getsid-keyed session file path."""
    pid = ppid if ppid is not None else _stable_pid()
    return Path.home() / ".config" / "nexus" / "sessions" / f"{pid}.session"


def write_session_file(session_id: str, ppid: int | None = None) -> Path:
    """Write *session_id* to the legacy getsid-keyed session file."""
    path = session_file_path(ppid)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd = os.open(str(path), os.O_CREAT | os.O_WRONLY | os.O_TRUNC, 0o600)
    try:
        os.write(fd, session_id.encode())
    finally:
        os.close(fd)
    return path


def read_session_id(ppid: int | None = None) -> str | None:
    """Read and return the session ID from the legacy getsid-keyed file, or None."""
    try:
        text = session_file_path(ppid).read_text().strip()
        return text or None
    except OSError:
        return None
