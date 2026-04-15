# SPDX-License-Identifier: AGPL-3.0-or-later
import json
import os
import signal
import socket
import subprocess
import time
from pathlib import Path
from uuid import uuid4

import structlog

_log = structlog.get_logger()

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
        return None  # intentional: file not created yet, normal on first run


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
            pass  # intentional: invalid NX_SESSION_PID env var, fall through to os.getsid(0)
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
        return None  # intentional: session file not created yet, normal on first run


# ── T1 server session management (RDR-010) ────────────────────────────────────

SESSIONS_DIR: Path = Path.home() / ".config" / "nexus" / "sessions"
_T1_SERVER_HOST: str = "127.0.0.1"
_SESSION_MAX_AGE_SECONDS: float = 24 * 3600.0
_SERVER_READY_TIMEOUT: float = 10.0


def _ppid_of(pid: int) -> int | None:
    """Return the parent PID of *pid*, or None if the process is gone.

    Tries ``/proc/{pid}/status`` first (Linux; works in minimal containers
    without ``ps``), then falls back to ``ps`` (macOS + Linux with procps).
    """
    # Linux: /proc is more reliable than ps in containers (Alpine, distroless).
    status_path = Path(f"/proc/{pid}/status")
    if status_path.exists():
        try:
            for line in status_path.read_text().splitlines():
                if line.startswith("PPid:"):
                    val = int(line.split()[1])
                    return val if val > 1 else None
        except (OSError, ValueError) as exc:
            _log.debug("ppid_proc_read_failed", pid=pid, error=str(exc))

    # Fallback: ps (macOS + Linux with procps)
    try:
        out = subprocess.check_output(
            ["ps", "-o", "ppid=", "-p", str(pid)],
            text=True,
            stderr=subprocess.DEVNULL,
        ).strip()
        val = int(out)
        return val if val > 1 else None
    except (subprocess.CalledProcessError, ValueError, FileNotFoundError, OSError):
        return None  # intentional: process gone or ps unavailable — expected during PPID walk


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
                if isinstance(record, dict):
                    if record.get("created_at", 0) < cutoff:
                        # Stale orphan: stop server (SIGTERM → SIGKILL) and remove file.
                        server_pid = record.get("server_pid")
                        if server_pid:
                            stop_t1_server(server_pid)
                        _try_remove_path(candidate)
                    elif "server_host" in record and "server_port" in record and "session_id" in record:
                        return record
            except (json.JSONDecodeError, OSError):
                _log.debug("find_ancestor_session: skipping corrupt/unreadable session file", path=str(candidate))
        pid = _ppid_of(pid)

    return None


