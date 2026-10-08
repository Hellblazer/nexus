# SPDX-License-Identifier: AGPL-3.0-or-later
"""The aspect-worker daemon on Windows (RDR-224, nexus-f9bgu.33/.34).

Three things the supervisor already had and the aspect worker did not:

* its spawn carries the supervisor's flags (own process group, no console
  window), so ``CTRL_BREAK`` can reach it and no window opens beside a
  console-less MCP host;
* a ``SIGBREAK`` handler that sets the same stop event ``SIGTERM`` does;
* a main-thread wait made of ticks no longer than one second, because a
  CPython ``SIGBREAK`` handler never runs inside one long wait
  (T2 ``nexus_rdr/224-research-22``).

The restart path in ``upgrade_finish`` stops it through the shared graceful-stop
primitive instead of ``os.kill(pid, SIGTERM)``, which is ``TerminateProcess``
on Windows and skips the worker's bounded drain.

Every Windows arm takes ``platform="win32"`` or a fake console API, so it runs
on macOS and Linux. Each test names something the POSIX arm cannot produce.
"""
from __future__ import annotations

import functools
import signal
import threading
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from nexus import upgrade_finish as uf
from nexus.daemon import aspect_worker_daemon as awd
from nexus.daemon.aspect_worker_daemon import AspectWorkerDaemon, ensure_aspect_worker_daemon
from nexus.daemon.service_registry import DEFAULT_HEARTBEAT_INTERVAL, request_graceful_stop
from nexus.upgrade_finish import SkewReport, StaleProcess
from nexus.util import win_job
from tests._module_seam import patch_in, patch_time

# ── spawn flags ──────────────────────────────────────────────────────────────────


class _FakePopen:
    calls: list[dict[str, Any]] = []

    def __init__(self, argv: list[str], **kwargs: Any) -> None:
        type(self).calls.append({"argv": argv, "kwargs": kwargs})
        self.pid = 4242


@pytest.fixture(autouse=True)
def _reset() -> Any:
    _FakePopen.calls = []
    awd._recent_spawn.clear()
    yield
    awd._recent_spawn.clear()


def test_windows_spawn_gets_its_own_group_and_no_window(tmp_path: Path) -> None:
    ensure_aspect_worker_daemon(
        config_dir=tmp_path, tenant="default", _popen=_FakePopen, _platform="win32",
    )
    kwargs = _FakePopen.calls[0]["kwargs"]
    assert kwargs["creationflags"] & ~win_job.CREATE_BREAKAWAY_FROM_JOB == (
        win_job.CREATE_NEW_PROCESS_GROUP | win_job.CREATE_NO_WINDOW
    )
    # start_new_session is ignored on Windows; passing it would only mislead.
    assert "start_new_session" not in kwargs
    # NEVER DETACHED_PROCESS: the stopper attaches to the target's console.
    assert not kwargs["creationflags"] & 0x00000008


def test_windows_spawn_breaks_away_from_the_hosts_job_first(tmp_path: Path) -> None:
    """The daemon outlives the storing process. Under the desktop extension
    that process sits in a kill-on-close Job Object, so a child that does not
    break away dies when the extension closes (RDR-224 review finding E)."""
    ensure_aspect_worker_daemon(
        config_dir=tmp_path, tenant="default", _popen=_FakePopen, _platform="win32",
    )
    assert len(_FakePopen.calls) == 1
    assert _FakePopen.calls[0]["kwargs"]["creationflags"] & win_job.CREATE_BREAKAWAY_FROM_JOB


def test_windows_spawn_falls_back_to_a_plain_spawn_when_breakaway_is_refused(
    tmp_path: Path,
) -> None:
    """A job that forbids breakaway refuses CreateProcess with access denied;
    the spawn is retried once without the flag (the session-end launcher's
    pattern) and the daemon still starts."""

    class _RefusesBreakaway(_FakePopen):
        def __init__(self, argv: list[str], **kwargs: Any) -> None:
            if kwargs["creationflags"] & win_job.CREATE_BREAKAWAY_FROM_JOB:
                type(self).calls.append({"argv": argv, "kwargs": kwargs, "refused": True})
                raise PermissionError(5, "Access is denied")
            super().__init__(argv, **kwargs)

    assert ensure_aspect_worker_daemon(
        config_dir=tmp_path, tenant="default", _popen=_RefusesBreakaway, _platform="win32",
    )
    first, second = _FakePopen.calls
    assert first["refused"] is True
    assert second["kwargs"]["creationflags"] == (
        win_job.CREATE_NEW_PROCESS_GROUP | win_job.CREATE_NO_WINDOW
    )
    assert second["argv"] == first["argv"]


