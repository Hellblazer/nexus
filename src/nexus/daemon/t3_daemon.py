# SPDX-License-Identifier: AGPL-3.0-or-later
"""RDR-120 P1.A (nexus-41unl) — T3 daemon lifecycle.

The T3 "daemon" is a managed ``chroma run`` subprocess. chromadb's
bundled HTTP server is the RPC layer; this module owns process
lifecycle and the on-disk discovery file at
``~/.config/nexus/t3_addr.<uid>``.

Local-mode only. Cloud mode (NX_LOCAL=0) raises ``T3CloudModeError`` —
chromadb's CloudClient is already HTTP-served, so there is no daemon
to run. Clients in cloud mode connect directly via CloudClient.

The chroma subprocess is spawned with ``start_new_session=True`` so a
SIGTERM at shutdown reaches the whole process group — chroma's
multiprocessing workers and resource_tracker child included.

T1/T3 non-collision invariant: T1 uses ephemeral tempdirs + a leased
registry record at ``t1_addr.<session_id>`` (RDR-149 P4); T3 uses
``nexus.config._default_local_path()`` + ``t3_addr.<uid>``. Both pick free
ports via the OS allocator. Distinct addr-file naming and distinct chroma
--path roots mean two coexisting chroma subprocesses on the same host do
not collide.
"""
from __future__ import annotations

import errno
import json
import os
import signal
import socket
import subprocess
import sys
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import structlog

from nexus.daemon.service_registry import (
    DEFAULT_HEARTBEAT_INTERVAL,
    ServiceRegistry,
    ServiceSupervisor,
)

_log = structlog.get_logger(__name__)


# chroma listens on loopback only (RDR-120 §Approach scope: no
# cross-host federation, no non-loopback TCP). The chroma CLI default
# is 127.0.0.1; we pass it explicitly for clarity.
_T3_HOST: str = "127.0.0.1"

# Discovery payload format version. Bump when the shape changes.
_DISCOVERY_FORMAT_VERSION: int = 1

# How long to wait for the chroma subprocess to begin accepting TCP
# connections before declaring the start a failure.
_READY_TIMEOUT: float = 10.0

# After SIGTERM, wait this long before escalating to SIGKILL.
_GRACEFUL_STOP_TIMEOUT: float = 3.0


class T3CloudModeError(RuntimeError):
    """Raised when ``start_t3_daemon`` is invoked in cloud mode.

    chromadb's CloudClient already speaks HTTP to a remote service;
    running ``chroma run`` locally would serve nothing useful. Clients
    in cloud mode bypass the daemon entirely (P1.B / nexus-beoh1
    enforces this on the T3Client factory side).
    """


class T3StartError(RuntimeError):
    """Raised when the chroma subprocess fails to become ready."""


def t3_discovery_path(config_dir: Path) -> Path:
    """Return the canonical discovery-file path for the T3 daemon.

    Delegates to ``nexus.daemon.discovery.discovery_path(tier='t3')`` so
    the daemon's WRITE side and the client's READ side derive the path
    from the same single source.
    """
    from nexus.daemon.discovery import discovery_path as _disc_path
    return _disc_path(config_dir, tier="t3")


def _build_payload(
    *,
    tcp_port: int,
    pid: int,
    local_path: Path,
    daemon_version: str,
) -> dict[str, Any]:
    return {
        "format_version": _DISCOVERY_FORMAT_VERSION,
        "tcp_host": _T3_HOST,
        "tcp_port": tcp_port,
        "pid": pid,
        "daemon_version": daemon_version,
        "start_time": datetime.now(timezone.utc).isoformat(),
        "local_path": str(local_path),
    }


