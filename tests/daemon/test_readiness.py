# SPDX-License-Identifier: AGPL-3.0-or-later
"""Unit tests for nexus.daemon.readiness.ReadinessMonitor (nexus-8vp0i / GH #1486).

All evidence sources (clock, log reader, pg probe, health probe, process
poll) are fakes under direct control — no real subprocess, filesystem, or
network I/O. See src/nexus/daemon/readiness.py for the state machine this
exercises, and tests/daemon/test_storage_service_daemon.py for the
supervisor-level wiring (log tailer offset-on-respawn, stale-lock cleanup
hookup, structlog throttling) that consumes this module.
"""

from __future__ import annotations

import pytest

from nexus.daemon.readiness import (
    DEFAULT_MIGRATION_STALL_TIMEOUT,
    DEFAULT_MIGRATION_UNOBSERVABLE_TIMEOUT,
    DEFAULT_PRE_POST_TIMEOUT,
    HealthAnswer,
    MigrationLogScanner,
    PgActivity,
    ReadinessMigrationFailedError,
    ReadinessMonitor,
    ReadinessPhase,
    ReadinessProcessExitedError,
    ReadinessStalledError,
    ReadinessStopRequestedError,
)

#: Engine lines as the engine renders them (service logback.xml). A marker counts only as the first token of the
#: message of a line from its own logger (RDR-167, nexus-dngtz.1.11): start/pending/complete from SchemaMigrator,
#: failed from Main at ERROR.
_MIGRATOR = "2026-11-17 02:10:03,114 INFO  [main] dev.nexus.service.db.SchemaMigrator [cid=- tenant=- session=-] - "
_MAIN_ERROR = "2026-11-17 02:10:03,114 ERROR [main] dev.nexus.service.Main [cid=- tenant=- session=-] - "


class _FakeClock:
    """Monotonic-style fake clock: advance() moves time forward explicitly."""

    def __init__(self, start: float = 0.0) -> None:
        self.t = start

    def __call__(self) -> float:
        return self.t

    def advance(self, dt: float) -> None:
        self.t += dt


class _ScriptedLogReader:
    """Returns queued line-batches, one batch per call; [] once exhausted."""

    def __init__(self) -> None:
        self._batches: list[list[str]] = []
        self.call_count = 0

    def queue(self, *lines: str) -> None:
        self._batches.append(list(lines))

    def __call__(self) -> list[str]:
        self.call_count += 1
        if self._batches:
            return self._batches.pop(0)
        return []


class _ScriptedPgProbe:
    def __init__(self, default: PgActivity = PgActivity.IDLE) -> None:
        self._default = default
        self._answers: list[PgActivity] = []
        self.call_count = 0

    def queue(self, activity: PgActivity) -> None:
        self._answers.append(activity)

    def __call__(self) -> PgActivity:
        self.call_count += 1
        if self._answers:
            return self._answers.pop(0)
        return self._default


class _ScriptedHealthProbe:
    def __init__(self, default: HealthAnswer = HealthAnswer.UNKNOWN) -> None:
        self._default = default
        self._answers: list[HealthAnswer] = []
        self.call_count = 0

    def queue(self, answer: HealthAnswer) -> None:
        self._answers.append(answer)

    def __call__(self) -> HealthAnswer:
        self.call_count += 1
        if self._answers:
            return self._answers.pop(0)
        return self._default


class _ScriptedProcessPoll:
    """None (alive) until .kill(returncode) is called."""

    def __init__(self) -> None:
        self._rc: int | None = None

    def kill(self, returncode: int = 137) -> None:
        self._rc = returncode

    def __call__(self) -> int | None:
        return self._rc


def _make_monitor(
    clock: _FakeClock,
    *,
    log_reader=None,
    pg_probe=None,
    health_probe=None,
    process_poll=None,
    **kwargs,
) -> tuple[ReadinessMonitor, _ScriptedLogReader, _ScriptedPgProbe, _ScriptedHealthProbe, _ScriptedProcessPoll]:
    lr = log_reader if log_reader is not None else _ScriptedLogReader()
    pp = pg_probe if pg_probe is not None else _ScriptedPgProbe()
    hp = health_probe if health_probe is not None else _ScriptedHealthProbe()
    poll = process_poll if process_poll is not None else _ScriptedProcessPoll()
    monitor = ReadinessMonitor(
        clock=clock,
        log_reader=lr,
        pg_probe=pp,
        health_probe=hp,
        process_poll=poll,
        **kwargs,
    )
    return monitor, lr, pp, hp, poll


