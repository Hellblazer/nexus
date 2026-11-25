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

#: One metadata-route page (the route's maximum, ``DOC_PAGE_MAX``).
_GC_PAGE = 1000


def _read_orphans(cat) -> list:
    """The documents whose ``miss_count`` the engine says is 2 or more, read through ``GET /docs/by-meta``
    (RDR-241 Phase 2, nexus-uwggq.2.15): pages of 1000 by offset until the offset reaches the route's exact count.

    The first occurrence of a tumbler that repeats across pages is kept. A route that is not answered, or a page
    that comes back empty before the count is reached, ends the command with an error: the delete set is never built
    from a partial list, and the catalog is never walked in its place."""
    from nexus.catalog.engine_reads.route_unavailable import EngineRouteUnavailable  # noqa: PLC0415 — deferred to avoid CLI startup cost

    rows: dict[str, object] = {}
    offset = 0
    while True:
        try:
            page, count = cat.documents_by_meta(meta_min=[("miss_count", 2)], limit=_GC_PAGE, offset=offset)
        except EngineRouteUnavailable as exc:
            raise click.ClickException(str(exc)) from exc
        if not page:
            if offset >= count:
                break
            raise click.ClickException(
                f"the engine's metadata route answered no documents at offset {offset} of {count}; "
                "nothing was changed. Run the command again."
            )
        for e in page:
            rows.setdefault(str(e.tumbler), e)
        offset += len(page)
        if offset >= count:
            break
    return list(rows.values())


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

    4.29.1 inverted the default from "delete unless --dry-run"
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

    from nexus.catalog.meta_filter import meta_at_least  # noqa: PLC0415 — deferred to avoid CLI startup cost
    from nexus.sensitive_output import CliDocumentLines, entries_flagged  # noqa: PLC0415 — deferred to avoid CLI startup cost

    # nexus-uwggq.2.2: one integer-bound rule; a miss_count that is not an integer is skipped, not raised.
    orphan_entries = [e for e in _read_orphans(cat) if meta_at_least(e.meta, "miss_count", 2)]
    orphans: list[tuple[str, str, str]] = [
        (str(e.tumbler), e.title or "", e.file_path or "") for e in orphan_entries
    ]

    if not orphans:
        click.echo("No orphan entries found.")
        return

    click.echo(
        f"Found {len(orphans)} orphan "
        f"{'entry' if len(orphans) == 1 else 'entries'} (miss_count >= 2):"
    )
    # nexus-rfvyy.9 item 2: a flagged document's title and path are content (the stub under Claude Code).
    flagged = entries_flagged(orphan_entries[:20], cat=cat)
    lines = CliDocumentLines()
    for tumbler_str, title, file_path in orphans[:20]:
        loc = f" ({file_path})" if file_path else ""
        click.echo(f"  {lines.label(tumbler_str, tumbler_str in flagged, f'{tumbler_str}: {title}{loc}')}")
    if len(orphans) > 20:
        click.echo(f"  ... ({len(orphans) - 20} more)")
    for line in lines.notes():
        click.echo(line)

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
