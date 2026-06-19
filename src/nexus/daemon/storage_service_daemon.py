# SPDX-License-Identifier: AGPL-3.0-or-later
"""RDR-152 P5.1 (nexus-gmiaf.30) — Storage-service + Postgres supervisor.

Mirrors ``daemon/t3_daemon.py`` (T3Supervisor / run_t3_supervisor) on the
RDR-149 shared primitive (``ServiceRegistry`` + ``ServiceSupervisor``).

Per ``src/nexus/daemon/AGENTS.md`` (the standing gate), ALL lifecycle
logic lives in the shared primitive, not here. This module:

1. Ensures the nx-managed Postgres cluster is running (reusing
   ``pg_provision._start_cluster``).
2. Starts the native nexus-service binary (RDR-157 native-image; acquired via
   ``nx daemon service install-binary``) with the environment variables read
   from ``pg_credentials``, a free ``NX_SERVICE_PORT``, and process-group
   isolation (``start_new_session=True`` / ``os.killpg`` on stop). The native
   binary is the SOLE launch artifact — RDR-161 expunged the legacy JVM
   launch path.
3. Waits for ``GET /health`` to return HTTP 200 (richer than bare-TCP: the
   HealthHandler returns ``{"status":"ok","db":"up"}`` or ``{"status":"error",
   "db":"down"}`` with a 503 status).
4. Publishes a lease via ``ServiceRegistry(tier="storage_service")`` under
   scope=str(os.getuid()) ONLY after a 200 response. The endpoint carries
   ``{"host": ..., "port": ..., "token": ...}``. The token is also published
   so clients can re-read it after an auto-restart (see HIGH-3 fix note).
   This matches what ``health._resolve_service_endpoint`` reads: tier=
   "storage_service", scope=str(os.getuid()) → addr file
   ``storage_service_addr.<uid>``.
5. Heartbeats the lease while: (a) service pid is alive, (b) ``/health`` returns
   200, (c) Postgres TCP-reachable. Delegates to ``supervisor.heartbeat_tick()``.
   When PG dies independently (service still alive), the run loop calls
   ``_ensure_pg_running()`` directly — a PG restart without a full service respawn.
   When the service is alive but ``/health`` returns non-200 for
   ``_MAX_UNHEALTHY_HEARTBEATS`` consecutive beats (stuck process: connection-pool
   exhaustion, internal deadlock), ``heartbeat_once()`` returns ``(False, pg_ok)``
   to force a respawn — treating a stuck process identically to a process death.
6. ``mark_shutting_down()`` BEFORE ``os.killpg`` (RDR-151 P1.3 ordering).
7. Auto-restarts on service death with a strictly higher generation (the primitive
   handles generation/fencing). ``_restart_count`` is windowed: reset to 0
   after ``_RESTART_WINDOW_HEARTBEATS`` clean heartbeats so transient clusters
   of failures don't permanently exhaust the budget.
8. LOUD failure when the service stays unreachable: ``StorageServiceStartError``
   (structured log + exception, no silent fall-through).

Postgres lifecycle ownership: the supervisor starts PG on demand but intentionally
does NOT stop PG on ``stop()`` — Postgres is an independently managed process that
may serve other clients. Only the nexus-service process group is managed by the
supervisor.

No direct-mode fallback — a service/PG outage is always fatal for callers.
"""
from __future__ import annotations

import contextlib
import errno
import os
import signal
import socket
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any, Callable, Optional

import structlog

from nexus.daemon.service_registry import (
    DEFAULT_HEARTBEAT_INTERVAL,
    ServiceRegistry,
    ServiceSupervisor,
)

_log = structlog.get_logger(__name__)

# ── Constants ──────────────────────────────────────────────────────────────────

#: Scope key published to the registry; MUST match health._STORAGE_SERVICE_SCOPE_KEY.
STORAGE_SERVICE_SCOPE_KEY: str = "storage_service"

#: Registry tier prefix. addr files = ``storage_service_addr.<uid>``.
#: DISTINCT from "t2" — see health._resolve_service_endpoint for the
#: tier+scope contract.
_REGISTRY_TIER: str = "storage_service"

#: The host the Java service binds to (loopback-only, no cross-host federation).
_SERVICE_HOST: str = "127.0.0.1"

#: Path suffix of the spawn lock file inside config_dir.
_SPAWN_LOCK_FILE: str = "storage_service_spawn.lock"

#: How long to wait for the service /health to return 200 before failing.
_READY_TIMEOUT: float = 60.0

#: Interval between /health polls during startup.
_READY_POLL_INTERVAL: float = 0.5

#: After SIGTERM, wait this long before escalating to SIGKILL.
_GRACEFUL_STOP_TIMEOUT: float = 5.0

#: Max auto-restart attempts in a window before giving up.
_MAX_RESTART_ATTEMPTS: int = 3

#: Delay between restart attempts.
_RESTART_BACKOFF: float = 2.0

#: Short HTTP timeout for /health probes.
_HEALTH_TIMEOUT: float = 2.0

#: Number of clean heartbeats after a restart before the restart budget resets.
#: Prevents lifetime-cumulative exhaustion from transient failure clusters.
#: At DEFAULT_HEARTBEAT_INTERVAL=1s, 300 heartbeats ≈ 5 minutes.
_RESTART_WINDOW_HEARTBEATS: int = 300

#: Consecutive heartbeats where the process is alive but /health returns
#: non-200 before triggering a forced respawn. Handles stuck-but-alive states
#: (connection-pool exhaustion, GC pause, internal deadlock) that are the
#: most common partial-failure mode. 3 beats at 1s interval = a 3s
#: grace window before respawn — large enough to absorb transient GC pauses,
#: small enough to recover quickly from real deadlocks.
_MAX_UNHEALTHY_HEARTBEATS: int = 3


