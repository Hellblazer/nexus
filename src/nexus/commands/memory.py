# SPDX-License-Identifier: AGPL-3.0-or-later
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import click

from nexus.config import get_credential
from nexus.db.t2 import T2Database
from nexus.db.t3 import T3Database
from nexus.ttl import parse_ttl


def _default_db_path() -> Path:
    return Path.home() / ".config" / "nexus" / "memory.db"


@click.group()
def memory() -> None:
    """Persistent per-project memory (survives across sessions)."""


@memory.command("put")
@click.argument("content")
@click.option("--project", "-p", required=True, help="Project namespace (e.g. BFDB_active)")
@click.option("--title", "-t", required=True, help="Entry title/filename")
@click.option("--tags", default="", help="Comma-separated tags")
@click.option("--ttl", default="30d", show_default=True, help="TTL: Nd, Nw, or permanent")
def put_cmd(content: str, project: str, title: str, tags: str, ttl: str) -> None:
    """Write content to the T2 memory bank.

    Use '-' as CONTENT to read from stdin.
    """
    if content == "-":
        content = sys.stdin.read()
    try:
        ttl_days = parse_ttl(ttl)
    except ValueError as exc:
        raise click.ClickException(str(exc)) from exc
    with T2Database(_default_db_path()) as db:
        row_id = db.put(project=project, title=title, content=content, tags=tags, ttl=ttl_days)
    click.echo(f"Stored: {project}/{title} (id={row_id})")


@memory.command("get")
@click.argument("id", required=False, type=int)
@click.option("--project", "-p", default=None, help="Project namespace")
@click.option("--title", "-t", default=None, help="Entry title")
def get_cmd(id: int | None, project: str | None, title: str | None) -> None:
    """Retrieve a memory entry by ID or by --project + --title."""
    if id is None and not (project and title):
        click.echo("Error: provide an ID or --project and --title", err=True)
        raise SystemExit(1)
    with T2Database(_default_db_path()) as db:
        if id is not None:
            result = db.get(id=id)
        else:
            result = db.get(project=project, title=title)
    if result is None:
        click.echo("Not found.", err=True)
        raise SystemExit(1)
    click.echo(result["content"])


@memory.command("search")
@click.argument("query")
@click.option("--project", "-p", default=None, help="Scope search to a project")
def search_cmd(query: str, project: str | None) -> None:
    """FTS5 keyword search across T2 memory entries."""
    with T2Database(_default_db_path()) as db:
        results = db.search(query=query, project=project)
    if not results:
        click.echo("No results found.")
        return
    for r in results:
        agent = r["agent"] or "-"
        click.echo(f"[{r['id']}] {r['project']}/{r['title']}  ({agent}, {r['timestamp']})")
        preview = r["content"][:200].replace("\n", " ")
        click.echo(f"  {preview}")


@memory.command("list")
@click.option("--project", "-p", default=None, help="Filter by project")
@click.option("--agent", "-a", default=None, help="Filter by agent name")
def list_cmd(project: str | None, agent: str | None) -> None:
    """List memory entries."""
    with T2Database(_default_db_path()) as db:
        entries = db.list_entries(project=project, agent=agent)
    if not entries:
        click.echo("No entries found.")
        return
    for e in entries:
        agent_str = e["agent"] or "-"
        click.echo(f"[{e['id']}] {e['title']}  ({agent_str}, {e['timestamp']})")


@memory.command("expire")
def expire_cmd() -> None:
    """Remove TTL-expired memory entries."""
    with T2Database(_default_db_path()) as db:
        count = db.expire()
    click.echo(f"Expired {count} {'entry' if count == 1 else 'entries'}.")


@memory.command("promote")
@click.argument("id", type=int)
@click.option("--collection", required=True, help="Target T3 collection name (e.g. knowledge__myproject)")
@click.option("--tags", default="", help="Comma-separated tags (overrides T2 tags when provided)")
@click.option("--remove", is_flag=True, default=False, help="Delete the entry from T2 after promoting.")
def promote_cmd(id: int, collection: str, tags: str, remove: bool) -> None:
    """Promote a T2 memory entry to T3 ChromaDB permanent storage."""
    with T2Database(_default_db_path()) as db:
        entry = db.get(id=id)
        if entry is None:
            raise click.ClickException(f"Entry {id} not found in T2 memory.")

        missing = [
            k
            for k in ("chroma_api_key", "voyage_api_key", "chroma_tenant", "chroma_database")
            if not get_credential(k)
        ]
        if missing:
            raise click.ClickException(
                f"T3 credentials not configured: {', '.join(missing)}. Run `nx config init`."
            )

        # Translate TTL: T2 ttl=None (permanent) -> T3 ttl_days=0; T2 ttl=N -> T3 ttl_days=N
        ttl_days: int = entry["ttl"] if entry["ttl"] is not None else 0  # type: ignore[assignment]
        merged_tags = tags if tags else (entry.get("tags") or "")

        # Compute expires_at from the T2 entry's original timestamp so that the
        # promoted T3 entry honours the remaining TTL rather than resetting it.
        if ttl_days > 0:
            base_ts = datetime.fromisoformat(entry["timestamp"])
            expires_at = (base_ts + timedelta(days=ttl_days)).isoformat()
        else:
            expires_at = ""  # permanent

        with T3Database(
            tenant=get_credential("chroma_tenant"),
            database=get_credential("chroma_database"),
            api_key=get_credential("chroma_api_key"),
            voyage_api_key=get_credential("voyage_api_key"),
        ) as t3:
            doc_id = t3.put(
                collection=collection,
                content=entry["content"],
                title=entry["title"],
                tags=merged_tags,
                ttl_days=ttl_days,
                expires_at=expires_at,
            )

        if remove:
            db.delete(entry["project"], entry["title"])
            click.echo(
                f"Promoted and removed: {entry['project']}/{entry['title']} -> {collection} (id={doc_id})"
            )
        else:
            click.echo(
                f"Promoted: {entry['project']}/{entry['title']} -> {collection} (id={doc_id})"
            )
