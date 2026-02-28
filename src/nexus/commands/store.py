# SPDX-License-Identifier: AGPL-3.0-or-later
import sys
from pathlib import Path

import click

from nexus.corpus import t3_collection_name
from nexus.db.t3_stores import t3_knowledge
from nexus.ttl import parse_ttl


@click.group()
def store() -> None:
    """Permanent semantic knowledge store (ChromaDB Cloud + Voyage AI)."""


@store.command("put")
@click.argument("source")
@click.option("--collection", "-c", default="knowledge", show_default=True,
              help="Collection name or prefix (default: knowledge)")
@click.option("--title", "-t", default="", help="Document title (required when SOURCE is -)")
@click.option("--tags", default="", help="Comma-separated tags")
@click.option("--category", default="", help="Category label")
@click.option("--ttl", default="permanent", show_default=True,
              help="TTL: Nd, Nw, or permanent")
@click.option("--session-id", default="", hidden=True)
@click.option("--agent", default="", hidden=True, help="Source agent name")
def put_cmd(
    source: str,
    collection: str,
    title: str,
    tags: str,
    category: str,
    ttl: str,
    session_id: str,
    agent: str,
) -> None:
    """Store SOURCE (file path or '-' for stdin) in the T3 knowledge store.

    SOURCE may be a file path or '-' to read from stdin.  When reading from
    stdin, --title is required.
    """
    if source == "-":
        if not title:
            raise click.ClickException("--title is required when reading from stdin (-)")
        content = sys.stdin.read()
    else:
        path = Path(source)
        if not path.exists():
            raise click.ClickException(f"File not found: {source}")
        try:
            content = path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            raise click.ClickException(f"File {source!r} is not valid UTF-8.")
        if not title:
            title = path.name

    try:
        days = parse_ttl(ttl)
    except ValueError as exc:
        raise click.ClickException(str(exc)) from exc
    ttl_days = days if days is not None else 0

    col_name = t3_collection_name(collection)
    if not col_name.startswith("knowledge__"):
        raise click.ClickException(
            f"'nx store put' writes to the knowledge store only; "
            f"got {col_name!r} (prefix {col_name.split('__')[0]!r}). "
            f"Use 'nx index' for code/docs/rdr collections."
        )
    db = t3_knowledge()
    doc_id = db.put(
        collection=col_name,
        content=content,
        title=title,
        tags=tags,
        category=category,
        session_id=session_id,
        source_agent=agent,
        ttl_days=ttl_days,
    )
    click.echo(f"Stored: {doc_id}  →  {col_name}")


@store.command("list")
@click.option("--collection", "-c", default="knowledge", show_default=True,
              help="Collection name or prefix (default: knowledge)")
@click.option("--limit", "-n", default=200, show_default=True,
              help="Maximum entries to show")
def list_cmd(collection: str, limit: int) -> None:
    """List entries in a T3 knowledge collection."""
    col_name = t3_collection_name(collection)
    entries = t3_knowledge().list_store(col_name, limit=limit)
    if not entries:
        click.echo(f"No entries in {col_name}.")
        return
    click.echo(f"{col_name}  ({len(entries)} {'entry' if len(entries) == 1 else 'entries'})\n")
    for e in entries:
        doc_id = e.get("id", "")[:12]
        title = (e.get("title") or "")[:40]
        tags = e.get("tags") or ""
        ttl_days = e.get("ttl_days", 0)
        expires_at = e.get("expires_at") or ""
        indexed_at = (e.get("indexed_at") or "")[:10]  # date only
        if ttl_days and ttl_days > 0 and expires_at:
            ttl_str = f"expires {expires_at[:10]}"
        else:
            ttl_str = "permanent"
        tag_str = f"  [{tags}]" if tags else ""
        click.echo(f"  {doc_id}  {title:<40}  {ttl_str:<24}  {indexed_at}{tag_str}")


@store.command("expire")
def expire_cmd() -> None:
    """Remove T3 knowledge__ entries whose TTL has expired."""
    count = t3_knowledge().expire()
    click.echo(f"Expired {count} {'entry' if count == 1 else 'entries'}.")
