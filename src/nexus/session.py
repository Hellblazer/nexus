# SPDX-License-Identifier: AGPL-3.0-or-later
import json
import os
import signal
import socket
import subprocess
import tempfile
import time
from pathlib import Path
from typing import Any
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
    the module attribute (``nexus.session.CLAUDE_SESSION_FILE``).
    """
    import os as _os

    override = _os.environ.get("NEXUS_CONFIG_DIR", "").strip()
    if override:
        return Path(override)
    return Path.home() / ".config" / "nexus"


#: Import-time snapshot kept for backward-compatibility with callers
#: that import the constant directly. New code uses
#: :func:`claude_session_file` (re-resolved per call so tests and
#: subprocesses honour mid-process ``NEXUS_CONFIG_DIR`` flips). The
#: read/write helpers below resolve the path per call so the constant
#: is no longer load-bearing.
CLAUDE_SESSION_FILE = _nexus_config_dir_at_import() / "current_session"


def claude_session_file() -> Path:
    """Return the path to ``current_session`` honouring the live
    ``NEXUS_CONFIG_DIR`` env. Re-resolved per call so a subprocess
    that inherits a different ``NEXUS_CONFIG_DIR`` (test isolation,
    sandbox, sub-agent dispatch) sees its own config dir without
    re-importing the module.
    """
    return _nexus_config_dir_at_import() / "current_session"


def generate_session_id() -> str:
    """Return a new UUID4 session ID string."""
    return str(uuid4())


def write_claude_session_id(session_id: str) -> None:
    """Write the Claude session ID to the stable flat file (mode 0o600)."""
    path = claude_session_file()
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd = os.open(str(path), os.O_CREAT | os.O_WRONLY | os.O_TRUNC, 0o600)
    try:
        os.write(fd, session_id.encode())
    finally:
        os.close(fd)


def read_claude_session_id() -> str | None:
    """Read the Claude session ID from the flat file, or None if not set."""
    try:
        text = claude_session_file().read_text().strip()
        return text or None
    except OSError:
        return None  # intentional: file not created yet, normal on first run


def resolve_active_session_id(arg: str | None = None) -> str | None:
    """Single source of truth for the active Claude session_id.

    Resolution chain (highest priority first):

    1. Explicit ``arg`` (caller-supplied; non-empty after strip).
    2. ``NX_SESSION_ID`` env var (non-empty after strip).
    3. ``~/.config/nexus/current_session`` via ``read_claude_session_id``.
    4. ``None``.

    Returns ``None`` when nothing in the chain resolves. Callers choose
    their own fallback at the call site:

    * ``T1Database._resolve_session_id`` and ``mcp/core._record_tier_write``
      substitute ``"unknown"`` so the per-entry / per-row session_id is
      never empty and the audit log and the T1 chunk store agree on
      attribution. Pre-PR T1 fell back to ``uuid4()`` while tier-write
      fell back to ``"unknown"`` -- the divergence that produced the
      nexus-h8ge bug class even after PR #590 lifted the chain into
      ``T1Database._resolve_session_id``: each open-coded copy could
      drift independently.
    * ``_session_end_launcher._print_tier_status_summary`` short-circuits
      on ``None`` (no useful per-session summary without a bound session
      -- querying ``WHERE session_id = "unknown"`` would leak rows from
      unrelated invocations into the user-facing summary).

    Issue #594 / nexus-9e9a: this helper is the structural fix for the
    three-site drift class. Any future change to the chain happens here
    once.
    """
    if arg:
        stripped = arg.strip()
        if stripped:
            return stripped
    env = os.environ.get("NX_SESSION_ID", "").strip()
    if env:
        return env
    file_id = read_claude_session_id()
    if file_id:
        return file_id
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

_T1_SERVER_HOST: str = "127.0.0.1"
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





def _is_pid_alive(pid: int) -> bool:
    """Return True if *pid* names a running process (liveness probe).

    Uses ``os.kill(pid, 0)`` — raises ``ProcessLookupError`` when the
    process is gone, ``PermissionError`` when it exists but is owned
    by a different uid (treated as alive). Invalid pids (<=0) are
    treated as dead.
    """
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True





def sweep_orphan_tmpdirs(
    tmpdir_root: Path | None = None,
    max_age_hours: float = 24.0,
) -> int:
    """Reap orphan ``nx_t1_*`` tmpdirs older than *max_age_hours*.

    Scans *tmpdir_root* (defaults to the system tempdir) for
    directories matching ``nx_t1_*`` (the prefix used by
    :func:`start_t1_server`). Reaps any whose mtime is older than
    the cutoff (default 24h) so legitimate in-flight tmpdirs (active
    chroma spawn between :func:`tempfile.mkdtemp` and the chroma
    process becoming live) are not accidentally removed.

    Returns the count of directories reaped. Best-effort cleanup
    that runs at top-level MCP startup; failures are non-fatal.

    Pre-RDR-105-P4 the sweep also took a ``sessions_dir`` parameter
    and skipped any tmpdir referenced by a live ``<uuid>.session``
    record. The session-record machinery is gone; mtime is the sole
    protection gate. Any ``nx_t1_*`` tmpdir older than 24h with no
    live owner is treated as an orphan; tests or operators that
    need to keep an old tmpdir around must touch it (refresh
    mtime) on a sub-24h cadence or move it outside the
    ``nx_t1_*`` namespace.
    """
    import shutil

    if tmpdir_root is None:
        tmpdir_root = Path(tempfile.gettempdir())
    if not tmpdir_root.exists():
        return 0

    cutoff = time.time() - max_age_hours * 3600.0
    reaped = 0
    for d in tmpdir_root.glob("nx_t1_*"):
        if not d.is_dir():
            continue
        try:
            mtime = d.stat().st_mtime
        except OSError:
            continue
        if mtime >= cutoff:
            continue
        shutil.rmtree(d, ignore_errors=True)
        if not d.exists():
            reaped += 1
            _log.info(
                "sweep_reaped_orphan_tmpdir",
                path=str(d),
                age_hours=round((time.time() - mtime) / 3600.0, 2),
            )
    return reaped


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
            # NOTE: An ``atexit.register(stop_t1_server, proc.pid)`` was added
            # in PR #220 as a "defence-in-depth fallback" for the case where
            # the SessionEnd hook doesn't fire. It was wrong: this function
            # runs inside the short-lived ``nx hook session-start`` process,
            # which exits *immediately* after spawning chroma. atexit then
            # killed the chroma server right after spawn — production T1
            # silently fell back to EphemeralClient on every conversation
            # since 4.9.1. The chroma server is meant to outlive this
            # process; cleanup is the SessionEnd hook's job. Ungraceful
            # exits (Claude Code SIGKILL/OOM) leak the server until the
            # next top-level MCP startup, which runs
            # ``sweep_orphan_t1_addr_files`` + ``sweep_orphan_tmpdirs``
            # to reap any leftovers.
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





def _command_name_of(pid: int) -> str:
    """Return the command name (argv[0] basename) of *pid*, or "" if unknown.

    Used by :func:`find_immediate_claude_pid` to identify which ancestor
    is Claude Code. Falls back to an empty string on any error; the
    caller treats that as "not a match" and keeps walking.
    """
    try:
        out = subprocess.check_output(
            ["ps", "-o", "comm=", "-p", str(pid)],
            stderr=subprocess.DEVNULL, text=True, timeout=2,
        ).strip()
        return Path(out).name if out else ""
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired,
            OSError):
        return ""





# ── RDR-105 hybrid-discovery primitives ──────────────────────────────────────
#
# The single T1 discovery surface as of P4 (nexus-jnx7). The legacy
# session-record machinery (multi-writer ``<uuid>.session`` JSON files,
# the topmost-walk ``find_claude_root_pid``, the watchdog sidecar, the
# reconcile probe) was deleted along with the bug class it produced.


def find_immediate_claude_pid(start_pid: int | None = None) -> int:
    """Return the FIRST ``claude*`` ancestor walking up from *start_pid*.

    RDR-105 RF-6 (CRITICAL): topmost-walk silently breaks owned-mode
    isolation. An owned ``claude -p`` subprocess MCP's process tree
    contains two ``claude*`` ancestors (the immediate parent
    ``claude -p`` and the user's top-level Claude). Topmost-walk
    returns the user's Claude → the owned MCP would (a) write its
    addr file at the parent's claude_pid, clobbering the parent's
    file, and (b) read its own discovery from the parent's file,
    silently sharing instead of isolating.

    Returning the FIRST match keys the addr file at the immediate
    Claude ancestor, sealing the owned subprocess from the parent.
    Verified across all four nesting cases per RF-6.

    Falls back to the immediate PPID when no ``claude*`` ancestor is
    found (matches the no-claude-in-chain semantics of the legacy
    function so consumers behave identically in that case). Returns
    0 only when the PPID chain cannot be walked at all.
    """
    pid = start_pid if start_pid is not None else os.getpid()
    seen: set[int] = set()
    cur = _ppid_of(pid)
    immediate_ppid = cur or 0
    while cur and cur not in seen and cur > 1:
        seen.add(cur)
        if _command_name_of(cur).lower().startswith("claude"):
            return cur
        cur = _ppid_of(cur)
    return immediate_ppid


_NEXUS_SKIP_T1_DEPRECATION_WARNED: bool = False





def _t1_isolated_env() -> bool:
    """Return True when the current env opts into per-process T1 ephemeral.

    Honours the new ``NX_T1_ISOLATED=1`` name; for the 4.27 -> 4.28
    deprecation cycle (RF-4) the legacy ``NEXUS_SKIP_T1=1`` is also
    accepted with a one-shot warning when the new name is absent.
    Removed in 5.0. The one-shot guard
    ``_NEXUS_SKIP_T1_DEPRECATION_WARNED`` keeps long-running MCP
    workflows from spamming the log on every ``T1Database``
    construction.
    """
    global _NEXUS_SKIP_T1_DEPRECATION_WARNED
    isolated = os.environ.get("NX_T1_ISOLATED", "").strip().lower() in ("1", "true", "yes")
    legacy = os.environ.get("NEXUS_SKIP_T1", "").strip().lower() in ("1", "true", "yes")
    if legacy and not isolated and not _NEXUS_SKIP_T1_DEPRECATION_WARNED:
        _NEXUS_SKIP_T1_DEPRECATION_WARNED = True
        _log.warning(
            "nexus_skip_t1_deprecated",
            message="NEXUS_SKIP_T1 is deprecated; use NX_T1_ISOLATED=1 instead. Will be removed in 5.0.",
        )
    return isolated or legacy


def t1_addr_path(claude_pid: int) -> Path:
    """Return the path to the address file for *claude_pid*.

    Resolved against ``NEXUS_CONFIG_DIR`` at call time so tests that
    set the env var see the redirect without monkeypatching module
    constants. Default location: ``~/.config/nexus/t1_addr.<pid>``.
    """
    return _nexus_config_dir_at_import() / f"t1_addr.{claude_pid}"


def write_t1_addr(claude_pid: int, host: str, port: int) -> Path:
    """Atomically write the address file for *claude_pid*.

    Single-writer contract: only the top-level (or owned subprocess)
    MCP at lifespan start writes this file. The atomic rename keeps a
    concurrent reader either on the prior contents or the new
    contents, never torn.
    """
    target = t1_addr_path(claude_pid)
    target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    tmp = target.with_suffix(target.suffix + f".{os.getpid()}.tmp")
    fd = os.open(str(tmp), os.O_CREAT | os.O_WRONLY | os.O_TRUNC, 0o600)
    try:
        try:
            os.write(fd, f"{host}:{port}\n".encode())
        finally:
            os.close(fd)
        tmp.replace(target)
    except BaseException:
        # Disk-full / permissions / interrupt: don't leave the tmp
        # file behind for the next sweep to clean up.
        try:
            tmp.unlink()
        except FileNotFoundError:
            pass
        raise
    return target


def read_t1_addr_for(claude_pid: int) -> tuple[str, int] | None:
    """Read the addr file for *claude_pid*. Returns ``(host, port)`` or None.

    None on missing file, malformed contents, or unreadable file;
    callers fail loud at the next layer (``T1Database`` constructor's
    raise) rather than constructing an EphemeralClient.
    """
    path = t1_addr_path(claude_pid)
    try:
        text = path.read_text().strip()
    except OSError:
        return None  # intentional: missing/unreadable, callers handle
    if ":" not in text:
        return None
    host, _, port_str = text.partition(":")
    try:
        return host, int(port_str)
    except ValueError:
        return None


def unlink_t1_addr(claude_pid: int) -> None:
    """Best-effort delete of the addr file. No-op if already gone."""
    path = t1_addr_path(claude_pid)
    try:
        path.unlink()
    except FileNotFoundError:
        return  # intentional: idempotent
    except OSError as exc:
        _log.debug("t1_addr_unlink_failed", path=str(path), error=str(exc))


def sweep_orphan_t1_addr_files() -> int:
    """Reap ``t1_addr.<claude_pid>`` files whose ``<claude_pid>`` is dead.

    Best-effort orphan cleanup runs at top-level MCP startup. A
    Claude Code session that exits ungracefully (SIGKILL, OOM, hard
    crash) leaves its addr file behind; the lifespan finally never
    runs. The next MCP boot's sweep reaps any stale files so a
    sibling subprocess does not connect to a dead chroma.

    Returns the count of files reaped. Failures are logged but
    never propagate; this is not load-bearing.

    PID reuse: if a live unrelated process happens to have the same
    PID as the dead Claude (PIDs wrap on Linux), the sweep skips the
    file (false-negative). Worst outcome: the file lingers until the
    next sweep, at which point either the PID is still reused (still
    skipped, still no harm) or it has exited (now reaped). No
    incorrect destructive action is possible. A ``comm`` cross-check
    would close the false-negative but adds two subprocess calls per
    file with portability concerns; not justified for a best-effort
    path.
    """
    config_dir = _nexus_config_dir_at_import()
    if not config_dir.exists():
        return 0
    reaped = 0
    for path in config_dir.glob("t1_addr.*"):
        suffix = path.suffix.lstrip(".")
        try:
            claude_pid = int(suffix)
        except ValueError:
            continue
        if claude_pid > 0 and _is_pid_alive(claude_pid):
            continue
        try:
            path.unlink()
            reaped += 1
            _log.info("sweep_reaped_orphan_t1_addr", path=str(path), pid=claude_pid)
        except FileNotFoundError:
            continue
        except OSError as exc:
            _log.debug("sweep_t1_addr_unlink_failed", path=str(path), error=str(exc))
    return reaped








# ── Private helpers ───────────────────────────────────────────────────────────

def _try_remove_path(path: Path) -> None:
    """Remove *path*, ignoring errors."""
    try:
        path.unlink(missing_ok=True)
    except OSError:
        pass  # intentional: best-effort file cleanup
