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
    """Backfill empty/NULL ``source_uri`` rows in ``document_aspects``.

    \b
    RDR-096 introduced ``document_aspects.source_uri`` (4.16.0) and
    the writer began emitting it on every new row from that point.
    Pre-existing rows and a transient writer-path gap left some rows
    with NULL or empty ``source_uri``; ``migrate_drop_source_path_column``
    (4.31.0) refuses to drop the legacy column until every row has a
    URI, so operators must run this verb before the next upgrade if
    their database has any unbackfilled rows.

    \b
    The verb is idempotent: only touches rows where ``source_uri`` is
    NULL or empty AND ``source_path`` is populated. Rows with empty
    ``source_path`` (research-2 mitigation, very rare) are skipped
    and reported separately for manual triage.

    \b
    URI scheme rules (matches the writer at
    ``nexus.aspect_extractor._build_record``):

      file://    rdr__* / docs__* / code__* (filesystem-backed)
      chroma://  knowledge__* and other chroma-backed prefixes

    \b
    Examples:
      nx aspects backfill-source-uri            # dry-run report
      nx aspects backfill-source-uri --apply    # actually write

    \b
    RDR-120 §A8 / nexus-6y2a9: carved out of
    ``migrate_document_aspects_source_uri`` and
    ``migrate_document_aspects_source_uri_backfill_empty``.
    """
    import sqlite3  # noqa: PLC0415 — deferred to keep CLI startup fast
    from nexus.aspect_readers import uri_for  # noqa: PLC0415 — deferred local import — avoids import-time cost / circular deps
    from nexus.commands._helpers import default_db_path  # noqa: PLC0415 — deferred local import — avoids import-time cost / circular deps

    mem_path = default_db_path()
    if not mem_path.exists():
        click.echo(f"No T2 database at {mem_path}; nothing to do.")
        return

    # Direct sqlite3 connection (epsilon-allow: RDR-120 §A8 verb).
    # T2Database.__init__ runs the migration chain on open; if any
    # downstream migration fails (e.g., migrate_drop_source_path_column
    # blocks on unbackfilled rows) the facade cannot open, leaving the
    # operator unable to run the very verb that fixes the precondition.
    # The verb's whole purpose is to operate when the migration chain
    # is stuck on this exact state, so opening sqlite3 directly is
    # correct here.
    conn = sqlite3.connect(str(mem_path))  # epsilon-allow: pre-migration repair verb
    try:
        cols = {
            r[1]
            for r in conn.execute("PRAGMA table_info(document_aspects)").fetchall()
        }
        if not cols:
            click.echo(
                "document_aspects table not present; nothing to do."
            )
            return
        if "source_uri" not in cols:
            click.echo(
                "document_aspects.source_uri column not present; the "
                "schema migration that adds it has not run yet. Run "
                "`nx upgrade` first.",
                err=True,
            )
            raise SystemExit(1)
        if "source_path" not in cols:
            click.echo(
                "document_aspects.source_path column already dropped; "
                "nothing to backfill from."
            )
            return

        rows = conn.execute(
            "SELECT rowid, collection, source_path FROM document_aspects "
            "WHERE (source_uri IS NULL OR source_uri = '') "
            "  AND source_path IS NOT NULL "
            "  AND source_path != ''",
        ).fetchall()
        empty_source_path = conn.execute(
            "SELECT COUNT(*) FROM document_aspects "
            "WHERE (source_uri IS NULL OR source_uri = '') "
            "  AND (source_path IS NULL OR source_path = '')",
        ).fetchone()[0]

        backfilled = 0
        skipped = 0
        if apply and rows:
            conn.execute("BEGIN")
            try:
                for rowid, collection, source_path in rows:
                    uri = uri_for(collection, source_path)
                    if uri is None:
                        skipped += 1
                        continue
                    conn.execute(
                        "UPDATE document_aspects SET source_uri = ? "
                        "WHERE rowid = ?",
                        (uri, rowid),
                    )
                    backfilled += 1
                conn.commit()
            except Exception:
                conn.rollback()
                raise
        else:
            for _, collection, source_path in rows:
                if uri_for(collection, source_path) is None:
                    skipped += 1
                else:
                    backfilled += 1

        verb = "backfilled" if apply else "would backfill"
        click.echo(
            f"document_aspects: {verb} {backfilled} row(s); "
            f"{skipped} row(s) had unresolvable URI; "
            f"{empty_source_path} row(s) have empty source_path "
            "(manual triage required)"
        )
        if backfilled > 0 and not apply:
            click.echo(
                "Re-run with --apply to actually write the backfill."
            )
    finally:
        conn.close()


