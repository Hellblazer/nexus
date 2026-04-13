# SPDX-License-Identifier: AGPL-3.0-or-later
"""CLI command for L1 context cache management (RDR-072)."""
from __future__ import annotations

import click


@click.group()
def context() -> None:
    """Manage the L1 context cache for agent session startup."""


@context.command("refresh")
@click.option("--global", "global_", is_flag=True, help="Include all collections, not just current repo")
def refresh_cmd(global_: bool) -> None:
    """Regenerate the L1 context cache from taxonomy topics."""
    from pathlib import Path

    from nexus.context import refresh_context_l1

    repo_path = None if global_ else Path.cwd()
    result = refresh_context_l1(repo_path=repo_path)
    if result:
        content = result.read_text()
        tokens = len(content) // 4
        click.echo(f"Context cache refreshed: {result} ({len(content)} chars, ~{tokens} tokens)")
    else:
        click.echo("No taxonomy topics found. Run `nx taxonomy discover --all` first.")


@context.command("show")
def show_cmd() -> None:
    """Show the current L1 context cache content."""
    from pathlib import Path

    from nexus.context import _context_path_for_repo

    path = _context_path_for_repo(Path.cwd())
    if path.exists():
        click.echo(path.read_text())
    else:
        click.echo("No context cache. Run `nx context refresh` to generate.")
