# SPDX-License-Identifier: AGPL-3.0-or-later
"""Clean stop at Windows session end (RDR-224, nexus-f9bgu.51).

A sign-out, restart or shutdown used to hard-kill the native engine and crash
PostgreSQL: no fast shutdown, a stale ``postmaster.pid``, WAL crash recovery at the
next start (measured on a Hyper-V guest, T2 ``nexus_rdr/224-f9bgu46-session-end-
measurement``). The supervisor owns a hidden console (``CREATE_NO_WINDOW``), and
session end reaches a console process as ``CTRL_LOGOFF_EVENT`` / ``CTRL_SHUTDOWN_EVENT``
(``CTRL_CLOSE_EVENT`` for a closing console). CPython maps none of them to a signal
(only ``CTRL_C`` and ``CTRL_BREAK``), so this module installs a ``SetConsoleCtrlHandler``
callback for the three.

The callback runs on a system thread, and Windows ends the process a few seconds after
it returns (about 5 s; this module budgets 4). So the callback does the whole stop
itself, synchronously: mark the stop for the launcher, set the supervisor's stop flag,
stop the engine (``CTRL_BREAK`` through the existing stop channel, a short bounded
wait), then ``pg_ctl stop -m fast -w``. It returns ``True`` for the three events it owns
and ``False`` for everything else, so ``CTRL_C`` / ``CTRL_BREAK`` stay with the signal
path in ``storage_service_daemon._install_stop_handlers``.

Not in ``service_registry.py`` on purpose (``nexus.daemon`` AGENTS.md, the standing
gate): that file owns lease discovery, election, fencing and version skew. This is
supervisor-process behaviour of the one tier that owns PostgreSQL; the lease half of a
stop (``mark_shutting_down`` / ``relinquish``) is unchanged and stays in
``StorageServiceSupervisor.stop``. The stop marker the callback writes IS the shared
primitive's (``service_registry.write_stop_marker``).

THE CONSOLE HANDLER ALONE DOES NOT FIRE for logoff or shutdown (measured on a Hyper-V
guest, round 1: installed, never called, engine killed, PostgreSQL crashed). The
supervisor interpreter has ``USER32.dll``, ``GDI32.dll`` and ``win32u.dll`` loaded, and
Windows does not deliver ``CTRL_LOGOFF_EVENT`` / ``CTRL_SHUTDOWN_EVENT`` to a console
process that loaded user32: it sends ``WM_QUERYENDSESSION`` / ``WM_ENDSESSION`` to the
process's top-level windows instead. So the PRIMARY mechanism is a hidden top-level
window on a dedicated daemon thread (:func:`install_session_end_window`). It must be
top-level, not ``HWND_MESSAGE``: message-only windows are not sent the broadcast. The
console handler stays, harmless, and still covers ``CTRL_CLOSE_EVENT``. Both feed ONE
:class:`SessionEndHandler`, so whichever arrives first does the stop and the other waits
and returns.

``SetProcessShutdownParameters(0x3FF, 0)`` runs first. Windows notifies processes at
shutdown highest level first; the default is 0x280 and 0x300..0x3FF is the application
range notified before it, so 0x3FF puts this supervisor ahead of the processes it has to
stop first (the engine and the postmaster keep the default) and ahead of anything that
would otherwise be terminated before the notification. That ordering is from the
documented level ranges, not measured on a guest; the guest run is what shows it.

Every OS touchpoint is injectable (registrar, window backend, the two stoppers, the
clock), so the logic is tested on every host. Nothing here imports a Windows-only module
at import time.
"""
from __future__ import annotations

import subprocess
import sys
import threading
import time
from collections.abc import Callable
from typing import Any, Protocol

import structlog

_log = structlog.get_logger(__name__)

CTRL_CLOSE_EVENT: int = 2
CTRL_LOGOFF_EVENT: int = 5
CTRL_SHUTDOWN_EVENT: int = 6

