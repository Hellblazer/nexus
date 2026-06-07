# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""``nx storage`` — storage migration and ETL commands (RDR-152).

Entry points for migrating T2 SQLite stores to the Postgres service tier.
Phase 1.8 implements memory ETL; later phases (.11-.18) will add plan,
catalog, and taxonomy stores.

Usage::

    nx storage migrate memory [--db PATH] [--service-url URL]

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
