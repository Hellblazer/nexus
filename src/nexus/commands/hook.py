# SPDX-License-Identifier: AGPL-3.0-or-later
"""nx hook — SessionStart and SessionEnd hook subcommands."""
import json
import sys

import click
import structlog

from nexus import hooks

_log = structlog.get_logger()


@click.group("hook")
def hook_group() -> None:
    """Claude Code lifecycle hook runners."""


@hook_group.command("session-start")
def session_start_cmd() -> None:
    """Run the SessionStart hook (called by Claude Code on session open)."""
    # Claude Code pipes a JSON payload to stdin with session_id
    claude_session_id = None
    try:
        data = json.loads(sys.stdin.read())
        claude_session_id = data.get("session_id")
    except Exception as exc:
        _log.debug("session_start_stdin_parse_failed", error=str(exc))
    output = hooks.session_start(claude_session_id=claude_session_id)
    click.echo(output)


@hook_group.command("session-end")
def session_end_cmd() -> None:
    """Run the SessionEnd hook (called by Claude Code on session close)."""
    output = hooks.session_end()
    click.echo(output)