def test_windows_spawn_that_fails_both_ways_raises_the_second_error(tmp_path: Path) -> None:
    class _AlwaysFails:
        calls = 0

        def __init__(self, argv: list[str], **kwargs: Any) -> None:
            type(self).calls += 1
            raise FileNotFoundError(2, "nx not found")

    with pytest.raises(FileNotFoundError):
        ensure_aspect_worker_daemon(
            config_dir=tmp_path, tenant="default", _popen=_AlwaysFails, _platform="win32",
        )
    assert _AlwaysFails.calls == 2


def test_posix_spawn_is_unchanged(tmp_path: Path) -> None:
    ensure_aspect_worker_daemon(
        config_dir=tmp_path, tenant="default", _popen=_FakePopen, _platform="linux",
    )
    kwargs = _FakePopen.calls[0]["kwargs"]
    assert kwargs["start_new_session"] is True
    assert "creationflags" not in kwargs
    assert len(_FakePopen.calls) == 1


# ── SIGBREAK handler and the ticked wait ─────────────────────────────────────────


class _FakeSignalModule:
    def __init__(self, *, with_sigbreak: bool) -> None:
        self.SIGTERM = 15
        self.SIGINT = 2
        if with_sigbreak:
            self.SIGBREAK = 21
        self.registered: dict[int, Any] = {}

    def signal(self, signum: int, handler: Any) -> None:
        self.registered[signum] = handler


class _TickRecorder:
    """Stands in for the stop event: records each wait timeout, reports the
    stop set on the *n*th wait."""

    def __init__(self, set_on: int) -> None:
        self.timeouts: list[float | None] = []
        self._set_on = set_on
        self._set = False

    def wait(self, timeout: float | None = None) -> bool:
        self.timeouts.append(timeout)
        if len(self.timeouts) >= self._set_on:
            self._set = True
        return self._set

    def set(self) -> None:
        self._set = True

    def is_set(self) -> bool:
        return self._set


def _daemon(tmp_path: Path) -> AspectWorkerDaemon:
    return AspectWorkerDaemon(
        config_dir=tmp_path, tenant="default", worker_factory=MagicMock,
        queue_factory=MagicMock,
    )


def test_sigbreak_is_registered_with_the_same_handler_as_sigterm(tmp_path: Path) -> None:
    d = _daemon(tmp_path)
    mod = _FakeSignalModule(with_sigbreak=True)
    d._stop = _TickRecorder(set_on=1)  # type: ignore[assignment]
    d.run_until_signal(signal_module=mod)  # type: ignore[arg-type]
    # Non-vacuity: BREAK is registered in addition to the two POSIX signals.
    assert set(mod.registered) == {15, 2, 21}
    assert mod.registered[15] is mod.registered[2] is mod.registered[21]


def test_the_sigbreak_handler_releases_the_wait(tmp_path: Path) -> None:
    d = _daemon(tmp_path)
    mod = _FakeSignalModule(with_sigbreak=True)
    stopper = threading.Event()
    d._stop = stopper
    t = threading.Thread(target=d.run_until_signal, kwargs={"signal_module": mod}, daemon=True)
    t.start()
    for _ in range(500):
        if len(mod.registered) == 3:
            break
        threading.Event().wait(0.01)
    assert len(mod.registered) == 3
    assert not stopper.is_set()
    mod.registered[21](21, None)
    t.join(timeout=5)
    assert not t.is_alive()
    assert stopper.is_set()


def test_no_sigbreak_registration_where_the_platform_has_none(tmp_path: Path) -> None:
    d = _daemon(tmp_path)
    mod = _FakeSignalModule(with_sigbreak=False)
    d._stop = _TickRecorder(set_on=1)  # type: ignore[assignment]
    d.run_until_signal(signal_module=mod)  # type: ignore[arg-type]
    assert set(mod.registered) == {15, 2}


def test_the_main_thread_waits_in_ticks_no_longer_than_one_second(tmp_path: Path) -> None:
    d = _daemon(tmp_path)
    rec = _TickRecorder(set_on=5)
    d._stop = rec  # type: ignore[assignment]
    d.run_until_signal(signal_module=_FakeSignalModule(with_sigbreak=True))  # type: ignore[arg-type]
    # Five ticks, every one bounded: an unbounded wait() has timeout None.
    assert len(rec.timeouts) == 5
    assert all(t is not None and 0 < t <= 1.0 for t in rec.timeouts)
    assert awd._STOP_TICK_S <= DEFAULT_HEARTBEAT_INTERVAL <= 1.0


