# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""The per-process token file (RDR-253 "Handover to the MCP server").

``<config>/identity/<claude pid>.<startedAt>.token``, mode 0600, one file per process start: two processes that
resume one session id never share one, and a superseded process's children never read the new holder's token.
The hook writes it after a bind; the MCP server reads it at start, re-reads it on every heartbeat, and rewrites it
when it refreshes the token at half life; every ``nx`` in a Bash child of the session reads the nearest Claude
ancestor's. It holds the identity and generation as well as the token, so the resolver can compare the file with
the session index (a Bash child started before a cross-seat ``/resume``) and a Step 1 engine, which sends no token,
still leaves the file that says who the process is.

A delegated child's file is named by ``NX_IDENTITY_TOKEN`` and has the same shape.
"""
from __future__ import annotations

import json
import os
import re
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any
from uuid import uuid4

IDENTITY_DIR_NAME = "identity"
_TOKEN_NAME = re.compile(r"^(?P<pid>[0-9]+)\.(?P<started_at>[0-9]+)\.token$")


@dataclass(frozen=True)
class TokenFile:
    """What the file holds. ``token`` and ``expires_at`` (Unix seconds) are None against an engine that issues no
    identity tokens; ``proc_start`` is the process's OS start time, which the hook's cleanup compares."""

    identity: str
    generation: int
    token: str | None
    expires_at: float | None
    session_id: str | None
    pid: int
    started_at: int
    proc_start: str | None
    host: str | None = None
    written_at: float = 0.0


def identity_dir(config_dir: Path) -> Path:
    return Path(config_dir) / IDENTITY_DIR_NAME


def token_path(directory: Path, pid: int, started_at: int) -> Path:
    return Path(directory) / f"{pid}.{started_at}.token"


def parse_token_name(path: Path) -> tuple[int, int] | None:
    """``(pid, startedAt)`` from a token file's name, or None for any other name."""
    m = _TOKEN_NAME.match(Path(path).name)
    return (int(m["pid"]), int(m["started_at"])) if m else None


def write_token_file(path: Path, record: TokenFile) -> None:
    """Publish *record* at *path*: a temp file created 0600 (so no moment has wider permissions), then one atomic
    rename, so a concurrent reader never sees a torn file."""
    path = Path(path)
    from nexus._winsec import make_user_dir, open_private  # noqa: PLC0415 — deferred: hook import cost

    make_user_dir(path.parent)
    stamped = TokenFile(**{**asdict(record), "written_at": record.written_at or time.time()})
    tmp = path.with_name(f"{path.name}.{os.getpid()}.{uuid4().hex}.tmp")
    fd = open_private(tmp, os.O_CREAT | os.O_WRONLY | os.O_TRUNC)  # owner-only before a byte is written, on Windows too
    try:
        os.write(fd, json.dumps(asdict(stamped)).encode("utf-8"))
    finally:
        os.close(fd)
    os.replace(str(tmp), str(path))


def _opt_int(value: Any) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _opt_num(value: Any) -> float | None:
    return float(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else None


def _opt_str(value: Any) -> str | None:
    return value if isinstance(value, str) and value else None


def read_token_file(path: Path) -> TokenFile | None:
    """The file at *path*, or None when it is absent, unreadable or not a token file. Never raises."""
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError, RecursionError):
        return None
    if not isinstance(data, dict):
        return None
    identity, generation = data.get("identity"), _opt_int(data.get("generation"))
    pid, started_at = _opt_int(data.get("pid")), _opt_int(data.get("started_at"))
    if not isinstance(identity, str) or not identity or generation is None or pid is None or started_at is None:
        return None
    return TokenFile(
        identity=identity, generation=generation, token=_opt_str(data.get("token")),
        expires_at=_opt_num(data.get("expires_at")), session_id=_opt_str(data.get("session_id")), pid=pid,
        started_at=started_at, proc_start=_opt_str(data.get("proc_start")), host=_opt_str(data.get("host")),
        written_at=_opt_num(data.get("written_at")) or 0.0,
    )


def delete_token_file(path: Path) -> None:
    """Best-effort, idempotent delete."""
    try:
        Path(path).unlink()
    except OSError:
        pass


def token_files(directory: Path) -> list[Path]:
    """Every per-process token file in *directory* (the delegated ones live in a subdirectory and are not listed)."""
    try:
        return sorted(p for p in Path(directory).iterdir() if parse_token_name(p) is not None)
    except OSError:
        return []


def ancestor_token_path(directory: Path, claude_pid: int, sessions_dir: Path | None = None) -> Path | None:
    """The token file path of the Claude process *claude_pid*: its pid and the ``startedAt`` its registry file
    records. None when the process has no readable registry file (the file's name cannot be known)."""
    from nexus.identity.holder import read_registry_entry  # noqa: PLC0415 — holder imports nothing from here; deferred for symmetry with the hook's cost budget

    entry = read_registry_entry(claude_pid, sessions_dir)
    if entry is None or entry.started_at is None:
        return None
    return token_path(directory, claude_pid, entry.started_at)
