# SPDX-License-Identifier: AGPL-3.0-or-later
"""Stop what holds the installed engine or PostgreSQL bundle, replace, restart.

RDR-224 (nexus-f9bgu.20), extended to macOS and Linux by nexus-86aaz. On Windows
a running ``nexus-service.exe`` cannot be overwritten and a running
``postgres.exe`` keeps its bundle directory from being renamed. On macOS and
Linux a running binary CAN be replaced, but the process that runs keeps the old
file and a following ``nx daemon service start`` finds the live lease and
starts nothing, so the swap succeeded and the old engine went on serving (the
well-known-path half of the nexus-4e96a class). So on every platform an upgrade
stops what holds the files first and starts it again afterwards.

:func:`quiesced` is the one context manager every replacement site uses:

* the storage service (supervisor and engine) is stopped with the same
  function ``nx daemon service stop`` uses (``stop_storage_service``; on
  Windows the ``CTRL_BREAK`` channel of nexus-f9bgu.17, on POSIX ``SIGTERM``),
  confirmed by the target's exit, and only the service of THIS config
  directory: another directory's service is never touched;
* PostgreSQL is stopped as well, but only when the PostgreSQL bundle is what is
  being replaced and a cluster is running (``replacing="pg_bundle"``); an engine
  replacement leaves PostgreSQL alone;
* a service that survives the stop blocks the replacement: the context manager
  raises :class:`~nexus.daemon.replace_guard.ReplaceBlockedError` before
  anything is replaced. On Windows a service in another session cannot be
  reached at all, is never hard-killed, and the error names the session and
  says to run the upgrade there (a Windows-only outcome: the POSIX stop never
  reports a refusal);
* afterwards PostgreSQL starts first, then the service, but only what this
  context manager stopped (a service that was not running is not started), and
  on failure as well as on success so a failed replacement does not leave the
  box down. A start that fails after a good replacement raises
  :class:`RestartAfterReplaceError`.

*restart_after* False leaves the start after a SUCCESSFUL body to the caller,
for one that restarts and verifies the service itself next
(the upgrade finish pass's engine convergence), so the service is stopped once and started
once per upgrade. Only the swap stops anything: a caller wraps just the
placement, so the download and the extraction run with the service up.

A stop whose result cannot be fully trusted (the process-table sweep did not run,
or a process could not be placed in any directory) does not block the swap, which
would leave a box with no ``ps`` unable to upgrade; it is logged and carried in
:attr:`QuiesceState.warnings` for the caller to print.

:class:`SharedQuiesce` is the same stop-replace-start for a run that replaces
several things in turn (``nx daemon service install-binary``: the engine, then the
PostgreSQL bundle): the first replacement that actually happens stops the stack, a
later one that needs more (PostgreSQL) stops that too, and the stack is started
once when the run ends. A replacement that is skipped as unchanged never joins, so
a run that changes nothing stops nothing.
"""
from __future__ import annotations

import contextlib
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

import structlog

from nexus.daemon.replace_guard import ReplaceBlockedError

_log = structlog.get_logger(__name__)

Replacing = Literal["engine", "pg_bundle"]


class RestartAfterReplaceError(OSError):
    """The replacement succeeded but what was stopped could not be started again.

    ``slow_start`` is True when the start did not fail but did not finish: the
    supervisor was spawned and is alive, and the engine has not answered within the
    start's 60 s (a migration can take far longer). It is the same split
    ``upgrade_finish._restart_and_verify`` makes ("may still be migrating"); a
    supervisor that exited, or any other failure, is False.
    """

    slow_start: bool = False


@dataclass
class QuiesceOps:
    """The effects :func:`quiesced` performs, as seams so every branch runs under
    test on every host. :func:`default_ops` builds the real ones."""

    stop_service: Callable[[], Any]
    pg_running: Callable[[], bool]
    stop_pg: Callable[[], None]
    start_pg: Callable[[], None]
    start_service: Callable[[], None]


@dataclass
class QuiesceState:
    """What the context manager stopped, for the caller and for the tests."""

    stopped_service: bool = False
    stopped_pg: bool = False
    restarted: list[str] = field(default_factory=list)
    #: Things the caller should tell the operator: a stop whose result could not be
    #: fully verified. The swap went ahead anyway.
    warnings: list[str] = field(default_factory=list)
    #: Stops PostgreSQL too when a cluster is running, for a replacement that joins
    #: a stop made for the engine. Set by :func:`quiesced`; raises
    #: :class:`ReplaceBlockedError` when the cluster cannot be stopped.
    stop_pg_if_running: Callable[[], None] = field(default=lambda: None, repr=False)


