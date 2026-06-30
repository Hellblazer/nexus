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
    from nexus.commands._helpers import default_db_path  # noqa: PLC0415 — deferred to avoid import cycle / CLI startup cost

    return default_db_path()


def _current_version() -> str:
    # RDR-170: the upgrade target is the canonical (registry-aware) schema
    # version — max(package, registry_max) — NOT the raw package version. If
    # this returned the frozen package version (5.10.6) while the registry
    # carries an ahead-of-release step (5.10.7), apply_pending would RUN that
    # step but stamp 5.10.6, leaving it perpetually "pending" to doctor and the
    # next upgrade. expected_t2_schema_version() keeps the stamp == what ran.
    from nexus.db.migrations import expected_t2_schema_version  # noqa: PLC0415 — branch-local; avoids import cost on cold CLI start

    return expected_t2_schema_version()


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
            _cycle_supervised_daemons_to_current(skip_t3=skip_t3)


def _quiesce_daemon() -> None:
    """RDR-128 P2: stop the running T2 daemon and WAIT for it to exit, so it
    has released its eight T2 connections before ``nx upgrade`` migrates.

    ``nx daemon t2 stop`` only *sends* SIGTERM; the daemon then drains and
    closes connections asynchronously, so we poll the recorded pid until it
    is gone (bounded) to close the signal-to-shutdown race. Best-effort:
    never raises — the migration flock + busy_timeout still protect
    correctness if a straggler connection lingers.
    """
    import json  # noqa: PLC0415 — stdlib import kept branch-local
    import os  # noqa: PLC0415 — stdlib import kept branch-local
    import subprocess  # noqa: PLC0415 — stdlib import kept branch-local
    import time as _time  # noqa: PLC0415 — stdlib import kept branch-local

    try:
        from nexus.commands.daemon import _resolve_nx_bin  # noqa: PLC0415 — deferred to avoid import cycle / CLI startup cost
        from nexus.config import nexus_config_dir  # noqa: PLC0415 — deferred to avoid import cycle / CLI startup cost
        from nexus.daemon.t2_daemon import t2_discovery_path  # noqa: PLC0415 — deferred to avoid import cycle / CLI startup cost

        from nexus.commands.daemon import _discovery_record_pid  # noqa: PLC0415 — deferred to avoid import cycle / CLI startup cost

        disc = t2_discovery_path(nexus_config_dir())
        pid: int | None = None
        if disc.exists():
            try:
                # RDR-149 P2: the lease record carries the owner pid under
                # ``endpoint``; the helper reads both shapes so the
                # post-stop exit-wait below still fires.
                pid = _discovery_record_pid(json.loads(disc.read_text()))
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
    except Exception as exc:  # noqa: BLE001 — best-effort daemon quiesce; failure logged via _log.warning and upgrade continues
        _log.warning("upgrade_daemon_quiesce_failed", error=str(exc))


