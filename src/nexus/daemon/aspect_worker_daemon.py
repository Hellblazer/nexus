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
import re
import shutil
import signal
import subprocess
import sys
import threading
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

import structlog

from nexus import clock as nxclock
from nexus.daemon.service_registry import (
    DEFAULT_HEARTBEAT_INTERVAL,
    HolderStopResult,
    DEFAULT_STOP_ELECTION_BUDGET,
    ServiceRegistry,
    ServiceSupervisor,
    popen_outliving_host_job,
    ttl_for_tier,
)

_log = structlog.get_logger(__name__)

#: Registry tier name — the leased tier this daemon registers under.
TIER: str = "aspect_worker"

#: Heartbeat / lease-reassert cadence (s). The registry TTL (``ttl_for_tier``)
#: must be >= this; the substrate enforces that invariant.
_HEARTBEAT_INTERVAL: float = 1.0

#: Longest single wait on the main thread (s). A CPython ``SIGBREAK`` handler
#: only runs when the main thread executes bytecode, never inside one long
#: ``Event.wait()`` (T2 ``nexus_rdr/224-research-22``), so the main thread waits
#: in ticks no longer than the heartbeat interval, exactly as the storage-service
#: supervisor does (RDR-224, nexus-f9bgu.33).
_STOP_TICK_S: float = DEFAULT_HEARTBEAT_INTERVAL

#: A factory producing the hosted worker. Injected in tests; the default builds
#: the real :class:`~nexus.aspect_worker.AspectExtractionWorker`.
WorkerFactory = Callable[[], Any]

#: How long (s) the engine may go unanswered before the daemon exits
#: (nexus-wl9pk). The clock starts at a failed claim round trip of the hosted
#: worker (``note_engine_failure``) and is cleared by the next answered one
#: (``note_engine_contact``). The daemon outlives
#: ``nx daemon service stop`` by design and idles with the stack down, so the
#: bound has to be far longer than a restart or an upgrade; it is the backstop
#: for a worker whose engine is never coming back (a gate's throwaway service,
#: a deleted install) and whose config directory still exists, which the
#: directory check cannot see. Exiting costs nothing the design does not already
#: accept: the next enqueue spawns a new worker, and a claim of a task this one
#: held lapses with its lease (the engine returns it on the next claim). A failed
#: claim retries within 30 s (``aspect_worker._TUPLE_CLAIM_RETRY_S``), far inside
#: the bound, so a recovered engine resets the clock before the bound. ``None`` or
#: 0 turns the bound off.
_ENGINE_UNREACHABLE_EXIT_S: float = 3600.0


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


def _process_age_ms_from(stat: str, uptime: str, clk_tck: int) -> float | None:
    """Age of the process in ms from ``/proc/<pid>/stat`` and ``/proc/uptime``
    text. Field 22 of stat is the start time in clock ticks since boot; the
    fields are counted after the last ``)`` because comm may hold spaces."""
    try:
        fields = stat[stat.rindex(")") + 2:].split()
        start_ticks = int(fields[19])
        return round((float(uptime.split()[0]) - start_ticks / clk_tck) * 1000, 1)
    except (ValueError, IndexError):
        return None


