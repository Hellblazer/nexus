# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""Was this Claude Code session launched with the conexus channel? (RDR-233,
bead nexus-7hzb2).

Claude Code registers a channel listener only when ``claude`` is launched with
``--channels`` (or ``--dangerously-load-development-channels``); no setting or
environment variable does it without the flag. Claude Desktop's Code tab
launches ``claude`` with neither, so the MCP channel waiter's notifications
reach nobody there. The SessionStart guidance and ``nx tuple wait`` both ask
this module whether the flag is present, so the channel-free waiter runs only
where the channel cannot.

The read is the one RDR-211's channel gate used and RDR-213 deleted (commit
f07e37a01): the ``claude`` process's own command line via ``ps -ww -o
command=``, flags matched as whole tokens (measured for both the server and
plugin forms in bead nexus-tk2cz). RDR-213 deleted it as proof that a claim was
safe; here it only picks which wake path to suggest. A present flag does not
prove a registered channel (a plugin off the channel allowlist, channels
disabled by the organisation, an unconfirmed development-channel dialog): such a
session reads as "channel" and gets no waiter, so its mail waits for the next
prompt and its board posts are not pushed, as before RDR-233; ``nx tuple wait
--force`` covers it. A false "no flag" costs a duplicate wake per row.

The answer is three-valued. ``None`` (no ``claude`` ancestor, or its command
line unreadable) means "cannot tell", and each caller keeps its pre-RDR-233
behaviour for it: the guidance keeps the channel wording, ``nx tuple wait``
runs.
"""
from __future__ import annotations

import subprocess
import sys
from collections.abc import Callable

import structlog

from nexus.bounded_subprocess import run_bounded

_log = structlog.get_logger(__name__)

#: The launch options that turn a channel on, and the values that name ours.
#: The plugin form's marketplace segment varies, so it matches up to the ``@``.
CHANNEL_OPTIONS: tuple[str, ...] = ("--channels", "--dangerously-load-development-channels")
CHANNEL_SERVER_VALUE: str = "server:nexus"
CHANNEL_PLUGIN_PREFIX: str = "plugin:conexus@"


def read_command_line(pid: int, *, platform: str | None = None) -> str:
    """*pid*'s full command line, or ``""`` on any failure. Never raises. On
    Windows the kernel answers (:mod:`nexus._install.winproc_core`), not ``ps``."""
    try:
        if (platform if platform is not None else sys.platform) == "win32":
            from nexus._install import winproc_core  # noqa: PLC0415 — deferred: Windows only

            return winproc_core.process_command_line(pid, winproc_core.ctypes_win_info_api())
        result = run_bounded(
            ["ps", "-ww", "-o", "command=", "-p", str(pid)],
            stderr=subprocess.DEVNULL, timeout=2.0,
        )
        return result.stdout or ""
    except Exception as exc:  # noqa: BLE001 — best-effort read; "" means "cannot tell"
        _log.debug("channel_launch_argv_unreadable", pid=pid, error=str(exc))
        return ""


def argv_has_channel_flag(argv: str) -> bool:
    """True iff *argv* passes a channel option a value naming ours: whole
    tokens, so a server named ``nexusdev`` is not read as ours; the value may
    follow ``=`` or be one of several space- or comma-separated values after
    the option."""
    tokens = argv.split()
    for i, token in enumerate(tokens):
        option, eq, inline = token.partition("=")
        if option not in CHANNEL_OPTIONS:
            continue
        values = [inline] if eq else []
        for later in tokens[i + 1:] if not eq else ():
            if later.startswith("-"):
                break
            values.append(later)
        for value in (v for chunk in values for v in chunk.split(",")):
            if value == CHANNEL_SERVER_VALUE or value.startswith(CHANNEL_PLUGIN_PREFIX):
                return True
    return False


def _claude_ancestor_argv() -> str:
    """The nearest ``claude`` ancestor's command line, or ``""`` when there is
    no such ancestor (:func:`nexus.session.find_immediate_claude_pid` falls back
    to the parent pid, so the name is checked here) or it cannot be read."""
    from nexus.session import _command_name_of, find_immediate_claude_pid  # noqa: PLC0415 — deferred: hook startup cost

    pid = find_immediate_claude_pid()
    if not pid or not _command_name_of(pid).lower().startswith("claude"):
        return ""
    return read_command_line(pid)


def channel_launch_state(*, argv_reader: Callable[[], str] | None = None) -> bool | None:
    """``True`` when the nearest ``claude`` ancestor carries a conexus channel
    launch flag, ``False`` when it was read and carries none, ``None`` when it
    could not be read. *argv_reader* is the seam tests replace."""
    argv = (argv_reader or _claude_ancestor_argv)().strip()
    if not argv:
        return None
    return argv_has_channel_flag(argv)
