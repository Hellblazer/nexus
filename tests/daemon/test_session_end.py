# SPDX-License-Identifier: AGPL-3.0-or-later
"""Session-end handling in the storage-service supervisor (RDR-224, nexus-f9bgu.51).

On Windows a sign-out, restart or shutdown reaches the supervisor's hidden console as
``CTRL_LOGOFF_EVENT`` / ``CTRL_SHUTDOWN_EVENT`` (``CTRL_CLOSE_EVENT`` for a closing
console). CPython maps none of the three to a signal, so only a
``SetConsoleCtrlHandler`` callback sees them. The callback stops the engine and then
PostgreSQL (``pg_ctl stop -m fast``) inside the budget Windows grants, so the next logon
does not crash-recover.

Every OS touchpoint is injected (registrar, engine stopper, pg stopper, clock), so all of
this runs on macOS and Linux. Each test names a fact the POSIX path cannot produce.
"""
from __future__ import annotations

import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any

import pytest

from nexus.daemon import service_registry as sr
from nexus.daemon import session_end as se
from nexus.daemon import storage_service_daemon as ssd
from nexus.daemon.service_registry import stop_requested_since


class _Clock:
    """A fixed, advanceable clock; a stopper "takes" time by advancing it."""

    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


class _Recorder:
    def __init__(self, clock: _Clock, *, engine_s: float = 0.0, pg_s: float = 0.0,
                 engine_ok: bool = True, pg_ok: bool = True) -> None:
        self.clock = clock
        self.engine_s, self.pg_s = engine_s, pg_s
        self.engine_ok, self.pg_ok = engine_ok, pg_ok
        self.calls: list[tuple[str, float]] = []
        self.stop_flag = threading.Event()
        self.marked = 0

    def stop_engine(self, budget: float) -> bool:
        self.calls.append(("engine", budget))
        self.clock.now += self.engine_s
        return self.engine_ok

    def stop_pg(self, budget: float) -> bool:
        self.calls.append(("pg", budget))
        self.clock.now += self.pg_s
        return self.pg_ok

    def mark(self) -> None:
        self.marked += 1

    def names(self) -> list[str]:
        return [n for n, _ in self.calls]


def _handler(rec: _Recorder, clock: _Clock, **kw: Any) -> se.SessionEndHandler:
    return se.SessionEndHandler(
        stop_requested=rec.stop_flag, stop_engine=rec.stop_engine, stop_pg=rec.stop_pg,
        mark_stop=rec.mark, clock=clock, **kw,
    )


# ── the event vocabulary ─────────────────────────────────────────────────────────


def test_owned_events_are_exactly_close_logoff_shutdown() -> None:
    # The Win32 values; CTRL_C (0) and CTRL_BREAK (1) are NOT ours.
    assert (se.CTRL_CLOSE_EVENT, se.CTRL_LOGOFF_EVENT, se.CTRL_SHUTDOWN_EVENT) == (2, 5, 6)
    assert se.OWNED_EVENTS == frozenset({2, 5, 6})


@pytest.mark.parametrize("event", [0, 1, 3, 4, 7, 99])
def test_events_the_handler_does_not_own_return_false_and_do_nothing(event: int) -> None:
    clock = _Clock()
    rec = _Recorder(clock)
    h = _handler(rec, clock)
    assert h(event) is False
    assert rec.calls == [] and rec.marked == 0 and not rec.stop_flag.is_set()


# ── the ordered stop ─────────────────────────────────────────────────────────────


@pytest.mark.parametrize("event", [2, 5, 6])
def test_an_owned_event_marks_flags_then_stops_engine_before_postgres(event: int) -> None:
    clock = _Clock()
    rec = _Recorder(clock, engine_s=0.3, pg_s=1.2)
    order: list[str] = []
    h = se.SessionEndHandler(
        stop_requested=rec.stop_flag,
        stop_engine=lambda b: (order.append("engine"), rec.stop_engine(b))[1],
        stop_pg=lambda b: (order.append("pg"), rec.stop_pg(b))[1],
        mark_stop=lambda: order.append("mark"),
        clock=clock,
    )
    assert h(event) is True
    assert order == ["mark", "engine", "pg"]
    assert rec.stop_flag.is_set()


