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

#: Stale-claim reclaim window (RDR-173 P3 / review CRITICAL-2). MUST exceed the
#: ``extract_aspects`` subprocess budget (``aspect_extractor`` runs ``claude -p``
#: with ``timeout=180``) with margin — otherwise the reclaim loop would reset a
#: row this daemon's own worker is ACTIVELY extracting back to ``pending``, a
#: second worker would re-claim it, and the original ``mark_done`` (a DELETE by
#: doc_id with no status guard) would silently cancel the second slot →
#: double-extraction + wasted quota. 300s = 180s budget + 120s margin. The old
#: in-process SQLite default (60s) was safe only because the reclaim shared the
#: worker's process; the multi-process daemon makes the race real.
_DEFAULT_STALE_TIMEOUT_S: int = 300

#: Reclaim SWEEP cadence (s) — how often we ask the service to reclaim, distinct
#: from the staleness threshold above. 30s matches the T2 predecessor
#: (``_ASPECT_RECLAIM_INTERVAL``), so a row that crosses the staleness threshold
#: is reclaimed within ~30s rather than waiting a full threshold-length interval.
_DEFAULT_RECLAIM_INTERVAL: float = 30.0

#: Grace window (s) to catch a daemon child that crashes immediately after spawn
#: (RDR-173 P5). On the store path only at actual spawn time (deduped), so the
#: cost is bounded; long enough for configure_logging + the credential guard to
#: run and exit on the common misconfigurations.
_SPAWN_LIVENESS_GRACE_S: float = 0.15


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


def _default_aspect_queue(tenant: str) -> Any:
    """Build the tenant-scoped service queue handle for the reclaim loop (RF-5).
    Deferred import; the service endpoint resolves via the standard chain."""
    from nexus.db.t2.http_aspect_queue import HttpAspectQueue  # noqa: PLC0415 - deferred; service-mode only

    return HttpAspectQueue(tenant=tenant)


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
        stale_timeout_seconds: int = _DEFAULT_STALE_TIMEOUT_S,
        reclaim_interval: float | None = None,
        queue_factory: Callable[[], Any] | None = None,
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
        # RDR-173 P3 (RF-5): the daemon owns reclaim_stale. TWO decoupled knobs
        # (review M1): _stale_timeout is the STALENESS THRESHOLD (a row must be
        # in_progress longer than this to be reclaimed — kept > the 180s
        # extraction budget to avoid false-reclaiming an in-flight row), while
        # _reclaim_interval is the SWEEP CADENCE (how often we ask the service to
        # reclaim — a frequent fixed 30s, matching the T2 predecessor, so a
        # genuinely-stranded row recovers promptly once it crosses the threshold).
        self._stale_timeout = stale_timeout_seconds
        self._reclaim_interval = (
            float(reclaim_interval) if reclaim_interval is not None else _DEFAULT_RECLAIM_INTERVAL
        )
        self._queue_factory = (
            queue_factory if queue_factory is not None
            else (lambda: _default_aspect_queue(self._tenant))
        )
        self._supervisor: ServiceSupervisor | None = None
        self._worker: Any = None
        self._hb_thread: threading.Thread | None = None
        self._reclaim_queue: Any = None
        self._reclaim_thread: threading.Thread | None = None
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
        # RDR-173 P3 (RF-5): the reclaim owner. Built here (not in __init__) so a
        # failed queue construction surfaces at start, alongside the lease.
        self._reclaim_queue = self._queue_factory()
        self._reclaim_thread = threading.Thread(
            target=self._reclaim_loop,
            name=f"aspect-worker-reclaim-{self._tenant}",
            daemon=True,
        )
        self._reclaim_thread.start()
        _log.info("aspect_worker_daemon.started", tenant=self._tenant, pid=os.getpid())

    def _reclaim_loop(self) -> None:
        """Periodically reset stranded ``in_progress`` rows to ``pending`` so a
        worker death self-heals (RF-5). Per-tenant: the service applies the
        tenant GUC. A transient failure is logged and the loop continues.

        RECLAIM-FIRST, then sleep (review H1, mirroring the T2 predecessor
        nexus-nhqll): a freshly (re)spawned daemon clears the stale-row backlog a
        prior worker death left BEHIND immediately, rather than waiting a full
        interval — in service mode this daemon is the SOLE reclaim owner."""
        while True:
            self._reclaim_once()
            if self._stop.wait(self._reclaim_interval):
                break

    def _reclaim_once(self) -> None:
        """One reclaim sweep (the loop body; also the main-thread test seam).
        Emits a structured signal when rows are reset (RDR-173 P5 observability,
        nexus-xv5fl) so self-healing is observable, and logs a failure without
        killing the loop.

        nexus-64np7: rebuilds ``self._reclaim_queue`` on demand (here, not
        just once at a failure site) so a rotated/expired credential baked
        into the handle at construction time self-heals within one interval
        instead of retrying the same broken client forever — this daemon is
        long-lived and previously held ONE handle for its whole lifetime,
        unlike the claim_batch path (mcp_infra._service_t2_write_locked)
        which already evicts-and-rebuilds on any exception. The
        2026-07-10 incident (401 every 30s for 23+ hours) had no recovery
        short of a manual daemon restart before this fix. Retrying the
        rebuild every call (rather than once at eviction time) also covers
        a rebuild that itself transiently fails (e.g. the service is briefly
        unreachable) — it gets another chance next interval instead of
        going permanently silent.
        """
        if self._reclaim_queue is None:
            try:
                self._reclaim_queue = self._queue_factory()
            except Exception as rebuild_exc:  # noqa: BLE001 - rebuild is best-effort; next interval retries
                _log.warning(
                    "aspect_worker_daemon.reclaim_queue_rebuild_failed",
                    tenant=self._tenant, error=str(rebuild_exc),
                )
                return
        queue = self._reclaim_queue
        try:
            n = queue.reclaim_stale(self._stale_timeout)
            if n:
                _log.info(
                    "aspect_worker_daemon.reclaimed_stale",
                    tenant=self._tenant, count=n, stale_timeout_seconds=self._stale_timeout,
                )
        except Exception as exc:  # noqa: BLE001 - reclaim is best-effort; keep the loop alive for the next interval
            _log.warning(
                "aspect_worker_daemon.reclaim_failed",
                tenant=self._tenant, error=str(exc),
            )
            self._reclaim_queue = None
            try:
                queue.close()
            except Exception as close_exc:  # noqa: BLE001 - best-effort teardown of an already-broken client
                _log.warning(
                    "aspect_worker_daemon.reclaim_queue_close_failed",
                    tenant=self._tenant, error=str(close_exc),
                )

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
        if self._reclaim_thread is not None:
            # 5s (> a normal sub-second reclaim_stale SQL UPDATE) so a reclaim
            # call in flight at shutdown finishes rather than spuriously logging a
            # join timeout (review M2); the loop wakes immediately on _stop.
            self._reclaim_thread.join(timeout=5.0)
            if self._reclaim_thread.is_alive():
                _log.warning("aspect_worker_daemon.reclaim_thread_join_timeout", tenant=self._tenant)
            self._reclaim_thread = None
        # Stop the worker BEFORE the final reclaim sweep so any row it was
        # extracting counts as genuinely-undrained owned work.
        if self._worker is not None:
            try:
                self._worker.stop(timeout=timeout)
            except Exception as exc:  # noqa: BLE001 - worker stop is best-effort during teardown
                _log.warning("aspect_worker_daemon.worker_stop_failed", tenant=self._tenant, error=str(exc))
            self._worker = None
        if self._reclaim_queue is not None:
            # RDR-173 P5 item 3 (review): a final sweep makes the daemon's death
            # OBSERVABLE — the rows it owned but could not finish — AND resets them
            # to pending for the next daemon (recovery in one). reclaim_stale(0):
            # the worker is already stopped, so any in_progress row is abandoned.
            try:
                undrained = self._reclaim_queue.reclaim_stale(0)
                if undrained:
                    _log.warning(
                        "aspect_worker_daemon.stopping_with_undrained_rows",
                        tenant=self._tenant, count=undrained,
                    )
            except Exception as exc:  # noqa: BLE001 - shutdown diagnostic is best-effort
                _log.warning("aspect_worker_daemon.final_reclaim_failed", tenant=self._tenant, error=str(exc))
            try:
                self._reclaim_queue.close()
            except Exception as exc:  # noqa: BLE001 - queue close is best-effort during teardown
                _log.warning("aspect_worker_daemon.reclaim_queue_close_failed", tenant=self._tenant, error=str(exc))
            self._reclaim_queue = None
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
            proc = _popen(
                argv,
                stdin=subprocess.DEVNULL,
                stdout=spawn_log,
                stderr=spawn_log,
                start_new_session=True,
            )
        finally:
            if not isinstance(spawn_log, int):
                spawn_log.close()
        # RDR-173 P5 (review CRITICAL): a successful Popen does NOT mean the
        # daemon is up — the detached child runs _require_extraction_credentials
        # + configure_logging, and the COMMON misconfiguration (claude missing,
        # credential failure, import error) crashes the child immediately while
        # the parent sees a clean return. Poll briefly so that fast crash is
        # LOUD at the spawning (store) process, not only in the child's crash log.
        # Best-effort + racy: a fast crash is caught; a slow one is missed (rarer)
        # and the reclaim loop / next enqueue still recover the work.
        _warn_if_child_died_fast(proc, tenant=tenant)
    return True