def sweep_stale_sessions(
    sessions_dir: Path | None = None,
    max_age_hours: float = _SESSION_MAX_AGE_SECONDS / 3600.0,
) -> None:
    """Scan *sessions_dir* for JSON records older than *max_age_hours*.

    For each stale record: sends SIGTERM → SIGKILL to server_pid, removes the
    backing tmpdir, and deletes the session file. Non-JSON files are ignored.
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
                server_pid = record.get("server_pid")
                if server_pid:
                    stop_t1_server(server_pid)
                tmpdir = record.get("tmpdir", "")
                if tmpdir:
                    import shutil
                    shutil.rmtree(tmpdir, ignore_errors=True)
                _try_remove_path(f)
        except (json.JSONDecodeError, OSError) as exc:
            _log.debug("sweep_corrupt_session_file", path=str(f), error=str(exc))


def _find_chroma() -> str | None:
    """Return the path to the chroma CLI co-installed with this interpreter.

    Since chromadb is a hard dependency of nexus, the chroma entry-point
    script is always present in the same bin directory as the nx tool and
    Python interpreter.  Checking there first avoids requiring the user to
    manually add it to PATH.  Falls back to a PATH search for unusual installs
    (system Python, path-only setup, etc.).
    """
    import sys as _sys
    candidate = Path(_sys.executable).parent / "chroma"
    if candidate.is_file():
        return str(candidate)
    import shutil as _shutil
    return _shutil.which("chroma")


def start_t1_server() -> tuple[str, int, int, str]:
    """Allocate a free localhost port and launch a ChromaDB HTTP server.

    Returns *(host, port, server_pid, tmpdir)*.

    Raises ``RuntimeError`` if the chroma entry-point cannot be located or
    the server does not become ready within the timeout.  The caller is
    responsible for the fallback.
    """
    import tempfile

    chroma = _find_chroma()
    if not chroma:
        raise RuntimeError(
            "chroma entry-point not found; reinstall nexus to restore it"
        )

    # Allocate a free port, then release the socket before handing the port to
    # chroma run.  There is an inherent TOCTOU race between releasing the port
    # and chroma binding it.  Known limitation; no retry logic is implemented
    # since the window is negligible on loopback.
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.bind((_T1_SERVER_HOST, 0))
    port: int = sock.getsockname()[1]
    sock.close()

    tmpdir = tempfile.mkdtemp(prefix="nx_t1_")
    proc = subprocess.Popen(
        [
            chroma, "run",
            "--host", _T1_SERVER_HOST,
            "--port", str(port),
            "--path", tmpdir,
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
            time.sleep(0.2)  # intentional: server not yet listening, retry loop

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
        return  # intentional: process already gone before SIGTERM
    # Wait up to 3 seconds for graceful exit
    deadline = time.time() + 3.0
    while time.time() < deadline:
        try:
            os.kill(server_pid, 0)  # check if alive
        except OSError:
            return  # intentional: process exited after SIGTERM — success
        time.sleep(0.1)
    # Escalate
    try:
        os.kill(server_pid, signal.SIGKILL)
    except OSError:
        return  # intentional: process exited between poll and SIGKILL
    # Reap the zombie so it does not linger in the process table.
    try:
        os.waitpid(server_pid, os.WNOHANG)
    except ChildProcessError:
        pass  # not our child (different parent process — acceptable)
    except OSError:
        pass  # intentional: process already gone after SIGKILL


def write_session_record(
    sessions_dir: Path,
    ppid: int,
    session_id: str,
    host: str,
    port: int,
    server_pid: int,
    tmpdir: str = "",
    *,
    pool_session: bool = False,
    pool_pid: int | None = None,
) -> Path:
    """Write a JSON session record to *sessions_dir*/{name}.session (mode 0o600).

    ``name`` is ``{ppid}`` for user sessions (the RDR-078 default) or
    ``{session_id}`` for pool sessions (RDR-079 P2.5). Pool sessions
    additionally persist ``pool_pid`` (for P2.2 liveness reconciliation via
    ``os.kill(pid, 0)``) and the marker ``pool_session: true``. User
    sessions omit both fields entirely — backward compatible with any
    existing consumer that reads the JSON.
    """
    if pool_session and pool_pid is None:
        raise ValueError(
            "pool_pid is required when pool_session=True — "
            "reconciliation cannot probe liveness without a PID"
        )
    sessions_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    # Pool sessions are named by their UUID so the session file is
    # discoverable by session_id; user sessions stay PPID-named so the
    # existing PPID-walk discovery path is unchanged.
    filename = f"{session_id}.session" if pool_session else f"{ppid}.session"
    path = sessions_dir / filename
    record: dict[str, object] = {
        "session_id": session_id,
        "server_host": host,
        "server_port": port,
        "server_pid": server_pid,
        "created_at": time.time(),
        "tmpdir": tmpdir,
    }
    if pool_session:
        record["pool_session"] = True
        record["pool_pid"] = pool_pid
    fd = os.open(str(path), os.O_CREAT | os.O_WRONLY | os.O_TRUNC, 0o600)
    try:
        os.write(fd, json.dumps(record).encode())
    finally:
        os.close(fd)
    return path


# ── Private helpers ───────────────────────────────────────────────────────────

def _try_remove_path(path: Path) -> None:
    """Remove *path*, ignoring errors."""
    try:
        path.unlink(missing_ok=True)
    except OSError:
        pass  # intentional: best-effort file cleanup
