# SPDX-License-Identifier: AGPL-3.0-or-later
"""Migration-aware readiness state machine (nexus-8vp0i / GH #1486).

The storage-service supervisor (``storage_service_daemon.py``) waits for the
Java engine's ``GET /health`` to return 200 before publishing its lease. The
engine runs Liquibase BEFORE binding HTTP (service ``Main.java``), so
``/health`` is unreachable for the entire duration of a migration — a real
first-boot migration on a large store can run 20-25 minutes (rdr180-001 on
107k chunks). The supervisor's old fixed 60s readiness timeout could not
distinguish "migrating" from "wedged" and killed the engine mid-changeset,
leaving ``public.databasechangeloglock`` stuck ``locked=true`` with a dead
holder — see the bead / GH #1486 for the full incident.

This module is the PURE decision core: given clock/log/pg-probe/health-probe/
process-poll readings, decide which phase the boot is in and whether the
current phase's deadline has been exceeded. It has NO filesystem, subprocess,
or network access of its own — every source of evidence is injected as a
callable, and every side effect (SIGTERM/SIGKILL, stale-lock cleanup,
structured logging) stays in ``storage_service_daemon.py``, which owns the OS.

Phases
------
``PRE_MIGRATION`` -> ``MIGRATING`` -> ``POST_MIGRATION`` -> ``READY``

- ``PRE_MIGRATION``: waiting for the first sign of either Liquibase activity
  (a migration marker) or a healthy ``/health`` (a fast boot with nothing to
  migrate never leaves this phase). Deadline: 60s from the last progress
  event (unchanged from the historical ``_READY_TIMEOUT``).
- ``MIGRATING``: a migration marker has been seen. Deadline: 600s from the
  last progress event when the PG probe is available (``_MIGRATION_STALL_
  TIMEOUT``), 3600s when it is not (``_MIGRATION_UNOBSERVABLE_TIMEOUT`` —
  log lines are the only evidence on a managed/no-psql install).
- ``POST_MIGRATION``: ``event=schema_migration_complete`` seen; the engine is
  now binding HTTP. Deadline: 60s from the last progress event, same as
  ``PRE_MIGRATION``.
- ``READY``: ``/health`` answered 200. Terminal.

"Progress" (what resets a phase's deadline) is any of: a new log line, a PG
probe reporting an executing admin backend, or ANY ``/health`` answer (even a
non-200 one — it proves the process is alive and its HTTP loop is up).

Process exit is fail-fast in every phase — there is no grace period for a
dead process, in any phase, matching the historical behaviour.
"""

from __future__ import annotations

import dataclasses
import re
import time
from collections.abc import Callable
from enum import Enum
from typing import Protocol


# ── Constants ────────────────────────────────────────────────────────────────

#: Deadline (seconds since last progress) outside a migration. Matches the
#: historical ``storage_service_daemon._READY_TIMEOUT``.
DEFAULT_PRE_POST_TIMEOUT: float = 60.0

#: Deadline (seconds since last progress) while MIGRATING, when the PG probe
#: is available (i.e. can positively distinguish "executing" from "stalled").
DEFAULT_MIGRATION_STALL_TIMEOUT: float = 600.0

#: Deadline (seconds since last progress) while MIGRATING, when the PG probe
#: is unavailable (managed PG, no local psql) and log lines are the only
#: evidence. A single silent changeset (rdr180-001: 24 minutes) must fit
#: comfortably inside this window.
DEFAULT_MIGRATION_UNOBSERVABLE_TIMEOUT: float = 3600.0

#: Minimum interval between PG probe invocations (a probe shells out to
#: psql; there is no reason to do that every poll tick).
DEFAULT_PG_PROBE_MIN_INTERVAL: float = 5.0

#: Minimum interval between "migration in progress" progress log lines
#: (structlog, emitted by the caller via the on_tick callback).
PROGRESS_LOG_INTERVAL: float = 30.0