def test_the_pg_budget_is_what_the_engine_left_of_the_whole_handler_budget() -> None:
    clock = _Clock()
    rec = _Recorder(clock, engine_s=1.5, pg_s=0.5)
    h = _handler(rec, clock)
    assert h(5) is True
    (_, engine_budget), (_, pg_budget) = rec.calls
    assert engine_budget == pytest.approx(se.ENGINE_BUDGET_S)
    assert pg_budget == pytest.approx(se.HANDLER_BUDGET_S - 1.5)


def test_a_slow_engine_cannot_starve_postgres_of_its_floor() -> None:
    # The engine ate the whole budget; PostgreSQL still gets PG_MIN_BUDGET_S, because a
    # clean PG stop is the one thing this handler exists for.
    clock = _Clock()
    rec = _Recorder(clock, engine_s=se.HANDLER_BUDGET_S + 5)
    h = _handler(rec, clock)
    assert h(6) is True
    assert rec.calls[1] == ("pg", se.PG_MIN_BUDGET_S)


def test_a_failing_engine_stop_does_not_stop_the_postgres_stop() -> None:
    clock = _Clock()
    rec = _Recorder(clock, engine_ok=False)
    h = _handler(rec, clock)
    assert h(5) is True
    assert rec.names() == ["engine", "pg"]


def test_an_engine_stopper_that_raises_is_contained() -> None:
    clock = _Clock()
    rec = _Recorder(clock)

    def boom(_b: float) -> bool:
        raise RuntimeError("engine stopper broke")

    h = se.SessionEndHandler(
        stop_requested=rec.stop_flag, stop_engine=boom, stop_pg=rec.stop_pg,
        mark_stop=rec.mark, clock=clock,
    )
    assert h(5) is True
    assert rec.names() == ["pg"]


def test_a_pg_stopper_that_raises_still_returns_true() -> None:
    # Returning False would hand the event to the default handler, which terminates the
    # process: strictly worse than returning after a failed stop.
    clock = _Clock()
    rec = _Recorder(clock)

    def boom(_b: float) -> bool:
        raise OSError("pg_ctl missing")

    h = se.SessionEndHandler(
        stop_requested=rec.stop_flag, stop_engine=rec.stop_engine, stop_pg=boom,
        mark_stop=rec.mark, clock=clock,
    )
    assert h(5) is True


def test_a_marker_that_cannot_be_written_does_not_stop_the_stop() -> None:
    clock = _Clock()
    rec = _Recorder(clock)

    def boom() -> None:
        raise OSError("disk full")

    h = se.SessionEndHandler(
        stop_requested=rec.stop_flag, stop_engine=rec.stop_engine, stop_pg=rec.stop_pg,
        mark_stop=boom, clock=clock,
    )
    assert h(5) is True
    assert rec.stop_flag.is_set()
    assert rec.names() == ["engine", "pg"]


def test_the_log_is_flushed_after_the_steps_even_when_the_flush_raises() -> None:
    clock = _Clock()
    rec = _Recorder(clock)
    order: list[str] = []

    def flush() -> None:
        order.append("flush")
        raise OSError("handler closed")

    h = se.SessionEndHandler(
        stop_requested=rec.stop_flag,
        stop_engine=lambda b: (order.append("engine"), True)[1],
        stop_pg=lambda b: (order.append("pg"), True)[1],
        mark_stop=rec.mark, clock=clock, flush=flush,
    )
    assert h(5) is True
    assert order == ["engine", "pg", "flush"]


# ── idempotence ──────────────────────────────────────────────────────────────────


def test_a_second_event_after_the_first_finished_does_not_stop_anything_again() -> None:
    clock = _Clock()
    rec = _Recorder(clock)
    h = _handler(rec, clock)
    assert h(5) is True
    assert h(6) is True  # a LOGOFF is typically followed by a SHUTDOWN
    assert rec.names() == ["engine", "pg"]
    assert rec.marked == 1


