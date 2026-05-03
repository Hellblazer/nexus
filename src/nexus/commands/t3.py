# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""``nx t3`` command group — T3 (ChromaDB) maintenance.

``nx t3 prune-stale`` (RDR-090 P1.4 / nexus-u7r0) sweeps each T3
collection's source_path values, removes chunks whose on-disk source
file is missing.

``nx t3 gc`` (RDR-101 Phase 6 / nexus-r5eo) is the SOLE post-Phase-3
emitter of ``ChunkOrphaned`` events and the SOLE post-Phase-3 path that
deletes T3 chunks. It joins the catalog projection (alive doc_ids per
collection) with T3 chunk metadata and removes chunks whose ``doc_id``
is dead AND whose ``indexed_at`` predates the orphan window.

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

import re
from datetime import UTC, datetime, timedelta
from pathlib import Path

import click
import structlog

_log = structlog.get_logger(__name__)


_DEFAULT_ORPHAN_WINDOW = "30d"
_WINDOW_PATTERN = re.compile(r"^\s*(\d+)\s*([smhdw])\s*$", re.IGNORECASE)
_WINDOW_UNIT_SECONDS = {
    "s": 1,
    "m": 60,
    "h": 3600,
    "d": 86400,
    "w": 604800,
}


def _parse_orphan_window(spec: str) -> timedelta:
    """Parse ``"30d"`` / ``"24h"`` / ``"2w"`` into a :class:`timedelta`.

    Supports s/m/h/d/w suffixes. A bare integer is rejected: operators
    must be explicit about the unit so a typo cannot silently mean
    ``30 seconds`` instead of ``30 days``. Zero (``"0d"``) is rejected:
    a zero window means every chunk older than "now" is eligible,
    which is rarely intentional and is dangerous when paired with
    ``--no-dry-run --yes``.
    """
    match = _WINDOW_PATTERN.match(spec)
    if not match:
        raise click.BadParameter(
            f"--orphan-window must be e.g. '30d' / '12h' / '2w', got {spec!r}"
        )
    n = int(match.group(1))
    if n <= 0:
        raise click.BadParameter(
            f"--orphan-window must be positive, got {spec!r}. "
            f"A zero or negative window would treat every orphaned chunk "
            f"as immediately eligible for deletion."
        )
    unit = match.group(2).lower()
    return timedelta(seconds=n * _WINDOW_UNIT_SECONDS[unit])


def _make_catalog():
    """Construct the default Catalog. Patched in tests for isolation."""
    from nexus.catalog.catalog import Catalog  # noqa: PLC0415
    from nexus.config import catalog_path  # noqa: PLC0415

    path = catalog_path()
    return Catalog(path, path / ".catalog.db")


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


