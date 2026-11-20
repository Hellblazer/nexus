# SPDX-License-Identifier: AGPL-3.0-or-later
"""``nx brief [TOPIC]``: reference context SessionStart no longer injects (see :mod:`nexus.brief`)."""
from __future__ import annotations

import click

from nexus.hooks.session_context import BRIEF_TOPICS


@click.command("brief")
@click.argument("topic", required=False, type=click.Choice(BRIEF_TOPICS, case_sensitive=False))
def brief_cmd(topic: str | None) -> None:
    """Print context SessionStart does not inject: T2 memory titles, ready beads, tool notes, the Knowledge Map, the RDR count, or your startup note.

    With no TOPIC, lists the topics. Each topic prints the same text the SessionStart hook used to inject, from the
    same code, for the repo in the working directory.
    """
    from nexus import brief  # noqa: PLC0415 — deferred: keeps CLI startup light

    if topic is None:
        click.echo(brief.index_text())
        return
    result = brief.render(topic, source="cli")
    if not result.ok:
        click.echo(result.text, err=True)
        raise SystemExit(1)
    click.echo(result.text)
