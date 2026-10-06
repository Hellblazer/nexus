# SPDX-License-Identifier: AGPL-3.0-or-later
"""The Windows stop channel inside the storage-service supervisor
(RDR-224, nexus-f9bgu.17).

The supervisor's end of the chain ``nx`` CLI -> supervisor -> engine:

* a ``SIGBREAK`` handler that sets the same stop event ``SIGTERM`` does;
* the engine spawned in its own process group and put in a Job Object;
* the engine stopped with ``CTRL_BREAK`` and, when it ignores the break,
  killed through the job after the grace;
* no long wait on the main thread, so the handler can run.

Every Windows branch takes ``platform="win32"`` and a fake Job Object API, so
the branch runs on macOS and Linux as well. A test that only asserted "no
error" would pass with the Windows branch deleted, so each names something the
POSIX branch cannot produce (a creation flag, a job call, an ordered ladder).
The engine in the stop tests is a REAL child process: the fake API delivers
the break to it as a file it polls, and an "ignoring" engine really ignores it.
"""
from __future__ import annotations

import contextlib
import os
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest
import structlog

from nexus.daemon import readiness
from nexus.daemon import storage_service_daemon as ssd
from nexus.daemon.storage_service_daemon import StorageServiceSupervisor
from nexus.util.process_group import KILL_SIGNAL
from tests.daemon._children import KILLED_RC as _KILLED_RC
from tests.daemon._children import WIN as _WIN
from tests.daemon._children import spawn_breakable

CREATE_NEW_PROCESS_GROUP = 0x00000200


# ── fixtures ─────────────────────────────────────────────────────────────────────


class _FakeJobApi:
    """Stands in for ``nexus.util.win_job``. ``break_action`` decides what the
    break does to the registered engine; ``close_job`` is TerminateJobObject:
    it hard-kills whatever is still alive and records that it was.

    The break is delivered to the engine as a FILE the stand-in engine polls
    (see :func:`_spawn_engine`), on every platform. A real ``CTRL_BREAK`` would
    reach the pytest run's own process group on Windows (the test run was
    measured dying with ``0xC000013A``), and ``SIGTERM`` is not a graceful
    signal there (``os.kill`` is ``TerminateProcess``), so neither can stand in
    for it; a file can, and it lets an "ignoring" engine really ignore it."""

    IS_WINDOWS = True

    def __init__(
        self,
        *,
        create_ok: bool = True,
        assign_ok: bool = True,
        break_action: str = "deliver",  # "deliver" | "none"
    ) -> None:
        self.create_ok = create_ok
        self.assign_ok = assign_ok
        self.break_action = break_action
        self.events: list[tuple[str, Any]] = []
        self.engine: subprocess.Popen[bytes] | None = None
        self.alive_when_closed: list[bool] = []

    def create_job(self) -> int | None:
        self.events.append(("create_job", None))
        return 77 if self.create_ok else None

    def assign_process(self, job: int | None, pid: int) -> bool:
        self.events.append(("assign", (job, pid)))
        return self.assign_ok

    def send_ctrl_break(self, pid: int) -> bool:
        self.events.append(("break", pid))
        if self.break_action == "deliver":
            assert self.engine is not None and pid == self.engine.pid
            self.engine.break_file.write_text("break")  # type: ignore[attr-defined]
        return True  # a send returning True proves nothing about delivery

    def close_job(self, job: int | None) -> bool:
        engine = self.engine
        alive = engine is not None and engine.poll() is None
        self.alive_when_closed.append(alive)
        self.events.append(("close_job", job))
        if alive and engine is not None:
            os.kill(engine.pid, KILL_SIGNAL)
        return True

    def names(self) -> list[str]:
        return [name for name, _ in self.events]


@pytest.fixture
def config_dir(tmp_path: Path) -> Path:
    cd = tmp_path / "cfg"
    cd.mkdir(parents=True, exist_ok=True, mode=0o700)
    return cd


def _creds() -> dict[str, str]:
    return {
        "NX_DB_URL": "jdbc:...",
        "NX_DB_USER": "svc",
        "NX_DB_PASS": "pass",
        "NX_DB_ADMIN_URL": "jdbc:...",
        "NX_DB_ADMIN_USER": "admin",
        "NX_DB_ADMIN_PASS": "adminpass",
        "PG_PORT": "15432",
        "PG_DATA": "/tmp/pgdata",
        "NX_SERVICE_TOKEN": "root-token-from-creds-deadbeef",
    }


