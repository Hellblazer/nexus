# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""RDR-173 Phase 1: the leased aspect-worker daemon.

The aspect-extraction worker is hosted by a long-running process supervised by
the EXISTING RDR-149 leased service-registry substrate (``service_registry.py``)
— the same lease / heartbeat / single-flight discipline that governs the
T1/T2/T3 daemons. This is *not* a new bespoke daemon class; it is one more leased
tier on the unified substrate. Extraction stays Python because it must
(``claude -p``); the Java service cannot host it (RDR-173 RF-6).

Lease scope is PER-TENANT. Per-host would need a daemon claiming across tenants,
which (without the ``nexus.tenant`` GUC) the service's RLS safe-default turns
into zero rows; BYPASSRLS is prohibited for the service role (RDR-152). One
daemon per active tenant, each carrying its tenant's scope, is the only
RLS-compatible model. v1 runs a single ``default`` tenant, so the practical
difference is nil, but the constraint governs every later tenant.

Single-flight: there is no SQLite writer here (unlike T2), so the registry's
per-scope election flock + generation fencing IS the whole election (as for
T1/T3). Two daemons spawned for one tenant both publish; the later wins a higher
generation and the earlier is fenced on its next heartbeat and exits. A brief
double-daemon window is harmless: the service queue claims rows with
``FOR UPDATE SKIP LOCKED``, so two workers never claim the same row.

