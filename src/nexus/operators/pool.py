# SPDX-License-Identifier: AGPL-3.0-or-later
"""Pool lifecycle + PID-liveness reconciliation — RDR-079 §P2.2.

This module owns the pool session file at
``~/.config/nexus/sessions/pool-<uuid>.session`` and the liveness logic
that cleans up stale peers on startup. Worker management (async
subprocess pool, streaming JSON parsing, retirement) lives in P2.1 and
will compose these primitives.

Invariants maintained here:
  I-1: pool session is distinct from any user T1 session — the session
       file is named ``pool-<uuid>.session`` (RDR-078 session files use
       ``{ppid}.session``), and ``resolve_t1_session`` returns the pool
       record to any worker whose ``NEXUS_T1_SESSION_ID`` matches.
  I-3: no orphan sessions after graceful shutdown — teardown removes
       the file and stops the T1 HTTP server.

SC coverage: SC-13 (a graceful stop, b stale reconcile, c live-peer
preserve), part of SC-11 (scratch-sentinel isolation needs a live pool
session to target) and SC-15 (pool startup fails fast when auth missing —
implemented at the pool-core layer in P2.1, not here).
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Callable
from uuid import uuid4

import structlog

# Indirection for test injection — monkeypatch these attributes to avoid
# spawning a real ChromaDB server in unit tests. Production callers should
# not override.
from nexus.session import (
    SESSIONS_DIR,
    start_t1_server as _start_t1_server,
    stop_t1_server as _stop_t1_server,
    write_session_record,
)

_log = structlog.get_logger()

__all__ = [
    "PoolSession",
    "create_pool_session",
    "probe_pid_alive",
    "reconcile_stale_pool_sessions",
    "teardown_pool_session",
]


@dataclass(frozen=True)
class PoolSession:
    """Metadata for a live pool T1 session.

    ``session_id`` is used as the filename stem (``pool-<uuid>.session``)
    and is also what workers receive via ``NEXUS_T1_SESSION_ID``. The
    T1 endpoint (``host``, ``port``) is the ChromaDB HTTP server the
    pool spawned for isolated scratch.
    """
    session_id: str
    host: str
    port: int
    server_pid: int
    pool_pid: int
    tmpdir: str


# ── Liveness probe ─────────────────────────────────────────────────────────


def probe_pid_alive(pid: int) -> bool:
    """Return True if ``pid`` names a running process on this host.

    ``os.kill(pid, 0)`` sends no signal — it just validates that the
    kernel recognises the PID and the caller has permission to signal
    it. A ``PermissionError`` means the process exists but belongs to
    another user (rare on single-user workstations; still counted as
    alive for safety — we do not own the PID).

    PID 0 is rejected because ``kill(0, ...)`` means "signal the whole
    process group" on POSIX, which is not the liveness question we are
    asking.
    """
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        # Different user owns the process — it exists but we can't signal it.
        # Treat as alive: safer than removing a file that belongs to a live
        # pool owned by someone else on this host.
        return True
    except OSError:
        # Catch-all for platform quirks (EINVAL etc.) — log and assume dead.
        _log.debug("probe_pid_alive_oserror", pid=pid, exc_info=True)
        return False


# ── Reconciliation ─────────────────────────────────────────────────────────


def reconcile_stale_pool_sessions(
    sessions_dir: Path | None = None,
) -> int:
    """Remove ``pool-*.session`` files whose ``pool_pid`` is no longer alive.

    Iterates ``{sessions_dir}/pool-*.session``. For each:
      * Parse JSON. If parseable AND ``pool_session`` is True:
          - If ``pool_pid`` is missing → treat as corrupt, remove.
          - Else probe ``pool_pid`` via :func:`probe_pid_alive`.
          - If dead, remove the file.
      * If JSON is corrupt, remove the file.
      * If ``pool_session`` is absent or False (user session), leave
        untouched — user sessions have their own cleanup path
        (:func:`nexus.session.sweep_stale_sessions`).

    Returns the count of files removed.
    """
    if sessions_dir is None:
        sessions_dir = SESSIONS_DIR
    if not sessions_dir.exists():
        return 0

    removed = 0
    for path in sorted(sessions_dir.glob("pool-*.session")):
        try:
            record = json.loads(path.read_text())
        except (json.JSONDecodeError, OSError):
            _log.debug("pool_reconcile_corrupt_file_removed", path=str(path))
            _try_unlink(path)
            removed += 1
            continue

        if not isinstance(record, dict) or not record.get("pool_session"):
            # Defensive: a file named pool-*.session that isn't a pool
            # record is suspicious but not ours to touch. Skip it.
            continue

        pool_pid = record.get("pool_pid")
        if not isinstance(pool_pid, int):
            _log.debug("pool_reconcile_missing_pool_pid", path=str(path))
            _try_unlink(path)
            removed += 1
            continue

        if not probe_pid_alive(pool_pid):
            _log.info(
                "pool_reconcile_stale_removed",
                path=str(path),
                pool_pid=pool_pid,
            )
            _try_unlink(path)
            removed += 1

    return removed


def _try_unlink(path: Path) -> None:
    """Remove a path, ignoring ``OSError`` (best effort)."""
    try:
        path.unlink(missing_ok=True)
    except OSError as exc:
        _log.debug("pool_unlink_failed", path=str(path), error=str(exc))


# ── Lifecycle: create / teardown ───────────────────────────────────────────


def create_pool_session(
    sessions_dir: Path | None = None,
) -> PoolSession:
    """Start a dedicated T1 HTTP server for the pool and record the session.

    Sequence (order matters):
      1. Run :func:`reconcile_stale_pool_sessions` to clean up dead peers
         from prior crashed pools BEFORE writing our own file (avoids
         self-removal on a race between write + scan).
      2. Generate a UUID and start the T1 HTTP server.
      3. Write ``~/.config/nexus/sessions/pool-<uuid>.session`` with
         ``pool_session=True`` and ``pool_pid=os.getpid()`` (for P2.2
         liveness reconciliation).

    Returns a :class:`PoolSession` the caller uses when teardown runs.
    """
    if sessions_dir is None:
        sessions_dir = SESSIONS_DIR

    # Step 1: reconcile BEFORE we add our own file so the scan cannot
    # mistake the new file for a stale peer.
    reconcile_stale_pool_sessions(sessions_dir)

    # Step 2: spawn T1 HTTP server for this pool.
    host, port, server_pid, tmpdir = _start_t1_server()

    # Step 3: generate session identity and persist the record.
    session_id = f"pool-{uuid4()}"
    pool_pid = os.getpid()
    write_session_record(
        sessions_dir=sessions_dir,
        ppid=0,  # unused for pool sessions
        session_id=session_id,
        host=host,
        port=port,
        server_pid=server_pid,
        tmpdir=tmpdir,
        pool_session=True,
        pool_pid=pool_pid,
    )
    _log.info(
        "pool_session_created",
        session_id=session_id,
        host=host,
        port=port,
        server_pid=server_pid,
        pool_pid=pool_pid,
    )
    return PoolSession(
        session_id=session_id,
        host=host,
        port=port,
        server_pid=server_pid,
        pool_pid=pool_pid,
        tmpdir=tmpdir,
    )


def teardown_pool_session(
    session: PoolSession,
    sessions_dir: Path | None = None,
) -> None:
    """Stop the pool's T1 HTTP server and remove its session file.

    Idempotent — a second call after a successful teardown is a no-op.
    Order: remove the session file first (so a racing reconcile cannot
    observe a live file pointing at a just-killed server), then stop
    the server.
    """
    if sessions_dir is None:
        sessions_dir = SESSIONS_DIR
    session_file = sessions_dir / f"{session.session_id}.session"
    _try_unlink(session_file)
    try:
        _stop_t1_server(session.server_pid)
    except Exception as exc:
        # Server may already be gone (e.g. crashed or torn down by a
        # signal handler). Not fatal.
        _log.debug(
            "pool_teardown_stop_server_failed",
            server_pid=session.server_pid,
            error=str(exc),
        )