def test_run_aspect_worker_daemon_serves_through_run_until_signal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The entry point calls the ticked wait, not a bare Event.wait()."""
    # The real prologue chdirs to config_dir; keep that from leaking into the
    # rest of the worker (it turned develop's shard 1 red).
    monkeypatch.chdir(Path.cwd())
    calls: list[str] = []

    class _D:
        def __init__(self, **_: Any) -> None: ...
        def start(self) -> None: calls.append("start")
        def run_until_signal(self) -> None: calls.append("run")
        def stop(self) -> None: calls.append("stop")

    with patch.object(awd, "AspectWorkerDaemon", _D), \
            patch.object(awd, "_require_extraction_credentials"), \
            patch("nexus.logging_setup.configure_logging"):
        awd.run_aspect_worker_daemon(config_dir=tmp_path, tenant="default")
    assert calls == ["start", "run", "stop"]


# ── the restart path stops it through the shared primitive ───────────────────────


class _ConsoleApi:
    def __init__(self, *, refuse: bool = False) -> None:
        self.calls: list[tuple[str, int | None]] = []
        self._refuse = refuse

    def free_console(self) -> bool:
        self.calls.append(("free", None))
        return True

    def attach_console(self, pid: int) -> tuple[bool, int]:
        self.calls.append(("attach", pid))
        return (False, 5) if self._refuse else (True, 0)

    def generate_ctrl_break(self, pid: int) -> tuple[bool, int]:
        self.calls.append(("send", pid))
        return True, 0

    def attach_parent_console(self) -> tuple[bool, int]:
        self.calls.append(("parent", None))
        return True, 0

    def session_of(self, pid: int) -> int | None:
        return {4321: 1}.get(pid, 0)


_COMMAND = (
    "/Users/u/.local/share/uv/tools/conexus/bin/python3 "
    "/Users/u/.local/bin/nx daemon aspect-worker start"
)


def _report() -> Any:
    r = SkewReport(installed_version="7.0.0")
    r.stale = [StaleProcess(pid=4321, kind="aspect-worker", command="w", age_s=99)]
    return r


def _run_restart(api: _ConsoleApi, *, alive: list[bool]) -> tuple[list[str], MagicMock]:
    """restart_stale on a Windows-armed primitive with a fake console API.

    *alive* is the sequence ``_pid_alive`` returns; once it runs out the pid
    stays alive. The clock is a fake that advances 1 s per read, so the 12 s
    drain deadline passes without waiting."""
    ticks = iter(range(10_000))
    fake_time = SimpleNamespace(time=lambda: float(next(ticks)), sleep=lambda _s: None)

    windows_stop = functools.partial(request_graceful_stop, platform="win32", console_api=api)
    kill = MagicMock()
    alive_iter = iter(alive)
    with patch.object(uf, "request_graceful_stop", windows_stop), \
            patch_in((uf, "nexus.daemon.service_registry", "nexus.util.process_group"), "os.kill", kill), \
            patch.object(uf, "process_command", return_value=_COMMAND), \
            patch.object(uf, "_process_markers", return_value=("/uv/tools/conexus",)), \
            patch.object(uf, "_pid_alive", side_effect=lambda _p: next(alive_iter, True)), \
            patch.object(uf, "time", fake_time), \
            patch("nexus.daemon.aspect_worker_daemon.ensure_aspect_worker_daemon", return_value=True):
        actions = uf.restart_stale(_report())
    return actions, kill


def test_windows_restart_sends_ctrl_break_never_terminateprocess() -> None:
    api = _ConsoleApi()
    actions, kill = _run_restart(api, alive=[True, False])
    # The break went through the console attach, and os.kill (TerminateProcess
    # on Windows) was never called for the worker.
    assert ("send", 4321) in api.calls
    kill.assert_not_called()
    assert any("cycled aspect-worker" in a for a in actions), actions


def test_windows_restart_confirms_by_exit_not_by_the_send() -> None:
    api = _ConsoleApi()
    # Still alive for every poll: the send returned True and delivered nothing.
    actions, _ = _run_restart(api, alive=[])
    assert not any("cycled" in a for a in actions), actions
    assert any("still draining" in a for a in actions), actions


def test_windows_restart_refuses_a_cross_session_worker_and_kills_nothing() -> None:
    api = _ConsoleApi(refuse=True)
    actions, kill = _run_restart(api, alive=[])
    kill.assert_not_called()
    assert ("send", 4321) not in api.calls
    joined = " ".join(actions)
    assert "REFUSED" in joined or "another session" in joined, actions
    assert not any("cycled" in a for a in actions)


def test_posix_restart_still_sends_sigterm() -> None:
    calls: list[tuple[int, int]] = []

    def _kill(pid: int, sig: int) -> None:
        calls.append((pid, sig))

    posix_stop = functools.partial(request_graceful_stop, platform="linux")
    with patch.object(uf, "request_graceful_stop", posix_stop), \
            patch_in((uf, "nexus.daemon.service_registry", "nexus.util.process_group"), "os.kill", side_effect=_kill), \
            patch.object(uf, "process_command", return_value=_COMMAND), \
            patch.object(uf, "_process_markers", return_value=("/uv/tools/conexus",)), \
            patch.object(uf, "_pid_alive", return_value=False), \
            patch_time(uf, "sleep"), \
            patch("nexus.daemon.aspect_worker_daemon.ensure_aspect_worker_daemon", return_value=True):
        uf.restart_stale(_report())
    assert calls[0] == (4321, signal.SIGTERM)