#: The console control events this handler owns. ``CTRL_C_EVENT`` (0) and
#: ``CTRL_BREAK_EVENT`` (1) are deliberately absent: they stay with CPython's own handler.
OWNED_EVENTS: frozenset[int] = frozenset(
    {CTRL_CLOSE_EVENT, CTRL_LOGOFF_EVENT, CTRL_SHUTDOWN_EVENT}
)

#: The whole callback, from entry to return. Windows terminates the process about 5 s
#: after a console handler is invoked for logoff or shutdown; 1 s of margin.
HANDLER_BUDGET_S: float = 4.0

#: The engine's share: a ``CTRL_BREAK`` and a short wait. The engine keeps no state of its
#: own beyond PostgreSQL, so it is the part that may be cut short. After this a job-object
#: kill takes it (instant).
ENGINE_BUDGET_S: float = 2.0

#: PostgreSQL's floor. A fast shutdown with its checkpoint measured about 1.2 s; the stop
#: that leaves a clean ``pg.log`` is the reason this handler exists, so it is never given
#: less than this however long the engine took.
PG_MIN_BUDGET_S: float = 1.0

#: ``pg_ctl``'s backstop sits this far above its own ``-t`` so pg_ctl reports its own
#: timeout before this module kills it.
_PG_BACKSTOP_SLACK_S: float = 0.5


class CtrlHandlerRegistrar(Protocol):
    """The OS registration seam (``SetConsoleCtrlHandler``)."""

    def register(self, callback: Callable[[int], bool]) -> bool: ...

    def unregister(self, callback: Callable[[int], bool]) -> bool: ...


class SessionEndHandler:
    """The console-control callback: stop the engine and PostgreSQL once, in time.

    Callable as ``handler(event) -> bool``, the shape ``SetConsoleCtrlHandler`` wants.
    *stop_engine* and *stop_pg* take a budget in seconds and return whether the thing
    stopped; neither is allowed to decide the return value, which is always ``True`` for
    an owned event (``False`` would hand the event to the default handler, which
    terminates the process, strictly worse than returning after a failed stop).
    """

    def __init__(
        self,
        *,
        stop_requested: threading.Event,
        stop_engine: Callable[[float], bool],
        stop_pg: Callable[[float], bool],
        mark_stop: Callable[[], Any],
        clock: Callable[[], float] = time.monotonic,
        budget_s: float = HANDLER_BUDGET_S,
        engine_budget_s: float = ENGINE_BUDGET_S,
        pg_min_budget_s: float = PG_MIN_BUDGET_S,
        flush: Callable[[], Any] | None = None,
    ) -> None:
        #: Called after the steps and before returning: the process is terminated shortly
        #: after, and a buffered ``session_end_done`` line is the evidence this ran.
        self._flush = flush
        self._stop_requested = stop_requested
        self._stop_engine = stop_engine
        self._stop_pg = stop_pg
        self._mark_stop = mark_stop
        self._clock = clock
        self._budget_s = budget_s
        self._engine_budget_s = engine_budget_s
        self._pg_min_budget_s = pg_min_budget_s
        self._lock = threading.Lock()
        self._started = False
        self._done = threading.Event()

    def __call__(self, event: int) -> bool:
        if event not in OWNED_EVENTS:
            return False
        with self._lock:
            first = not self._started
            self._started = True
        if not first:
            # A LOGOFF is routinely followed by a SHUTDOWN, and two threads can be in here.
            # One stop runs; the others wait for it (bounded) and return.
            self._done.wait(timeout=self._budget_s)
            _log.info("session_end_repeat_event", ctrl_event=event, finished=self._done.is_set())
            return True
        try:
            self._run(event)
        finally:
            if self._flush is not None:
                try:
                    self._flush()
                except Exception as exc:  # noqa: BLE001 — a flush that fails must not mask the stop
                    _log.warning("session_end_flush_failed", error=str(exc))
            self._done.set()
        return True

    def _run(self, event: int) -> None:
        deadline = self._clock() + self._budget_s
        _log.info("session_end_begin", ctrl_event=event, budget_s=self._budget_s)

        # The launcher must not respawn a supervisor that is going away on purpose.
        try:
            self._mark_stop()
        except Exception as exc:  # noqa: BLE001 — a marker that cannot be written must not stop the stop
            _log.warning("session_end_mark_failed", error=str(exc))
        self._stop_requested.set()

        engine_budget = max(0.0, min(self._engine_budget_s, deadline - self._clock()))
        engine_stopped = self._guarded("engine", self._stop_engine, engine_budget)

        pg_budget = max(self._pg_min_budget_s, deadline - self._clock())
        pg_stopped = self._guarded("pg", self._stop_pg, pg_budget)

        _log.info(
            "session_end_done",
            ctrl_event=event,
            engine_stopped=engine_stopped,
            pg_stopped=pg_stopped,
            elapsed_s=round(self._budget_s - (deadline - self._clock()), 3),
        )

    @staticmethod
    def _guarded(name: str, stop: Callable[[float], bool], budget: float) -> bool:
        try:
            ok = bool(stop(budget))
        except Exception as exc:  # noqa: BLE001 — each step is independent; a raise must not skip the next
            _log.warning("session_end_step_failed", step=name, error=str(exc))
            return False
        _log.info("session_end_step", step=name, stopped=ok, budget_s=round(budget, 3))
        return ok


