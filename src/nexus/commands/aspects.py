# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""CLI command: ``nx aspects`` — aspect extraction queue management.

Subcommands:

  drain         -- drain the aspect extraction queue before a PK migration.
  gc            -- garbage-collect orphan aspect rows.
  gc-fixtures   -- hard-delete test-fixture aspect rows (consumer-driven).

K2 fix (RDR-108 Phase 1, nexus-lh8c): adds the ``nx aspects drain``
operator-facing verb so upgrade docs and MigrationError messages can
point to a concrete, runnable command.

RDR-120 §A8 / nexus-yulol: ``gc-fixtures`` was carved out of the PK-swap
migrations' Step 2 fixture-DELETE block; it and ``aspects gc`` are
unconditional guided refusals since the RDR-158 P4 retirement deleted
the local SQLite stores they swept (the ``_FIXTURE_COLLECTION_PATTERNS``
classifier died with the sweep).
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
    from nexus.aspect_worker import DrainTimeoutError, drain_worker  # noqa: PLC0415 — deferred local import — avoids import-time cost / circular deps
    from nexus.commands._helpers import default_db_path  # noqa: PLC0415 — deferred local import — avoids import-time cost / circular deps

    mem_path = default_db_path()
    click.echo(f"Draining service aspect queue (timeout={timeout}s)...")

    try:
        drain_worker(mem_path, timeout=timeout, poll_interval=poll_interval)
    except DrainTimeoutError as e:
        base = f"Drain timeout: {e.stuck_count} row(s) still active after {timeout}s."
        # Honor the honest service-mode hint (e.g. crashed-worker rows stuck
        # in_progress -> a running worker's stale-reclaim loop resets them). Falls back to the generic re-run
        # advice when no detail was attached.
        suffix = (
            f" {e.detail}"
            if e.detail
            else " Re-run after the worker processes or times out its in-flight rows."
        )
        click.echo(base + suffix, err=True)
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
    """RETIRED: the source_uri-keyed orphan sweep has no service equivalent.

    \b
    The sweep ATTACHed the local .catalog.db SQLite cache to the local T2 —
    both substrates were deleted in the RDR-158 P4 retirement, so the verb
    is an unconditional guided refusal (nexus-ingey precedent: refuse loud
    rather than print a clean bill of health for a sweep that never ran).
    """
    # nexus-ingey: on the service backend delete_orphans was a documented
    # (0, 0) noop, and (0, 0) is INDISTINGUISHABLE downstream from
    # "examined every row, found nothing wrong". Refuse loud instead.
    #
    # State BOTH halves, because the honest answer is not simply
    # "unavailable":
    #   - Rows orphaned by a DOCUMENT DELETE cannot accumulate on the service
    #     backend: fk-001-catalog-cross-store binds
    #     document_aspects (tenant_id, doc_id) -> catalog_documents
    #     (tenant_id, tumbler) ON DELETE CASCADE.
    #   - The sweep THIS verb performed keyed on source_uri, which that
    #     changeset deliberately does NOT constrain ("a URI path string, not
    #     a catalog tumbler reference"). So it has no service equivalent, and
    #     claiming a clean sweep here would be a different lie from the one
    #     being fixed.
    raise click.UsageError(
        "aspects gc is retired: the orphan sweep ATTACHed the local SQLite "
        ".catalog.db to the local T2, and both substrates were deleted in "
        "the RDR-158 P4 retirement (service/Postgres is the only backend).\n"
        "\n"
        "On the service backend, aspect rows orphaned by a document delete "
        "cannot accumulate: document_aspects.doc_id is FK-bound to "
        "catalog_documents ON DELETE CASCADE (fk-001-catalog-cross-store).\n"
        "\n"
        "NOT covered by that FK: the source_uri-keyed sweep this verb "
        "performed. source_uri is a path string, not a tumbler reference, and "
        "is deliberately unconstrained — so this command can neither run nor "
        "certify that class clean. Track: nexus-ingey."
    )


