# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""The pending bind (RDR-253 "A holder that no longer exists", last paragraph).

The hook may run before its own registry file is written, and the bind needs the file's ``startedAt``. The hook
waits up to 5 seconds; if the file has still not appeared it binds nothing and writes its payload to
``<config>/identity/pending.<claude pid>.<OS start epoch seconds>``. The MCP server, on a heartbeat after its Claude
process's registry file exists, takes the file only when the pid and start time in its NAME are its own Claude
process's, binds from it with the data credential and deletes it. The hook deletes any pending file whose process
is not running or whose start time differs (a reused pid).

The name carries the OS start time so a pid reused after a crash never adopts a dead process's payload.
"""
from __future__ import annotations

import contextlib
import json
import os
import re
import time
from collections.abc import Callable
from dataclasses import asdict, dataclass
from pathlib import Path
from uuid import uuid4

_NAME = re.compile(r"^pending\.(?P<pid>[0-9]+)\.(?P<epoch>[0-9]+)$")


@dataclass(frozen=True)
class Pending:
    """The hook's payload: what it knew at SessionStart. ``seat`` is ``NX_SEAT`` and ``name`` the ``-n`` name (read
    from the Claude process's arguments, since the registry file that would hold it is what is missing);
    ``os_start`` is the process's OS start instant."""

    session_id: str
    source: str
    seat: str | None
    os_start: str
    claude_pid: int
    pane: str | None = None
    written_at: float = 0.0
    name: str | None = None


def pending_path(directory: Path, claude_pid: int, start_epoch: int) -> Path:
    return Path(directory) / f"pending.{claude_pid}.{start_epoch}"


def parse_pending_name(path: Path) -> tuple[int, int] | None:
    m = _NAME.match(Path(path).name)
    return (int(m["pid"]), int(m["epoch"])) if m else None


def write_pending(directory: Path, record: Pending, start_epoch: int) -> Path:
    """Publish *record*, mode 0600, atomically. Returns the path."""
    from nexus._winsec import make_user_dir, open_private  # noqa: PLC0415 — deferred: hook import cost

    make_user_dir(Path(directory))
    path = pending_path(directory, record.claude_pid, start_epoch)
    tmp = path.with_name(f"{path.name}.{os.getpid()}.{uuid4().hex}.tmp")
    stamped = Pending(**{**asdict(record), "written_at": record.written_at or time.time()})
    try:
        fd = open_private(tmp, os.O_CREAT | os.O_WRONLY | os.O_TRUNC)  # owner-only before a byte is written, on Windows too
        try:
            os.write(fd, json.dumps(asdict(stamped)).encode("utf-8"))
        finally:
            os.close(fd)
        os.replace(str(tmp), str(path))
    except BaseException:
        with contextlib.suppress(OSError):
            tmp.unlink()
        raise
    return path


def read_pending(path: Path) -> Pending | None:
    """The record at *path*, or None when it is absent, unreadable or malformed. Never raises."""
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError, RecursionError):
        return None
    if not isinstance(data, dict):
        return None
    sid, source, start, pid = data.get("session_id"), data.get("source"), data.get("os_start"), data.get("claude_pid")
    if not (isinstance(sid, str) and sid and isinstance(source, str) and isinstance(start, str) and start):
        return None
    if not isinstance(pid, int) or isinstance(pid, bool):
        return None
    seat, pane, name = data.get("seat"), data.get("pane"), data.get("name")
    written = data.get("written_at")
    return Pending(session_id=sid, source=source, seat=seat if isinstance(seat, str) and seat else None, os_start=start,
                   claude_pid=pid, pane=pane if isinstance(pane, str) and pane else None,
                   name=name if isinstance(name, str) and name else None,
                   written_at=float(written) if isinstance(written, (int, float)) and not isinstance(written, bool) else 0.0)


def delete_pending(path: Path) -> None:
    try:
        Path(path).unlink()
    except OSError:
        pass


def pending_files(directory: Path) -> list[Path]:
    try:
        return sorted(p for p in Path(directory).iterdir() if parse_pending_name(p) is not None)
    except OSError:
        return []


def take_pending(directory: Path, claude_pid: int, start_epoch: int) -> tuple[Path, Pending] | None:
    """The pending file for exactly this process (pid AND start epoch in its name), or None. A file left by an
    earlier owner of the pid has another epoch in its name and is never returned."""
    path = pending_path(directory, claude_pid, start_epoch)
    record = read_pending(path)
    return (path, record) if record is not None else None


def remove_stale(directory: Path, *, alive: Callable[[int], bool], os_start_epoch: Callable[[int], int | None]) -> list[Path]:
    """Delete every pending file whose process is not running or whose OS start time differs from the name's.
    Returns what it deleted. A process whose start time cannot be read is left alone (unknown is not stale)."""
    removed: list[Path] = []
    for path in pending_files(directory):
        parsed = parse_pending_name(path)
        if parsed is None:
            continue
        pid, epoch = parsed
        if not alive(pid):
            stale = True
        else:
            started = os_start_epoch(pid)
            stale = started is not None and started != epoch
        if stale:
            delete_pending(path)
            removed.append(path)
    return removed
