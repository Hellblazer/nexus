# SPDX-License-Identifier: AGPL-3.0-or-later
"""The engine bind wait tracks migration progress, not a fixed clock.

develop CI run 42436306115 (shard 3/6, 2f85935a2) errored two tests at setup with
``T2 engine substrate: service did not bind port 22980``. The engine log tail showed
Liquibase still applying changesets (vectors-046-4 ran, vectors-047-1 was starting)
when the fixed 60 s bind deadline gave up: under pool load the migration is slower
than that deadline, and the engine was healthy and advancing.

``_wait_engine_ready`` now gives up on one of two things only: no migration progress
for a stall window, or a hard ceiling. Progress is what the engine's own log shows
(``Running Changeset:`` lines and the ``schema_migration_*`` events, read through
``nexus.daemon.readiness.MigrationLogScanner``), never log growth as such. A dead
process still fails at once.

These tests drive the wait with a fake clock and a fake log: no engine, no Postgres,
no real sleeping::

    NX_TEST_T2_SUBSTRATE=none uv run pytest tests/test_engine_substrate_bind_wait.py -q
"""
from __future__ import annotations

import socket
from collections.abc import Callable, Iterator
from pathlib import Path

import pytest

from tests import _engine_substrate as sub

_STALL_S = 60.0
_MIGRATING_STALL_S = 180.0
_CEILING_S = 300.0
_RUNAWAY_S = 10_000.0


class _Clock:
    """A clock that only moves when the wait sleeps. Each sleep first runs the scripted
    engine behaviour for the new time, so the log grows exactly as the script says."""

    def __init__(self) -> None:
        self.now = 0.0
        self._hooks: list[Callable[[float], None]] = []

    def __call__(self) -> float:
        return self.now

    def on_tick(self, hook: Callable[[float], None]) -> None:
        self._hooks.append(hook)

    def sleep(self, seconds: float) -> None:
        self.now += seconds
        # A wait with no ceiling would loop forever on a fake clock; fail instead of hanging.
        assert self.now < _RUNAWAY_S, "the wait ran past any sane ceiling (a bound is missing)"
        for hook in self._hooks:
            hook(self.now)


class _Proc:
    """``Popen.poll()`` over the fake clock: alive until ``die_at``."""

    def __init__(self, clock: _Clock, *, die_at: float | None = None, rc: int = 1) -> None:
        self._clock = clock
        self._die_at = die_at
        self._rc = rc

    def poll(self) -> int | None:
        if self._die_at is not None and self._clock.now >= self._die_at:
            return self._rc
        return None


class _EngineLog:
    def __init__(self, path: Path) -> None:
        self.path = path
        path.write_text("")

    def line(self, text: str) -> None:
        with self.path.open("a", encoding="utf-8") as fh:
            fh.write(text + "\n")

    def changeset(self, name: str) -> None:
        self.line(f"Running Changeset: db/changelog/{name}.xml::{name}::nexus-test")

    def migration_start(self, pending: int = 400) -> None:
        self.line("2026-11-08 07:00:00,000 INFO  [main] d.n.s.d.SchemaMigrator - event=schema_migration_start changelog=x")
        self.line(f"2026-11-08 07:00:00,100 INFO  [main] d.n.s.d.SchemaMigrator - event=schema_migration_pending changesets={pending}")

    def migration_complete(self) -> None:
        self.line("2026-11-08 07:01:00,000 INFO  [main] d.n.s.d.SchemaMigrator - event=schema_migration_complete new_changesets=400")

    def ready(self, port: int) -> None:
        self.line(f"2026-11-08 07:01:30,000 INFO  [main] d.n.s.NexusService - {sub._ENGINE_READY_PREFIX}{port}")


@pytest.fixture
def clock(monkeypatch: pytest.MonkeyPatch) -> _Clock:
    monkeypatch.setattr(sub, "_ENGINE_POLL_S", 1.0)
    return _Clock()