def default_ops(config_dir: Path) -> QuiesceOps:
    """The real stop and start effects for the stack under *config_dir*."""

    def _creds() -> dict[str, str] | None:
        from nexus.daemon.storage_service_daemon import _read_pg_credentials  # noqa: PLC0415 - deferred, heavy import

        try:
            return _read_pg_credentials(config_dir / "pg_credentials")
        except OSError:
            return None

    def stop_service() -> Any:
        from nexus.daemon.storage_service_daemon import stop_storage_service  # noqa: PLC0415 - deferred, heavy import

        return stop_storage_service(config_dir=config_dir)

    def pg_running() -> bool:
        from nexus.daemon.storage_service_daemon import _port_accepting  # noqa: PLC0415 - deferred, heavy import

        creds = _creds()
        port = (creds or {}).get("PG_PORT", "")
        return bool(port.isdigit() and _port_accepting("127.0.0.1", int(port)))

    def stop_pg() -> None:
        from nexus.bounded_subprocess import run_bounded  # noqa: PLC0415 - deferred import
        from nexus.db.pg_provision import discover_pg_binaries  # noqa: PLC0415 - deferred, heavy import

        creds = _creds() or {}
        pg_data = creds.get("PG_DATA", "")
        if not pg_data:
            raise ReplaceBlockedError(
                "PostgreSQL is running but PG_DATA is missing from pg_credentials, "
                "so it cannot be stopped for the replacement. Stop it with pg_ctl "
                "and run the command again."
            )
        run_bounded(
            [str(discover_pg_binaries().pg_ctl), "-D", pg_data, "-m", "fast", "stop"],
            check=True,
            timeout=60,
        )

    def start_pg() -> None:
        from nexus.db.pg_provision import _start_cluster, apply_managed_conf_before_start, discover_pg_binaries, refuse_cluster_below_floor  # noqa: PLC0415 - deferred, heavy import

        creds = _creds() or {}
        pgdata, port = Path(creds["PG_DATA"]), int(creds["PG_PORT"])
        refuse_cluster_below_floor(pgdata)  # before the conf rewrite (nexus-hck2c)
        apply_managed_conf_before_start(pgdata, port)  # the managed block first, best effort (nexus-o26hs)
        _start_cluster(discover_pg_binaries(), pgdata, port)

    def start_service() -> None:
        from nexus.commands.daemon import ensure_storage_supervisor  # noqa: PLC0415 - deferred, heavy import

        ensure_storage_supervisor(config_dir)

    return QuiesceOps(stop_service, pg_running, stop_pg, start_pg, start_service)


def blocked_message(outcome: Any, *, replacing: Replacing) -> str | None:
    """Why the stop does not allow a replacement, or ``None`` when it does.

    A refused pid is the Windows cross-session case: nothing was signalled or
    killed, and the message says whose session to run the upgrade from. A pid
    that survived the whole escalation is another. A supervisor a heartbeating
    lease names but this process cannot see (another PID namespace, nexus-dyfsw)
    is the third. Each says that no file was replaced.
    """
    what = "engine" if replacing == "engine" else "PostgreSQL bundle"
    refused = tuple(getattr(outcome, "refused", ()) or ())
    refused_pids = {r.pid for r in refused}
    stubborn = [p for p in getattr(outcome, "stubborn", ()) if p not in refused_pids]
    parts: list[str] = []
    remedies: list[str] = []
    for r in refused:
        if (
            r.target_session is not None
            and r.own_session is not None
            and r.target_session != r.own_session
        ):
            parts.append(
                f"the storage service (pid {r.pid}) runs in Windows session "
                f"{r.target_session}; this shell is in session {r.own_session}, and "
                "a console stop cannot cross Windows sessions"
            )
            remedy = (
                f"Run this upgrade from session {r.target_session}: sign in to that "
                "session, or use a terminal on that desktop."
            )
        elif getattr(r, "stage", None) == "helper":
            parts.append(
                f"the storage service (pid {r.pid}) could not be reached: the console "
                "helper did not answer, and the service is not known to be in this "
                "Windows session"
            )
            remedy = "Run this upgrade again from the session that started the service."
        else:
            parts.append(
                f"the storage service (pid {r.pid}) could not be reached: access was "
                "denied when attaching to its console"
            )
            remedy = (
                "Run this upgrade as the account, and with the elevation, that "
                "started the service."
            )
        if remedy not in remedies:
            remedies.append(remedy)
    if stubborn:
        parts.append(
            f"pid(s) {', '.join(str(p) for p in stubborn)} survived the stop "
            "escalation and may still be running"
        )
        remedies.append("Stop them by hand, then run the upgrade again.")
    invisible = tuple(getattr(outcome, "invisible", ()) or ())
    if invisible:
        parts.append(
            f"the storage-service lease is still heartbeating, but its supervisor "
            f"(pid {', '.join(str(p) for p in invisible)}) is not visible from here, so it runs "
            "in another PID namespace that shares this config directory and was not stopped"
        )
        remedies.append("Stop it from where it runs, then run the upgrade again.")
    if not parts:
        return None
    if refused:
        tail = "Nothing was hard-killed across sessions and no file was replaced. "
    else:
        tail = "No file was replaced. "
    return f"cannot replace the {what}: " + "; ".join(parts) + ". " + tail + " ".join(remedies)


