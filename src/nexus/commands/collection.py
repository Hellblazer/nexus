# SPDX-License-Identifier: AGPL-3.0-or-later
import sys

import click

from nexus.commands.store import _t3
from nexus.corpus import embedding_model_for_collection, index_model_for_collection


@click.group()
def collection() -> None:
    """Manage ChromaDB collections (list, delete, rename)."""


@collection.command("list")
def list_cmd() -> None:
    """List all T3 collections with document counts."""
    cols = _t3().list_collections()
    if not cols:
        click.echo("No collections found.")
        return
    width = max(len(c["name"]) for c in cols)
    for c in sorted(cols, key=lambda x: x["name"]):
        click.echo(f"{c['name']:<{width}}  {c['count']:>6} docs")


@collection.command("info")
@click.argument("name")
def info_cmd(name: str) -> None:
    """Show details for a single collection."""
    db = _t3()
    cols = db.list_collections()
    match = next((c for c in cols if c["name"] == name), None)
    if match is None:
        raise click.ClickException(f"Collection not found: {name}")

    query_model = embedding_model_for_collection(name)
    idx_model   = index_model_for_collection(name)

    info = db.collection_info(name)

    col = db.get_or_create_collection(name)
    result = col.get(include=["metadatas"])
    metadatas: list[dict] = result.get("metadatas") or []
    timestamps = [m["indexed_at"] for m in metadatas if m and "indexed_at" in m]
    last_indexed = max(timestamps) if timestamps else "unknown"

    click.echo(f"Collection:  {match['name']}")
    click.echo(f"Documents:   {match['count']}")
    click.echo(f"Index model: {idx_model}")
    click.echo(f"Query model: {query_model}")
    click.echo(f"Indexed:     {last_indexed}")


@collection.command("delete")
@click.argument("name")
@click.option("--yes", "-y", "--confirm", is_flag=True, help="Skip interactive confirmation prompt")
def delete_cmd(name: str, yes: bool) -> None:
    """Delete a T3 collection (irreversible)."""
    if not yes:
        click.confirm(f"Delete collection '{name}'? This cannot be undone.", abort=True)
    _t3().delete_collection(name)
    click.echo(f"Deleted: {name}")


@collection.command("verify")
@click.argument("name")
@click.option("--deep", is_flag=True, help="Run embedding probe query to verify index health")
def verify_cmd(name: str, deep: bool) -> None:
    """Verify a collection exists and report its document count."""
    db = _t3()
    cols = db.list_collections()
    match = next((c for c in cols if c["name"] == name), None)
    if match is None:
        raise click.ClickException(f"Collection not found: {name}")

    if not deep:
        click.echo(f"Collection '{name}': {match['count']} documents — OK")
        return

    count = match["count"]
    if count == 0:
        click.echo(f"Warning: collection '{name}' is empty (0 documents) — skipping embedding probe")
        return

    try:
        db.search(query="health check probe", collection_names=[name], n_results=1)
        click.echo(f"Collection '{name}': {count} documents — embedding health OK")
    except Exception as exc:
        click.echo(f"Error: embedding probe failed for '{name}': {exc}", err=True)
        sys.exit(1)