@pytest.fixture
def log(tmp_path: Path) -> _EngineLog:
    return _EngineLog(tmp_path / "engine.log")


@pytest.fixture
def listener() -> Iterator[socket.socket]:
    """A real listener standing in for the engine once it has bound (the wait confirms the
    port answers after it reads the ready line)."""
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    sock.listen(16)
    try:
        yield sock
    finally:
        sock.close()


def _wait(clock: _Clock, proc: _Proc, port: int, log: _EngineLog, **kw) -> None:
    sub._wait_engine_ready(
        proc, port, str(log.path),
        stall_s=kw.get("stall_s", _STALL_S), ceiling_s=kw.get("ceiling_s", _CEILING_S),
        migrating_stall_s=kw.get("migrating_stall_s", _MIGRATING_STALL_S),
        clock=clock, sleep=clock.sleep,
    )


def _migrate_every(clock: _Clock, log: _EngineLog, *, every: float, until: float = float("inf"), prefix: str = "cs") -> None:
    """Log a new changeset every *every* seconds until *until*."""
    state = {"n": 0, "next": every}

    def tick(now: float) -> None:
        while now >= state["next"] and state["next"] <= until:
            state["n"] += 1
            log.changeset(f"{prefix}-{state['n']}")
            state["next"] += every

    clock.on_tick(tick)


def _schedule(clock: _Clock, events: dict[float, Callable[[], None]]) -> None:
    """Run each callable once, at the first tick at or after its time."""
    pending = sorted(events.items())

    def tick(now: float) -> None:
        while pending and now >= pending[0][0]:
            pending.pop(0)[1]()

    clock.on_tick(tick)


class TestMigrationStillAdvancing:
    def test_a_migration_that_runs_past_the_old_fixed_deadline_is_waited_for_and_binds(
        self, clock, log, listener,
    ) -> None:
        """The run-42436306115 shape: Liquibase is slow but advancing. A changeset every
        5 s for 90 s is past the old 60 s deadline, then the engine binds."""
        port = listener.getsockname()[1]
        log.migration_start()
        _migrate_every(clock, log, every=5.0, until=90.0)

        _schedule(clock, {94.0: log.migration_complete, 95.0: lambda: log.ready(port)})
        _wait(clock, _Proc(clock), port, log)
        assert clock.now > 60.0, "the wait ended before the old deadline, so it proved nothing"

    def test_the_stall_window_runs_from_the_last_progress_not_from_the_spawn(
        self, clock, log, listener,
    ) -> None:
        """A changeset that takes 50 s of silence, twice, is not a stall at a 60 s window."""
        port = listener.getsockname()[1]
        log.migration_start()
        _schedule(clock, {
            10.0: lambda: log.changeset("cs-1"),
            60.0: lambda: log.changeset("cs-2"),
            110.0: lambda: log.changeset("cs-3"),
            115.0: lambda: (log.migration_complete(), log.ready(port)),
        })
        _wait(clock, _Proc(clock), port, log)
        assert clock.now > 100.0


class TestStallWindows:
    def test_one_changeset_silent_past_the_short_window_is_not_a_stall_while_migrating(
        self, clock, log, listener,
    ) -> None:
        """A single slow changeset under load: 120 s with no line, inside the migrating
        window (180 s) though past the 60 s one."""
        port = listener.getsockname()[1]
        log.migration_start()
        _schedule(clock, {
            5.0: lambda: log.changeset("slow-1"),
            125.0: lambda: log.changeset("slow-2"),
            130.0: lambda: (log.migration_complete(), log.ready(port)),
        })
        _wait(clock, _Proc(clock), port, log)
        assert clock.now > 120.0

    def test_the_short_window_still_applies_after_complete(self, clock, log) -> None:
        log.migration_start()
        _schedule(clock, {5.0: lambda: log.changeset("cs-1"), 6.0: log.migration_complete})
        with pytest.raises(TimeoutError, match=r"stalled after schema_migration_complete for 60s"):
            _wait(clock, _Proc(clock), 1, log)
        assert clock.now <= 70.0

    def test_the_short_window_applies_before_any_migration_has_started(self, clock, log) -> None:
        with pytest.raises(TimeoutError, match=r"stalled before any migration output for 60s"):
            _wait(clock, _Proc(clock), 1, log)
        assert clock.now <= 62.0


