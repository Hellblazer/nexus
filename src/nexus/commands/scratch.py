# SPDX-License-Identifier: AGPL-3.0-or-later
import sys
from typing import Any

import click

from nexus.commands._helpers import default_db_path as _default_db_path
from nexus.db.t1 import T1Database
from nexus.db.t2 import T2Database


def _t1() -> T1Database:
    """Return a T1Database connected to the current session's server.

    T1Database resolves the session automatically via the PPID chain, falling
    back to a local EphemeralClient if no server record is found.
    """
    return T1Database()


@click.group()
def scratch() -> None:
    """Temporary in-session scratch space (cleared when the session ends)."""


@scratch.command("put")
@click.argument("content")
@click.option("--tags", default="", help="Comma-separated tags")
@click.option("--persist", is_flag=True, help="Flag for auto-flush to T2 on SessionEnd")
@click.option("--project", "-p", default="", help="Explicit T2 destination project")
@click.option("--title", "-t", default="", help="Explicit T2 destination title")
def put_cmd(content: str, tags: str, persist: bool, project: str, title: str) -> None:
    """Store content in T1 session scratch.

    Use '-' as CONTENT to read from stdin.
    """
    if content == "-":
        content = sys.stdin.read()
    t1 = _t1()
    doc_id = t1.put(content=content, tags=tags, persist=persist,
                    flush_project=project, flush_title=title)
    click.echo(f"Stored: {doc_id}")


def _resolve_entry_id(t1: Any, entry_id: str) -> str:
    """Resolve an exact UUID or unique prefix (as printed by ``scratch list``)
    to a full entry id. Raises ClickException on miss / ambiguity."""
    entries = t1.list_entries()
    matches = [e["id"] for e in entries if e["id"].startswith(entry_id)]
    exact = [m for m in matches if m == entry_id]
    if exact:
        return exact[0]
    if not matches:
        raise click.ClickException(f"scratch entry {entry_id!r} not found — use: nx scratch list")
    if len(matches) > 1:
        raise click.ClickException(
            f"ambiguous ID prefix {entry_id!r} — {len(matches)} entries match; be more specific"
        )
    return matches[0]


@scratch.command("get")
@click.argument("entry_id", metavar="ID")
def get_cmd(entry_id: str) -> None:
    """Retrieve a scratch entry by ID prefix (as shown by 'nx scratch list')."""
    t1 = _t1()
    full_id = _resolve_entry_id(t1, entry_id)
    result = t1.get(full_id)
    if result is None:
        raise click.ClickException(f"scratch entry {entry_id!r} not found — use: nx scratch list")
    click.echo(result["content"])


@scratch.command("search")
@click.argument("query")
@click.option("--n", default=10, show_default=True, help="Max results")
def search_cmd(query: str, n: int) -> None:
    """Semantic search over T1 scratch entries."""
    results = _t1().search(query, n_results=n)
    if not results:
        click.echo("No results.")
        return
    for r in results:
        click.echo(f"[{r['id'][:8]}] {r['tags'] or '-'}  dist={r['distance']:.4f}")
        preview = r["content"][:200].replace("\n", " ")
        click.echo(f"  {preview}")


@scratch.command("list")
def list_cmd() -> None:
    """List all T1 scratch entries for the current session."""
    entries = _t1().list_entries()
    if not entries:
        click.echo("No scratch entries.")
        return
    for e in entries:
        click.echo(f"[{e['id'][:8]}] {e['tags'] or '-'}  flagged={e['flagged']}")
        click.echo(f"  {e['content'][:120].replace(chr(10), ' ')}")


@scratch.command("flag")
@click.argument("entry_id", metavar="ID")
@click.option("--project", "-p", default="", help="Explicit T2 destination project")
@click.option("--title", "-t", default="", help="Explicit T2 destination title")
def flag_cmd(entry_id: str, project: str, title: str) -> None:
    """Mark a scratch entry for SessionEnd flush to T2."""
    t1 = _t1()
    try:
        t1.flag(entry_id, project=project, title=title)
    except KeyError as exc:
        raise click.ClickException(str(exc)) from exc
    click.echo(f"Flagged: {entry_id}")


@scratch.command("unflag")
@click.argument("entry_id", metavar="ID")
def unflag_cmd(entry_id: str) -> None:
    """Remove the SessionEnd flush marking from a scratch entry."""
    t1 = _t1()
    try:
        t1.unflag(entry_id)
    except KeyError as exc:
        raise click.ClickException(str(exc)) from exc
    click.echo(f"Unflagged: {entry_id}")


@scratch.command("promote")
@click.argument("entry_id", metavar="ID")
@click.option("--project", "-p", required=True, help="Target T2 project")
@click.option("--title", "-t", required=True, help="Target T2 title")
def promote_cmd(entry_id: str, project: str, title: str) -> None:
    """Copy a scratch entry to T2 immediately."""
    t1 = _t1()
    with T2Database(_default_db_path()) as t2:
        try:
            report = t1.promote(entry_id, project=project, title=title, t2=t2)
        except KeyError as exc:
            raise click.ClickException(str(exc)) from exc
    click.echo(f"Promoted {entry_id} -> {project}/{title} (action={report.action})")



@scratch.command("delete")
@click.argument("entry_id", metavar="ID")
def delete_cmd(entry_id: str) -> None:
    """Delete a scratch entry by ID prefix (as shown by 'nx scratch list')."""
    t1 = _t1()
    entries = t1.list_entries()
    matches = [e["id"] for e in entries if e["id"].startswith(entry_id)]
    # Exact match takes priority over prefix matches
    exact = [m for m in matches if m == entry_id]
    if exact:
        matches = exact
    if not matches:
        raise click.ClickException(f"scratch entry {entry_id!r} not found")
    if len(matches) > 1:
        raise click.ClickException(
            f"ambiguous ID prefix {entry_id!r} — {len(matches)} entries match; be more specific"
        )
    if not t1.delete(matches[0]):
        raise click.ClickException(f"scratch entry {entry_id!r} not found")
    click.echo(f"Deleted: {entry_id}")

@scratch.command("clear")
def clear_cmd() -> None:
    """Remove all T1 scratch entries for the current session."""
    count = _t1().clear()
    click.echo(f"Cleared {count} {'entry' if count == 1 else 'entries'}.")
