# SPDX-License-Identifier: AGPL-3.0-or-later
"""RDR-152 P5.1 (nexus-gmiaf.30) — Storage-service + Postgres supervisor.

Mirrors ``daemon/t3_daemon.py`` (T3Supervisor / run_t3_supervisor) on the
RDR-149 shared primitive (``ServiceRegistry`` + ``ServiceSupervisor``).

Per ``src/nexus/daemon/AGENTS.md`` (the standing gate), ALL lifecycle
logic lives in the shared primitive, not here. This module:

1. Ensures the nx-managed Postgres cluster is running (reusing
   ``pg_provision._start_cluster``).
2. Starts the Java storage-service JAR (``service/target/nexus-service-*.jar``)
   with the environment variables read from ``pg_credentials``, a free
   ``NX_SERVICE_PORT``, and process-group isolation (``start_new_session=True``
   / ``os.killpg`` on stop).
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
5. Heartbeats the lease while: (a) jar pid is alive, (b) ``/health`` returns
   200, (c) Postgres TCP-reachable. Delegates to ``supervisor.heartbeat_tick()``.
   When PG dies independently (jar still alive), the run loop calls
   ``_ensure_pg_running()`` directly — a PG restart without a full jar respawn.
   When the jar is alive but ``/health`` returns non-200 for
   ``_MAX_UNHEALTHY_HEARTBEATS`` consecutive beats (stuck JVM: connection-pool
   exhaustion, GC deadlock), ``heartbeat_once()`` returns ``(False, pg_ok)``
   to force a respawn — treating a stuck JVM identically to a jar death.
6. ``mark_shutting_down()`` BEFORE ``os.killpg`` (RDR-151 P1.3 ordering).
7. Auto-restarts on jar death with a strictly higher generation (the primitive
   handles generation/fencing). ``_restart_count`` is windowed: reset to 0
   after ``_RESTART_WINDOW_HEARTBEATS`` clean heartbeats so transient clusters
   of failures don't permanently exhaust the budget.
8. LOUD failure when the service stays unreachable: ``StorageServiceStartError``
   (structured log + exception, no silent fall-through).

Postgres lifecycle ownership: the supervisor starts PG on demand but intentionally
does NOT stop PG on ``stop()`` — Postgres is an independently managed process that
may serve other clients. Only the Java JAR process group is managed by the supervisor.

No direct-mode fallback — a service/PG outage is always fatal for callers.
"""
from __future__ import annotations

import contextlib
import errno
import glob
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

#: Consecutive heartbeats where jar is alive but /health returns non-200
#: before triggering a forced respawn. Handles stuck-but-alive JVM states
#: (connection-pool exhaustion, GC pause, internal deadlock) that are the
#: most common Java partial-failure mode. 3 beats at 1s interval = a 3s
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


# ── JAR discovery ──────────────────────────────────────────────────────────────