# ── Errors ─────────────────────────────────────────────────────────────────────


class StorageServiceStartError(RuntimeError):
    """Raised when the storage service or Postgres fails to become ready.

    Per the LOUD-failure contract: a service/PG outage blocks all storage
    and must surface clearly. Never silently degrade.
    """


# ── Credential helpers ─────────────────────────────────────────────────────────


def _read_pg_credentials(creds_path: Path) -> dict[str, str]:
    """Parse ``pg_credentials`` shell-env file into a {key: value} dict.

    Lines starting with ``#`` and blank lines are skipped.
    Raises ``FileNotFoundError`` if the file is absent.
    """
    result: dict[str, str] = {}
    for line in creds_path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if "=" in line:
            k, _, v = line.partition("=")
            result[k.strip()] = v.strip()
    return result


# ── Native binary discovery ──────────────────────────────────────────────────


def _find_service_binary(config_dir: Path) -> Path | None:
    """Locate the nexus-service NATIVE binary, or None when none is installed.

    RDR-157 ships per-OS/arch native-image binaries (no JVM); RDR-161 made the
    native binary the SOLE launch artifact. When one is present the supervisor
    execs it directly. Absence is NOT an error here — the caller raises loudly
    (there is no legacy JVM fallback to defer to).

    Search order:
    1. ``NEXUS_SERVICE_BIN`` env override. Set-but-missing FAILS LOUD — an
       operator who named a binary that does not exist made a mistake worth
       surfacing.
    2. The well-known installed location ``<config_dir>/service/nexus-service``.
    """
    # NOTE (RDR-157 P4.2, bead nexus-vwvv5.18): positioning the binary at the
    # well-known path / setting NEXUS_SERVICE_BIN is the distribution launcher's
    # job, delivered by the fresh-machine E2E. P4.1 only makes the supervisor
    # ABLE to launch a binary that is already present.
    env_override = os.environ.get("NEXUS_SERVICE_BIN", "").strip()
    if env_override:
        p = Path(env_override)
        if p.is_file():
            return _require_executable(p, "NEXUS_SERVICE_BIN")
        raise StorageServiceStartError(
            f"NEXUS_SERVICE_BIN is set to {env_override!r} but the file does not "
            "exist. Point it at a built native nexus-service binary, or unset it "
            "to use the installed binary."
        )

    from nexus.daemon.binary_lifecycle import well_known_binary_path
    well_known = well_known_binary_path(config_dir)
    if well_known.is_file():
        return _require_executable(well_known, "nexus-service")
    return None


def _require_executable(p: Path, label: str) -> Path:
    """Return *p* if it carries the execute bit; fail loud with a chmod remedy.

    A present-but-non-executable binary would otherwise surface as a bare
    ``PermissionError`` from ``subprocess.Popen`` — an errno, not a remedy.
    """
    if not os.access(p, os.X_OK):
        raise StorageServiceStartError(
            f"{label} at {p} is not executable. Make it runnable: chmod +x {p}"
        )
    return p


# ── Port helpers ───────────────────────────────────────────────────────────────


def _allocate_free_port(host: str = _SERVICE_HOST) -> int:
    """Bind a free loopback port, then close it.

    No SO_REUSEADDR on the probe socket: with REUSEADDR another listener
    could steal the same port between close and the JVM's bind. The probe
    is closed immediately so the kernel TIME_WAIT window guards against
    double-allocation; the TOCTOU window between close and JVM bind is
    negligible on loopback (mirrors t3_daemon._allocate_free_port).
    """
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.bind((host, 0))
    port: int = sock.getsockname()[1]
    sock.close()
    return port


def _port_accepting(host: str, port: int, timeout: float = 0.5) -> bool:
    """Return True when *host:port* accepts a TCP connection."""
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


# ── Pid helpers ────────────────────────────────────────────────────────────────


def _pid_is_alive(pid: int) -> bool:
    """Return True if signalling pid 0 to *pid* succeeds."""
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


# ── Version helper ─────────────────────────────────────────────────────────────


def _daemon_version() -> str:
    """Return the conexus package version for the lease."""
    try:
        from importlib.metadata import version
        return version("conexus")
    except Exception:
        return "0.0.0"


# ── StorageServiceSupervisor ───────────────────────────────────────────────────


