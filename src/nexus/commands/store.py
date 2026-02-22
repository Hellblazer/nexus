# SPDX-License-Identifier: AGPL-3.0-or-later
import sys
from pathlib import Path

import click

from nexus.corpus import t3_collection_name
from nexus.db import make_t3
from nexus.db.t3 import T3Database
from nexus.ttl import parse_ttl


def _t3() -> T3Database:
    from nexus.config import get_credential

    tenant = get_credential("chroma_tenant")
    database = get_credential("chroma_database")
    api_key = get_credential("chroma_api_key")
    voyage_api_key = get_credential("voyage_api_key")

    if not api_key:
        raise click.ClickException(
            "CHROMA_API_KEY is not set. Run 'nx config init' or set the environment variable."
        )
    if not voyage_api_key:
        raise click.ClickException(
            "VOYAGE_API_KEY is not set. Run 'nx config init' or set the environment variable."
        )
    if not tenant or not database:
        raise click.ClickException(
            "ChromaDB tenant/database not configured. "
            "Run 'nx config init' or set chroma_tenant/chroma_database via 'nx config set'."
        )
    return make_t3()


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
        content = path.read_text()
        if not title:
            title = path.name

    try:
        days = parse_ttl(ttl)
    except ValueError as exc:
        raise click.ClickException(str(exc)) from exc
    ttl_days = days if days is not None else 0

    col_name = t3_collection_name(collection)
    db = _t3()
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


@store.command("expire")
def expire_cmd() -> None:
    """Remove T3 knowledge__ entries whose TTL has expired."""
    count = _t3().expire()
    click.echo(f"Expired {count} {'entry' if count == 1 else 'entries'}.")
