# SPDX-License-Identifier: AGPL-3.0-or-later
"""``nx catalog gc-audit list`` — read the destructive-T3-op audit trail.

``nexus.gc_audit`` (nexus-jqvzk) is written by the engine's background
reaps (``actor="engine"``) and, since nexus-fduai, by ``nx t3 gc`` reporting
its own client-side delete (``operation="t3_gc"``). Until this verb the only
reader was ``nx doctor``'s pass/fail non-empty check — an audit trail with
no way to look at it (substantive-critic, 2026-08-28). This is the thin
lister over :meth:`~nexus.catalog.http_catalog_client.HttpCatalogClient.
gc_audit_list`; it interprets nothing, the engine's rows are shown as sent.
"""

from __future__ import annotations

import json

import click
import httpx


def _fetch(cat, **filters) -> list[dict]:  # noqa: ANN001 — catalog reader handle
    try:
        return cat.gc_audit_list(**filters)
    except httpx.HTTPStatusError as exc:
        if exc.response is not None and exc.response.status_code == 404:
            raise click.ClickException(
                "this engine has no /gc_audit/list route (predates "
                "engine-service-v0.1.62, nexus-jqvzk) — converge it with "
                "'nx upgrade' first."
            ) from exc
        raise


@click.group("gc-audit")
def gc_audit_group() -> None:
    """Read the destructive-T3-op audit trail (nexus.gc_audit)."""


@gc_audit_group.command("list")
@click.option("--collection", default=None, help="Only rows for this collection.")
@click.option(
    "--operation", default=None,
    help="Only rows for this operation (t3_gc, purge_trash, gc_quarantine_orphans, ...).",
)
@click.option("--limit", default=50, show_default=True, type=int, help="Rows per page.")
@click.option("--offset", default=0, show_default=True, type=int, help="Rows to skip.")
@click.option("--json", "json_out", is_flag=True, default=False, help="Emit the rows as JSON.")
def gc_audit_list_cmd(
    collection: str | None, operation: str | None, limit: int, offset: int, json_out: bool,
) -> None:
    """List gc_audit rows, newest first.

    Each row is what the engine stored: id, created_at, operation, actor,
    collection, dry_run, chash_count, the (engine-capped) chashes, and the
    producer's details. Text mode shows one line per row; --json shows
    every field.
    """
    from nexus.commands import catalog as _cat_cmd  # noqa: PLC0415 — circular-dep avoidance (catalog.py registers this module)

    cat = _cat_cmd._get_catalog()
    entries = _fetch(
        cat, collection=collection, operation=operation, limit=limit, offset=offset,
    )
    if json_out:
        click.echo(json.dumps(entries, indent=2, default=str))
        return
    if not entries:
        click.echo("No gc_audit rows match.")
        return
    click.echo(f"{'id':>8}  {'created_at':<25} {'operation':<22} {'actor':<12} {'dry':<3} {'chashes':>7}  collection")
    for row in entries:
        click.echo(
            f"{row.get('id', ''):>8}  {str(row.get('created_at', ''))[:25]:<25} "
            f"{str(row.get('operation', '')):<22} {str(row.get('actor', '')):<12} "
            f"{'yes' if row.get('dry_run') else 'no':<3} {row.get('chash_count', 0):>7}  "
            f"{row.get('collection') or '-'}"
        )
    click.echo(
        f"{len(entries)} row(s) shown (offset {offset}); use --offset {offset + limit} "
        f"for the next page, --json for chashes and details."
    )


def register(group: click.Group) -> None:
    """Attach ``gc-audit`` to the shared ``catalog`` group."""
    group.add_command(gc_audit_group)
