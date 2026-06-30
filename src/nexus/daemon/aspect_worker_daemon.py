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


def run_aspect_worker_daemon(*, config_dir: Path, tenant: str) -> None:
    """Run an aspect-worker daemon to completion (start → serve → stop on signal).

    The spawn entrypoint. MUST be launched as a child of a process that already
    has ``claude -p`` credentials (RDR-173 credential model); it inherits that
    environment. Phase 2 wires the enqueue-hook spawn + tests the credential
    inheritance.
    """
    from nexus.logging_setup import configure_logging  # noqa: PLC0415 - deferred to avoid circular import at module load

    configure_logging("aspect_worker_daemon", config_dir=config_dir)
    daemon = AspectWorkerDaemon(config_dir=config_dir, tenant=tenant)
    daemon.start()
    try:
        daemon.run_until_signal()
    finally:
        daemon.stop()