class StorageServiceSupervisor:
    """RDR-149 P5.1: the long-lived storage-service supervisor.

    Mirrors :class:`nexus.daemon.t3_daemon.T3Supervisor` for the Java
    storage service + local Postgres backend.

    Lifecycle invariants (RDR-151 + AGENTS.md):
    - Lease published ONLY after ``/health`` returns 200 (DB confirmed up).
    - Heartbeat = (service pid alive) AND (/health 200) AND (PG TCP reachable),
      then ``supervisor.heartbeat_tick()``.
    - PG-only death (service still alive): ``_ensure_pg_running()`` called
      directly from the run loop without a full service respawn.
    - ``mark_shutting_down()`` BEFORE ``os.killpg`` (RDR-151 P1.3).
    - Auto-restart on service death: respawn + republish (higher generation).
    - ``_restart_count`` is windowed (reset after _RESTART_WINDOW_HEARTBEATS
      clean heartbeats) so transient failure clusters don't permanently
      exhaust the budget.
    - NX_SERVICE_TOKEN included in the lease endpoint so clients can
      re-read the token after an auto-restart.
    - LOUD failure: cannot bring up the service -> ``StorageServiceStartError``.

    Postgres ownership: the supervisor starts PG on demand and restarts it
    independently when it dies, but does NOT stop PG on stop() — PG is an
    independently managed process that may serve other clients.

    All lifecycle state (generation, fencing, TTL) lives in the shared
    ``ServiceRegistry`` / ``ServiceSupervisor`` primitive; this class only
    orchestrates process management.
    """

    def __init__(
        self,
        *,
        config_dir: Path,
        pg_port: int,
        service_port: int,
        creds: dict[str, str],
        binary_path: Path,
        lease_clock: Callable[[], float] = time.time,
        supervised: bool = False,
    ) -> None:
        # RDR-161: the native binary is the SOLE launch artifact — the
        # legacy JVM launch path is expunged. A missing binary is fatal here.
        if binary_path is None:
            raise StorageServiceStartError(
                "StorageServiceSupervisor needs a native binary to launch; "
                "none was provided. Acquire one via "
                "'nx daemon service install-binary <tag>'."
            )
        self._config_dir = config_dir
        self._binary_path = binary_path
        self._svc_log_name = "storage_service_native"
        self._pg_port = pg_port
        self._service_port = service_port
        self._creds = creds
        self._lease_clock = lease_clock
        self._supervised = supervised
        self._scope = str(os.getuid())
        self._proc: subprocess.Popen[bytes] | None = None
        self._registry: ServiceRegistry | None = None
        self._supervisor: ServiceSupervisor | None = None
        self._restart_count: int = 0
        # Windowed restart budget: counts clean heartbeats since the last restart.
        self._clean_heartbeats_since_restart: int = 0
        # Consecutive unhealthy heartbeats counter: process alive but /health
        # non-200. When this reaches _MAX_UNHEALTHY_HEARTBEATS the run loop
        # treats the stuck process like a process death and triggers _respawn().
        self._consecutive_unhealthy_heartbeats: int = 0
        # Persistent root bearer token (gmiaf.32.5). Read from pg_credentials
        # (or the env override); NOT derived from DB passwords. Stable across
        # restarts because it is persisted, not because it is a function of the
        # credentials. Clients re-read it from the lease endpoint after restart.
        self._service_token: str = self._resolve_service_token()

    def _resolve_service_token(self) -> str:
        """Return the persistent NX_SERVICE_TOKEN (the bound root token).

        Resolution order:
        1. ``NX_SERVICE_TOKEN`` env var (operator / test override).
        2. ``NX_SERVICE_TOKEN`` persisted in ``pg_credentials`` by
           ``nx init --service`` (a random secret minted at provisioning time).

        Retires the gmiaf.30 ``_derive_stable_token`` coupling: the token is a
        random secret, NOT ``sha256(admin_pass:svc_pass)``, so rotating the DB
        passwords does not change the bearer token and reading pg_credentials no
        longer reveals a derivable token. Fail loud if absent — a missing token
        means the cluster was not provisioned through ``nx init --service``
        (no silent fallback for an auth-correctness input).
        """
        env_tok = os.environ.get("NX_SERVICE_TOKEN", "").strip()
        if env_tok:
            _log.info("storage_service_token_path", path="env")
            return env_tok
        creds_tok = self._creds.get("NX_SERVICE_TOKEN", "").strip()
        if creds_tok:
            _log.info("storage_service_token_path", path="pg_credentials")
            return creds_tok
        raise StorageServiceStartError(
            "NX_SERVICE_TOKEN is absent from both the environment and "
            "pg_credentials. Run 'nx init --service' to provision the cluster "
            "and persist the root token."
        )

    # -- Public properties --------------------------------------------------

    @property
    def service_pid(self) -> int | None:
        return self._proc.pid if self._proc is not None else None

    @property
    def fenced(self) -> bool:
        return self._supervisor is not None and self._supervisor.fenced

    # -- Internal helpers ---------------------------------------------------

    def _spawn_service(self) -> tuple[subprocess.Popen[bytes], int]:
        """Spawn the native service process with env vars, returning (proc, port).

        RDR-161: the RDR-157 native binary (``self._binary_path``) is the sole
        launch artifact. Configuration reaches the service ENTIRELY via the
        environment below.
        """
        port = _allocate_free_port()
        env = dict(os.environ)
        # Credentials from pg_credentials
        for k in (
            "NX_DB_URL", "NX_DB_USER", "NX_DB_PASS",
            "NX_DB_ADMIN_URL", "NX_DB_ADMIN_USER", "NX_DB_ADMIN_PASS",
        ):
            if k in self._creds:
                env[k] = self._creds[k]
        env["NX_SERVICE_PORT"] = str(port)
        # NX_CHROMA_PATH injection removed (RDR-155 P4a.2, nexus-1k8s1): the
        # Java service no longer reads any NX_CHROMA_* variable — it serves
        # vectors from pgvector. Leaving the injection in place was harmless
        # but misleading to operators inspecting the process env (P4a.2
        # dual-review finding M-2).
        # Use the stable token so clients don't get 401 after a restart.
        env["NX_SERVICE_TOKEN"] = self._service_token

        # nexus-pebfx.2: the service only reads NX_VOYAGE_API_KEY; without it the
        # service embeds local ONNX (RDR-160: bge-768) and refuses every
        # voyage-* collection. Resolve
        # through the nexus credential chain (VOYAGE_API_KEY env > config.yml
        # credentials) so `nx daemon service start` works without manual env
        # plumbing. An explicit NX_VOYAGE_API_KEY in the caller's env wins.
        if not env.get("NX_VOYAGE_API_KEY"):
            from nexus.config import get_credential
            voyage_key = get_credential("voyage_api_key")
            if voyage_key:
                env["NX_VOYAGE_API_KEY"] = voyage_key
                _log.info("storage_service_voyage_key_resolved", source="credential_chain")
            else:
                _log.warning(
                    "storage_service_no_voyage_key",
                    embedding_mode="onnx-local",
                    consequence="voyage-* collections will be refused (HTTP 422)",
                    hint="set VOYAGE_API_KEY or `nx config set voyage_api_key <key>`",
                )

        argv = [str(self._binary_path)]
        artifact = str(self._binary_path)
        # nexus-ovbr7: route both streams to one file so interleaved output keeps
        # its order; O_APPEND means a respawn never truncates the previous
        # process's final (crash) output. The native binary writes to
        # storage_service_native.log.
        from nexus.logging_setup import open_child_log_or_devnull

        svc_log = open_child_log_or_devnull(self._svc_log_name, self._config_dir)
        try:
            proc = subprocess.Popen(
                argv,
                env=env,
                stdout=svc_log,
                stderr=svc_log,
                start_new_session=True,
            )
        finally:
            # The child holds its own duplicated fd; the parent's handle is
            # no longer needed (and must not leak across respawns).
            if not isinstance(svc_log, int):
                svc_log.close()
        _log.info(
            "storage_service_spawned",
            pid=proc.pid,
            port=port,
            artifact=artifact,
            service_log=getattr(svc_log, "name", "DEVNULL"),
        )
        return proc, port

    def _service_healthy(self, port: int | None = None) -> bool:
        """Return True iff the service /health endpoint returns HTTP 200."""
        _port = port if port is not None else self._service_port
        if _port <= 0:
            return False
        url = f"http://{_SERVICE_HOST}:{_port}/health"
        try:
            import urllib.request
            req = urllib.request.Request(url, method="GET")
            with urllib.request.urlopen(req, timeout=_HEALTH_TIMEOUT) as resp:
                return resp.status == 200
        except Exception:
            return False

    def _pg_reachable(self) -> bool:
        """Return True iff the Postgres port accepts TCP."""
        return _port_accepting(_SERVICE_HOST, self._pg_port, timeout=0.5)

    def _wait_for_service_ready(
        self,
        proc: subprocess.Popen[bytes],
        port: int,
        timeout: float = _READY_TIMEOUT,
    ) -> None:
        """Poll ``/health`` until 200 or timeout.

        On timeout: send SIGTERM + grace window + SIGKILL (matching _stop_service,
        so the service can flush), then raise loudly.
        Raises :class:`StorageServiceStartError` — the LOUD failure contract.
        """
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if proc.poll() is not None:
                raise StorageServiceStartError(
                    f"Storage service process (pid={proc.pid}) exited with code "
                    f"{proc.returncode} before /health became ready on port {port}. "
                    "Check service logs for details."
                )
            if self._service_healthy(port):
                _log.info("storage_service_ready", port=port, pid=proc.pid)
                return
            time.sleep(_READY_POLL_INTERVAL)

        # Timeout: SIGTERM + grace + SIGKILL (matches _stop_service, not bare SIGKILL)
        # so the service can flush open connections and logs before the kill.
        _log.warning(
            "storage_service_readiness_timeout",
            port=port,
            timeout=timeout,
            pid=proc.pid,
        )
        with contextlib.suppress(Exception):
            from nexus.util.process_group import safe_killpg
            safe_killpg(proc.pid, signal.SIGTERM)
            kill_deadline = time.monotonic() + _GRACEFUL_STOP_TIMEOUT
            while time.monotonic() < kill_deadline and _pid_is_alive(proc.pid):
                time.sleep(0.1)
            if _pid_is_alive(proc.pid):
                safe_killpg(proc.pid, signal.SIGKILL)

        raise StorageServiceStartError(
            f"Storage service did not become healthy at http://{_SERVICE_HOST}:{port}/health "
            f"within {timeout:.0f}s. The service or Postgres may not have started correctly."
        )

    def _publish(self, port: int) -> None:
        """Publish the lease to the registry AFTER service is healthy.

        The endpoint includes the NX_SERVICE_TOKEN so HTTP clients can
        re-read it after a restart (HIGH-3 fix: token in lease endpoint).
        """
        assert self._proc is not None
        endpoint: dict[str, Any] = {
            "host": _SERVICE_HOST,
            "port": port,
            "pid": self._proc.pid,
            "token": self._service_token,
        }
        self._service_port = port
        self._registry = ServiceRegistry(
            dir=self._config_dir,
            tier=_REGISTRY_TIER,
            clock=self._lease_clock,
        )
        self._supervisor = ServiceSupervisor(
            self._registry,
            self._scope,
            version=_daemon_version(),
            endpoint_provider=lambda: endpoint,
            payload={"supervisor_pid": os.getpid()} if self._supervised else {},
        )
        record = self._supervisor.publish_once()
        _log.info(
            "storage_service_lease_published",
            scope=self._scope,
            generation=record.generation,
            port=port,
        )

    def _stop_service(self) -> None:
        """Send SIGTERM (escalating to SIGKILL) to the service process group.

        Postgres is intentionally NOT stopped here — PG is independently
        managed and may serve other clients (see module docstring).
        """
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

    def _respawn(self) -> None:
        """Spawn and re-publish after a process death or stuck-process respawn.

        SIGNIFICANT-2 fix: ``_restart_count`` is windowed — reset to 0 after
        ``_RESTART_WINDOW_HEARTBEATS`` clean heartbeats following a restart.

        ROUND-3 fix: stop the old process group BEFORE spawning the replacement.
        On the natural process-death path the process is already gone, so
        ``_stop_service()`` is a guarded no-op (it checks ``_pid_is_alive``). On
        the stuck-process path (``heartbeat_once`` signals respawn while the
        process is still physically alive) this is load-bearing: without it the
        old process is orphaned, keeps its Postgres connections open, and
        accumulates one leak per respawn cycle. Stopping first also covers the
        budget-exhausted raise path so we never leave a stuck process behind
        when giving up.
        """
        self._stop_service()
        self._restart_count += 1
        self._clean_heartbeats_since_restart = 0  # reset the clean window
        if self._restart_count > _MAX_RESTART_ATTEMPTS:
            raise StorageServiceStartError(
                f"Storage service auto-restart failed {self._restart_count} times "
                f"in the current restart window (max={_MAX_RESTART_ATTEMPTS}). "
                "Giving up. Check service logs and Postgres status."
            )
        _log.warning(
            "storage_service_restarting",
            attempt=self._restart_count,
            max_attempts=_MAX_RESTART_ATTEMPTS,
        )
        time.sleep(_RESTART_BACKOFF)
        proc, port = self._spawn_service()
        self._proc = proc
        self._wait_for_service_ready(proc, port)
        # Republish — ServiceRegistry.publish() under the election flock bumps
        # the generation monotonically, providing the higher-generation fencing
        # the RDR-149 conformance battery requires.
        self._publish(port)

    def _maybe_reset_restart_budget(self) -> None:
        """After a clean heartbeat following a restart, advance the window counter.

        If the window threshold is reached, reset _restart_count to 0 so
        isolated bursts of failures don't permanently exhaust the budget.
        """
        if self._restart_count > 0:
            self._clean_heartbeats_since_restart += 1
            if self._clean_heartbeats_since_restart >= _RESTART_WINDOW_HEARTBEATS:
                _log.info(
                    "storage_service_restart_budget_reset",
                    previous_count=self._restart_count,
                    window=_RESTART_WINDOW_HEARTBEATS,
                )
                self._restart_count = 0
                self._clean_heartbeats_since_restart = 0

    # -- Public lifecycle API -----------------------------------------------

    def start(self) -> dict[str, Any]:
        """Acquire spawn lock, ensure PG is up, spawn service, publish lease.

        Returns the flat discovery payload {host, port, pid, generation, token}.
        Idempotent: a live lease short-circuits without a duplicate spawn.
        Raises :class:`StorageServiceStartError` on failure (LOUD).
        """
        import fcntl

        self._config_dir.mkdir(parents=True, exist_ok=True)
        lock_path = self._config_dir / _SPAWN_LOCK_FILE
        lock_fd = os.open(str(lock_path), os.O_WRONLY | os.O_CREAT, 0o600)
        try:
            fcntl.flock(lock_fd, fcntl.LOCK_EX)
            return self._start_locked()
        finally:
            try:
                fcntl.flock(lock_fd, fcntl.LOCK_UN)
            except OSError:
                pass
            try:
                os.close(lock_fd)
            except OSError:
                pass

    def _start_locked(self) -> dict[str, Any]:
        """Inner start, called under the spawn lock."""
        # Short-circuit: a live lease already exists (parallel caller won the race).
        registry = ServiceRegistry(
            dir=self._config_dir,
            tier=_REGISTRY_TIER,
            clock=self._lease_clock,
        )
        existing = registry.discover(self._scope)
        if existing is not None:
            ep = existing.endpoint
            _log.info(
                "storage_service_already_running",
                host=ep.get("host"),
                port=ep.get("port"),
                generation=existing.generation,
            )
            return {
                "host": ep.get("host", _SERVICE_HOST),
                "port": ep.get("port", 0),
                "pid": ep.get("pid"),
                "generation": existing.generation,
                "token": ep.get("token", ""),
            }

        # Step 1: ensure Postgres is accepting connections on the provisioned port.
        self._ensure_pg_running()

        # RDR-161: the legacy schema-skew gate (nexus-pebfx.4) is expunged with
        # the legacy launch path. A native binary ships its Liquibase changelog baked
        # at build time alongside the schema, so the old-artifact-vs-new-schema
        # risk that gate guarded against does not apply to native launches.

        # Step 2: spawn the native service.
        proc, port = self._spawn_service()
        self._proc = proc

        # Step 3: wait for /health 200 — LOUD failure if it doesn't come up.
        try:
            self._wait_for_service_ready(proc, port)
        except StorageServiceStartError:
            self._stop_service()
            raise

        # Step 4: publish lease ONLY after service is healthy.
        self._publish(port)

        assert self._supervisor is not None
        record = self._supervisor.record
        assert record is not None
        payload = {
            "host": _SERVICE_HOST,
            "port": port,
            "pid": proc.pid,
            "generation": record.generation,
            "token": self._service_token,
        }
        return payload

    def _ensure_pg_running(self) -> None:
        """Confirm Postgres is accepting connections; start it if not.

        Reuses ``pg_provision._start_cluster`` so PG lifecycle logic stays
        in the provisioner, not here. Raises ``StorageServiceStartError``
        if PG cannot be started.

        Known limitation (nexus-14k0m critic): "accepting" means TCP
        accept, not query readiness. A PG in crash recovery (WAL replay)
        accepts connections while rejecting queries, so a success here
        does not guarantee the service's subsequent /health ready-wait (60s)
        will pass — a long replay can still burn one respawn attempt. A
        ``pg_isready``-grade probe is the follow-on if that residual is
        ever observed in practice.
        """
        if _port_accepting(_SERVICE_HOST, self._pg_port):
            _log.debug("storage_service_pg_already_running", port=self._pg_port)
            return

        _log.info("storage_service_starting_pg", port=self._pg_port)
        try:
            from nexus.db.pg_provision import discover_pg_binaries, _start_cluster
            pg_data_str = self._creds.get("PG_DATA", "")
            if not pg_data_str:
                raise StorageServiceStartError(
                    "PG_DATA not found in pg_credentials. "
                    "Re-run 'nx init --service' to reprovision."
                )
            pgdata = Path(pg_data_str)
            bins = discover_pg_binaries()
            _start_cluster(bins, pgdata, self._pg_port)
        except StorageServiceStartError:
            raise
        except Exception as exc:
            raise StorageServiceStartError(
                f"Failed to start Postgres on port {self._pg_port}: {exc}. "
                "Check the Postgres data directory and pg_credentials."
            ) from exc

        if not _port_accepting(_SERVICE_HOST, self._pg_port):
            raise StorageServiceStartError(
                f"Postgres on port {self._pg_port} did not accept connections "
                "after pg_ctl start. Check pg_data/pg.log."
            )
        _log.info("storage_service_pg_ready", port=self._pg_port)

    def heartbeat_once(self) -> tuple[bool, bool]:
        """Re-stamp the lease iff service is alive AND healthy AND PG reachable.

        Returns (service_running, pg_ok) so the run loop can handle PG-only
        failure without triggering a service respawn:

        - (False, _)    — process exited OR stuck-process threshold crossed;
                          caller should call _respawn(). When the process is
                          physically alive but /health has returned non-200 for
                          _MAX_UNHEALTHY_HEARTBEATS consecutive beats, this
                          method returns (False, pg_ok) to force a respawn —
                          a stuck-but-alive process (connection-pool exhaustion,
                          internal deadlock) is treated like a process death.
        - (True, False) — process alive and healthy, PG down; caller should call
                          _ensure_pg_running() directly.
        - (True, True)  — everything healthy; lease re-stamped. NOTE: the
                          (True, True) path is the ONLY path that re-stamps
                          the lease. The (True, False) path does NOT re-stamp
                          so the lease ages out via TTL, making the service
                          appear 'down' to discoverers while the process is alive.

        _consecutive_unhealthy_heartbeats is reset to 0 on any healthy beat
        so transient 503s (GC pause, brief connection spike) do not accumulate
        toward the threshold.
        """
        if self._proc is None or self._supervisor is None:
            return False, False
        if (rc := self._proc.poll()) is not None:
            # nexus-ovbr7: the returncode is the single cheapest diagnostic a
            # dead service process leaves behind (137=SIGKILL/oom, 143=SIGTERM,
            # 1=error) — record it, plus where the process's own output went.
            _log.warning(
                "storage_service_exit_detected",
                pid=self._proc.pid,
                returncode=rc,
                service_log=str(self._config_dir / "logs" / f"{self._svc_log_name}.log"),
            )
            return False, False  # process exited; signal the run loop to respawn

        service_alive = _pid_is_alive(self._proc.pid)
        service_ok = self._service_healthy()
        pg_ok = self._pg_reachable()

        if not service_alive:
            self._consecutive_unhealthy_heartbeats = 0
            return False, pg_ok

        if not service_ok:
            self._consecutive_unhealthy_heartbeats += 1
            _log.warning(
                "storage_service_unhealthy",
                service_ok=service_ok,
                pg_ok=pg_ok,
                port=self._service_port,
                pg_port=self._pg_port,
                consecutive_unhealthy=self._consecutive_unhealthy_heartbeats,
                threshold=_MAX_UNHEALTHY_HEARTBEATS,
            )
            if self._consecutive_unhealthy_heartbeats >= _MAX_UNHEALTHY_HEARTBEATS:
                # Stuck process: treat like a death so the run loop calls _respawn().
                _log.warning(
                    "storage_service_stuck_respawn",
                    consecutive_unhealthy=self._consecutive_unhealthy_heartbeats,
                    msg="Stuck process threshold reached; signalling respawn",
                )
                self._consecutive_unhealthy_heartbeats = 0
                return False, pg_ok
            # Below threshold: lease NOT re-stamped (TTL ages out); do not respawn yet.
            return True, pg_ok

        if not pg_ok:
            # Service is healthy, PG is down. Clear unhealthy counter since
            # the JVM itself is responding correctly.
            self._consecutive_unhealthy_heartbeats = 0
            _log.warning(
                "storage_service_pg_unreachable",
                pg_port=self._pg_port,
            )
            # Lease NOT re-stamped: TTL ages out.
            return True, False

        # Fully healthy path: reset unhealthy counter + re-stamp lease.
        self._consecutive_unhealthy_heartbeats = 0
        self._supervisor.heartbeat_tick()
        if self._supervisor.fenced:
            _log.warning(
                "storage_service_lease_fenced",
                scope=self._scope,
            )
        self._maybe_reset_restart_budget()
        return True, True

    def stop(self) -> None:
        """Graceful shutdown: mark_shutting_down -> relinquish -> killpg.

        Postgres is intentionally NOT stopped — PG is independently managed.
        """
        if self._registry is not None and self._supervisor is not None:
            rec = self._supervisor.record
            if rec is not None:
                # RDR-151 P1.3: publish shutdown marker BEFORE tearing down the
                # process so discoverers stop resolving us immediately.
                with contextlib.suppress(Exception):
                    self._registry.mark_shutting_down(rec)
                self._registry.relinquish(rec)

        self._stop_service()
        self._supervisor = None
        self._registry = None
        self._proc = None