#: Channel provenance (nexus-8vp0i review round 2, substantive-critic
#: finding 4). All engine output — stdout AND stderr — is redirected to one
#: captured log file (nexus-ovbr7), but three DIFFERENT logging channels
#: land there, and only two of the five markers below are load-bearing:
#:
#: - ``MARKER_START`` / ``MARKER_PENDING`` / ``MARKER_COMPLETE`` /
#:   ``MARKER_FAILED`` are the service's OWN ``SchemaMigrator`` SLF4J
#:   ``log.info(...)`` calls, routed through the service's real
#:   logback pipeline. These are the ONLY markers phase transitions
#:   (``ReadinessMonitor``) key on — the actual "wait vs kill" decision.
#: - ``RE_RUNNING_CHANGESET`` matches Liquibase's OWN progress line,
#:   written to its UI output channel — plain stdout, no logback prefix
#:   (the real log shows it as a bare "Running Changeset: ..." line).
#: - ``MARKER_WAITING_FOR_LOCK`` is Liquibase's OWN logger; with no SLF4J
#:   bridge wired into this service (verified against the shipped
#:   liquibase-core jar — no ``liquibase-slf4j`` dependency, no
#:   ``SLF4JBridgeHandler`` installed), it goes through
#:   ``java.util.logging``'s default ``ConsoleHandler`` to stderr, landing
#:   in the same captured file only via that indirect, unconfigured path.
#:
#: Both Liquibase-channel markers are SECONDARY / best-effort: they widen a
#: wait bound and populate a progress line's changeset name, never gate a
#: phase transition on their own. The 600s/3600s PG-probe-backed stall
#: timeout is the real safety net regardless of whether either assumption
#: about JUL's default bootstrap under GraalVM native-image ever breaks.
RE_RUNNING_CHANGESET = re.compile(r"Running Changeset:\s*(\S+)")
RE_PENDING = re.compile(r"event=schema_migration_pending\b.*?changesets=(\d+)")

MARKER_START = "event=schema_migration_start"
MARKER_PENDING = "event=schema_migration_pending"
MARKER_COMPLETE = "event=schema_migration_complete"
MARKER_FAILED = "event=schema_migration_failed"
MARKER_WAITING_FOR_LOCK = "Waiting for changelog lock"


# ── Phase / probe enums ──────────────────────────────────────────────────────


class ReadinessPhase(Enum):
    """Boot phase, in the order they are visited."""

    PRE_MIGRATION = "pre_migration"
    MIGRATING = "migrating"
    POST_MIGRATION = "post_migration"
    READY = "ready"


class PgActivity(Enum):
    """Outcome of one PG probe (``pg_stat_activity`` for an executing admin
    backend on the nexus database)."""

    #: An admin-user backend is ``state='active'`` and NOT waiting on a lock
    #: (CPU, IO, Timeout/pg_sleep all count). Counts as progress.
    EXECUTING = "executing"
    #: The probe ran successfully and found nothing executing. Does NOT
    #: count as progress, but proves the probe IS observable (600s bound).
    IDLE = "idle"
    #: The probe could not run at all (no PG_DATA / no psql / probe error).
    #: The rest of MIGRATING uses the 3600s unobservable bound.
    UNAVAILABLE = "unavailable"


class HealthAnswer(Enum):
    """Outcome of one ``/health`` probe. Mirrors ``storage_service_daemon.
    HealthProbe`` deliberately (this module must not import that one — it
    would create a dependency the wrong way round for a pure primitive)."""

    OK = "ok"
    UNREADY = "unready"
    UNKNOWN = "unknown"


# ── Shared log-marker scanner ────────────────────────────────────────────────


