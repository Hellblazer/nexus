# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""``nx catalog trash`` / ``nx catalog restore`` (nexus-dkymw).

Sam's 2026-09-07 ruling on nexus-dkymw (the RDR-106 Option A regression):
tombstones ARE the recovery story — do not resurrect the backup-before-
delete + undelete + list-backups + vacuum-backups machinery that shipped in
4.29.1 and was retired with the SQLite catalog (RDR-158 P4). ``nx catalog
delete`` already soft-tombstones every document (``deleted_at`` stamped,
nothing cascaded); these two verbs are the operator door in and out of that
state.

Closes nexus-xavu7: three sites (``collection_rename.py``,
``catalog_cmds/collections.py``'s ``rename-collection``, ``nx doctor
--collections-drift``) told an operator to "restore the trashed
document(s)" with zero CLI/MCP/REST surface to do it. ``nexus.document_restore``
(``catalog-003-soft-delete.xml``, RDR-156 P1.2) had existed engine-side with
no caller anywhere in the stack since that changeset — ``HttpCatalogClient
.restore_document`` / ``.list_trash`` (and the engine routes ``POST
/v1/catalog/restore`` / ``GET /v1/catalog/trash``, nexus-dkymw) are that
caller, mirroring how nexus-3ck2g gave ``nexus.purge_trash`` its first
caller the same way.

The recovery HORIZON is exactly ``nx catalog purge-trash``'s grace window,
nothing more: a tombstoned document's catalog row, manifest, and T3 chunks
stay together until ``--older-than-days`` passes (catalog-026), so restore
works for the whole window. Past it, ``purge-trash`` has physically
reclaimed the row and restore returns nothing — recovery at that point
means re-indexing the source, never a hand-written SQL ``UPDATE`` against
``catalog_documents.deleted_at``. There is no supported direct-SQL recovery
path and none should be improvised.
"""
from __future__ import annotations

import click
import httpx


def _raise_engine_floor(route: str) -> None:
    raise click.ClickException(
        f"nx catalog {route}: this engine does not yet expose the "
        f"nexus-dkymw {route} route (POST /v1/catalog/restore, GET "
        "/v1/catalog/trash). Upgrade the deployed engine-service, then "
        "re-run."
    )


def _resolve_restore_target(cat, value: str) -> tuple[str | None, str | None]:
    """Resolve TUMBLER_OR_TITLE for ``restore``, considering tombstoned rows.

    Tries the live resolver first (``nexus.catalog.resolve_tumbler``) — it
    handles the bare-tumbler-string case directly and the common case where
    the title is unambiguous among LIVE documents. Live reads exclude
    tombstoned rows by design (nexus-mqd6t's ``liveParentDoc`` filter), so a
    title that resolves to nothing there might still name a TOMBSTONED
    document — exactly the case this verb exists for — so a second pass
    over ``nx catalog trash``'s listing follows before giving up.
    """
    from nexus.catalog import resolve_tumbler  # noqa: PLC0415 — command-local import
    from nexus.catalog.tumbler import Tumbler  # noqa: PLC0415 — command-local import

    t, err = resolve_tumbler(cat, value)
    if t is not None:
        return str(t), None

    # A bare tumbler string that the live resolver could not CONFIRM (because
    # reads hide tombstoned rows) is still passed straight through —
    # restore_document itself validates it against the tombstoned population.
    try:
        Tumbler.parse(value)
        return value, None
    except ValueError:
        pass

    try:
        trash = cat.list_trash(limit=300)
    except httpx.HTTPStatusError as exc:
        if exc.response.status_code == 404:
            _raise_engine_floor("trash")
        raise
    matches = [d for d in trash if d.get("title") == value]
    if matches:
        if len(matches) == 1:
            return matches[0]["tumbler"], None
        return (
            None,
            f"Ambiguous: {len(matches)} tombstoned documents match {value!r} "
            "— use the tumbler instead (see 'nx catalog trash').",
        )
    return None, err or f"Not found (live or tombstoned): {value!r}"


@click.command("trash")
@click.option(
    "--limit", "-n", type=int, default=200,
    help="Page size (default 200).",
)
def trash_cmd(limit: int) -> None:
    """List this tenant's tombstoned (soft-deleted) documents.

    Newest-tombstoned first: tumbler, title, and when it was deleted.
    Read-only — the counterpart to 'nx catalog restore'. Everything listed
    here is restorable via 'nx catalog restore <tumbler>' until 'nx catalog
    purge-trash' physically reclaims it (see that command's grace window).
    """
    from nexus.commands import catalog as _cat_cmd  # noqa: PLC0415 — module-routed helper access keeps import acyclic + monkeypatch-visible

    cat = _cat_cmd._get_catalog()
    try:
        docs = cat.list_trash(limit=limit)
    except httpx.HTTPStatusError as exc:
        if exc.response.status_code == 404:
            _raise_engine_floor("trash")
        raise

    if not docs:
        click.echo("Trash is empty.")
        return

    click.echo(f"{len(docs)} tombstoned document(s):")
    click.echo(f"  {'TUMBLER':<16} {'DELETED_AT':<26} TITLE")
    for d in docs:
        click.echo(
            f"  {str(d.get('tumbler', '')):<16} "
            f"{str(d.get('deleted_at') or ''):<26} "
            f"{d.get('title', '')}"
        )


@click.command("restore")
@click.argument("tumbler_or_title")
def restore_cmd(tumbler_or_title: str) -> None:
    """Undo a soft delete: clear deleted_at on one tombstoned document.

    Accepts a tumbler or a title — title resolution considers tombstoned
    rows (see 'nx catalog trash' to list what is restorable), since the
    live resolver alone cannot see them. Restoring an already-live,
    unknown, or genuinely-purged document is reported as a no-op, never a
    silent success.
    """
    from nexus.commands import catalog as _cat_cmd  # noqa: PLC0415 — module-routed helper access keeps import acyclic + monkeypatch-visible

    cat = _cat_cmd._get_catalog()
    tumbler, err = _resolve_restore_target(cat, tumbler_or_title)
    if tumbler is None:
        raise click.ClickException(err or f"Not found: {tumbler_or_title!r}")

    writer = _cat_cmd._get_catalog_writer()
    try:
        restored = writer.restore_document(tumbler)
    except httpx.HTTPStatusError as exc:
        if exc.response.status_code == 404:
            _raise_engine_floor("restore")
        raise

    if restored:
        click.echo(f"Restored: {tumbler}")
    else:
        click.echo(
            f"Not restored: {tumbler} — unknown, already live, or its "
            "tombstone was already reclaimed by 'nx catalog purge-trash'. "
            "Past that grace window recovery means re-indexing the source, "
            "not restore."
        )


def register(group: click.Group) -> None:
    """Attach the trash/restore command pair to the shared ``catalog`` group."""
    group.add_command(trash_cmd)
    group.add_command(restore_cmd)