def make_pg_stopper(
    *,
    pg_ctl: str,
    pgdata: str,
    run: Callable[[list[str], float], int],
) -> Callable[[float], bool]:
    """``pg_ctl -D <pgdata> -m fast -w -t N stop`` as a ``stop(budget) -> bool``.

    *run* executes the argv with a hard timeout and returns the exit code (the seam: tests
    pass a fake, the supervisor passes ``pg_provision._run``). ``-t`` is whole seconds
    and never above the budget; the hard timeout sits just above it. A non-zero exit
    (including "server is not running") or a timeout is "not stopped", never an error.
    """

    def stop(budget_s: float) -> bool:
        wait_s = max(1, int(budget_s))
        cmd = [pg_ctl, "-D", pgdata, "-m", "fast", "-w", "-t", str(wait_s), "stop"]
        try:
            code = run(cmd, wait_s + _PG_BACKSTOP_SLACK_S)
        except subprocess.TimeoutExpired:
            _log.warning("session_end_pg_stop_timeout", wait_s=wait_s)
            return False
        if code != 0:
            _log.warning("session_end_pg_stop_nonzero", exit_code=code)
        return code == 0

    return stop


#: Strong references to the ctypes callbacks the OS holds. A ``WINFUNCTYPE`` object that
#: is collected while registered turns the next event into a call through freed memory.
#: Keyed by the Python callable, so unregister can find the exact object it registered.
_LIVE_CALLBACKS: dict[int, Any] = {}


class _CtypesRegistrar:
    """``SetConsoleCtrlHandler`` through ctypes. Windows only; built lazily."""

    def __init__(self) -> None:
        import ctypes  # noqa: PLC0415 — deferred import — Windows-only types are touched only here
        from ctypes import wintypes  # noqa: PLC0415 — deferred import — Windows-only

        self._proto = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.DWORD)  # type: ignore[attr-defined]
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)  # type: ignore[attr-defined]
        kernel32.SetConsoleCtrlHandler.argtypes = [self._proto, wintypes.BOOL]
        kernel32.SetConsoleCtrlHandler.restype = wintypes.BOOL
        self._set = kernel32.SetConsoleCtrlHandler

    def register(self, callback: Callable[[int], bool]) -> bool:
        def trampoline(event: int) -> int:
            try:
                return 1 if callback(int(event)) else 0
            except Exception:  # noqa: BLE001 — an exception out of a ctypes callback is printed and returns FALSE
                _log.exception("session_end_callback_raised", ctrl_event=int(event))
                return 1 if int(event) in OWNED_EVENTS else 0

        cfunc = self._proto(trampoline)
        _LIVE_CALLBACKS[id(callback)] = cfunc
        ok = bool(self._set(cfunc, True))
        if not ok:
            _LIVE_CALLBACKS.pop(id(callback), None)
        return ok

    def unregister(self, callback: Callable[[int], bool]) -> bool:
        cfunc = _LIVE_CALLBACKS.pop(id(callback), None)
        if cfunc is None:
            return False
        return bool(self._set(cfunc, False))