class TestHealthyFastBoot:
    def test_health_ok_on_first_tick_returns_ready(self):
        clock = _FakeClock()
        hp = _ScriptedHealthProbe()
        hp.queue(HealthAnswer.OK)
        monitor, *_ = _make_monitor(clock, health_probe=hp)

        result = monitor.tick()
        assert result.ready is True
        assert monitor.phase is ReadinessPhase.READY

    def test_health_ok_never_visits_migrating(self):
        """No migration marker ever appears; a couple of UNKNOWN health
        answers followed by OK stays in PRE_MIGRATION the whole time."""
        clock = _FakeClock()
        hp = _ScriptedHealthProbe()
        hp.queue(HealthAnswer.UNKNOWN)
        hp.queue(HealthAnswer.UNKNOWN)
        hp.queue(HealthAnswer.OK)
        monitor, *_ = _make_monitor(clock, health_probe=hp)

        r1 = monitor.tick()
        assert r1.ready is False and r1.phase is ReadinessPhase.PRE_MIGRATION
        clock.advance(1.0)
        r2 = monitor.tick()
        assert r2.ready is False and r2.phase is ReadinessPhase.PRE_MIGRATION
        clock.advance(1.0)
        r3 = monitor.tick()
        assert r3.ready is True


class TestMigrationKeptAliveByPgProbe:
    def test_25_minute_silent_changeset_survives_via_pg_probe(self):
        """rdr180-001 shape: one changeset emits NOTHING to the log for its
        entire run. The PG probe reporting EXECUTING every ~5s (throttled)
        must keep resetting the MIGRATING deadline so 25 real minutes does
        not trip the 600s stall bound."""
        clock = _FakeClock()
        lr = _ScriptedLogReader()
        lr.queue(_MIGRATOR + "event=schema_migration_start")
        pg = _ScriptedPgProbe(default=PgActivity.EXECUTING)
        hp = _ScriptedHealthProbe(default=HealthAnswer.UNKNOWN)
        monitor, *_ = _make_monitor(clock, log_reader=lr, pg_probe=pg, health_probe=hp)

        # First tick observes the start marker -> MIGRATING.
        r = monitor.tick()
        assert r.phase is ReadinessPhase.MIGRATING

        # Advance in 5s steps (the probe throttle interval) for 25 minutes.
        # No log lines at all; only the PG probe answers EXECUTING.
        for _ in range(int(25 * 60 / 5)):
            clock.advance(5.0)
            r = monitor.tick()
            assert r.ready is False
            assert r.phase is ReadinessPhase.MIGRATING

        # Now the changeset finishes: log line + health goes OK.
        lr.queue(_MIGRATOR + "event=schema_migration_complete")
        clock.advance(1.0)
        r = monitor.tick()
        assert r.phase is ReadinessPhase.POST_MIGRATION

        hp.queue(HealthAnswer.OK)
        clock.advance(1.0)
        r = monitor.tick()
        assert r.ready is True

    def test_pg_probe_throttled_to_min_interval(self):
        """The probe must not be re-invoked on every tick — only after the
        throttle interval has elapsed."""
        clock = _FakeClock()
        lr = _ScriptedLogReader()
        lr.queue(_MIGRATOR + "event=schema_migration_start")
        pg = _ScriptedPgProbe(default=PgActivity.EXECUTING)
        monitor, _, pg_probe, _, _ = _make_monitor(
            clock, log_reader=lr, pg_probe=pg, pg_probe_min_interval=5.0
        )

        monitor.tick()  # enters MIGRATING, first probe call
        assert pg_probe.call_count == 1

        # Ticking again immediately (0s elapsed) must NOT re-probe.
        for _ in range(10):
            monitor.tick()
        assert pg_probe.call_count == 1

        clock.advance(5.0)
        monitor.tick()
        assert pg_probe.call_count == 2