def _process_age_ms(
    *,
    platform: str | None = None,
    win_info_api: Any = None,
    now: Callable[[], float] = time.time,
) -> float | None:
    """This process's age in ms on Linux, None where /proc is absent (nexus-1m9sb:
    the slow boot was in a Linux container, and the time before start() -- CLI
    startup -- is invisible to start()'s own phase timings).

    Windows reads the creation time through ``GetProcessTimes`` instead
    (RDR-224, nexus-f9bgu.21): same quantity, same unit, None when unreadable.
    """
    if (platform if platform is not None else sys.platform) == "win32":
        from nexus._install import winproc_core  # noqa: PLC0415 — Windows-only path, deferred

        api = win_info_api if win_info_api is not None else winproc_core.ctypes_win_info_api()
        age = winproc_core.process_age_seconds(os.getpid(), api, now=now)
        return None if age is None else round(age * 1000, 1)
    try:
        stat = Path("/proc/self/stat").read_text(encoding="utf-8")
        uptime = Path("/proc/uptime").read_text(encoding="utf-8")
        return _process_age_ms_from(stat, uptime, os.sysconf("SC_CLK_TCK"))
    except (OSError, ValueError):
        return None


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
        engine_unreachable_exit_seconds: float | None = _ENGINE_UNREACHABLE_EXIT_S,
        monotonic: Callable[[], float] = time.monotonic,
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
        # nexus-wl9pk: when the worker's claim round trips began failing with no
        # answer since (monotonic seconds), and whether the daemon is leaving
        # because that outlasted the bound. The worker thread writes, the
        # heartbeat thread reads; a float assignment needs no lock.
        self._engine_unreachable_exit_s = (
            float(engine_unreachable_exit_seconds) if engine_unreachable_exit_seconds else None
        )
        self._monotonic = monotonic
        self._unreachable_since: float | None = None
        self._left_for_unreachable_engine = False

    # ── lifecycle ─────────────────────────────────────────────────────────────

    def start(self) -> None:
        """Claim the per-tenant lease, start the hosted worker, and begin
        heartbeating. Idempotent only across distinct instances — call once.

        nexus-1m9sb: a fullstack-container run measured ~3 minutes between
        this daemon's pid being assigned and ``aspect_worker_daemon.started``
        logging, with nothing in between naming which phase was slow (no
        code-level cause was found — see the module's own investigation
        record). ``t_*`` below time each phase against the injected
        ``self._clock`` so the final log line carries a per-phase
        millisecond breakdown instead of a single opaque timestamp; a
        recurrence is then diagnosable from the log alone.
        """
        t_start = self._clock()
        endpoint = {"pid": os.getpid()}
        self._supervisor = ServiceSupervisor(
            self._registry,
            scope_key=self._tenant,
            version=_daemon_version(),
            endpoint_provider=lambda: endpoint,
            # nexus-wl9pk: a worker whose config directory was deleted under it
            # (a killed gate's scratch dir) stands down; the registry would
            # otherwise re-create the directory on the next heartbeat.
            stand_down_when_dir_gone=True,
        )
        self._supervisor.publish_once()
        t_lease = self._clock()
        self._worker = self._worker_factory()
        t_worker_built = self._clock()
        # nexus-59611 stage 2 (review): the daemon's --config-dir is the one
        # the wake file must be read from; fakes without the hook are untouched.
        bind = getattr(self._worker, "bind_config_dir", None)
        if callable(bind):
            bind(self._config_dir)
        # nexus-yg70j: hand this daemon's stand-down hook to the worker. A
        # worker that detects its own environment is broken must be able to
        # stop the PROCESS -- stopping only its thread leaves this daemon
        # heartbeating a healthy-looking lease while draining nothing, which is
        # the silent-permanent-death shape the bead exists to remove. Attached
        # by duck-typing so the zero-arg worker fakes the injected
        # ``worker_factory`` protocol allows keep working unchanged.
        _attach = getattr(self._worker, "set_self_fault_handler", None)
        if callable(_attach):
            _attach(self._on_worker_self_fault)
        # nexus-wl9pk: the worker reports each claim round trip the engine
        # answered, which clears the unreachable clock, and each one that failed,
        # which starts it.
        _attach_contact = getattr(self._worker, "set_engine_contact_handler", None)
        if callable(_attach_contact):
            _attach_contact(self.note_engine_contact)
        _attach_failure = getattr(self._worker, "set_engine_failure_handler", None)
        if callable(_attach_failure):
            _attach_failure(self.note_engine_failure)
        # Cleared BEFORE the worker starts: the worker thread can report a
        # self fault the instant it runs, and clearing afterwards would erase
        # that stand-down (nexus-yg70j).
        self._stop.clear()
        self._worker.start()
        t_worker_started = self._clock()
        self._hb_thread = threading.Thread(
            target=self._heartbeat_loop,
            name=f"aspect-worker-hb-{self._tenant}",
            daemon=True,
        )
        self._hb_thread.start()
        t_done = self._clock()
        _log.info(
            "aspect_worker_daemon.started",
            tenant=self._tenant, pid=os.getpid(),
            lease_publish_ms=round((t_lease - t_start) * 1000, 1),
            worker_build_ms=round((t_worker_built - t_lease) * 1000, 1),
            worker_start_ms=round((t_worker_started - t_worker_built) * 1000, 1),
            total_startup_ms=round((t_done - t_start) * 1000, 1),
            process_age_ms=_process_age_ms(),
        )

    def _on_worker_self_fault(self, reason: str) -> None:
        """Stand this daemon down because the hosted worker reported that THIS
        PROCESS's environment is broken (nexus-yg70j).

        Setting ``_stop`` releases ``run_until_signal``, whose caller's
        ``finally`` calls :meth:`stop` and relinquishes the tenant lease. The
        next enqueue then spawns a daemon with a fresh environment; the claims
        this worker held are released by :meth:`stop` or lapse with their leases.
        """
        _log.error(
            "aspect_worker_daemon.worker_self_fault",
            tenant=self._tenant, pid=os.getpid(), reason=reason,
            action="relinquishing lease; respawn will get a fresh environment",
        )
        self._stop.set()

    def note_engine_contact(self) -> None:
        """The hosted worker's claim round trip was answered: the engine is
        reachable. Clears the unreachable clock (nexus-wl9pk). Called from the
        worker thread; a float assignment needs no lock."""
        self._unreachable_since = None

    def note_engine_failure(self) -> None:
        """The hosted worker's claim round trip failed: starts the unreachable
        clock if no earlier failure is already running it (nexus-wl9pk). The
        next answered round trip clears it. Called from the worker thread."""
        if self._unreachable_since is None:
            self._unreachable_since = self._monotonic()

    def heartbeat_once(self) -> None:
        """Run a single heartbeat tick (test seam + the loop body)."""
        if self._supervisor is not None:
            self._supervisor.heartbeat_tick()

    def _engine_unreachable_past_bound(self) -> bool:
        """True once every claim round trip since ``_unreachable_since`` failed
        and that has lasted past the bound (nexus-wl9pk)."""
        since = self._unreachable_since
        bound = self._engine_unreachable_exit_s
        return since is not None and bound is not None and self._monotonic() - since >= bound

    def is_fenced(self) -> bool:
        """True once a newer-generation owner has fenced this daemon."""
        return bool(self._supervisor is not None and self._supervisor.fenced)

    def _heartbeat_loop(self) -> None:
        while not nxclock.wait(self._monotonic, self._stop, self._heartbeat_interval):
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
                elif supervisor.orphaned:
                    _log.warning(
                        "aspect_worker_daemon.config_dir_gone",
                        tenant=self._tenant, config_dir=str(self._config_dir),
                        action="exiting: the directory this worker serves was removed",
                    )
                    self._stop.set()
                elif self._engine_unreachable_past_bound():
                    self._left_for_unreachable_engine = True
                    _log.warning(
                        "aspect_worker_daemon.engine_unreachable_exit",
                        tenant=self._tenant,
                        unreachable_for_seconds=round(self._monotonic() - (self._unreachable_since or 0.0), 1),
                        bound_seconds=self._engine_unreachable_exit_s,
                        action="exiting: the next enqueue spawns a worker if the engine returns",
                    )
                    self._stop.set()
            except Exception as exc:  # noqa: BLE001 - reassert is best-effort; log + keep trying
                _log.warning(
                    "aspect_worker_daemon.heartbeat_failed",
                    tenant=self._tenant, error=str(exc),
                )

    def run_until_signal(self, *, signal_module: Any = signal) -> None:
        """Block until SIGTERM/SIGINT/SIGBREAK, or until fenced by a newer owner.

        ``SIGBREAK`` is the Windows stop signal (RDR-224): ``nx`` sends
        ``CTRL_BREAK`` to this process's group through the shared graceful-stop
        primitive, and CPython raises it as ``SIGBREAK``. It sets the same event
        ``SIGTERM`` does, so the stop that follows cannot diverge by platform.
        The wait is a loop of ticks of at most :data:`_STOP_TICK_S` so the
        handler runs promptly. *signal_module* is a test seam.
        """
        def _handle(_signum: int, _frame: Any) -> None:
            self._stop.set()

        signal_module.signal(signal_module.SIGTERM, _handle)
        signal_module.signal(signal_module.SIGINT, _handle)
        sigbreak = getattr(signal_module, "SIGBREAK", None)
        if sigbreak is not None:
            signal_module.signal(sigbreak, _handle)
        while not self._stop.wait(_STOP_TICK_S):
            pass

    def stop(self, timeout: float = 10.0) -> None:
        """Stop the worker, then relinquish the lease (idempotent)."""
        # nexus-cd1k0.6 finding (6): captured once, up front, so the
        # mark/relinquish guard below sees the SAME answer even if a heartbeat
        # tick flips `supervisor.fenced` mid-stop.
        fenced = self.is_fenced()
        self._stop.set()
        if self._hb_thread is not None:
            self._hb_thread.join(timeout=2.0)
            if self._hb_thread.is_alive():
                # Contested election flock held the tick past the join window; the
                # thread is a daemon thread (reaped at process exit) but log it so a
                # lingering reassert is not mistaken for a leak (code-review M1).
                _log.warning("aspect_worker_daemon.heartbeat_thread_join_timeout", tenant=self._tenant)
            self._hb_thread = None
        # Stop the worker first, then release the claims it still holds.
        if self._worker is not None:
            try:
                self._worker.stop(timeout=timeout)
            except Exception as exc:  # noqa: BLE001 - worker stop is best-effort during teardown
                _log.warning("aspect_worker_daemon.worker_stop_failed", tenant=self._tenant, error=str(exc))
            # RDR-235: the stop sweep. A claim on the aspects queue is the
            # engine's, and its lease would run out (an attempt counted)
            # before the next daemon could take the task. Each claim is released
            # by its own claim_id and claimant string, so this runs when FENCED
            # too: the claimant strings are this process's alone (W is unique per
            # live process), and a successor cannot hold them. A worker fake
            # without the hook (the injected-factory protocol) is untouched.
            release = getattr(self._worker, "release_held_claims", None)
            if self._left_for_unreachable_engine:
                # nexus-wl9pk: each release is a request to an engine that has
                # not answered for the whole bound; it would wait out the client
                # timeouts claim by claim. The leases lapse on their own.
                release = None
            if callable(release):
                try:
                    released = release()
                    if released:
                        _log.info(
                            "aspect_worker_daemon.released_tuple_claims",
                            tenant=self._tenant, count=released,
                        )
                except Exception as exc:  # noqa: BLE001 - shutdown sweep is best-effort; the leases lapse otherwise
                    _log.warning(
                        "aspect_worker_daemon.release_tuple_claims_failed",
                        tenant=self._tenant, error=str(exc),
                    )
            self._worker = None
        supervisor = self._supervisor
        if supervisor is not None:
            # A fenced loser does not own the record; mark/relinquish are
            # owner-token-guarded no-ops there, but skip them to avoid noise.
            #
            # nexus-cd1k0.6 review-wave2 follow-up: `fenced` is captured ONCE
            # at the top of this method, so the only question stop() answers
            # is "was this daemon fenced when its shutdown began", not "is it
            # fenced right now at every checkpoint".
            # Safe even when this races the true state: mark_shutting_down
            # /relinquish are already owner-token-guarded no-ops on a
            # record this daemon no longer owns (the comment above), so a
            # captured-False call against an actually-just-fenced record
            # costs nothing beyond a redundant round trip.
            # nexus-wl9pk: an orphaned supervisor has no lease directory to mark
            # or relinquish in, and doing either would re-create it.
            if not fenced and not supervisor.orphaned and supervisor.record is not None:
                try:
                    # nexus-cd1k0 review round 3 finding 6 (sibling of the
                    # storage-service fix): both calls previously took no
                    # budget, an unconditionally blocking flock wait that
                    # could hang this stop() for as long as some other
                    # process held the scope's election lock. Already
                    # best-effort end to end (the enclosing try/except), so
                    # bounding the wait only tightens how quickly a busy
                    # flock degrades to "ages out via TTL instead" —
                    # nothing here changes.
                    self._registry.mark_shutting_down(
                        supervisor.record, budget=DEFAULT_STOP_ELECTION_BUDGET
                    )
                    self._registry.relinquish(
                        supervisor.record, budget=DEFAULT_STOP_ELECTION_BUDGET
                    )
                except Exception as exc:  # noqa: BLE001 - relinquish is best-effort
                    _log.warning("aspect_worker_daemon.relinquish_failed", tenant=self._tenant, error=str(exc))
            self._supervisor = None
        _log.info("aspect_worker_daemon.stopped", tenant=self._tenant)


