# SPDX-License-Identifier: AGPL-3.0-or-later
"""nx hook — SessionStart and SessionEnd hook subcommands."""
import json
import os
import sys
from typing import IO

import click
import structlog

from nexus import hooks

_log = structlog.get_logger()


def _read_stdin_session_id(stream: IO[str]) -> str | None:
    """Read and parse a Claude-Code SessionStart JSON payload from *stream*.

    Returns the ``session_id`` field or ``None`` when no usable payload
    is available. Designed to be safe against the three problematic
    inputs that produced nexus-rv2x:

    * **TTY stdin** (no piped input). Reading would block until EOF
      (Ctrl+D) or process death. Detected via ``isatty()`` and skipped
      without calling ``read()``.
    * **Empty / closed pipe**. ``read()`` returns ``""`` promptly;
      ``json.loads`` raises; helper returns ``None``.
    * **Malformed JSON**. Same swallow-and-return-None as empty.

    The Claude Code invocation path is unchanged: a pipe carrying a
    valid JSON payload reads and returns the ``session_id`` as before.
    """
    try:
        if stream.isatty():
            return None
        raw = stream.read()
    except Exception as exc:
        _log.debug("session_start_stdin_read_failed", error=str(exc))
        return None
    if not raw:
        return None
    try:
        data = json.loads(raw)
    except Exception as exc:
        _log.debug("session_start_stdin_parse_failed", error=str(exc))
        return None
    if not isinstance(data, dict):
        return None
    sid = data.get("session_id")
    return sid if isinstance(sid, str) and sid else None


@click.group("hook")
def hook_group() -> None:
    """Claude Code lifecycle hook runners."""


@hook_group.command("session-start")
def session_start_cmd() -> None:
    """Run the SessionStart hook (called by Claude Code on session open)."""
    # nexus-rv2x: TTY-aware stdin parse. Skips read() on a TTY so
    # ``nx hook session-start`` invoked from a shell does not hang.
    claude_session_id = _read_stdin_session_id(sys.stdin)
    output = hooks.session_start(claude_session_id=claude_session_id)
    click.echo(output)


@hook_group.command("session-end")
def session_end_cmd() -> None:
    """Run the SessionEnd hook (called by Claude Code on session close)."""
    output = hooks.session_end()
    click.echo(output)


@hook_group.command("session-end-flush")
def session_end_flush_cmd() -> None:
    """Run only the storage-flush portion of SessionEnd (RDR-094 Phase B).

    Runs T1 scratch flush + T2 expire; does NOT touch chroma. The
    nx-session-end-launcher's grandchild dispatches to this entry
    point (Phase C / nexus-l828); the chroma teardown is owned by
    nx-mcp's lifespan + signal handler + atexit chain.
    """
    output = hooks.session_end_flush()
    click.echo(output)


@hook_group.command("session-end-detach")
def session_end_detach_cmd() -> None:
    """Fire-and-forget SessionEnd runner (nexus-99jb Layer 2).

    Double-forks into a detached grandchild that runs ``session_end``
    synchronously, then returns control to Claude Code in <50ms. This
    pattern survives the SIGTERM Claude Code sends to hook subprocesses
    at session close (anthropics/claude-code#41577) because by the time
    the signal is delivered the grandchild has already been reparented
    to init and no longer shares the hook's process group.

    On platforms without ``os.fork`` (Windows), falls through to the
    synchronous path so behaviour degrades gracefully rather than
    silently skipping the cleanup entirely.
    """
    if not hasattr(os, "fork"):
        output = hooks.session_end()
        click.echo(output)
        return

    # First fork: allow the parent (us, the hook subprocess Claude Code
    # launched) to return immediately. The child will daemonize.
    try:
        first_pid = os.fork()
    except OSError as exc:
        _log.warning("session_end_detach_fork_failed", error=str(exc))
        hooks.session_end()
        return
    if first_pid > 0:
        # Parent: exit immediately. Claude Code sees exit 0 and moves on.
        os._exit(0)

    # Child: detach from controlling terminal and start a new session
    # so the grandchild isn't in Claude Code's process group and
    # survives a pgrp-wide SIGTERM.
    try:
        os.setsid()
    except OSError:
        pass

    # Second fork: the grandchild is not a session leader, so it can
    # never reacquire a controlling terminal. That's the canonical
    # daemon recipe.
    try:
        second_pid = os.fork()
    except OSError:
        # Best effort: run the work right here if we can't fork again.
        hooks.session_end()
        os._exit(0)
    if second_pid > 0:
        os._exit(0)

    # Redirect stdio to /dev/null so the grandchild doesn't share the
    # hook's fds (Claude Code may close them on shutdown, and writes
    # to a closed fd would kill us).
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

    # Now do the real work. Exceptions are swallowed — we're detached
    # and nothing can observe us anyway.
    try:
        hooks.session_end()
    except Exception:
        pass
    os._exit(0)
