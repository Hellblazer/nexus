# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""CLI command: ``nx aspects`` — aspect extraction queue management.

Subcommands:

  drain   -- drain the aspect extraction queue before a PK migration.

K2 fix (RDR-108 Phase 1, nexus-lh8c): adds the ``nx aspects drain``
operator-facing verb so upgrade docs and MigrationError messages can
point to a concrete, runnable command.
"""
from __future__ import annotations

import click
import structlog

_log = structlog.get_logger(__name__)


@click.group(name="aspects")
def aspects_group() -> None:
    """Aspect extraction queue management."""


@aspects_group.command(name="drain")
@click.option(
    "--timeout",
    default=30.0,
    type=float,
    show_default=True,
    help="Seconds to wait for in-flight rows to complete before raising.",
)
@click.option(
    "--poll-interval",
    default=0.1,
    type=float,
    show_default=True,
    help="Seconds between queue-empty checks.",
)
def aspects_drain(timeout: float, poll_interval: float) -> None:
    """Drain the aspect extraction queue.

    Stops the singleton AspectExtractionWorker (if running in this process),
    then waits until all pending and in-progress rows are processed or the
    timeout elapses.

    Use this before running ``nx upgrade`` when the MigrationError reports
    that the aspect_extraction_queue is not drained.

    Exit codes:
      0  Queue is drained (or was already empty).
      1  Timeout: queue still has active rows after --timeout seconds.
    """
    from nexus.aspect_worker import DrainTimeoutError, drain_worker
    from nexus.commands._helpers import default_db_path

    mem_path = default_db_path()
    click.echo(f"Draining aspect queue at {mem_path} (timeout={timeout}s)...")

    try:
        drain_worker(mem_path, timeout=timeout, poll_interval=poll_interval)
    except DrainTimeoutError as e:
        click.echo(
            f"Drain timeout: {e.stuck_count} row(s) still active after {timeout}s. "
            "Re-run after the worker processes or times out its in-flight rows.",
            err=True,
        )
        raise SystemExit(1) from e

    click.echo("Aspect queue drained. Safe to run 'nx upgrade'.")


@aspects_group.command(name="gc")
@click.option(
    "--apply",
    is_flag=True,
    default=False,
    help="Actually delete orphan rows. Without this flag the command "
    "is a dry-run report only.",
)
def aspects_gc(apply: bool) -> None:
    """Garbage-collect document_aspects rows whose source document was deleted.

    \b
    An aspect row is orphan when its ``source_uri`` no longer appears
    in the catalog ``documents`` table. This happens whenever a
    document is removed (``cat.delete_document``, source-file removal,
    rename) without a corresponding cleanup of the aspect rows. The
    catalog and T2 databases are separate SQLite files (see
    ``docs/architecture.md``) so SQL cross-DB FK CASCADE is not
    available; this verb is the periodic-sweep equivalent.

    \b
    Default is dry-run: reports the orphan count without writing.
    Pass ``--apply`` to actually delete.

    \b
    Aspects with empty ``source_uri`` are NOT classified as orphans
    (legacy / pre-RDR-096 P2.1 rows that lack the URI binding).
    Address those via ``rename_collection`` or direct ``delete``
    paths if needed.

    \b
    Examples:
      nx aspects gc                  # dry-run report
      nx aspects gc --apply          # actually delete

    \b
    Filed under nexus-urj4 (RDR-108 Phase 5 follow-up).
    """
    from nexus.commands._helpers import default_db_path
    from nexus.config import catalog_path
    from nexus.db.t2 import T2Database

    mem_path = default_db_path()
    cat_db = catalog_path() / ".catalog.db"

    if not cat_db.exists():
        click.echo(
            f"No catalog at {cat_db}. Cannot identify orphans without "
            "the live document set; run 'nx catalog setup' first.",
            err=True,
        )
        raise SystemExit(1)

    with T2Database(mem_path) as db:
        orphans, total = db.document_aspects.delete_orphans(
            cat_db, dry_run=not apply,
        )

    verb = "would delete" if not apply else "deleted"
    click.echo(
        f"document_aspects: examined {total} row(s) with non-empty source_uri; "
        f"{verb} {orphans} orphan(s) "
        f"({orphans / total * 100:.1f}% orphan rate)"
        if total > 0 else
        f"document_aspects: examined 0 row(s) with non-empty source_uri; "
        f"{verb} 0 orphan(s)"
    )
    if orphans > 0 and not apply:
        click.echo(
            "Re-run with --apply to actually delete the orphan rows."
        )