def unverified_stop_warning(outcome: Any) -> str | None:
    """What the operator should know when the stop could not rule out a running
    service, or ``None`` when the stop is fully verified.

    The stop can finish without having looked everywhere: the process-table sweep
    did not run (``sweep_verified`` False, or ``source == "process_table_unavailable"``)
    or a candidate process could not be placed in any config directory
    (``unreadable``). A service started some way the lease does not record may then
    still be running on the old files. The replacement is not blocked for it (a box
    without a readable process table could never upgrade), so the caller says so.
    """
    unreadable = tuple(getattr(outcome, "unreadable", ()) or ())
    unverified = (
        getattr(outcome, "sweep_verified", True) is False
        or getattr(outcome, "source", "") == "process_table_unavailable"
        or bool(unreadable)
    )
    if not unverified:
        return None
    detail = (
        f"pid(s) {', '.join(str(p) for p in unreadable)} could not be placed in any "
        "config directory" if unreadable else "the process table could not be read"
    )
    return (
        f"a running storage service could not be ruled out ({detail}); if one is "
        "running it may still be using the old files. Restart it by hand: "
        "nx daemon service stop, then nx daemon service start"
    )


@contextlib.contextmanager
def quiesced(
    config_dir: Path,
    *,
    replacing: Replacing,
    platform: str | None = None,
    restart_after: bool = True,
    ops: QuiesceOps | None = None,
) -> Iterator[QuiesceState]:
    """Stop the stack for a replacement, restart what was stopped afterwards.

    *restart_after* False skips the restart after a SUCCESSFUL body, for a
    caller that restarts (and verifies) the service itself next; a failed body
    always restarts, so a failed replacement never leaves the service down.

    The stop and the start are the same on every platform (nexus-86aaz).
    *platform* is accepted for the callers and tests that inject the host; it
    no longer changes what is done, only which refusals the stop can carry
    (a refusal exists on Windows alone).

    Raises :class:`ReplaceBlockedError` before the body runs when the stack
    cannot be stopped (see :func:`blocked_message`), and
    :class:`RestartAfterReplaceError` after a successful body when what was
    stopped could not be started.
    """
    state = QuiesceState()
    effects = ops if ops is not None else default_ops(config_dir)

    outcome = effects.stop_service()
    refused_pids = {r.pid for r in getattr(outcome, "refused", ()) or ()}
    survivors = set(getattr(outcome, "stubborn", ()) or ())
    state.stopped_service = any(
        p not in refused_pids and p not in survivors for p in getattr(outcome, "pids", ())
    )
    blocked = blocked_message(outcome, replacing=replacing)
    if blocked is not None:
        _log.warning(
            "replace_quiesce_blocked", replacing=replacing, reason=blocked,
            stopped_service=state.stopped_service,
        )
        raise ReplaceBlockedError(blocked)

    warning = unverified_stop_warning(outcome)
    if warning is not None:
        _log.warning("replace_quiesce_stop_unverified", replacing=replacing, warning=warning)
        state.warnings.append(warning)

    def _stop_pg_if_running() -> None:
        if state.stopped_pg:
            return
        try:
            if effects.pg_running():
                effects.stop_pg()
                state.stopped_pg = True
        except ReplaceBlockedError:
            raise
        except Exception as exc:
            raise ReplaceBlockedError(
                f"cannot replace the PostgreSQL bundle: PostgreSQL could not be "
                f"stopped ({exc}). The storage service was started again if this "
                "run had stopped it. Stop PostgreSQL by hand and run the command "
                "again."
            ) from exc

    state.stop_pg_if_running = _stop_pg_if_running
    if replacing == "pg_bundle":
        try:
            _stop_pg_if_running()
        except ReplaceBlockedError:
            _restart_best_effort(effects, state)
            raise
    _log.info(
        "replace_quiesced", replacing=replacing,
        stopped_service=state.stopped_service, stopped_pg=state.stopped_pg,
    )

    try:
        yield state
    except BaseException as exc:
        failure = _restart_best_effort(effects, state)
        if failure and isinstance(exc, Exception):
            exc.add_note(f"The restart afterwards also failed: {failure}")
        raise
    if restart_after:
        _restart(effects, state)


