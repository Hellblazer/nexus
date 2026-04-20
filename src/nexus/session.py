# SPDX-License-Identifier: AGPL-3.0-or-later
import atexit
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
def _nexus_config_dir_at_import() -> Path:
    """Resolve the Nexus config dir honouring ``NEXUS_CONFIG_DIR`` at import time.

    ``session.py`` holds module-level path constants that must be redirectable
    under sandbox / test isolation. Callers setting ``NEXUS_CONFIG_DIR`` in
    the shell before invoking ``nx`` see the constants resolved against the
    sandbox. Tests that need to flip the dir mid-process still monkeypatch
    the module attribute (``nexus.session.SESSIONS_DIR`` /
    ``nexus.session.CLAUDE_SESSION_FILE``) — both access paths work.
    """
    import os as _os

    override = _os.environ.get("NEXUS_CONFIG_DIR", "").strip()
    if override:
        return Path(override)
    return Path.home() / ".config" / "nexus"


CLAUDE_SESSION_FILE = _nexus_config_dir_at_import() / "current_session"


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
    return _nexus_config_dir_at_import() / "sessions" / f"{pid}.session"


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

SESSIONS_DIR: Path = _nexus_config_dir_at_import() / "sessions"
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
        # Migration: numeric-stem files come from the legacy PID-keyed scheme
        # that bound T1 to the terminal session instead of the Claude
        # conversation. They were never doing the right thing — sweep
        # unconditionally on first new-code SessionStart, regardless of age.
        # The chroma servers they pointed at were leaked aliases; reap them
        # too. New code only writes UUID-keyed files.
        if f.stem.isdigit():
            try:
                legacy = json.loads(f.read_text())
                if isinstance(legacy, dict):
                    server_pid = legacy.get("server_pid")
                    if server_pid:
                        stop_t1_server(server_pid)
                    tmpdir = legacy.get("tmpdir", "")
                    if tmpdir:
                        import shutil
                        shutil.rmtree(tmpdir, ignore_errors=True)
            except (json.JSONDecodeError, OSError):
                pass  # intentional: best-effort migration; the file gets removed regardless
            _try_remove_path(f)
            continue
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
    # start_new_session=True isolates chroma + its multiprocessing workers
    # into their own process group so ``safe_killpg(pid, …)`` (defined in
    # ``nexus.util.process_group``) reaches the whole subtree at shutdown.
    # Without this, SIGTERM only hits the chroma head; workers get
    # orphaned and their POSIX named semaphores are never
    # ``sem_unlink()``-ed → Errno 28 namespace exhaustion (beads
    # nexus-dc57 / nexus-ze2a root cause).
    proc = subprocess.Popen(
        [
            chroma, "run",
            "--host", _T1_SERVER_HOST,
            "--port", str(port),
            "--path", tmpdir,
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
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
            # Defence-in-depth: atexit reaps chroma on graceful interpreter
            # exit even when the SessionEnd hook never fires (harness
            # teardown cancels the hook, OOM, terminal SIGHUP that doesn't
            # propagate because of start_new_session=True). Idempotent —
            # stop_t1_server tolerates an already-dead PID.
            atexit.register(stop_t1_server, proc.pid)
            return _T1_SERVER_HOST, port, proc.pid, tmpdir
        except OSError:
            time.sleep(0.2)  # intentional: server not yet listening, retry loop

    proc.kill()
    raise RuntimeError(
        f"T1 ChromaDB server on {_T1_SERVER_HOST}:{port} did not become ready "
        f"within {_SERVER_READY_TIMEOUT:.0f}s"
    )


def stop_t1_server(server_pid: int) -> None:
    """Send SIGTERM → SIGKILL to the *entire process group* owned by
    *server_pid*.

    Signals the process group via ``safe_killpg`` (pgid of *server_pid*)
    rather than ``os.kill(pid, …)`` so chroma's multiprocessing workers
    and their ``resource_tracker`` children receive the signal too. The
    tracker unlinks POSIX named semaphores during its own shutdown;
    without it, workers' semaphores stay registered with the kernel
    until reboot and eventually exhaust ``kern.posix.sem.max`` (beads
    nexus-dc57 + nexus-ze2a).

    Graceful SIGTERM first; escalates to SIGKILL after 3 s. Both signal
    calls go through ``nexus.util.process_group.safe_killpg`` so the
    mock-guard + error-swallow contract stays consistent with every
    other subprocess cleanup site.
    """
    from nexus.util.process_group import safe_killpg

    if not safe_killpg(server_pid, signal.SIGTERM):
        return  # process already gone or unreachable before any signal

    # Wait up to 3 seconds for graceful exit.
    deadline = time.time() + 3.0
    while time.time() < deadline:
        try:
            os.kill(server_pid, 0)  # readiness probe — NOT a signal delivery
        except OSError:
            return  # process exited after SIGTERM — success
        time.sleep(0.1)

    # Escalate.
    safe_killpg(server_pid, signal.SIGKILL)

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
) -> Path:
    """Write a JSON session record to *sessions_dir*/{ppid}.session (mode 0o600).

    .. deprecated::
        Legacy PID-keyed write. Use :func:`write_session_record_by_id` instead.
        Kept as an alias for one release; no production code path calls this.
    """
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


# ── UUID-keyed session records (the current scheme) ──────────────────────────
#
# T1 must be scoped to a Claude conversation, not to a terminal session. The
# previous PID-keyed scheme walked the PPID chain to "find the ancestor's
# session file" — which on systems where Claude Code is invoked directly from
# a shell lands on the login shell's PID. Two ``claude`` invocations in the
# same shell then shared one T1 server; the same conversation accessed from a
# different shell could not find it. The UUID-keyed scheme fixes both: the
# Claude session UUID arrives via the SessionStart hook payload, and child
# processes inherit it through ``NX_SESSION_ID`` (race-free) with the legacy
# ``current_session`` flat file as a fallback for tools launched outside the
# Claude process tree.

_NX_SESSION_ID_ENV = "NX_SESSION_ID"


def write_session_record_by_id(
    sessions_dir: Path,
    session_id: str,
    host: str,
    port: int,
    server_pid: int,
    tmpdir: str = "",
) -> Path:
    """Write a JSON session record at *sessions_dir*/{session_id}.session.

    The UUID-keyed counterpart of :func:`write_session_record`. Always use
    this in new code so T1 is scoped to a Claude conversation rather than
    a terminal session.
    """
    sessions_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    path = sessions_dir / f"{session_id}.session"
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


def find_session_by_id(
    sessions_dir: Path | None = None,
    session_id: str | None = None,
) -> dict | None:
    """Look up the T1 session record for *session_id* in *sessions_dir*.

    When *session_id* is None, resolves it in this order:

    1. ``NX_SESSION_ID`` environment variable (set by the SessionStart hook
       so direct child processes inherit it without a race).
    2. ``current_session`` flat file (fallback for tools launched outside
       the Claude process tree, e.g. an MCP server reconnecting after the
       hook has already exited).

    Returns the parsed record dict (keys: session_id, server_host,
    server_port, server_pid, tmpdir, created_at) or None if no record
    exists for that ID, or if no ID could be resolved at all.

    Stale records (older than 24 h) are reaped on the way out — same
    policy as the older PPID-walking variant.
    """
    if sessions_dir is None:
        sessions_dir = SESSIONS_DIR
    if session_id is None:
        session_id = os.environ.get(_NX_SESSION_ID_ENV) or read_claude_session_id()
    if not session_id:
        return None
    candidate = sessions_dir / f"{session_id}.session"
    if not candidate.exists():
        return None
    try:
        record = json.loads(candidate.read_text())
    except (json.JSONDecodeError, OSError) as exc:
        _log.debug(
            "find_session_by_id: corrupt or unreadable session file",
            path=str(candidate),
            error=str(exc),
        )
        return None
    if not isinstance(record, dict):
        return None
    cutoff = time.time() - _SESSION_MAX_AGE_SECONDS
    if record.get("created_at", 0) < cutoff:
        # Stale: reap server + remove file, return None so caller falls
        # back to a fresh start.
        server_pid = record.get("server_pid")
        if server_pid:
            stop_t1_server(server_pid)
        _try_remove_path(candidate)
        return None
    if (
        "server_host" not in record
        or "server_port" not in record
        or "session_id" not in record
    ):
        return None
    return record


# ── Private helpers ───────────────────────────────────────────────────────────

def _try_remove_path(path: Path) -> None:
    """Remove *path*, ignoring errors."""
    try:
        path.unlink(missing_ok=True)
    except OSError:
        pass  # intentional: best-effort file cleanup