# ── Module-level start / stop / run functions ──────────────────────────────────


def _load_credentials(config_dir: Path) -> dict[str, str]:
    """Read pg_credentials; raise StorageServiceStartError if absent."""
    from nexus.db.pg_provision import CREDENTIALS_FILENAME
    creds_path = config_dir / CREDENTIALS_FILENAME
    if not creds_path.exists():
        raise StorageServiceStartError(
            f"pg_credentials not found at {creds_path}. "
            "Run 'nx init --service' to provision Postgres and write credentials."
        )
    creds = _read_pg_credentials(creds_path)
    # Self-heal upgraded clusters (gmiaf.32.5): a cluster provisioned before the
    # persistent root token existed has no NX_SERVICE_TOKEN in its credentials. Backfill
    # one here so the supervisor starts cleanly on upgrade rather than hard-failing in
    # _resolve_service_token. Idempotent (no-op if already present).
    if not creds.get("NX_SERVICE_TOKEN"):
        import secrets
        from nexus.db.pg_provision import _persist_service_token
        _persist_service_token(creds_path, secrets.token_hex(32))
        creds = _read_pg_credentials(creds_path)
    return creds


def _require_service_binary(config_dir: Path) -> Path:
    """Resolve the native service binary or fail loud (RDR-161 native-only).

    There is no legacy JVM fallback: a missing binary means the service was
    never acquired. Direct the operator at the install verb.
    """
    binary_path = _find_service_binary(config_dir)
    if binary_path is None:
        raise StorageServiceStartError(
            "No nexus-service native binary found. Acquire one:\n"
            "  nx daemon service install-binary <engine-service tag>\n"
            "  (installs to the well-known location under the config dir),\n"
            "or point NEXUS_SERVICE_BIN at a built native binary."
        )
    return binary_path


