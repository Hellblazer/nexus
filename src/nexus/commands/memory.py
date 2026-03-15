# SPDX-License-Identifier: AGPL-3.0-or-later
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import click

from nexus.commands._helpers import default_db_path as _default_db_path
from nexus.config import get_credential
from nexus.db.t2 import T2Database
from nexus.db.t3 import T3Database
from nexus.ttl import parse_ttl


@click.group()
def memory() -> None:
    """Persistent per-project memory (survives across sessions)."""


@memory.command("put")
@click.argument("content")
@click.option("--project", "-p", required=True, help="Project namespace (e.g. BFDB)")
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
@click.argument("entry_id", metavar="ID", required=False, type=int)
@click.option("--project", "-p", default=None, help="Project namespace")
@click.option("--title", "-t", default=None, help="Entry title")
def get_cmd(entry_id: int | None, project: str | None, title: str | None) -> None:
    """Retrieve a memory entry by ID or by --project + --title."""
    if entry_id is None and not (project and title):
        raise click.UsageError("provide an ID or --project and --title")
    with T2Database(_default_db_path()) as db:
        if entry_id is not None:
            result = db.get(id=entry_id)
        else:
            result = db.get(project=project, title=title)
    if result is None:
        raise click.ClickException("entry not found — use: nx memory list to see available entries")
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
        click.echo(f"[{e['id']}] {e['project']}/{e['title']}  ({agent_str}, {e['timestamp']})")


@memory.command("delete")
@click.option("--project", "-p", default=None, help="Project namespace")
@click.option("--title", "-t", default=None, help="Entry title")
@click.option("--id", "entry_id", default=None, type=int, help="Numeric row ID")
@click.option("--all", "all_entries", is_flag=True, default=False, help="Delete all entries in --project")
@click.option("--yes", "-y", is_flag=True, default=False, help="Skip confirmation prompt")
def delete_cmd(
    project: str | None,
    title: str | None,
    entry_id: int | None,
    all_entries: bool,
    yes: bool,
) -> None:
    """Delete one or more memory entries."""
    # Mutual exclusion — Click has no built-in mechanism; enforce manually.
    if entry_id is not None and (project or title or all_entries):
        raise click.UsageError("--id is mutually exclusive with --project, --title, and --all")
    if all_entries and not project:
        raise click.UsageError("--all requires --project")
    if all_entries and title:
        raise click.UsageError("--all and --title are mutually exclusive")
    if entry_id is None and not all_entries and not (project and title):
        raise click.UsageError("provide --id, or --project and --title, or --project and --all")

    with T2Database(_default_db_path()) as db:
        if all_entries:
            entries = db.list_entries(project=project)
            count = len(entries)
            if count == 0:
                raise click.ClickException(f"No entries found in project {project!r}")
            if not yes:
                n = "entry" if count == 1 else "entries"
                click.echo(f"Found {count} {n} in {project!r}.")
                click.confirm(f"Delete {count} {n} from {project!r}?", abort=True)
            for e in entries:
                db.delete(project=project, title=e["title"])
            click.echo(f"Deleted {count} {'entry' if count == 1 else 'entries'} from {project!r}.")
        else:
            entry = db.get(id=entry_id) if entry_id is not None else db.get(project=project, title=title)
            if entry is None:
                raise click.ClickException("entry not found — use: nx memory list to see available entries")
            if not yes:
                preview = entry["content"][:120].replace("\n", " ")
                click.echo(f"{entry['project']}/{entry['title']}")
                click.echo(f"  {preview}")
                click.confirm("Delete?", abort=True)
            if entry_id is not None:
                db.delete(id=entry_id)
            else:
                db.delete(project=entry["project"], title=entry["title"])
            click.echo(f"Deleted: {entry['project']}/{entry['title']}")


@memory.command("expire")
def expire_cmd() -> None:
    """Remove TTL-expired memory entries."""
    with T2Database(_default_db_path()) as db:
        count = db.expire()
    click.echo(f"Expired {count} {'entry' if count == 1 else 'entries'}.")


@memory.command("promote")
@click.argument("entry_id", metavar="ID", type=int)
@click.option("--collection", required=True, help="Target T3 collection name (e.g. knowledge__myproject)")
@click.option("--tags", default="", help="Comma-separated tags (overrides T2 tags when provided)")
@click.option("--remove", is_flag=True, default=False, help="Delete the entry from T2 after promoting.")
def promote_cmd(entry_id: int, collection: str, tags: str, remove: bool) -> None:
    """Promote a T2 memory entry to T3 ChromaDB permanent storage."""
    with T2Database(_default_db_path()) as db:
        entry = db.get(id=entry_id)
        if entry is None:
            raise click.ClickException(f"Entry {entry_id} not found in T2 memory.")

        from nexus.config import is_local_mode
        from nexus.db import make_t3

        if not is_local_mode():
            missing = [
                k
                for k in ("chroma_api_key", "voyage_api_key", "chroma_database")
                if not get_credential(k)
            ]
            if missing:
                raise click.ClickException(
                    f"{', '.join(missing)} not set — run: nx config init"
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

        with make_t3() as t3:
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
