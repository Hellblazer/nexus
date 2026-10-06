# SPDX-License-Identifier: AGPL-3.0-or-later
"""The hidden-window path of the Windows session-end stop (RDR-224, nexus-f9bgu.51 round 2).

A console process that has loaded user32 (the supervisor interpreter has: USER32, GDI32 and
win32u are loaded, measured on a guest) is not sent ``CTRL_LOGOFF_EVENT`` /
``CTRL_SHUTDOWN_EVENT``; Windows sends ``WM_QUERYENDSESSION`` / ``WM_ENDSESSION`` to its
top-level windows instead. So a hidden top-level window is the primary mechanism and the
console handler (``tests/daemon/test_session_end.py``) stays for ``CTRL_CLOSE``. The window
backend is injected, so all of this runs on every host.
"""
from __future__ import annotations

import threading
import time
from pathlib import Path
from typing import Any

import pytest

from nexus.daemon import session_end as se
from nexus.daemon import storage_service_daemon as ssd
from tests.daemon.test_session_end import (
    _Clock,
    _FakeJob,
    _FakeRegistrar,
    _handler,
    _Recorder,
    _supervisor,
)


class _Events:
    """A stand-in for the shared SessionEndHandler: records the console event it was given."""

    def __init__(self) -> None:
        self.events: list[int] = []

    def __call__(self, event: int) -> bool:
        self.events.append(event)
        return True


class _FakeWindowBackend:
    """Runs no OS window. ``run_message_loop`` records the dispatch the real window procedure
    would call, reports ready, and blocks until closed, like a real message loop."""

    def __init__(self, *, ready: bool = True) -> None:
        self.ready = ready
        self.order: list[str] = []
        self.dispatch: Any = None
        self.closed = threading.Event()
        self.close_requests = 0
        self.levels: list[int] = []

    def set_shutdown_level(self, level: int) -> None:
        self.order.append("level")
        self.levels.append(level)

    def run_message_loop(self, dispatch: Any, ready: Any) -> None:
        self.order.append("window")
        self.dispatch = dispatch
        ready(self.ready)
        if self.ready:
            self.closed.wait(timeout=10)

    def request_close(self) -> None:
        self.close_requests += 1
        self.closed.set()


# ── the message vocabulary ───────────────────────────────────────────────────────


def test_the_window_message_constants_are_the_win32_values() -> None:
    assert (se.WM_QUERYENDSESSION, se.WM_ENDSESSION, se.WM_CLOSE) == (0x0011, 0x0016, 0x0010)
    assert se.ENDSESSION_LOGOFF == 0x80000000
    assert se.SHUTDOWN_LEVEL == 0x3FF


def test_query_end_session_allows_the_session_end_and_stops_nothing() -> None:
    ev = _Events()
    h = se.SessionEndMessageHandler(ev)
    assert h(se.WM_QUERYENDSESSION, 0, 0) == 1
    assert h(se.WM_QUERYENDSESSION, 0, se.ENDSESSION_LOGOFF) == 1
    assert ev.events == [], "the session may still be cancelled; nothing is stopped yet"


def test_end_session_with_the_logoff_flag_runs_the_handler_as_a_logoff() -> None:
    ev = _Events()
    assert se.SessionEndMessageHandler(ev)(se.WM_ENDSESSION, 1, se.ENDSESSION_LOGOFF) == 0
    assert ev.events == [se.CTRL_LOGOFF_EVENT]


def test_end_session_without_the_logoff_flag_is_a_shutdown() -> None:
    ev = _Events()
    assert se.SessionEndMessageHandler(ev)(se.WM_ENDSESSION, 1, 0) == 0
    assert ev.events == [se.CTRL_SHUTDOWN_EVENT]


def test_other_lparam_bits_do_not_change_the_event_and_a_signed_lparam_is_masked() -> None:
    ev = _Events()
    h = se.SessionEndMessageHandler(ev)
    h(se.WM_ENDSESSION, 1, se.ENDSESSION_LOGOFF | 0x1)  # ENDSESSION_CLOSEAPP
    h(se.WM_ENDSESSION, 1, 0x40000000)  # ENDSESSION_CRITICAL: a shutdown
    h(se.WM_ENDSESSION, 1, -0x80000000)  # a 32-bit LPARAM with the top bit set reads negative
    assert ev.events == [se.CTRL_LOGOFF_EVENT, se.CTRL_SHUTDOWN_EVENT, se.CTRL_LOGOFF_EVENT]


def test_a_cancelled_session_end_runs_nothing() -> None:
    ev = _Events()
    assert se.SessionEndMessageHandler(ev)(se.WM_ENDSESSION, 0, se.ENDSESSION_LOGOFF) == 0
    assert ev.events == []


def test_messages_the_window_does_not_own_fall_through_to_the_default_procedure() -> None:
    h = se.SessionEndMessageHandler(_Events())
    for msg in (0x0000, se.WM_CLOSE, 0x0002, 0x0113, 0x0400):
        assert h(msg, 0, 0) is None