_GC_PRE_RDR096_PREDICATE = (
    "WHERE problem_formulation IS NULL "
    "  AND proposed_method IS NULL "
    "  AND (experimental_datasets IS NULL OR experimental_datasets = '[]') "
    "  AND (experimental_baselines IS NULL OR experimental_baselines = '[]') "
    "  AND experimental_results IS NULL "
    "  AND (extras IS NULL OR extras = '{}')"
    "  AND confidence IS NULL"
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
    """Delete pre-RDR-096 read-failure rows from ``document_aspects``.

    \b
    The seven-clause discriminator from RDR-096 research-3 (id 1010)
    identifies rows that pre-RDR-096 extractors emitted as the
    fingerprint of a read failure: every aspect field empty plus
    ``confidence IS NULL``. The going-forward writer contract is that
    structured-zero successes (parser ran, no scholarly structure)
    write ``confidence = 1.0`` explicitly, so ``confidence IS NULL``
    combined with all-empty fields is structurally reachable only
    from a pre-RDR-096 read failure.

    \b
    Two clauses are load-bearing:

      * ``confidence IS NULL`` — without it, the verb silently drops
        ``rdr-frontmatter-v1`` structured-zero successes (51 such rows
        on live nexus_rdr at RDR-096 ship time).
      * ``(experimental_datasets IS NULL OR = '[]')`` and analogous
        for baselines / extras — the writer stores ``json.dumps([])``
        which is the literal string ``'[]'``, not SQL NULL.

    \b
    Idempotent: re-running on a cleaned database deletes 0 rows.
    Safe on a missing table or empty database.

    \b
    Examples:
      nx aspects gc-pre-rdr096            # dry-run report
      nx aspects gc-pre-rdr096 --apply    # actually delete

    \b
    RDR-120 §A8 / nexus-6y2a9: carved out of
    ``migrate_drop_null_aspect_rows``.
    """
    import sqlite3  # noqa: PLC0415 — deferred to keep CLI startup fast
    from nexus.commands._helpers import default_db_path  # noqa: PLC0415 — deferred local import — avoids import-time cost / circular deps

    mem_path = default_db_path()
    if not mem_path.exists():
        click.echo(f"No T2 database at {mem_path}; nothing to do.")
        return

    # Direct sqlite3 (epsilon-allow): same rationale as
    # backfill-source-uri above. Pre-migration repair verbs cannot
    # depend on T2Database.__init__'s migration chain succeeding.
    conn = sqlite3.connect(str(mem_path))  # epsilon-allow: pre-migration repair verb
    try:
        cols = {
            r[1]
            for r in conn.execute("PRAGMA table_info(document_aspects)").fetchall()
        }
        if not cols:
            click.echo("document_aspects table not present; nothing to do.")
            return

        matched = conn.execute(
            "SELECT COUNT(*) FROM document_aspects "
            + _GC_PRE_RDR096_PREDICATE,
        ).fetchone()[0]

        if matched == 0:
            click.echo("document_aspects: 0 pre-RDR-096 read-failure rows.")
            return

        if apply:
            cur = conn.execute(
                "DELETE FROM document_aspects " + _GC_PRE_RDR096_PREDICATE,
            )
            conn.commit()
            click.echo(
                f"document_aspects: deleted {cur.rowcount} pre-RDR-096 "
                "read-failure row(s)."
            )
        else:
            click.echo(
                f"document_aspects: would delete {matched} pre-RDR-096 "
                "read-failure row(s). Re-run with --apply."
            )
    finally:
        conn.close()


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