def _write_discovery_atomic(path: Path, payload: dict[str, Any]) -> None:
    """Atomically write *path* with 0o600 permissions.

    Create the file at mode 0o600 via ``os.open`` so the file is never
    world-readable on disk (a prior implementation created at the
    umask-applied mode and chmodded after, leaving a TOCTOU window
    where the PID + TCP port leaked on multi-user hosts). Then write +
    close + replace.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    body = json.dumps(payload).encode("utf-8")
    fd = os.open(
        str(tmp),
        os.O_WRONLY | os.O_CREAT | os.O_TRUNC,
        0o600,
    )
    try:
        os.write(fd, body)
    finally:
        os.close(fd)
    os.replace(str(tmp), str(path))


def _read_discovery(path: Path) -> dict[str, Any] | None:
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text())
    except (OSError, json.JSONDecodeError) as exc:
        _log.warning("t3_discovery_read_failed", path=str(path), err=str(exc))
        return None


def _pid_is_alive(pid: int) -> bool:
    """Return True iff signalling pid 0 to *pid* succeeds (or hits EPERM —
    process exists but owned by another uid, treat as alive)."""
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError as exc:
        return exc.errno != errno.ESRCH
    return True


def _find_chroma() -> str:
    """Locate the chroma CLI co-installed with this interpreter.

    chromadb is a hard dependency, so the chroma entry-point lives in
    the same bin directory as the Python interpreter. Falls back to a
    PATH search.
    """
    candidate = Path(sys.executable).parent / "chroma"
    if candidate.is_file():
        return str(candidate)
    import shutil
    found = shutil.which("chroma")
    if not found:
        raise T3StartError(
            "chroma CLI not found alongside Python interpreter or on PATH. "
            "Reinstall nexus to restore the chromadb entry-point."
        )
    return found


def _daemon_version() -> str:
    """Return the conexus package version embedded in discovery payloads."""
    try:
        from importlib.metadata import version
        return version("conexus")
    except Exception:
        return "0.0.0"


def _allocate_free_port() -> int:
    """Bind a free loopback port, then close it.

    No ``SO_REUSEADDR`` on the probe: with REUSEADDR another listener
    can steal the same port between close and chroma's bind. The probe
    is closed immediately so the kernel TIME_WAIT window suffices to
    guard against double-allocation; the TOCTOU window between close
    and chroma binding the port is negligible on loopback.
    """
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.bind((_T3_HOST, 0))
    port: int = sock.getsockname()[1]
    sock.close()
    return port


def _wait_for_ready(
    host: str, port: int, proc: subprocess.Popen[bytes], timeout: float
) -> None:
    """Poll until *host:port* accepts TCP or *proc* exits."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if proc.poll() is not None:
            raise T3StartError(
                f"chroma run exited with code {proc.returncode} "
                f"before becoming ready on {host}:{port}"
            )
        try:
            conn = socket.create_connection((host, port), timeout=0.5)
            conn.close()
            return
        except OSError:
            time.sleep(0.2)
    proc.kill()
    raise T3StartError(
        f"chroma run on {host}:{port} did not become ready within {timeout:.0f}s"
    )


#: Spawn-lock file: serialises parallel ``start_t3_daemon`` calls so
#: two callers do not race to spawn two chroma subprocesses against
#: the same config_dir, both writing the same discovery path (the
#: stress harness ``TestSpawnLockContention`` surfaces this).
_T3_SPAWN_LOCK_FILE: str = "t3_spawn.lock"


def _flatten_lease_payload(record: Any) -> dict[str, Any]:
    """Flatten a ``LeaseRecord`` to the legacy-shaped dict that
    ``start_t3_daemon`` callers expect (top-level tcp_host / tcp_port /
    pid / local_path) plus the lease metadata."""
    ep = dict(record.endpoint)
    return {
        "format_version": 1,
        "tcp_host": ep.get("tcp_host"),
        "tcp_port": ep.get("tcp_port"),
        "pid": ep.get("pid"),  # the chroma subprocess pid
        "local_path": ep.get("local_path"),
        "daemon_version": record.version,
        "generation": record.generation,
        "owner_token": record.owner_token,
        "supervisor_pid": record.payload.get("supervisor_pid"),
    }


