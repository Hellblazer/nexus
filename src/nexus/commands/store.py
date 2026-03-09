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

    database = get_credential("chroma_database")
    api_key = get_credential("chroma_api_key")
    voyage_api_key = get_credential("voyage_api_key")

    if not api_key:
        raise click.ClickException(
            "chroma_api_key not set — run: nx config set chroma_api_key <value>"
        )
    if not voyage_api_key:
        raise click.ClickException(
            "voyage_api_key not set — run: nx config set voyage_api_key <value>"
        )
    if not database:
        raise click.ClickException(
            "chroma_database not set — run: nx config init"
        )
    try:
        return make_t3()
    except RuntimeError as exc:
        raise click.ClickException(str(exc)) from exc


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

    \b
    Examples:
      nx store put ./notes.md --collection knowledge --tags "arch,decision"
      echo "key insight" | nx store put - --title "finding-01" --collection knowledge
      nx store put ./doc.md --ttl 30d --title "sprint-notes"
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


@store.command("list")
@click.option("--collection", "-c", default="knowledge", show_default=True,
              help="Collection name or prefix (default: knowledge)")
@click.option("--limit", "-n", default=200, show_default=True,
              help="Maximum entries to show")
def list_cmd(collection: str, limit: int) -> None:
    """List entries in a T3 knowledge collection."""
    col_name = t3_collection_name(collection)
    entries = _t3().list_store(col_name, limit=limit)
    if not entries:
        click.echo(f"No entries in {col_name}.")
        return
    click.echo(f"{col_name}  ({len(entries)} {'entry' if len(entries) == 1 else 'entries'})\n")
    for e in entries:
        doc_id = e.get("id", "")[:16]
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



@store.command("get")
@click.argument("doc_id")
@click.option("--collection", "-c", default="knowledge", show_default=True,
              help="Collection name or prefix (default: knowledge)")
@click.option("--json", "json_out", is_flag=True, default=False,
              help="Output as JSON")
def get_cmd(doc_id: str, collection: str, json_out: bool) -> None:
    """Retrieve a T3 knowledge entry by its document ID.

    DOC_ID is the 16-char hex ID shown by 'nx store list'.

    \b
    Examples:
      nx store get a1b2c3d4e5f6g7h8
      nx store get a1b2c3d4e5f6g7h8 --collection code__myrepo --json
    """
    col_name = t3_collection_name(collection)
    entry = _t3().get_by_id(col_name, doc_id)
    if entry is None:
        raise click.ClickException(f"Entry {doc_id!r} not found in {col_name}")

    if json_out:
        import json
        click.echo(json.dumps(entry, indent=2))
    else:
        title = entry.get("title", "")
        tags = entry.get("tags", "")
        indexed_at = (entry.get("indexed_at") or "")[:10]
        click.echo(f"ID:         {entry['id']}")
        click.echo(f"Collection: {col_name}")
        if title:
            click.echo(f"Title:      {title}")
        if tags:
            click.echo(f"Tags:       {tags}")
        if indexed_at:
            click.echo(f"Indexed:    {indexed_at}")
        click.echo(f"\n{entry.get('content', '')}")


@store.command("delete")
@click.option("--collection", "-c", required=True,
              help="Collection name (required)")
@click.option("--id", "doc_id", default=None,
              help="Exact 16-char document ID from 'nx store list'")
@click.option("--title", default=None,
              help="Exact title metadata match (deletes all matching chunks)")
@click.option("--yes", "-y", is_flag=True, default=False,
              help="Skip confirmation prompt")
def delete_cmd(collection: str, doc_id: str | None, title: str | None, yes: bool) -> None:
    """Delete an entry from a T3 knowledge collection.

    Use --id for a single known entry, --title to delete all chunks of a document.
    To remove an entire collection use: nx collection delete <name>
    """
    if not doc_id and not title:
        raise click.UsageError("provide --id or --title")
    if doc_id and title:
        raise click.UsageError("--id and --title are mutually exclusive")

    col_name = t3_collection_name(collection)
    db = _t3()

    if doc_id:
        if not db.delete_by_id(col_name, doc_id):
            raise click.ClickException(f"Entry {doc_id!r} not found in {col_name}")
        click.echo(f"Deleted: {doc_id}  from  {col_name}")
    else:
        ids = db.find_ids_by_title(col_name, title)
        if not ids:
            raise click.ClickException(f"No entries with title {title!r} in {col_name}")
        if not yes:
            n = "entry" if len(ids) == 1 else "entries"
            click.echo(f"Found {len(ids)} {n} with title {title!r} in {col_name}.")
            click.confirm("Delete?", abort=True)
        db.batch_delete(col_name, ids)
        click.echo(f"Deleted {len(ids)} {'entry' if len(ids) == 1 else 'entries'} with title {title!r} from {col_name}.")

@store.command("expire")
def expire_cmd() -> None:
    """Remove T3 knowledge__ entries whose TTL has expired."""
    count = _t3().expire()
    click.echo(f"Expired {count} {'entry' if count == 1 else 'entries'}.")
