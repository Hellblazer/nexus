# SPDX-License-Identifier: AGPL-3.0-or-later
import sys
from pathlib import Path

import click

from nexus.commands._helpers import default_db_path as _default_db_path
from nexus.db.t1 import T1Database
from nexus.db.t2 import T2Database
from nexus.session import read_session_id, write_session_file, generate_session_id


def _t1() -> T1Database:
    session_id = read_session_id()
    if session_id is None:
        session_id = generate_session_id()
        write_session_file(session_id)
    return T1Database(session_id=session_id)


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


@scratch.command("get")
@click.argument("entry_id", metavar="ID")
def get_cmd(entry_id: str) -> None:
    """Retrieve a scratch entry by ID."""
    result = _t1().get(entry_id)
    if result is None:
        raise click.ClickException("Not found.")
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
            t1.promote(entry_id, project=project, title=title, t2=t2)
        except KeyError as exc:
            raise click.ClickException(str(exc)) from exc
    click.echo(f"Promoted {entry_id} -> {project}/{title}")


@scratch.command("clear")
def clear_cmd() -> None:
    """Remove all T1 scratch entries for the current session."""
    count = _t1().clear()
    click.echo(f"Cleared {count} {'entry' if count == 1 else 'entries'}.")