def _restart(effects: QuiesceOps, state: QuiesceState) -> None:
    """Start what this run stopped, PostgreSQL first. Raises on failure.

    A step that already ran (``state.restarted``) is not run again, so a restart on
    a failure path after one that already happened does not start anything twice."""
    step = ""
    try:
        if state.stopped_pg and "postgresql" not in state.restarted:
            step = "PostgreSQL"
            effects.start_pg()
            state.restarted.append("postgresql")
        if state.stopped_service and "service" not in state.restarted:
            step = "the storage service"
            effects.start_service()
            state.restarted.append("service")
    except Exception as exc:
        err = RestartAfterReplaceError(
            f"{step} could not be started again after the replacement: {exc}. "
            + ("Run `nx backup`, then 'nx daemon service start'."  # the legacy-chash guard refused (exit 75)
               if getattr(exc, "exit_code", None) == 75 else "Run 'nx daemon service start'.")
        )
        # nexus-86aaz r2: the raiser decides (StorageServiceStartError.still_starting), never the
        # message text, so a unit that is crash-looping or reported an error stays a failure.
        err.slow_start = step == "the storage service" and bool(getattr(exc, "still_starting", False))
        raise err from exc


def _restart_best_effort(effects: QuiesceOps, state: QuiesceState) -> str | None:
    """The restart on a failure path: never raises, returns the failure text."""
    try:
        _restart(effects, state)
    except RestartAfterReplaceError as exc:
        _log.error("replace_quiesce_restart_failed", error=str(exc))
        return str(exc)
    return None


class SharedQuiesce:
    """One stop and one start across the replacements of one run.

    A caller that replaces several things in turn (``nx daemon service
    install-binary``: the engine, then the PostgreSQL bundle) hands each site
    :meth:`guard` as that site's *quiesce*. The first site to enter stops the
    stack (the stop :func:`quiesced` makes); a later one that needs PostgreSQL
    stopped as well stops it; a site that is skipped as unchanged never enters and
    stops nothing. Leaving a guard starts nothing. Leaving this object does: what
    was stopped is started, PostgreSQL first, once, and also when the run failed
    (a failed run never leaves the box down).

    Use it as a context manager around all the sites::

        with SharedQuiesce(config_dir) as shared:
            install_binary(..., quiesce=shared.guard("engine"))
            extract_bundle(..., quiesce=shared.guard("pg_bundle"))

    *restart_after*, *platform* and *ops* mean what they mean for
    :func:`quiesced`. :attr:`state` is the :class:`QuiesceState` once a site has
    entered, ``None`` before.
    """

    def __init__(
        self,
        config_dir: Path,
        *,
        platform: str | None = None,
        restart_after: bool = True,
        ops: QuiesceOps | None = None,
    ) -> None:
        self._config_dir = config_dir
        self._platform = platform
        self._restart_after = restart_after
        self._ops = ops
        self._cm: contextlib.AbstractContextManager[QuiesceState] | None = None
        self.state: QuiesceState | None = None

    def guard(self, replacing: Replacing) -> contextlib.AbstractContextManager[QuiesceState]:
        """The context manager one replacement site enters around its swap."""
        return _SharedGuard(self, replacing)

    def _join(self, replacing: Replacing) -> QuiesceState:
        if self._cm is None:
            cm = quiesced(
                self._config_dir, replacing=replacing, platform=self._platform,
                restart_after=self._restart_after, ops=self._ops,
            )
            state = cm.__enter__()  # raises ReplaceBlockedError with nothing held
            self._cm, self.state = cm, state
            return state
        assert self.state is not None
        if replacing == "pg_bundle":
            self.state.stop_pg_if_running()
        return self.state

    def __enter__(self) -> SharedQuiesce:
        return self

    def __exit__(self, exc_type: Any, exc: Any, tb: Any) -> bool | None:
        cm, self._cm = self._cm, None
        if cm is None:
            return None
        return cm.__exit__(exc_type, exc, tb)


class _SharedGuard:
    """What :meth:`SharedQuiesce.guard` returns: joins the shared stop on enter,
    does nothing on exit (the shared object owns the start)."""

    def __init__(self, shared: SharedQuiesce, replacing: Replacing) -> None:
        self._shared = shared
        self._replacing = replacing

    def __enter__(self) -> QuiesceState:
        return self._shared._join(self._replacing)

    def __exit__(self, exc_type: Any, exc: Any, tb: Any) -> None:
        return None
