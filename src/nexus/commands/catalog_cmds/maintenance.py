# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""Catalog hygiene / sweep commands for the ``nx catalog`` group (nexus-whh61.4).

Carved verbatim out of ``commands.catalog``: ``gc`` (delete orphan catalog
entries with miss_count >= 2) and ``chash-reconcile`` (sweep stale chash_index
rows pointing at deleted T3 collections). Behaviour-preserving; ``register``
attaches both to the shared ``catalog`` group so ``nx catalog gc`` /
``chash-reconcile`` resolve exactly as before.

``gc`` reaches ``_get_catalog`` / ``_get_catalog_writer`` through the
``nexus.commands.catalog`` module object inside its body — keeping this
module's imports acyclic and preserving the
``patch("nexus.commands.catalog._get_catalog", …)`` test seam.
``chash-reconcile`` uses no shared helper (it opens the T2 db directly).

Scope note: only the two STANDALONE hygiene verbs live here. The other two
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

    # Backup before delete (RDR-106 Option A).
    from nexus.catalog.catalog_backup import snapshot_documents  # noqa: PLC0415 — deferred import; rare/branch-local path or circular-dep / startup-cost avoidance
    backup_path = snapshot_documents(
        cat,
        [t for t, _, _ in orphans],
        verb="gc",
        reason="miss_count >= 2",
    )
    if backup_path:
        click.echo(
            f"\nBackup snapshot written: {backup_path}"
            f"\n  Restore with: nx catalog undelete {backup_path.name}"
        )

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


@click.command("chash-reconcile")
@click.option(
    "--apply",
    is_flag=True,
    default=False,
    help="Actually delete ghost rows. Without this flag the command "
    "is a dry-run report only.",
)
def chash_reconcile_cmd(apply: bool) -> None:
    """Sweep stale ``chash_index`` rows pointing at deleted T3 collections.

    \b
    SCOPE (RDR-187 / nexus-piwya.4): this verb operates on the LOCAL
    SQLite ``chash_index`` ONLY — it opens the T2 db file directly, so
    it is meaningful solely on pre-migration installs whose SQLite
    router still routes (frozen migration source, RDR-158). On a
    migrated (service-mode) install there is no local router to sweep:
    the verb exits at the ``No T2 db`` guard, and the PG-side router is
    retired outright by RDR-187 (its ghosts die with the table at the
    DROP) — server-side, "which collections hold chunks" is answered
    from the chunks tables themselves, which cannot go stale.

    \b
    The SQLite ``chash_index`` is the routing table that resolves
    ``chash:<hex>`` link spans to the (collection, chunk) they live
    in. Rows accumulate over time: when a collection is deleted from
    T3 (``nx collection delete`` or operator-driven cleanup), the
    chash_index rows for that collection are NOT cascaded, so they
    remain as ghosts pointing at a non-existent collection. (The
    per-access delete_stale self-heal that complemented this sweep was
    retired by RDR-187; on the SQLite path this verb is now the only
    reconciliation.)

    \b
    Default is dry-run: reports per-collection ghost counts without
    writing. Pass ``--apply`` to actually delete.

    \b
    Examples:
      nx catalog chash-reconcile         # dry-run report
      nx catalog chash-reconcile --apply # actually delete

    \b
    Filed under nexus-w9vq (RDR-108 Phase 5 follow-up).
    """
    from nexus.commands._helpers import default_db_path  # noqa: PLC0415 — deferred import; rare/branch-local path or circular-dep / startup-cost avoidance
    from nexus.db import make_t3  # noqa: PLC0415 — deferred import; rare/branch-local path or circular-dep / startup-cost avoidance
    from nexus.db.t2.chash_index import ChashIndex  # noqa: PLC0415 — deferred import; rare/branch-local path or circular-dep / startup-cost avoidance

    db_path = default_db_path()
    if not db_path.exists():
        click.echo(
            f"No T2 db at {db_path}. Nothing to reconcile.",
            err=True,
        )
        raise SystemExit(1)

    try:
        t3 = make_t3()
        # nexus-l1yt: chromadb's list_collections shape varies by
        # backend version (Collection objects vs string names).
        # Every other call site in nexus uses the same defensive
        # ``isinstance(c, str)`` guard; without it this verb crashes
        # with AttributeError on the string-returning versions.
        live_collections = {
            (c if isinstance(c, str) else c.name)
            for c in t3._client.list_collections()
        }
    except Exception as exc:  # noqa: BLE001 — re-raises after cleanup/translation
        click.echo(f"Failed to list T3 collections: {exc}", err=True)
        raise SystemExit(1)

    idx = ChashIndex(db_path)
    try:
        indexed_collections = idx.distinct_collections()
        ghost_collections = sorted(indexed_collections - live_collections)
        live_in_index = indexed_collections & live_collections
        unindexed_in_t3 = sorted(live_collections - indexed_collections)

        if not ghost_collections:
            click.echo(
                f"chash_index: {len(indexed_collections)} distinct "
                f"collection(s); 0 ghost(s). Nothing to reconcile."
            )
            if unindexed_in_t3:
                click.echo(
                    f"  Note: {len(unindexed_in_t3)} T3 collection(s) "
                    f"have no chash_index rows (likely empty or never "
                    f"backfilled)."
                )
            return

        # Per-collection ghost row counts (read-only).
        ghost_counts: list[tuple[str, int]] = []
        for coll_name in ghost_collections:
            n = idx.count_for_collection(coll_name)
            ghost_counts.append((coll_name, n))
        total_ghost_rows = sum(n for _, n in ghost_counts)

        verb = "would delete" if not apply else "deleted"
        click.echo(
            f"chash_index: {len(indexed_collections)} distinct "
            f"collection(s); {len(ghost_collections)} ghost(s) "
            f"({total_ghost_rows} row(s) total)"
        )
        click.echo(f"  live (in both T3 and index): {len(live_in_index)}")
        if unindexed_in_t3:
            click.echo(
                f"  unindexed (in T3 but not index): {len(unindexed_in_t3)}"
            )

        # Per-ghost-collection breakdown (capped at 20 to keep output sane).
        for coll_name, n in ghost_counts[:20]:
            click.echo(f"  {verb} {n:>6} row(s) from ghost: {coll_name}")
        if len(ghost_counts) > 20:
            click.echo(f"  ... and {len(ghost_counts) - 20} more ghost collection(s)")

        if apply:
            actually_deleted = 0
            for coll_name in ghost_collections:
                actually_deleted += idx.delete_collection(coll_name)
            click.echo(
                f"\nSummary: deleted {actually_deleted} row(s) across "
                f"{len(ghost_collections)} ghost collection(s)."
            )
        else:
            click.echo(
                f"\nSummary: would delete {total_ghost_rows} row(s) "
                f"across {len(ghost_collections)} ghost collection(s). "
                f"Re-run with --apply to actually delete."
            )
    finally:
        idx.close()


def register(group: click.Group) -> None:
    """Attach the hygiene/sweep commands to the shared ``catalog`` group."""
    group.add_command(gc_cmd)
    group.add_command(chash_reconcile_cmd)