def ctypes_ctrl_registrar() -> CtrlHandlerRegistrar:
    """The real registrar. Raises off Windows (``WINFUNCTYPE`` does not exist there)."""
    return _CtypesRegistrar()


def install_session_end_handler(
    handler: SessionEndHandler,
    *,
    registrar: CtrlHandlerRegistrar | None = None,
    platform: str | None = None,
) -> Callable[[], None]:
    """Register *handler* for the console control events; return an idempotent uninstall.

    A no-op off Windows (the registrar is never built there). A registration the OS
    refuses is logged and degrades to the pre-existing behaviour (no clean session-end
    stop); it never fails the supervisor.
    """
    if (platform if platform is not None else sys.platform) != "win32":
        return _noop
    try:
        reg = registrar if registrar is not None else ctypes_ctrl_registrar()
        registered = reg.register(handler)
    except Exception as exc:  # noqa: BLE001 — degrade, never fail the supervisor over this
        _log.warning("session_end_handler_unavailable", error=str(exc))
        return _noop
    if not registered:
        _log.warning("session_end_handler_refused")
        return _noop
    _log.info("session_end_handler_installed", events=sorted(OWNED_EVENTS))
    uninstalled = False

    def uninstall() -> None:
        nonlocal uninstalled
        if uninstalled:
            return
        uninstalled = True
        try:
            reg.unregister(handler)
        except Exception as exc:  # noqa: BLE001 — teardown must not raise
            _log.warning("session_end_handler_unregister_failed", error=str(exc))

    return uninstall


def _noop() -> None:
    return None


# ── The hidden top-level window (the primary mechanism) ──────────────────────────

WM_CLOSE: int = 0x0010
WM_DESTROY: int = 0x0002
WM_QUERYENDSESSION: int = 0x0011
WM_ENDSESSION: int = 0x0016

#: ``WM_ENDSESSION``'s lParam bit for a logoff (clear: a shutdown or restart).
ENDSESSION_LOGOFF: int = 0x80000000

#: ``SetProcessShutdownParameters`` level: the top of the application range that is
#: notified before the default 0x280. See the module docstring.
SHUTDOWN_LEVEL: int = 0x3FF

#: The window class. One supervisor per user, so a fixed name is enough, and a test (or a
#: debugger) can find the window with ``FindWindowW``.
WINDOW_CLASS_NAME: str = "NexusStorageSupervisorSessionEnd"

#: How long the install waits for the window thread to report. Creating a window takes
#: milliseconds; this only bounds a wedged ``CreateWindowExW``.
WINDOW_READY_TIMEOUT_S: float = 5.0

#: How long uninstall waits for the window thread after asking it to close.
WINDOW_JOIN_TIMEOUT_S: float = 2.0


class SessionEndMessageHandler:
    """The window procedure's logic, with no OS in it: ``handler(msg, wparam, lparam)``.

    Returns the ``LRESULT`` for a message it owns and ``None`` for one it does not (the
    backend then calls ``DefWindowProcW``).

    * ``WM_QUERYENDSESSION``: ``1``, the session may end. Nothing is stopped yet; the end
      can still be cancelled.
    * ``WM_ENDSESSION`` with ``wParam != 0``: the session IS ending. Run *stop* (the shared
      :class:`SessionEndHandler`) synchronously with ``CTRL_LOGOFF_EVENT`` when lParam has
      :data:`ENDSESSION_LOGOFF`, else ``CTRL_SHUTDOWN_EVENT``, then return ``0``.
    * ``WM_ENDSESSION`` with ``wParam == 0``: the end was cancelled; do nothing.
    """

    def __init__(self, stop: Callable[[int], bool]) -> None:
        self._stop = stop

    def __call__(self, msg: int, wparam: int, lparam: int) -> int | None:
        if msg == WM_QUERYENDSESSION:
            return 1
        if msg != WM_ENDSESSION:
            return None
        if wparam == 0:
            _log.info("session_end_cancelled")
            return 0
        # lParam is a signed LPARAM: a 32-bit one with the top bit set reads negative.
        logoff = bool((lparam & 0xFFFFFFFF) & ENDSESSION_LOGOFF)
        ctrl_event = CTRL_LOGOFF_EVENT if logoff else CTRL_SHUTDOWN_EVENT
        _log.info("session_end_window_message", logoff=logoff, lparam=lparam & 0xFFFFFFFF)
        try:
            self._stop(ctrl_event)
        except Exception as exc:  # noqa: BLE001 — an exception must not leave the window procedure
            _log.warning("session_end_window_stop_failed", error=str(exc))
        return 0