def _warn_if_child_died_fast(proc: Any, *, tenant: str) -> None:
    """Emit a LOUD signal if the just-spawned daemon child exited within a short
    grace window (review CRITICAL: spawn-succeeds-child-dies was silent)."""
    poll = getattr(proc, "poll", None)
    if poll is None:
        return  # injected fakes without poll() opt out
    time.sleep(_SPAWN_LIVENESS_GRACE_S)
    rc = poll()
    if rc is not None:
        _log.error(
            "aspect_worker_daemon.spawn_child_died",
            tenant=tenant, returncode=rc,
            hint="daemon exited immediately after spawn — see the aspect_worker_daemon "
                 "crash log under the config logs dir (commonly: claude not on PATH / "
                 "credential failure / import error)",
        )


def run_aspect_worker_daemon(
    *,
    config_dir: Path,
    tenant: str,
    stale_timeout_seconds: int = _DEFAULT_STALE_TIMEOUT_S,
) -> None:
    """Run an aspect-worker daemon to completion (start → serve → stop on signal).

    The spawn entrypoint. MUST be launched as a child of a process that already
    has ``claude -p`` credentials (RDR-173 credential model); it inherits that
    environment. ``stale_timeout_seconds`` is the reclaim staleness threshold —
    keep it above the ``claude -p`` extraction budget (review L3 operator knob).
    """
    from nexus.logging_setup import configure_logging  # noqa: PLC0415 - deferred to avoid circular import at module load

    configure_logging("aspect_worker_daemon", config_dir=config_dir)
    _require_extraction_credentials()
    daemon = AspectWorkerDaemon(
        config_dir=config_dir, tenant=tenant,
        stale_timeout_seconds=stale_timeout_seconds,
    )
    try:
        daemon.start()
        daemon.run_until_signal()
    finally:
        daemon.stop()