@aspects_group.command(name="gc-fixtures")
@click.option(
    "--yes",
    is_flag=True,
    default=False,
    help="Confirm the destructive delete. Without this flag the "
    "command is a dry-run report only.",
)
def aspects_gc_fixtures(yes: bool) -> None:
    """RETIRED: fixture cleanup used raw SQLite cursors that no longer exist.

    \b
    The verb issued raw SQL DELETEs against the local document_aspects and
    aspect_extraction_queue SQLite tables (RDR-120 §A8 / nexus-yulol). Those
    stores were deleted in the RDR-158 P4 retirement, so the verb is an
    unconditional guided refusal. Track: nexus-gmiaf.37.
    """
    raise click.UsageError(
        "gc-fixtures is retired: fixture cleanup issued raw SQL DELETEs via "
        "SQLite cursors, and the local SQLite document_aspects / "
        "aspect_extraction_queue stores were deleted in the RDR-158 P4 "
        "retirement (service/Postgres is the only backend; raw cursors are "
        "unavailable over HTTP). Track: nexus-gmiaf.37"
    )


@aspects_group.command(name="backfill-source-uri")
@click.option(
    "--apply",
    is_flag=True,
    default=False,
    help="Actually write the source_uri backfill. Without this flag "
    "the command is a dry-run report only.",
)
def aspects_backfill_source_uri(apply: bool) -> None:
    """RETIRED: the pre-migration source_uri repair has no subject here.

    \b
    The verb backfilled ``source_uri`` on the LOCAL SQLite
    ``document_aspects`` so ``migrate_drop_source_path_column`` (4.31.0)
    could proceed. The migration chain it unblocked was deleted in the
    RDR-158 P4 retirement, and the local ``.db`` is a FROZEN migration
    source (RDR-176 Gap 2) that this version must never write — the
    unguarded raw UPDATE this verb carried was the last write path into
    it (Stage 4 critique Critical). Run the repair, if ever needed, on
    the last migration-capable 6.x release, which still ships both the
    verb and the migration it serves.
    """
    raise click.UsageError(
        "aspects backfill-source-uri is retired: it repaired the local "
        "SQLite document_aspects ahead of a migration chain that was "
        "deleted in the RDR-158 P4 retirement, and on this version the "
        "local .db is a frozen migration source that must not be written "
        "(RDR-176 Gap 2). If a pre-migration install still needs the "
        "repair, run it on the last migration-capable 6.x release."
    )


@aspects_group.command(name="gc-pre-rdr096")
@click.option(
    "--apply",
    is_flag=True,
    default=False,
    help="Actually delete pre-RDR-096 read-failure rows. Without this "
    "flag the command is a dry-run report only.",
)
def aspects_gc_pre_rdr096(apply: bool) -> None:
    """RETIRED: the pre-RDR-096 read-failure sweep wrote the frozen source.

    \b
    The seven-clause discriminator deleted pre-RDR-096 read-failure rows
    from the LOCAL SQLite ``document_aspects``. On this version that file
    is a FROZEN migration source (RDR-176 Gap 2); the raw DELETE this verb
    carried was an unguarded write path into it (Stage 4 critique
    Critical). The engine's live table cannot hold the pre-RDR-096
    fingerprint (the going-forward writer contract predates the
    migration), so there is nothing to sweep service-side either.
    """
    raise click.UsageError(
        "aspects gc-pre-rdr096 is retired: it deleted pre-RDR-096 rows "
        "from the local SQLite document_aspects, which on this version is "
        "a frozen migration source that must not be written (RDR-176 "
        "Gap 2). If a pre-migration install still needs the sweep, run it "
        "on the last migration-capable 6.x release."
    )


