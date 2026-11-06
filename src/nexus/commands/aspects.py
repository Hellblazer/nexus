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

from typing import Any

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
    then waits until the ``aspects`` tuple queue has no available or claimed
    task, or the timeout elapses. Dead tasks do not block it.

    Use this before running ``nx upgrade`` when the MigrationError reports
    that the aspect queue is not drained.

    Exit codes:
      0  The queue is drained (or was already empty).
      1  Timeout: a task is still available or claimed after --timeout seconds.
    """
    from nexus.aspect_worker import DrainTimeoutError, drain_worker  # noqa: PLC0415 — deferred local import — avoids import-time cost / circular deps
    from nexus.commands._helpers import default_db_path  # noqa: PLC0415 — deferred local import — avoids import-time cost / circular deps

    mem_path = default_db_path()
    click.echo(f"Draining service aspect queue (timeout={timeout}s)...")

    try:
        drain_worker(mem_path, timeout=timeout, poll_interval=poll_interval)
    except DrainTimeoutError as e:
        base = f"Drain timeout: {e.stuck_count} task(s) still active after {timeout}s."
        # Honor the honest service-mode hint (e.g. lapsed claims the next
        # claim releases). Falls back to the generic re-run advice when no
        # detail was attached.
        suffix = (
            f" {e.detail}"
            if e.detail
            else " Re-run after the worker processes or times out its in-flight tasks."
        )
        click.echo(base + suffix, err=True)
        raise SystemExit(1) from e

    click.echo("Aspect queue drained. Safe to run 'nx upgrade'.")


# nexus-uks83: the four retired verbs below are HIDDEN from `--help`, not
# deleted. Deleting them would answer an old invocation with "No such
# command", which is exactly the silent outcome the nexus-ingey precedent
# rejected: each one refuses LOUDLY and names what to do instead, and
# tests/test_aspects_consumer_verbs.py pins that guidance. Hiding keeps the
# guidance for anyone who still types the verb while leaving `--help` a list
# of things that work.
@aspects_group.command(name="gc", hidden=True)
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


@aspects_group.command(name="gc-fixtures", hidden=True)
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


@aspects_group.command(name="backfill-source-uri", hidden=True)
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


@aspects_group.command(name="gc-pre-rdr096", hidden=True)
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


#: Rows asked of the ``aspects`` subspace per ``rd`` page. The engine clamps ``n``
#: to its own read cap (TupleRepository, 300 by default), so a page SHORTER than
#: this is not the last page: the scan stops on an empty page.
_TASK_PAGE: int = 300


def _task_identity(row) -> dict | None:  # noqa: ANN001 — a TupleRow
    """The identity body of an ``aspects`` task (``collection``, ``source_path``,
    ``doc_id``, ``content_hash``), or None when the body is not that."""
    import json  # noqa: PLC0415 — deferred local import — avoids import-time cost / circular deps

    try:
        body = json.loads(row.body or "")
    except ValueError:
        return None
    return body if isinstance(body, dict) and body.get("doc_id") else None


_CENSUS_UNREAD: Any = object()


def _dead_aspect_tasks(tuples, collection: str | None, *, census: Any = _CENSUS_UNREAD) -> list:  # noqa: ANN001 — an HttpTupleStore
    """Dead tasks of the ``aspects`` subspace, oldest first, optionally those of
    one collection (read from the body: the queue is one per tenant).

    ``rd`` has no state filter, so the scan pages the whole subspace with
    ``since=(created_at, id)`` and keeps the rows whose ``claim_state`` is
    ``dead``; the census says first whether there are any, which spares a
    10,000-row scan on a healthy queue. An engine with no ``aspects`` template
    has none (RDR-235)."""
    from nexus.db.t2.http_aspect_queue import ASPECTS_KEYS, ASPECTS_SUBSPACE, aspects_census  # noqa: PLC0415 — deferred local import — avoids import-time cost / circular deps

    if census is _CENSUS_UNREAD:  # a caller that already read it passes it in
        census = aspects_census(tuples)
    if census is None or census.dead == 0:
        return []
    dead: list = []
    since = None
    while True:
        page = tuples.rd(ASPECTS_SUBSPACE, dict(ASPECTS_KEYS), n=_TASK_PAGE, since=since)
        if not page:
            break
        dead.extend(r for r in page if r.claim_state == "dead")
        cursor = (page[-1].created_at, page[-1].id)
        if cursor == since:  # an engine that ignored `since` would loop forever
            break
        since = cursor
    if collection:
        dead = [
            r for r in dead
            if (ident := _task_identity(r)) is not None and ident.get("collection") == collection
        ]
    return dead


@aspects_group.command(name="requeue-failed")
@click.option(
    "--collection",
    default=None,
    help="Only revive dead tasks in this collection (default: all).",
)
@click.option(
    "--limit",
    type=int,
    default=None,
    help="Revive at most this many tasks (oldest first). Use to "
    "pace recovery of a large backlog and avoid a thundering herd of workers "
    "hammering a just-restored API quota.",
)
@click.option(
    "--dry-run",
    is_flag=True,
    default=False,
    help="Report the tasks that would be revived without writing.",
)
def aspects_requeue_failed(
    collection: str | None, limit: int | None, dry_run: bool,
) -> None:
    """Bulk revive dead tasks of the ``aspects`` queue (nexus-2c51v).

    A task dies when its attempts run out on the backoff-retry ladder
    (RDR-163), when a worker dead-letters it for a non-retryable error, or
    when the engine's lease sweep finds it at ``max_attempts``. Once the
    operator fixes the root cause (restored API quota, repaired source
    identity), this verb revives each dead task with ``out`` ``mode=replace``
    on its own body, which makes it available again with its attempts reset
    and its retention clock restarted, so a worker picks it up again.
    ``--collection`` matches a task by the collection in its body. An engine
    with no ``aspects`` template has no dead tasks.

    \b
    Examples:
      nx aspects requeue-failed                       # all dead tasks
      nx aspects requeue-failed --collection knowledge__x
      nx aspects requeue-failed --limit 100           # pace a large backlog
      nx aspects requeue-failed --dry-run             # report only, no writes

    \b
    Operator note: single-operator recovery verb. It only touches ``dead``
    tasks (a terminal state, never written by a claim), so it is safe to
    re-run; but do not run two instances concurrently, as the read snapshot
    and the per-task write have no cross-task transaction.

    \b
    Pairs with the RDR-163 ladder: the ladder reduces how often tasks reach
    terminal; this clears the ones that still do. Dead-backlog visibility is
    ``nx doctor --check-aspect-queue``.

    A revive is an enqueue, so the dead tasks pass the sensitivity gate
    first (RDR-243): the task of a document flagged sensitive stays dead and
    is named on stderr, and when the catalog cannot say, nothing is revived
    and the command exits 1. ``--limit`` counts the tasks revived.

    The retired table's failed rows are not read: the table is gone
    (nexus-hbnml).
    """
    from nexus.commands._helpers import default_db_path  # noqa: PLC0415 — deferred local import — avoids import-time cost / circular deps
    from nexus.db.t2 import T2Database  # noqa: PLC0415 — deferred local import — avoids import-time cost / circular deps
    from nexus.db.t2.http_aspect_queue import (  # noqa: PLC0415 — deferred local import — avoids import-time cost / circular deps
        ASPECTS_KEYS,
        ASPECTS_SUBSPACE,
        AspectQueueTableModeRetiredError,
        aspect_queue_mode,
        aspects_census,
    )
    from nexus.mcp_infra import t2_index_write  # noqa: PLC0415 — deferred local import — avoids import-time cost / circular deps

    if limit is not None and limit <= 0:
        click.echo("--limit must be a positive integer.", err=True)
        raise SystemExit(1)

    # A stale NX_ASPECT_QUEUE=table is refused here, before any read or write, with the
    # retirement message.
    try:
        aspect_queue_mode()
    except AspectQueueTableModeRetiredError as exc:
        click.echo(str(exc), err=True)
        raise SystemExit(1) from exc

    # No local-file existence gate: the queue is engine-side, so the local
    # memory.db file's absence says nothing about whether dead tasks exist.
    mem_path = default_db_path()

    # Read is concurrent-safe (no single-writer concern); the facade routes
    # to the PG-service HTTP client.
    with T2Database(mem_path) as db:  # boundary-allow: read-only dead-task inspection for requeue-failed; routes to active backend, no WAL writer contention
        try:
            census = aspects_census(db.tuples)
            dead = _dead_aspect_tasks(db.tuples, collection, census=census)
        except Exception as exc:  # noqa: BLE001 — fail loud before any write
            click.echo(
                f"Could not read the dead tasks of the 'aspects' queue: {exc}. "
                "Nothing was re-enqueued.",
                err=True,
            )
            raise SystemExit(1) from exc

    scope = f" in {collection}" if collection else ""
    if not dead:
        click.echo(f"aspects queue: no dead tasks{scope}.")
        return

    # RDR-243 census row 1: a revive is an enqueue, so it passes the one enqueue gate. A dead
    # task whose document was flagged after it died is not revived; it stays dead (the body is
    # identity only, never text, and retention clears it). A catalog that cannot answer revives
    # nothing, before any write.
    from nexus.aspect_worker import sensitive_enqueue_filter  # noqa: PLC0415 — deferred local import — avoids import-time cost / circular deps
    from nexus.doc_sensitivity import SensitivityUnknown  # noqa: PLC0415 — deferred local import — avoids import-time cost / circular deps

    idents = [i for t in dead if (i := _task_identity(t)) is not None]
    try:
        clear = {r["doc_id"] for r in sensitive_enqueue_filter(idents)}
    except SensitivityUnknown as exc:
        click.echo(
            "The catalog could not say whether the dead tasks' documents are sensitive "
            f"({exc.detail}). Nothing was revived.",
            err=True,
        )
        raise SystemExit(1) from exc
    flagged = [t for t in dead if (i := _task_identity(t)) is not None and i["doc_id"] not in clear]
    flagged_ids = {t.id for t in flagged}
    dead = [t for t in dead if t.id not in flagged_ids]
    # --limit counts revivals, so it slices after the gate: flagged tasks stay dead and sort
    # oldest first, and slicing before would let them fill every limited run.
    if limit is not None:
        dead = dead[:limit]

    if dry_run:
        click.echo(f"Would revive {len(dead)} dead task(s){scope}:")
        for task in dead:
            ident = _task_identity(task) or {}
            click.echo(
                f"  {ident.get('collection', '?')}  {ident.get('source_path', '?')}"
                f"  (attempts={task.attempts})"
            )
        if flagged:
            click.echo(f"Would leave {len(flagged)} dead task(s): the document is flagged sensitive:")
            for task in flagged:
                ident = _task_identity(task) or {}
                click.echo(f"  {ident.get('collection', '?')}  {ident.get('source_path', '?')}  ({ident.get('doc_id', '?')})")
        click.echo("Re-run without --dry-run to revive.")
        return

    for task in flagged:
        ident = _task_identity(task) or {}
        click.echo(
            f"Left dead task {task.id} dead: its document {ident.get('doc_id', '?')} "
            f"({ident.get('collection', '?')} {ident.get('source_path', '?')}) is flagged sensitive.",
            err=True,
        )
    revived = 0
    for task in dead:
        ident = _task_identity(task)
        if ident is None:
            click.echo(
                f"Skipped dead task {task.id}: its body is not an identity body.", err=True,
            )
            continue
        # out mode=replace on the same body and nonce: the dead task becomes
        # available again with attempts reset and a restarted retention clock.
        # Daemon-routed write (nexus-zir76).
        t2_index_write(
            lambda db, _b=task.body, _n=ident["doc_id"]: db.tuples.out_replace(
                ASPECTS_SUBSPACE, dict(ASPECTS_KEYS), None, _b, nonce=_n,
            )
        )
        revived += 1
    left = f"; left {len(flagged)} dead (sensitive document)" if flagged else ""
    click.echo(f"Revived {revived} dead task(s){scope} to available{left}.")

