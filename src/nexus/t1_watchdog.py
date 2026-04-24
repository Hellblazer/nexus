# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""T1 ChromaDB server watchdog (nexus-99jb Layer 1).

Detached sidecar spawned alongside each per-session chroma server.
Polls the Claude Code root PID and the chroma PID every
:data:`POLL_INTERVAL` seconds. When Claude Code goes away it triggers
the graceful shutdown path (``stop_t1_server``) that sends SIGTERM to
chroma's process group, giving chroma's multiprocessing workers and
resource_tracker time to ``sem_unlink`` their POSIX named semaphores
(the worker-cleanup invariant from beads nexus-dc57 / nexus-ze2a).

Defence-in-depth for the three failure modes documented in
anthropics/claude-code#41577 (SessionEnd killed before completion)
and #17885 (SessionEnd never fires on /exit). The watchdog is
independent of any hook firing — as long as the Claude Code root
PID disappears, chroma dies within ``POLL_INTERVAL`` seconds.

Launched as ``python -m nexus.t1_watchdog`` by ``session_start()``.
Intentionally uses only stdlib to avoid import-time overhead; the
hot path is a ``time.sleep`` + two ``os.kill`` calls per tick.
"""
from __future__ import annotations

import argparse
import os
import shutil
import sys
import time
from pathlib import Path

#: Seconds between liveness checks. Trade-off: lower value means faster
#: reap when Claude Code dies; higher value means less CPU. 5s keeps
#: the observable leak window small without adding meaningful load.
POLL_INTERVAL: float = 5.0


def _is_alive(pid: int) -> bool:
    """Return True if *pid* names a live process.

    Uses ``os.kill(pid, 0)`` — the POSIX liveness-test idiom. A
    ``PermissionError`` means the process exists but we can't signal
    it (uid mismatch), so treat as alive. ``ProcessLookupError`` is
    the only definitive "gone" signal.
    """
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _cleanup(
    *, chroma_pid: int, session_file: Path | None, tmpdir: Path | None,
) -> None:
    """Graceful shutdown: stop chroma, then remove state on disk.

    Imports ``stop_t1_server`` lazily so the watchdog's hot loop stays
    free of nexus package imports (keeps startup cost negligible).
    """
    try:
        from nexus.session import stop_t1_server
        stop_t1_server(chroma_pid)
    except Exception:
        # Best-effort: the chroma process may already be gone, or
        # nexus may not be importable (unusual install). Proceed to
        # on-disk cleanup regardless.
        pass

    if session_file is not None:
        try:
            session_file.unlink(missing_ok=True)
        except OSError:
            pass

    if tmpdir is not None:
        try:
            shutil.rmtree(tmpdir, ignore_errors=True)
        except OSError:
            pass


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="T1 ChromaDB server watchdog (nexus-99jb).",
    )
    parser.add_argument("--claude-pid", type=int, required=True,
                        help="Root Claude Code PID to watch.")
    parser.add_argument("--chroma-pid", type=int, required=True,
                        help="PID of the chroma server to supervise.")
    parser.add_argument("--session-file", default="",
                        help="Session record file to remove on cleanup.")
    parser.add_argument("--tmpdir", default="",
                        help="ChromaDB tmpdir to remove on cleanup.")
    args = parser.parse_args(argv)

    session_file = Path(args.session_file) if args.session_file else None
    tmpdir = Path(args.tmpdir) if args.tmpdir else None

    while True:
        time.sleep(POLL_INTERVAL)
        # Chroma crashed or was stopped by SessionEnd — nothing to
        # watch anymore. Exit silently; the SessionEnd path owns the
        # on-disk cleanup when it was responsible for the stop.
        if not _is_alive(args.chroma_pid):
            return 0
        # Claude Code is gone. Trigger graceful shutdown.
        if not _is_alive(args.claude_pid):
            break

    _cleanup(
        chroma_pid=args.chroma_pid,
        session_file=session_file,
        tmpdir=tmpdir,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
