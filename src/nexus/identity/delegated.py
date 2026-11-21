# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""A delegated identity's holder is the ``nx`` dispatcher, and a dead dispatcher gets a ``gone`` row (RDR-253
"Delegation", "Retention"; Step 4, nexus-hczdo.1.5).

A delegated identity (a ``claude -p`` child) is bound by the operator dispatcher: its binding names the dispatcher's
process (``host``, ``pid``, ``started_at`` in milliseconds, ``proc_start``). The dispatcher has no registry file, so
the registry legs of :func:`~nexus.identity.holder.check_holder` do not apply: :func:`check_dispatcher` is the pid
probe and the start-time compare alone, which is what that check does for any holder without a token file. (A
``claude -p`` child writes a registry file of its own with ``entrypoint: sdk-cli``, but it is not the holder.)

Decided at implementation (the bead's DISCOVER item 1): the SessionStart sweep and ``nx identity alive`` run this
check on delegated identities too, and report a dead dispatcher to the ``gone`` route, so retention's "lapsed with its
holder gone" and ``purge`` can fire for one. The hook also removes the token files in ``<config>/identity/delegated/``
whose dispatcher is gone: the dispatcher deletes its own on every exit it survives, and a dead one cannot.

:func:`~nexus.identity.sweep.sweep_host` still skips delegated identities (it counts them as ``skipped_delegated``
and hands their views back in ``SweepReport.delegated_views``); :func:`sweep_delegated` runs on those views.
"""
from __future__ import annotations

import time
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import structlog

from nexus.identity.bind_client import IdentityApiError, IdentityUnavailableError
from nexus.identity.hostname import describe_host, is_key
from nexus.identity.holder import (
    GONE, OTHER_HOST, RUNNING, UNKNOWN, Processes, Verdict, holder_process, normalise_instant,
)
from nexus.identity.token_file import delete_token_file, read_token_file

_log = structlog.get_logger(__name__)

DELEGATED_DIR_NAME = "delegated"


def delegated_dir(identity_dir: Path) -> Path:
    """``<config>/identity/delegated``: one ``<id>.token`` per running delegated child, mode 0600."""
    return Path(identity_dir) / DELEGATED_DIR_NAME


def delegated_token_path(identity_dir: Path, identity: str) -> Path:
    return delegated_dir(identity_dir) / f"{identity}.token"


def no_identity_path(identity_dir: Path) -> Path:
    """The token file a child names when its dispatcher made it no delegated identity: it never exists. The child's
    ``NX_IDENTITY_TOKEN`` then reads as "this process is a delegated child (bind nothing) with no identity"."""
    return delegated_dir(identity_dir) / "none"


def _dispatcher_local(holder: Mapping[str, Any], here: str, identity_dir: Path | None, identity: str | None) -> bool:
    """Whether the dispatcher *holder* names is on this machine: its host is *here*, or a name that is not a key and that
    the delegated token file of *identity* (this machine's own, in *identity_dir*) records for the same pid
    (:mod:`nexus.identity.hostname`, "Rows under a name that is not a key")."""
    name = holder.get("host")
    if name == here:
        return True
    if not isinstance(name, str) or is_key(name) or identity_dir is None or identity is None:
        return False
    record = read_token_file(delegated_token_path(identity_dir, identity))
    return record is not None and record.host == name and record.pid == holder.get("pid")


def check_dispatcher(holder: Mapping[str, Any], *, procs: Processes | Any, host: str | None = None,
                     identity_dir: Path | None = None, identity: str | None = None) -> Verdict:
    """Whether the dispatcher process *holder* names still exists: the pid probe, then the OS start time against
    ``proc_start`` (a reused pid is gone). Another host's dispatcher is judged by its lease alone (``other_host``);
    a pid that runs but whose start time cannot be read, or a binding that recorded none, is running, never gone."""
    here = host if host is not None else procs.host
    if not _dispatcher_local(holder, here, identity_dir, identity):
        return Verdict(OTHER_HOST, f"dispatched from {describe_host(holder.get('host'))}, not from {describe_host(here)}")
    pid = holder.get("pid")
    if not isinstance(pid, int) or isinstance(pid, bool):
        return Verdict(UNKNOWN, "the binding names no pid")
    if not procs.alive(pid):
        return Verdict(GONE, f"dispatcher pid {pid} is not running")
    recorded = normalise_instant(holder.get("proc_start"))
    if recorded is None:
        return Verdict(RUNNING, f"dispatcher pid {pid} is running (the binding recorded no start time)")
    started = procs.os_start(pid)
    if started is None:
        return Verdict(UNKNOWN, f"dispatcher pid {pid} is running but its start time cannot be read here")
    if started != recorded:
        return Verdict(GONE, f"dispatcher pid {pid} started {started}, not {recorded} (the pid was reused)")
    return Verdict(RUNNING, f"dispatcher pid {pid} is running")


def sweep_delegated(client: Any, procs: Processes | Any, views: list[Mapping[str, Any]], *, host: str,
                    report: Any = None, identity_dir: Path | None = None) -> list[str]:
    """Report each delegated identity in *views* (live or lapsed bindings on *host*) whose dispatcher is gone to the
    ``gone`` route. Returns the ids the engine recorded. A 403 (a credential that may not report ``gone``) ends it
    quietly and, with a *report* (the main sweep's), marks it ``forbidden`` so the caller deletes no token file; a 409 (rebound, or recorded already) is skipped, as in the main sweep; any other failure of one row is
    logged and the rest are still checked."""
    recorded: list[str] = []
    for view in views:
        holder = view.get("holder")
        if view.get("kind") != "delegated" or not isinstance(holder, Mapping):
            continue
        if check_dispatcher(holder, procs=procs, host=host, identity_dir=identity_dir, identity=str(view.get("id"))).state != GONE:
            continue
        try:
            answer = client.gone(view["id"], int(view["generation"]), holder_process(holder))
        except IdentityApiError as exc:
            if exc.status == 403:
                if report is not None:
                    report.forbidden = True
                return recorded
            if exc.status == 409:
                continue
            _log.warning("delegated_sweep_row_failed", identity=view.get("id"), error=str(exc))
            continue
        except IdentityUnavailableError as exc:
            _log.warning("delegated_sweep_row_failed", identity=view.get("id"), error=str(exc))
            continue
        if answer.get("recorded"):
            recorded.append(view["id"])
    return recorded


def clean_delegated_tmp(identity_dir: Path, *, now: float | None = None) -> list[str]:
    """Delete the stale temp files in the delegated directory (a crashed dispatcher's half-written token file), by the
    rule of the top-level :func:`~nexus.identity.sweep.clean_local_files`: older than ``TMP_MAX_AGE_S`` by mtime, so a
    file a live dispatcher is writing this instant is never touched. Returns the names removed."""
    from nexus.identity.sweep import TMP_MAX_AGE_S  # noqa: PLC0415 — sweep imports the token modules; deferred for the hook's import cost

    removed: list[str] = []
    moment = time.time() if now is None else now
    try:
        for tmp in Path(delegated_dir(identity_dir)).glob("*.tmp"):
            try:
                if moment - tmp.stat().st_mtime > TMP_MAX_AGE_S:
                    tmp.unlink()
                    removed.append(tmp.name)
            except OSError:
                continue
    except OSError:
        pass
    return removed


def clean_delegated_files(identity_dir: Path, procs: Processes | Any, *, host: str) -> list[str]:
    """Delete the token files in the delegated directory whose dispatcher process is gone (it died before it could
    delete its own). A file with no readable record, or whose dispatcher runs or cannot be judged, stays. Returns the
    names removed."""
    removed: list[str] = []
    directory = delegated_dir(identity_dir)
    try:
        files = sorted(directory.glob("*.token"))
    except OSError:
        return removed
    for path in files:
        record = read_token_file(path)
        if record is None:
            continue
        holder = {"host": record.host or host, "pid": record.pid, "started_at": record.started_at,
                  "proc_start": record.proc_start}
        if check_dispatcher(holder, procs=procs, host=host, identity_dir=identity_dir, identity=path.stem).state == GONE:
            delete_token_file(path)
            removed.append(path.name)
    return removed
