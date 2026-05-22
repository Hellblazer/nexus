# SPDX-License-Identifier: AGPL-3.0-or-later
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import click

from nexus.commands._helpers import default_db_path as _default_db_path
from nexus.commands._helpers import t2_handle
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

    RDR-120 P6 follow-up (nexus-w6txl): routes through the T2 daemon
    so host CLI + Cowork-bridged MCP + dev-container CLI all share
    the same arbitrated state. Requires the T2 daemon running; start
    it with ``nx daemon t2 start``.
    """
    if content == "-":
        content = sys.stdin.read()
    try:
        ttl_days = parse_ttl(ttl)
    except ValueError as exc:
        raise click.ClickException(str(exc)) from exc
    with t2_handle() as db:
        row_id = db.memory.put(
            project=project, title=title, content=content, tags=tags, ttl=ttl_days,
        )
    click.echo(f"Stored: {project}/{title} (id={row_id})")


@memory.command("get")
@click.argument("entry_id", metavar="ID", required=False, type=int)
@click.option("--project", "-p", default=None, help="Project namespace")
@click.option("--title", "-t", default=None, help="Entry title (exact match, or unique prefix)")
def get_cmd(entry_id: int | None, project: str | None, title: str | None) -> None:
    """Retrieve a memory entry by ID or by --project + --title.

    Title resolution is exact-then-prefix (nexus-e59o): if no entry matches
    --title exactly, a unique prefix match is returned. Ambiguous prefixes
    list the candidates and fail rather than picking silently.
    """
    if entry_id is None and not (project and title):
        raise click.UsageError("provide an ID or --project and --title")
    with t2_handle() as db:
        if entry_id is not None:
            result = db.memory.get(id=entry_id)
            if result is None:
                raise click.ClickException(
                    f"entry not found — id={entry_id}. "
                    "Use: nx memory list to see available entries",
                )
            click.echo(result["content"])
            return
        resolved, candidates = db.memory.resolve_title(project=project, title=title)
    if resolved is not None:
        click.echo(resolved["content"])
        return
    if candidates:
        lines = [
            f"Ambiguous title prefix — {len(candidates)} entries match "
            f"{title!r} in project {project!r}:",
        ]
        for c in candidates:
            lines.append(f"  [{c['id']}] {c['title']}")
        lines.append("Re-run with the full title or pass --id.")
        raise click.ClickException("\n".join(lines))
    raise click.ClickException(
        "entry not found — use: nx memory list to see available entries",
    )


@memory.command("search")
@click.argument("query")
@click.option("--project", "-p", default=None, help="Scope search to a project")
def search_cmd(query: str, project: str | None) -> None:
    """FTS5 keyword search across T2 memory entries."""
    with t2_handle() as db:
        results = db.memory.search(query=query, project=project)
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
    with t2_handle() as db:
        entries = db.memory.list_entries(project=project, agent=agent)
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

    with t2_handle() as db:
        if all_entries:
            entries = db.memory.list_entries(project=project)
            count = len(entries)
            if count == 0:
                raise click.ClickException(f"No entries found in project {project!r}")
            if not yes:
                n = "entry" if count == 1 else "entries"
                click.echo(f"Found {count} {n} in {project!r}.")
                click.confirm(f"Delete {count} {n} from {project!r}?", abort=True)
            for e in entries:
                _delete_with_taxonomy_cascade(
                    db, project=project, title=e["title"],
                )
            click.echo(f"Deleted {count} {'entry' if count == 1 else 'entries'} from {project!r}.")
        else:
            entry = (
                db.memory.get(id=entry_id)
                if entry_id is not None
                else db.memory.get(project=project, title=title)
            )
            if entry is None:
                raise click.ClickException("entry not found — use: nx memory list to see available entries")
            if not yes:
                preview = entry["content"][:120].replace("\n", " ")
                click.echo(f"{entry['project']}/{entry['title']}")
                click.echo(f"  {preview}")
                click.confirm("Delete?", abort=True)
            _delete_with_taxonomy_cascade(
                db,
                project=entry["project"],
                title=entry["title"],
                id=entry_id,
            )
            click.echo(f"Deleted: {entry['project']}/{entry['title']}")


def _delete_with_taxonomy_cascade(
    db,
    *,
    project: str | None = None,
    title: str | None = None,
    id: int | None = None,
) -> bool:
    """Memory + taxonomy cascade — replaces the facade-side cascade.

    The pre-RDR-120 ``T2Database.delete`` ran (1) ``memory.delete`` and
    (2) ``taxonomy.purge_assignments_for_doc`` in sequence. T2Client's
    ``database`` proxy doesn't expose the facade method, so the CLI
    drives the cascade itself with two store-level RPC calls. In direct
    mode (tests injecting a T2Database via the handle), the same two
    calls hit the in-process facade and produce identical state.
    """
    deleted = db.memory.delete(project=project, title=title, id=id)
    if deleted and project and title:
        db.taxonomy.purge_assignments_for_doc(project=project, title=title)
    return deleted


@memory.command("expire")
def expire_cmd() -> None:
    """Remove TTL-expired memory entries.

    Cross-domain operation: also purges the relevance_log table
    older than 90 days. RDR-120 P6 follow-up (nexus-w6txl): the
    cascade runs client-side via two store-level calls when the CLI
    is talking to the daemon; in direct mode (tests) the same calls
    land on the in-process facade.
    """
    with t2_handle() as db:
        count = db.memory.expire()
        try:
            db.telemetry.expire_relevance_log(days=90)
        except Exception:  # noqa: BLE001
            # The relevance_log purge is best-effort; the facade
            # logged a structured ``relevance_log_error`` event for
            # this. Without the daemon-side facade we lose that
            # signal, but the memory-side expiry still landed.
            pass
    click.echo(f"Expired {count} {'entry' if count == 1 else 'entries'}.")


@memory.command("promote")
@click.argument("entry_id", metavar="ID", type=int)
@click.option("--collection", required=True, help="T3 collection name (e.g. 'knowledge' or 'knowledge__myproject')")
@click.option("--tags", default="", help="Comma-separated tags (overrides T2 tags when provided)")
@click.option("--remove", is_flag=True, default=False, help="Delete the entry from T2 after promoting.")
def promote_cmd(entry_id: int, collection: str, tags: str, remove: bool) -> None:
    """Promote a T2 memory entry to T3 ChromaDB permanent storage."""
    from nexus.corpus import t3_collection_name

    with t2_handle() as db:
        entry = db.memory.get(id=entry_id)
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

        # nexus-hmxi: probe T3 so promote targets land in the same
        # collection that ``nx store list`` / ``nx search`` resolve to.
        # Resolution runs AFTER the credential-missing fail-fast so
        # operators with incomplete config see the actionable message
        # instead of a generic T3 connection error.
        t3_for_probe = make_t3()
        try:
            collection = t3_collection_name(collection, t3=t3_for_probe)
        finally:
            close = getattr(t3_for_probe, "close", None)
            if callable(close):
                try:
                    close()
                except Exception:
                    pass

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

        # nexus-8g79.1: pre-register the catalog entry so the T3 chunk
        # carries the resulting tumbler as ``doc_id`` at write-time and
        # the manifest hook in HookRegistry.fire_store_chains populates
        # document_chunks + documents.chunk_count for this promotion.
        # Without this, the promoted entry lands in T3 with no catalog
        # identity — same regression class as nexus-zq79 / nexus-lf8f.
        import hashlib
        from nexus.catalog.store_hook import catalog_store_hook
        chunk_chroma_id = hashlib.sha256(entry["content"].encode()).hexdigest()[:32]
        catalog_doc_id = catalog_store_hook(
            title=entry["title"],
            doc_id=chunk_chroma_id,
            collection_name=collection,
        )

        with make_t3() as t3:
            doc_id = t3.put(
                collection=collection,
                content=entry["content"],
                title=entry["title"],
                tags=merged_tags,
                ttl_days=ttl_days,
                expires_at=expires_at,
                catalog_doc_id=catalog_doc_id,
            )

        # nexus-9099: fire post-store chains so the promoted T3 row
        # reaches chash_index / taxonomy / aspect queue. RDR-095
        # symmetric-fire; this path was missed by the original commit.
        # nexus-8g79.1: thread catalog_doc_id through so the manifest
        # hook can populate document_chunks + chunk_count.
        from nexus.hook_registry import HookRegistry, install_default_hooks
        hooks = HookRegistry()
        install_default_hooks(hooks)
        hooks.fire_store_chains(
            [doc_id], collection, [entry["content"]],
            catalog_doc_id=catalog_doc_id,
        )

        if remove:
            _delete_with_taxonomy_cascade(
                db, project=entry["project"], title=entry["title"],
            )
            click.echo(
                f"Promoted and removed: {entry['project']}/{entry['title']} -> {collection} (id={doc_id})"
            )
        else:
            click.echo(
                f"Promoted: {entry['project']}/{entry['title']} -> {collection} (id={doc_id})"
            )