class TestStallDetection:
    def test_no_progress_stalls_at_600s(self):
        """MIGRATING, PG probe available and IDLE (no executing backend),
        no log lines: must raise ReadinessStalledError once 600s of
        silence elapses, never sooner."""
        clock = _FakeClock()
        lr = _ScriptedLogReader()
        lr.queue(_MIGRATOR + "event=schema_migration_start")
        pg = _ScriptedPgProbe(default=PgActivity.IDLE)
        monitor, *_ = _make_monitor(clock, log_reader=lr, pg_probe=pg)

        monitor.tick()  # -> MIGRATING, progress resets at t=0

        # Just under the stall bound: must not raise.
        clock.advance(DEFAULT_MIGRATION_STALL_TIMEOUT - 1.0)
        r = monitor.tick()
        assert r.ready is False

        # Crossing it: must raise.
        clock.advance(2.0)
        with pytest.raises(ReadinessStalledError) as exc_info:
            monitor.tick()
        assert exc_info.value.phase is ReadinessPhase.MIGRATING

    def test_pre_migration_stalls_at_60s_default(self):
        clock = _FakeClock()
        monitor, *_ = _make_monitor(clock)

        clock.advance(DEFAULT_PRE_POST_TIMEOUT + 1.0)
        with pytest.raises(ReadinessStalledError) as exc_info:
            monitor.tick()
        assert exc_info.value.phase is ReadinessPhase.PRE_MIGRATION
        assert exc_info.value.timeout == DEFAULT_PRE_POST_TIMEOUT


class TestUnobservableProbe:
    def test_waits_up_to_3600s_on_log_lines_only(self):
        """No PG probe available at all (managed PG / no psql): only log
        lines count as progress, and the bound widens to 3600s."""
        clock = _FakeClock()
        lr = _ScriptedLogReader()
        lr.queue(_MIGRATOR + "event=schema_migration_start")
        pg = _ScriptedPgProbe(default=PgActivity.UNAVAILABLE)
        monitor, *_ = _make_monitor(clock, log_reader=lr, pg_probe=pg)

        monitor.tick()  # -> MIGRATING; first probe call reports UNAVAILABLE

        # Well past the 600s stall bound but under 3600s, with periodic log
        # lines keeping progress alive.
        for _ in range(6):
            clock.advance(500.0)
            lr.queue("Running Changeset: some/path::id::author")
            r = monitor.tick()
            assert r.ready is False
            assert r.phase is ReadinessPhase.MIGRATING

    def test_unobservable_probe_still_stalls_at_3600s(self):
        clock = _FakeClock()
        lr = _ScriptedLogReader()
        lr.queue(_MIGRATOR + "event=schema_migration_start")
        pg = _ScriptedPgProbe(default=PgActivity.UNAVAILABLE)
        monitor, *_ = _make_monitor(clock, log_reader=lr, pg_probe=pg)

        monitor.tick()  # -> MIGRATING, UNAVAILABLE observed

        clock.advance(DEFAULT_MIGRATION_UNOBSERVABLE_TIMEOUT - 1.0)
        r = monitor.tick()
        assert r.ready is False

        clock.advance(2.0)
        with pytest.raises(ReadinessStalledError) as exc_info:
            monitor.tick()
        assert exc_info.value.timeout == DEFAULT_MIGRATION_UNOBSERVABLE_TIMEOUT

    def test_probe_unavailable_is_sticky_no_repeated_calls(self):
        """Once UNAVAILABLE is observed, the probe must not be re-invoked
        every tick for the rest of the wait (nothing to gain, and it may be
        an expensive shell-out)."""
        clock = _FakeClock()
        lr = _ScriptedLogReader()
        lr.queue(_MIGRATOR + "event=schema_migration_start")
        pg = _ScriptedPgProbe(default=PgActivity.UNAVAILABLE)
        monitor, _, pg_probe, _, _ = _make_monitor(clock, log_reader=lr, pg_probe=pg)

        monitor.tick()
        assert pg_probe.call_count == 1
        for i in range(20):
            clock.advance(10.0)
            monitor.tick()
        assert pg_probe.call_count == 1


