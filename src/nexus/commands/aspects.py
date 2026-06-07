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
migrations' Step 2 fixture-DELETE block. The substrate retains only
the structurally-required PK swap; operators run the fixture cleanup
explicitly against named patterns. ``_FIXTURE_COLLECTION_PATTERNS``
lives here, not in ``nexus.db.migrations``.
"""
from __future__ import annotations

import click
import structlog

_log = structlog.get_logger(__name__)


#: Test-fixture collection prefixes/names recognised by ``gc-fixtures``.
#: Patterns ending in ``-`` are LIKE-prefix matched; bare names are
#: equality-matched. Operators with additional fixture collections
#: should add them here.
_FIXTURE_COLLECTION_PATTERNS: tuple[str, ...] = (
    "knowledge__cli-",
    "knowledge__nexus-integration-test",
    "knowledge__reproducer",
    "knowledge__pagtest",
    "knowledge__pagend",
)


def _is_fixture_collection(collection: str) -> bool:
    """Return True iff *collection* is a test-fixture name to hard-delete."""
    for pat in _FIXTURE_COLLECTION_PATTERNS:
        if pat.endswith("-") and collection.startswith(pat):
            return True
        if collection == pat:
            return True
    return False


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

    with T2Database(mem_path) as db:  # epsilon-allow: aspects gc delete_orphans cross-DB ATTACHes the catalog database; not a routable single-store op (RDR-128 P3 documented-irreducible)
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


@aspects_group.command(name="gc-fixtures")
@click.option(
    "--yes",
    is_flag=True,
    default=False,
    help="Confirm the destructive delete. Without this flag the "
    "command is a dry-run report only.",
)
def aspects_gc_fixtures(yes: bool) -> None:
    """Hard-delete test-fixture aspect rows from document_aspects and
    aspect_extraction_queue.

    \b
    Recognises a small allowlist of test-fixture collection prefixes
    (``knowledge__cli-*``, ``knowledge__nexus-integration-test``,
    ``knowledge__reproducer``, ``knowledge__pagtest``,
    ``knowledge__pagend``) that the test suite creates and that
    should never persist into production. The PK-swap migrations
    (4.30.0) used to drop these unconditionally; under RDR-120 §A8
    that fixture cleanup is consumer-driven and explicit.

    \b
    Default is dry-run: reports the per-pattern row counts without
    writing. Pass ``--yes`` to actually delete.

    \b
    Examples:
      nx aspects gc-fixtures            # dry-run report
      nx aspects gc-fixtures --yes      # actually delete

    \b
    Run this before ``nx upgrade`` if the PK-swap migration reports a
    high-volume unmapped collection that matches one of the fixture
    patterns. RDR-120 §A8 / nexus-yulol.
    """
    from nexus.commands._helpers import default_db_path
    from nexus.db.t2 import T2Database

    mem_path = default_db_path()
    if not mem_path.exists():
        click.echo(f"No T2 database at {mem_path}; nothing to do.")
        return

    verb = "deleted" if yes else "would delete"
    any_rows = False
    from nexus.db.storage_mode import StorageBackend, storage_backend_for
    if storage_backend_for("document_aspects") == StorageBackend.SERVICE:
        raise NotImplementedError(
            "gc-fixtures not yet supported on the service backend "
            "(document_aspects=service); fixture cleanup uses raw SQL DELETE "
            "via SQLite cursors which are unavailable over HTTP. "
            "Track: nexus-gmiaf.37"
        )

    with T2Database(mem_path) as db:  # epsilon-allow: gc-fixtures issues raw multi-store DELETE via live cursors, no store method to route (RDR-128 P3 documented-irreducible)
        # Both target stores expose ``conn`` directly (matching the
        # existing module convention; their writers go through the
        # store's lock, but the verb's serial DELETEs do not need the
        # finer-grained guard). The aspect_extraction_queue table is
        # post-RDR-108; older installs may not have it yet, so guard the
        # presence check via PRAGMA.
        stores = [
            ("document_aspects", db.document_aspects.conn),
            ("aspect_extraction_queue", db.aspect_queue.conn),
        ]
        for table, conn in stores:
            present = conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
                (table,),
            ).fetchone()
            if not present:
                continue
            for pat in _FIXTURE_COLLECTION_PATTERNS:
                if pat.endswith("-"):
                    count_sql = (
                        f"SELECT COUNT(*) FROM {table} WHERE collection LIKE ?"
                    )
                    delete_sql = (
                        f"DELETE FROM {table} WHERE collection LIKE ?"
                    )
                    arg = pat + "%"
                else:
                    count_sql = (
                        f"SELECT COUNT(*) FROM {table} WHERE collection = ?"
                    )
                    delete_sql = (
                        f"DELETE FROM {table} WHERE collection = ?"
                    )
                    arg = pat

                n = conn.execute(count_sql, (arg,)).fetchone()[0]
                if n == 0:
                    continue
                any_rows = True
                click.echo(f"  {table} / {pat}: {verb} {n} row(s)")
                if yes:
                    conn.execute(delete_sql, (arg,))
                    conn.commit()
    if not any_rows:
        click.echo("No fixture rows found.")
    elif not yes:
        click.echo("Re-run with --yes to actually delete the fixture rows.")
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
    import sqlite3
    from nexus.aspect_readers import uri_for
    from nexus.commands._helpers import default_db_path

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
    import sqlite3
    from nexus.commands._helpers import default_db_path

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