@aspects_group.command(name="requeue-failed")
@click.option(
    "--collection",
    default=None,
    help="Only re-enqueue failed rows in this collection (default: all).",
)
@click.option(
    "--limit",
    type=int,
    default=None,
    help="Re-enqueue at most this many rows (oldest-enqueued first). Use to "
    "pace recovery of a large backlog and avoid a thundering herd of workers "
    "hammering a just-restored API quota.",
)
@click.option(
    "--dry-run",
    is_flag=True,
    default=False,
    help="Report the rows that would be re-enqueued without writing.",
)
def aspects_requeue_failed(
    collection: str | None, limit: int | None, dry_run: bool,
) -> None:
    """Bulk re-enqueue terminal-``failed`` aspect-queue rows (nexus-2c51v).

    A row reaches ``failed`` after exhausting the backoff-retry ladder
    (RDR-163) or on a non-retryable error. Once the operator fixes the
    root cause (restored API quota, repaired source identity), this verb
    re-enqueues each failed row at its ``(collection, source_path)`` key,
    resetting it to ``pending`` with ``retry_count=0`` (the exhaustion depth
    shown in ``--dry-run`` is discarded) so the worker picks it up again.
    The write is daemon-routed (nexus-zir76); reads use the active backend
    (SQLite or the PG service).

    \b
    Rows are processed oldest-``enqueued_at``-first (enqueue order, NOT
    most-recently-failed). ``--limit`` caps how many are re-enqueued.

    \b
    Examples:
      nx aspects requeue-failed                       # all failed rows
      nx aspects requeue-failed --collection knowledge__x
      nx aspects requeue-failed --limit 100           # pace a large backlog
      nx aspects requeue-failed --dry-run             # report only, no writes

    \b
    Operator note: single-operator recovery verb. It only touches ``failed``
    rows (a terminal state no worker writes), so it is safe to re-run; but do
    not run two instances concurrently — the read-snapshot / per-row-write
    split has no cross-row transaction, so concurrent runs could redundantly
    re-enqueue the same rows.

    \b
    Pairs with the RDR-163 ladder: the ladder reduces how often rows reach
    terminal; this clears the ones that still do. Failed-backlog visibility
    is ``nx doctor --check-aspect-queue``.
    """
    from nexus.commands._helpers import default_db_path  # noqa: PLC0415 — deferred local import — avoids import-time cost / circular deps
    from nexus.db.t2 import T2Database  # noqa: PLC0415 — deferred local import — avoids import-time cost / circular deps
    from nexus.mcp_infra import t2_index_write  # noqa: PLC0415 — deferred local import — avoids import-time cost / circular deps

    if limit is not None and limit <= 0:
        click.echo("--limit must be a positive integer.", err=True)
        raise SystemExit(1)

    # No local-file existence gate: the queue is engine-side (HttpAspectQueue
    # is the only queue since nexus-i711w Stage 2 sub-stage A), so the local
    # memory.db file's absence says nothing about whether failed rows exist.
    # The old gate no-opped the verb on any box without the legacy file
    # (porter-b defect report, 2026-07-30).
    mem_path = default_db_path()

    # Read is concurrent-safe (no single-writer concern); the facade routes
    # to the PG-service HTTP client.
    with T2Database(mem_path) as db:  # epsilon-allow: read-only failed-row inspection for requeue-failed; routes to active backend, no WAL writer contention
        failed = db.aspect_queue.list_failed(collection)

    if limit is not None:
        failed = failed[:limit]

    scope = f" in {collection}" if collection else ""
    if not failed:
        click.echo(f"aspect_extraction_queue: no failed rows{scope}.")
        return

    if dry_run:
        click.echo(f"Would re-enqueue {len(failed)} failed row(s){scope}:")
        for row in failed:
            click.echo(f"  {row.collection}  {row.source_path}  (retry_count={row.retry_count})")
        click.echo("Re-run without --dry-run to re-enqueue.")
        return

    for row in failed:
        # Daemon-routed write (nexus-zir76); INSERT OR REPLACE resets the row
        # to pending / retry_count=0 / clears any stale next_retry_at backoff.
        t2_index_write(
            lambda db, _r=row: db.aspect_queue.enqueue(
                _r.collection, _r.source_path,
                content_hash=_r.content_hash, content=_r.content,
                doc_id=_r.doc_id,
            )
        )
    click.echo(f"Re-enqueued {len(failed)} failed row(s){scope} to pending.")
