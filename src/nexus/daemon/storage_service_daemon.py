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
   isolation (``start_new_session=True`` / ``os.killpg`` on stop). The
   cosign-verified native binary is the production launch artifact (RDR-161).
   ``NEXUS_SERVICE_JAR`` is an EXPLICIT dev/test opt-in that launches a JVM
   (``java -jar``) instead — never auto-discovered, never a silent fallback,
   and logged loudly as UNVERIFIED (amends RDR-161).
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
   so the run loop exits non-zero — treating a stuck process identically to a
   process death and letting the OS watchdog restart the whole supervisor.
6. ``mark_shutting_down()`` BEFORE ``os.killpg`` (RDR-151 P1.3 ordering).
7. RDR-175: OS init (launchd/systemd, RDR-174) is the single process watchdog.
   On service death OR the stuck-process threshold, the supervisor EXITS non-zero
   (3=service-unrecoverable, 4=PG-unrecoverable) and the OS restarts the whole
   process — there is no in-process respawn mechanism. PG-only death (service
   still alive) is the lone in-place recovery: the run loop restarts PG without
   bouncing the JVM.
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
import re
import shutil
import signal
import socket
import subprocess
import threading
import time
from pathlib import Path
from typing import Any, Callable, Optional

import structlog

from nexus import pdeathsig as _pdeathsig
from nexus.daemon.service_registry import (
    DEFAULT_HEARTBEAT_INTERVAL,
    ServiceRegistry,
    ServiceSupervisor,
    ttl_for_tier,
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

#: PR_SET_PDEATHSIG arming lives in the shared ``nexus.pdeathsig`` primitive so
#: the JVM child (nexus-03bcg: an OOM-killed supervisor leaves NO orphaned-but-
#: serving JVM, no pid-keyed addr file or orphan sweep — both banned by the
#: RDR-149 lifecycle gate) and the aspect-worker's ``claude -p`` child
#: (nexus-4r9ja, RDR-173 RF-8) use ONE implementation. Re-exported under the
#: historical names so the Popen call site below and its tests are unchanged.
_LIBC = _pdeathsig.LIBC
_set_pdeathsig_preexec = _pdeathsig.set_pdeathsig_preexec

#: Path suffix of the spawn lock file inside config_dir.
_SPAWN_LOCK_FILE: str = "storage_service_spawn.lock"

#: How long to wait for the service /health to return 200 before failing.
_READY_TIMEOUT: float = 60.0

#: Interval between /health polls during startup.
_READY_POLL_INTERVAL: float = 0.5

#: After SIGTERM, wait this long before escalating to SIGKILL.
_GRACEFUL_STOP_TIMEOUT: float = 5.0

#: Short HTTP timeout for /health probes.
_HEALTH_TIMEOUT: float = 2.0

#: The storage-service lease TTL is a SUBSTRATE parameter — it lives in the shared
#: primitive (``service_registry.TIER_TTLS["storage_service"]``, resolved via
#: ``ttl_for_tier``), NOT here, per RDR-149 (no tier-specific lifecycle code
#: outside the substrate). nexus-lz3f2: the storage-service heartbeat tick can
#: take up to ``_HEALTH_TIMEOUT`` (2s) + ``DEFAULT_HEARTBEAT_INTERVAL`` (1s) ≈ 3s,
#: grazing the 3s default; the 15s tier override gives ~15 missed-beats of margin.
#: TRADE-OFF (nexus-om64x): this also widens the post-restart stale-endpoint
#: window — the old lease lingers until TTL — from 3s to 15s; the client re-spawn
#: backstop nexus-03bcg is what closes that window for a dead supervisor.

#: Consecutive heartbeats where the process is alive but /health returns
#: non-200 before the supervisor exits non-zero for an OS restart. Handles
#: stuck-but-alive states (connection-pool exhaustion, GC pause, internal
#: deadlock) that are the most common partial-failure mode — and which the OS
#: watchdog (RDR-175) cannot catch on its own, since it only sees process
#: death. 3 beats at 1s interval = a 3s grace window before exit — large
#: enough to absorb transient GC pauses, small enough to recover quickly from
#: real deadlocks. RDR-175 retired the in-process respawn mechanism; this
#: DETECTION is retained but its action is now exit-for-OS-restart, not respawn.
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

    from nexus.daemon.binary_lifecycle import well_known_binary_path  # noqa: PLC0415 — deferred import — platform/heavy dep loaded only on the path that needs it
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


def _find_service_jar() -> Path | None:
    """Resolve an EXPLICIT, opt-in service JAR, or None.

    Dev/test escape hatch amending RDR-161 (which made the cosign-signed native
    binary the sole launch artifact). The JAR is **never auto-discovered and
    never a silent fallback** — it launches ONLY when ``NEXUS_SERVICE_JAR`` is
    set, preserving RDR-161's "never silently mask a missing native binary"
    supply-chain intent. The JAR is NOT signature-verified; the caller logs
    that loudly. Set-but-missing fails loud (an operator who named a JAR that
    does not exist made a mistake worth surfacing).
    """
    override = os.environ.get("NEXUS_SERVICE_JAR", "").strip()
    if not override:
        return None
    p = Path(override)
    if p.is_file():
        return p
    raise StorageServiceStartError(
        f"NEXUS_SERVICE_JAR is set to {override!r} but the file does not exist. "
        "Point it at a built nexus-service-*.jar, or unset it to use the "
        "installed native binary."
    )


def _resolve_java_executable() -> str:
    """Return the ``java`` launcher for the JAR path, or fail loud with a remedy.

    Honours ``JAVA_HOME`` (``$JAVA_HOME/bin/java``) then falls back to ``java``
    on ``PATH``. A JAR launch with no JVM available must fail with an actionable
    message, not a bare ``FileNotFoundError`` from ``Popen``.
    """
    java_home = os.environ.get("JAVA_HOME", "").strip()
    if java_home:
        cand = Path(java_home) / "bin" / "java"
        if cand.is_file() and os.access(cand, os.X_OK):
            return str(cand)
    found = shutil.which("java")
    if found:
        return found
    raise StorageServiceStartError(
        "NEXUS_SERVICE_JAR launch requires a Java runtime, but no 'java' was "
        "found on PATH or via JAVA_HOME. Install a JDK (>= 21), or unset "
        "NEXUS_SERVICE_JAR to use the native binary."
    )


def _resolve_launch_artifact(config_dir: Path) -> tuple[Path, str]:
    """Resolve the service launch artifact and its kind.

    Returns ``(path, kind)`` where kind is ``"jar"`` (explicit ``NEXUS_SERVICE_JAR``
    opt-in, dev/test, UNVERIFIED) or ``"native"`` (the RDR-161 cosign-verified
    binary, the production default). The JAR is checked first ONLY because it is
    an explicit override; with it unset the native binary is the sole path.
    """
    jar = _find_service_jar()
    if jar is not None:
        _log.warning(
            "storage_service_jar_launch",
            jar=str(jar),
            verified=False,
            note="launching the UNVERIFIED dev/test JAR via NEXUS_SERVICE_JAR, "
                 "not the cosign-signed native binary (RDR-161). For production "
                 "use the installed native binary.",
        )
        return jar, "jar"
    binary = _find_service_binary(config_dir)
    if binary is None:
        raise StorageServiceStartError(
            "No nexus-service launch artifact found. Acquire the signed native "
            "binary:\n"
            "  nx daemon service install-binary <engine-service tag>\n"
            "or point NEXUS_SERVICE_BIN at a built native binary. For local "
            "dev/test you may instead set NEXUS_SERVICE_JAR to a built "
            "nexus-service-*.jar (unverified; requires a JVM)."
        )
    return binary, "native"


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
        from importlib.metadata import version  # noqa: PLC0415 — deferred import — platform/heavy dep loaded only on the path that needs it
        return version("conexus")
    except Exception:  # noqa: BLE001 — best-effort version probe; falls back to 0.0.0
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
    - Service death or stuck-process threshold: the supervisor EXITS non-zero
      (RDR-175) and the OS watchdog (RDR-174 launchd/systemd units) restarts the
      whole process. No in-process respawn mechanism.
    - NX_SERVICE_TOKEN included in the lease endpoint so clients can
      re-read the token after an OS restart.
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
        launch_kind: str = "native",
        lease_clock: Callable[[], float] = time.time,
        supervised: bool = False,
    ) -> None:
        # RDR-161: the cosign-verified native binary is the production launch
        # artifact. ``launch_kind="jar"`` is the explicit dev/test opt-in
        # (NEXUS_SERVICE_JAR), launched via the JVM — never an auto-fallback.
        if binary_path is None:
            raise StorageServiceStartError(
                "StorageServiceSupervisor needs a launch artifact; none was "
                "provided. Acquire the native binary via "
                "'nx daemon service install-binary <tag>', or set "
                "NEXUS_SERVICE_JAR for a local dev/test JVM launch."
            )
        if launch_kind not in ("native", "jar"):
            raise StorageServiceStartError(
                f"launch_kind must be 'native' or 'jar', got {launch_kind!r}."
            )
        self._config_dir = config_dir
        self._binary_path = binary_path
        self._launch_kind = launch_kind
        self._svc_log_name = (
            "storage_service_jar" if launch_kind == "jar"
            else "storage_service_native"
        )
        self._pg_port = pg_port
        self._service_port = service_port
        self._creds = creds
        self._lease_clock = lease_clock
        self._supervised = supervised
        self._scope = str(os.getuid())
        self._proc: subprocess.Popen[bytes] | None = None
        self._registry: ServiceRegistry | None = None
        self._supervisor: ServiceSupervisor | None = None
        # Consecutive unhealthy heartbeats counter: process alive but /health
        # non-200. When this reaches _MAX_UNHEALTHY_HEARTBEATS the run loop
        # treats the stuck process like a process death and exits non-zero so
        # the OS watchdog (RDR-175) restarts the whole supervisor.
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
        # nexus-03bcg: arm the Java-side parent-death watchdog so the JVM exits if
        # THIS supervisor dies — the portable (Linux + macOS) orphan-prevention
        # complement to the Linux-only PR_SET_PDEATHSIG preexec below. Only the
        # supervisor-spawned JVM gets this (standalone binary runs do not), so a
        # dead supervisor never leaves an orphaned-but-serving service. NX_SERVICE_BIND
        # (container bind override, default loopback) passes through via env inheritance.
        env["NX_SERVICE_PARENT_DEATH_EXIT"] = "1"

        # nexus-pebfx.2: the service only reads NX_VOYAGE_API_KEY; without it the
        # service embeds local ONNX (RDR-160: bge-768) and refuses every
        # voyage-* collection. Resolve
        # through the nexus credential chain (VOYAGE_API_KEY env > config.yml
        # credentials) so `nx daemon service start` works without manual env
        # plumbing. An explicit NX_VOYAGE_API_KEY in the caller's env wins.
        if not env.get("NX_VOYAGE_API_KEY"):
            from nexus.config import get_credential  # noqa: PLC0415 — deferred import — platform/heavy dep loaded only on the path that needs it
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

        # Launch artifact: native binary (argv[0] = binary) or, when explicitly
        # opted in via NEXUS_SERVICE_JAR, the JVM (argv = java -jar <jar>). The
        # -Xmx option below is accepted by both GraalVM native-image and the JVM.
        if self._launch_kind == "jar":
            java_exe = _resolve_java_executable()
            argv = [java_exe]
        else:
            argv = [str(self._binary_path)]
        # nexus-lz3f2: optional max-heap bound for memory-constrained hosts
        # (e.g. the migration-rehearsal container, where an unbounded native-image
        # heap peak during bge-768 ONNX load + PG + the Python supervisor tripped
        # the cgroup OOM killer — which SIGKILLed the *supervisor*, not the JVM,
        # silently vanishing the lease). GraalVM native-image consumes -Xmx as a
        # runtime option before the app sees argv. Unset by default → no change to
        # production serving; set NX_SERVICE_MAX_HEAP (e.g. "1g") to bound it.
        max_heap = os.environ.get("NX_SERVICE_MAX_HEAP", "").strip()
        if max_heap:
            # Validate before injection: a malformed -Xmx makes the native binary
            # exit immediately, and the supervisor would then blame a /health
            # timeout (pointing at the log, not the env var). Fail loud + actionable.
            if not re.fullmatch(r"\d+[kKmMgG]", max_heap):
                raise StorageServiceStartError(
                    f"NX_SERVICE_MAX_HEAP={max_heap!r} is not a valid JVM heap size "
                    "(expected e.g. '512m', '1g', '2048k')."
                )
            argv.append(f"-Xmx{max_heap}")
        # JVM launch: the runnable artifact follows as `-jar <jar>` (after any
        # -Xmx, which the JVM requires before -jar). Native: argv[0] is already
        # the binary.
        if self._launch_kind == "jar":
            argv += ["-jar", str(self._binary_path)]
        artifact = str(self._binary_path)
        # nexus-ovbr7: route both streams to one file so interleaved output keeps
        # its order; O_APPEND means a respawn never truncates the previous
        # process's final (crash) output. The native binary writes to
        # storage_service_native.log.
        from nexus.logging_setup import open_child_log_or_devnull  # noqa: PLC0415 — deferred import — platform/heavy dep loaded only on the path that needs it

        svc_log = open_child_log_or_devnull(self._svc_log_name, self._config_dir)
        try:
            proc = subprocess.Popen(
                argv,
                env=env,
                stdout=svc_log,
                stderr=svc_log,
                start_new_session=True,
                # nexus-03bcg: die with the supervisor (Linux PR_SET_PDEATHSIG) so
                # an OOM-killed supervisor leaves no orphaned JVM. None off Linux.
                preexec_fn=_set_pdeathsig_preexec if _LIBC is not None else None,
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
            launch_kind=self._launch_kind,
            verified=self._launch_kind == "native",
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
            import urllib.request  # noqa: PLC0415 — deferred import — platform/heavy dep loaded only on the path that needs it
            req = urllib.request.Request(url, method="GET")
            with urllib.request.urlopen(req, timeout=_HEALTH_TIMEOUT) as resp:
                return resp.status == 200
        except Exception:  # noqa: BLE001 — best-effort reachability probe; returns False on any error
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
            from nexus.util.process_group import safe_killpg  # noqa: PLC0415 — deferred import — platform/heavy dep loaded only on the path that needs it
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
            ttl=ttl_for_tier(_REGISTRY_TIER),
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
        from nexus.util.process_group import safe_killpg  # noqa: PLC0415 — deferred import — platform/heavy dep loaded only on the path that needs it

        pid = self._proc.pid
        if _pid_is_alive(pid):
            safe_killpg(pid, signal.SIGTERM)
            deadline = time.monotonic() + _GRACEFUL_STOP_TIMEOUT
            while time.monotonic() < deadline and _pid_is_alive(pid):
                time.sleep(0.1)
            if _pid_is_alive(pid):
                safe_killpg(pid, signal.SIGKILL)
        self._proc = None

    # RDR-175: the in-process respawn mechanism (``_respawn`` + the windowed
    # restart budget ``_maybe_reset_restart_budget``) was retired. OS init
    # (RDR-174 launchd/systemd units) is now the single process watchdog: on
    # service death or the stuck-process threshold the supervise loop exits
    # non-zero and the OS restarts the whole process. The ``(True, False)``
    # PG-only arm restarts PG in place (see ``_supervise_until_stopped``).

    # -- Public lifecycle API -----------------------------------------------

    def start(self) -> dict[str, Any]:
        """Acquire spawn lock, ensure PG is up, spawn service, publish lease.

        Returns the flat discovery payload {host, port, pid, generation, token}.
        Idempotent: a live lease short-circuits without a duplicate spawn.
        Raises :class:`StorageServiceStartError` on failure (LOUD).
        """
        import fcntl  # noqa: PLC0415 — deferred import — platform/heavy dep loaded only on the path that needs it

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
            from nexus.db.pg_provision import discover_pg_binaries, _start_cluster  # noqa: PLC0415 — deferred import — platform/heavy dep loaded only on the path that needs it
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
        failure without exiting the supervisor:

        - (False, _)    — process exited OR stuck-process threshold crossed;
                          the run loop EXITS non-zero so the OS watchdog
                          (RDR-175) restarts the whole supervisor. When the
                          process is physically alive but /health has returned
                          non-200 for _MAX_UNHEALTHY_HEARTBEATS consecutive
                          beats, this method returns (False, pg_ok) to force
                          that exit — a stuck-but-alive process (connection-pool
                          exhaustion, internal deadlock) is treated like a
                          process death (the OS watchdog cannot see it
                          otherwise, since the process never dies).
        - (True, False) — process alive and healthy, PG down; the run loop calls
                          _ensure_pg_running() directly (PG restart in place,
                          no supervisor exit, JVM untouched).
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
            return False, False  # process exited; signal the run loop to exit

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
                # Stuck process: treat like a death so the run loop exits
                # non-zero and the OS watchdog restarts the whole supervisor.
                _log.warning(
                    "storage_service_stuck_exit",
                    consecutive_unhealthy=self._consecutive_unhealthy_heartbeats,
                    msg="Stuck process threshold reached; signalling supervisor exit",
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
    from nexus.db.pg_provision import CREDENTIALS_FILENAME  # noqa: PLC0415 — deferred import — platform/heavy dep loaded only on the path that needs it
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
        import secrets  # noqa: PLC0415 — deferred import — platform/heavy dep loaded only on the path that needs it
        from nexus.db.pg_provision import _persist_service_token  # noqa: PLC0415 — deferred import — platform/heavy dep loaded only on the path that needs it
        _persist_service_token(creds_path, secrets.token_hex(32))
        creds = _read_pg_credentials(creds_path)
    return creds




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
        from nexus.config import nexus_config_dir  # noqa: PLC0415 — deferred import — platform/heavy dep loaded only on the path that needs it
        config_dir = nexus_config_dir()

    creds = _load_credentials(config_dir)
    pg_port_str = creds.get("PG_PORT", "")
    if not pg_port_str.isdigit():
        raise StorageServiceStartError(
            f"PG_PORT in pg_credentials is not a valid integer: {pg_port_str!r}. "
            "Re-run 'nx init --service'."
        )
    pg_port = int(pg_port_str)

    binary_path, launch_kind = _resolve_launch_artifact(config_dir)

    sup = StorageServiceSupervisor(
        config_dir=config_dir,
        binary_path=binary_path,
        launch_kind=launch_kind,
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
    calls _ensure_pg_running() directly — a PG restart in place without bouncing
    the JVM (the lone in-process recovery retained under the OS-watchdog model).

    Service failure or stuck process (RDR-175): when heartbeat_once() returns
    (False, _), the supervisor EXITS non-zero so the OS process watchdog
    (launchd / systemd, RDR-174) restarts the whole supervisor — there is no
    in-process respawn. The (False, _) signal is raised both when the service
    process exits AND when it is alive but /health has returned non-200 for
    _MAX_UNHEALTHY_HEARTBEATS consecutive beats (stuck process). Exit codes:
    3 = service-unrecoverable, 4 = PG-unrecoverable. NOTE (RDR-175): exit 4 is
    emitted ONLY from the (True, False) PG-only arm (PG dies while the service
    is alive). A simultaneous service+PG death exits 3, and if PG is then
    permanently unrecoverable the OS-restart's start() raises
    StorageServiceStartError → the crash backstop re-raises → process exits 1.
    Under StartLimitIntervalSec=0 / KeepAlive all of 1/3/4 trigger an OS
    restart, so this narrowing affects log-based triage only, not recovery.
    Returns the exit code.
    """
    if config_dir is None:
        from nexus.config import nexus_config_dir  # noqa: PLC0415 — deferred import — platform/heavy dep loaded only on the path that needs it
        config_dir = nexus_config_dir()

    # nexus-ovbr7: route structlog to <config_dir>/logs/storage_service.log
    # (mirrors run_t2_daemon / nexus-n8sbw). The detached spawn DEVNULLs
    # stderr, so without this file sink every lifecycle event below —
    # including the restart-exhausted and crash paths — was invisible and
    # four supervisor deaths went undiagnosed.
    from nexus.logging_setup import configure_logging, flush_logging  # noqa: PLC0415 — deferred import — platform/heavy dep loaded only on the path that needs it

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

    binary_path, launch_kind = _resolve_launch_artifact(config_dir)

    _log.info(
        "storage_service_supervisor_started",
        pid=os.getpid(),
        artifact=str(binary_path),
        launch_kind=launch_kind,
        pg_port=pg_port,
        config_dir=str(config_dir),
    )

    sup = StorageServiceSupervisor(
        config_dir=config_dir,
        binary_path=binary_path,
        launch_kind=launch_kind,
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
    the crash backstop wraps start() and the loop uniformly).

    RDR-175: OS init is the single process watchdog. The loop is start →
    heartbeat → die-non-zero. On service death OR the stuck-process threshold
    (``service_running`` falsey) the supervisor exits 3 and the OS restarts the
    whole process (which re-runs ``start()`` — including a fresh PG bring-up),
    instead of an in-process respawn. The lone in-place recovery is the
    ``(True, False)`` PG-only arm: PG is restarted directly while the alive JVM
    keeps running (the OS supervises the supervisor process, not PG)."""
    sup.start()

    exit_code = 0
    while not stop_requested.is_set():
        service_running, pg_ok = sup.heartbeat_once()

        if not service_running:
            # Service process exited OR the stuck-process detection threshold
            # was breached (wedged-but-alive JVM). Under the OS-watchdog model
            # (RDR-175) the supervisor no longer respawns in-process: it exits
            # non-zero so the OS init unit (launchd/systemd) restarts the whole
            # supervisor, which re-runs start() — including a fresh
            # _ensure_pg_running(). A both-down (False, False) beat is covered
            # by the same exit: the OS restart brings PG back up via start().
            # 3 = service-unrecoverable.
            _log.warning(
                "storage_service_exited",
                msg="service child gone or wedged; exiting non-zero for OS restart",
                pg_ok=pg_ok,
            )
            exit_code = 3
            break

        if not pg_ok:
            # PG died independently while the service is still alive — restart
            # PG directly without bouncing the JVM (PRESERVED under the OS
            # watchdog: the OS supervises the supervisor process, not PG).
            # 4 = PG-unrecoverable.
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
        from nexus.config import nexus_config_dir  # noqa: PLC0415 — deferred import — platform/heavy dep loaded only on the path that needs it
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
        from nexus.util.process_group import safe_killpg  # noqa: PLC0415 — deferred import — platform/heavy dep loaded only on the path that needs it
        safe_killpg(pid_to_signal, signal.SIGTERM)
        # Clean up the lease record.
        with contextlib.suppress(Exception):
            registry.relinquish(record)
        _log.info("storage_service_stopped", pid=pid_to_signal)
        return pid_to_signal

    _log.info("storage_service_stop_noop", reason="no_live_pid")
    return None