def _find_service_jar() -> Path:
    """Locate the nexus-service JAR built by Maven.

    Search order:
    1. Explicit ``NEXUS_SERVICE_JAR`` environment variable override (tests).
    2. The canonical Maven output path relative to the repo root.

    Raises :class:`StorageServiceStartError` when no JAR is found.
    """
    env_override = os.environ.get("NEXUS_SERVICE_JAR", "").strip()
    if env_override:
        p = Path(env_override)
        if p.is_file():
            return p
        raise StorageServiceStartError(
            f"NEXUS_SERVICE_JAR is set to {env_override!r} but the file does not "
            "exist. Rebuild the service: "
            "cd service && mvn package -DskipTests -q"
        )

    # Canonical Maven target directory relative to this module's location.
    # Resolve from: src/nexus/daemon/storage_service_daemon.py ->
    # repo_root/service/target/nexus-service-*.jar
    repo_root = Path(__file__).parent.parent.parent.parent
    pattern = str(repo_root / "service" / "target" / "nexus-service-*.jar")
    matches = [p for p in sorted(glob.glob(pattern)) if not p.endswith("-sources.jar")]
    if matches:
        return Path(matches[-1])

    raise StorageServiceStartError(
        "No nexus-service JAR found. Build it first:\n"
        "  cd service && mvn package -DskipTests -q\n"
        f"Expected pattern: {pattern}"
    )


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
    - Heartbeat = (jar pid alive) AND (/health 200) AND (PG TCP reachable),
      then ``supervisor.heartbeat_tick()``.
    - PG-only death (jar still alive): ``_ensure_pg_running()`` called
      directly from the run loop without a full jar respawn.
    - ``mark_shutting_down()`` BEFORE ``os.killpg`` (RDR-151 P1.3).
    - Auto-restart on jar death: respawn + republish (higher generation).
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
        jar_path: Path,
        pg_port: int,
        service_port: int,
        creds: dict[str, str],
        lease_clock: Callable[[], float] = time.time,
        supervised: bool = False,
    ) -> None:
        self._config_dir = config_dir
        self._jar_path = jar_path
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
        # Consecutive unhealthy heartbeats counter: jar alive but /health non-200.
        # When this reaches _MAX_UNHEALTHY_HEARTBEATS the run loop treats the
        # stuck JVM like a jar death and triggers _respawn().
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
        """Spawn the Java JAR with env vars, returning (proc, port)."""
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
        # NX_CHROMA_PATH: point service at the same chroma data the T3 daemon manages.
        if "NX_CHROMA_PATH" not in env:
            try:
                from nexus.config import _default_local_path
                env["NX_CHROMA_PATH"] = str(_default_local_path())
            except Exception:
                pass
        # Use the stable token so clients don't get 401 after a restart.
        env["NX_SERVICE_TOKEN"] = self._service_token

        java_bin = self._find_java()
        proc = subprocess.Popen(
            [java_bin, "-jar", str(self._jar_path)],
            env=env,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
        _log.info(
            "storage_service_spawned",
            pid=proc.pid,
            port=port,
            jar=str(self._jar_path),
        )
        return proc, port

    @staticmethod
    def _find_java() -> str:
        """Locate the java binary. Prefers JAVA_HOME, then PATH."""
        java_home = os.environ.get("JAVA_HOME", "")
        if java_home:
            candidate = Path(java_home) / "bin" / "java"
            if candidate.is_file():
                return str(candidate)
        import shutil
        found = shutil.which("java")
        if found:
            return found
        raise StorageServiceStartError(
            "java binary not found. Set JAVA_HOME or put java on PATH. "
            "The storage service requires a JDK (Java 17+)."
        )

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
        so the JVM can flush), then raise loudly.
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
        # so the JVM can flush open connections and logs before the kill.
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
        """Spawn and re-publish after a jar death or stuck-JVM respawn.

        SIGNIFICANT-2 fix: ``_restart_count`` is windowed — reset to 0 after
        ``_RESTART_WINDOW_HEARTBEATS`` clean heartbeats following a restart.

        ROUND-3 fix: stop the old process group BEFORE spawning the replacement.
        On the natural jar-death path the process is already gone, so
        ``_stop_service()`` is a guarded no-op (it checks ``_pid_is_alive``). On
        the stuck-JVM path (``heartbeat_once`` signals respawn while the jar is
        still physically alive) this is load-bearing: without it the old JVM is
        orphaned, keeps its Postgres connections open, and accumulates one leak
        per respawn cycle. Stopping first also covers the budget-exhausted raise
        path so we never leave a stuck JVM behind when giving up.
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
        """Acquire spawn lock, ensure PG is up, spawn jar, publish lease.

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

        # Step 2: spawn the Java JAR.
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

        Returns (jar_running, pg_ok) so the run loop can handle PG-only failure
        without triggering a jar respawn:

        - (False, _)    — jar exited OR stuck-JVM threshold crossed; caller
                          should call _respawn(). When the jar is physically
                          alive but /health has returned non-200 for
                          _MAX_UNHEALTHY_HEARTBEATS consecutive beats, this
                          method returns (False, pg_ok) to force a respawn —
                          a stuck-but-alive JVM (connection-pool exhaustion,
                          GC deadlock) is treated like a jar death.
        - (True, False) — jar alive and healthy, PG down; caller should call
                          _ensure_pg_running() directly.
        - (True, True)  — everything healthy; lease re-stamped. NOTE: the
                          (True, True) path is the ONLY path that re-stamps
                          the lease. The (True, False) path does NOT re-stamp
                          so the lease ages out via TTL, making the service
                          appear 'down' to discoverers while the jar is alive.

        _consecutive_unhealthy_heartbeats is reset to 0 on any healthy beat
        so transient 503s (GC pause, brief connection spike) do not accumulate
        toward the threshold.
        """
        if self._proc is None or self._supervisor is None:
            return False, False
        if self._proc.poll() is not None:
            return False, False  # jar exited; signal the run loop to respawn

        jar_alive = _pid_is_alive(self._proc.pid)
        service_ok = self._service_healthy()
        pg_ok = self._pg_reachable()

        if not jar_alive:
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
                # Stuck JVM: treat like a jar death so the run loop calls _respawn().
                _log.warning(
                    "storage_service_stuck_jvm_respawn",
                    consecutive_unhealthy=self._consecutive_unhealthy_heartbeats,
                    msg="Stuck JVM threshold reached; signalling respawn",
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


def start_storage_service(
    *,
    config_dir: Path | None = None,
    jar_path: Path | None = None,
) -> dict[str, Any]:
    """Ensure the storage service is running; return the discovery payload.

    1. Reads ``pg_credentials`` to get PG_PORT + DB env vars.
    2. Discovers / builds the JAR path.
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

    if jar_path is None:
        jar_path = _find_service_jar()
    elif not jar_path.is_file():
        raise StorageServiceStartError(
            f"nexus-service JAR not found at {jar_path}. "
            "Build it first: cd service && mvn package -DskipTests -q"
        )

    sup = StorageServiceSupervisor(
        config_dir=config_dir,
        jar_path=jar_path,
        pg_port=pg_port,
        service_port=0,  # allocated inside start()
        creds=creds,
    )
    return sup.start()


