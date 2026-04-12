# SPDX-License-Identifier: AGPL-3.0-or-later
"""CLI command group for topic taxonomy (RDR-061 P3-2)."""
import click

from nexus.commands._helpers import default_db_path as _default_db_path
from nexus.db.t2 import T2Database


@click.group()
def taxonomy() -> None:
    """Topic taxonomy — browsable knowledge hierarchy."""


@taxonomy.command("list")
@click.option("--collection", "-c", default="", help="Filter by collection/project")
@click.option("--depth", "-d", default=2, type=int, help="Tree depth", show_default=True)
def list_cmd(collection: str, depth: int) -> None:
    """Show topic tree."""
    from nexus.taxonomy import get_topic_tree

    depth = min(depth, 4)
    with T2Database(_default_db_path()) as db:
        tree = get_topic_tree(db, collection, max_depth=depth)
    if not tree:
        click.echo("No topics found. Run `nx taxonomy rebuild --project <name>` first.")
        return
    for node in tree:
        _print_tree(node, indent=0)


def _print_tree(node: dict, indent: int = 0) -> None:
    prefix = "  " * indent + ("├── " if indent > 0 else "")
    click.echo(f"{prefix}{node['label']} ({node['doc_count']} docs)")
    for child in node.get("children", []):
        _print_tree(child, indent + 1)


@taxonomy.command("show")
@click.argument("topic_id", type=int)
@click.option("--limit", "-n", default=20, help="Max docs to show", show_default=True)
def show_cmd(topic_id: int, limit: int) -> None:
    """Show documents assigned to a topic."""
    from nexus.taxonomy import get_topic_docs

    with T2Database(_default_db_path()) as db:
        docs = get_topic_docs(db, topic_id, limit=limit)
    if not docs:
        click.echo(f"No documents in topic {topic_id}.")
        return
    click.echo(f"Topic {topic_id}: {len(docs)} documents")
    click.echo("-" * 60)
    for doc in docs:
        click.echo(f"  {doc['doc_id']}")


@taxonomy.command("rebuild")
@click.option("--project", "-p", required=True, help="Project to rebuild taxonomy for")
@click.option("-k", default=None, type=int, help="Number of clusters (auto if omitted)")
def rebuild_cmd(project: str, k: int | None) -> None:
    """Rebuild topic taxonomy from scratch.

    RDR-070: rebuild now requires embeddings from a T3 collection.
    Use ``nx taxonomy discover`` (nexus-2dq) instead.
    """
    click.echo(
        "Error: `nx taxonomy rebuild` requires the new discover pipeline (RDR-070).\n"
        "Use `nx taxonomy discover --collection <name>` once nexus-2dq lands.",
        err=True,
    )
    raise SystemExit(1)
