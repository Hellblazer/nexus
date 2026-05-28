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
    t2_migration_flock,
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
    # RDR-128 P2: quiesce the daemon BEFORE migrating so its live T2
    # connections are released — the migration flock serializes the two
    # MIGRATOR processes, but only quiescing frees the daemon's serving
    # connections so the migration DDL has clear access. try/finally brings
    # the daemon back even if the upgrade fails (a failed upgrade must not
    # leave the daemon down; its startup migration is idempotent + flocked).
    try:
        if not dry_run:
            _quiesce_daemon()
        _run_upgrade(dry_run=dry_run, force=force, auto_mode=auto_mode, skip_t3=skip_t3)
    except Exception:
        if auto_mode:
            _log.warning("upgrade_auto_error", exc_info=True)
            return
        raise
    finally:
        # nexus-5ldk1: a running T2 daemon froze its code at start and now
        # predates this upgrade. Bring it to the just-installed version so
        # the upgrade is live rather than pending a manual daemon restart.
        # ensure-running is version-aware: no-op on a current daemon,
        # graceful cycle on a stale one. Best-effort, non-dry-run only.
        if not dry_run:
            _cycle_daemon_to_current()


def _quiesce_daemon() -> None:
    """RDR-128 P2: stop the running T2 daemon and WAIT for it to exit, so it
    has released its eight T2 connections before ``nx upgrade`` migrates.

    ``nx daemon t2 stop`` only *sends* SIGTERM; the daemon then drains and
    closes connections asynchronously, so we poll the recorded pid until it
    is gone (bounded) to close the signal-to-shutdown race. Best-effort:
    never raises — the migration flock + busy_timeout still protect
    correctness if a straggler connection lingers.
    """
    import json
    import os
    import subprocess
    import time as _time

    try:
        from nexus.commands.daemon import _resolve_nx_bin
        from nexus.config import nexus_config_dir
        from nexus.daemon.t2_daemon import t2_discovery_path

        disc = t2_discovery_path(nexus_config_dir())
        pid: int | None = None
        if disc.exists():
            try:
                pid = json.loads(disc.read_text()).get("pid")
            except (OSError, json.JSONDecodeError):
                pid = None

        subprocess.run(
            [*_resolve_nx_bin(), "daemon", "t2", "stop"],
            timeout=30,
            check=False,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )

        # Wait for the daemon process to actually exit (release its conns).
        if isinstance(pid, int) and pid > 0:
            deadline = _time.monotonic() + 10.0
            while _time.monotonic() < deadline:
                try:
                    os.kill(pid, 0)
                except (ProcessLookupError, PermissionError):
                    break  # gone
                _time.sleep(0.1)
    except Exception as exc:  # noqa: BLE001
        _log.warning("upgrade_daemon_quiesce_failed", error=str(exc))


