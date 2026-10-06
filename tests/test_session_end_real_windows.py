# SPDX-License-Identifier: AGPL-3.0-or-later
"""The session-end console-control handler against the real kernel32
(RDR-224, nexus-f9bgu.51).

``tests/daemon/test_session_end.py`` proves the stop logic on every host with every OS
touchpoint faked. This file is what only a Windows run can show: ``SetConsoleCtrlHandler``
accepts the ctypes callback, the callback object survives in the module-level reference
table while registered, the trampoline the OS would call maps an owned event to ``TRUE`` and
any other to ``FALSE``, and unregistering removes it. It raises no real console event (that
would end the test run); the callback is invoked directly, the way the OS would call it.
The real sign-out and restart are verified on a guest by hand (see the bead).
"""
from __future__ import annotations

import ctypes
import sys
import threading
from ctypes import wintypes

import pytest
import structlog.testing

from nexus.daemon import session_end as se

pytestmark = pytest.mark.skipif(sys.platform != "win32", reason="SetConsoleCtrlHandler is Windows-only")


class _Steps:
    def __init__(self) -> None:
        self.calls: list[str] = []
        self.stop = threading.Event()

    def handler(self) -> se.SessionEndHandler:
        return se.SessionEndHandler(
            stop_requested=self.stop,
            stop_engine=lambda _b: self.calls.append("engine") or True,
            stop_pg=lambda _b: self.calls.append("pg") or True,
            mark_stop=lambda: self.calls.append("mark"),
        )


class TestRealWindows:
    def test_the_real_registrar_registers_and_unregisters_the_callback(self) -> None:
        assert sys.platform == "win32", "non-vacuity: this class must run on Windows"
        steps = _Steps()
        handler = steps.handler()
        registrar = se.ctypes_ctrl_registrar()
        assert registrar.register(handler) is True
        try:
            assert id(handler) in se._LIVE_CALLBACKS, "the OS holds this object; it must stay referenced"
        finally:
            assert registrar.unregister(handler) is True
        assert id(handler) not in se._LIVE_CALLBACKS
        assert registrar.unregister(handler) is False  # already gone

    def test_the_registered_callback_handles_a_logoff_and_declines_ctrl_c(self) -> None:
        steps = _Steps()
        handler = steps.handler()
        registrar = se.ctypes_ctrl_registrar()
        assert registrar.register(handler) is True
        try:
            cfunc = se._LIVE_CALLBACKS[id(handler)]
            assert cfunc(0) == 0 and cfunc(1) == 0, "CTRL_C and CTRL_BREAK stay with CPython"
            assert steps.calls == [] and not steps.stop.is_set()
            assert cfunc(se.CTRL_LOGOFF_EVENT) == 1
            assert steps.calls == ["mark", "engine", "pg"]
            assert steps.stop.is_set()
            assert cfunc(se.CTRL_SHUTDOWN_EVENT) == 1  # the usual second event
            assert steps.calls == ["mark", "engine", "pg"], "nothing is stopped twice"
        finally:
            registrar.unregister(handler)

    def test_install_session_end_handler_round_trips_with_the_default_registrar(self) -> None:
        handler = _Steps().handler()
        uninstall = se.install_session_end_handler(handler)
        assert id(handler) in se._LIVE_CALLBACKS, "the real install registered the callback"
        uninstall()
        assert id(handler) not in se._LIVE_CALLBACKS
        uninstall()  # idempotent

    # ── the hidden top-level window: the mechanism that fires for logoff and shutdown ──

    def test_the_real_window_answers_query_and_runs_the_stop_on_end_session(self) -> None:
        assert sys.platform == "win32", "non-vacuity: this class must run on Windows"
        user32 = _user32()
        steps = _Steps()
        uninstall = se.install_session_end_window(steps.handler())
        try:
            hwnd = user32.FindWindowW(se.WINDOW_CLASS_NAME, None)
            assert hwnd, "the installed window must be findable by its class name"

            # Top-level and hidden. A message-only window (parent HWND_MESSAGE) would never be
            # sent the broadcast WM_QUERYENDSESSION / WM_ENDSESSION.
            assert user32.GetAncestor(hwnd, _GA_PARENT) == user32.GetDesktopWindow()
            assert not user32.IsWindowVisible(hwnd)

            assert user32.SendMessageW(hwnd, se.WM_QUERYENDSESSION, 0, 0) == 1
            assert steps.calls == [], "a query only asks; nothing is stopped"

            # A cancelled session end runs nothing.
            assert user32.SendMessageW(hwnd, se.WM_ENDSESSION, 0, se.ENDSESSION_LOGOFF) == 0
            assert steps.calls == [] and not steps.stop.is_set()

            with structlog.testing.capture_logs() as logs:
                assert user32.SendMessageW(hwnd, se.WM_ENDSESSION, 1, se.ENDSESSION_LOGOFF) == 0
            assert steps.calls == ["mark", "engine", "pg"]
            assert steps.stop.is_set()
            names = [entry["event"] for entry in logs]
            assert "session_end_begin" in names and "session_end_done" in names
            assert names.count("session_end_step") == 2
            message = next(e for e in logs if e["event"] == "session_end_window_message")
            assert message["logoff"] is True

            # The console event that follows must not stop anything again.
            user32.SendMessageW(hwnd, se.WM_ENDSESSION, 1, 0)
            assert steps.calls == ["mark", "engine", "pg"]
        finally:
            uninstall()
        assert not user32.FindWindowW(se.WINDOW_CLASS_NAME, None), "uninstall destroys the window"
        uninstall()  # idempotent

    def test_the_real_window_treats_no_logoff_flag_as_a_shutdown(self) -> None:
        user32 = _user32()
        seen: list[int] = []
        uninstall = se.install_session_end_window(lambda event: seen.append(event) or True)
        try:
            hwnd = user32.FindWindowW(se.WINDOW_CLASS_NAME, None)
            assert hwnd
            assert user32.SendMessageW(hwnd, se.WM_ENDSESSION, 1, 0) == 0
        finally:
            uninstall()
        assert seen == [se.CTRL_SHUTDOWN_EVENT]


_GA_PARENT = 1


def _user32():  # noqa: ANN202 — a ctypes WinDLL, Windows only
    user32 = ctypes.WinDLL("user32", use_last_error=True)  # type: ignore[attr-defined]
    user32.FindWindowW.argtypes = [wintypes.LPCWSTR, wintypes.LPCWSTR]
    user32.FindWindowW.restype = wintypes.HWND
    user32.SendMessageW.argtypes = [wintypes.HWND, wintypes.UINT, ctypes.c_size_t, ctypes.c_ssize_t]
    user32.SendMessageW.restype = ctypes.c_ssize_t
    user32.GetAncestor.argtypes = [wintypes.HWND, wintypes.UINT]
    user32.GetAncestor.restype = wintypes.HWND
    user32.GetDesktopWindow.restype = wintypes.HWND
    user32.IsWindowVisible.argtypes = [wintypes.HWND]
    user32.IsWindowVisible.restype = wintypes.BOOL
    return user32