def _cycle_daemon_to_current() -> None:
    """Bring a stale T2 daemon to the just-installed version (best-effort).

    Shells out to ``nx daemon t2 ensure-running --quiet``, the same
    version-aware primitive the plugin/mcpb session-start hooks use. Never
    raises: a daemon nudge must not fail the upgrade.
    """
    import subprocess  # noqa: PLC0415 — stdlib import kept branch-local

    try:
        from nexus.commands.daemon import _resolve_nx_bin  # noqa: PLC0415 — deferred to avoid import cycle / CLI startup cost

        subprocess.run(
            [*_resolve_nx_bin(), "daemon", "t2", "ensure-running", "--quiet"],
            timeout=30,
            check=False,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
    except Exception as exc:  # noqa: BLE001 — best-effort daemon cycle; failure logged via _log.warning and upgrade continues
        _log.warning("upgrade_daemon_cycle_failed", error=str(exc))


def _cycle_t3_daemon_to_current() -> None:
    """Bring a stale supervised T3 daemon to the just-installed version
    (best-effort). RDR-149 P3 (#1112): the supervised T3 daemon froze its
    Python bytecode at start; only a process RESTART (not an in-process
    chroma respawn) refreshes it, so this stops then starts the supervisor.

    Only acts on a running daemon (no auto-spawn during upgrade). Local
    mode only; a no-op when no T3 daemon is running or in cloud mode.
    Never raises.
    """
    import subprocess  # noqa: PLC0415 — stdlib import kept branch-local

    try:
        from nexus.config import is_local_mode  # noqa: PLC0415 — deferred to avoid import cycle / CLI startup cost
        from nexus.daemon.discovery import find_t3_daemon  # noqa: PLC0415 — deferred to avoid import cycle / CLI startup cost

        if not is_local_mode() or find_t3_daemon() is None:
            return  # nothing running to cycle (or cloud mode)

        from nexus.commands.daemon import _resolve_nx_bin  # noqa: PLC0415 — deferred to avoid import cycle / CLI startup cost

        nx = _resolve_nx_bin()
        for verb in ("stop", "start"):
            subprocess.run(
                [*nx, "daemon", "t3", verb],
                timeout=30,
                check=False,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
    except Exception as exc:  # noqa: BLE001 — best-effort T3 daemon cycle; failure logged via _log.warning and upgrade continues
        _log.warning("upgrade_t3_daemon_cycle_failed", error=str(exc))


def _cycle_storage_service_to_current(
    *,
    _discover_fn=None,
    _run_fn=None,
    _nx_bin_fn=None,
) -> None:
    """Bring a stale supervised storage service to the just-installed version
    (best-effort). RDR-149 P5.1 (nexus-gmiaf.30): symmetric to
    ``_cycle_t3_daemon_to_current`` for the Java storage-service + Postgres.

    The supervisor starts the Java JAR with the nexus Python code from the
    same Python install, so a nexus upgrade requires a supervisor restart to
    pick up the new StorageServiceSupervisor bytecode. Only acts on a running
    service (no auto-spawn during upgrade). Never raises.

    Keyword-only ``_discover_fn``, ``_run_fn``, ``_nx_bin_fn`` are injectable
    seams for unit tests (avoids patching local imports deep in try blocks).
    Default values reproduce production behaviour exactly.
    """
    import os  # noqa: PLC0415 — stdlib import kept branch-local
    import subprocess  # noqa: PLC0415 — stdlib import kept branch-local

    try:
        from nexus.config import nexus_config_dir  # noqa: PLC0415 — deferred to avoid import cycle / CLI startup cost
        from nexus.daemon.service_registry import ServiceRegistry  # noqa: PLC0415 — deferred to avoid import cycle / CLI startup cost

        # Discover via the storage_service tier (matches what the supervisor
        # publishes and health._resolve_service_endpoint reads).
        if _discover_fn is None:
            registry = ServiceRegistry(dir=nexus_config_dir(), tier="storage_service")
            scope = str(os.getuid())
            live = registry.discover(scope)
        else:
            live = _discover_fn()

        if live is None:
            return  # nothing running to cycle

        from nexus.commands.daemon import _resolve_nx_bin as _real_nx_bin  # noqa: PLC0415 — deferred to avoid import cycle / CLI startup cost
        nx = _nx_bin_fn() if _nx_bin_fn is not None else _real_nx_bin()
        _run = _run_fn if _run_fn is not None else subprocess.run
        for verb in ("stop", "start"):
            _run(
                [*nx, "daemon", "service", verb],
                timeout=60,  # service start waits for PG + JVM
                check=False,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
    except Exception as exc:  # noqa: BLE001 — best-effort storage-service cycle; failure logged via _log.warning and upgrade continues
        _log.warning("upgrade_storage_service_cycle_failed", error=str(exc))


def _cycle_supervised_daemons_to_current(*, skip_t3: bool = False) -> None:
    """Cycle every supervised storage daemon to the just-installed version.

    RDR-149 P3 (#1112 root cause): the bug class arose because version-skew
    cycling was scattered per-tier and one tier (T3) was forgotten. This is
    now the SINGLE place an upgrade refreshes supervised daemons, so a new
    supervised tier is added here once rather than risk being missed.

    The §Decision's "supervisor-owned cycle" is realised as the
    ``ServiceSupervisor.cycle_to_current`` primitive (P1), but a long-lived
    Python daemon cannot refresh its own bytecode in-process — code refresh
    requires a process restart, which only a separate upgrade process can
    perform. So the per-tier idioms differ (T2 uses version-aware
    ``ensure-running``; T3 has no ensure-running yet, so stop+start), but
    they are invoked from this one orchestrator. Best-effort; never raises.
    """
    _cycle_daemon_to_current()  # T2
    if not skip_t3:
        _cycle_t3_daemon_to_current()  # T3
        _cycle_storage_service_to_current()  # Java storage service + Postgres (P5.1)


def _run_upgrade(*, dry_run: bool, force: bool, auto_mode: bool, skip_t3: bool = False) -> None:
    from pathlib import Path  # noqa: PLC0415 — stdlib import kept branch-local

    # RDR-176 Phase 1 (Gap 2, non-mutation): in service mode the local SQLite
    # T2 (and legacy Chroma T3) are a frozen migration SOURCE — the Java service
    # owns its own Postgres schema. Opening the ``.db`` read-write here (PRAGMA
    # journal_mode=WAL is a header write; bootstrap_version/apply_pending stamp
    # ``_nexus_version``) would mutate the source and break the downgrade
    # guarantee. There is no local schema to migrate, so no-op.
    from nexus.db.storage_mode import (  # noqa: PLC0415 — deferred import — keep CLI startup cheap
        StorageBackend,
        storage_backend_for,
    )

    if storage_backend_for("memory") == StorageBackend.SERVICE:
        if not auto_mode:
            click.echo(
                "Service mode: the local SQLite/Chroma tiers are an immutable "
                "migration source — no local schema migration to run."
            )
        return

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

        # Compute pending T3 steps (skip in auto mode or when --skip-t3)
        pending_t3 = []
        if not auto_mode and not skip_t3:
            pending_t3 = [
                s
                for s in T3_UPGRADES
                if _parse_version(s.introduced) > last_seen_t
            ]

        if dry_run:
            # RDR-142 P2.1: report T2 pending work from the read-only
            # step-resolver, NOT a bare version-range filter. The resolver runs
            # each eligible step's precondition (no DDL / no writes) and reports
            # would-succeed / would-defer (MigrationRetry) / would-gate
            # (MigrationError) WITH remediation — so --dry-run can no longer say
            # "no pending" while a deferred/gated step would block the next start
            # (the RDR-142 reporting-lie). This subsumes and replaces the GH-1061
            # E2 `_check_deferred_migrations` stopgap (deleted), covering all 7
            # defer/gate sites including the undrained-queue and drop_source_path
            # conditions the stopgap missed.
            from nexus.db.migrations import (  # noqa: PLC0415 — branch-local; avoids import cost on the non-dry-run path
                StepOutcome,
                resolve_blocking_steps,
            )

            steps = resolve_blocking_steps(conn, current, last_seen=last_seen)
            if not steps and not pending_t3:
                click.echo(f"Up to date (v{current}). No pending migrations.")
                return

            # Two classes, framed accurately (RDR-142 P2.1 review):
            #  - eligible: introduced > last_seen — WILL run on the next upgrade /
            #    daemon start. A gate here genuinely blocks that run.
            #  - supplementary: the version gate has already passed, so
            #    apply_pending will NOT re-run it. A non-succeed verdict here is an
            #    incomplete TABLE STATE with RUNTIME impact (code expects the new
            #    schema), not a next-start crash — labelled distinctly so an
            #    operator isn't told the daemon will crash when it won't.
            eligible = [s for s in steps if s.eligible]
            supplementary = [s for s in steps if not s.eligible]

            def _emit(s) -> None:
                if s.outcome == StepOutcome.WOULD_GATE and s.informational:
                    tag = "[needs attention — apply_pending will attempt to resolve automatically]"
                elif s.outcome == StepOutcome.WOULD_GATE:
                    tag = "[BLOCKED — would gate on next start]" if s.eligible \
                        else "[TABLE STATE INCOMPLETE — runtime queries will fail; apply_pending will NOT re-run (version gate passed)]"
                elif s.outcome == StepOutcome.WOULD_DEFER:
                    tag = "[deferred — retried on next start]" if s.eligible \
                        else "[deferred — catalog absent; apply_pending will NOT re-run at this version]"
                else:
                    tag = ""
                click.echo(f"  T2: [{s.introduced}] {s.name}  {tag}".rstrip())
                if s.detail:
                    click.echo(f"    Reason:      {s.detail}")
                if s.remediation:
                    click.echo(f"    Remediation: {s.remediation}")

            if eligible or pending_t3:
                click.echo(
                    f"Dry-run: pending migrations (last seen: v{last_seen}, current: v{current}):"
                )
                for s in eligible:
                    _emit(s)
                for s in pending_t3:
                    click.echo(f"  T3: [{s.introduced}] {s.name} (heavy — skip with --skip-t3)")

            if supplementary:
                click.echo(
                    "Table-state checks (version gate already passed; apply_pending will "
                    "not re-run these — they indicate incomplete migration state with runtime impact):"
                )
                for s in supplementary:
                    _emit(s)
            return

        # Eligible T2 steps for the post-apply report (lower-bound only, mirroring
        # apply_pending). Computed BEFORE apply_pending stamps the version.
        pending_t2 = [
            m for m in MIGRATIONS if _parse_version(m.introduced) > last_seen_t
        ]

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
            from nexus.commands._helpers import default_db_path  # noqa: PLC0415 — deferred to avoid import cycle / CLI startup cost
            from nexus.db import make_t3  # noqa: PLC0415 — deferred to avoid import cycle / CLI startup cost
            from nexus.db.t2 import T2Database  # noqa: PLC0415 — deferred to avoid import cycle / CLI startup cost

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
                            except Exception as step_exc:  # noqa: BLE001 — per-step T3 upgrade failure logged + flagged, remaining steps continue
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

        # Refresh nexus-managed git hooks across every registered repo so a
        # stanza change (e.g. a new pgrep guard) lands everywhere in one
        # upgrade instead of a per-repo `nx hooks update`. Best-effort,
        # non-auto, non-dry-run; only touches already-managed hooks.
        if not auto_mode and not dry_run:
            _refresh_all_git_hooks()

    finally:
        conn.close()


def _refresh_all_git_hooks() -> None:
    """Refresh nexus-managed git hooks across all registered repos.

    Best-effort: never raises — a hook-refresh failure must not fail the
    upgrade. Silent when no managed hooks exist anywhere.
    """
    try:
        from nexus.commands.hooks import refresh_all_managed_hooks  # noqa: PLC0415 — deferred to avoid import cycle / CLI startup cost

        summary = refresh_all_managed_hooks(echo=False)
        if summary["refreshed"]:
            click.echo(
                f"\nRefreshed {summary['refreshed']} git hook(s) across "
                f"{summary['repos']} repo(s)"
                + (
                    f"; {summary['errors']} repo(s) skipped "
                    "(see `nx hooks update-all`)."
                    if summary["errors"]
                    else "."
                )
            )
    except Exception as exc:  # noqa: BLE001 — best-effort git-hook refresh; failure logged via _log.warning and upgrade continues
        _log.warning("upgrade_git_hook_refresh_failed", error=str(exc))


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
    from pathlib import Path  # noqa: PLC0415 — stdlib import kept branch-local

    from nexus.config import nexus_config_dir  # noqa: PLC0415 — deferred to avoid import cycle / CLI startup cost

    reg_path = nexus_config_dir() / "repos.json"
    if not reg_path.exists():
        return  # idempotent — no-op when already absent

    try:
        from nexus.catalog.factory import make_catalog_reader  # noqa: PLC0415 — deferred to avoid import cycle / CLI startup cost
        from nexus.config import catalog_path  # noqa: PLC0415 — deferred to avoid import cycle / CLI startup cost
        from nexus.repo_identity import _repo_identity  # noqa: PLC0415 — deferred to avoid import cycle / CLI startup cost
        from nexus.repos import _read_repos_json, _repos_json_is_parseable  # noqa: PLC0415 — deferred to avoid import cycle / CLI startup cost

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

        cat = make_catalog_reader()
        if cat is None:
            click.echo(
                f"Note: {reg_path} present but catalog not initialised; "
                f"skipping migration (run 'nx catalog setup' first)."
            )
            return

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
    except Exception as exc:  # noqa: BLE001 — best-effort repos.json migration; failure logged at warning
        _log.warning("repos_json_migration_failed", error=str(exc))


def _emit_name_vs_embed_dim_advisory() -> None:
    """Run the name-vs-embed-dim doctor check and emit a one-liner
    if any collections are mislabeled. Silent on PASS, error-tolerant
    (T3 may be unavailable on a freshly-migrated install)."""
    try:
        from nexus.commands.catalog_cmds.doctor import _run_name_vs_embed_dim  # noqa: PLC0415 — deferred to avoid import cycle / CLI startup cost
        report = _run_name_vs_embed_dim()
    except Exception:  # noqa: BLE001 — best-effort doctor advisory; silent return when T3 unavailable
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