class TestPartialLines:
    def test_a_half_written_changeset_line_is_counted_once_when_it_completes(
        self, clock, log,
    ) -> None:
        """Engine stdout flushes in small buffers: a read can end mid-line. The half must
        not be fed to the scanner (it would name a truncated changeset, and the rest of
        the line, fed alone, would match nothing)."""
        log.migration_start()

        def write_half() -> None:
            with log.path.open("a", encoding="utf-8") as fh:
                fh.write("Running Changeset: db/changelog/vectors-0")

        def write_rest() -> None:
            with log.path.open("a", encoding="utf-8") as fh:
                fh.write("47.xml::vectors-047-1::nexus-test\n")

        _schedule(clock, {3.0: write_half, 5.0: write_rest})
        with pytest.raises(TimeoutError) as err:
            _wait(clock, _Proc(clock), 1, log)
        assert "stalled at db/changelog/vectors-047.xml::vectors-047-1::nexus-test for 180s" in str(err.value)
        assert 184.0 <= clock.now <= 187.0, "progress is the completed line (t=5), not the half (t=3)"


class TestStall:
    def test_no_progress_for_the_stall_window_fails_naming_the_last_changeset(
        self, clock, log,
    ) -> None:
        log.migration_start()
        _migrate_every(clock, log, every=5.0, until=20.0, prefix="vectors-047")
        with pytest.raises(TimeoutError) as err:
            _wait(clock, _Proc(clock), 1, log)
        text = str(err.value)
        assert "stalled at db/changelog/vectors-047-4.xml::vectors-047-4::nexus-test for 180s" in text
        assert "ceiling" not in text
        assert 199.0 <= clock.now <= 202.0, f"gave up at t={clock.now}, not one stall window after the last progress"

    def test_log_growth_that_is_not_migration_progress_does_not_reset_the_stall(
        self, clock, log,
    ) -> None:
        """Lock-wait spam and unrelated output grow the log every second; only changeset
        lines and migration events are progress."""
        log.migration_start()
        log.changeset("cs-1")

        def noise(_now: float) -> None:
            log.line("Waiting for changelog lock....")
            log.line("2026-11-08 INFO  HikariPool-1 - Pool stats")

        clock.on_tick(noise)
        with pytest.raises(TimeoutError, match=r"stalled at \S*cs-1\S* for 180s"):
            _wait(clock, _Proc(clock), 1, log)
        assert clock.now <= 190.0

    def test_a_silent_engine_stalls_before_any_migration_output(self, clock, log) -> None:
        with pytest.raises(TimeoutError, match=r"stalled before any migration output for 60s"):
            _wait(clock, _Proc(clock), 1, log)
        assert 60.0 <= clock.now <= 62.0

    def test_a_migration_that_finished_but_never_binds_stalls_after_the_complete_event(
        self, clock, log,
    ) -> None:
        log.migration_start()
        log.changeset("cs-1")
        _schedule(clock, {10.0: log.migration_complete})
        with pytest.raises(TimeoutError, match=r"stalled after schema_migration_complete for 60s"):
            _wait(clock, _Proc(clock), 1, log)
        assert 69.0 <= clock.now <= 72.0