def _cycle_daemon_to_current() -> None:
    """Bring a stale T2 daemon to the just-installed version (best-effort).

    Shells out to ``nx daemon t2 ensure-running --quiet``, the same
    version-aware primitive the plugin/mcpb session-start hooks use. Never
    raises: a daemon nudge must not fail the upgrade.
    """
    import subprocess

    try:
        from nexus.commands.daemon import _resolve_nx_bin

        subprocess.run(
            [*_resolve_nx_bin(), "daemon", "t2", "ensure-running", "--quiet"],
            timeout=30,
            check=False,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
    except Exception as exc:  # noqa: BLE001
        _log.warning("upgrade_daemon_cycle_failed", error=str(exc))


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
    # epsilon-allow: nx upgrade is the chicken-and-egg substrate
    # bootstrap path — schema migration cannot route through the
    # daemon when the daemon's startup requires the schema to be
    # migrated. Operator coordinates by stopping the daemon, running
    # nx upgrade, restarting the daemon.
    conn = sqlite3.connect(str(db_path), check_same_thread=False)  # epsilon-allow: nx upgrade chicken-and-egg substrate bootstrap (cannot route through daemon)
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

        # RDR-128 P2: serialize against the daemon's startup migration via
        # the cross-process flock (the daemon was quiesced above, but a
        # session-start hook could respawn it mid-upgrade; the flock makes
        # that respawn's migration WAIT rather than race the WAL).
        with t2_migration_flock(db_path.parent):
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
                    # RDR-128 P3 (nexus-sbxbe.3): the T3 steps mutate T2
                    # (taxonomy) data and write _nexus_t3_steps. Hold the
                    # same cross-process migration flock that guards
                    # apply_pending above (it was released by the time we
                    # get here) so a session-start-respawned daemon's
                    # startup migration WAITS rather than racing these
                    # writes on the single WAL writer lock.
                    with t2_migration_flock(db_path.parent):
                        t3_db = make_t3()
                        t2_db = T2Database(default_db_path())  # epsilon-allow: nx upgrade is the bootstrap chicken-and-egg (it migrates the schema the daemon serves, so it cannot route through the daemon); daemon quiesced during upgrade, the migration flock serializes a respawned daemon's startup migration cross-process. NOTE: shares the process with the apply_pending migration conn (pre-existing, single-threaded sequential writes, tracked by nexus-izpcb) (RDR-128 P3 documented-irreducible)
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

        # nexus-b03o: post-migration advisory — pre-4.32 local-mode
        # installs wrote 384d MiniLM vectors into collections named for
        # voyage-* (1024d). The forward fix shipped in 4.32.0 (RDR-109
        # Phase 2); existing mislabeled collections persist until the
        # operator runs `nx collection rename`. Surface a one-liner so
        # they know to look. Advisory only — does not fail the upgrade.
        if not auto_mode and not skip_t3:
            _emit_name_vs_embed_dim_advisory()

        # RDR-137 Phase 5.2 (nexus-tts0d.19): one-shot migration of
        # ~/.config/nexus/repos.json into the catalog. Idempotent: no-op
        # when the file is already absent. Safety: refuses to delete on
        # any catalog-vs-registry disagreement (per OQ-7 lock).
        if not auto_mode:
            _migrate_repos_json_to_catalog(dry_run=dry_run)

    finally:
        conn.close()


def _migrate_repos_json_to_catalog(*, dry_run: bool) -> None:
    """RDR-137 Phase 5.2 (nexus-tts0d.19): one-shot migration.

    Reads ``~/.config/nexus/repos.json``, verifies every entry has a
    matching catalog owner with the same ``repo_hash``. On full parity,
    deletes the file. On any disagreement, logs the divergent entries
    and leaves the file in place for operator review.

    OQ-7 lock: the safe-by-default behaviour. Operators who want
    forced cleanup of a stale repos.json copied from another machine
    can run ``nx catalog migrate-repos --force`` once that verb lands;
    until then they delete the file manually after reading the log.
    """
    from pathlib import Path

    from nexus.config import nexus_config_dir

    reg_path = nexus_config_dir() / "repos.json"
    if not reg_path.exists():
        return  # idempotent — no-op when already absent

    try:
        from nexus.catalog.catalog import Catalog
        from nexus.config import catalog_path
        from nexus.repo_identity import _repo_identity
        from nexus.repos import _read_repos_json, _repos_json_is_parseable

        # RDR-137 followup CRITICAL-4 (nexus-43qgm.4): refuse to delete
        # a malformed/truncated repos.json. _read_repos_json returns
        # {} on parse failure (with a warning log); without this
        # pre-validation the parity check would vacuously hold and
        # the file would be silently unlinked, losing recoverable
        # data.
        if not _repos_json_is_parseable(reg_path):
            _log.warning(
                "repos_json_malformed",
                path=str(reg_path),
                hint="file present but unparseable; migration refused to delete",
            )
            click.echo(
                f"\nERROR: {reg_path} is malformed/unparseable; NOT deleting.\n"
                f"Inspect manually with `cat {reg_path}` and either repair the JSON or move it aside, "
                f"then re-run nx upgrade.",
                err=True,
            )
            return

        cat_dir = catalog_path()
        if not (cat_dir / ".catalog.db").exists():
            click.echo(
                f"Note: {reg_path} present but catalog not initialised; "
                f"skipping migration (run 'nx catalog setup' first)."
            )
            return

        cat = Catalog(cat_dir, cat_dir / ".catalog.db")
        disagreements: list[str] = []
        for repo_str in _read_repos_json(reg_path).keys():
            repo = Path(repo_str)
            if not repo.exists():
                continue  # stale registry entry; skip
            _, repo_hash = _repo_identity(repo)
            owner = cat.owner_for_repo(repo_hash)
            if owner is None:
                disagreements.append(
                    f"  {repo_str} (repo_hash {repo_hash}) — no catalog owner"
                )

        if disagreements:
            click.echo(
                f"\nRepos.json migration: {len(disagreements)} entry(ies) "
                f"lack catalog parity. File NOT deleted; entries:"
            )
            for d in disagreements:
                click.echo(d)
            click.echo(
                "\nRe-run 'nx index repo <path>' on each listed path to "
                "register the missing owner, then re-run 'nx upgrade'."
            )
            return

        # Full parity — safe to delete.
        if dry_run:
            click.echo(
                f"\nDry-run: would delete {reg_path} (catalog parity holds)."
            )
            return
        reg_path.unlink()
        click.echo(
            f"\nRepos.json migration: catalog parity confirmed; "
            f"{reg_path} deleted."
        )
    except Exception as exc:
        _log.warning("repos_json_migration_failed", error=str(exc))


def _emit_name_vs_embed_dim_advisory() -> None:
    """Run the name-vs-embed-dim doctor check and emit a one-liner
    if any collections are mislabeled. Silent on PASS, error-tolerant
    (T3 may be unavailable on a freshly-migrated install)."""
    try:
        from nexus.commands.catalog import _run_name_vs_embed_dim
        report = _run_name_vs_embed_dim()
    except Exception:
        return
    if report.get("error"):
        return
    n = len(report.get("mismatches", []))
    if n == 0:
        return
    click.echo(
        f"\nAdvisory: {n} collection(s) appear mislabeled "
        f"(pre-4.32 local-mode data). Run `nx catalog doctor "
        f"--name-vs-embed-dim` for details and remediation."
    )
