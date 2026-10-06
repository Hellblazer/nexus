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
import os
import subprocess
import sys
import threading
from collections.abc import Callable
from ctypes import wintypes
from pathlib import Path

import pytest

from nexus.daemon import session_end as se
from tests.daemon._children import CHILD_PYTHON
from tests.daemon._logs import info_logs

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

            with info_logs() as logs:
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


    # ── the in-process PostgreSQL stop against the real kernel ──

    def test_the_real_signal_pipe_call_delivers_one_byte_and_reads_the_echo(self) -> None:
        api = se.ctypes_pg_stop_api()
        received: list[int] = []
        server = _PipeServer(f"\\\\.\\pipe\\pgsignal_{os.getpid()}", received)
        server.start()
        try:
            assert server.ready.wait(5), "the in-test pipe server did not come up"
            delivered, error = api.send_signal(os.getpid(), se.PG_SIGNAL_FAST, 2000)
        finally:
            server.join(10)
        assert (delivered, error) == (True, 0)
        assert received == [se.PG_SIGNAL_FAST], "the pipe got exactly the one fast-shutdown byte"

    def test_a_missing_signal_pipe_is_reported_not_delivered(self) -> None:
        delivered, error = se.ctypes_pg_stop_api().send_signal(0x7FFFFFF0, se.PG_SIGNAL_FAST, 200)
        assert delivered is False and error != 0

    def test_the_real_wait_sees_a_live_process_then_its_exit(self) -> None:
        api = se.ctypes_pg_stop_api()
        child = subprocess.Popen([CHILD_PYTHON, "-c", "import time; time.sleep(60)"])  # noqa: S603
        try:
            assert api.wait_exit(child.pid, 0.2) is False, "a live process has not exited"
            child.kill()
            assert api.wait_exit(child.pid, 10) is True
        finally:
            child.kill()
            child.wait(10)
        assert api.wait_exit(child.pid, 1) is True, "a pid that no longer exists has exited"

    def test_the_real_image_identity_names_this_interpreter_and_nothing_for_a_dead_pid(self) -> None:
        api = se.ctypes_pg_stop_api()
        assert "python" in api.image_stem(os.getpid())
        child = subprocess.Popen([CHILD_PYTHON, "-c", "pass"])  # noqa: S603
        child.wait(10)
        assert api.image_stem(child.pid) == ""

    def test_the_whole_in_process_stop_end_to_end_with_a_stand_in_postmaster(
        self, tmp_path: Path,
    ) -> None:
        """A real child is the postmaster, a real named pipe is its signal pipe, and the
        server thread exits the child on receiving the byte, as a postmaster does on SIGINT.
        Only the image-name check is faked (the stand-in is python.exe, not postgres.exe);
        the pid file, the pipe write and the process wait are the real calls."""
        child = subprocess.Popen([CHILD_PYTHON, "-c", "import time; time.sleep(60)"])  # noqa: S603
        received: list[int] = []
        server = _PipeServer(f"\\\\.\\pipe\\pgsignal_{child.pid}", received, then=child.kill)
        try:
            (tmp_path / "postmaster.pid").write_text(f"{child.pid}\nC:/pgdata\n", encoding="utf-8")

            class _Api:
                def __init__(self) -> None:
                    self.real = se.ctypes_pg_stop_api()

                def image_stem(self, pid: int) -> str:
                    return "postgres"

                def send_signal(self, pid: int, signo: int, timeout_ms: int) -> tuple[bool, int]:
                    return self.real.send_signal(pid, signo, timeout_ms)

                def wait_exit(self, pid: int, timeout_s: float) -> bool:
                    return self.real.wait_exit(pid, timeout_s)

            server.start()
            assert server.ready.wait(5)
            stop = se.make_inprocess_pg_stopper(pgdata=str(tmp_path), api=_Api())
            with info_logs() as logs:
                assert stop(5.0) is True
            assert received == [se.PG_SIGNAL_FAST]
            assert child.wait(10) is not None
            names = [e["event"] for e in logs]
            assert names == ["session_end_pg_signal_sent", "session_end_pg_exited"]
        finally:
            child.kill()
            child.wait(10)
            server.join(10)

    def test_the_real_stop_refuses_a_pid_that_is_not_postgres(self, tmp_path: Path) -> None:
        # Real identity check: this very process is python, so a postmaster.pid naming it
        # must not be signalled. No pipe server exists; reaching the pipe would fail loudly.
        (tmp_path / "postmaster.pid").write_text(f"{os.getpid()}\n", encoding="utf-8")
        stop = se.make_inprocess_pg_stopper(pgdata=str(tmp_path))
        with info_logs() as logs:
            assert stop(2.0) is False
        assert [e["event"] for e in logs] == ["session_end_pg_pid_not_postgres"]


class _PipeServer(threading.Thread):
    """A one-shot named-pipe server shaped like the postmaster's signal pipe: read one byte,
    run *then*, echo the byte back."""

    def __init__(self, name: str, received: list[int], then: Callable[[], object] | None = None) -> None:
        super().__init__(daemon=True)
        self.pipe_name, self.received, self.then = name, received, then
        self.ready = threading.Event()

    def run(self) -> None:
        k = ctypes.WinDLL("kernel32", use_last_error=True)  # type: ignore[attr-defined]
        h = wintypes.HANDLE
        k.CreateNamedPipeW.argtypes = [
            wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, wintypes.DWORD,
            wintypes.DWORD, wintypes.DWORD, wintypes.DWORD, wintypes.LPVOID,
        ]
        k.CreateNamedPipeW.restype = h
        for name in ("ConnectNamedPipe",):
            getattr(k, name).argtypes = [h, wintypes.LPVOID]
            getattr(k, name).restype = wintypes.BOOL
        for name in ("ReadFile", "WriteFile"):
            getattr(k, name).argtypes = [
                h, wintypes.LPVOID, wintypes.DWORD, ctypes.POINTER(wintypes.DWORD), wintypes.LPVOID,
            ]
            getattr(k, name).restype = wintypes.BOOL
        for name in ("FlushFileBuffers", "DisconnectNamedPipe", "CloseHandle"):
            getattr(k, name).argtypes = [h]
            getattr(k, name).restype = wintypes.BOOL
        # PIPE_ACCESS_DUPLEX, PIPE_TYPE_MESSAGE | PIPE_READMODE_MESSAGE | PIPE_WAIT, one instance.
        handle = k.CreateNamedPipeW(self.pipe_name, 3, 6, 1, 16, 16, 0, None)
        if handle in (None, ctypes.c_void_p(-1).value):
            self.ready.set()
            return
        self.ready.set()
        try:
            k.ConnectNamedPipe(handle, None)  # 0 with ERROR_PIPE_CONNECTED if the client beat us
            buf, count = ctypes.c_ubyte(0), wintypes.DWORD(0)
            if k.ReadFile(handle, ctypes.byref(buf), 1, ctypes.byref(count), None):
                self.received.append(buf.value)
                if self.then is not None:
                    self.then()
                k.WriteFile(handle, ctypes.byref(buf), 1, ctypes.byref(count), None)
                k.FlushFileBuffers(handle)
            k.DisconnectNamedPipe(handle)
        finally:
            k.CloseHandle(handle)


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