@dataclasses.dataclass
class MigrationLogScanner:
    """Pure, phase-agnostic classifier for engine log lines (nexus-8vp0i
    review round 2, code-review-expert finding 3 / substantive-critic
    finding 5): marker logic lives HERE ONCE, not reimplemented per caller.

    Feed lines in order via :meth:`feed`. ``started`` / ``complete`` /
    ``failed`` are STICKY — once True they never reset, matching how a
    caller consuming a growing log incrementally expects "have we ever seen
    X" to behave. ``changeset`` / ``pending`` hold the MOST RECENT capture.
    ``waiting_for_lock`` is the one non-sticky signal: :meth:`feed` returns
    whether the lock-wait marker was present in THIS batch, since callers
    use it as a fresh-progress/retry signal, not a lifetime flag.

    Shared by :class:`ReadinessMonitor` (phase transitions are keyed off
    ``started`` / ``complete``, gated by the caller's own phase state — a
    concern this scanner does not know about) and the CLI-level
    ``wait_for_registry_lease_migration_aware`` / ``upgrade_finish`` waits
    (which read ``migrating`` directly, with no phase state of their own).
    """

    started: bool = False
    complete: bool = False
    failed: bool = False
    failure_line: str | None = None
    changeset: str | None = None
    pending: int | None = None

    def feed(self, lines: list[str]) -> bool:
        """Feed NEW lines, in order. Returns True iff this batch contained
        the changelog-lock-wait marker."""
        waiting_for_lock = False
        for line in lines:
            if MARKER_FAILED in line:
                self.failed = True
                self.failure_line = line
                continue
            if MARKER_WAITING_FOR_LOCK in line:
                waiting_for_lock = True
            changeset_match = RE_RUNNING_CHANGESET.search(line)
            if (
                MARKER_START in line
                or MARKER_PENDING in line
                or MARKER_WAITING_FOR_LOCK in line
                or changeset_match
            ):
                self.started = True
            if changeset_match:
                self.changeset = changeset_match.group(1)
            if m := RE_PENDING.search(line):
                self.pending = int(m.group(1))
            if MARKER_COMPLETE in line:
                self.complete = True
        return waiting_for_lock

    @property
    def migrating(self) -> bool:
        """Started and not yet complete."""
        return self.started and not self.complete


# ── Injected-evidence protocols ─────────────────────────────────────────────


class LogReader(Protocol):
    def __call__(self) -> list[str]:
        """Return NEW complete log lines observed since the last call.

        Implementations own their own offset/cursor (see
        ``storage_service_daemon._LogTailer``) — a respawn must not read the
        previous process's lines, which is why the offset is recorded at
        spawn time, not derived from file size at first read.
        """


class PgProbe(Protocol):
    def __call__(self) -> PgActivity: ...


class HealthProbe(Protocol):
    def __call__(self) -> HealthAnswer: ...


class ProcessPoll(Protocol):
    def __call__(self) -> int | None:
        """Mirrors ``subprocess.Popen.poll()``: None while running, the
        return code once exited."""


# ── Results / exceptions ─────────────────────────────────────────────────────


@dataclasses.dataclass(frozen=True)
class TickResult:
    """Snapshot returned by :meth:`ReadinessMonitor.tick` on a non-terminal
    (still-waiting) tick. See :class:`ReadinessMonitor` for the terminal
    cases (``ready`` or a raised exception)."""

    ready: bool
    phase: ReadinessPhase
    elapsed_total: float
    elapsed_in_phase: float
    deadline: float
    changeset: str | None
    pending: int | None
    waiting_for_lock: bool
    pg_probe_used: bool

    @property
    def migration_active(self) -> bool:
        return self.phase is ReadinessPhase.MIGRATING


class ReadinessError(Exception):
    """Base for every terminal (fail) outcome of :class:`ReadinessMonitor`."""


class ReadinessProcessExitedError(ReadinessError):
    """The watched process exited before becoming ready, in any phase."""

    def __init__(self, returncode: int | None) -> None:
        self.returncode = returncode
        super().__init__(f"process exited (returncode={returncode})")


class ReadinessStalledError(ReadinessError):
    """A phase's deadline (measured from the last progress event) elapsed
    with no new evidence of forward motion."""

    def __init__(
        self,
        *,
        phase: ReadinessPhase,
        elapsed: float,
        timeout: float,
        changeset: str | None,
        pending: int | None,
    ) -> None:
        self.phase = phase
        self.elapsed = elapsed
        self.timeout = timeout
        self.changeset = changeset
        self.pending = pending
        super().__init__(
            f"stalled in phase={phase.value} after {elapsed:.0f}s "
            f"(timeout={timeout:.0f}s), changeset={changeset!r}, pending={pending!r}"
        )