def test_a_concurrent_second_event_waits_for_the_first_and_stops_nothing_twice() -> None:
    rec_clock = time.monotonic  # real clock: this test is about real threads
    gate = threading.Event()
    calls: list[str] = []

    def slow_pg(_b: float) -> bool:
        calls.append("pg")
        gate.wait(timeout=5)
        return True

    stop = threading.Event()
    h = se.SessionEndHandler(
        stop_requested=stop, stop_engine=lambda _b: calls.append("engine") or True,
        stop_pg=slow_pg, mark_stop=lambda: None, clock=rec_clock,
    )
    results: list[bool] = []
    first = threading.Thread(target=lambda: results.append(h(5)))
    first.start()
    while "pg" not in calls:
        time.sleep(0.005)
    second = threading.Thread(target=lambda: results.append(h(6)))
    second.start()
    time.sleep(0.05)
    assert second.is_alive(), "the second event must wait for the stop already in flight"
    gate.set()
    first.join(5)
    second.join(5)
    assert results == [True, True]
    assert calls == ["engine", "pg"]


def test_a_second_event_does_not_wait_past_the_budget_for_a_wedged_first() -> None:
    gate = threading.Event()
    stop = threading.Event()
    h = se.SessionEndHandler(
        stop_requested=stop, stop_engine=lambda _b: True,
        stop_pg=lambda _b: gate.wait(timeout=10) or True,
        mark_stop=lambda: None, budget_s=0.3,
    )
    first = threading.Thread(target=lambda: h(5), daemon=True)
    first.start()
    time.sleep(0.05)
    t0 = time.monotonic()
    assert h(6) is True
    assert time.monotonic() - t0 < 2.0
    gate.set()
    first.join(5)


# ── installation ─────────────────────────────────────────────────────────────────


class _FakeRegistrar:
    def __init__(self, *, ok: bool = True) -> None:
        self.ok = ok
        self.registered: list[Any] = []
        self.unregistered: list[Any] = []

    def register(self, callback: Any) -> bool:
        self.registered.append(callback)
        return self.ok

    def unregister(self, callback: Any) -> bool:
        self.unregistered.append(callback)
        return True


def _noop_handler() -> se.SessionEndHandler:
    return se.SessionEndHandler(
        stop_requested=threading.Event(), stop_engine=lambda _b: True,
        stop_pg=lambda _b: True, mark_stop=lambda: None,
    )


def test_install_registers_the_handler_and_uninstall_unregisters_it_once() -> None:
    reg = _FakeRegistrar()
    h = _noop_handler()
    uninstall = se.install_session_end_handler(h, registrar=reg, platform="win32")
    assert reg.registered == [h]
    uninstall()
    uninstall()  # idempotent
    assert reg.unregistered == [h]