class T3Supervisor:
    """RDR-149 P3: the long-lived T3 supervisor (the user-chosen model).

    Mirrors the T2 daemon's role for chroma: it spawns the managed
    ``chroma run`` subprocess, publishes a lease via the shared
    ``ServiceRegistry`` (scope = uid), and re-stamps that lease every
    heartbeat interval — but ONLY while its chroma child is alive AND
    serving (RF-4: liveness = chroma pid alive ∧ port reachable; chroma
    cannot heartbeat a nexus lease itself). The lease endpoint carries
    chroma's connection fields + pid; the lease ``payload`` carries the
    supervisor's own pid (the process ``stop`` signals).

    Version-skew cycle (the #1112 fix): for a long-lived Python supervisor,
    "cycle to the current version" means RESTARTING THE SUPERVISOR PROCESS
    — respawning chroma alone cannot refresh the supervisor's own (now
    stale) bytecode. So the cycle is a process restart, orchestrated
    uniformly across all supervised tiers by the upgrade
    (``upgrade._cycle_supervised_daemons_to_current``), not an in-process
    method here. ``ServiceSupervisor.cycle_to_current`` remains the generic
    in-process primitive for services whose code is not process-bound.
    """

    def __init__(
        self,
        *,
        config_dir: Path,
        local_path: Path,
        lease_clock: Any = time.time,
        supervised: bool = False,
    ) -> None:
        self._config_dir = config_dir
        self._local_path = local_path
        self._lease_clock = lease_clock
        # Only the long-lived runner (run_t3_supervisor) records its pid as
        # the lease's supervisor_pid — that is the process ``stop`` signals.
        # A bare start_t3_daemon (no persistent heartbeater) must NOT record
        # the caller's pid, or ``stop`` would signal an unrelated process
        # (e.g. the test runner) instead of chroma.
        self._supervised = supervised
        self._scope = str(os.getuid())
        self._proc: subprocess.Popen[bytes] | None = None
        self._registry: ServiceRegistry | None = None
        self._supervisor: ServiceSupervisor | None = None
        self._payload: dict[str, Any] | None = None

    @property
    def chroma_pid(self) -> int | None:
        return self._proc.pid if self._proc is not None else None

    @property
    def fenced(self) -> bool:
        return self._supervisor is not None and self._supervisor.fenced

    def _spawn_chroma(self) -> tuple[subprocess.Popen[bytes], int]:
        self._local_path.mkdir(parents=True, exist_ok=True)
        chroma = _find_chroma()
        port = _allocate_free_port()
        proc = subprocess.Popen(
            [chroma, "run", "--host", _T3_HOST, "--port", str(port),
             "--path", str(self._local_path)],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
        try:
            _wait_for_ready(_T3_HOST, port, proc, _READY_TIMEOUT)
        except T3StartError:
            t3_discovery_path(self._config_dir).unlink(missing_ok=True)
            raise
        return proc, port

    def _publish(self, port: int) -> None:
        assert self._proc is not None
        endpoint = {
            "tcp_host": _T3_HOST,
            "tcp_port": port,
            "pid": self._proc.pid,
            "local_path": str(self._local_path),
        }
        self._endpoint_port = port
        self._registry = ServiceRegistry(
            dir=self._config_dir, tier="t3", clock=self._lease_clock
        )
        self._supervisor = ServiceSupervisor(
            self._registry,
            self._scope,
            version=_daemon_version(),
            endpoint_provider=lambda: endpoint,
            payload={"supervisor_pid": os.getpid()} if self._supervised else {},
        )
        record = self._supervisor.publish_once()
        self._payload = _flatten_lease_payload(record)

    def start(self) -> dict[str, Any]:
        """Acquire the spawn lock, spawn chroma, publish the lease. Returns
        the flat discovery payload. Idempotent: a live lease short-circuits
        to the existing payload without a duplicate spawn."""
        import fcntl

        from nexus.config import is_local_mode

        if not is_local_mode():
            raise T3CloudModeError(
                "T3 daemon is a no-op in cloud mode. chromadb's CloudClient "
                "is already HTTP-served; there is no local daemon to run. "
                "Set NX_LOCAL=1 to opt into local mode."
            )

        from nexus.daemon.discovery import find_t3_daemon

        self._config_dir.mkdir(parents=True, exist_ok=True)
        lock_path = self._config_dir / _T3_SPAWN_LOCK_FILE
        lock_fd = os.open(str(lock_path), os.O_WRONLY | os.O_CREAT, 0o600)
        try:
            fcntl.flock(lock_fd, fcntl.LOCK_EX)
            existing = find_t3_daemon(self._config_dir)
            if existing is not None:
                _log.info(
                    "t3_daemon_already_running",
                    pid=existing.get("pid"),
                    tcp_port=existing.get("tcp_port"),
                )
                self._payload = existing
                return existing

            proc, port = self._spawn_chroma()
            self._proc = proc
            self._publish(port)
            _log.info(
                "t3_daemon_started",
                pid=proc.pid,
                tcp_port=port,
                local_path=str(self._local_path),
            )
            assert self._payload is not None
            return self._payload
        finally:
            try:
                fcntl.flock(lock_fd, fcntl.LOCK_UN)
            except OSError:
                pass
            try:
                os.close(lock_fd)
            except OSError:
                pass

    def _chroma_reachable(self) -> bool:
        """RF-4: liveness is (chroma pid alive) AND (chroma port reachable).
        A wedged chroma (pid alive, not accepting connections) must NOT keep
        the lease fresh, or clients resolve a dead-but-not-exited endpoint —
        exactly the stale-but-live-pid class the substrate exists to kill."""
        port = getattr(self, "_endpoint_port", None)
        if port is None:
            return False
        try:
            with socket.create_connection((_T3_HOST, port), timeout=0.5):
                return True
        except OSError:
            return False

    def heartbeat_once(self) -> bool:
        """Re-stamp the lease iff chroma is alive AND serving (RF-4). Returns
        False only when chroma has EXITED (the caller stops; the process
        supervisor restarts us). A transiently-unreachable-but-alive chroma
        returns True but does NOT re-stamp, so the lease ages out and clients
        see 'down' until chroma serves again."""
        if self._proc is None or self._supervisor is None:
            return False
        if self._proc.poll() is not None:
            return False  # chroma exited; stop heartbeating so the lease ages out
        if not self._chroma_reachable():
            _log.warning("t3_daemon_chroma_unreachable", port=self._endpoint_port)
            return True  # keep supervising; skip the heartbeat so the lease expires
        self._supervisor.heartbeat_tick()
        if self._supervisor.fenced:
            _log.warning("t3_daemon_lease_fenced", scope=self._scope)
        return True

    def _stop_chroma(self) -> None:
        if self._proc is None:
            return
        from nexus.util.process_group import safe_killpg

        pid = self._proc.pid
        if _pid_is_alive(pid):
            safe_killpg(pid, signal.SIGTERM)
            deadline = time.monotonic() + _GRACEFUL_STOP_TIMEOUT
            while time.monotonic() < deadline and _pid_is_alive(pid):
                time.sleep(0.1)
            if _pid_is_alive(pid):
                safe_killpg(pid, signal.SIGKILL)
        self._proc = None

    def stop(self) -> None:
        """Relinquish the lease (own-record-only) and stop chroma."""
        if self._registry is not None and self._supervisor is not None:
            rec = self._supervisor.record
            if rec is not None:
                self._registry.relinquish(rec)
        self._stop_chroma()
        self._supervisor = None
        self._registry = None
        self._payload = None


def start_t3_daemon(*, config_dir: Path, local_path: Path) -> dict[str, Any]:
    """Spawn the managed chroma subprocess and publish its lease, returning
    the flat discovery payload.

    RDR-149 P3: the on-disk record is now a leased registry record; the
    returned dict keeps the legacy flat shape (top-level tcp_host /
    tcp_port / pid / local_path) for back-compat. Continuous lease
    heartbeating is provided by the long-lived supervisor
    (``run_t3_supervisor``, the ``--foreground`` path the service
    templates run); a bare ``start_t3_daemon`` publishes a lease that the
    next supervisor tick (or the TTL grace window) covers.

    Idempotent on a live lease; parallel callers serialise on the
    ``t3_spawn.lock`` fcntl lock (inside ``T3Supervisor.start``).

    Raises:
        T3CloudModeError: when ``is_local_mode()`` is False.
        T3StartError: when chroma cannot be located or fails to become
            ready within ``_READY_TIMEOUT``.
    """
    return T3Supervisor(config_dir=config_dir, local_path=local_path).start()


def run_t3_supervisor(*, config_dir: Path, local_path: Path) -> int:
    """Blocking long-lived T3 supervisor (the ``--foreground`` path).

    Spawns chroma, publishes the lease, then heartbeats it every interval
    while chroma is alive. On SIGTERM/SIGINT it relinquishes the lease and
    stops chroma; if chroma exits on its own it returns a non-zero code so
    the process supervisor (launchd KeepAlive / systemd Restart) respawns.
    Returns the intended process exit code.
    """
    # P3 review H-3: register signal handlers BEFORE start(), so a SIGTERM
    # arriving during chroma startup (up to _READY_TIMEOUT) is captured and
    # leads to a clean stop() rather than the default handler killing us and
    # orphaning chroma / leaking the lease.
    stop_requested = threading.Event()

    def _on_signal(_signum: int, _frame: Any) -> None:
        stop_requested.set()

    signal.signal(signal.SIGTERM, _on_signal)
    signal.signal(signal.SIGINT, _on_signal)

    sup = T3Supervisor(
        config_dir=config_dir, local_path=local_path, supervised=True
    )
    sup.start()

    exit_code = 0
    # A signal during start() -> stop immediately (start already published;
    # stop() relinquishes + tears down chroma).
    while not stop_requested.is_set():
        if not sup.heartbeat_once():
            _log.warning("t3_supervisor_chroma_exited", msg="chroma child gone")
            exit_code = 3
            break
        time.sleep(DEFAULT_HEARTBEAT_INTERVAL)
    sup.stop()
    return exit_code


def stop_t3_daemon(*, config_dir: Path) -> int | None:
    """Stop the running T3 daemon. Sends SIGTERM (escalating to SIGKILL
    after ``_GRACEFUL_STOP_TIMEOUT``) and unlinks the discovery file.

    Returns the PID that was signalled, or ``None`` when no discovery
    file exists. Process-group SIGTERM ensures chroma's multiprocessing
    workers + resource_tracker are signalled too.
    """
    from nexus.util.process_group import safe_killpg

    from nexus.daemon.discovery import (
        find_t3_daemon,
        is_lease_record,
        normalize_discovery_view,
    )

    disc_path = t3_discovery_path(config_dir)
    payload = _read_discovery(disc_path)
    if payload is None:
        _log.info("t3_daemon_stop_noop", reason="no_discovery_file")
        return None

    # RDR-149 P3: prefer signalling the long-lived supervisor — it
    # relinquishes the lease and tears down chroma's process group itself.
    # The supervisor pid rides the lease ``payload.supervisor_pid``; the
    # chroma pid is the endpoint pid (the bare-start / legacy fallback).
    #
    # CRITICAL (P3 review C-1): only trust ``supervisor_pid`` from a FRESH
    # lease. A stale lease left by a SIGKILL'd supervisor names a pid that
    # the kernel may have recycled to an unrelated same-uid process; the
    # freshness gate prevents SIGTERM-ing it. A stale lease falls through to
    # the chroma-pid path (also dead by then -> a clean unlink).
    supervisor_pid = None
    if is_lease_record(payload) and find_t3_daemon(config_dir) is not None:
        supervisor_pid = (payload.get("payload") or {}).get("supervisor_pid")
    if (
        isinstance(supervisor_pid, int)
        and supervisor_pid > 0
        and _pid_is_alive(supervisor_pid)
    ):
        try:
            os.kill(supervisor_pid, signal.SIGTERM)
        except (ProcessLookupError, PermissionError):
            pass
        deadline = time.monotonic() + _GRACEFUL_STOP_TIMEOUT
        while time.monotonic() < deadline:
            if not _pid_is_alive(supervisor_pid) or not disc_path.exists():
                break
            time.sleep(0.1)
        if _pid_is_alive(supervisor_pid):
            try:
                os.kill(supervisor_pid, signal.SIGKILL)
            except (ProcessLookupError, PermissionError):
                pass
        disc_path.unlink(missing_ok=True)
        _log.info("t3_daemon_stopped", supervisor_pid=supervisor_pid)
        return supervisor_pid

    # No live supervisor: signal the chroma process group directly.
    pid = normalize_discovery_view(payload).get("pid")
    if not isinstance(pid, int) or pid <= 0:
        _log.warning("t3_daemon_stop_invalid_pid", payload=payload)
        disc_path.unlink(missing_ok=True)
        return None

    if not _pid_is_alive(pid):
        _log.info("t3_daemon_stop_stale_pid", pid=pid)
        disc_path.unlink(missing_ok=True)
        return pid

    if safe_killpg(pid, signal.SIGTERM):
        deadline = time.monotonic() + _GRACEFUL_STOP_TIMEOUT
        while time.monotonic() < deadline:
            if not _pid_is_alive(pid):
                break
            time.sleep(0.1)
        if _pid_is_alive(pid):
            safe_killpg(pid, signal.SIGKILL)
            try:
                os.waitpid(pid, os.WNOHANG)
            except (ChildProcessError, OSError):
                pass
            # Confirm SIGKILL took effect before removing the discovery
            # file. If the process survives a brief window (Linux
            # uninterruptible sleep, foreign-uid race), unlinking would
            # orphan a chroma still bound to its port: the next ``start``
            # would allocate a fresh port and the old daemon would leak.
            # Brief polling, then leave the discovery file in place and
            # surface a loud warning.
            confirm_deadline = time.monotonic() + 1.0
            while time.monotonic() < confirm_deadline:
                if not _pid_is_alive(pid):
                    break
                time.sleep(0.05)
            if _pid_is_alive(pid):
                _log.warning(
                    "t3_daemon_stop_kill_failed",
                    pid=pid,
                    msg="SIGKILL did not reap process; discovery file "
                        "preserved to avoid orphaning a bound port",
                )
                return pid

    disc_path.unlink(missing_ok=True)
    _log.info("t3_daemon_stopped", pid=pid)
    return pid