def start_storage_service(
    *,
    config_dir: Path | None = None,
) -> dict[str, Any]:
    """Ensure the storage service is running; return the discovery payload.

    1. Reads ``pg_credentials`` to get PG_PORT + DB env vars.
    2. Resolves the native service binary (RDR-161: the sole launch artifact).
    3. Starts the supervisor (idempotent on a live lease).

    Raises :class:`StorageServiceStartError` loudly on any failure.
    """
    if config_dir is None:
        from nexus.config import nexus_config_dir
        config_dir = nexus_config_dir()

    creds = _load_credentials(config_dir)
    pg_port_str = creds.get("PG_PORT", "")
    if not pg_port_str.isdigit():
        raise StorageServiceStartError(
            f"PG_PORT in pg_credentials is not a valid integer: {pg_port_str!r}. "
            "Re-run 'nx init --service'."
        )
    pg_port = int(pg_port_str)

    binary_path = _require_service_binary(config_dir)

    sup = StorageServiceSupervisor(
        config_dir=config_dir,
        binary_path=binary_path,
        pg_port=pg_port,
        service_port=0,  # allocated inside start()
        creds=creds,
    )
    return sup.start()


def run_storage_supervisor(
    *,
    config_dir: Path | None = None,
) -> int:
    """Blocking long-lived storage-service supervisor (the ``--foreground`` path).

    Starts the service, publishes the lease, then heartbeats it every
    ``DEFAULT_HEARTBEAT_INTERVAL`` while the process is alive. On SIGTERM/SIGINT
    it gracefully shuts down.

    PG-only failure: when heartbeat_once() returns (True, False), the run loop
    calls _ensure_pg_running() directly — a PG restart without a service respawn.

    Simultaneous service+PG death (False, False): PG is restarted FIRST, then the
    service respawns (nexus-14k0m) — a respawn against a dead PG can never pass
    /health and would burn the restart budget with no PG attempt. If PG is
    unrecoverable the supervisor exits 4 without consuming respawn budget.

    Service failure or stuck process: when heartbeat_once() returns (False, _),
    auto-restart up to _MAX_RESTART_ATTEMPTS times in the current window.
    The (False, _) signal is raised both when the service process exits AND when
    it is alive but /health has returned non-200 for _MAX_UNHEALTHY_HEARTBEATS
    consecutive beats (stuck process). On exhaustion, returns non-zero so the
    process supervisor (launchd / systemd) can restart the *supervisor process*.
    Returns the intended process exit code.
    """
    if config_dir is None:
        from nexus.config import nexus_config_dir
        config_dir = nexus_config_dir()

    # nexus-ovbr7: route structlog to <config_dir>/logs/storage_service.log
    # (mirrors run_t2_daemon / nexus-n8sbw). The detached spawn DEVNULLs
    # stderr, so without this file sink every lifecycle event below —
    # including the restart-exhausted and crash paths — was invisible and
    # four supervisor deaths went undiagnosed.
    from nexus.logging_setup import configure_logging, flush_logging

    configure_logging("storage_service", config_dir=config_dir)

    # Register signal handlers BEFORE start() so a SIGTERM during startup
    # leads to a clean stop() rather than orphaning the service.
    stop_requested = threading.Event()

    def _on_signal(_signum: int, _frame: Any) -> None:
        stop_requested.set()

    signal.signal(signal.SIGTERM, _on_signal)
    signal.signal(signal.SIGINT, _on_signal)

    creds = _load_credentials(config_dir)
    pg_port_str = creds.get("PG_PORT", "")
    if not pg_port_str.isdigit():
        raise StorageServiceStartError(
            f"PG_PORT in pg_credentials is not a valid integer: {pg_port_str!r}."
        )
    pg_port = int(pg_port_str)

    binary_path = _require_service_binary(config_dir)

    _log.info(
        "storage_service_supervisor_started",
        pid=os.getpid(),
        artifact=str(binary_path),
        pg_port=pg_port,
        config_dir=str(config_dir),
    )

    sup = StorageServiceSupervisor(
        config_dir=config_dir,
        binary_path=binary_path,
        pg_port=pg_port,
        service_port=0,
        creds=creds,
        supervised=True,
    )
    try:
        return _supervise_until_stopped(sup, stop_requested, flush_logging)
    except Exception:
        # Last-resort backstop (t2_daemon precedent): an exception escaping
        # the supervisor loop must hit the log file, not a DEVNULL'd stderr —
        # an unlogged supervisor death is the exact defect this fixes.
        _log.exception("storage_service_supervisor_crashed")
        flush_logging()
        raise