@t3.command("gc")
@click.option(
    "--collection",
    "-c",
    required=True,
    help="Collection to GC. Required (orphan diff is per-collection).",
)
@click.option(
    "--orphan-window",
    default=_DEFAULT_ORPHAN_WINDOW,
    show_default=True,
    help="Grace period before an orphaned chunk becomes eligible for "
    "deletion. Format: e.g. '30d', '12h', '2w'. The default protects "
    "against transient orphans during a re-index.",
)
@click.option(
    "--dry-run/--no-dry-run",
    default=True,
    help="Report-only (default). Use --no-dry-run to actually delete.",
)
@click.option(
    "--yes",
    is_flag=True,
    default=False,
    help="Required alongside --no-dry-run to actually delete chunks. "
    "Without --yes, the command falls back to report-only.",
)
def gc_cmd(
    collection: str,
    orphan_window: str,
    dry_run: bool,
    yes: bool,
) -> None:
    """Garbage-collect orphaned T3 chunks (RDR-101 Phase 6).

    \b
    A chunk is an orphan when:
      - its ``doc_id`` metadata is not in the catalog projection's
        alive set for ``--collection``, AND
      - its ``indexed_at`` predates ``--orphan-window`` (default 30d).

    \b
    Per RF-101-3, ``nx t3 gc`` is the SOLE post-Phase-3 emitter of
    ``ChunkOrphaned`` events and the SOLE path that physically deletes
    T3 chunks. The strict order on each candidate is:

        1. Append ``ChunkOrphaned(chunk_id, reason)`` to the event log.
        2. Call ``T3Database.delete_by_chunk_ids`` for that chunk.

    \b
    A crash between (1) and (2) leaves the log consistent with T3 (event
    present + delete failed): the next ``nx t3 gc`` run idempotently
    retries the delete. The opposite ordering would leave T3 ahead of
    the log (delete succeeded + crash before event), violating
    replay-equality.

    \b
    Chunks missing ``doc_id`` (legacy pre-Phase-2 backfill) are
    UNDECIDABLE here and skipped: operators must run a maintenance
    backfill verb, not GC, to address them.

    \b
    Examples:
      nx t3 gc -c knowledge__delos --dry-run                # report only
      nx t3 gc -c rdr__nexus-571b8edd --no-dry-run --yes    # actually GC
      nx t3 gc -c code__nexus --orphan-window 7d --dry-run  # tighter window
    """
    from nexus.catalog.event_log import EventLog  # noqa: PLC0415
    from nexus.catalog.events import (  # noqa: PLC0415
        ChunkOrphanedPayload,
        make_event,
    )
    from nexus.db import make_t3  # noqa: PLC0415

    window = _parse_orphan_window(orphan_window)
    cutoff = datetime.now(UTC) - window

    will_delete = (not dry_run) and yes
    if (not dry_run) and not yes:
        click.echo(
            "--no-dry-run alone is treated as report-only. "
            "Add --yes to actually delete chunks."
        )
        will_delete = False

    t3_db = make_t3()
    cat = _make_catalog()

    try:
        alive = {e.tumbler for e in cat.list_by_collection(collection)}
    except Exception as exc:
        click.echo(f"Failed to read catalog: {exc}")
        raise click.exceptions.Exit(1)
    alive_str = {str(t) for t in alive}

    candidates: list[tuple[str, str]] = []
    skipped_no_doc_id = 0
    skipped_no_indexed_at = 0
    skipped_within_window = 0

    try:
        chunks = list(t3_db.list_chunks_with_metadata(collection))
    except Exception as exc:
        click.echo(f"Failed to list chunks for {collection}: {exc}")
        raise click.exceptions.Exit(1)

    for chunk_id, meta in chunks:
        doc_id = meta.get("doc_id", "")
        if not doc_id:
            skipped_no_doc_id += 1
            continue
        if doc_id in alive_str:
            continue
        indexed_at = meta.get("indexed_at", "")
        if not indexed_at:
            skipped_no_indexed_at += 1
            continue
        try:
            indexed_dt = datetime.fromisoformat(indexed_at)
        except ValueError:
            skipped_no_indexed_at += 1
            continue
        if indexed_dt > cutoff:
            skipped_within_window += 1
            continue
        candidates.append((chunk_id, doc_id))

    click.echo(
        f"{collection}: {len(candidates)} orphan chunk(s) eligible "
        f"(window={orphan_window})"
    )
    if skipped_no_doc_id:
        click.echo(f"  skipped {skipped_no_doc_id} chunk(s) with no doc_id")
    if skipped_no_indexed_at:
        click.echo(
            f"  skipped {skipped_no_indexed_at} chunk(s) with no/bad indexed_at"
        )
    if skipped_within_window:
        click.echo(
            f"  skipped {skipped_within_window} chunk(s) inside the orphan window"
        )

    for chunk_id, doc_id in candidates:
        click.echo(f"  {chunk_id}  ->  doc_id={doc_id}")

    if not candidates:
        click.echo("\nSummary: 0 orphan(s); nothing to do.")
        return

    if not will_delete:
        click.echo(
            f"\nSummary: would delete {len(candidates)} chunk(s) from {collection}."
        )
        return

    event_log = EventLog(cat._dir)
    deleted_total = 0
    for chunk_id, doc_id in candidates:
        # Strict order (RF-101-3): event first, then delete. A crash
        # between them leaves the log consistent with T3 (event
        # present, delete pending → next gc retries).
        event = make_event(
            ChunkOrphanedPayload(
                chunk_id=chunk_id,
                reason=f"doc_id {doc_id} no longer alive in {collection}",
            )
        )
        event_log.append(event)
        try:
            deleted_total += t3_db.delete_by_chunk_ids(collection, [chunk_id])
        except Exception as exc:
            click.echo(f"  delete failed for {chunk_id}: {exc}")

    click.echo(f"\nSummary: deleted {deleted_total} chunk(s) from {collection}.")