class WindowBackend(Protocol):
    """The OS window seam."""

    def set_shutdown_level(self, level: int) -> None: ...

    def run_message_loop(
        self, dispatch: Callable[[int, int, int], int | None], ready: Callable[[bool], None],
    ) -> None:
        """On the CALLING thread: create the window, call ``ready(True)`` (``ready(False)`` if
        it could not be created), pump messages until the window is closed, clean up."""
        ...

    def request_close(self) -> None:
        """From any thread: make the message loop end."""
        ...


def install_session_end_window(
    stop: Callable[[int], bool],
    *,
    backend: WindowBackend | None = None,
    platform: str | None = None,
    ready_timeout_s: float = WINDOW_READY_TIMEOUT_S,
    join_timeout_s: float = WINDOW_JOIN_TIMEOUT_S,
) -> Callable[[], None]:
    """Start the hidden window thread for *stop*; return an idempotent uninstall.

    A no-op off Windows (the backend is never built there). The shutdown priority is raised
    before the window exists. Any failure (no backend, the window cannot be created, a
    loop that dies before it reports) is logged and degrades to a no-op: the supervisor
    still runs, with only the console handler.
    """
    if (platform if platform is not None else sys.platform) != "win32":
        return _noop
    try:
        be = backend if backend is not None else ctypes_window_backend()
    except Exception as exc:  # noqa: BLE001 — degrade, never fail the supervisor over this
        _log.warning("session_end_window_unavailable", error=str(exc))
        return _noop
    try:
        be.set_shutdown_level(SHUTDOWN_LEVEL)
    except Exception as exc:  # noqa: BLE001 — a refused priority still leaves a working window
        _log.warning("session_end_shutdown_level_failed", error=str(exc))

    dispatch = SessionEndMessageHandler(stop)
    outcome: list[bool] = []
    reported = threading.Event()

    def ready(ok: bool) -> None:
        outcome.append(ok)
        reported.set()

    def thread_main() -> None:
        try:
            be.run_message_loop(dispatch, ready)
        except Exception as exc:  # noqa: BLE001 — the thread must not die with a traceback on stderr
            _log.warning("session_end_window_loop_failed", error=str(exc))
        finally:
            reported.set()  # a loop that died before reporting must not hold the install

    thread = threading.Thread(target=thread_main, name="session-end-window", daemon=True)
    thread.start()
    if not reported.wait(timeout=ready_timeout_s) or not outcome or not outcome[0]:
        _log.warning("session_end_window_not_created", reported=reported.is_set())
        thread.join(timeout=join_timeout_s)
        return _noop
    _log.info("session_end_window_installed", level=hex(SHUTDOWN_LEVEL))
    uninstalled = False

    def uninstall() -> None:
        nonlocal uninstalled
        if uninstalled:
            return
        uninstalled = True
        try:
            be.request_close()
        except Exception as exc:  # noqa: BLE001 — teardown must not raise
            _log.warning("session_end_window_close_failed", error=str(exc))
        thread.join(timeout=join_timeout_s)

    return uninstall


#: The ctypes window procedure, strongly referenced for the life of the process: the OS
#: calls it from the window thread, and a collected ``WNDPROC`` is a call into freed memory.
_WNDPROC_REFS: list[Any] = []