def aspect_worker_running(config_dir: Path | str, tenant: str = "default") -> bool:
    """Whether a worker holds a fresh lease for *tenant* in *config_dir*.
    ``nx doctor`` uses it to tell a draining backlog from one nobody is draining
    (nexus-wl9pk: a worker leaves after an hour without an engine). Creates
    nothing; it does best-effort delete a lease file that has expired (that is
    ``ServiceRegistry.discover``'s own cleanup, harmless to a live worker, which
    rewrites its lease every heartbeat). Reads this machine's *config_dir* only:
    a worker on another machine is not seen."""
    registry = ServiceRegistry(dir=Path(config_dir), tier=TIER, ttl=ttl_for_tier(TIER))
    return registry.discover(tenant) is not None


def _claude_available() -> bool:
    """Whether an LLM backend can serve the daemon's extraction: the one
    precondition the spawner can check before it forks. Delegates to
    :func:`nexus.operators.llm_backend.llm_backend_available` (nexus-2k66t.3):
    true when ``claude`` is on ``PATH`` (setting ``auto`` or ``cli``), or, with no
    CLI, when an ``anthropic_api_key`` resolves (``auto`` / ``anthropic``) or an
    ``openai_compat`` endpoint is configured. The name stays so the spawner, the
    daemon entrypoint and ``nx doctor`` keep one seam. The PATH lookup is this
    module's own ``shutil.which``, so a test that fakes it here still moves the
    answer."""
    from nexus.operators.llm_backend import llm_backend_available  # noqa: PLC0415 - deferred: keeps daemon/CLI startup light

    return llm_backend_available(which=shutil.which)


