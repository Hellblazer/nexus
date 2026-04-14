# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""``nx upgrade`` — run pending T2 schema migrations and T3 upgrade steps.

RDR-076 (nexus-jda).
"""
from __future__ import annotations

import sqlite3

import click
import structlog

from nexus.db.migrations import (
    MIGRATIONS,
    T3_UPGRADES,
    _parse_version,
    _upgrade_done,
    apply_pending,
)

_log = structlog.get_logger()


def _db_path() -> "Path":  # noqa: F821 — lazy import
    from nexus.commands._helpers import default_db_path

    return default_db_path()


def _current_version() -> str:
    try:
        from importlib.metadata import version as _pkg_version

        return _pkg_version("conexus")
    except Exception:
        return "0.0.0"


def _get_bootstrapped_version(conn: sqlite3.Connection) -> str:
    """Read the stored version after ensuring bootstrap has run.

    Runs the bootstrap step from apply_pending (create _nexus_version,
    detect existing vs fresh install, seed version) so the returned
    version is accurate for pending-migration computation.
    """
    from nexus.db.migrations import (
        PRE_REGISTRY_VERSION,
        _bootstrap_lock,
        _is_existing_install,
    )

    # Detect existing install before creating base tables
    pre_existing = _is_existing_install(conn)

    # Create base tables + version table (idempotent)
    from nexus.db.migrations import _create_base_tables

    _create_base_tables(conn)
    conn.execute(
        "CREATE TABLE IF NOT EXISTS _nexus_version ("
        "    key   TEXT PRIMARY KEY,"
        "    value TEXT NOT NULL"
        ")"
    )
    conn.commit()

    # Bootstrap if needed
    with _bootstrap_lock:
        row = conn.execute(
            "SELECT value FROM _nexus_version WHERE key='cli_version'"
        ).fetchone()
        if row is None:
            seed = PRE_REGISTRY_VERSION if pre_existing else "0.0.0"
            conn.execute(
                "INSERT OR IGNORE INTO _nexus_version (key, value) "
                "VALUES ('cli_version', ?)",
                (seed,),
            )
            conn.commit()

    row = conn.execute(
        "SELECT value FROM _nexus_version WHERE key='cli_version'"
    ).fetchone()
    return row[0] if row else "0.0.0"


@click.command("upgrade")
@click.option("--dry-run", is_flag=True, help="List pending migrations without executing.")
@click.option("--force", is_flag=True, help="Reset version gate and re-run all migrations.")
@click.option("--auto", "auto_mode", is_flag=True, help="Quiet mode for hook invocation (T2 only, exit 0 always).")
def upgrade(dry_run: bool, force: bool, auto_mode: bool) -> None:
    """Run pending database migrations and upgrade steps."""
    try:
        _run_upgrade(dry_run=dry_run, force=force, auto_mode=auto_mode)
    except Exception:
        if auto_mode:
            _log.warning("upgrade_auto_error", exc_info=True)
            return
        raise


def _run_upgrade(*, dry_run: bool, force: bool, auto_mode: bool) -> None:
    from pathlib import Path

    db_path = _db_path()
    current = _current_version()
    current_t = _parse_version(current)

    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db_path), check_same_thread=False)
    conn.execute("PRAGMA busy_timeout=5000")
    conn.execute("PRAGMA journal_mode=WAL")

    try:
        # Read last-seen version (after bootstrap, so it's accurate)
        last_seen = _get_bootstrapped_version(conn)
        last_seen_t = _parse_version(last_seen)

        if force:
            last_seen = "0.0.0"
            last_seen_t = (0, 0, 0)
            # Clear process-level fast path so apply_pending actually runs
            path_key = str(Path(db_path).resolve())
            _upgrade_done.discard(path_key)

        # Compute pending T2 migrations
        pending_t2 = [
            m
            for m in MIGRATIONS
            if _parse_version(m.introduced) > last_seen_t
            and _parse_version(m.introduced) <= current_t
        ]

        # Compute pending T3 steps (skip in auto mode)
        pending_t3 = []
        if not auto_mode:
            pending_t3 = [
                s
                for s in T3_UPGRADES
                if _parse_version(s.introduced) > last_seen_t
                and _parse_version(s.introduced) <= current_t
            ]

        if dry_run:
            if not pending_t2 and not pending_t3:
                click.echo(f"Up to date (v{current}). No pending migrations.")
                return

            click.echo(f"Dry-run: pending migrations (last seen: v{last_seen}, current: v{current}):")
            for m in pending_t2:
                click.echo(f"  T2: [{m.introduced}] {m.name}")
            for s in pending_t3:
                click.echo(f"  T3: [{s.introduced}] {s.name}")
            return

        # Execute T2 migrations
        if force:
            # Reset version gate — apply_pending will re-run from 0.0.0
            conn.execute(
                "INSERT OR REPLACE INTO _nexus_version (key, value) VALUES ('cli_version', '0.0.0')"
            )
            conn.commit()

        # Clear fast path so apply_pending runs
        path_key = str(Path(db_path).resolve())
        _upgrade_done.discard(path_key)

        apply_pending(conn, current)

        if not auto_mode:
            if pending_t2:
                click.echo(f"Applied {len(pending_t2)} T2 migration(s).")
                for m in pending_t2:
                    click.echo(f"  [{m.introduced}] {m.name}")
            else:
                click.echo(f"Up to date (v{current}).")

        # T3 steps (skipped in auto mode)
        # TODO: implement T3 step execution when T3_UPGRADES is populated

    finally:
        conn.close()