class ReadinessMigrationFailedError(ReadinessError):
    """``event=schema_migration_failed`` observed in the engine log — a
    definite failure, not a stall; fail fast rather than waiting out the
    phase deadline."""

    def __init__(self, raw_line: str) -> None:
        self.raw_line = raw_line
        super().__init__(f"migration failed: {raw_line!r}")


# ── The state machine ────────────────────────────────────────────────────────


class ReadinessMonitor:
    """Pure migration-aware readiness state machine.

    Every source of evidence is injected; the monitor holds no filesystem,
    subprocess, or network handle of its own. Construct one per boot attempt
    (it is not meant to be reused across a respawn — build a fresh log
    reader too, seeded at the new process's spawn-time log offset).
    """

    def __init__(
        self,
        *,
        clock: Callable[[], float] = time.monotonic,
        log_reader: LogReader,
        pg_probe: PgProbe,
        health_probe: HealthProbe,
        process_poll: ProcessPoll,
        pre_migration_timeout: float = DEFAULT_PRE_POST_TIMEOUT,
        post_migration_timeout: float = DEFAULT_PRE_POST_TIMEOUT,
        migration_stall_timeout: float = DEFAULT_MIGRATION_STALL_TIMEOUT,
        migration_unobservable_timeout: float = DEFAULT_MIGRATION_UNOBSERVABLE_TIMEOUT,
        pg_probe_min_interval: float = DEFAULT_PG_PROBE_MIN_INTERVAL,
    ) -> None:
        self._clock = clock
        self._log_reader = log_reader
        self._pg_probe = pg_probe
        self._health_probe = health_probe
        self._process_poll = process_poll
        self._pre_migration_timeout = pre_migration_timeout
        self._post_migration_timeout = post_migration_timeout
        self._migration_stall_timeout = migration_stall_timeout
        self._migration_unobservable_timeout = migration_unobservable_timeout
        self._pg_probe_min_interval = pg_probe_min_interval

        self._start = clock()
        self._phase = ReadinessPhase.PRE_MIGRATION
        self._phase_entered_at = self._start
        self._last_progress_at = self._start
        # Line-classification lives entirely in the shared scanner (nexus-8vp0i
        # review round 2) — this monitor owns only the PHASE-gating logic
        # (when a scanner-detected transition is honoured) and reads
        # changeset/pending/failure_line straight off it.
        self._scanner = MigrationLogScanner()
        self._pg_probe_unavailable = False
        self._last_pg_probe_at: float | None = None
        self._last_pg_activity: PgActivity | None = None

    # -- Public read-only state --------------------------------------------

    @property
    def phase(self) -> ReadinessPhase:
        return self._phase

    # -- Internal helpers -----------------------------------------------------

    def _current_deadline(self) -> float:
        if self._phase is ReadinessPhase.PRE_MIGRATION:
            return self._pre_migration_timeout
        if self._phase is ReadinessPhase.POST_MIGRATION:
            return self._post_migration_timeout
        # MIGRATING
        if self._pg_probe_unavailable:
            return self._migration_unobservable_timeout
        return self._migration_stall_timeout

    def _mark_progress(self, now: float) -> None:
        self._last_progress_at = now

    def _enter_phase(self, phase: ReadinessPhase, now: float) -> None:
        self._phase = phase
        self._phase_entered_at = now
        self._mark_progress(now)

    def _consume_log_lines(self, lines: list[str], now: float) -> tuple[bool, bool]:
        """Feed NEW log lines to the shared :class:`MigrationLogScanner`,
        then apply the PHASE-gating this monitor owns (line classification
        itself lives entirely in the scanner). Returns (waiting_for_lock,
        saw_failure) — ``saw_failure`` reads the scanner's sticky ``failed``
        flag directly; this is safe because a True value always raises in
        the SAME ``tick()`` call and terminates the wait loop, so no later
        tick ever observes a stale True from an earlier, already-handled
        failure.
        """
        if lines:
            self._mark_progress(now)

        waiting_for_lock = self._scanner.feed(lines)

        if self._phase is ReadinessPhase.PRE_MIGRATION and self._scanner.started:
            self._enter_phase(ReadinessPhase.MIGRATING, now)
        if self._phase is ReadinessPhase.MIGRATING and self._scanner.complete:
            self._enter_phase(ReadinessPhase.POST_MIGRATION, now)

        return waiting_for_lock, self._scanner.failed

    def _consume_pg_probe(self, now: float) -> bool:
        """Run the PG probe if the phase wants it and the throttle allows.
        Returns whether the probe was actually invoked this tick."""
        if self._phase is not ReadinessPhase.MIGRATING:
            return False
        if self._pg_probe_unavailable:
            return False
        if (
            self._last_pg_probe_at is not None
            and (now - self._last_pg_probe_at) < self._pg_probe_min_interval
        ):
            return False
        self._last_pg_probe_at = now
        activity = self._pg_probe()
        self._last_pg_activity = activity
        if activity is PgActivity.UNAVAILABLE:
            self._pg_probe_unavailable = True
            return True
        if activity is PgActivity.EXECUTING:
            self._mark_progress(now)
        return True

    def _consume_health_probe(self, now: float) -> HealthAnswer:
        answer = self._health_probe()
        if answer is not HealthAnswer.UNKNOWN:
            # Any answer (even UNREADY) proves the process is alive and its
            # HTTP loop is responsive — that is progress regardless of phase.
            self._mark_progress(now)
        return answer

    # -- Public API -----------------------------------------------------------

    def tick(self) -> TickResult:
        """Advance the state machine by one step and return the current
        snapshot, or raise a :class:`ReadinessError` subclass on a terminal
        failure (process exit, phase stall, or a migration-failed marker).

        Does not sleep — the caller (or :meth:`wait_ready`) owns pacing.
        """
        now = self._clock()

        rc = self._process_poll()
        if rc is not None:
            raise ReadinessProcessExitedError(rc)

        lines = self._log_reader()
        waiting_for_lock, saw_failure = self._consume_log_lines(lines, now)
        if saw_failure:
            raise ReadinessMigrationFailedError(
                self._scanner.failure_line or "event=schema_migration_failed"
            )

        pg_probe_used = self._consume_pg_probe(now)
        health = self._consume_health_probe(now)
        if health is HealthAnswer.OK:
            self._enter_phase(ReadinessPhase.READY, now)
            return TickResult(
                ready=True,
                phase=self._phase,
                elapsed_total=now - self._start,
                elapsed_in_phase=0.0,
                deadline=0.0,
                changeset=self._scanner.changeset,
                pending=self._scanner.pending,
                waiting_for_lock=waiting_for_lock,
                pg_probe_used=pg_probe_used,
            )

        elapsed_in_phase = now - self._last_progress_at
        deadline = self._current_deadline()
        if elapsed_in_phase >= deadline:
            raise ReadinessStalledError(
                phase=self._phase,
                elapsed=elapsed_in_phase,
                timeout=deadline,
                changeset=self._scanner.changeset,
                pending=self._scanner.pending,
            )

        return TickResult(
            ready=False,
            phase=self._phase,
            elapsed_total=now - self._start,
            elapsed_in_phase=elapsed_in_phase,
            deadline=deadline,
            changeset=self._scanner.changeset,
            pending=self._scanner.pending,
            waiting_for_lock=waiting_for_lock,
            pg_probe_used=pg_probe_used,
        )

    def wait_ready(
        self,
        *,
        sleep: Callable[[float], None] = time.sleep,
        poll_interval: float = 0.5,
        on_tick: Callable[[TickResult], None] | None = None,
    ) -> TickResult:
        """Loop :meth:`tick` until ready or a :class:`ReadinessError` is
        raised. ``sleep`` and ``poll_interval`` are injected so this loop
        itself stays deterministic under a fake clock/sleep in tests.

        ``on_tick`` is the caller's side-effect hook (throttled structlog,
        stale-changelog-lock cleanup on ``waiting_for_lock``) — it never
        affects the state machine's own decisions.
        """
        while True:
            result = self.tick()
            if on_tick is not None:
                on_tick(result)
            if result.ready:
                return result
            sleep(poll_interval)