class TestProcessExitFailsFast:
    @pytest.mark.parametrize(
        "phase_setup",
        ["pre_migration", "migrating", "post_migration"],
    )
    def test_exit_raises_immediately_in_any_phase(self, phase_setup):
        clock = _FakeClock()
        lr = _ScriptedLogReader()
        if phase_setup in ("migrating", "post_migration"):
            lr.queue(_MIGRATOR + "event=schema_migration_start")
        if phase_setup == "post_migration":
            lr.queue(_MIGRATOR + "event=schema_migration_complete")
        poll = _ScriptedProcessPoll()
        monitor, *_ = _make_monitor(clock, log_reader=lr, process_poll=poll)

        if phase_setup != "pre_migration":
            monitor.tick()
        if phase_setup == "post_migration":
            monitor.tick()

        poll.kill(returncode=1)
        with pytest.raises(ReadinessProcessExitedError) as exc_info:
            monitor.tick()
        assert exc_info.value.returncode == 1

    def test_exit_takes_priority_over_a_stalled_deadline(self):
        """A process that both stalled AND exited must report exit — it is
        the more specific, more actionable diagnosis."""
        clock = _FakeClock()
        poll = _ScriptedProcessPoll()
        monitor, *_ = _make_monitor(clock, process_poll=poll)

        clock.advance(DEFAULT_PRE_POST_TIMEOUT + 10.0)
        poll.kill(returncode=137)
        with pytest.raises(ReadinessProcessExitedError):
            monitor.tick()


class TestMigrationFailedMarker:
    def test_failed_marker_takes_priority_over_process_exit_same_tick(self):
        """nexus-cd1k0.6 finding (8): an engine that crashes mid-migration
        exits AND leaves the failure marker in its final log lines in the
        SAME tick. The marker is the actionable diagnosis and must win
        over a bare process-exit report, which names only a returncode."""
        clock = _FakeClock()
        lr = _ScriptedLogReader()
        lr.queue(_MIGRATOR + "event=schema_migration_start")
        poll = _ScriptedProcessPoll()
        monitor, *_ = _make_monitor(clock, log_reader=lr, process_poll=poll)
        monitor.tick()  # -> MIGRATING

        lr.queue(_MAIN_ERROR + "event=schema_migration_failed changeset=rdr180-001 error=boom")
        poll.kill(returncode=1)
        with pytest.raises(ReadinessMigrationFailedError) as exc_info:
            monitor.tick()
        assert "schema_migration_failed" in str(exc_info.value)

    def test_failed_marker_raises_immediately_not_a_stall(self):
        clock = _FakeClock()
        lr = _ScriptedLogReader()
        lr.queue(_MIGRATOR + "event=schema_migration_start")
        monitor, *_ = _make_monitor(clock, log_reader=lr)
        monitor.tick()  # -> MIGRATING

        lr.queue(_MAIN_ERROR + "event=schema_migration_failed changeset=foo::bar::baz")
        clock.advance(1.0)  # nowhere near any deadline
        with pytest.raises(ReadinessMigrationFailedError) as exc_info:
            monitor.tick()
        assert "schema_migration_failed" in str(exc_info.value)


class TestWaitingForLockMarker:
    def test_waiting_for_lock_surfaces_on_tick_result(self):
        clock = _FakeClock()
        lr = _ScriptedLogReader()
        lr.queue("Waiting for changelog lock")
        monitor, *_ = _make_monitor(clock, log_reader=lr)

        r = monitor.tick()
        assert r.waiting_for_lock is True
        assert r.phase is ReadinessPhase.MIGRATING  # the marker itself enters MIGRATING

    def test_waiting_for_lock_not_set_when_absent(self):
        clock = _FakeClock()
        lr = _ScriptedLogReader()
        lr.queue("some other benign line")
        monitor, *_ = _make_monitor(clock, log_reader=lr)

        r = monitor.tick()
        assert r.waiting_for_lock is False


