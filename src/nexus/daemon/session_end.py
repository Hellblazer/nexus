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

A process that loads ``user32`` / ``gdi32`` receives ``WM_QUERYENDSESSION`` /
``WM_ENDSESSION`` instead of the LOGOFF and SHUTDOWN events and this callback never
fires for them. That is verified only on a real session end; see the bead.

Every OS touchpoint is injectable (registrar, the two stoppers, the clock), so the logic
is tested on every host. Nothing here imports a Windows-only module at import time.
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
    ) -> None:
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


__all__ = [
    "CTRL_CLOSE_EVENT",
    "CTRL_LOGOFF_EVENT",
    "CTRL_SHUTDOWN_EVENT",
    "ENGINE_BUDGET_S",
    "HANDLER_BUDGET_S",
    "OWNED_EVENTS",
    "PG_MIN_BUDGET_S",
    "CtrlHandlerRegistrar",
    "SessionEndHandler",
    "ctypes_ctrl_registrar",
    "install_session_end_handler",
    "make_pg_stopper",
]
