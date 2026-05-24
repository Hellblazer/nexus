# SPDX-License-Identifier: AGPL-3.0-or-later
"""RDR-121 phase-review-gate sentinel.

The /conexus:phase-review-gate command writes a sentinel file on a PASSED
outcome. The phase_review_close_requires_gate routing hook reads the
sentinel before allowing a phase-review bead close. Sentinel + reader
must ship together (RDR-121 P2 hard coupling).

Sentinel path: ``${TMPDIR:-/tmp}/nx-phase-gate-sentinel/<claude_pid>-<rdr-id>-<phase>.json``

Sentinel JSON shape::

    {
        "outcome": "PASSED",
        "rdr_id": "112",
        "phase": "1",
        "claude_pid": 12345,
        "timestamp": "2026-05-20T10:42:00+00:00"
    }

Sweep-on-write cleanup: best-effort drop of sentinels whose claude_pid
is no longer alive. Cheap (one kill(0) per file) and bounded (file
count grows with phase-review pass count per session).
"""
from __future__ import annotations

import datetime as _dt
import json
import os
import pathlib
from typing import Any


def sentinel_dir() -> pathlib.Path:
    """Return the directory holding sentinel files.

    ``${TMPDIR:-/tmp}/nx-phase-gate-sentinel/``. Honors ``TMPDIR`` so
    tests can redirect via ``monkeypatch.setenv("TMPDIR", str(tmp))``.
    """
    base = os.environ.get("TMPDIR", "/tmp").rstrip("/")
    return pathlib.Path(base) / "nx-phase-gate-sentinel"


def sentinel_path(claude_pid: int, rdr_id: str, phase: str) -> pathlib.Path:
    """Return the sentinel file path for ``(claude_pid, rdr_id, phase)``."""
    return sentinel_dir() / f"{claude_pid}-{rdr_id}-{phase}.json"


def find_claude_pid() -> int:
    """Resolve the nearest Claude Code ancestor PID.

    Delegates to :func:`nexus.session.find_immediate_claude_pid` so the
    sentinel keys against the same PID the T1 discovery surface uses.
    Falls back to ``os.getppid()`` if nexus.session import fails (e.g.
    running outside a real Claude shell during tests).
    """
    try:
        from nexus.session import find_immediate_claude_pid

        return find_immediate_claude_pid()
    except Exception:
        return os.getppid()


def _pid_alive(pid: int) -> bool:
    """Return True iff ``kill(pid, 0)`` succeeds."""
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        # PermissionError means the pid exists but we cannot signal it.
        return True
    except OSError:
        return False
    return True


def sweep_dead_sentinels() -> int:
    """Delete sentinel files whose ``<claude_pid>`` is no longer alive.

    Returns the number of files deleted. Never raises; sweep is best-
    effort and any IO error is silently ignored.
    """
    deleted = 0
    sd = sentinel_dir()
    if not sd.exists():
        return 0
    for path in sd.iterdir():
        if not path.is_file() or not path.name.endswith(".json"):
            continue
        pid_str = path.name.split("-", 1)[0]
        try:
            pid = int(pid_str)
        except ValueError:
            continue
        if not _pid_alive(pid):
            try:
                path.unlink()
                deleted += 1
            except OSError:
                pass
    return deleted


def write_sentinel(rdr_id: str, phase: str, *, claude_pid: int | None = None) -> pathlib.Path:
    """Write the PASSED sentinel and sweep dead-pid stale ones.

    Returns the path written. Caller is responsible for only invoking
    this on the PASSED branch; we do not store ``outcome`` other than
    ``"PASSED"`` because the hook reads the file's existence + content
    as a PASSED claim.
    """
    pid = claude_pid if claude_pid is not None else find_claude_pid()
    sd = sentinel_dir()
    sd.mkdir(parents=True, exist_ok=True)

    sweep_dead_sentinels()

    target = sentinel_path(pid, rdr_id, phase)
    record: dict[str, Any] = {
        "outcome": "PASSED",
        "rdr_id": rdr_id,
        "phase": phase,
        "claude_pid": pid,
        "timestamp": _dt.datetime.now(_dt.timezone.utc).isoformat(),
    }
    target.write_text(json.dumps(record), encoding="utf-8")
    return target


def read_sentinel(claude_pid: int, rdr_id: str, phase: str) -> dict[str, Any] | None:
    """Read sentinel for ``(claude_pid, rdr_id, phase)`` or return None.

    Returns the parsed payload dict on success, ``None`` if the file is
    absent, unreadable, or non-JSON.
    """
    path = sentinel_path(claude_pid, rdr_id, phase)
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return data if isinstance(data, dict) else None
