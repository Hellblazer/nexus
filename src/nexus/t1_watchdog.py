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

import structlog

from nexus.logging_setup import configure_logging

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


#: Grace period for the MCP server's lifespan finally / atexit to
#: clean chroma after we send SIGTERM on Claude crash. Long enough
#: for anyio cancellation to propagate through the async context
#: manager but short enough that the watchdog reaps quickly if the
#: MCP server is wedged.
MCP_GRACE_SECS: float = 2.0


def _signal_then_kill(pid: int) -> None:
    """SIGTERM the pid, wait MCP_GRACE_SECS, SIGKILL if still alive.

    Used on Claude-crash trigger to give the orphaned MCP server a
    chance to run its lifespan finally block before we fall through
    to the chroma-pgrp belt-and-braces cleanup.
    """
    import signal

    try:
        os.kill(pid, signal.SIGTERM)
    except (ProcessLookupError, PermissionError, OSError):
        return
    time.sleep(MCP_GRACE_SECS)
    if _is_alive(pid):
        try:
            os.kill(pid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError, OSError):
            pass


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="T1 ChromaDB server watchdog (nexus-99jb / RDR-094).",
    )
    parser.add_argument("--claude-pid", type=int, required=True,
                        help="Root Claude Code PID to watch.")
    parser.add_argument("--chroma-pid", type=int, required=True,
                        help="PID of the chroma server to supervise.")
    parser.add_argument(
        "--mcp-pid", type=int, default=0,
        help=(
            "Optional MCP server PID for dual-watch mode (RDR-094 "
            "FM-NEW-1). When set, OR-trigger logic fires: mcp_pid "
            "death cleans chroma directly (lifespan/atexit did not "
            "run); claude_pid death sends SIGTERM to mcp_pid (giving "
            "lifespan finally a chance to run), waits MCP_GRACE_SECS, "
            "then SIGKILLs and cleans chroma. When 0 (default), "
            "single-watch claude-only mode is preserved for backwards "
            "compat with the hook-spawned watchdog."
        ),
    )
    parser.add_argument("--session-file", default="",
                        help="Session record file to remove on cleanup.")
    parser.add_argument("--tmpdir", default="",
                        help="ChromaDB tmpdir to remove on cleanup.")
    args = parser.parse_args(argv)

    configure_logging("watchdog")
    log = structlog.get_logger("nexus.t1_watchdog")

    session_file = Path(args.session_file) if args.session_file else None
    tmpdir = Path(args.tmpdir) if args.tmpdir else None
    dual_watch = args.mcp_pid > 0

    log.info(
        "watchdog_started",
        claude_pid=args.claude_pid,
        chroma_pid=args.chroma_pid,
        mcp_pid=args.mcp_pid,
        dual_watch=dual_watch,
        poll_interval_s=POLL_INTERVAL,
    )

    while True:
        time.sleep(POLL_INTERVAL)
        log.debug(
            "poll_tick",
            chroma_pid=args.chroma_pid,
            claude_pid=args.claude_pid,
            mcp_pid=args.mcp_pid,
        )
        # Chroma crashed or was stopped by some other path (lifespan,
        # atexit, SessionEnd in legacy mode) -- nothing to watch
        # anymore. Exit silently; whoever stopped chroma owns the
        # on-disk cleanup.
        if not _is_alive(args.chroma_pid):
            log.info(
                "watchdog_exiting",
                reason="chroma_died_externally",
                chroma_pid=args.chroma_pid,
            )
            return 0
        # Dual-watch mode (RDR-094 FM-NEW-1): MCP server died without
        # its lifespan/atexit firing (SIGKILL, segfault). Clean chroma
        # directly via the chroma-pgrp belt-and-braces path.
        if dual_watch and not _is_alive(args.mcp_pid):
            log.info("mcp_pid_disappeared", mcp_pid=args.mcp_pid)
            break
        # Claude Code is gone.
        if not _is_alive(args.claude_pid):
            log.info("claude_pid_disappeared", claude_pid=args.claude_pid)
            # Dual-watch + Claude crash: signal mcp_pid first so its
            # lifespan finally runs (cleans chroma cleanly), then fall
            # through to the chroma-pgrp belt-and-braces in case the
            # MCP server's finally is wedged or already ran.
            if dual_watch and _is_alive(args.mcp_pid):
                log.info("signalling_mcp_pid", mcp_pid=args.mcp_pid)
                _signal_then_kill(args.mcp_pid)
            break

    log.info("chroma_cleanup_started", chroma_pid=args.chroma_pid)
    _cleanup(
        chroma_pid=args.chroma_pid,
        session_file=session_file,
        tmpdir=tmpdir,
    )
    log.info("chroma_cleanup_complete", chroma_pid=args.chroma_pid)
    log.info("watchdog_exiting", reason="cleanup_complete")
    return 0


if __name__ == "__main__":
    sys.exit(main())