def run_storage_supervisor(
    *,
    config_dir: Path | None = None,
    jar_path: Path | None = None,
) -> int:
    """Blocking long-lived storage-service supervisor (the ``--foreground`` path).

    Starts the service, publishes the lease, then heartbeats it every
    ``DEFAULT_HEARTBEAT_INTERVAL`` while the jar is alive. On SIGTERM/SIGINT
    it gracefully shuts down.

    PG-only failure: when heartbeat_once() returns (True, False), the run loop
    calls _ensure_pg_running() directly — a PG restart without a jar respawn.

    Jar failure or stuck JVM: when heartbeat_once() returns (False, _),
    auto-restart up to _MAX_RESTART_ATTEMPTS times in the current window.
    The (False, _) signal is raised both when the jar process exits AND when
    it is alive but /health has returned non-200 for _MAX_UNHEALTHY_HEARTBEATS
    consecutive beats (stuck JVM). On exhaustion, returns non-zero so the
    process supervisor (launchd / systemd) can restart the *supervisor process*.
    Returns the intended process exit code.
    """
    if config_dir is None:
        from nexus.config import nexus_config_dir
        config_dir = nexus_config_dir()

    # Register signal handlers BEFORE start() so a SIGTERM during startup
    # leads to a clean stop() rather than orphaning the jar.
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

    if jar_path is None:
        jar_path = _find_service_jar()

    sup = StorageServiceSupervisor(
        config_dir=config_dir,
        jar_path=jar_path,
        pg_port=pg_port,
        service_port=0,
        creds=creds,
        supervised=True,
    )
    sup.start()

    exit_code = 0
    while not stop_requested.is_set():
        jar_running, pg_ok = sup.heartbeat_once()

        if not jar_running:
            # Jar exited (or stuck-JVM threshold hit) — attempt auto-restart.
            # NOTE: _respawn() does NOT call _ensure_pg_running(); if PG is also
            # down the new jar's /health will keep failing until PG is restarted
            # (next loop's `elif not pg_ok` branch) or the restart budget is
            # exhausted and the supervisor exits.
            _log.warning("storage_service_jar_exited", msg="jar child gone; attempting restart")
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
            # PG died independently while jar is still alive — restart PG
            # directly without triggering a jar respawn.
            _log.warning(
                "storage_service_pg_died_independently",
                msg="PG unreachable while jar alive; attempting PG restart",
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

    # No live supervisor: signal the jar process group directly.
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