def _supervise_until_stopped(
    sup: StorageServiceSupervisor,
    stop_requested: threading.Event,
    flush_logging: Callable[[], None],
) -> int:
    """The supervise loop body of ``run_storage_supervisor`` (split out so
    the crash backstop wraps start() and the loop uniformly)."""
    sup.start()

    exit_code = 0
    while not stop_requested.is_set():
        service_running, pg_ok = sup.heartbeat_once()

        if not service_running:
            # Service exited (or stuck-process threshold hit) — auto-restart.
            if not pg_ok:
                # nexus-14k0m: simultaneous service+PG death. _respawn() never
                # starts PG, and the `elif not pg_ok` branch below only fires
                # while the service is ALIVE — so without this, the respawned
                # service's /health could never pass and the restart budget
                # burned down with zero pg_ctl attempts. Restart PG FIRST;
                # if PG is unrecoverable, respawning the service is futile —
                # exit 4 (the PG-unrecoverable contract) with budget intact.
                # Covers BOTH routes into service_running=False (process exit
                # hardcodes pg_ok=False; stuck-process carries a live probe) —
                # _ensure_pg_running() re-probes, so a hardcoded False with
                # PG actually up is a cheap no-op, never a false exit 4.
                # Loop invariant: heartbeat_once() returned (False, False)
                # with _proc/_supervisor still set (stop() only runs after
                # the loop) — do not call sup.stop() mid-loop.
                _log.warning(
                    "storage_service_and_pg_died",
                    msg="service process and PG both down; restarting PG before respawn",
                )
                try:
                    sup._ensure_pg_running()
                    _log.info("storage_service_pg_restarted_before_respawn")
                except StorageServiceStartError as exc:
                    _log.error(
                        "storage_service_and_pg_restart_failed",
                        error=str(exc),
                        msg="Could not restart PG; supervisor exiting",
                    )
                    exit_code = 4
                    break
            _log.warning(
                "storage_service_exited",
                msg="service child gone; attempting restart",
            )
            try:
                sup._respawn()
                _log.info("storage_service_restarted_successfully")
            except StorageServiceStartError as exc:
                _log.error(
                    "storage_service_restart_exhausted",
                    error=str(exc),
                    msg="Max restart attempts reached; supervisor exiting",
                )
                exit_code = 3
                break

        elif not pg_ok:
            # PG died independently while the service is still alive — restart
            # PG directly without triggering a service respawn.
            _log.warning(
                "storage_service_pg_died_independently",
                msg="PG unreachable while service alive; attempting PG restart",
            )
            try:
                sup._ensure_pg_running()
                _log.info("storage_service_pg_restarted_independently")
            except StorageServiceStartError as exc:
                _log.error(
                    "storage_service_pg_restart_failed",
                    error=str(exc),
                    msg="Could not restart PG; supervisor exiting",
                )
                exit_code = 4
                break

        time.sleep(DEFAULT_HEARTBEAT_INTERVAL)

    # Exit breadcrumb BEFORE stop(): a death without this line means the
    # supervisor was killed, not that it chose to exit. Flush immediately —
    # stop() can stall, and the breadcrumb is the diagnostic (nexus-61539).
    _log.info(
        "storage_service_supervisor_exit",
        exit_code=exit_code,
        stop_requested=stop_requested.is_set(),
    )
    flush_logging()
    sup.stop()
    return exit_code


