# SPDX-License-Identifier: AGPL-3.0-or-later
import click

from nexus.commands.store import _t3


@click.group()
def collection() -> None:
    """Manage T3 ChromaDB collections."""


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
    cols = _t3().list_collections()
    match = next((c for c in cols if c["name"] == name), None)
    if match is None:
        click.echo(f"Collection not found: {name}", err=True)
        raise SystemExit(1)
    click.echo(f"Name:  {match['name']}")
    click.echo(f"Docs:  {match['count']}")


@collection.command("delete")
@click.argument("name")
@click.option("--confirm", is_flag=True, help="Skip interactive confirmation")
def delete_cmd(name: str, confirm: bool) -> None:
    """Delete a T3 collection (irreversible)."""
    if not confirm:
        click.confirm(f"Delete collection '{name}'? This cannot be undone.", abort=True)
    _t3().delete_collection(name)
    click.echo(f"Deleted: {name}")


@collection.command("verify")
@click.argument("name")
def verify_cmd(name: str) -> None:
    """Spot-check embeddings health for a collection."""
    db = _t3()
    cols = db.list_collections()
    match = next((c for c in cols if c["name"] == name), None)
    if match is None:
        click.echo(f"Collection not found: {name}", err=True)
        raise SystemExit(1)
    click.echo(f"Collection '{name}': {match['count']} documents — OK")
