# SPDX-License-Identifier: AGPL-3.0-or-later
import json
import os
import signal
import socket
import subprocess
import time
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


# ── T1 server session management (RDR-010) ────────────────────────────────────

SESSIONS_DIR: Path = Path.home() / ".config" / "nexus" / "sessions"
_T1_SERVER_HOST: str = "127.0.0.1"
_SESSION_MAX_AGE_SECONDS: float = 24 * 3600.0
_SERVER_READY_TIMEOUT: float = 10.0


def _ppid_of(pid: int) -> int | None:
    """Return the parent PID of *pid* via ps, or None if the process is gone."""
    try:
        out = subprocess.check_output(
            ["ps", "-o", "ppid=", "-p", str(pid)],
            text=True,
            stderr=subprocess.DEVNULL,
        ).strip()
        val = int(out)
        return val if val > 1 else None
    except (subprocess.CalledProcessError, ValueError, FileNotFoundError, OSError):
        return None


def find_ancestor_session(
    sessions_dir: Path | None = None,
    start_pid: int | None = None,
) -> dict | None:
    """Walk the PPID chain from *start_pid* looking for a valid JSON session record.

    Returns the parsed record dict on success (keys: session_id, server_host,
    server_port, server_pid, created_at), or None if no valid ancestor session
    is found (ps unavailable, no files, stale, or corrupt).

    Stale records (older than 24 h) are cleaned up automatically during the walk.
    """
    if sessions_dir is None:
        sessions_dir = SESSIONS_DIR

    pid = start_pid if start_pid is not None else os.getpid()
    seen: set[int] = set()
    cutoff = time.time() - _SESSION_MAX_AGE_SECONDS

    while pid and pid not in seen:
        seen.add(pid)
        candidate = sessions_dir / f"{pid}.session"
        if candidate.exists():
            try:
                record = json.loads(candidate.read_text())
                if not isinstance(record, dict):
                    pass  # not a JSON object — skip
                elif record.get("created_at", 0) < cutoff:
                    # Stale orphan: kill server and remove file
                    _try_kill(record.get("server_pid"))
                    _try_remove_path(candidate)
                elif "server_host" in record and "server_port" in record and "session_id" in record:
                    return record
            except (json.JSONDecodeError, OSError):
                pass  # corrupt or unreadable — skip silently
        pid = _ppid_of(pid)

    return None


def sweep_stale_sessions(
    sessions_dir: Path | None = None,
    max_age_hours: float = 24.0,
) -> None:
    """Scan *sessions_dir* for JSON records older than *max_age_hours*.

    For each stale record: sends SIGTERM to server_pid, removes the backing
    tmpdir, and deletes the session file. Non-JSON files are ignored silently.
    """
    if sessions_dir is None:
        sessions_dir = SESSIONS_DIR
    if not sessions_dir.exists():
        return
    cutoff = time.time() - max_age_hours * 3600.0
    for f in sessions_dir.glob("*.session"):
        try:
            record = json.loads(f.read_text())
            if not isinstance(record, dict):
                continue
            if record.get("created_at", time.time()) < cutoff:
                _try_kill(record.get("server_pid"))
                tmpdir = record.get("tmpdir", "")
                if tmpdir:
                    import shutil
                    shutil.rmtree(tmpdir, ignore_errors=True)
                _try_remove_path(f)
        except (json.JSONDecodeError, OSError):
            pass


def start_t1_server() -> tuple[str, int, int, str]:
    """Allocate a free localhost port and launch a ChromaDB HTTP server.

    Returns *(host, port, server_pid, tmpdir)*.

    Raises ``RuntimeError`` if *chroma* is not on PATH or the server does not
    become ready within the timeout.  The caller is responsible for the fallback.
    """
    import shutil as _shutil
    import tempfile

    if not _shutil.which("chroma"):
        raise RuntimeError("chroma not found on PATH; T1 server cannot start")

    # Allocate a free port, then release the socket before handing the port
    # to chroma run (TOCTOU window exists but is negligible on localhost).
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.bind((_T1_SERVER_HOST, 0))
    port: int = sock.getsockname()[1]
    sock.close()

    tmpdir = tempfile.mkdtemp(prefix="nx_t1_")
    proc = subprocess.Popen(
        [
            "chroma", "run",
            "--host", _T1_SERVER_HOST,
            "--port", str(port),
            "--path", tmpdir,
            "--log-level", "ERROR",
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )

    # Poll until the server accepts TCP connections or the process exits.
    deadline = time.time() + _SERVER_READY_TIMEOUT
    while time.time() < deadline:
        if proc.poll() is not None:
            raise RuntimeError(
                f"chroma run exited with code {proc.returncode} before becoming ready"
            )
        try:
            conn = socket.create_connection((_T1_SERVER_HOST, port), timeout=0.5)
            conn.close()
            return _T1_SERVER_HOST, port, proc.pid, tmpdir
        except OSError:
            time.sleep(0.2)

    proc.kill()
    raise RuntimeError(
        f"T1 ChromaDB server on {_T1_SERVER_HOST}:{port} did not become ready "
        f"within {_SERVER_READY_TIMEOUT:.0f}s"
    )


def stop_t1_server(server_pid: int) -> None:
    """Send SIGTERM to *server_pid*; escalate to SIGKILL after 3 seconds."""
    try:
        os.kill(server_pid, signal.SIGTERM)
    except OSError:
        return
    # Wait up to 3 seconds for graceful exit
    deadline = time.time() + 3.0
    while time.time() < deadline:
        try:
            os.kill(server_pid, 0)  # check if alive
        except OSError:
            return  # process gone
        time.sleep(0.1)
    # Escalate
    try:
        os.kill(server_pid, signal.SIGKILL)
    except OSError:
        pass


def write_session_record(
    sessions_dir: Path,
    ppid: int,
    session_id: str,
    host: str,
    port: int,
    server_pid: int,
    tmpdir: str = "",
) -> Path:
    """Write a JSON session record to *sessions_dir*/{ppid}.session (mode 0o600)."""
    sessions_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    path = sessions_dir / f"{ppid}.session"
    record = {
        "session_id": session_id,
        "server_host": host,
        "server_port": port,
        "server_pid": server_pid,
        "created_at": time.time(),
        "tmpdir": tmpdir,
    }
    fd = os.open(str(path), os.O_CREAT | os.O_WRONLY | os.O_TRUNC, 0o600)
    try:
        os.write(fd, json.dumps(record).encode())
    finally:
        os.close(fd)
    return path


# ── Private helpers ───────────────────────────────────────────────────────────

def _try_kill(server_pid: int | None) -> None:
    """Send SIGTERM to *server_pid*, ignoring errors."""
    if not server_pid:
        return
    try:
        os.kill(server_pid, signal.SIGTERM)
    except OSError:
        pass


def _try_remove_path(path: Path) -> None:
    """Remove *path*, ignoring errors."""
    try:
        path.unlink(missing_ok=True)
    except OSError:
        pass