def stop_storage_service(*, config_dir: Path | None = None) -> int | None:
    """Send SIGTERM to the running storage-service supervisor.

    Returns the supervisor PID that was signalled, or ``None`` if no live
    lease is found (already stopped).

    Freshness gate (mirrors stop_t3_daemon CRITICAL P3 guard): only trust
    ``supervisor_pid`` from the lease payload when ``registry.discover()``
    returns a fresh record (TTL-live). If a SIGKILL'd supervisor left a
    stale lease, the kernel may have recycled its pid to an unrelated process;
    trusting that pid would SIGTERM the wrong process. Since
    ``ServiceRegistry.discover()`` already reaps expired leases (returning
    None for stale ones), a non-None return is the freshness proxy.
    """
    if config_dir is None:
        from nexus.config import nexus_config_dir
        config_dir = nexus_config_dir()

    registry = ServiceRegistry(dir=config_dir, tier=_REGISTRY_TIER)
    scope = str(os.getuid())
    # Freshness gate: discover() reaps stale leases; non-None means live.
    record = registry.discover(scope)
    if record is None:
        _log.info("storage_service_stop_noop", reason="no_live_lease")
        return None

    supervisor_pid = record.payload.get("supervisor_pid")
    pid_to_signal = record.endpoint.get("pid")

    # Only trust supervisor_pid from a FRESH lease (guaranteed above by
    # discover()'s TTL reap), and only when the process is still alive.
    if isinstance(supervisor_pid, int) and supervisor_pid > 0 and _pid_is_alive(supervisor_pid):
        _log.info("storage_service_stopping_supervisor", supervisor_pid=supervisor_pid)
        try:
            os.kill(supervisor_pid, signal.SIGTERM)
        except (ProcessLookupError, PermissionError):
            pass
        deadline = time.monotonic() + _GRACEFUL_STOP_TIMEOUT
        while time.monotonic() < deadline:
            if not _pid_is_alive(supervisor_pid):
                break
            time.sleep(0.1)
        if _pid_is_alive(supervisor_pid):
            try:
                os.kill(supervisor_pid, signal.SIGKILL)
            except (ProcessLookupError, PermissionError):
                pass
        return supervisor_pid

    # No live supervisor: signal the service process group directly.
    if isinstance(pid_to_signal, int) and pid_to_signal > 0:
        from nexus.util.process_group import safe_killpg
        safe_killpg(pid_to_signal, signal.SIGTERM)
        # Clean up the lease record.
        with contextlib.suppress(Exception):
            registry.relinquish(record)
        _log.info("storage_service_stopped", pid=pid_to_signal)
        return pid_to_signal

    _log.info("storage_service_stop_noop", reason="no_live_pid")
    return None