class _CtypesWindowBackend:
    """``user32`` / ``kernel32`` through ctypes. Windows only; built lazily."""

    def __init__(self) -> None:
        import ctypes  # noqa: PLC0415 — deferred import — Windows-only types are touched only here
        from ctypes import wintypes  # noqa: PLC0415 — deferred import — Windows-only

        self._ct = ctypes
        lresult, wparam_t, lparam_t = ctypes.c_ssize_t, ctypes.c_size_t, ctypes.c_ssize_t
        self._wndproc_t = ctypes.WINFUNCTYPE(  # type: ignore[attr-defined]
            lresult, wintypes.HWND, wintypes.UINT, wparam_t, lparam_t,
        )

        class WNDCLASSEXW(ctypes.Structure):
            _fields_ = [  # noqa: RUF012 — ctypes layout
                ("cbSize", wintypes.UINT), ("style", wintypes.UINT),
                ("lpfnWndProc", self._wndproc_t), ("cbClsExtra", ctypes.c_int),
                ("cbWndExtra", ctypes.c_int), ("hInstance", wintypes.HINSTANCE),
                ("hIcon", wintypes.HANDLE), ("hCursor", wintypes.HANDLE),
                ("hbrBackground", wintypes.HANDLE), ("lpszMenuName", wintypes.LPCWSTR),
                ("lpszClassName", wintypes.LPCWSTR), ("hIconSm", wintypes.HANDLE),
            ]

        class MSG(ctypes.Structure):
            _fields_ = [  # noqa: RUF012 — ctypes layout
                ("hwnd", wintypes.HWND), ("message", wintypes.UINT), ("wParam", wparam_t),
                ("lParam", lparam_t), ("time", wintypes.DWORD), ("pt", wintypes.POINT),
            ]

        self._WNDCLASSEXW, self._MSG = WNDCLASSEXW, MSG
        u = ctypes.WinDLL("user32", use_last_error=True)  # type: ignore[attr-defined]
        k = ctypes.WinDLL("kernel32", use_last_error=True)  # type: ignore[attr-defined]
        u.DefWindowProcW.argtypes = [wintypes.HWND, wintypes.UINT, wparam_t, lparam_t]
        u.DefWindowProcW.restype = lresult
        u.RegisterClassExW.argtypes = [ctypes.POINTER(WNDCLASSEXW)]
        u.RegisterClassExW.restype = wintypes.ATOM
        u.UnregisterClassW.argtypes = [wintypes.LPCWSTR, wintypes.HINSTANCE]
        u.UnregisterClassW.restype = wintypes.BOOL
        u.CreateWindowExW.argtypes = [
            wintypes.DWORD, wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.DWORD,
            ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int,
            wintypes.HWND, wintypes.HMENU, wintypes.HINSTANCE, wintypes.LPVOID,
        ]
        u.CreateWindowExW.restype = wintypes.HWND
        u.GetMessageW.argtypes = [ctypes.POINTER(MSG), wintypes.HWND, wintypes.UINT, wintypes.UINT]
        u.GetMessageW.restype = ctypes.c_int
        u.TranslateMessage.argtypes = [ctypes.POINTER(MSG)]
        u.DispatchMessageW.argtypes = [ctypes.POINTER(MSG)]
        u.DispatchMessageW.restype = lresult
        u.PostMessageW.argtypes = [wintypes.HWND, wintypes.UINT, wparam_t, lparam_t]
        u.PostMessageW.restype = wintypes.BOOL
        u.PostQuitMessage.argtypes = [ctypes.c_int]
        k.GetModuleHandleW.argtypes = [wintypes.LPCWSTR]
        k.GetModuleHandleW.restype = wintypes.HMODULE
        k.SetProcessShutdownParameters.argtypes = [wintypes.DWORD, wintypes.DWORD]
        k.SetProcessShutdownParameters.restype = wintypes.BOOL
        self._u, self._k = u, k
        self._hwnd: int | None = None
        self._lock = threading.Lock()

    def set_shutdown_level(self, level: int) -> None:
        if not self._k.SetProcessShutdownParameters(level, 0):
            raise OSError(f"SetProcessShutdownParameters({level:#x}) failed: {self._ct.get_last_error()}")

    def run_message_loop(
        self, dispatch: Callable[[int, int, int], int | None], ready: Callable[[bool], None],
    ) -> None:
        ct, u = self._ct, self._u

        def wndproc(hwnd: Any, msg: int, wparam: int, lparam: int) -> int:
            if msg == WM_DESTROY:
                u.PostQuitMessage(0)
                return 0
            try:
                result = dispatch(int(msg), int(wparam), int(lparam))
            except Exception:  # noqa: BLE001 — an exception out of a ctypes callback is printed and lost
                _log.exception("session_end_wndproc_raised", msg=int(msg))
                result = None
            if result is None:
                return int(u.DefWindowProcW(hwnd, msg, wparam, lparam))
            return int(result)

        proc = self._wndproc_t(wndproc)
        _WNDPROC_REFS.append(proc)  # for the life of the process, deliberately
        hinst = self._k.GetModuleHandleW(None)
        cls = self._WNDCLASSEXW()
        cls.cbSize = ct.sizeof(self._WNDCLASSEXW)
        cls.lpfnWndProc = proc
        cls.hInstance = hinst
        cls.lpszClassName = WINDOW_CLASS_NAME
        registered = bool(u.RegisterClassExW(ct.byref(cls)))
        if not registered and ct.get_last_error() != 1410:  # ERROR_CLASS_ALREADY_EXISTS
            ready(False)
            raise OSError(f"RegisterClassExW failed: {ct.get_last_error()}")
        # Top-level and hidden: no WS_VISIBLE, never shown, hWndParent NULL. NOT HWND_MESSAGE:
        # a message-only window is not sent the broadcast WM_QUERYENDSESSION / WM_ENDSESSION.
        hwnd = u.CreateWindowExW(
            0, WINDOW_CLASS_NAME, "nexus storage supervisor", 0, 0, 0, 0, 0, None, None, hinst, None,
        )
        if not hwnd:
            error = ct.get_last_error()
            if registered:
                u.UnregisterClassW(WINDOW_CLASS_NAME, hinst)
            ready(False)
            raise OSError(f"CreateWindowExW failed: {error}")
        with self._lock:
            self._hwnd = int(hwnd)
        ready(True)
        msg = self._MSG()
        try:
            while True:
                got = u.GetMessageW(ct.byref(msg), None, 0, 0)
                if got <= 0:  # 0: WM_QUIT; -1: error
                    break
                u.TranslateMessage(ct.byref(msg))
                u.DispatchMessageW(ct.byref(msg))
        finally:
            with self._lock:
                self._hwnd = None
            if registered:
                u.UnregisterClassW(WINDOW_CLASS_NAME, hinst)

    def request_close(self) -> None:
        with self._lock:
            hwnd = self._hwnd
        if hwnd:
            # Posted, not DestroyWindow: only the owning thread may destroy a window.
            self._u.PostMessageW(hwnd, WM_CLOSE, 0, 0)


def ctypes_window_backend() -> WindowBackend:
    """The real backend. Raises off Windows (``WINFUNCTYPE`` does not exist there)."""
    return _CtypesWindowBackend()


__all__ = [
    "CTRL_CLOSE_EVENT",
    "CTRL_LOGOFF_EVENT",
    "CTRL_SHUTDOWN_EVENT",
    "ENGINE_BUDGET_S",
    "HANDLER_BUDGET_S",
    "OWNED_EVENTS",
    "PG_MIN_BUDGET_S",
    "CtrlHandlerRegistrar",
    "ENDSESSION_LOGOFF",
    "SHUTDOWN_LEVEL",
    "WM_CLOSE",
    "WM_ENDSESSION",
    "WM_QUERYENDSESSION",
    "WindowBackend",
    "SessionEndHandler",
    "SessionEndMessageHandler",
    "ctypes_ctrl_registrar",
    "ctypes_window_backend",
    "install_session_end_window",
    "install_session_end_handler",
    "make_pg_stopper",
]
