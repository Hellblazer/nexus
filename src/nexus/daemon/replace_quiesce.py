# SPDX-License-Identifier: AGPL-3.0-or-later
"""Stop what holds the installed engine or PostgreSQL bundle, replace, restart.

RDR-224 (nexus-f9bgu.20). On Windows a running ``nexus-service.exe`` cannot be
overwritten and a running ``postgres.exe`` keeps its bundle directory from being
renamed, so an upgrade stops them first and starts them again afterwards.

:func:`quiesced` is the one context manager every replacement site uses:

* the storage service (supervisor and engine) is stopped with the same
  function ``nx daemon service stop`` uses, which is the ``CTRL_BREAK`` channel
  of nexus-f9bgu.17 confirmed by the target's exit;
* PostgreSQL is stopped as well, but only when the PostgreSQL bundle is what is
  being replaced and a cluster is running (``replacing="pg_bundle"``); an engine
  replacement leaves PostgreSQL alone;
* a service that runs in another Windows session cannot be reached, and is
  never hard-killed: the context manager raises
  :class:`~nexus.daemon.replace_guard.ReplaceBlockedError` that names the
  session and says to run the upgrade there, before anything is replaced;
* afterwards PostgreSQL starts first, then the service, but only what this
  context manager stopped, and on failure as well as on success so a failed
  replacement does not leave the box down.

On POSIX the context manager does nothing: a running binary can be replaced
there, and the behaviour is the one the code had before.
"""
from __future__ import annotations

import contextlib
import sys
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

import structlog

from nexus.daemon.replace_guard import ReplaceBlockedError

_log = structlog.get_logger(__name__)

Replacing = Literal["engine", "pg_bundle"]


class RestartAfterReplaceError(OSError):
    """The replacement succeeded but what was stopped could not be started again."""


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
        from nexus.db.pg_provision import _start_cluster, discover_pg_binaries  # noqa: PLC0415 - deferred, heavy import

        creds = _creds() or {}
        _start_cluster(
            discover_pg_binaries(), Path(creds["PG_DATA"]), int(creds["PG_PORT"]),
        )

    def start_service() -> None:
        from nexus.daemon.storage_service_daemon import ensure_storage_supervisor  # noqa: PLC0415 - deferred, heavy import

        ensure_storage_supervisor(config_dir)

    return QuiesceOps(stop_service, pg_running, stop_pg, start_pg, start_service)


def blocked_message(outcome: Any, *, replacing: Replacing) -> str | None:
    """Why the stop does not allow a replacement, or ``None`` when it does.

    A refused pid is the Windows cross-session case: nothing was signalled or
    killed, and the message says whose session to run the upgrade from. A pid
    that survived the whole escalation is the other case. Both say that no file
    was replaced.
    """
    what = "engine" if replacing == "engine" else "PostgreSQL bundle"
    refused = tuple(getattr(outcome, "refused", ()) or ())
    refused_pids = {r.pid for r in refused}
    stubborn = [p for p in getattr(outcome, "stubborn", ()) if p not in refused_pids]
    parts: list[str] = []
    remedies: list[str] = []
    for r in refused:
        if r.target_session is not None and r.target_session != r.own_session:
            parts.append(
                f"the storage service (pid {r.pid}) runs in Windows session "
                f"{r.target_session}; this shell is in session {r.own_session}, and "
                "a console stop cannot cross Windows sessions"
            )
            remedy = (
                f"Run this upgrade from session {r.target_session}: sign in to that "
                "session, or use a terminal on that desktop."
            )
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
    if not parts:
        return None
    return (
        f"cannot replace the {what}: " + "; ".join(parts) + ". Nothing was "
        "hard-killed across sessions and no file was replaced. " + " ".join(remedies)
    )


def _is_windows(platform: str | None) -> bool:
    return (platform if platform is not None else sys.platform).startswith("win")


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

    Raises :class:`ReplaceBlockedError` before the body runs when the stack
    cannot be stopped (see :func:`blocked_message`), and
    :class:`RestartAfterReplaceError` after a successful body when what was
    stopped could not be started.
    """
    state = QuiesceState()
    if not _is_windows(platform):
        yield state
        return
    effects = ops if ops is not None else default_ops(config_dir)

    outcome = effects.stop_service()
    refused_pids = {r.pid for r in getattr(outcome, "refused", ()) or ()}
    survivors = set(getattr(outcome, "stubborn", ()) or ())
    state.stopped_service = any(
        p not in refused_pids and p not in survivors for p in getattr(outcome, "pids", ())
    )
    blocked = blocked_message(outcome, replacing=replacing)
    if blocked is not None:
        _log.warning("replace_quiesce_blocked", replacing=replacing, reason=blocked)
        if state.stopped_service and not survivors and not refused_pids:
            _restart_best_effort(effects, state)
        raise ReplaceBlockedError(blocked)

    if replacing == "pg_bundle":
        try:
            if effects.pg_running():
                effects.stop_pg()
                state.stopped_pg = True
        except Exception as exc:
            _restart_best_effort(effects, state)
            if isinstance(exc, ReplaceBlockedError):
                raise
            raise ReplaceBlockedError(
                f"cannot replace the PostgreSQL bundle: PostgreSQL could not be "
                f"stopped ({exc}). The storage service was started again if this "
                "run had stopped it. Stop PostgreSQL by hand and run the command "
                "again."
            ) from exc
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
    """Start what this run stopped, PostgreSQL first. Raises on failure."""
    step = ""
    try:
        if state.stopped_pg:
            step = "PostgreSQL"
            effects.start_pg()
            state.restarted.append("postgresql")
        if state.stopped_service:
            step = "the storage service"
            effects.start_service()
            state.restarted.append("service")
    except Exception as exc:
        raise RestartAfterReplaceError(
            f"{step} could not be started again after the replacement: {exc}. "
            "Run 'nx daemon service start'."
        ) from exc


def _restart_best_effort(effects: QuiesceOps, state: QuiesceState) -> str | None:
    """The restart on a failure path: never raises, returns the failure text."""
    try:
        _restart(effects, state)
    except RestartAfterReplaceError as exc:
        _log.error("replace_quiesce_restart_failed", error=str(exc))
        return str(exc)
    return None
