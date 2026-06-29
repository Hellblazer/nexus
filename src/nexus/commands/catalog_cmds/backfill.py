# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""Owner-id backfill command for the ``nx catalog`` group (nexus-kgyoz).

Carved verbatim out of ``commands.catalog``: ``backfill-owner-id`` — the
one-time RDR-137 P1.5a migration that populates ``collections.owner_id`` for
legacy 2-segment collection names. Behaviour-preserving; ``register`` attaches
it to the shared ``catalog`` group so ``nx catalog backfill-owner-id`` resolves
exactly as before. ``_get_catalog`` is reached through the
``nexus.commands.catalog`` module object so the import stays acyclic and the
``patch("nexus.commands.catalog._get_catalog", …)`` test seam keeps working.
"""
from __future__ import annotations

import click


@click.command("backfill-owner-id")
@click.option(
    "--dry-run/--no-dry-run",
    default=True,
    help="Report-only (default). Use --no-dry-run to actually update. "
    "Matches the safe-default convention of the other Phase 6 verbs.",
)
@click.option(
    "--from-documents/--no-from-documents",
    default=True,
    help="Use the documents-table fallback to recover owner_id for legacy "
    "2-segment names (default). Disable to restrict the backfill to "
    "the auto-migration's conformant-name path only.",
)
def backfill_owner_id_cmd(dry_run: bool, from_documents: bool) -> None:
    """Populate ``collections.owner_id`` for empty rows (RDR-137 P1.5a).

    \b
    The CatalogStore's auto-migration handles conformant RDR-103
    four-segment names on every DB open. This verb adds the documents-
    table fallback that recovers owner_id for legacy 2-segment names
    (e.g. ``knowledge__delos``) by inferring owner from documents that
    are physically registered against the collection.

    \b
    Ambiguous rows (documents from multiple distinct owners) are
    skipped with a warning. The auto-migration is idempotent — running
    this with ``--from-documents=false`` is equivalent to opening any
    CatalogStore connection.

    \b
    Examples:
      nx catalog backfill-owner-id --dry-run
      nx catalog backfill-owner-id --no-dry-run
      nx catalog backfill-owner-id --no-dry-run --no-from-documents
    """
    from nexus.catalog.collections_owner_backfill import (  # noqa: PLC0415  — command-local import (nexus.catalog.collections_owner_backfill)
        backfill_owner_id,
    )
    from nexus.catalog.factory import _is_catalog_service_mode  # noqa: PLC0415  — command-local import (nexus.catalog.factory)
    from nexus.commands import catalog as _cat_cmd  # noqa: PLC0415 — module-routed helper access keeps import acyclic + monkeypatch-visible

    if _is_catalog_service_mode():
        raise click.ClickException(
            "nx catalog backfill-owner-id is not supported in service mode — "
            "this one-time migration runs against the SQLite database directly. "
            "Run it locally before switching to service mode. "
            "Tracked in nexus follow-on bead (gated on nexus-gmiaf.24)."
        )

    cat = _cat_cmd._get_catalog()
    # epsilon-allow: backfill_owner_id() requires a raw SQLite connection by
    # contract; service mode is guarded above. This pair will remain until
    # the backfill function is refactored to use the catalog API.
    with cat._db:  # epsilon-allow: SQLite-only write, service mode guarded above
        result = backfill_owner_id(
            cat._db,  # epsilon-allow: SQLite-only write, service mode guarded above
            include_documents_fallback=from_documents,
            dry_run=dry_run,
        )

    verb = "would update" if dry_run else "updated"
    click.echo(
        f"{verb} {result.updated_from_name} via name + "
        f"{result.updated_from_documents} via documents fallback "
        f"(total empty: {result.total_empty})"
    )
    if result.skipped_ambiguous:
        click.echo(
            f"  skipped {result.skipped_ambiguous} ambiguous "
            f"(multi-owner) collection(s)"
        )
    if result.skipped_unresolvable:
        click.echo(
            f"  skipped {result.skipped_unresolvable} unresolvable "
            f"collection(s) — manual review required"
        )


def register(group: click.Group) -> None:
    """Attach the owner-id backfill command to the shared ``catalog`` group."""
    group.add_command(backfill_owner_id_cmd)
