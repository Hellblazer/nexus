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
    bootstrap_version,
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


@click.command("upgrade")
@click.option("--dry-run", is_flag=True, help="List pending migrations without executing (creates base tables if absent).")
@click.option("--force", is_flag=True, help="Reset version gate and re-run all migrations.")
@click.option("--auto", "auto_mode", is_flag=True, help="Quiet mode for hook invocation (T2 only, exit 0 always).")
@click.option("--skip-t3", is_flag=True, help="Skip T3 upgrade steps (e.g., cross-collection projection backfill). Useful for fast T2-only migrations.")
def upgrade(dry_run: bool, force: bool, auto_mode: bool, skip_t3: bool) -> None:
    """Run pending database migrations and upgrade steps."""
    try:
        _run_upgrade(dry_run=dry_run, force=force, auto_mode=auto_mode, skip_t3=skip_t3)
    except Exception:
        if auto_mode:
            _log.warning("upgrade_auto_error", exc_info=True)
            return
        raise


def _run_upgrade(*, dry_run: bool, force: bool, auto_mode: bool, skip_t3: bool = False) -> None:
    from pathlib import Path

    db_path = _db_path()
    current = _current_version()
    current_t = _parse_version(current)

    if current_t == (0, 0, 0) and dry_run:
        click.echo(
            "Cannot determine pending migrations — CLI version is "
            "unresolvable (pre-release or dev install). Run 'nx upgrade' directly."
        )
        return

    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db_path), check_same_thread=False)
    conn.execute("PRAGMA busy_timeout=5000")
    conn.execute("PRAGMA journal_mode=WAL")

    try:
        # CLI review: in --dry-run we must never write. ``bootstrap_version``
        # creates base tables and seeds ``_nexus_version`` when absent,
        # which is a legitimate write. Peek at the version row directly
        # when dry-run is set and treat a missing row as the pre-bootstrap
        # seed value (``PRE_REGISTRY_VERSION`` for an existing schema,
        # ``0.0.0`` for a fresh DB).
        if dry_run:
            try:
                row = conn.execute(
                    "SELECT value FROM _nexus_version WHERE key='cli_version'"
                ).fetchone()
                last_seen = row[0] if row else "0.0.0"
            except sqlite3.OperationalError:
                # _nexus_version doesn't exist yet; fresh install.
                last_seen = "0.0.0"
        else:
            # Read last-seen version (bootstrap_version handles base tables,
            # version table creation, and existing-vs-fresh detection).
            last_seen = bootstrap_version(conn)
        last_seen_t = _parse_version(last_seen)

        if force:
            last_seen = "0.0.0"
            last_seen_t = (0, 0, 0)

        # Compute pending T2 migrations
        pending_t2 = [
            m
            for m in MIGRATIONS
            if _parse_version(m.introduced) > last_seen_t
            and _parse_version(m.introduced) <= current_t
        ]

        # Compute pending T3 steps (skip in auto mode or when --skip-t3)
        pending_t3 = []
        if not auto_mode and not skip_t3:
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
                click.echo(f"  T3: [{s.introduced}] {s.name} (heavy — skip with --skip-t3)")
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

        # T3 steps (skipped in auto mode — require ChromaDB, may exceed hook timeout)
        #
        # CLI review: track T3 step completion in a dedicated
        # ``_nexus_t3_steps`` table so a failed step is retried on the
        # next ``nx upgrade`` run. The previous code caught the exception
        # with a warning — but because ``apply_pending`` had already
        # advanced ``_nexus_version.cli_version`` to ``current``, the
        # next run computed ``pending_t3 = []`` and never re-tried.
        if not auto_mode and pending_t3:
            from nexus.commands._helpers import default_db_path
            from nexus.db import make_t3
            from nexus.db.t2 import T2Database

            conn.execute(
                "CREATE TABLE IF NOT EXISTS _nexus_t3_steps ("
                "    introduced TEXT NOT NULL, "
                "    name       TEXT NOT NULL, "
                "    applied_at TEXT NOT NULL, "
                "    PRIMARY KEY (introduced, name)"
                ")"
            )
            done_rows = conn.execute(
                "SELECT introduced, name FROM _nexus_t3_steps"
            ).fetchall()
            done_set = {(r[0], r[1]) for r in done_rows}
            unapplied = [s for s in pending_t3 if (s.introduced, s.name) not in done_set]
            if not unapplied:
                click.echo("All T3 upgrade steps already applied.")
            else:
                t2_db: T2Database | None = None
                applied = 0
                any_failed = False
                try:
                    t3_db = make_t3()
                    t2_db = T2Database(default_db_path())
                    for step in unapplied:
                        click.echo(f"  T3: [{step.introduced}] {step.name}")
                        try:
                            step.fn(t3_db, t2_db.taxonomy)
                        except Exception as step_exc:
                            any_failed = True
                            _log.warning(
                                "t3_upgrade_step_failed",
                                introduced=step.introduced,
                                name=step.name,
                                error=str(step_exc),
                                exc_info=True,
                            )
                            click.echo(
                                f"    FAILED: {step_exc} — will retry on next `nx upgrade`",
                                err=True,
                            )
                            continue
                        conn.execute(
                            "INSERT OR REPLACE INTO _nexus_t3_steps "
                            "(introduced, name, applied_at) VALUES (?, ?, datetime('now'))",
                            (step.introduced, step.name),
                        )
                        conn.commit()
                        applied += 1
                finally:
                    if t2_db is not None:
                        t2_db.close()
                if applied:
                    click.echo(f"Applied {applied}/{len(unapplied)} T3 upgrade step(s).")
                if any_failed:
                    # Non-zero exit so CI / orchestrators see the failure.
                    raise click.exceptions.Exit(1)

    finally:
        conn.close()
