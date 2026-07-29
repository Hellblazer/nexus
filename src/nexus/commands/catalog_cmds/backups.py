# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""Backup-snapshot commands for the ``nx catalog`` group (nexus-whh61.4).

Carved verbatim out of ``commands.catalog``: ``list-backups`` /
``vacuum-backups`` — the lifecycle verbs over the JSONL snapshots that
destructive catalog verbs write before deleting (RDR-106). Behaviour-
preserving; ``register`` attaches both to the shared ``catalog`` group so
``nx catalog list-backups`` (etc.) resolve exactly as before.

``_get_catalog`` is reached through the ``nexus.commands.catalog`` module
object inside each body — keeping this module's imports acyclic and preserving
the ``patch("nexus.commands.catalog._get_catalog", …)`` test seam.

``undelete`` was REMOVED in nexus-i711w Stage 2 sub-stage C-store. Restoring a
snapshot re-emitted events through the local rich Catalog's low-level event log
— deep maintenance with no service-mode expression, so it already refused
there. Hal ruled it unsupported rather than reimplemented (2026-07-29). The
snapshots themselves are unaffected: they are still WRITTEN before every
destructive verb, and ``list-backups`` / ``vacuum-backups`` still manage them.
What is gone is the in-product restore path, not the backup.
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

    Default retention is 30 days. Removed files are gone for good — the
    snapshot is the only copy of the rows a destructive verb deleted.
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
    group.add_command(vacuum_backups_cmd)