class TestCeiling:
    def test_progress_that_never_ends_stops_at_the_ceiling_and_says_so(self, clock, log) -> None:
        log.migration_start()
        _migrate_every(clock, log, every=5.0, prefix="cs")
        with pytest.raises(TimeoutError) as err:
            _wait(clock, _Proc(clock), 1, log)
        text = str(err.value)
        assert "ceiling 300s reached while still migrating at db/changelog/cs-" in text
        assert "stalled" not in text
        assert 300.0 <= clock.now <= 302.0

    def test_the_ceiling_after_the_migration_finished_does_not_claim_it_is_still_migrating(
        self, clock, log,
    ) -> None:
        log.migration_start()
        _schedule(clock, {280.0: log.migration_complete})
        _migrate_every(clock, log, every=30.0, until=270.0)
        with pytest.raises(TimeoutError) as err:
            _wait(clock, _Proc(clock), 1, log)
        text = str(err.value)
        assert "ceiling 300s reached" in text
        assert "still migrating" not in text
        assert "after schema_migration_complete" in text


class TestReadyLineThenNoAnswer:
    @pytest.fixture
    def silent_port(self) -> Iterator[int]:
        """A port that is bound but not listening: a connect is refused at once."""
        sock = socket.socket()
        sock.bind(("127.0.0.1", 0))
        try:
            yield sock.getsockname()[1]
        finally:
            sock.close()

    def test_an_engine_that_logs_ready_and_then_dies_fails_at_once_with_its_log(
        self, clock, log, silent_port,
    ) -> None:
        """OOM kill or crash right after the ready line: nothing listens, the process is
        gone. The connect phase must not wait out the ceiling."""
        log.migration_start()
        log.migration_complete()
        log.ready(silent_port)
        log.line("java.lang.OutOfMemoryError: Java heap space")
        with pytest.raises(RuntimeError) as err:
            _wait(clock, _Proc(clock, die_at=2.0, rc=137), silent_port, log)
        assert not isinstance(err.value, TimeoutError)
        assert "exited with rc=137" in str(err.value)
        assert "OutOfMemoryError" in str(err.value)
        assert clock.now <= 3.0, f"a dead engine was waited on until t={clock.now}"

    def test_a_live_process_whose_port_never_answers_fails_within_the_connect_phase(
        self, clock, log, silent_port,
    ) -> None:
        log.ready(silent_port)
        with pytest.raises(TimeoutError) as err:
            _wait(clock, _Proc(clock), silent_port, log)
        assert f"port {silent_port} did not answer for 5s" in str(err.value)
        assert clock.now <= 7.0, "the connect phase borrowed the ceiling"


class TestDeadProcess:
    def test_a_process_that_exits_mid_migration_fails_at_once_with_its_log(self, clock, log) -> None:
        log.migration_start()
        _migrate_every(clock, log, every=2.0)
        _schedule(clock, {12.0: lambda: log.line("java.lang.IllegalStateException: boom")})
        with pytest.raises(RuntimeError) as err:
            _wait(clock, _Proc(clock, die_at=12.0, rc=1), 22980, log)
        assert not isinstance(err.value, TimeoutError)
        text = str(err.value)
        assert "exited with rc=1" in text
        assert "service did not bind port 22980" in text
        assert "IllegalStateException" in text, "the log tail travels with the failure"
        assert clock.now <= 13.0, "a dead engine was waited on"

    def test_a_dead_process_on_a_bind_failure_is_still_a_lost_port(self, clock, log) -> None:
        log.line("Exception in thread \"main\" java.net.BindException: Address already in use")
        with pytest.raises(sub._PortLost):
            _wait(clock, _Proc(clock, die_at=0.0, rc=1), 22980, log)


def test_the_constants_leave_the_stall_windows_inside_the_ceiling() -> None:
    assert 0 < sub._ENGINE_STALL_S <= sub._ENGINE_MIGRATING_STALL_S < sub._ENGINE_CEILING_S
    assert 0 < sub._ENGINE_CONNECT_S <= 10.0
    # The old fixed deadline was 60 s from the spawn; a stall window shorter than that
    # would make this wait stricter than the one it replaces.
    assert sub._ENGINE_STALL_S >= 60.0
