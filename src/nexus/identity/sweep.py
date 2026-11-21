# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""The SessionStart sweep and the local file cleanup that rides it (RDR-253 "Binding", "Retention").

At every SessionStart, after its own bind or rebind, the hook lists the live, lapsed and ``unleased`` bindings on
its host, skips its own process's binding and every delegated identity here (a delegated identity is renewed by the
``nx`` dispatcher and has no registry file; its dispatcher process is checked by :mod:`nexus.identity.delegated`),
runs the holder check on the rest and reports the gone ones to the engine's ``gone`` route, so a crashed session's identity is recorded as gone even if its seat is
never claimed again. The engine accepts a verdict only when it names the identity's current holder under the
generation the sweep read, so a binding that was rebound since is left alone: its answer is a 409 and the sweep
counts it as skipped, never as an error.

Local files the hook owns the cleanup of: the token files of processes the holder check finds gone, and the
pending files whose process is not running or whose start time differs.
"""
from __future__ import annotations

import time
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import structlog

from nexus.identity.bind_client import IdentityApiError
from nexus.identity.hostname import bindings_here
from nexus.identity.holder import GONE, Processes, check_holder, epoch_seconds, holder_process
from nexus.identity.pending import remove_stale
from nexus.identity.token_file import delete_token_file, parse_token_name, read_token_file, token_files, token_path

#: A temp file this old is a crashed writer's.
TMP_MAX_AGE_S: float = 600.0

_log = structlog.get_logger(__name__)


@dataclass
class SweepReport:
    """What one sweep did. ``checked`` counts the bindings the holder check ran on."""

    listed: int = 0
    checked: int = 0
    recorded_gone: list[str] = field(default_factory=list)
    skipped_own: int = 0
    skipped_delegated: int = 0
    #: the delegated bindings the sweep listed and skipped, for :func:`nexus.identity.delegated.sweep_delegated`
    delegated_views: list[Any] = field(default_factory=list)
    skipped_rebound: int = 0
    running: int = 0
    unknown: int = 0
    other_host: int = 0
    forbidden: bool = False
    swept: bool = False
    #: identity id -> (pid, started_at) of every binding the sweep listed, so cleanup knows who the engine still has
    holders: dict[str, tuple[Any, Any]] = field(default_factory=dict)
    unjudged_other_registry: int = 0
    #: identities whose ``gone`` the engine refused with an error other than 403/409; the sweep went on past each
    failed: list[str] = field(default_factory=list)
    token_files_removed: list[str] = field(default_factory=list)
    tmp_files_removed: list[str] = field(default_factory=list)
    pending_files_removed: list[str] = field(default_factory=list)


def sweep_host(client: Any, procs: Processes | Any, *, host: str, own: tuple[int, int | None] | None,
               identity_dir: Path) -> SweepReport:
    """Report the gone holders on *host* to ``gone``. *own* is ``(pid, started_at)`` of the calling process, whose
    binding is skipped. The token file of every holder found gone is deleted too. A holder is judged only against the
    registry directory its own token file names (:func:`~nexus.identity.holder.check_holder`): one bound by a Claude
    under another ``CLAUDE_CONFIG_DIR`` is left alone, not reported gone. ``gone`` is the tenant credential's: a 403
    ends the sweep quietly (``forbidden``), it is not an error. Any other refusal of one row is logged and counted
    (``failed``) and the sweep goes on: one failing row does not stop the rest (RDR-253 Test Plan). An engine that
    cannot be reached at all still ends the sweep, by its own error."""
    report = SweepReport()
    views = bindings_here(client, host, identity_dir=identity_dir)
    report.listed = len(views)
    report.swept = True
    for v in views:
        if isinstance(v.get("holder"), Mapping):
            report.holders[v["id"]] = (v["holder"].get("pid"), v["holder"].get("started_at"))
    for view in views:
        holder = view.get("holder")
        if view.get("kind") == "delegated":
            report.skipped_delegated += 1
            report.delegated_views.append(view)
            continue
        if not isinstance(holder, Mapping):
            continue
        if own is not None and holder.get("pid") == own[0] and holder.get("started_at") == own[1]:
            report.skipped_own += 1
            continue
        report.checked += 1
        verdict = check_holder(holder, procs=procs, host=host, identity_dir=identity_dir)
        if verdict.state != GONE:
            if verdict.state == "running":
                report.running += 1
            elif verdict.state == "unknown":
                report.unknown += 1
                if "token file records" in verdict.reason or "no token file" in verdict.reason:
                    report.unjudged_other_registry += 1
            else:
                report.other_host += 1
            continue
        try:
            answer = client.gone(view["id"], int(view["generation"]), holder_process(holder))
        except IdentityApiError as exc:
            if exc.status == 403:  # this credential may not report gone (a data credential): nothing to sweep with
                report.forbidden = True
                return report
            if exc.status == 409:  # rebound, or already recorded, since the read: not ours to mark
                report.skipped_rebound += 1
                continue
            report.failed.append(view["id"])
            _log.warning("identity_sweep_gone_failed", identity=view["id"], status=exc.status, code=exc.code)
            continue
        if answer.get("recorded"):
            report.recorded_gone.append(view["id"])
        if isinstance(holder.get("pid"), int) and isinstance(holder.get("started_at"), int):
            path = token_path(identity_dir, holder["pid"], holder["started_at"])
            if path.exists():
                delete_token_file(path)
                report.token_files_removed.append(path.name)
    return report


def clean_local_files(identity_dir: Path, procs: Processes | Any, *, host: str, own_token: Path | None = None,
                      report: SweepReport | None = None) -> SweepReport:
    """Delete token files of gone processes and pending files that are stale. A token file the hook just wrote
    (*own_token*) is never touched. A token file is deleted only after this run's sweep (*report*, from
    :func:`sweep_host`) has dealt with its holder: the engine recorded the gone, or no longer lists the file's process as
    the holder of the file's identity (it ended, was released or was taken over). A run with no sweep, or a sweep the
    credential could not complete, deletes no token file: the file may be the only thing that names a live
    binding's registry directory."""
    rep = report if report is not None else SweepReport()
    for path in token_files(identity_dir):
        if own_token is not None and path == own_token:
            continue
        parsed = parse_token_name(path)
        if parsed is None:
            continue
        pid, started_at = parsed
        record = read_token_file(path)
        if record is None or not rep.swept or rep.forbidden:
            continue
        holder = {"host": (record.host if record.host else host), "pid": pid, "started_at": started_at,
                  "proc_start": record.proc_start}
        if check_holder(holder, procs=procs, host=host, identity_dir=identity_dir).state != GONE:
            continue
        still_listed = rep.holders.get(record.identity) == (pid, started_at)
        if record.identity in rep.recorded_gone or not still_listed:
            delete_token_file(path)
            if path.name not in rep.token_files_removed:
                rep.token_files_removed.append(path.name)

    # temp files a crashed writer left (a token file's, a pending file's): nothing is mid-write for ten minutes
    try:
        for tmp in Path(identity_dir).glob("*.tmp"):
            if time.time() - tmp.stat().st_mtime > TMP_MAX_AGE_S:
                tmp.unlink()
                rep.tmp_files_removed.append(tmp.name)
    except OSError:
        pass

    def start_epoch(pid: int) -> int | None:
        started = procs.os_start(pid)
        return epoch_seconds(started) if started else None

    rep.pending_files_removed.extend(p.name for p in remove_stale(identity_dir, alive=procs.alive, os_start_epoch=start_epoch))
    return rep