def _require_extraction_credentials() -> None:
    """Fail LOUD at entrypoint if no LLM backend is available (no ``claude`` on
    ``PATH`` and no Anthropic key).

    The backstop. The spawner (:func:`ensure_aspect_worker_daemon`) checks the
    same precondition first and does not fork a child that would only refuse;
    this stays for a daemon started by hand or by an older spawner.

    Without it, the daemon would publish its lease, heartbeat healthy, and then
    silently fail extraction per-row inside ``claude -p`` — exactly the silent
    store-time failure RDR-173 exists to eliminate. This is a fast presence
    check, not an API-credential probe; the full inherited-env credential model
    + its test are Phase 2's job. It is the minimum guard so a credential-bare
    invocation refuses to start rather than masquerading as a working daemon.
    """
    if not _claude_available():
        msg = (
            "aspect-worker daemon: no LLM backend is available (the `claude` binary is not on "
            "PATH and no anthropic_api_key is set) — extraction would silently fail every row. "
            "The daemon must be spawned as a "
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
#: Whether this process has already said that it is not spawning because
#: ``claude`` is absent. One debug line per process (``nx doctor`` carries the
#: user-facing warning): on a box without ``claude`` every
#: indexing step and every ``nx store put`` reaches the spawner, and a child per
#: call left 119 refusals and a 400 KB crash log in one measured run.
_claude_absent_logged = False
_SPAWN_SUPPRESS_WINDOW: float = 10.0


def _popen_outliving_host_job(
    popen: Callable[..., Any], argv: list[str], **kwargs: Any,
) -> Any:
    """The aspect worker's spawn: the shared breakaway-then-retry rule (RDR-224
    review finding E), now in ``service_registry.popen_outliving_host_job``
    where the storage supervisor's spawn uses it too (nexus-9pc2f)."""
    return popen_outliving_host_job(popen, argv, **kwargs)


def ensure_aspect_worker_daemon(
    *,
    config_dir: Path | str,
    tenant: str = "default",
    _popen: Callable[..., Any] = subprocess.Popen,
    _clock: Callable[[], float] = time.monotonic,
    _platform: str | None = None,
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
    -p``. Detached so the daemon outlives the (often short-lived) storing
    process: ``start_new_session`` on POSIX; on Windows its own process group and
    no console window (the supervisor's flags, so ``CTRL_BREAK`` can reach it and
    no window opens beside a console-less MCP host; RDR-224, nexus-f9bgu.33).

    Returns True if a current-version daemon is up or a spawn was initiated;
    the spawned daemon may not have published its lease yet (the name is
    "ensure", not "running"). Returns False, spawning nothing, when ``claude`` is
    not on ``PATH``: the child's own credential guard would refuse it, so the
    spawner checks first and notes it once per process at debug level (``nx
    doctor`` reports the idle queue). Enqueued rows are
    unaffected and wait in the service queue for a worker on a box that has
    ``claude``.
    """
    global _claude_absent_logged
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
        if not _claude_available():
            if not _claude_absent_logged:
                _claude_absent_logged = True
                # debug, not warning (nexus-s80u9 N3): every CLI invocation is
                # its own process, so a once-per-process warning printed on
                # every `nx store put` and `nx index repo`. The durable signal
                # is `nx doctor`'s "aspect extraction is idle" WARN, which
                # names the pending count and the remedy.
                _log.debug(
                    "aspect_worker_daemon.spawn_skipped_claude_absent",
                    tenant=tenant,
                    hint="aspect extraction is off: no LLM backend (the `claude` binary is "
                         "not on PATH and no anthropic_api_key is set). Enqueued rows wait "
                         "in the queue until a worker runs on a box that has one.",
                )
            return False
        _recent_spawn[tenant] = _clock() + _SPAWN_SUPPRESS_WINDOW

        from nexus.commands.daemon import (  # noqa: PLC0415 — deferred to break the CLI<->daemon import cycle
            _resolve_nx_bin,
            _supervisor_popen_kwargs,
        )
        from nexus.logging_setup import open_child_log_or_devnull  # noqa: PLC0415 — deferred; CLI/daemon-only

        argv = [
            *_resolve_nx_bin(), "daemon", "aspect-worker", "start",
            "--config-dir", str(config_dir.absolute()), "--tenant", tenant,
        ]  # absolute: the child starts in a stable cwd, not this one (nexus-x3m8e)
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
            # nexus-6y4e0 surveyed this site and left it unwired: this
            # spawn is never killed by pid at all -- the daemon it starts
            # is retired by LEASE FENCING (a newer-generation daemon claims
            # the lease and the stale one exits on its own next heartbeat,
            # per this function's own docstring), not by anything signalling
            # this pid. There is no kill call site for containment to serve.
            proc = _popen_outliving_host_job(
                _popen,
                argv,
                stdin=subprocess.DEVNULL,
                stdout=spawn_log,
                stderr=spawn_log,
                **_supervisor_popen_kwargs(_platform),
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
        # and the next enqueue recovers the work.
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
    engine_unreachable_exit_seconds: float | None = _ENGINE_UNREACHABLE_EXIT_S,
) -> None:
    """Run an aspect-worker daemon to completion (start → serve → stop on signal).

    The spawn entrypoint. MUST be launched as a child of a process that already
    has ``claude -p`` credentials (RDR-173 credential model); it inherits that
    environment.
    """
    import os  # noqa: PLC0415 — branch-local, startup-cost avoidance

    from nexus.logging_setup import configure_logging  # noqa: PLC0415 - deferred to avoid circular import at module load

    configure_logging("aspect_worker_daemon", config_dir=config_dir)

    # nexus-yg70j: DETACH FROM THE LAUNCH DIRECTORY BEFORE ANYTHING ELSE.
    #
    # A long-lived daemon inherits the cwd of whatever spawned it, and this
    # daemon is spawned from an interactive session (RDR-173 requires a parent
    # holding `claude -p` credentials). When that directory is later removed,
    # `os.getcwd()` raises FileNotFoundError inside every subsequent
    # `os.path.abspath()` — and the worker dies PERMANENTLY and SILENTLY: 16
    # consecutive batches raised at WARNING with nothing alerting, zero
    # successes, ~40 minutes on a production install (2026-08-24).
    #
    # This is the NORMAL case here, not an edge case: this project's own
    # guidance routes sessions into `git worktree` checkouts under
    # /private/tmp, so the launch directory is expected to be short-lived.
    # The observed instance was a worktree removed after its branch merged --
    # a correct developer action. The daemon depending on it was the defect.
    #
    # nexus-x3m8e: the rule now lives in the shared primitive, where the
    # supervisor and the MinerU server use it too. The directory is the
    # filesystem root, not config_dir (a process whose cwd is a directory keeps
    # that directory from being deleted on Windows, and config_dir can be
    # relocated by `nx uninstall`); config_dir is made absolute FIRST so the
    # daemon's own paths do not move with the cwd.
    from nexus.daemon.service_registry import enter_stable_daemon_cwd  # noqa: PLC0415 — deferred; daemon-start only

    config_dir = Path(config_dir).absolute()
    enter_stable_daemon_cwd()

    _require_extraction_credentials()
    daemon = AspectWorkerDaemon(
        config_dir=config_dir, tenant=tenant,
        engine_unreachable_exit_seconds=engine_unreachable_exit_seconds,
    )
    try:
        daemon.start()
        daemon.run_until_signal()
    finally:
        daemon.stop()


def is_aspect_worker_command(command: str) -> bool:
    """``python -m nexus.cli daemon aspect-worker start`` (any interpreter): the
    pid-recycle re-check every stop of this tier makes before it signals."""
    return "aspect-worker" in command and "nexus" in command


def _config_dir_value(command: str) -> str | None:
    """The ``--config-dir`` value of a JOINED command line (space or ``=``), or
    ``None``: the text after the flag up to the next ``--option`` or the end.
    The joined text cannot tell a path with spaces from several arguments, so
    the value is the whole run, never a prefix of it: ``/a/b c`` is not
    ``/a/b``. Used where the platform offers no argv (``ps`` on macOS); the
    argv reader (:func:`_config_dir_of_argv`) is exact."""
    match = re.search(r"--config-dir(?:=|\s+)(.*)$", command)
    if match is None:
        return None
    tokens = match.group(1).split()
    run: list[str] = []
    for token in tokens:
        if run and token.startswith("--"):
            break
        run.append(token)
    return " ".join(run).strip("\"'") or None


def _config_dir_of_argv(argv: list[str]) -> str | None:
    """The value of the last ``--config-dir`` (``--config-dir X`` or
    ``--config-dir=X``) in *argv*, as click would read it, else ``None``."""
    value: str | None = None
    for i, arg in enumerate(argv):
        if arg == "--config-dir":
            value = argv[i + 1] if i + 1 < len(argv) else None
        elif arg.startswith("--config-dir="):
            value = arg.partition("=")[2]
    return value or None


def _same_dir(left: str, right: str) -> bool:
    """Same directory by real path (macOS spells a temp dir /var/... and
    /private/var/...), falling back to the lexical spelling."""
    if left == right:
        return True
    return os.path.realpath(left) == os.path.realpath(right)


def is_aspect_worker_command_serving(config_dir: Path | str) -> Callable[[str], bool]:
    """The pid-recycle re-check of :func:`stop_aspect_workers` on a joined
    command line: an aspect-worker command whose ``--config-dir`` is
    *config_dir*. A lease record in directory A that names a pid now running a
    worker of directory B must not stop B (nexus-wl9pk). The spawn always passes
    an absolute ``--config-dir``. Prefer :func:`is_aspect_worker_argv_serving`
    where the argv is readable."""
    want = str(config_dir)

    def _serves(command: str) -> bool:
        value = _config_dir_value(command)
        return is_aspect_worker_command(command) and value is not None and _same_dir(value, want)

    return _serves


def is_aspect_worker_argv_serving(config_dir: Path | str) -> Callable[[list[str]], bool]:
    """The same re-check on an argv list: the ``--config-dir`` element equals
    *config_dir* by real path, so a worker serving ``/a/b c`` is never taken for
    one serving ``/a/b``."""
    want = str(config_dir)

    def _serves(argv: list[str]) -> bool:
        value = _config_dir_of_argv(argv)
        return is_aspect_worker_command(" ".join(argv)) and value is not None and _same_dir(value, want)

    return _serves


def stop_aspect_workers(config_dir: Path | str) -> HolderStopResult:
    """Stop every aspect worker whose lease lives in *config_dir* and drop the
    leases (nexus-wl9pk). The verb behind ``nx daemon aspect-worker stop``.

    A thin call into the shared teardown primitive
    (:func:`~nexus.daemon.service_registry.stop_tier_holders`): the pids come
    from the tier's own lease records, each is re-checked against its live
    command line (an aspect worker serving THIS directory,
    :func:`is_aspect_worker_command_serving`), and the stop is the graceful
    send, the drain grace, then the hard kill. No process-table pattern is
    involved, so a worker serving another config directory is never touched. A
    live pid that fails the re-check is reported in ``foreign`` and its lease
    stays (``keep_foreign_leases``): the stop must not read as done. A gate's
    cleanup calls it BEFORE it stops the service, so the worker's stop sweep
    (releasing its held claims) still has an engine.
    """
    from nexus.daemon.service_registry import stop_tier_holders  # noqa: PLC0415 — deferred; stop-path only

    return stop_tier_holders(
        dir=Path(config_dir), tier=TIER,
        is_ours=is_aspect_worker_command_serving(config_dir),
        is_ours_argv=is_aspect_worker_argv_serving(config_dir),
        keep_foreign_leases=True,
    )
