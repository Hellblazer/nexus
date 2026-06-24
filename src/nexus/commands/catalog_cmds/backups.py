# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""Backup-snapshot commands for the ``nx catalog`` group (nexus-whh61.4).

Carved verbatim out of ``commands.catalog``: ``list-backups`` / ``undelete`` /
``vacuum-backups`` — the lifecycle verbs over the JSONL snapshots that
destructive catalog verbs write before deleting (RDR-106). Behaviour-
preserving; ``register`` attaches all three to the shared ``catalog`` group so
``nx catalog list-backups`` (etc.) resolve exactly as before.

``_get_catalog`` is reached through the ``nexus.commands.catalog`` module
object inside each body — keeping this module's imports acyclic and preserving
the ``patch("nexus.commands.catalog._get_catalog", …)`` test seam. ``undelete``
opens an admin catalog directly (``make_catalog_admin``), mirroring the
original.
"""
from __future__ import annotations

from pathlib import Path

import click


@click.command("list-backups")
def list_backups_cmd() -> None:
    """List backup snapshots written by destructive catalog verbs.

    Each destructive catalog verb (``delete``, ``gc``, ``prune-stale``,
    ``link-bulk-delete``) writes a JSONL snapshot of the rows about
    to be deleted under ``$NEXUS_CONFIG_DIR/catalog/.deleted-backups/``
    BEFORE the actual delete. This verb shows what's recoverable
    without inspecting the files manually.
    """
    from nexus.catalog.catalog_backup import list_backups  # noqa: PLC0415 — deferred import; rare/branch-local path or circular-dep / startup-cost avoidance
    from nexus.commands import catalog as _cat_cmd  # noqa: PLC0415 — module-routed helper access keeps import acyclic + monkeypatch-visible
    cat = _cat_cmd._get_catalog()
    records = list_backups(cat)
    if not records:
        click.echo("No backups found.")
        return
    click.echo(f"{len(records)} backup(s) (newest first):\n")
    for rec in records:
        click.echo(
            f"  {rec.path.name}\n"
            f"    verb={rec.verb}  ts={rec.timestamp}  "
            f"rows={rec.rows_count}\n"
            f"    reason={rec.reason or '<none>'}"
        )


@click.command("undelete")
@click.argument("backup")
def undelete_cmd(backup: str) -> None:
    """Restore documents (and their links) from a backup snapshot.

    BACKUP is either a filename inside ``.deleted-backups/`` or an
    absolute path. Documents are re-emitted as DocumentRegistered
    events in events.jsonl (event-sourced; full audit trail);
    inbound and outbound links are re-emitted as LinkCreated events
    via ``link_if_absent`` (idempotent).

    Documents are restored with their ORIGINAL tumblers — the tumbler
    minting path is bypassed. Re-running this on an already-restored
    backup is a no-op (DocumentRegistered on existing tumbler is
    idempotent via INSERT OR REPLACE).
    """
    from nexus.catalog.catalog_backup import restore_documents  # noqa: PLC0415 — deferred import; rare/branch-local path or circular-dep / startup-cost avoidance
    from nexus.catalog.factory import (  # noqa: PLC0415 — deferred import; rare/branch-local path or circular-dep / startup-cost avoidance
        CatalogAdminDaemonLiveError,
        make_catalog_admin,
    )
    # Deep-maintenance: restore_documents re-emits events through the
    # catalog's low-level event log, not the 22 daemon write ops (RDR-146).
    try:
        cat = make_catalog_admin()
    except CatalogAdminDaemonLiveError as exc:
        raise click.ClickException(str(exc)) from exc
    if cat is None:
        raise click.ClickException(
            "Catalog not initialized. Run 'nx catalog setup' first."
        )
    if backup.startswith("/"):
        backup_path = Path(backup)
    else:
        backup_path = cat._dir / ".deleted-backups" / backup
    if not backup_path.exists():
        raise click.ClickException(f"Backup not found: {backup_path}")

    docs, links = restore_documents(cat, backup_path)
    click.echo(
        f"Restored {docs} document(s) and {links} link(s) "
        f"from {backup_path.name}."
    )


@click.command("vacuum-backups")
@click.option(
    "--older-than-days", default=30, show_default=True,
    help="Drop backup files older than this many days.",
)
@click.option(
    "--dry-run/--no-dry-run", default=True,
    help="Report-only (default). Use --no-dry-run to delete.",
)
def vacuum_backups_cmd(older_than_days: int, dry_run: bool) -> None:
    """Drop old backup snapshots past the retention window.

    Default retention is 30 days. Removed files are gone for good —
    after vacuum, the rows in those backups are no longer recoverable
    via ``nx catalog undelete``.
    """
    from nexus.catalog.catalog_backup import vacuum_old_backups  # noqa: PLC0415 — deferred import; rare/branch-local path or circular-dep / startup-cost avoidance
    from nexus.commands import catalog as _cat_cmd  # noqa: PLC0415 — module-routed helper access keeps import acyclic + monkeypatch-visible
    cat = _cat_cmd._get_catalog()
    removed, kept = vacuum_old_backups(
        cat, older_than_days=older_than_days, dry_run=dry_run,
    )
    if dry_run:
        click.echo(
            f"Would remove {removed} backup file(s) "
            f"(keeping {kept}). "
            f"Run with --no-dry-run to actually delete."
        )
    else:
        click.echo(
            f"Removed {removed} backup file(s); kept {kept}."
        )


def register(group: click.Group) -> None:
    """Attach the backup-snapshot commands to the shared ``catalog`` group."""
    group.add_command(list_backups_cmd)
    group.add_command(undelete_cmd)
    group.add_command(vacuum_backups_cmd)
