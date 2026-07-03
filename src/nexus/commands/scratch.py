# SPDX-License-Identifier: AGPL-3.0-or-later
import functools
import sys
from typing import Any

import click

from nexus.db.t1 import T1ServerNotFoundError, get_t1_database


def _clean_service_errors(fn):
    """Convert CALL-time T1 service errors into clean ClickExceptions.

    ``_t1()`` already converts constructor-time failures; operation calls
    (put/search/...) can still raise ``HttpScratchStore`` RuntimeErrors —
    most importantly the 401 from the require-minted session gate
    (nexus-h8rf6 T1-401 finding). The 401 is BY DESIGN: service-backed T1
    needs a MINTED session token (a live ``session_tokens`` row), and
    re-minting ROTATES the token (``TokenStore.issueSessionToken`` is
    ``ON CONFLICT DO UPDATE``), so the bare CLI must never self-mint for a
    session an MCP server may own — the only correct CLI behavior is a
    crisp explanation of the sanctioned paths.
    """
    @functools.wraps(fn)
    def wrapper(*args, **kwargs):
        try:
            return fn(*args, **kwargs)
        except RuntimeError as exc:
            from nexus.db.http_scratch_store import SESSION_UNAUTHORIZED_MARKER  # noqa: PLC0415 — deferred import (http store must not load on every CLI start)

            msg = str(exc)
            if SESSION_UNAUTHORIZED_MARKER in msg:
                raise click.ClickException(
                    f"{msg}\n\n"
                    "Service-backed T1 scratch is session-scoped and requires a "
                    "MINTED session token (the nx-mcp server mints one at session "
                    "start and exports NX_T1_SESSION). A bare CLI cannot safely "
                    "self-mint: re-minting rotates the token and would break the "
                    "session's MCP server.\n\n"
                    "Sanctioned paths:\n"
                    "  * run inside a Claude session (inherits the minted token), or\n"
                    "  * prefix with NX_T1_ISOLATED=1 for in-process ephemeral scratch."
                ) from exc
            raise click.ClickException(msg) from exc
    return wrapper


def _t1():
    """Return the active T1 scratch store for this process.

    Routes through :func:`~nexus.db.t1.get_t1_database` so the correct
    backend is selected at runtime:

    * ``NX_STORAGE_BACKEND_T1=service`` → :class:`~nexus.db.http_scratch_store.HttpScratchStore`
      (Postgres UNLOGGED, RDR-152 bead nexus-gmiaf.13).
    * Default → :class:`~nexus.db.t1.T1Database` (ChromaDB path, unchanged).

    On ``T1ServerNotFoundError`` (Chroma path) or a service-endpoint
    ``RuntimeError`` (service path, ``NX_STORAGE_BACKEND=service`` with no
    reachable nexus-service — nexus-0l5ym), surface a clean actionable message
    via ``click.ClickException`` (exit 1, no traceback) rather than a wall of
    traceback (nexus-gff3g).
    """
    try:
        return get_t1_database()
    except T1ServerNotFoundError as exc:
        raise click.ClickException(
            f"{exc}\n\n"
            "Quick fix: prefix the command with NX_T1_ISOLATED=1 for an "
            "in-process ephemeral scratch (not shared with the MCP server), or "
            "reconnect the conexus MCP/extension so a session-id lease is "
            "published for this session."
        ) from exc
    except RuntimeError as exc:
        # nexus-0l5ym: service-mode T1 (HttpScratchStore) raises a raw
        # RuntimeError from resolve_service_config when no nexus-service
        # endpoint resolves. Convert it to the same clean guidance instead of
        # dumping a traceback on `nx scratch`.
        raise click.ClickException(
            f"{exc}\n\n"
            "Quick fix: prefix the command with NX_T1_ISOLATED=1 for an "
            "in-process ephemeral scratch, start the service with "
            "'nx daemon service start', or export NX_SERVICE_URL / "
            "NX_SERVICE_TOKEN so the service-backed T1 endpoint resolves."
        ) from exc


@click.group()
def scratch() -> None:
    """Temporary in-session scratch space (cleared when the session ends)."""


@scratch.command("put")
@click.argument("content")
@click.option("--tags", default="", help="Comma-separated tags")
@click.option("--persist", is_flag=True, help="Flag for auto-flush to T2 on SessionEnd")
@click.option("--project", "-p", default="", help="Explicit T2 destination project")
@click.option("--title", "-t", default="", help="Explicit T2 destination title")
@_clean_service_errors
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
@_clean_service_errors
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
@_clean_service_errors
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
@_clean_service_errors
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
@_clean_service_errors
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
@_clean_service_errors
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
@_clean_service_errors
def promote_cmd(entry_id: str, project: str, title: str) -> None:
    """Copy a scratch entry to T2 immediately."""
    from nexus.mcp_infra import t2_index_write  # noqa: PLC0415 — deliberate function-scoped import (defer heavy/optional dep, avoid circular import)

    t1 = _t1()
    try:
        # RDR-128 P3 (nexus-sbxbe.3): route the T2 write through the daemon.
        # T1Database.promote calls ``t2.put(...)``; T2Client's facade
        # passthrough makes that work on the routed client (memory.put RPC)
        # or the direct-fallback T2Database.
        report = t2_index_write(
            lambda t2: t1.promote(entry_id, project=project, title=title, t2=t2)
        )
    except KeyError as exc:
        raise click.ClickException(str(exc)) from exc
    click.echo(f"Promoted {entry_id} -> {project}/{title} (action={report.action})")



@scratch.command("delete")
@click.argument("entry_id", metavar="ID")
@_clean_service_errors
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
@_clean_service_errors
def clear_cmd() -> None:
    """Remove all T1 scratch entries for the current session."""
    count = _t1().clear()
    click.echo(f"Cleared {count} {'entry' if count == 1 else 'entries'}.")
