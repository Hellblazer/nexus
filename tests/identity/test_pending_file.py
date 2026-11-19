# SPDX-License-Identifier: AGPL-3.0-or-later
"""The pending file itself (RDR-253): its name carries the Claude pid and the OS start epoch, the MCP side takes only
its own, and the hook removes stale ones. The bind from it is tests/identity/test_pending_bind.py. Tenant-free."""
from __future__ import annotations

import os
import stat
from pathlib import Path

from nexus.identity.holder import epoch_seconds
from nexus.identity.pending import (
    Pending, parse_pending_name, pending_files, pending_path, remove_stale, take_pending, write_pending,
)

T0 = "2026-11-17T08:00:00Z"


def _rec(pid: int = 4242, os_start: str = T0) -> Pending:
    return Pending(session_id="sid-1", source="startup", seat="marginalia", os_start=os_start, claude_pid=pid)


def test_the_name_carries_the_pid_and_the_os_start_epoch(tmp_path: Path) -> None:
    epoch = epoch_seconds(T0)
    path = write_pending(tmp_path, _rec(), epoch)
    assert path == pending_path(tmp_path, 4242, epoch)
    assert path.name == f"pending.4242.{epoch}" and parse_pending_name(path) == (4242, epoch)
    if os.name == "posix":                      # Windows has no POSIX mode bits
        assert stat.S_IMODE(os.stat(path).st_mode) == 0o600


def test_only_the_file_named_for_this_process_is_taken(tmp_path: Path) -> None:
    """A file left by an earlier owner of the pid has another epoch in its name and is never returned."""
    epoch = epoch_seconds(T0)
    write_pending(tmp_path, _rec(os_start="2026-11-16T01:00:00Z"), epoch - 86_400)
    assert take_pending(tmp_path, 4242, epoch) is None
    write_pending(tmp_path, _rec(), epoch)
    got = take_pending(tmp_path, 4242, epoch)
    assert got is not None and got[1].session_id == "sid-1" and got[1].seat == "marginalia"


def test_stale_files_are_removed_and_live_ones_kept(tmp_path: Path) -> None:
    epoch = epoch_seconds(T0)
    dead = write_pending(tmp_path, _rec(pid=111), epoch)
    reused = write_pending(tmp_path, _rec(pid=222), epoch - 5)            # pid 222 now started at `epoch`
    live = write_pending(tmp_path, _rec(pid=333), epoch)
    unknown = write_pending(tmp_path, _rec(pid=444), epoch)               # start time unreadable: left alone
    starts = {222: epoch, 333: epoch, 444: None}
    removed = remove_stale(tmp_path, alive=lambda pid: pid != 111, os_start_epoch=lambda pid: starts.get(pid))
    assert sorted(removed) == sorted([dead, reused])
    assert sorted(pending_files(tmp_path)) == sorted([live, unknown])