def test_install_is_a_no_op_off_windows_and_never_builds_a_registrar(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def boom() -> Any:
        raise AssertionError("POSIX must not construct the ctypes registrar")

    monkeypatch.setattr(se, "ctypes_ctrl_registrar", boom)
    uninstall = se.install_session_end_handler(_noop_handler(), platform="linux")
    uninstall()


def test_a_registration_the_os_refuses_leaves_a_no_op_uninstall() -> None:
    reg = _FakeRegistrar(ok=False)
    uninstall = se.install_session_end_handler(_noop_handler(), registrar=reg, platform="win32")
    uninstall()
    assert reg.unregistered == []


def test_importing_the_module_loads_no_windows_only_ctypes_module() -> None:
    # A clean interpreter: the module's own import must not touch ctypes.wintypes (the
    # Windows-only types are built lazily inside the registrar), on any host. On Windows
    # ``import nexus`` itself loads ctypes.wintypes (truststore, nexus-f9bgu.52), so the
    # package is imported first and the module dropped before session_end is imported.
    code = (
        "import sys, nexus;"
        "sys.modules.pop('ctypes.wintypes', None);"
        "import nexus.daemon.session_end;"
        "sys.exit(1 if 'ctypes.wintypes' in sys.modules else 0)"
    )
    assert subprocess.run([sys.executable, "-c", code], check=False).returncode == 0


# ── the supervisor's engine stopper ──────────────────────────────────────────────


class _FakeProc:
    def __init__(self, *, exits_on_break: bool = True, alive: bool = True) -> None:
        self.pid = 4242
        self.exits_on_break = exits_on_break
        self.returncode: int | None = None if alive else 0
        self.waits: list[float | None] = []

    def poll(self) -> int | None:
        return self.returncode

    def break_delivered(self) -> None:
        if self.exits_on_break:
            self.returncode = 0

    def wait(self, timeout: float | None = None) -> int:
        self.waits.append(timeout)
        if self.returncode is None:
            raise subprocess.TimeoutExpired("engine", timeout or 0)
        return self.returncode


class _FakeJob:
    IS_WINDOWS = True

    def __init__(self, proc: _FakeProc | None) -> None:
        self.proc = proc
        self.events: list[str] = []

    def create_job(self) -> int:
        return 1

    def assign_process(self, job: Any, pid: int) -> bool:
        return True

    def send_ctrl_break(self, pid: int) -> bool:
        self.events.append("break")
        if self.proc is not None:
            self.proc.break_delivered()
        return True

    def close_job(self, job: Any) -> bool:
        self.events.append("close_job")
        if self.proc is not None:
            self.proc.returncode = 1  # TerminateJobObject
        return True


def _supervisor(tmp_path: Path, job: _FakeJob) -> ssd.StorageServiceSupervisor:
    return ssd.StorageServiceSupervisor(
        config_dir=tmp_path, binary_path=Path("/fake/nexus-service"), pg_port=15432,
        service_port=18080, creds={"PG_PORT": "15432", "PG_DATA": "/tmp/pgdata",
                                   "NX_SERVICE_TOKEN": "t"},
        engine_liveness_scan=lambda _c, _b: [], platform="win32", win_job_api=job,
    )


def test_session_end_engine_stop_sends_a_break_and_waits_for_exit(tmp_path: Path) -> None:
    proc = _FakeProc()
    job = _FakeJob(proc)
    sup = _supervisor(tmp_path, job)
    sup._proc = proc  # type: ignore[assignment]
    assert sup.stop_engine_for_session_end(2.0) is True
    assert job.events == ["break"]


def test_session_end_engine_stop_kills_the_job_when_the_break_is_ignored(tmp_path: Path) -> None:
    proc = _FakeProc(exits_on_break=False)
    job = _FakeJob(proc)
    sup = _supervisor(tmp_path, job)
    sup._engine_job = 1
    sup._proc = proc  # type: ignore[assignment]
    assert sup.stop_engine_for_session_end(0.2) is True
    assert job.events == ["break", "close_job"]


def test_session_end_engine_stop_is_a_no_op_for_a_dead_or_absent_engine(tmp_path: Path) -> None:
    job = _FakeJob(None)
    sup = _supervisor(tmp_path, job)
    assert sup.stop_engine_for_session_end(1.0) is True  # no engine at all
    sup._proc = _FakeProc(alive=False)  # type: ignore[assignment]
    assert sup.stop_engine_for_session_end(1.0) is True
    assert job.events == []


def test_session_end_engine_stop_does_not_race_the_normal_stop(tmp_path: Path) -> None:
    """The normal shutdown path (_stop_service, on the main thread) and the handler
    (a system thread) share a lock: while one holds it the other waits at most its budget
    and never signals the engine a second time."""
    proc = _FakeProc()
    job = _FakeJob(proc)
    sup = _supervisor(tmp_path, job)
    sup._proc = proc  # type: ignore[assignment]
    assert sup._engine_stop_lock.acquire(timeout=1)
    try:
        t0 = time.monotonic()
        assert sup.stop_engine_for_session_end(0.2) is False
        assert time.monotonic() - t0 < 2.0
        assert job.events == []
    finally:
        sup._engine_stop_lock.release()


# ── wiring in the supervisor ─────────────────────────────────────────────────────


def test_the_wiring_installs_nothing_on_posix(tmp_path: Path) -> None:
    sup = ssd.StorageServiceSupervisor(
        config_dir=tmp_path, binary_path=Path("/fake/nexus-service"), pg_port=15432,
        service_port=18080, creds={"PG_PORT": "15432", "PG_DATA": "/tmp/pgdata",
                                   "NX_SERVICE_TOKEN": "t"},
        engine_liveness_scan=lambda _c, _b: [], platform="linux",
    )
    reg = _FakeRegistrar()
    uninstall = ssd._install_session_end_handler(
        sup, threading.Event(), tmp_path, {"PG_DATA": "/x"}, registrar=reg,
        platform="linux",
    )
    uninstall()
    assert reg.registered == []


def test_the_wiring_on_windows_registers_a_handler_that_stops_everything_and_marks_the_stop(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    proc = _FakeProc()
    job = _FakeJob(proc)
    sup = _supervisor(tmp_path, job)
    sup._proc = proc  # type: ignore[assignment]
    stop = threading.Event()
    pg_calls: list[float] = []
    monkeypatch.setattr(
        ssd, "_session_end_pg_stopper",
        lambda creds: (lambda budget: pg_calls.append(budget) or True),
    )
    monkeypatch.setattr(ssd, "_session_end_write_marker", lambda cd: se_marks.append(cd))
    se_marks: list[Path] = []
    # This test is about the console handler; the window path has its own tests
    # (test_session_end_window.py), so keep it from building a real window on a Windows host.
    monkeypatch.setattr(se, "ctypes_window_backend", lambda: (_ for _ in ()).throw(OSError("off")))
    reg = _FakeRegistrar()
    uninstall = ssd._install_session_end_handler(
        sup, stop, tmp_path, {"PG_DATA": "/x"}, registrar=reg, platform="win32",
    )
    (handler,) = reg.registered
    assert handler(5) is True
    assert stop.is_set()
    assert job.events == ["break"]
    assert len(pg_calls) == 1
    assert se_marks == [tmp_path]
    uninstall()
    assert reg.unregistered == [handler]


def test_the_session_end_marker_is_the_launchers_stop_marker(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The launcher must not respawn the supervisor while the session is ending; it reads
    this very marker (windows_autostart.run_launcher), so the handler writes THAT one."""
    # The owner-only ACL step needs a real Windows; the POSIX arm creates the file instead
    # (the same seam tests/daemon/test_windows_stop_marker.py uses).
    real = sr.open_private
    monkeypatch.setattr(sr, "open_private", lambda p, f, **kw: real(p, f, platform="linux"))
    before = time.time() - 1
    # Off Windows write_stop_marker returns None by design, so exercise the real writer
    # through its platform seam.
    path = ssd._session_end_write_marker(tmp_path, platform="win32", scope_key="S-1")
    assert path is not None and path.exists()
    assert stop_requested_since(tmp_path, "storage_service", "S-1", before) is True


def test_run_storage_supervisor_installs_and_uninstalls_the_handler(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[str] = []
    monkeypatch.setattr(ssd, "_install_session_end_handler",
                        lambda *a, **k: (events.append("install"), lambda: events.append("uninstall"))[1])
    monkeypatch.setattr(ssd, "_supervise_until_stopped",
                        lambda *a, **k: (events.append("supervise"), 0)[1])
    monkeypatch.setattr(ssd, "_load_credentials",
                        lambda cd: {"PG_PORT": "15432", "PG_DATA": "x", "NX_SERVICE_TOKEN": "t"})
    monkeypatch.setattr(ssd, "_resolve_launch_artifact", lambda cd: (Path("/fake"), "native"))
    monkeypatch.setattr(ssd, "_install_stop_handlers", lambda *a, **k: None)
    assert ssd.run_storage_supervisor(config_dir=tmp_path) == 0
    assert events == ["install", "supervise", "uninstall"]
