# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""``nx storage`` — storage migration and ETL commands (RDR-152).

Entry points for migrating T2 SQLite stores to the Postgres service tier.
Phase 1.8 implements memory ETL; Phase 2.1 adds plan ETL.

Usage::

    nx storage migrate memory    [--db PATH] [--service-url URL]
    nx storage migrate plans     [--db PATH] [--service-url URL]
    nx storage migrate telemetry [--db PATH] [--service-url URL]
    nx storage migrate taxonomy  [--db PATH] [--service-url URL]
    nx storage migrate chash     [--db PATH] [--service-url URL]

Run flags:
  --db PATH       Path to the SQLite T2 database (default: auto-detected
                  via NX_DB_PATH / ~/.config/nexus/t2.db).
  --service-url   Override base URL of the nexus-service (default: from
                  NX_SERVICE_HOST + NX_SERVICE_PORT env vars).
  --dry-run       Print row count from source without writing.

The ETL is idempotent: running it multiple times produces no duplicates.
The SQLite source is never modified (copy-not-move).
"""
from __future__ import annotations

import os
from pathlib import Path

import click
import structlog

_log = structlog.get_logger(__name__)


@click.group(name="storage")
def storage_group() -> None:
    """Storage migration and ETL commands (RDR-152)."""


@storage_group.group(name="migrate")
def migrate_group() -> None:
    """Migrate a T2 store from SQLite to the Postgres service tier."""


@migrate_group.command(name="memory")
@click.option(
    "--db",
    "db_path",
    default=None,
    type=click.Path(exists=True, dir_okay=False, readable=True, path_type=Path),
    help=(
        "Path to the SQLite T2 database file. "
        "Defaults to NX_DB_PATH env var or ~/.config/nexus/t2.db."
    ),
)
@click.option(
    "--service-url",
    "service_url",
    default=None,
    help=(
        "Base URL of the nexus-service (e.g. http://127.0.0.1:8080). "
        "Defaults to NX_SERVICE_HOST + NX_SERVICE_PORT env vars."
    ),
)
@click.option(
    "--dry-run",
    is_flag=True,
    default=False,
    help="Count rows in the source without writing. No service connection is made.",
)
def migrate_memory_cmd(
    db_path: Path | None,
    service_url: str | None,
    dry_run: bool,
) -> None:
    """Migrate the SQLite memory store to Postgres via the nexus-service.

    Reads all rows from the SQLite ``memory`` table and writes them through
    the service HTTP API (``PUT /v1/memory/put``).  The ETL is idempotent:
    running it multiple times produces no duplicates (server-side upsert on
    ``(tenant_id, project, title)``).  The SQLite source is NEVER modified.

    Requires NX_SERVICE_PORT and NX_SERVICE_TOKEN to be set (or --service-url
    for the URL component; token is always read from NX_SERVICE_TOKEN).

    Examples::

        # Auto-detect DB, service from env:
        nx storage migrate memory

        # Explicit paths:
        nx storage migrate memory --db ~/.config/nexus/t2.db --service-url http://127.0.0.1:8080

        # Dry run (count only, no writes):
        nx storage migrate memory --dry-run
    """
    # Resolve source DB path
    resolved_db = _resolve_db_path(db_path)
    if not resolved_db.exists():
        raise click.ClickException(
            f"SQLite database not found: {resolved_db}\n"
            "Set NX_DB_PATH or pass --db."
        )

    if dry_run:
        from nexus.db.t2.memory_etl import count_source_rows

        try:
            count = count_source_rows(resolved_db)
        except RuntimeError as exc:
            raise click.ClickException(str(exc))
        click.echo(f"Dry run: source has {count} memory rows (no writes performed).")
        return

    # Construct the HttpMemoryStore
    from nexus.db.t2.http_memory_store import HttpMemoryStore

    token = os.environ.get("NX_SERVICE_TOKEN", "")
    if not token:
        raise click.ClickException(
            "NX_SERVICE_TOKEN is required for storage migrate memory.\n"
            "Set it to the bearer token configured in the nexus-service."
        )

    try:
        if service_url:
            store = HttpMemoryStore(base_url=service_url, _token=token)
        else:
            store = HttpMemoryStore()
    except RuntimeError as exc:
        raise click.ClickException(str(exc))

    # Run the ETL
    from nexus.db.t2.memory_etl import migrate_memory_rows

    click.echo(f"Migrating memory store from {resolved_db} ...")
    try:
        result = migrate_memory_rows(resolved_db, store)
    except Exception as exc:
        raise click.ClickException(f"ETL failed: {exc}")
    finally:
        store.close()

    read_n = result["read"]
    written_m = result["written"]
    skipped = read_n - written_m

    click.echo(f"Done. read={read_n}, written={written_m}", err=False)
    if skipped:
        click.echo(
            f"Warning: {skipped} row(s) failed to write — check logs for details.",
            err=True,
        )

    _log.info(
        "storage.migrate.memory.complete",
        db=str(resolved_db),
        read=read_n,
        written=written_m,
    )


@migrate_group.command(name="plans")
@click.option(
    "--db",
    "db_path",
    default=None,
    type=click.Path(exists=True, dir_okay=False, readable=True, path_type=Path),
    help=(
        "Path to the SQLite T2 database file. "
        "Defaults to NX_DB_PATH env var or ~/.config/nexus/t2.db."
    ),
)
@click.option(
    "--service-url",
    "service_url",
    default=None,
    help=(
        "Base URL of the nexus-service (e.g. http://127.0.0.1:8080). "
        "Defaults to NX_SERVICE_HOST + NX_SERVICE_PORT env vars."
    ),
)
@click.option(
    "--dry-run",
    is_flag=True,
    default=False,
    help="Count rows in the source without writing. No service connection is made.",
)
def migrate_plans_cmd(
    db_path: Path | None,
    service_url: str | None,
    dry_run: bool,
) -> None:
    """Migrate the SQLite plans store to Postgres via the nexus-service.

    Reads all rows from the SQLite ``plans`` table and writes them through
    the service HTTP API (``POST /v1/plans/import``). The ETL is idempotent:
    running it multiple times produces no duplicates (server-side upsert on
    ``(tenant_id, project, query)``). The SQLite source is NEVER modified.

    Fidelity-preserving: ``created_at``, ``use_count``, ``last_used``,
    ``match_count``, ``match_conf_sum``, ``success_count``, and
    ``failure_count`` are copied verbatim from the source row.

    Requires NX_SERVICE_PORT and NX_SERVICE_TOKEN to be set (or --service-url
    for the URL component; token is always read from NX_SERVICE_TOKEN).

    Examples::

        # Auto-detect DB, service from env:
        nx storage migrate plans

        # Explicit paths:
        nx storage migrate plans --db ~/.config/nexus/t2.db --service-url http://127.0.0.1:8080

        # Dry run (count only, no writes):
        nx storage migrate plans --dry-run
    """
    resolved_db = _resolve_db_path(db_path)
    if not resolved_db.exists():
        raise click.ClickException(
            f"SQLite database not found: {resolved_db}\n"
            "Set NX_DB_PATH or pass --db."
        )

    if dry_run:
        from nexus.db.t2.plan_etl import count_source_rows

        try:
            count = count_source_rows(resolved_db)
        except RuntimeError as exc:
            raise click.ClickException(str(exc))
        click.echo(f"Dry run: source has {count} plan rows (no writes performed).")
        return

    from nexus.db.t2.http_plan_library import HttpPlanLibrary

    token = os.environ.get("NX_SERVICE_TOKEN", "")
    if not token:
        raise click.ClickException(
            "NX_SERVICE_TOKEN is required for storage migrate plans.\n"
            "Set it to the bearer token configured in the nexus-service."
        )

    try:
        if service_url:
            store = HttpPlanLibrary(base_url=service_url, _token=token)
        else:
            store = HttpPlanLibrary()
    except RuntimeError as exc:
        raise click.ClickException(str(exc))

    from nexus.db.t2.plan_etl import migrate_plan_rows

    click.echo(f"Migrating plans store from {resolved_db} ...")
    try:
        result = migrate_plan_rows(resolved_db, store)
    except Exception as exc:
        raise click.ClickException(f"ETL failed: {exc}")
    finally:
        store.close()

    read_n = result["read"]
    written_m = result["written"]
    skipped = read_n - written_m

    click.echo(f"Done. read={read_n}, written={written_m}", err=False)
    if skipped:
        click.echo(
            f"Warning: {skipped} row(s) failed to write — check logs for details.",
            err=True,
        )

    _log.info(
        "storage.migrate.plans.complete",
        db=str(resolved_db),
        read=read_n,
        written=written_m,
    )


@migrate_group.command(name="telemetry")
@click.option(
    "--db",
    "db_path",
    default=None,
    type=click.Path(exists=True, dir_okay=False, readable=True, path_type=Path),
    help=(
        "Path to the SQLite T2 database file. "
        "Defaults to NX_DB_PATH env var or ~/.config/nexus/t2.db."
    ),
)
@click.option(
    "--service-url",
    "service_url",
    default=None,
    help=(
        "Base URL of the nexus-service (e.g. http://127.0.0.1:8080). "
        "Defaults to NX_SERVICE_HOST + NX_SERVICE_PORT env vars."
    ),
)
@click.option(
    "--dry-run",
    is_flag=True,
    default=False,
    help="Count rows in all six source tables without writing. No service connection is made.",
)
def migrate_telemetry_cmd(
    db_path: Path | None,
    service_url: str | None,
    dry_run: bool,
) -> None:
    """Migrate the SQLite telemetry stores to Postgres via the nexus-service.

    Reads all rows from the six telemetry tables (relevance_log,
    search_telemetry, tier_writes, nx_answer_runs, hook_failures, frecency)
    and writes them through the service HTTP API. The ETL is idempotent:

    - Event logs (relevance_log, tier_writes, nx_answer_runs, hook_failures):
      DO NOTHING on conflict — historical events are never overwritten.
    - search_telemetry: DO NOTHING on composite PK conflict.
    - frecency: GREATEST for counters/score/last_hit_at; LEAST for embedded_at.

    FIDELITY-PRESERVING: all six tables use POST /v1/telemetry/import which
    writes timestamp columns VERBATIM from the source row. The SQLite source
    is NEVER modified (copy-not-move).

    Requires NX_SERVICE_PORT and NX_SERVICE_TOKEN to be set (or --service-url
    for the URL component; token is always read from NX_SERVICE_TOKEN).

    Examples::

        # Auto-detect DB, service from env:
        nx storage migrate telemetry

        # Explicit paths:
        nx storage migrate telemetry --db ~/.config/nexus/t2.db --service-url http://127.0.0.1:8080

        # Dry run (count only, no writes):
        nx storage migrate telemetry --dry-run
    """
    resolved_db = _resolve_db_path(db_path)
    if not resolved_db.exists():
        raise click.ClickException(
            f"SQLite database not found: {resolved_db}\n"
            "Set NX_DB_PATH or pass --db."
        )

    if dry_run:
        from nexus.db.t2.telemetry_etl import count_source_rows

        try:
            counts = count_source_rows(resolved_db)
        except RuntimeError as exc:
            raise click.ClickException(str(exc))
        total = sum(counts.values())
        click.echo(f"Dry run: source has {total} telemetry rows across 6 tables:")
        for table, n in counts.items():
            click.echo(f"  {table}: {n}")
        click.echo("(no writes performed)")
        return

    from nexus.db.t2.http_telemetry_store import HttpTelemetryStore

    token = os.environ.get("NX_SERVICE_TOKEN", "")
    if not token:
        raise click.ClickException(
            "NX_SERVICE_TOKEN is required for storage migrate telemetry.\n"
            "Set it to the bearer token configured in the nexus-service."
        )

    try:
        if service_url:
            store = HttpTelemetryStore(base_url=service_url, _token=token)
        else:
            store = HttpTelemetryStore()
    except RuntimeError as exc:
        raise click.ClickException(str(exc))

    from nexus.db.t2.telemetry_etl import migrate_telemetry_rows

    click.echo(f"Migrating telemetry stores from {resolved_db} ...")
    try:
        results = migrate_telemetry_rows(resolved_db, store)
    except Exception as exc:
        raise click.ClickException(f"ETL failed: {exc}")
    finally:
        store.close()

    total_read    = sum(v["read"]    for v in results.values())
    total_written = sum(v["written"] for v in results.values())
    skipped       = total_read - total_written

    click.echo(f"Done. total_read={total_read}, total_written={total_written}")
    for table, v in results.items():
        if v["read"] > 0:
            click.echo(f"  {table}: read={v['read']}, written={v['written']}")
    if skipped:
        click.echo(
            f"Warning: {skipped} row(s) failed to write — check logs for details.",
            err=True,
        )

    _log.info(
        "storage.migrate.telemetry.complete",
        db=str(resolved_db),
        total_read=total_read,
        total_written=total_written,
        by_table=results,
    )


@migrate_group.command(name="taxonomy")
@click.option(
    "--db",
    "db_path",
    default=None,
    type=click.Path(exists=True, dir_okay=False, readable=True, path_type=Path),
    help=(
        "Path to the SQLite T2 database file. "
        "Defaults to NX_DB_PATH env var or ~/.config/nexus/t2.db."
    ),
)
@click.option(
    "--service-url",
    "service_url",
    default=None,
    help=(
        "Base URL of the nexus-service (e.g. http://127.0.0.1:8080). "
        "Defaults to NX_SERVICE_HOST + NX_SERVICE_PORT env vars."
    ),
)
@click.option(
    "--dry-run",
    is_flag=True,
    default=False,
    help="Count rows in all four source tables without writing. No service connection is made.",
)
def migrate_taxonomy_cmd(
    db_path: Path | None,
    service_url: str | None,
    dry_run: bool,
) -> None:
    """Migrate the SQLite taxonomy tables to Postgres via the nexus-service.

    Reads all rows from the four taxonomy tables (topics, topic_assignments,
    topic_links, taxonomy_meta) and writes them through the service HTTP API.
    The ETL is idempotent:

    - topics: GREATEST for doc_count; existing label/created_at preserved;
      review_status/centroid_hash/terms always updated from source.
    - topic_assignments: GREATEST for similarity; other columns from source.
    - topic_links: GREATEST for link_count.
    - taxonomy_meta: GREATEST for last_discover_doc_count; last_discover_at
      updated from source.

    FIDELITY-PRESERVING: topics migration writes the original SQLite row id
    so topic_id references in assignments/links remain consistent. The SQLite
    source is NEVER modified (copy-not-move).

    CHROMA BOUNDARY: only the four relational tables are migrated. The
    ``taxonomy__centroids`` ChromaDB collection is NOT touched by this command.

    Requires NX_SERVICE_PORT and NX_SERVICE_TOKEN to be set (or --service-url
    for the URL component; token is always read from NX_SERVICE_TOKEN).

    Examples::

        # Auto-detect DB, service from env:
        nx storage migrate taxonomy

        # Explicit paths:
        nx storage migrate taxonomy --db ~/.config/nexus/t2.db --service-url http://127.0.0.1:8080

        # Dry run (count only, no writes):
        nx storage migrate taxonomy --dry-run
    """
    resolved_db = _resolve_db_path(db_path)
    if not resolved_db.exists():
        raise click.ClickException(
            f"SQLite database not found: {resolved_db}\n"
            "Set NX_DB_PATH or pass --db."
        )

    if dry_run:
        from nexus.db.t2.taxonomy_etl import count_source_rows

        try:
            counts = count_source_rows(resolved_db)
        except RuntimeError as exc:
            raise click.ClickException(str(exc))
        total = sum(counts.values())
        click.echo(f"Dry run: source has {total} taxonomy rows across 4 tables:")
        for table, n in counts.items():
            click.echo(f"  {table}: {n}")
        click.echo("(no writes performed)")
        return

    from nexus.db.t2.http_taxonomy_store import HttpTaxonomyStore

    token = os.environ.get("NX_SERVICE_TOKEN", "")
    if not token:
        raise click.ClickException(
            "NX_SERVICE_TOKEN is required for storage migrate taxonomy.\n"
            "Set it to the bearer token configured in the nexus-service."
        )

    try:
        if service_url:
            store = HttpTaxonomyStore(base_url=service_url, _token=token)
        else:
            store = HttpTaxonomyStore()
    except RuntimeError as exc:
        raise click.ClickException(str(exc))

    from nexus.db.t2.taxonomy_etl import migrate_taxonomy_rows

    click.echo(f"Migrating taxonomy stores from {resolved_db} ...")
    try:
        results = migrate_taxonomy_rows(resolved_db, store)
    except Exception as exc:
        raise click.ClickException(f"ETL failed: {exc}")
    finally:
        store.close()

    total_read    = sum(v["read"]    for v in results.values())
    total_written = sum(v["written"] for v in results.values())
    skipped       = total_read - total_written

    click.echo(f"Done. total_read={total_read}, total_written={total_written}")
    for table, v in results.items():
        if v["read"] > 0:
            click.echo(f"  {table}: read={v['read']}, written={v['written']}")
    if skipped:
        click.echo(
            f"Warning: {skipped} row(s) failed to write — check logs for details.",
            err=True,
        )

    _log.info(
        "storage.migrate.taxonomy.complete",
        db=str(resolved_db),
        total_read=total_read,
        total_written=total_written,
        by_table=results,
    )


@migrate_group.command(name="chash")
@click.option(
    "--db",
    "db_path",
    default=None,
    type=click.Path(exists=True, dir_okay=False, readable=True, path_type=Path),
    help=(
        "Path to the SQLite T2 database file. "
        "Defaults to NX_DB_PATH env var or ~/.config/nexus/t2.db."
    ),
)
@click.option(
    "--service-url",
    "service_url",
    default=None,
    help=(
        "Base URL of the nexus-service (e.g. http://127.0.0.1:8080). "
        "Defaults to NX_SERVICE_HOST + NX_SERVICE_PORT env vars."
    ),
)
@click.option(
    "--dry-run",
    is_flag=True,
    default=False,
    help="Count rows in the source without writing. No service connection is made.",
)
def migrate_chash_cmd(
    db_path: Path | None,
    service_url: str | None,
    dry_run: bool,
) -> None:
    """Migrate the SQLite chash_index store to Postgres via the nexus-service.

    Reads all rows from the SQLite ``chash_index`` table and writes them through
    the service HTTP API (``POST /v1/chash/import``).  The ETL is idempotent:
    running it multiple times produces no duplicates (server-side upsert on
    ``(tenant_id, chash, physical_collection)``).  The SQLite source is NEVER
    modified.

    Chash entries are content-addressed and immutable; ``created_at`` is
    preserved verbatim.

    Requires NX_SERVICE_PORT and NX_SERVICE_TOKEN to be set (or --service-url
    for the URL component; token is always read from NX_SERVICE_TOKEN).

    Examples::

        # Auto-detect DB, service from env:
        nx storage migrate chash

        # Explicit paths:
        nx storage migrate chash --db ~/.config/nexus/t2.db --service-url http://127.0.0.1:8080

        # Dry run (count only, no writes):
        nx storage migrate chash --dry-run
    """
    import sqlite3

    resolved_db = _resolve_db_path(db_path)
    if not resolved_db.exists():
        raise click.ClickException(
            f"SQLite database not found: {resolved_db}\n"
            "Use --db to specify the path, or set NX_DB_PATH."
        )

    # Count source rows for dry-run or progress display
    try:
        conn = sqlite3.connect(str(resolved_db), check_same_thread=False)
        try:
            row = conn.execute("SELECT COUNT(*) FROM chash_index").fetchone()
            source_count = int(row[0]) if row else 0
        except Exception:
            source_count = 0
        finally:
            conn.close()
    except Exception as exc:
        raise click.ClickException(f"Cannot open SQLite db: {exc}")

    click.echo(f"Source: {resolved_db} ({source_count} row(s) in chash_index)")

    if dry_run:
        click.echo("[dry-run] No writes performed.")
        return

    token = os.environ.get("NX_SERVICE_TOKEN", "")
    if not token:
        raise click.ClickException(
            "NX_SERVICE_TOKEN is required for storage migrate chash.\n"
            "Set it to the bearer token configured in the nexus-service."
        )

    from nexus.db.t2.http_chash_index import HttpChashIndex

    if service_url:
        store = HttpChashIndex(base_url=service_url)
    else:
        store = HttpChashIndex()

    from nexus.db.t2.chash_etl import migrate_chash_rows

    try:
        results = migrate_chash_rows(resolved_db, store)
    except Exception as exc:
        raise click.ClickException(f"ETL failed: {exc}")
    finally:
        store.close()

    total    = results["total"]
    imported = results["imported"]
    errors   = results.get("errors", 0)

    click.echo(f"Done. total={total}, imported={imported}")
    if errors:
        click.echo(f"Warning: {errors} row(s) failed to write — check logs.", err=True)

    _log.info(
        "storage.migrate.chash.complete",
        db=str(resolved_db),
        total=total,
        imported=imported,
        errors=errors,
    )


def _resolve_db_path(explicit: Path | None) -> Path:
    """Resolve the SQLite T2 database path.

    Priority:
    1. Explicit ``--db PATH`` argument.
    2. ``NX_DB_PATH`` environment variable.
    3. :func:`nexus.config.default_db_path` (canonical default — typically
       ``~/.config/nexus/memory.db`` on a standard install).
    """
    if explicit is not None:
        return explicit
    env_path = os.environ.get("NX_DB_PATH", "")
    if env_path:
        return Path(env_path)
    from nexus.config import default_db_path
    return default_db_path()
