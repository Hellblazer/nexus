# SPDX-License-Identifier: AGPL-3.0-or-later
"""CLI command group for retrieval feedback (RDR-061 E2)."""
import click

from nexus.commands._helpers import default_db_path as _default_db_path
from nexus.db.t2 import T2Database


@click.group()
def feedback() -> None:
    """Retrieval feedback — tracks which search results are used."""


@feedback.command("stats")
@click.option("--collection", "-c", default=None, help="Filter by collection name")
@click.option("--limit", "-n", default=20, help="Max rows to show", show_default=True)
def stats_cmd(collection: str | None, limit: int) -> None:
    """Show recent retrieval feedback entries."""
    from nexus.feedback import query_feedback_stats

    with T2Database(_default_db_path()) as db:
        rows = query_feedback_stats(db, collection=collection, limit=limit)
    if not rows:
        click.echo("No feedback entries found.")
        return
    # Header
    click.echo(f"{'doc_id':<40} {'collection':<25} {'action':<15} {'ts'}")
    click.echo("-" * 100)
    for row in rows:
        click.echo(
            f"{row['doc_id']:<40} {row['collection']:<25} {row['action']:<15} {row['ts']}"
        )
    click.echo(f"\n{len(rows)} entries shown.")