def test_a_raising_stop_never_escapes_the_window_procedure() -> None:
    def boom(_e: int) -> bool:
        raise RuntimeError("stop broke")

    assert se.SessionEndMessageHandler(boom)(se.WM_ENDSESSION, 1, 0) == 0


def test_the_window_and_the_console_handler_share_one_stop() -> None:
    clock = _Clock()
    rec = _Recorder(clock)
    h = _handler(rec, clock)
    window = se.SessionEndMessageHandler(h)
    assert window(se.WM_ENDSESSION, 1, se.ENDSESSION_LOGOFF) == 0
    assert h(se.CTRL_SHUTDOWN_EVENT) is True  # the console event that may follow
    assert rec.names() == ["engine", "pg"] and rec.marked == 1


# ── installing the window ────────────────────────────────────────────────────────


def test_install_raises_the_shutdown_priority_before_creating_the_window() -> None:
    be = _FakeWindowBackend()
    uninstall = se.install_session_end_window(_Events(), backend=be, platform="win32")
    try:
        assert be.order == ["level", "window"]
        assert be.levels == [0x3FF]
    finally:
        uninstall()


def test_the_installed_window_dispatches_to_the_shared_handler() -> None:
    be = _FakeWindowBackend()
    ev = _Events()
    uninstall = se.install_session_end_window(ev, backend=be, platform="win32")
    try:
        assert be.dispatch(se.WM_QUERYENDSESSION, 0, 0) == 1
        assert be.dispatch(se.WM_ENDSESSION, 1, se.ENDSESSION_LOGOFF) == 0
        assert ev.events == [se.CTRL_LOGOFF_EVENT]
    finally:
        uninstall()


def test_uninstall_closes_the_window_and_joins_its_thread_once() -> None:
    be = _FakeWindowBackend()
    before = threading.active_count()
    uninstall = se.install_session_end_window(_Events(), backend=be, platform="win32")
    assert threading.active_count() == before + 1
    uninstall()
    uninstall()  # idempotent
    assert be.close_requests == 1
    assert threading.active_count() == before


def test_a_window_that_cannot_be_created_degrades_to_a_no_op_uninstall() -> None:
    be = _FakeWindowBackend(ready=False)
    uninstall = se.install_session_end_window(_Events(), backend=be, platform="win32")
    uninstall()
    assert be.close_requests == 0


def test_a_backend_that_cannot_be_built_degrades_to_a_no_op(monkeypatch: pytest.MonkeyPatch) -> None:
    def boom() -> Any:
        raise OSError("no user32")

    monkeypatch.setattr(se, "ctypes_window_backend", boom)
    se.install_session_end_window(_Events(), platform="win32")()


def test_a_message_loop_that_dies_before_reporting_ready_does_not_hang_the_install() -> None:
    class Dies(_FakeWindowBackend):
        def run_message_loop(self, dispatch: Any, ready: Any) -> None:
            raise OSError("RegisterClassExW failed")

    t0 = time.monotonic()
    uninstall = se.install_session_end_window(
        _Events(), backend=Dies(), platform="win32", ready_timeout_s=5.0,
    )
    uninstall()
    assert time.monotonic() - t0 < 4.0, "a dead loop releases the install at once, not at the timeout"


def test_install_is_a_no_op_off_windows_and_never_builds_a_backend(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def boom() -> Any:
        raise AssertionError("POSIX must not construct the window backend")

    monkeypatch.setattr(se, "ctypes_window_backend", boom)
    se.install_session_end_window(_Events(), platform="linux")()


# ── wiring in the supervisor ─────────────────────────────────────────────────────


def test_the_wiring_on_windows_installs_both_mechanisms_and_uninstalls_both(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    sup = _supervisor(tmp_path, _FakeJob(None))
    monkeypatch.setattr(ssd, "_session_end_pg_stopper", lambda creds: (lambda b: True))
    monkeypatch.setattr(ssd, "_session_end_write_marker", lambda cd: None)
    reg = _FakeRegistrar()
    be = _FakeWindowBackend()
    stop = threading.Event()
    uninstall = ssd._install_session_end_handler(
        sup, stop, tmp_path, {"PG_DATA": "/x"},
        registrar=reg, window_backend=be, platform="win32",
    )
    assert len(reg.registered) == 1 and be.dispatch is not None
    # One shared handler behind both: the window's ENDSESSION runs the stop,
    # and the console event that follows it does not run it again.
    assert be.dispatch(se.WM_ENDSESSION, 1, se.ENDSESSION_LOGOFF) == 0
    assert stop.is_set()
    assert reg.registered[0](se.CTRL_SHUTDOWN_EVENT) is True
    uninstall()
    assert reg.unregistered == reg.registered and be.close_requests == 1


def test_the_wiring_on_posix_builds_no_window(tmp_path: Path) -> None:
    sup = _supervisor(tmp_path, _FakeJob(None))
    be = _FakeWindowBackend()
    ssd._install_session_end_handler(
        sup, threading.Event(), tmp_path, {"PG_DATA": "/x"},
        registrar=_FakeRegistrar(), window_backend=be, platform="linux",
    )()
    assert be.dispatch is None