def _supervisor(
    config_dir: Path,
    *,
    platform: str | None,
    job: _FakeJobApi | None = None,
) -> StorageServiceSupervisor:
    return StorageServiceSupervisor(
        config_dir=config_dir,
        binary_path=Path("/fake/nexus-service"),
        pg_port=15432,
        service_port=18080,
        creds=_creds(),
        engine_liveness_scan=lambda _c, _b: [],
        platform=platform,
        win_job_api=job,
    )


def _spawn_engine(*, ignore_break: bool, where: Path) -> subprocess.Popen[bytes]:
    """A real child standing in for the engine (see ``tests/daemon/_children.py``)."""
    return spawn_breakable(ignore_break=ignore_break, where=where)


@contextlib.contextmanager
def _reaped(proc: subprocess.Popen[bytes]):
    try:
        yield proc
    finally:
        with contextlib.suppress(OSError):
            proc.kill()
        with contextlib.suppress(subprocess.TimeoutExpired, ChildProcessError):
            proc.wait(timeout=5)


# ── step 1: the SIGBREAK handler ─────────────────────────────────────────────────


class _FakeSignalModule:
    def __init__(self, *, with_sigbreak: bool) -> None:
        self.SIGTERM = 15
        self.SIGINT = 2
        if with_sigbreak:
            self.SIGBREAK = 21
        self.registered: dict[int, Any] = {}

    def signal(self, signum: int, handler: Any) -> None:
        self.registered[signum] = handler


def test_sigbreak_sets_the_same_stop_event_sigterm_sets() -> None:
    mod = _FakeSignalModule(with_sigbreak=True)
    stop = threading.Event()
    ssd._install_stop_handlers(stop, signal_module=mod)  # type: ignore[arg-type]
    # Non-vacuity: BREAK is registered in addition to the two POSIX signals.
    assert set(mod.registered) == {15, 2, 21}
    handler = mod.registered[21]
    assert not stop.is_set()
    handler(21, None)
    assert stop.is_set()
    # The same handler object serves all three, so the stop path cannot diverge.
    assert mod.registered[15] is mod.registered[2] is mod.registered[21]


def test_no_sigbreak_registration_where_the_platform_has_none() -> None:
    mod = _FakeSignalModule(with_sigbreak=False)
    ssd._install_stop_handlers(threading.Event(), signal_module=mod)  # type: ignore[arg-type]
    assert set(mod.registered) == {15, 2}


