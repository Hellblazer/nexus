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

The recovery HORIZON is exactly ``nx catalog purge-trash``'s grace window —
FOR ``nx catalog delete`` / ``nx catalog purge-trash`` ONLY (review round 2,
T2 [24834]): a document tombstoned by ``nx catalog delete`` keeps its
catalog row, manifest, and T3 chunks together until ``--older-than-days``
passes (catalog-026), so restore works for the whole window; past it,
``purge-trash`` has physically reclaimed the row and restore returns
nothing — recovery at that point means re-indexing the source, never a
hand-written SQL ``UPDATE`` against ``catalog_documents.deleted_at`` (there
is no supported direct-SQL recovery path and none should be improvised).

TWO OTHER PATHS REACH THE SAME CONTENT OUTSIDE THAT WINDOW, and this verb's
restore does NOT undo their chunk/row loss:

* the MCP ``store_delete`` tool — tombstones the catalog row and
  hard-deletes the T3 chunk in the SAME call; there is no window at all.
* ``nx collection delete`` / ``nx collection prune``
  (``purge_collection_cascade``) — irreversible, never tombstones.

``nx t3 gc``'s orphan sweep is NO LONGER on this list (Sam's second
2026-09-07 ruling on nexus-dkymw): its alive-set (``chashesForCollection``)
now PROTECTS a tombstoned-but-not-yet-purged document's chashes, superseding
nexus-mqd6t's original immediate-exclusion filter for that one read, so its
clock (chunk ``indexed_at`` vs ``--orphan-window``, still independent of
``purge-trash``'s own window) can no longer reap a just-tombstoned
document's chunks inside the grace window above.

After either of the two remaining paths, recovery is a RE-INDEX, not ``nx
catalog restore``. ``restore`` reports success on the CATALOG ROW regardless
of whether the T3 chunks are still there (it never checks) — after
restoring a document that may have gone through one of these two paths,
confirm with ``nx catalog show``'s chunk count before trusting the content
is actually back.
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


def _resolve_restore_target(cat, value: str) -> tuple[str | None, str | None, bool]:
    """Resolve TUMBLER_OR_TITLE for ``restore``, considering tombstoned rows.

    Returns ``(tumbler, error, known_live_no_trash_match)``. The third field
    is ``True`` only when *value* named a title, the live resolver matched
    it, and nothing in the trash shares that title — the caller uses it to
    print a more specific "already live" message than the generic
    unknown/live/purged catch-all.

    A bare tumbler string is passed straight through unconditionally —
    ``restore_document`` itself validates it against the tombstoned
    population, and a tumbler input carries no title-collision question.

    A title is resolved against BOTH the live catalog and the trash listing
    (review round 2, T2 [24833]) — trying the live resolver alone would
    silently resolve a title shared by a live document AND a tombstoned one
    to the LIVE doc, reporting "already live" with no mention that a
    tombstoned copy under the same title exists and IS restorable. Live
    reads exclude tombstoned rows by design (nexus-mqd6t's
    ``liveParentDoc`` filter), so the two searches see disjoint populations
    and must both run before a title can be called unambiguous.
    """
    from nexus.catalog import resolve_tumbler  # noqa: PLC0415 — command-local import
    from nexus.catalog.tumbler import Tumbler  # noqa: PLC0415 — command-local import

    try:
        Tumbler.parse(value)
        return value, None, False
    except ValueError:
        pass

    live_tumbler, live_err = resolve_tumbler(cat, value)

    try:
        trash = cat.list_trash(limit=300)
    except httpx.HTTPStatusError as exc:
        if exc.response.status_code == 404:
            _raise_engine_floor("trash")
        raise
    trash_matches = [d for d in trash if d.get("title") == value]

    if live_tumbler is not None and trash_matches:
        tumblers = ", ".join(str(d.get("tumbler", "")) for d in trash_matches)
        return (
            None,
            f"{value!r} matches both a LIVE document ({live_tumbler}) and "
            f"{len(trash_matches)} tombstoned document(s) ({tumblers}) — "
            "ambiguous. Pass the tumbler of the one you mean (see 'nx "
            "catalog trash').",
            False,
        )

    if live_tumbler is not None:
        # True "already live" case: nothing tombstoned shares this title.
        return str(live_tumbler), None, True

    if trash_matches:
        if len(trash_matches) == 1:
            return trash_matches[0]["tumbler"], None, False
        return (
            None,
            f"Ambiguous: {len(trash_matches)} tombstoned documents match "
            f"{value!r} — use the tumbler instead (see 'nx catalog trash').",
            False,
        )

    return None, live_err or f"Not found (live or tombstoned): {value!r}", False


@click.command("trash")
@click.option(
    "--limit", "-n", type=int, default=200,
    help="Page size (default 200).",
)
def trash_cmd(limit: int) -> None:
    """List this tenant's tombstoned (soft-deleted) documents.

    Newest-tombstoned first: tumbler, title, and when it was deleted.
    Read-only — the counterpart to 'nx catalog restore'. A row listed here
    is restorable via 'nx catalog restore <tumbler>' until 'nx catalog
    purge-trash' physically reclaims it (see that command's grace window) --
    for tombstones made by 'nx catalog delete', and now also 'nx t3 gc'
    (its alive-set protects a tombstoned-but-not-yet-purged document's
    chunks, nexus-dkymw). The MCP store_delete tool and 'nx collection
    delete'/'prune' can still each remove a document's chunks (or the whole
    document) outside that window; 'restore' cannot undo those. See this
    module's own docstring for the full carve-out.
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
    live resolver alone cannot see them; a title matching BOTH a live
    document and a tombstoned one is refused as ambiguous rather than
    silently restoring (or no-op'ing on) the wrong one. Restoring an
    already-live, unknown, or genuinely-purged document is reported as a
    no-op, never a silent success.

    A restored ROW is not a guarantee the CONTENT is back: this reports
    success once 'deleted_at' clears, without checking whether the T3
    chunks are still there. The MCP store_delete tool and 'nx collection
    delete'/'prune' can each remove chunks (or the whole collection)
    independently of 'nx catalog delete's tombstone window ('nx t3 gc' no
    longer can — nexus-dkymw's alive-set fix) -- after restoring, confirm
    with 'nx catalog show's chunk count. See this module's own docstring for
    the full carve-out.
    """
    from nexus.commands import catalog as _cat_cmd  # noqa: PLC0415 — module-routed helper access keeps import acyclic + monkeypatch-visible

    cat = _cat_cmd._get_catalog()
    tumbler, err, known_live_no_trash_match = _resolve_restore_target(cat, tumbler_or_title)
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
    elif known_live_no_trash_match:
        click.echo(
            f"Not restored: {tumbler} is already live. No tombstoned "
            "document matches this title in the trash (see 'nx catalog "
            "trash')."
        )
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
