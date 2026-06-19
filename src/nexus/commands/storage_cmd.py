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
    nx storage migrate vectors   [--local-path PATH | --cloud] [--rollback]

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

import contextlib
import os
import sys
from pathlib import Path

import click
import structlog

_log = structlog.get_logger(__name__)


@click.group(name="storage")
def storage_group() -> None:
    """Storage migration and ETL commands (RDR-152)."""


@storage_group.group(name="migration-report")
def migration_report_group() -> None:
    """Inspect RDR-153 migration-report artifacts."""


@migration_report_group.command(name="show")
@click.argument(
    "report_file",
    type=click.Path(path_type=Path),
)
def migration_report_show_cmd(report_file: Path) -> None:
    """Summarize a migration report: gate verdict, by-action rollup
    (severity-descending), and per-issue triage lines (RDR-153 Phase 4).

    The Phase-4 SQLite-deletion gate reads this artifact; the predicate is
    ``summary.total_failed == 0``. Exits non-zero when the gate fails so
    the verdict is scriptable.
    """
    import json as _json

    from nexus.migration.migration_report import ACTION_SEVERITY, load_report

    try:
        report = load_report(report_file)
    except FileNotFoundError:
        raise click.ClickException(f"report not found: {report_file}")
    except ValueError as exc:  # JSONDecodeError subclasses ValueError
        raise click.ClickException(f"unreadable report {report_file}: {exc}")

    schema = str(report.get("schema_version", "?"))
    if schema != "1":
        click.echo(
            f"note: schema_version {schema} is newer than this viewer "
            "understands — best-effort display",
            err=True,
        )

    # NEVER default-to-pass (P4 review CRITICAL): this command is the
    # RDR-152 Phase-4 deletion gate's reading tool — a structurally
    # damaged artifact (missing summary, missing total_failed) must FAIL
    # the gate loudly, not evaluate the predicate against defaults.
    summary = report.get("summary")
    if not isinstance(summary, dict) or "total_failed" not in summary:
        raise click.ClickException(
            f"report has no summary.total_failed — cannot evaluate the gate "
            f"predicate (schema_version="
            f"{report.get('schema_version', '?')}): {report_file}"
        )
    try:
        gate_total_failed = int(summary["total_failed"])
    except (TypeError, ValueError) as exc:
        raise click.ClickException(
            f"report summary.total_failed is not an integer "
            f"({summary['total_failed']!r}): {report_file} — {exc}"
        )
    click.echo(f"migration: {report.get('migration_id', '?')}")
    click.echo(
        f"  {report.get('started_at', '?')} -> {report.get('completed_at', '?')}"
    )
    source = report.get("source", {})
    if source:
        click.echo(f"  source: {source}")
    click.echo(f"verification: {report.get('verification', '(not recorded)')}")
    click.echo(f"max_severity={summary.get('max_severity', '?')}")
    by_action = summary.get("by_action", {})
    ordered_actions = sorted(
        by_action,
        key=lambda a: ACTION_SEVERITY.get(a, -1),
        reverse=True,
    )
    click.echo(
        "  " + " ".join(f"{a}={by_action[a]}" for a in ordered_actions)
    )
    click.echo(
        f"  total_read={summary.get('total_read', '?')} "
        f"total_written={summary.get('total_written', '?')} "
        f"total_failed={summary.get('total_failed', '?')}"
    )

    # Per-issue triage lines, severity-descending (the actionable ones first).
    issues: list[tuple[int, str]] = []
    for store in report.get("stores", []):
        for table in store.get("tables", []):
            for issue in table.get("issues", []):
                sample = (issue.get("sample_ids") or ["-"])[0]
                issues.append((
                    int(issue.get("severity", 0)),
                    f"  [{issue.get('severity', '?')}] "
                    f"{store.get('store', '?')}.{table.get('table', '?')} "
                    f"{issue.get('class', '?')}/{issue.get('action', '?')} "
                    f"count={issue.get('count', '?')} sample={sample} — "
                    f"{issue.get('reason', '')}",
                ))
    if issues:
        click.echo("issues (severity-descending):")
        for _, line in sorted(issues, key=lambda t: t[0], reverse=True):
            click.echo(line)

    if gate_total_failed == 0:
        click.echo("GATE: PASS (total_failed=0)")
    else:
        click.echo(f"GATE: FAIL (total_failed={gate_total_failed})", err=True)
        click.echo(
            "  failed rows are triaged above; after repairing parents, "
            "re-running the ETL is idempotent (ON CONFLICT DO NOTHING).",
            err=True,
        )
        sys.exit(1)


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
@click.option(
    "--report",
    "report_path",
    default=None,
    type=click.Path(dir_okay=False, path_type=Path),
    help="Write a single-store RDR-153 migration report to PATH "
         "(default when omitted: <config>/migration-reports/"
         "migration-<id>.json).",
)
def migrate_memory_cmd(
    db_path: Path | None,
    service_url: str | None,
    dry_run: bool,
    report_path: Path | None,
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

    from nexus.migration.migration_report import IssueCollector

    collector = IssueCollector()
    click.echo(f"Migrating memory store from {resolved_db} ...")
    try:
        result = migrate_memory_rows(resolved_db, store, collector=collector)
    except Exception as exc:
        # Partial data beats no data (P3 critique S2): the report is
        # written even on a mid-run crash, so the operator always has a
        # triage artifact covering everything the run recorded.
        _emit_store_report(collector, resolved_db, report_path)
        raise click.ClickException(f"ETL failed: {exc}")
    finally:
        store.close()

    _emit_store_report(collector, resolved_db, report_path)

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
@click.option(
    "--report",
    "report_path",
    default=None,
    type=click.Path(dir_okay=False, path_type=Path),
    help="Write a single-store RDR-153 migration report to PATH.",
)
def migrate_plans_cmd(
    db_path: Path | None,
    service_url: str | None,
    dry_run: bool,
    report_path: Path | None,
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

    from nexus.migration.migration_report import IssueCollector

    _collector = IssueCollector()
    click.echo(f"Migrating plans store from {resolved_db} ...")
    try:
        result = migrate_plan_rows(resolved_db, store, collector=_collector)
    except Exception as exc:
        # Partial data beats no data (P3 critique S2).
        _emit_store_report(_collector, resolved_db, report_path)
        raise click.ClickException(f"ETL failed: {exc}")
    finally:
        store.close()

    _emit_store_report(_collector, resolved_db, report_path)

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
@click.option(
    "--report",
    "report_path",
    default=None,
    type=click.Path(dir_okay=False, path_type=Path),
    help="Write a single-store RDR-153 migration report to PATH.",
)
def migrate_telemetry_cmd(
    db_path: Path | None,
    service_url: str | None,
    dry_run: bool,
    report_path: Path | None,
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

    from nexus.migration.migration_report import IssueCollector

    _collector = IssueCollector()
    click.echo(f"Migrating telemetry stores from {resolved_db} ...")
    try:
        results = migrate_telemetry_rows(resolved_db, store, collector=_collector)
    except Exception as exc:
        # Partial data beats no data (P3 critique S2).
        _emit_store_report(_collector, resolved_db, report_path)
        raise click.ClickException(f"ETL failed: {exc}")
    finally:
        store.close()

    _emit_store_report(_collector, resolved_db, report_path)

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
@click.option(
    "--report",
    "report_path",
    default=None,
    type=click.Path(dir_okay=False, path_type=Path),
    help="Write a single-store RDR-153 migration report to PATH.",
)
def migrate_taxonomy_cmd(
    db_path: Path | None,
    service_url: str | None,
    dry_run: bool,
    report_path: Path | None,
) -> None:
    """Migrate the SQLite taxonomy tables to Postgres via the nexus-service.

    Reads all rows from the four taxonomy tables (topics, topic_assignments,
    topic_links, taxonomy_meta) and writes them through the service HTTP API.
    The ETL is idempotent:

    - topics: doc_count is NOT merged on conflict (trigger-maintained since
      RDR-154 P0, nexus-i7ivk); existing label/created_at preserved;
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

    NO CATALOG PREREQUISITE: topic_assignments.doc_id is a chunk chash, not a
    document tumbler, and carries NO catalog FK (fk_ta_catalog_doc was never
    registered — nexus-sa14p). Assignments import independently of the catalog.
    (topic_id -> topics(id) IS enforced; assignments referencing a deleted topic
    fail and are reported, per the RDR-153 migration data-quality policy.)

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

    from nexus.migration.migration_report import IssueCollector

    _collector = IssueCollector()
    click.echo(f"Migrating taxonomy stores from {resolved_db} ...")
    try:
        results = migrate_taxonomy_rows(resolved_db, store, collector=_collector)
    except Exception as exc:
        # Partial data beats no data (P3 critique S2).
        _emit_store_report(_collector, resolved_db, report_path)
        raise click.ClickException(f"ETL failed: {exc}")
    finally:
        store.close()

    _emit_store_report(_collector, resolved_db, report_path)

    total_read    = sum(v["read"]    for v in results.values())
    total_written = sum(v["written"] for v in results.values())
    # "skipped" is a generic skip-accounting outcome (no taxonomy import currently
    # skips — fk_ta_catalog_doc was never registered, nexus-sa14p), distinct from a
    # genuine write failure. Retained for forward-compat with the generic ETL loop.
    total_skipped = sum(v.get("skipped", 0) for v in results.values())
    failed        = total_read - total_written - total_skipped

    click.echo(
        f"Done. total_read={total_read}, total_written={total_written}, "
        f"total_skipped={total_skipped}"
    )
    for table, v in results.items():
        if v["read"] > 0:
            line = f"  {table}: read={v['read']}, written={v['written']}"
            if v.get("skipped"):
                line += f", skipped={v['skipped']}"
            click.echo(line)
    if total_skipped:
        click.echo(
            f"Note: {total_skipped} row(s) skipped (see logs for the per-row reason).",
            err=True,
        )
    if failed:
        click.echo(
            f"Warning: {failed} row(s) failed to write — check logs for details.",
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
@click.option(
    "--report",
    "report_path",
    default=None,
    type=click.Path(dir_okay=False, path_type=Path),
    help="Write a single-store RDR-153 migration report to PATH.",
)
def migrate_chash_cmd(
    db_path: Path | None,
    service_url: str | None,
    dry_run: bool,
    report_path: Path | None,
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
        conn = sqlite3.connect(str(resolved_db), check_same_thread=False)  # epsilon-allow: ETL source-read; resolved_db is the migration SOURCE SQLite (never T2Database); read-only count query
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
    from nexus.migration.migration_report import IssueCollector

    _collector = IssueCollector()
    try:
        results = migrate_chash_rows(resolved_db, store, collector=_collector)
    except Exception as exc:
        # Partial data beats no data (P3 critique S2).
        _emit_store_report(_collector, resolved_db, report_path)
        raise click.ClickException(f"ETL failed: {exc}")
    finally:
        store.close()

    _emit_store_report(_collector, resolved_db, report_path)

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


@migrate_group.command(name="catalog")
@click.option(
    "--catalog-db",
    "catalog_db_path",
    default=None,
    type=click.Path(exists=True, dir_okay=False, readable=True, path_type=Path),
    help=(
        "Path to the SQLite catalog DB file (.catalog.db). "
        "Defaults to NX_CATALOG_DB_PATH env var or "
        "~/.config/nexus/catalog/.catalog.db."
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
    help="Count rows in all catalog tables without writing. No service connection is made.",
)
@click.option(
    "--report",
    "report_path",
    default=None,
    type=click.Path(dir_okay=False, path_type=Path),
    help="Write a single-store RDR-153 migration report to PATH.",
)
def migrate_catalog_cmd(
    catalog_db_path: Path | None,
    service_url: str | None,
    dry_run: bool,
    report_path: Path | None,
) -> None:
    """Migrate the SQLite catalog to Postgres via the nexus-service.

    Reads owners, documents, links, collections, document_chunks, and
    _meta from the SQLite catalog DB and writes them through the service
    HTTP import API.  The ETL is idempotent:

    - owners:           ON CONFLICT DO UPDATE (all fields from EXCLUDED)
    - documents:        ON CONFLICT DO UPDATE (GREATEST source_mtime)
    - collections:      ON CONFLICT DO NOTHING
    - document_chunks:  ON CONFLICT DO NOTHING
    - links:            ON CONFLICT DO NOTHING

    Insertion order: owners -> documents -> collections -> document_chunks
    -> links (respects cross-store FK constraints from fk-001-catalog).

    Requires NX_SERVICE_PORT and NX_SERVICE_TOKEN to be set (or --service-url
    for the URL component; token is always read from NX_SERVICE_TOKEN).

    Examples::

        # Auto-detect catalog DB, service from env:
        nx storage migrate catalog

        # Explicit paths:
        nx storage migrate catalog \\
            --catalog-db ~/.config/nexus/catalog/.catalog.db \\
            --service-url http://127.0.0.1:8080

        # Dry run (count only, no writes):
        nx storage migrate catalog --dry-run
    """
    resolved_catalog = _resolve_catalog_db_path(catalog_db_path)
    if not resolved_catalog.exists():
        raise click.ClickException(
            f"SQLite catalog DB not found: {resolved_catalog}\n"
            "Set NX_CATALOG_DB_PATH or pass --catalog-db."
        )

    if dry_run:
        from nexus.db.t2.catalog_etl import count_source_rows

        try:
            counts = count_source_rows(resolved_catalog)
        except RuntimeError as exc:
            raise click.ClickException(str(exc))
        total = sum(counts.values())
        click.echo(f"Dry run: source has {total} catalog rows across {len(counts)} tables:")
        for table, n in counts.items():
            click.echo(f"  {table}: {n}")
        click.echo("(no writes performed)")
        return

    from nexus.catalog.factory import make_catalog_client_for_migration

    token = os.environ.get("NX_SERVICE_TOKEN", "")
    if not token:
        raise click.ClickException(
            "NX_SERVICE_TOKEN is required for storage migrate catalog.\n"
            "Set it to the bearer token configured in the nexus-service."
        )

    try:
        client = make_catalog_client_for_migration(base_url=service_url, token=token)
    except RuntimeError as exc:
        raise click.ClickException(str(exc))

    from nexus.db.t2.catalog_etl import migrate_catalog

    from nexus.migration.migration_report import IssueCollector

    _collector = IssueCollector()
    click.echo(f"Migrating catalog from {resolved_catalog} ...")
    try:
        results = migrate_catalog(resolved_catalog, client, collector=_collector)
    except Exception as exc:
        # Partial data beats no data (P3 critique S2).
        _emit_store_report(_collector, resolved_catalog, report_path)
        raise click.ClickException(f"ETL failed: {exc}")
    finally:
        client.close()

    _emit_store_report(_collector, resolved_catalog, report_path)

    from nexus.db.t2.catalog_etl import IMPORT_TABLE_KEYS

    total_read    = sum(results[k]["read"]    for k in IMPORT_TABLE_KEYS if k in results)
    total_written = sum(results[k]["written"] for k in IMPORT_TABLE_KEYS if k in results)
    skipped       = total_read - total_written

    click.echo(f"Done. total_read={total_read}, total_written={total_written}")
    for table in IMPORT_TABLE_KEYS:
        v = results.get(table)
        if v and v["read"] > 0:
            click.echo(f"  {table}: read={v['read']}, written={v['written']}")

    # Bookkeeping entries reported distinctly so dry-run and live counts reconcile.
    meta = results.get("catalog_meta")
    if meta and meta.get("skipped"):
        click.echo(
            f"  catalog_meta: {meta['skipped']} row(s) intentionally skipped "
            "(SQLite projection markers, not applicable to Postgres)"
        )
    reconcile = results.get("next_seq_reconcile")
    if reconcile and reconcile.get("written"):
        click.echo(
            f"  next_seq: reconciled on {reconcile['written']} owner(s) "
            "(tumbler allocation floored at high-water mark)"
        )
    if reconcile and reconcile.get("failed"):
        click.echo(
            f"ERROR: next_seq reconciliation FAILED on {reconcile['failed']} owner(s) — "
            "those owners may collide on the first new document registration. "
            "Re-run the migration before cutover (the pass is idempotent).",
            err=True,
        )

    if skipped:
        click.echo(
            f"Warning: {skipped} row(s) failed to write — check logs for details.",
            err=True,
        )

    _log.info(
        "storage.migrate.catalog.complete",
        catalog_db=str(resolved_catalog),
        total_read=total_read,
        total_written=total_written,
        by_table=results,
    )


@migrate_group.command(name="vectors")
@click.option(
    "--local-path",
    "local_path",
    default=None,
    type=click.Path(file_okay=False, path_type=Path),
    help=(
        "Path to the on-disk Chroma store for the LOCAL leg. "
        "Defaults to ~/.config/nexus/chroma (the store the retired T3 "
        "daemon served). Ignored with --cloud."
    ),
)
@click.option(
    "--cloud",
    is_flag=True,
    default=False,
    help=(
        "Run the CLOUD leg instead: read via the ChromaCloud REST/auth API "
        "(credentials from nx config chroma_*). Run each leg separately — "
        "an ETL with only one leg is a silent half-migration."
    ),
)
@click.option(
    "--collections",
    "collections_csv",
    default="",
    help="Comma-separated collection subset. Default: every source collection.",
)
@click.option(
    "--service-url",
    "service_url",
    default=None,
    help="Base URL of the nexus-service. Defaults to NX_SERVICE_URL.",
)
@click.option(
    "--dry-run",
    is_flag=True,
    default=False,
    help="Count source chunks per collection without writing.",
)
@click.option(
    "--rollback",
    is_flag=True,
    default=False,
    help=(
        "Undo the copy: delete from pgvector exactly the chashes present "
        "in the source collections. The Chroma source is never modified."
    ),
)
def migrate_vectors_cmd(
    local_path: Path | None,
    cloud: bool,
    collections_csv: str,
    service_url: str | None,
    dry_run: bool,
    rollback: bool,
) -> None:
    """Migrate Chroma vector collections into pgvector (RDR-155 Phase 5).

    COPY-NOT-MOVE: chunk text + chash + metadata transfer verbatim and the
    service re-embeds server-side (vector-identity decision (a), bead
    nexus-unp61); the Chroma source is never modified. Idempotent: re-runs
    upsert onto ``(tenant, collection, chash)``. Collection names are
    preserved VERBATIM so ``topic_assignments.source_collection`` stays
    valid.

    Examples::

        nx storage migrate vectors --dry-run          # local leg, count only
        nx storage migrate vectors                    # local leg
        nx storage migrate vectors --cloud            # ChromaCloud leg
        nx storage migrate vectors --rollback         # undo the local copy
    """
    if dry_run and rollback:
        raise click.ClickException("--dry-run and --rollback are mutually exclusive.")

    # No explicit NX_SERVICE_TOKEN gate (nexus-pebfx.1): the client resolves
    # {url, token} from the supervisor's ServiceRegistry lease, with env as
    # the override. Pre-flight the resolution here so an unresolvable
    # endpoint is a clean early error BEFORE the (potentially long) source
    # read — except for --dry-run, which only counts source chunks and
    # never touches the service at all.
    if service_url:
        os.environ["NX_SERVICE_URL"] = service_url
    if not dry_run:
        from nexus.db.http_vector_client import _resolve_endpoint

        try:
            _resolve_endpoint()
        except RuntimeError as exc:
            raise click.ClickException(str(exc))

    from nexus.db.http_vector_client import HttpVectorClient

    vector_client = HttpVectorClient()
    collections = [c.strip() for c in collections_csv.split(",") if c.strip()] or None

    if local_path is None:
        from nexus.config import nexus_config_dir

        local_path = nexus_config_dir() / "chroma"

    from nexus.migration.vector_etl import (
        migrate_cloud,
        migrate_local,
        rollback_collections,
    )

    if rollback:
        from nexus.migration.chroma_read import (
            open_cloud_read_client,
            open_local_read_client,
        )

        try:
            read_client = (
                open_cloud_read_client() if cloud else open_local_read_client(local_path)
            )
            deleted = rollback_collections(
                read_client, vector_client, collections=collections
            )
        except Exception as exc:
            raise click.ClickException(f"rollback failed: {exc}")
        for name, count in sorted(deleted.items()):
            click.echo(f"rolled-back  {name}: {count} chunk(s) removed from pgvector")
        click.echo(f"Done. {sum(deleted.values())} chunk(s) removed; source untouched.")
        return

    # nexus-pebfx.3: live, FLUSHED per-collection progress. The 2026-06-10
    # production run left a redirected log empty while 35k+ rows landed —
    # stdout is block-buffered off a tty, so flush after every line.
    def _echo_progress(r) -> None:
        line = (
            f"{r.status:<13} {r.collection}: source={r.source_count} "
            f"written={r.written_count} ({r.duration_s:.1f}s)"
        )
        if r.reason:
            line += f" — {r.reason}"
        is_err = r.status in ("failed", "skipped")
        click.echo(line, err=is_err)
        (sys.stderr if is_err else sys.stdout).flush()

    try:
        if cloud:
            report = migrate_cloud(
                vector_client, collections=collections, dry_run=dry_run,
                on_result=_echo_progress,
            )
        else:
            report = migrate_local(
                local_path, vector_client, collections=collections,
                dry_run=dry_run, on_result=_echo_progress,
            )
    except Exception as exc:
        raise click.ClickException(f"ETL failed: {exc}")

    _echo_summary_table(report)
    if not report.ok:
        raise click.ClickException(
            "migration is NOT clean — fix the failed/skipped collections above "
            "and re-run (idempotent)."
        )


def _echo_summary_table(report) -> None:
    """Final per-collection summary so the operator never scrolls structlog
    (nexus-pebfx.3 item 4). Sorted failures-first so the actionable rows
    are adjacent to the verdict line."""
    rank = {
        "failed": 0, "skipped": 1, "skipped-empty": 2, "excluded": 3,
        "dry-run": 4, "migrated": 5,
    }
    rows = sorted(report.results, key=lambda r: (rank.get(r.status, 9), r.collection))
    name_w = max([len(r.collection) for r in rows] + [10])
    click.echo("")
    click.echo(f"{'STATUS':<13} {'COLLECTION':<{name_w}} {'SOURCE':>8} {'WRITTEN':>8} {'TIME':>8}")
    click.echo("-" * (13 + 1 + name_w + 27))
    for r in rows:
        line = (
            f"{r.status:<13} {r.collection:<{name_w}} {r.source_count:>8} "
            f"{r.written_count:>8} {r.duration_s:>7.1f}s"
        )
        # Rows with a reason carry it — the table is the permanent
        # scrollback record and must be sufficient on its own (no structlog
        # scrolling). skipped-empty included: the operator reviewing a
        # redirected log needs the disposition rationale in the table.
        if r.reason and r.status in ("failed", "skipped", "skipped-empty", "excluded"):
            line += f"  — {r.reason}"
        click.echo(line)
    click.echo("-" * (13 + 1 + name_w + 27))
    click.echo(
        f"{'TOTAL':<13} {report.leg + ' leg':<{name_w}} {report.total_source:>8} "
        f"{report.total_written:>8}   ok={report.ok}"
    )
    sys.stdout.flush()
# ── RDR-153 Phase 3: migrate-all orchestration ───────────────────────────────


def _default_report_path(migration_id: str) -> Path:
    """``<config>/migration-reports/migration-<id>.json`` — a run ALWAYS
    produces an artifact, even when the operator forgets ``--report``."""
    from nexus.config import nexus_config_dir

    return nexus_config_dir() / "migration-reports" / f"migration-{migration_id}.json"


def _write_report(report: dict, path: Path) -> None:
    import json as _json

    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(_json.dumps(report, indent=2, sort_keys=True))


#: (store, table) → Postgres relation counted during verification. Only
#: tables with a 1:1 name mapping are verified.
#:
#: Normal check: pg_count >= report_written (the target may carry rows from
#: previous idempotent runs -- equality only holds on a fresh target).
#:
#: Plans convergence (nexus-d583z): plan_etl imports via POST /v1/plans/import →
#: Java PlanRepository.importRow which is ON CONFLICT (tenant_id, project, query)
#: DO UPDATE.  HTTP acks count *source rows processed*; multiple source rows that
#: share the same (tenant, project, query) key converge onto ONE PG row (each ack
#: overwrites the same row).  Landed rows < acks = source-duplicate convergence, by
#: schema design.  Correct check for plans: pg_count > 0 AND pg_count <= written
#: (some rows landed AND we did not land MORE than we sent, which would be truly
#: impossible).  written=0 is a trivial pass for idempotent re-runs.
#: Relations that need the convergence-aware check are listed in
#: :data:`_VERIFY_TABLES_DEDUP`.
def _emit_store_report(
    collector, sqlite_path: Path, report_path: Path | None,
) -> None:
    """Write the single-store RDR-153 report (per-store ``--report``).

    A report is ALWAYS written (default path when the flag is omitted) —
    the run must leave a triage artifact.
    """
    import uuid as _uuid

    from nexus.migration.migration_report import build_report

    migration_id = str(_uuid.uuid4())
    report = build_report(
        collector,
        source={"sqlite": str(sqlite_path)},
        target={"service_url": os.environ.get("NX_SERVICE_URL", "(lease)")},
        migration_id=migration_id,
    )
    out_path = report_path or _default_report_path(migration_id)
    _write_report(report, out_path)
    click.echo(f"report: {out_path}")


@migrate_group.command(name="all")
@click.option(
    "--report",
    "report_path",
    default=None,
    type=click.Path(dir_okay=False, path_type=Path),
    help="Report artifact path (default: <config>/migration-reports/"
         "migration-<id>.json — a run always produces an artifact).",
)
@click.option(
    "--db", "db_path", default=None,
    type=click.Path(exists=True, dir_okay=False, path_type=Path),
    help="SQLite T2 source (default: NX_DB_PATH or the canonical path).",
)
@click.option(
    "--catalog-db", "catalog_db_path", default=None,
    type=click.Path(exists=True, dir_okay=False, path_type=Path),
    help="SQLite catalog source (default: NX_CATALOG_DB_PATH or the "
         "canonical path).",
)
def migrate_all_cmd(
    report_path: Path | None,
    db_path: Path | None,
    catalog_db_path: Path | None,
) -> None:
    """Run ALL eight store migrations in the RDR-152 ladder order and emit
    ONE migration report (RDR-153 Phase 3).

    Order: memory → plans → telemetry → taxonomy → aspects → chash →
    catalog → aspects_queue (the last two trail so FK targets exist). One
    shared IssueCollector spans the run; the report is the triage/recovery
    artifact and the Phase-4 gate input (``summary.total_failed == 0``).
    Post-run count verification is LOUD when it cannot run (nexus-r0esi:
    never SKIP-then-'all passed').
    """
    from nexus.migration.etl_registry import EtlSources
    from nexus.migration.orchestrator import migrate_all

    sources = EtlSources(
        sqlite_path=_resolve_db_path(db_path),
        catalog_db_path=_resolve_catalog_db_path(catalog_db_path),
    )

    def _on_store(store: str) -> None:
        click.echo(f"migrating {store} …")
        sys.stdout.flush()

    def _on_store_failed(store: str, exc: Exception) -> None:
        # Real-time crash signal so an operator watching a multi-minute run
        # sees which store blew up at crash time, not only in the summary.
        click.echo(f"  {store}: CRASHED — {exc}", err=True)
        sys.stderr.flush()

    # RDR-159 P-1a: the orchestration lives in nexus.migration now (the
    # guided upgrade engine + the conexus veneer consume the same callable).
    # Count verification routes through the service REST endpoint, not psql
    # (RDR-152 bars a direct Python PG connection). The verdict is folded
    # INTO the report so the artifact is self-contained for triage.
    report = migrate_all(
        sources, on_store=_on_store, on_store_failed=_on_store_failed,
    )

    _echo_verification(report)

    out_path = report_path or _default_report_path(report["migration_id"])
    _write_report(report, out_path)

    summary = report["summary"]
    click.echo(f"report: {out_path}")
    click.echo(
        f"total_read={summary['total_read']} "
        f"total_written={summary['total_written']} "
        f"total_failed={summary['total_failed']} "
        f"max_severity={summary['max_severity']}"
    )
    if summary["total_failed"] > 0:
        raise click.ClickException(
            f"migration is NOT clean — total_failed={summary['total_failed']}; "
            f"triage with: nx storage migration-report show {out_path}"
        )
    if report["verification"] == "mismatch":
        raise click.ClickException(
            "VERIFICATION MISMATCH — Postgres counts are below the report's "
            "written totals; the report and logs identify the tables."
        )


def _echo_verification(report: dict) -> None:
    """Print the report's verification verdict LOUDLY (nexus-r0esi:
    indeterminate is a WARNING, never a silent pass).

    The verdict was computed by :func:`nexus.migration.orchestrator.migrate_all`
    and is already recorded in ``report["verification"]``; this only renders
    it for the operator.
    """
    verification = report.get("verification", "indeterminate")
    convergence_notes = report.get("verification_convergence_notes", [])
    checked = report.get("relations_checked", 0)
    if verification == "verified":
        if convergence_notes:
            notes_str = "; ".join(convergence_notes)
            click.echo(
                f"verification: verified ({checked} relations checked; "
                f"convergence detected — {notes_str})"
            )
        else:
            click.echo(
                f"verification: verified (pg counts >= report written across the "
                f"{checked} mappable relations; unmapped tables are not checked)"
            )
    elif verification == "mismatch":
        click.echo("verification: VERIFICATION MISMATCH", err=True)
    else:
        click.echo(
            "verification: VERIFICATION INDETERMINATE — count source "
            "unresolved; counts were NOT checked (this is a warning, not a "
            "pass — fix the environment and re-run, the ETL is idempotent)",
            err=True,
        )
    sys.stdout.flush()
    sys.stderr.flush()


def _resolve_catalog_db_path(explicit: Path | None) -> Path:
    """Resolve the SQLite catalog DB path.

    Priority:
    1. Explicit ``--catalog-db PATH`` argument.
    2. ``NX_CATALOG_DB_PATH`` environment variable.
    3. ``~/.config/nexus/catalog/.catalog.db`` (conventional default).
    """
    if explicit is not None:
        return explicit
    env_path = os.environ.get("NX_CATALOG_DB_PATH", "")
    if env_path:
        return Path(env_path)
    from nexus.config import nexus_config_dir
    return nexus_config_dir() / "catalog" / ".catalog.db"


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
