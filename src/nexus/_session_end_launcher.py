# SPDX-License-Identifier: AGPL-3.0-or-later
"""Fork-first SessionEnd daemonizer (nexus-2u7o, RDR-094 Phase C).

The 4.10.3 double-fork path in ``nexus.commands.hook.session_end_detach_cmd``
waits for Click to parse argv and for ``nexus.hooks`` + friends to import
before calling ``os.fork()``. Cold-start cost on a reference install is
~2 seconds, and Claude Code's shutdown SIGTERM to the hook's process
group arrives faster than that on some machines, so the first fork
never runs and ``Hook cancelled`` is logged instead of the graceful
cleanup.

This module flips the order: the ``__main__`` block uses only ``os``
and ``sys`` from the standard library (both are preloaded by the
interpreter, so no import cost), forks, ``setsid``s, forks again, and
redirects stdio to ``/dev/null`` -- all before touching a single nexus
module. Then in the fully detached grandchild it imports
``nexus.hooks`` and runs ``session_end_flush()``. Wall-clock cost
to return control to Claude Code: ~17ms.

RDR-094 Phase C swap: the launcher now dispatches to
``hooks.session_end_flush`` (storage-only path, fork-safe) instead of
``hooks.session_end``. With the MCP server owning chroma's lifecycle
under ``NEXUS_MCP_OWNS_T1`` (Phase 4), a hook-side
``stop_t1_server`` raced the MCP lifespan/atexit/signal-handler
cleanup. Pointing the launcher at the flush-only entry retires that
race entirely. The legacy ``hooks.session_end`` chroma-stop block is
still reachable via ``nx hook session-end`` for the flag-off rollout
window and as a manual-debug entry point.

**BANNED invariant**: this module must never import any ``nexus.*``
module before ``os.fork()``. The whole point is that the parent
process pays no nexus-import cost before forking off the daemon.
Imports happen only inside ``_run_session_end_synchronously`` which
runs in the grandchild.

Shell invocation (wired into ``nx/hooks/hooks.json``)::

    nx-session-end-launcher

On platforms without ``os.fork`` (Windows), falls through to the
synchronous path so cleanup still happens, at the cost of the hook
blocking until done.
"""
from __future__ import annotations

import os
import sys


def _run_session_end_synchronously() -> None:
    """Import nexus.hooks and call session_end_flush; swallow exceptions.

    Runs in the fully detached grandchild, so exceptions are no longer
    observable by Claude Code -- they must not escape and crash the
    daemon. Logging goes through the structlog pipeline nexus.hooks
    already configures (RotatingFileHandler under ~/.config/nexus/logs).

    RDR-094 Phase C: dispatches to ``session_end_flush`` (storage-only)
    rather than ``session_end`` (storage + chroma teardown). The
    chroma teardown is owned by the MCP server's lifespan/atexit/
    signal handlers under ``NEXUS_MCP_OWNS_T1``; calling it here
    races those paths and was the documented source of double-stop
    failures.
    """
    try:
        from nexus import hooks
        hooks.session_end_flush()
    except Exception:
        # Fully detached; nothing upstream can observe us. Swallow.
        pass


def _daemonize_and_run() -> None:
    """Daemonize via the canonical double-fork + setsid, then run cleanup.

    Contract: returns control to the caller (Claude Code's hook runner)
    in the parent in single-digit milliseconds. The grandchild runs the
    actual cleanup and exits via ``os._exit(0)``.
    """
    # First fork: let the original parent return to the shell /
    # Claude Code immediately.
    try:
        first_pid = os.fork()
    except OSError:
        # No fork available for some reason; fall through to synchronous.
        _run_session_end_synchronously()
        return
    if first_pid > 0:
        return  # Original process — return to Click caller which then exits.

    # Child: create a new session to leave Claude Code's process group
    # so a pgrp-wide SIGTERM from Claude Code doesn't reap us.
    try:
        os.setsid()
    except OSError:
        pass

    # Second fork: ensure the grandchild is not a session leader, so it
    # can never reacquire a controlling terminal (canonical daemon
    # recipe).
    try:
        second_pid = os.fork()
    except OSError:
        _run_session_end_synchronously()
        os._exit(0)
    if second_pid > 0:
        os._exit(0)

    # Grandchild: redirect stdio to /dev/null. Claude Code may close the
    # original hook fds during shutdown; leaving them open would let a
    # write at shutdown kill us with SIGPIPE.
    try:
        devnull = os.open(os.devnull, os.O_RDWR)
        for fd in (0, 1, 2):
            try:
                os.dup2(devnull, fd)
            except OSError:
                pass
        if devnull > 2:
            os.close(devnull)
    except OSError:
        pass

    _run_session_end_synchronously()
    os._exit(0)


def main() -> None:
    if not hasattr(os, "fork"):
        # Windows etc — no fork, run synchronously.
        _run_session_end_synchronously()
        return
    _daemonize_and_run()


if __name__ == "__main__":
    main()
    sys.exit(0)