class TestChangesetAndPendingTracking:
    def test_running_changeset_and_pending_count_captured(self):
        clock = _FakeClock()
        lr = _ScriptedLogReader()
        lr.queue(
            _MIGRATOR + "event=schema_migration_pending changesets=3",
            "Running Changeset: db/changelog/rdr180-001.xml::rdr180-001::hal",
        )
        monitor, *_ = _make_monitor(clock, log_reader=lr)

        r = monitor.tick()
        assert r.pending == 3
        assert r.changeset == "db/changelog/rdr180-001.xml::rdr180-001::hal"
        assert r.phase is ReadinessPhase.MIGRATING


class TestWaitReadyLoop:
    def test_wait_ready_calls_on_tick_and_sleeps_between_ticks(self):
        clock = _FakeClock()
        hp = _ScriptedHealthProbe()
        hp.queue(HealthAnswer.UNKNOWN)
        hp.queue(HealthAnswer.OK)
        monitor, *_ = _make_monitor(clock, health_probe=hp)

        sleeps: list[float] = []
        ticks: list = []

        def fake_sleep(seconds: float) -> None:
            sleeps.append(seconds)
            clock.advance(seconds)

        result = monitor.wait_ready(
            sleep=fake_sleep, poll_interval=0.5, on_tick=ticks.append
        )
        assert result.ready is True
        assert len(ticks) == 2
        assert sleeps == [0.5]

    def test_wait_ready_propagates_stall_error(self):
        clock = _FakeClock()
        monitor, *_ = _make_monitor(clock)

        def fake_sleep(seconds: float) -> None:
            clock.advance(seconds)

        with pytest.raises(ReadinessStalledError):
            monitor.wait_ready(sleep=fake_sleep, poll_interval=10.0)

    def test_stop_check_raises_before_the_stall_timeout(self):
        """nexus-cd1k0.19: a stop request must be noticed on the VERY NEXT
        tick, not only once some other terminal condition (stall, process
        exit) would eventually fire on its own -- the whole point is that
        a real migration wait can otherwise run unattended for up to
        migration_unobservable_timeout (an hour) with the request sitting
        unread."""
        clock = _FakeClock()
        monitor, *_ = _make_monitor(clock)

        sleeps: list[float] = []

        def fake_sleep(seconds: float) -> None:
            sleeps.append(seconds)
            clock.advance(seconds)

        calls = {"n": 0}

        def stop_check() -> bool:
            calls["n"] += 1
            return calls["n"] >= 3

        with pytest.raises(ReadinessStopRequestedError):
            monitor.wait_ready(
                sleep=fake_sleep, poll_interval=1.0, stop_check=stop_check,
            )
        # Raised on the 3rd tick, well before the 600s default stall
        # timeout would ever fire.
        assert calls["n"] == 3
        assert sum(sleeps) < 10.0

    def test_no_stop_check_behaves_exactly_as_before(self):
        """stop_check=None (the default) must not change behavior at all —
        every existing caller that never passes it is unaffected."""
        clock = _FakeClock()
        hp = _ScriptedHealthProbe()
        hp.queue(HealthAnswer.OK)
        monitor, *_ = _make_monitor(clock, health_probe=hp)
        result = monitor.wait_ready(sleep=lambda _s: None, poll_interval=0.5)
        assert result.ready is True

    def test_on_tick_never_sees_the_stop_check_decision(self):
        """on_tick stays a pure side-effect hook (its own documented
        contract) -- it must not be consulted, or even called, on the tick
        where stop_check fires; the loop must raise BEFORE calling tick()
        at all on that iteration."""
        clock = _FakeClock()
        monitor, *_ = _make_monitor(clock)
        on_tick_calls: list = []
        with pytest.raises(ReadinessStopRequestedError):
            monitor.wait_ready(
                sleep=lambda _s: None,
                on_tick=on_tick_calls.append,
                stop_check=lambda: True,
            )
        assert on_tick_calls == [], (
            "stop_check firing on tick 1 must pre-empt tick()/on_tick "
            "entirely, not race it"
        )


