# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""The SessionStart sweep and the local file cleanup that rides it (RDR-253 "Binding", "Retention").

At every SessionStart, after its own bind or rebind, the hook lists the live, lapsed and ``unleased`` bindings on
its host, skips its own process's binding and every delegated identity (a delegated identity is renewed by the
``nx`` dispatcher, has no registry file and lapses on its lease), runs the holder check on the rest and reports the
gone ones to the engine's ``gone`` route, so a crashed session's identity is recorded as gone even if its seat is
never claimed again. The engine accepts a verdict only when it names the identity's current holder under the
generation the sweep read, so a binding that was rebound since is left alone: its answer is a 409 and the sweep
counts it as skipped, never as an error.

Local files the hook owns the cleanup of: the token files of processes the holder check finds gone, and the
pending files whose process is not running or whose start time differs.
"""
from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from nexus.identity.bind_client import IdentityApiError
from nexus.identity.holder import GONE, Processes, check_holder, epoch_seconds, holder_process
from nexus.identity.pending import remove_stale
from nexus.identity.token_file import delete_token_file, parse_token_name, read_token_file, token_files, token_path


@dataclass
class SweepReport:
    """What one sweep did. ``checked`` counts the bindings the holder check ran on."""

    listed: int = 0
    checked: int = 0
    recorded_gone: list[str] = field(default_factory=list)
    skipped_own: int = 0
    skipped_delegated: int = 0
    skipped_rebound: int = 0
    running: int = 0
    unknown: int = 0
    other_host: int = 0
    token_files_removed: list[str] = field(default_factory=list)
    pending_files_removed: list[str] = field(default_factory=list)


def sweep_host(client: Any, procs: Processes | Any, *, host: str, own: tuple[int, int | None] | None,
               identity_dir: Path | None = None) -> SweepReport:
    """Report the gone holders on *host* to ``gone``. *own* is ``(pid, started_at)`` of the calling process, whose
    binding is skipped. With *identity_dir*, the token file of every holder found gone is deleted too."""
    report = SweepReport()
    views = client.bindings_by_host(host)
    report.listed = len(views)
    for view in views:
        holder = view.get("holder")
        if view.get("kind") == "delegated":
            report.skipped_delegated += 1
            continue
        if not isinstance(holder, Mapping):
            continue
        if own is not None and holder.get("pid") == own[0] and holder.get("started_at") == own[1]:
            report.skipped_own += 1
            continue
        report.checked += 1
        verdict = check_holder(holder, procs=procs, host=host)
        if verdict.state != GONE:
            if verdict.state == "running":
                report.running += 1
            elif verdict.state == "unknown":
                report.unknown += 1
            else:
                report.other_host += 1
            continue
        try:
            answer = client.gone(view["id"], int(view["generation"]), holder_process(holder))
        except IdentityApiError as exc:
            if exc.status == 409:  # rebound, or already recorded, since the read: not ours to mark
                report.skipped_rebound += 1
                continue
            raise
        if answer.get("recorded"):
            report.recorded_gone.append(view["id"])
        if identity_dir is not None and isinstance(holder.get("pid"), int) and isinstance(holder.get("started_at"), int):
            path = token_path(identity_dir, holder["pid"], holder["started_at"])
            if path.exists():
                delete_token_file(path)
                report.token_files_removed.append(path.name)
    return report


def clean_local_files(identity_dir: Path, procs: Processes | Any, *, host: str, own_token: Path | None = None,
                      report: SweepReport | None = None) -> SweepReport:
    """Delete token files whose process the holder check finds gone and pending files that are stale. A token file
    the hook just wrote (*own_token*) is never touched."""
    rep = report if report is not None else SweepReport()
    for path in token_files(identity_dir):
        if own_token is not None and path == own_token:
            continue
        parsed = parse_token_name(path)
        if parsed is None:
            continue
        pid, started_at = parsed
        record = read_token_file(path)
        holder = {"host": (record.host if record and record.host else host), "pid": pid, "started_at": started_at,
                  "proc_start": record.proc_start if record else None}
        if check_holder(holder, procs=procs, host=host).state == GONE:
            delete_token_file(path)
            if path.name not in rep.token_files_removed:
                rep.token_files_removed.append(path.name)

    def start_epoch(pid: int) -> int | None:
        started = procs.os_start(pid)
        return epoch_seconds(started) if started else None

    rep.pending_files_removed.extend(p.name for p in remove_stale(identity_dir, alive=procs.alive, os_start_epoch=start_epoch))
    return rep