def test_run_storage_supervisor_installs_the_handlers_through_the_helper(
    config_dir: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The helper is what run_storage_supervisor calls, so the SIGBREAK
    registration is on the real path rather than a free-standing function."""
    seen: list[threading.Event] = []

    def fake_install(stop: threading.Event, **_k: Any) -> None:
        seen.append(stop)

    monkeypatch.setattr(ssd, "_install_stop_handlers", fake_install)
    monkeypatch.setattr(ssd, "_load_credentials", MagicMock(side_effect=RuntimeError("stop here")))
    monkeypatch.setattr("nexus.logging_setup.configure_logging", lambda *a, **k: None)
    with pytest.raises(RuntimeError, match="stop here"):
        ssd.run_storage_supervisor(config_dir=config_dir)
    assert len(seen) == 1 and isinstance(seen[0], threading.Event)


# ── step 3: engine spawn flags and the Job Object ────────────────────────────────


def _spawn_capturing(
    sup: StorageServiceSupervisor, monkeypatch: pytest.MonkeyPatch,
) -> dict[str, Any]:
    captured: dict[str, Any] = {}

    def fake_popen(argv: list[str], **kw: Any) -> MagicMock:
        captured.update(kw)
        return MagicMock(pid=43210)

    monkeypatch.setattr(ssd, "_popen", fake_popen)
    sup._spawn_service()
    return captured


def test_windows_engine_spawns_in_its_own_process_group_and_not_posix_shaped(
    config_dir: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    sup = _supervisor(config_dir, platform="win32", job=_FakeJobApi())
    kw = _spawn_capturing(sup, monkeypatch)
    assert kw["creationflags"] == CREATE_NEW_PROCESS_GROUP
    # Not CREATE_NO_WINDOW and not DETACHED: the engine shares the supervisor's
    # console, which is what lets the supervisor's CTRL_BREAK reach it.
    assert kw["creationflags"] & 0x08000000 == 0 and kw["creationflags"] & 0x00000008 == 0
    assert "start_new_session" not in kw and "preexec_fn" not in kw


def test_posix_engine_spawn_is_unchanged(
    config_dir: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    job = _FakeJobApi()
    sup = _supervisor(config_dir, platform="linux", job=job)
    kw = _spawn_capturing(sup, monkeypatch)
    assert kw["start_new_session"] is True
    assert "creationflags" not in kw
    assert "preexec_fn" in kw
    assert job.events == [], "POSIX never touches a Job Object"


def test_engine_is_put_in_a_job_object_right_after_spawn(
    config_dir: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    job = _FakeJobApi()
    sup = _supervisor(config_dir, platform="win32", job=job)
    _spawn_capturing(sup, monkeypatch)
    assert job.events == [("create_job", None), ("assign", (77, 43210))]
    assert sup._engine_job == 77


def test_a_job_that_cannot_be_made_degrades_with_a_warning_and_the_start_goes_on(
    config_dir: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    job = _FakeJobApi(create_ok=False)
    sup = _supervisor(config_dir, platform="win32", job=job)
    with structlog.testing.capture_logs() as logs:
        _spawn_capturing(sup, monkeypatch)
    assert sup._engine_job is None
    assert any(e["event"] == "storage_service_job_object_unavailable" for e in logs)


def test_a_refused_assignment_closes_the_job_it_made(
    config_dir: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    job = _FakeJobApi(assign_ok=False)
    sup = _supervisor(config_dir, platform="win32", job=job)
    with structlog.testing.capture_logs() as logs:
        _spawn_capturing(sup, monkeypatch)
    assert sup._engine_job is None
    assert job.names() == ["create_job", "assign", "close_job"]
    assert any(e["event"] == "storage_service_job_object_unavailable" for e in logs)


# ── steps 4 and 7: the stop ladder, with a real engine child ─────────────────────


@pytest.fixture
def fast_grace(monkeypatch: pytest.MonkeyPatch) -> float:
    monkeypatch.setattr(ssd, "_GRACEFUL_STOP_TIMEOUT", 0.6)
    return 0.6


def test_windows_stop_sends_the_break_and_a_responsive_engine_exits_cleanly(
    config_dir: Path, fast_grace: float,
) -> None:
    job = _FakeJobApi(break_action="deliver")
    sup = _supervisor(config_dir, platform="win32", job=job)
    with _reaped(_spawn_engine(ignore_break=False, where=config_dir)) as engine:
        job.engine = engine
        sup._proc = engine
        sup._engine_job = 77
        with structlog.testing.capture_logs() as logs:
            sup._stop_service()
        assert engine.poll() is not None, "the engine must be reaped"
    assert job.names() == ["break", "close_job"], job.names()
    # The job was closed AFTER the engine had gone: a release, not a kill.
    assert job.alive_when_closed == [False]
    assert not any(e["event"] == "storage_service_engine_unclean_stop" for e in logs)
    assert sup._proc is None and sup._engine_job is None


def test_an_engine_that_ignores_the_break_is_killed_through_the_job_after_the_grace(
    config_dir: Path, fast_grace: float,
) -> None:
    """The backstop (acceptance): the engine ignores CTRL_BREAK, the supervisor
    waits the grace, terminates the job, and the engine is gone and reaped."""
    job = _FakeJobApi(break_action="deliver")  # delivered, and this engine ignores it
    sup = _supervisor(config_dir, platform="win32", job=job)
    with _reaped(_spawn_engine(ignore_break=True, where=config_dir)) as engine:
        job.engine = engine
        sup._proc = engine
        sup._engine_job = 77
        t0 = time.monotonic()
        with structlog.testing.capture_logs() as logs:
            sup._stop_service()
        elapsed = time.monotonic() - t0
        # Non-vacuity: the engine was ALIVE when the job was terminated, so the
        # job close is what killed it, and it did wait the grace first.
        assert job.alive_when_closed == [True]
        assert elapsed >= fast_grace
        assert engine.poll() == _KILLED_RC
    assert job.names() == ["break", "close_job"]
    unclean = [e for e in logs if e["event"] == "storage_service_engine_unclean_stop"]
    assert len(unclean) == 1 and unclean[0]["via"] == "job_object"
    assert sup._proc is None and sup._engine_job is None


def test_without_a_job_the_backstop_is_the_hard_kill(
    config_dir: Path, fast_grace: float, monkeypatch: pytest.MonkeyPatch,
) -> None:
    job = _FakeJobApi(break_action="none")
    sup = _supervisor(config_dir, platform="win32", job=job)
    killed: list[int] = []
    real = __import__("nexus.util.process_group", fromlist=["safe_killpg"]).safe_killpg

    def spy(proc_or_pid: Any, sig: int = KILL_SIGNAL) -> bool:
        killed.append(proc_or_pid)
        return real(proc_or_pid, sig)

    monkeypatch.setattr("nexus.util.process_group.safe_killpg", spy)
    with _reaped(_spawn_engine(ignore_break=True, where=config_dir)) as engine:
        sup._proc = engine
        sup._engine_job = None
        with structlog.testing.capture_logs() as logs:
            sup._stop_service()
        assert engine.poll() == _KILLED_RC
    assert killed == [engine.pid]
    assert job.names() == ["break"], "no job to terminate, and none invented"
    unclean = [e for e in logs if e["event"] == "storage_service_engine_unclean_stop"]
    assert len(unclean) == 1 and unclean[0]["via"] == "hard_kill"


def test_the_job_is_released_when_the_engine_died_on_its_own(
    config_dir: Path,
) -> None:
    """Exit-3 path: the engine is already gone when stop() runs. The job
    handle must still close, or it leaks for the life of the supervisor."""
    job = _FakeJobApi()
    sup = _supervisor(config_dir, platform="win32", job=job)
    done = subprocess.Popen([sys.executable, "-c", "pass"])  # noqa: S603
    done.wait(timeout=30)
    sup._proc = done
    sup._engine_job = 77
    sup._stop_service()
    assert job.names() == ["close_job"]
    assert sup._engine_job is None


def test_readiness_failure_kill_uses_the_same_ladder(
    config_dir: Path, fast_grace: float,
) -> None:
    job = _FakeJobApi(break_action="deliver")
    sup = _supervisor(config_dir, platform="win32", job=job)
    with _reaped(_spawn_engine(ignore_break=True, where=config_dir)) as engine:
        job.engine = engine
        sup._engine_job = 77
        sup._kill_after_readiness_failure(engine)
        assert engine.poll() == _KILLED_RC
    assert job.names() == ["break", "close_job"]
    assert job.alive_when_closed == [True]


@pytest.mark.skipif(
    _WIN,
    reason="the POSIX arm stops the engine's process GROUP with SIGTERM and reads a "
    "-SIGTERM exit; Windows has no group signal and os.kill(SIGTERM) is TerminateProcess, "
    "so the arm cannot run there (its Windows counterpart is the Windows ladder tests above)",
)
def test_posix_stop_is_unchanged_and_never_touches_a_job(
    config_dir: Path, fast_grace: float,
) -> None:
    job = _FakeJobApi()
    sup = _supervisor(config_dir, platform="linux", job=job)
    with _reaped(_spawn_engine(ignore_break=False, where=config_dir)) as engine:
        sup._proc = engine
        sup._stop_service()
        assert engine.poll() == -signal.SIGTERM
    assert job.events == []


# ── what survives a supervisor stop ──────────────────────────────────────────────


def test_postgres_is_never_assigned_to_the_engine_job(
    config_dir: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The job holds exactly one process, the engine. PostgreSQL is started by
    pg_provision through its own Popen and is never passed to the supervisor's
    job API, so a plain stop leaves the postmaster running by construction and
    --with-pg stops it explicitly (pg_ctl stop -m fast)."""
    job = _FakeJobApi()
    sup = _supervisor(config_dir, platform="win32", job=job)
    _spawn_capturing(sup, monkeypatch)
    assigned = [arg for name, arg in job.events if name == "assign"]
    assert assigned == [(77, 43210)], "only the engine pid is ever assigned"


# ── step 2: no long wait on the supervisor's main thread ─────────────────────────


class _LoopSupervisor:
    """The minimum ``_supervise_until_stopped`` needs: a healthy beat, forever."""

    owns_process = True
    fenced = False
    _scope = "test-scope"

    def __init__(self) -> None:
        self.beats = 0
        self.stopped = False

    def start(self, *, stop_requested: Any = None) -> None:
        pass

    def heartbeat_once(self) -> tuple[bool, bool]:
        self.beats += 1
        return True, True

    def stop(self) -> None:
        self.stopped = True


def test_the_supervise_loop_waits_in_ticks_no_longer_than_one_second_and_a_stop_ends_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A Windows stop handler (``SIGBREAK``) runs on this thread only between waits, and the
    supervisor's grace is 20 s: a 30 s wait here would make every stop outlast the grace and
    end in the hard kill (J06). Every wait is bounded by one second, and a stop set during a
    wait ends the loop at the next iteration instead of beating again."""
    stop = threading.Event()
    sup = _LoopSupervisor()
    sleeps: list[float] = []

    def sleep(seconds: float) -> None:
        sleeps.append(seconds)
        if len(sleeps) == 3:
            stop.set()  # the handler running during the third wait
        if len(sleeps) > 6:
            raise AssertionError("the loop kept waiting after a stop was set")

    monkeypatch.setattr(ssd.time, "sleep", sleep)
    code = ssd._supervise_until_stopped(sup, stop, lambda: None)  # type: ignore[arg-type]
    assert code == 0 and sup.stopped
    assert len(sleeps) == 3, "the loop must end at the iteration after the stop"
    assert sup.beats == 3, "no heartbeat after the stop was set"
    assert all(0 < s <= 1.0 for s in sleeps), sleeps


def test_spawn_lock_wait_is_a_loop_of_short_nonblocking_tries_on_windows() -> None:
    attempts: list[bool] = []
    sleeps: list[float] = []

    def lock(fd: int, *, blocking: bool) -> None:
        attempts.append(blocking)
        if len(attempts) < 4:
            raise BlockingIOError("held")

    ssd._acquire_spawn_lock(
        9, None, windows=True, lock=lock, sleep=sleeps.append,
    )
    assert attempts == [False, False, False, False]
    assert len(sleeps) == 3 and all(0 < s <= 1.0 for s in sleeps), sleeps


def test_spawn_lock_wait_sees_a_stop_request_on_windows() -> None:
    stop = threading.Event()
    sleeps: list[float] = []

    def lock(fd: int, *, blocking: bool) -> None:
        raise BlockingIOError("held")

    def sleep(s: float) -> None:
        sleeps.append(s)
        if len(sleeps) == 2:
            stop.set()

    with pytest.raises(readiness.ReadinessStopRequestedError):
        ssd._acquire_spawn_lock(9, stop, windows=True, lock=lock, sleep=sleep)
    assert len(sleeps) == 2


def test_spawn_lock_on_posix_is_the_single_blocking_call_it_always_was() -> None:
    calls: list[bool] = []
    ssd._acquire_spawn_lock(
        9, threading.Event(), windows=False,
        lock=lambda fd, *, blocking: calls.append(blocking),
        sleep=lambda s: pytest.fail("POSIX must not poll"),
    )
    assert calls == [True]


def test_a_stop_during_the_postgres_start_unwinds_as_a_clean_stop(
    config_dir: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """StartInterruptedError from the Windows pg_ctl wait becomes the same
    ReadinessStopRequestedError a mid-readiness stop raises, so
    _supervise_until_stopped treats it as a clean exit-0 stop, not a crash
    and not a StorageServiceStartError."""
    from nexus.db import pg_provision

    sup = _supervisor(config_dir, platform="win32", job=_FakeJobApi())
    stop = threading.Event()
    sup._stop_requested = stop
    seen_kwargs: dict[str, Any] = {}

    def fake_start(bins: Any, pgdata: Path, port: int, **kw: Any) -> None:
        seen_kwargs.update(kw)
        raise pg_provision.StartInterruptedError("stop")

    monkeypatch.setattr(pg_provision, "_start_cluster", fake_start)
    monkeypatch.setattr(pg_provision, "discover_pg_binaries", lambda: object())
    monkeypatch.setattr(ssd, "_port_accepting", lambda *a, **k: False)
    with pytest.raises(readiness.ReadinessStopRequestedError):
        sup._ensure_pg_running()
    # The Windows supervisor hands the stop event to the start as its check.
    assert seen_kwargs["stop_check"] == stop.is_set


def test_posix_postgres_start_gets_no_stop_check(
    config_dir: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    from nexus.db import pg_provision

    sup = _supervisor(config_dir, platform="linux")
    sup._stop_requested = threading.Event()
    seen_kwargs: dict[str, Any] = {}

    def fake_start(bins: Any, pgdata: Path, port: int, **kw: Any) -> None:
        seen_kwargs.update(kw)

    monkeypatch.setattr(pg_provision, "_start_cluster", fake_start)
    monkeypatch.setattr(pg_provision, "discover_pg_binaries", lambda: object())
    accepting = iter([False, True])
    monkeypatch.setattr(ssd, "_port_accepting", lambda *a, **k: next(accepting))
    monkeypatch.setattr(sup, "_backfill_provision_grants", lambda: None)
    sup._ensure_pg_running()
    assert seen_kwargs.get("stop_check") is None


@pytest.mark.skipif(not hasattr(signal, "setitimer"), reason="Windows arms faulthandler instead of an interval timer")
def test_the_per_test_watchdog_is_armed_for_this_file() -> None:
    """A hang here fails instead of stalling the run (tests/daemon/_watchdog.py)."""
    assert signal.getitimer(signal.ITIMER_REAL)[0] > 0, (
        "the autouse watchdog fixture did not arm for this module: check WATCHED_MODULES"
    )
