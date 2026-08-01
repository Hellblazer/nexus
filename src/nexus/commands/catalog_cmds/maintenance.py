# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""Catalog hygiene / sweep commands for the ``nx catalog`` group (nexus-whh61.4).

Carved verbatim out of ``commands.catalog``: ``gc`` (delete orphan catalog
entries with miss_count >= 2). ``register`` attaches it to the shared
``catalog`` group so ``nx catalog gc`` resolves exactly as before.

``gc`` reaches ``_get_catalog`` / ``_get_catalog_writer`` through the
``nexus.commands.catalog`` module object inside its body — keeping this
module's imports acyclic and preserving the
``patch("nexus.commands.catalog._get_catalog", …)`` test seam.

RETIRED (RDR-155 P4b P3 / RDR-187): ``chash-reconcile`` swept stale
``chash_index`` rows pointing at deleted T3 collections. RDR-187 DROPped
``nexus.chash_index`` — the chunks tables ARE the chash-keyed store — so the
verb reconciled nothing against nothing. It had already refused
service-backed installs outright (nexus-yh044), making it a legacy-SQLite-only
repair path for a table that no longer exists in either mode.

Scope note: only the STANDALONE hygiene verb lives here. The other two
historically-"maintenance" verbs — ``prune-stale`` and ``remediate-paths`` —
share six private path-remediation helpers (``_rdr_prefix_of``,
``_build_rdr_prefix_index``, ``_build_basename_index``,
``_entry_needs_remediation``, ``_resolve_candidate``,
``_resolve_via_devonthink``) and are carved separately into
``catalog_cmds/remediation.py`` (nexus-whh61.4), so this module stays
helper-free.
"""
from __future__ import annotations

import click

from nexus.catalog.tumbler import Tumbler


@click.command("gc")
@click.option(
    "--dry-run/--no-dry-run", default=True,
    help="Report-only (default). Use --no-dry-run to perform deletions.",
)
@click.option(
    "--confirm", is_flag=True, default=False,
    help="Required alongside --no-dry-run to actually delete catalog rows.",
)
def gc_cmd(dry_run: bool, confirm: bool) -> None:
    """Remove orphan catalog entries that have miss_count >= 2.

    \b
    Orphans are entries that were absent in two or more consecutive index runs.
    Default is read-only (--dry-run is on). To actually delete:
      nx catalog gc --no-dry-run --confirm

    \b
    Examples:
      nx catalog gc                          # report (read-only)
      nx catalog gc --no-dry-run --confirm  # actually delete

    nexus-tnz3: 4.29.1 inverted the default from "delete unless --dry-run"
    to "report unless --no-dry-run --confirm" so a forgotten flag no longer
    silently destroys orphan entries.
    """
    from nexus.commands import catalog as _cat_cmd  # noqa: PLC0415 — module-routed helper access keeps import acyclic + monkeypatch-visible

    will_delete = (not dry_run) and confirm
    if (not dry_run) and not confirm:
        click.echo(
            "--no-dry-run alone is treated as report-only. "
            "Add --confirm to actually delete catalog rows."
        )

    cat = _cat_cmd._get_catalog()
    writer = _cat_cmd._get_catalog_writer()

    # nexus-xnz0o: use all_documents() (uniform across SQLite + service mode).
    all_docs = []
    offset = 0
    while True:
        page = cat.all_documents(limit=200, offset=offset)
        if not page:
            break
        all_docs.extend(page)
        if len(page) < 200:
            break
        offset += 200

    orphans: list[tuple[str, str, str]] = []
    for entry in all_docs:
        if int(entry.meta.get("miss_count", 0)) >= 2:
            orphans.append((str(entry.tumbler), entry.title or "", entry.file_path or ""))

    if not orphans:
        click.echo("No orphan entries found.")
        return

    click.echo(
        f"Found {len(orphans)} orphan "
        f"{'entry' if len(orphans) == 1 else 'entries'} (miss_count >= 2):"
    )
    for tumbler_str, title, file_path in orphans[:20]:
        loc = f" ({file_path})" if file_path else ""
        click.echo(f"  {tumbler_str}: {title}{loc}")
    if len(orphans) > 20:
        click.echo(f"  ... ({len(orphans) - 20} more)")

    if not will_delete:
        click.echo(
            f"\n{len(orphans)} {'entry' if len(orphans) == 1 else 'entries'} "
            f"would be deleted. Run with --no-dry-run --confirm to apply."
        )
        return

    # The pre-delete local backup snapshot (RDR-106 Option A) died with the
    # local catalog (nexus-i711w): backups were local-catalog-only.

    # nexus-xedhp: batch via delete_many (service mode) instead of one
    # writer.delete_document() per entry. SQLite/daemon-mode writers don't
    # expose delete_many (capability check falls back safely, unchanged
    # behaviour there).
    _delete_many = getattr(writer, "delete_many", None)
    if callable(_delete_many):
        n_deleted = len(_delete_many(
            [Tumbler.parse(t) for t, _, _ in orphans]
        ))
    else:
        n_deleted = 0
        for tumbler_str, title, file_path in orphans:
            if writer.delete_document(Tumbler.parse(tumbler_str)):
                n_deleted += 1

    click.echo(
        f"\nDeleted {n_deleted} orphan "
        f"{'entry' if n_deleted == 1 else 'entries'}."
    )


def register(group: click.Group) -> None:
    """Attach the hygiene/sweep commands to the shared ``catalog`` group."""
    group.add_command(gc_cmd)