class TestMigrationLogScanner:
    """nexus-8vp0i review round 2 (code-review-expert finding 3 /
    substantive-critic finding 5): marker logic lives in ONE shared,
    public, phase-agnostic scanner — both ReadinessMonitor and the CLI-level
    waits (storage_service_daemon.wait_for_registry_lease_migration_aware,
    upgrade_finish._restart_and_verify) consume it. These tests exercise
    the scanner directly, independent of any phase-gating caller."""

    def test_started_is_sticky_across_feed_calls(self):
        scanner = MigrationLogScanner()
        assert scanner.started is False
        scanner.feed([_MIGRATOR + "event=schema_migration_start"])
        assert scanner.started is True
        scanner.feed([])  # empty batch must not reset it
        assert scanner.started is True

    def test_complete_is_sticky_and_flips_migrating_false(self):
        scanner = MigrationLogScanner()
        scanner.feed([_MIGRATOR + "event=schema_migration_start"])
        assert scanner.migrating is True
        scanner.feed([_MIGRATOR + "event=schema_migration_complete"])
        assert scanner.complete is True
        assert scanner.migrating is False
        scanner.feed([])
        assert scanner.migrating is False  # still sticky-complete

    def test_migrating_false_before_any_start_marker(self):
        scanner = MigrationLogScanner()
        scanner.feed(["some unrelated benign line"])
        assert scanner.migrating is False
        assert scanner.started is False

    def test_running_changeset_line_sets_started_and_changeset(self):
        scanner = MigrationLogScanner()
        scanner.feed(["Running Changeset: db/changelog/x.xml::x-1::hal"])
        assert scanner.started is True
        assert scanner.changeset == "db/changelog/x.xml::x-1::hal"

    def test_pending_count_captured(self):
        scanner = MigrationLogScanner()
        scanner.feed([_MIGRATOR + "event=schema_migration_pending changesets=7"])
        assert scanner.pending == 7
        assert scanner.started is True

    # nexus-dngtz.1.5: the engine's logback line gained ``[cid=.. tenant=.. session=..]`` before `` - ``
    # (RDR-167 Step 3). An engine before that prints no MDC block, and may still be the one running during an
    # upgrade, so both forms are read. Lines taken from tests/fixtures/windows_engine/engine-first-boot.log.
    _OLD_FORMAT = "2026-10-05 13:56:13,157 INFO  [main] dev.nexus.service.db.SchemaMigrator - "
    _NEW_FORMAT = _MIGRATOR
    _REQUEST_LINE = (
        "2026-11-17 02:10:03,114 WARN  [HTTP-Dispatcher] dev.nexus.service.http.AuthFilter "
        "[cid=nxe-0123456789abcdef0123456789abcdef tenant=tenant-a session=session-a1] - "
    )

    def test_pending_line_gives_the_count_in_both_formats(self):
        for prefix in (self._OLD_FORMAT, self._NEW_FORMAT):
            scanner = MigrationLogScanner()
            scanner.feed([prefix + "event=schema_migration_pending changesets=7"])
            assert scanner.pending == 7, prefix
            assert scanner.started is True

    def test_start_and_complete_lines_set_the_flags_in_both_formats(self):
        for prefix in (self._OLD_FORMAT, self._NEW_FORMAT):
            scanner = MigrationLogScanner()
            scanner.feed([prefix + "event=schema_migration_start changelog=db/changelog/db.changelog-master.xml"])
            assert scanner.started is True, prefix
            scanner.feed([prefix + "event=schema_migration_complete new_changesets=508 reexecuted_changesets=0"])
            assert scanner.complete is True, prefix

    # RDR-167, nexus-dngtz.1.11: a client-chosen value printed in an ordinary line must not forge a marker.
    def test_a_marker_inside_a_client_value_is_ignored(self):
        forged = [
            self._REQUEST_LINE + 'event=request_failed path="/v1/x event=schema_migration_failed" status=500',
            self._REQUEST_LINE + 'event=request_failed op="event=schema_migration_complete" status=500',
            _MIGRATOR + 'event=schema_migration_session search_path="event=schema_migration_complete"',
        ]
        scanner = MigrationLogScanner()
        assert scanner.feed(forged) is False
        assert (scanner.started, scanner.complete, scanner.failed, scanner.pending) == (False, False, False, None)

    def test_a_marker_mid_line_or_from_another_logger_is_ignored(self):
        forged = [
            # the marker after other message text
            _MIGRATOR + "event=other x=1 event=schema_migration_start",
            _MAIN_ERROR + "event=request_failed event=schema_migration_failed",
            # the right first token from the wrong logger, or failed at the wrong level
            self._REQUEST_LINE + "event=schema_migration_start",
            self._REQUEST_LINE + "event=schema_migration_pending changesets=9",
            "2026-11-17 02:10:03,114 INFO  [main] dev.nexus.service.Main [cid=- tenant=- session=-] - "
            "event=schema_migration_failed error=x",
            _MIGRATOR.replace("INFO ", "ERROR") + "event=schema_migration_failed error=x",
            # a marker glued to a longer token
            _MIGRATOR + "event=schema_migration_started",
            # not an engine line at all: a bare marker, a quoted one, a forged prefix inside a value
            "event=schema_migration_failed",
            'Caused by: java.lang.IllegalStateException: tenant "event=schema_migration_failed"',
            self._REQUEST_LINE + "x 2026-11-17 02:10:03,114 ERROR [main] dev.nexus.service.Main - event=schema_migration_failed",
            # the Liquibase markers anywhere but the start of a line
            self._REQUEST_LINE + 'path="Running Changeset: a::b::c"',
            self._REQUEST_LINE + 'path="Waiting for changelog lock"',
        ]
        for line in forged:
            scanner = MigrationLogScanner()
            assert scanner.feed([line]) is False, line
            assert (scanner.started, scanner.complete, scanner.failed, scanner.changeset, scanner.pending) == (
                False, False, False, None, None), line

    def test_real_liquibase_lines_still_match(self):
        scanner = MigrationLogScanner()
        assert scanner.feed(["INFO: Waiting for changelog lock...."]) is True
        scanner.feed(["Running Changeset: db/changelog/role-001-nexus-svc.xml::role-001-1::nexus-9qvam"])
        assert scanner.started is True
        assert scanner.changeset == "db/changelog/role-001-nexus-svc.xml::role-001-1::nexus-9qvam"

    def test_the_real_failed_boot_line_matches(self):
        # tests/fixtures/rdr231_halt/engine_native_log_failed_boot.txt, line 14
        line = '2026-10-29 13:56:09,901 ERROR [main] dev.nexus.service.Main - event=schema_migration_failed error="Liquibase migration failed"'
        scanner = MigrationLogScanner()
        scanner.feed([line])
        assert scanner.failed is True and scanner.failure_line == line

    def test_waiting_for_lock_is_not_sticky(self):
        scanner = MigrationLogScanner()
        first = scanner.feed(["Waiting for changelog lock"])
        assert first is True
        assert scanner.started is True  # the marker itself counts as "started"
        second = scanner.feed(["some other line"])
        assert second is False  # not sticky — only reports THIS batch

    def test_failed_marker_is_sticky_and_captures_the_line(self):
        scanner = MigrationLogScanner()
        scanner.feed([_MAIN_ERROR + "event=schema_migration_failed changeset=x::y::z"])
        assert scanner.failed is True
        assert scanner.failure_line == _MAIN_ERROR + "event=schema_migration_failed changeset=x::y::z"
        scanner.feed([])
        assert scanner.failed is True  # still sticky

    def test_public_marker_and_regex_names_importable(self):
        """nexus-8vp0i review round 2: no underscore reach-across — the
        marker constants and regexes must be public names other modules
        (storage_service_daemon.py, upgrade_finish.py) can use directly."""
        from nexus.daemon.readiness import (
            MARKER_COMPLETE,
            MARKER_FAILED,
            MARKER_PENDING,
            MARKER_START,
            MARKER_WAITING_FOR_LOCK,
            RE_PENDING,
            RE_RUNNING_CHANGESET,
        )

        assert MARKER_START == "event=schema_migration_start"
        assert MARKER_PENDING == "event=schema_migration_pending"
        assert MARKER_COMPLETE == "event=schema_migration_complete"
        assert MARKER_FAILED == "event=schema_migration_failed"
        assert MARKER_WAITING_FOR_LOCK == "Waiting for changelog lock"
        assert RE_RUNNING_CHANGESET.search("Running Changeset: a::b::c") is not None
        assert RE_PENDING.search("event=schema_migration_pending changesets=2") is not None
