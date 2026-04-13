# SPDX-License-Identifier: AGPL-3.0-or-later
"""CLI command for L1 context cache management (RDR-072)."""
from __future__ import annotations

import click


@click.group()
def context() -> None:
    """Manage the L1 context cache for agent session startup."""


@context.command("refresh")
def refresh_cmd() -> None:
    """Regenerate the L1 context cache from taxonomy topics."""
    from nexus.context import refresh_context_l1

    result = refresh_context_l1()
    if result:
        content = result.read_text()
        tokens = len(content) // 4
        click.echo(f"Context cache refreshed: {result} ({len(content)} chars, ~{tokens} tokens)")
    else:
        click.echo("No taxonomy topics found. Run `nx taxonomy discover --all` first.")


@context.command("show")
def show_cmd() -> None:
    """Show the current L1 context cache content."""
    from nexus.context import CONTEXT_L1_PATH

    if CONTEXT_L1_PATH.exists():
        click.echo(CONTEXT_L1_PATH.read_text())
    else:
        click.echo("No context cache. Run `nx context refresh` to generate.")
