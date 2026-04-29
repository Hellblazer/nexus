# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""``nx t3`` command group — T3 (ChromaDB) maintenance.

``nx t3 prune-stale`` (RDR-090 P1.4 / nexus-u7r0) sweeps each T3
collection's source_path values, removes chunks whose on-disk source
file is missing.

The collection mode iterates ``T3Database.list_unique_source_paths``
plus a ``Path(p).exists()`` check; the staleness predicate is
intentionally simple (file present / absent) — broken-symlink
handling and partial-content checks are out of scope.

Out of scope:
  - Catalog-side prune-stale (``nx catalog prune-stale``) is a
    separate bead (nexus-zg4c).
  - The ``nx collection audit --verify-chroma`` cross-check between
    catalog chunk_ids and chroma chunk_ids (GH #335) shares the
    drift-detection idea but is a different surface.
"""
from __future__ import annotations

from pathlib import Path

import click
import structlog

_log = structlog.get_logger(__name__)


@click.group()
def t3() -> None:
    """T3 (ChromaDB) maintenance commands."""


@t3.command("prune-stale")
@click.option(
    "--collection",
    "-c",
    default="",
    help="Limit to one collection. Omit to scan every T3 collection.",
)
@click.option(
    "--dry-run/--no-dry-run",
    default=True,
    help="Report-only (default). Use --no-dry-run to perform deletions.",
)
@click.option(
    "--confirm",
    is_flag=True,
    default=False,
    help="Required alongside --no-dry-run to actually delete chunks "
    "(the explicit-affirmation belt-and-suspenders pattern).",
)
def prune_stale_cmd(collection: str, dry_run: bool, confirm: bool) -> None:
    """Delete T3 chunks whose ``source_path`` is missing from disk.

    \b
    Reports per-collection summary lines: stale source_paths and
    chunk counts. By default this is read-only (--dry-run is on).

    \b
    To actually delete, pass BOTH --no-dry-run AND --confirm. The
    two-flag dance is deliberate: --no-dry-run flips the intent,
    --confirm verifies the operator typed it on purpose. Either flag
    alone runs the report without deleting.

    \b
    Examples:
      nx t3 prune-stale                              # report all collections
      nx t3 prune-stale -c rdr__nexus-571b8edd       # one collection
      nx t3 prune-stale --no-dry-run --confirm       # actually delete
    """
    from nexus.db import make_t3  # noqa: PLC0415

    will_delete = (not dry_run) and confirm
    if (not dry_run) and not confirm:
        click.echo(
            "--no-dry-run alone is treated as report-only. "
            "Add --confirm to actually delete chunks."
        )
        will_delete = False

    t3_db = make_t3()

    if collection:
        target_collections = [collection]
    else:
        try:
            all_colls = t3_db.list_collections()
        except Exception as exc:
            click.echo(f"Failed to list collections: {exc}")
            raise click.exceptions.Exit(1)
        target_collections = [c["name"] for c in all_colls]

    if not target_collections:
        click.echo("No collections to scan.")
        return

    total_stale_paths = 0
    total_stale_chunks = 0
    affected_collections = 0

    for coll_name in target_collections:
        try:
            unique_paths = t3_db.list_unique_source_paths(coll_name)
        except Exception as exc:
            click.echo(f"  {coll_name}: SKIP (list failed: {exc})")
            continue

        stale_paths: list[str] = [p for p in unique_paths if not Path(p).exists()]
        if not stale_paths:
            continue

        affected_collections += 1
        click.echo(f"\n{coll_name}: {len(stale_paths)} stale source_path(s)")
        coll_chunks = 0
        for p in stale_paths:
            try:
                ids = t3_db.ids_for_source(coll_name, p)
            except Exception as exc:
                click.echo(f"  {p}: SKIP (ids_for_source failed: {exc})")
                continue
            click.echo(f"  {p}  ->  {len(ids)} chunk(s)")
            coll_chunks += len(ids)
            if will_delete:
                try:
                    deleted = t3_db.delete_by_source(coll_name, p)
                    if deleted != len(ids):
                        click.echo(
                            f"    WARN: deleted {deleted}, expected {len(ids)}"
                        )
                except Exception as exc:
                    click.echo(f"    delete failed: {exc}")
        total_stale_paths += len(stale_paths)
        total_stale_chunks += coll_chunks

    verb = "deleted" if will_delete else "would delete"
    click.echo(
        f"\nSummary: {verb} {total_stale_chunks} chunk(s) "
        f"across {total_stale_paths} stale path(s) "
        f"in {affected_collections} collection(s)."
    )
