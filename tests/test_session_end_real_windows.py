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

import sys
import threading

import pytest

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