Credential model (Phase 2 establishes + tests this): the daemon is spawned as a
CHILD of the enqueue-triggering process so it inherits that process's
environment — the ``claude`` binary on ``PATH``, ``~/.claude``, and the
Anthropic credential context those store paths already use for ``claude -p``.
``run_aspect_worker_daemon`` is therefore designed for inherited-env; a
credential-bare spawn path is forbidden.
"""
from __future__ import annotations

import os
import signal
import subprocess
import threading
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

import structlog

from nexus.daemon.service_registry import (
    ServiceRegistry,
    ServiceSupervisor,
    ttl_for_tier,
)

_log = structlog.get_logger(__name__)

#: Registry tier name — the leased tier this daemon registers under.
TIER: str = "aspect_worker"

#: Heartbeat / lease-reassert cadence (s). The registry TTL (``ttl_for_tier``)
#: must be >= this; the substrate enforces that invariant.
_HEARTBEAT_INTERVAL: float = 1.0

#: A factory producing the hosted worker. Injected in tests; the default builds
#: the real :class:`~nexus.aspect_worker.AspectExtractionWorker`.
WorkerFactory = Callable[[], Any]


def _daemon_version() -> str:
    try:
        from importlib.metadata import version  # noqa: PLC0415 - branch-local; deferred to call time

        return version("conexus")
    except Exception:  # noqa: BLE001 - version lookup is best-effort metadata
        return "0+unknown"


def _default_worker_factory() -> Any:
    """Build the real extraction worker. Deferred import keeps the daemon module
    import-light and avoids a cycle through the heavy aspect pipeline."""
    from nexus.aspect_worker import AspectExtractionWorker  # noqa: PLC0415 - deferred; heavy import + avoids cycle

    return AspectExtractionWorker()


class AspectWorkerDaemon:
    """A per-tenant leased host for the aspect-extraction worker loop.

    Mirrors the T1/T3 lease discipline: ``publish_once`` claims the per-tenant
    scope, a heartbeat thread re-asserts the lease and detects fencing, and
    ``stop`` relinquishes gracefully. The hosted worker is started on
    :meth:`start` and stopped on :meth:`stop`.
    """

    def __init__(
        self,
        *,
        config_dir: Path | str,
        tenant: str,
        worker_factory: WorkerFactory = _default_worker_factory,
        clock: Callable[[], float] = time.time,
        heartbeat_interval: float = _HEARTBEAT_INTERVAL,
    ) -> None:
        if not tenant:
            raise ValueError("aspect-worker daemon requires a non-empty tenant (per-tenant scope)")
        # The tenant becomes a discovery-file scope key (``aspect_worker_addr.<tenant>``);
        # reject path-special values before Phase 2 wires arbitrary tenant strings
        # from request context (code-review M3).
        if "/" in tenant or tenant.startswith("."):
            raise ValueError(
                f"invalid tenant {tenant!r}: must not contain '/' or start with '.' "
                "(it is used as a registry scope-key / discovery-file suffix)"
            )
        self._config_dir = Path(config_dir)
        self._tenant = tenant
        self._worker_factory = worker_factory
        self._clock = clock
        self._heartbeat_interval = heartbeat_interval
        self._registry = ServiceRegistry(
            dir=self._config_dir,
            tier=TIER,
            clock=clock,
            ttl=ttl_for_tier(TIER),
        )
        self._supervisor: ServiceSupervisor | None = None
        self._worker: Any = None
        self._hb_thread: threading.Thread | None = None
        self._stop = threading.Event()

    # ── lifecycle ─────────────────────────────────────────────────────────────

    def start(self) -> None:
        """Claim the per-tenant lease, start the hosted worker, and begin
        heartbeating. Idempotent only across distinct instances — call once."""
        endpoint = {"pid": os.getpid()}
        self._supervisor = ServiceSupervisor(
            self._registry,
            scope_key=self._tenant,
            version=_daemon_version(),
            endpoint_provider=lambda: endpoint,
        )
        self._supervisor.publish_once()
        self._worker = self._worker_factory()
        self._worker.start()
        self._stop.clear()
        self._hb_thread = threading.Thread(
            target=self._heartbeat_loop,
            name=f"aspect-worker-hb-{self._tenant}",
            daemon=True,
        )
        self._hb_thread.start()
        _log.info("aspect_worker_daemon.started", tenant=self._tenant, pid=os.getpid())

    def heartbeat_once(self) -> None:
        """Run a single heartbeat tick (test seam + the loop body)."""
        if self._supervisor is not None:
            self._supervisor.heartbeat_tick()

    def is_fenced(self) -> bool:
        """True once a newer-generation owner has fenced this daemon."""
        return bool(self._supervisor is not None and self._supervisor.fenced)

    def _heartbeat_loop(self) -> None:
        while not self._stop.wait(self._heartbeat_interval):
            supervisor = self._supervisor
            if supervisor is None:
                continue
            try:
                supervisor.heartbeat_tick()
                if supervisor.fenced:
                    # A newer daemon won the tenant scope — stand down so exactly
                    # one owner survives (RDR-149 concurrent-one-owner).
                    _log.warning("aspect_worker_daemon.fenced", tenant=self._tenant)
                    self._stop.set()
            except Exception as exc:  # noqa: BLE001 - reassert is best-effort; log + keep trying
                _log.warning(
                    "aspect_worker_daemon.heartbeat_failed",
                    tenant=self._tenant, error=str(exc),
                )

    def run_until_signal(self) -> None:
        """Block until SIGTERM/SIGINT, or until fenced by a newer owner."""
        def _handle(_signum: int, _frame: Any) -> None:
            self._stop.set()

        signal.signal(signal.SIGTERM, _handle)
        signal.signal(signal.SIGINT, _handle)
        self._stop.wait()

    def stop(self, timeout: float = 10.0) -> None:
        """Stop the worker, then relinquish the lease (idempotent)."""
        self._stop.set()
        if self._hb_thread is not None:
            self._hb_thread.join(timeout=2.0)
            if self._hb_thread.is_alive():
                # Contested election flock held the tick past the join window; the
                # thread is a daemon thread (reaped at process exit) but log it so a
                # lingering reassert is not mistaken for a leak (code-review M1).
                _log.warning("aspect_worker_daemon.heartbeat_thread_join_timeout", tenant=self._tenant)
            self._hb_thread = None
        if self._worker is not None:
            try:
                self._worker.stop(timeout=timeout)
            except Exception as exc:  # noqa: BLE001 - worker stop is best-effort during teardown
                _log.warning("aspect_worker_daemon.worker_stop_failed", tenant=self._tenant, error=str(exc))
            self._worker = None
        supervisor = self._supervisor
        if supervisor is not None:
            # A fenced loser does not own the record; mark/relinquish are
            # owner-token-guarded no-ops there, but skip them to avoid noise.
            if not supervisor.fenced and supervisor.record is not None:
                try:
                    self._registry.mark_shutting_down(supervisor.record)
                    self._registry.relinquish(supervisor.record)
                except Exception as exc:  # noqa: BLE001 - relinquish is best-effort
                    _log.warning("aspect_worker_daemon.relinquish_failed", tenant=self._tenant, error=str(exc))
            self._supervisor = None
        _log.info("aspect_worker_daemon.stopped", tenant=self._tenant)


def _require_extraction_credentials() -> None:
    """Fail LOUD at entrypoint if the ``claude`` binary is not on ``PATH``.

    Without it, the daemon would publish its lease, heartbeat healthy, and then
    silently fail extraction per-row inside ``claude -p`` — exactly the silent
    store-time failure RDR-173 exists to eliminate. This is a fast presence
    check, not an API-credential probe; the full inherited-env credential model
    + its test are Phase 2's job. It is the minimum guard so a credential-bare
    invocation refuses to start rather than masquerading as a working daemon.
    """
    import shutil  # noqa: PLC0415 - branch-local; trivial stdlib

    if shutil.which("claude") is None:
        msg = (
            "aspect-worker daemon: the `claude` binary is not on PATH — `claude -p` "
            "extraction would silently fail every row. The daemon must be spawned as a "
            "child of a credential-bearing process (RDR-173 credential model); refusing "
            "to start a daemon that would heartbeat healthy but extract nothing."
        )
        # Route through structlog before raising so the failure is visible in the
        # daemon's configured log stream, not ONLY in the spawn crash file (which
        # may have degraded to DEVNULL) — the parent that Popen'd us cannot see
        # this RuntimeError (review M1).
        _log.error("aspect_worker_daemon.missing_claude_credentials")
        raise RuntimeError(msg)


# Intra-process spawn de-duplication (review M2). A long-lived MCP process
# handling a batch of N concurrent stores would otherwise have every thread
# discover "absent" before the first daemon publishes its lease and each fire a
# Popen — N forks for one daemon. The lock serializes discover→spawn, and the
# suppression window skips re-spawning while a just-spawned daemon comes up
# (cross-process convergence is still the registry's generation fencing).
_spawn_lock = threading.Lock()
_recent_spawn: dict[str, float] = {}  # tenant -> monotonic deadline
_SPAWN_SUPPRESS_WINDOW: float = 10.0


def ensure_aspect_worker_daemon(
    *,
    config_dir: Path | str,
    tenant: str = "default",
    _popen: Callable[..., Any] = subprocess.Popen,
    _clock: Callable[[], float] = time.monotonic,
) -> bool:
    """Spawn-if-absent: ensure a CURRENT-version leased aspect-worker daemon is up
    for *tenant* (RDR-173 P2 / bead nexus-gtdtc). The enqueue-hook replacement for
    the in-process worker thread.

    Discovers the Phase-1 leased tier. If a fresh lease resolves AND its version
    matches this binary, returns without spawning. If the lease is absent OR on a
    STALE version, spawns ``nx daemon aspect-worker start`` — a new daemon claims
    a higher generation and FENCES the stale predecessor (which exits on its next
    heartbeat). This is the upgrade path the Phase-1 version_cycle N/A deferred to
    the spawn authority (review SIG-1: the long-lived daemon would otherwise run
    stale code forever).

    Credential model (nexus-x01oe): the spawn passes NO ``env=`` override, so the
    CHILD inherits this process's environment — ``PATH``, ``~/.claude``, and the
    Anthropic credential context the storing process already uses for ``claude
    -p``. Detached via ``start_new_session`` so the daemon outlives the (often
    short-lived) storing process.

    Returns True if a current-version daemon is up or a spawn was initiated;
    the spawned daemon may not have published its lease yet (the name is
    "ensure", not "running").
    """
    config_dir = Path(config_dir)
    registry = ServiceRegistry(dir=config_dir, tier=TIER, ttl=ttl_for_tier(TIER))
    current = _daemon_version()
    with _spawn_lock:
        rec = registry.discover(tenant)
        if rec is not None and rec.version == current:
            return True  # a current-version daemon already owns this tenant
        # rec is None (absent) OR a stale-version lease (spawn fences it).
        if _recent_spawn.get(tenant, 0.0) > _clock():
            return True  # we spawned within the suppression window; it is coming up
        _recent_spawn[tenant] = _clock() + _SPAWN_SUPPRESS_WINDOW

        from nexus.commands.daemon import _resolve_nx_bin  # noqa: PLC0415 — deferred to break the CLI<->daemon import cycle
        from nexus.logging_setup import open_child_log_or_devnull  # noqa: PLC0415 — deferred; CLI/daemon-only

        argv = [
            *_resolve_nx_bin(), "daemon", "aspect-worker", "start",
            "--config-dir", str(config_dir), "--tenant", tenant,
        ]
        _log.info(
            "aspect_worker_daemon.spawn_if_absent",
            tenant=tenant, argv=argv,
            reason="stale_version" if rec is not None else "absent",
        )
        # Capture the spawn crash channel so the loud credential guard
        # (_require_extraction_credentials) and any pre-logging failure are not
        # lost to DEVNULL. Inherited env: NO env= override.
        spawn_log = open_child_log_or_devnull("aspect_worker_daemon.crash", config_dir)
        try:
            _popen(
                argv,
                stdin=subprocess.DEVNULL,
                stdout=spawn_log,
                stderr=spawn_log,
                start_new_session=True,
            )
        finally:
            if not isinstance(spawn_log, int):
                spawn_log.close()
    return True


def run_aspect_worker_daemon(*, config_dir: Path, tenant: str) -> None:
    """Run an aspect-worker daemon to completion (start → serve → stop on signal).

    The spawn entrypoint. MUST be launched as a child of a process that already
    has ``claude -p`` credentials (RDR-173 credential model); it inherits that
    environment. Phase 2 wires the enqueue-hook spawn + tests the credential
    inheritance.
    """
    from nexus.logging_setup import configure_logging  # noqa: PLC0415 - deferred to avoid circular import at module load

    configure_logging("aspect_worker_daemon", config_dir=config_dir)
    _require_extraction_credentials()
    daemon = AspectWorkerDaemon(config_dir=config_dir, tenant=tenant)
    try:
        daemon.start()
        daemon.run_until_signal()
    finally:
        daemon.stop()
