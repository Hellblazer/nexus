# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""``nx upgrade`` — run pending T2 schema migrations and T3 upgrade steps.

RDR-076 (nexus-jda).
"""
from __future__ import annotations

import contextlib

import click
import structlog

_log = structlog.get_logger()


@click.command("upgrade")
@click.option("--dry-run", is_flag=True, help="Report pending upgrade-ladder rungs (read-only detect() walk) without executing.")
# --force REMOVED (RDR-158 P4 Stage 4, review Significant): it reset the
# local-SQLite version gate so apply_pending re-ran every migration — the
# machinery is deleted, the flag read nothing, and dead-but-documented
# flags actively mislead. 7.0.0 is a major; scripts passing --force now get
# Click's no-such-option error, which is the honest outcome.
@click.option("--auto", "auto_mode", is_flag=True, help="Quiet mode for hook invocation (exit 0 always).")
@click.option("--skip-t3", is_flag=True, help="Skip T3 upgrade steps (e.g., cross-collection projection backfill). Useful for fast T2-only migrations.")
@click.option("--yes", "assume_yes", is_flag=True, help="Assume yes to the billed re-embed consent prompt (equivalent to NX_ASSUME_YES=1). Nothing else prompts.")
def upgrade(
    dry_run: bool, auto_mode: bool, skip_t3: bool, assume_yes: bool
) -> None:
    """Run pending database migrations and upgrade steps.

    ``--yes`` is NOT a general "say yes to everything" switch, and must not
    become one: the only prompt the ladder can raise is the billed Voyage
    re-embed (RDR-185 ## Constraints' third genuine decision; source-gone and
    rollback DEFER rather than ask). It exists because `nx upgrade` is the one
    verb and an ancient install must still converge UNATTENDED (SC-1) — without
    a consent channel, making the cost gate actually fire (nexus-k1m2f) traded a
    silent bill for a silent hang on a `click.confirm` no hook can answer.
    """
    # NO pre-migration daemon quiesce: RDR-128 P2 stopped the T2 daemon here so
    # it released its serving connections before the migration DDL ran. The
    # daemon is retired (nexus-i711w Stage 2 sub-stage B), so there are no
    # serving connections to free; the migration flock that serialized the
    # MIGRATOR processes is untouched and still does its half.
    try:
        _run_upgrade(dry_run=dry_run, auto_mode=auto_mode, skip_t3=skip_t3)
        # RDR-185 P3.1 (nexus-n7u38.23): the two non-data axes converge as
        # STATELESS preconditions before the ladder walks — re-derived from
        # on-disk state each invocation (sidecar/lease/metadata), never
        # recorded. --auto keeps the engine install with the version-
        # transition path (hook timeout budget); process freshness stays.
        # (check_version_transition at the ROOT cli group has already run on
        # this invocation — on a version transition it converged the engine
        # inline via the same shared mechanism; this stage's engine step
        # then no-ops. One mechanism, two triggers: see the P3 decision
        # addendum nexus_rdr/185-p3-engine-trigger-duality-decision.)
        if not dry_run:
            _converge_preconditions(auto_mode=auto_mode, skip_t3=skip_t3)
        # RDR-185 P0.4 (nexus-n7u38.4): `nx upgrade` is the SINGLE trigger for
        # the upgrade ladder — every pending rung converges here (dry-run
        # reports read-only from detect()). The registry is empty until native
        # rungs land (t2-schema P1, substrate-etl P2), so this is silent today.
        # RDR-185 P4.2 (nexus-n7u38.29): the nexus-0rwwv substrate-migration
        # bridge is RETIRED here — the walk above IS the cutover. The bridge
        # existed because `nx upgrade` and the one-time cutover were two
        # commands with nothing between them: a local-mode user saw
        # "migrations complete" and no pointer to the real next step. P4.0
        # registered the substrate rung and P4.0b made provisioning a
        # precondition, so by the time control reaches this line the pending
        # cutover has already converged in THIS invocation. Keeping the
        # pointer would (a) advertise `nx guided-upgrade` — demoted to an
        # internal primitive by P4.1, invisible in --help — as the everyday
        # remedy, breaking "one story, one verb", and (b) re-answer a
        # DATA-rung question ("is a substrate transition pending?") from an
        # ad-hoc re-sample instead of the ladder, which is precisely the
        # third mechanism the Gap-4 criterion bans. Genuine decisions the
        # walk cannot derive (source-gone, billed re-embed) surface from
        # INSIDE the rung; pending state is reported by `nx doctor`.
        with _standing_consent(assume_yes):
            _run_ladder(dry_run=dry_run, auto_mode=auto_mode)

        # nexus-g7ijj: an install that converged purely via `nx upgrade`
        # (never re-running `nx init`) never got install.mode stamped —
        # is_local_mode() fell through to pg_credentials artifact inference
        # forever. Backfill it here, on the ladder-succeeded path only
        # (a hard ladder failure raises before this line); --dry-run must
        # write nothing.
        if not dry_run:
            # nexus-g7ijj fix round: no actual import cycle between
            # nexus.config and nexus.commands.upgrade — deferred to match
            # this file's convention of keeping imports off the CLI's
            # cold-start path, and because backfill_install_mode_record()
            # is genuinely exception-safe (see its docstring): this call
            # site does not need its own try/except.
            from nexus.config import backfill_install_mode_record  # noqa: PLC0415 — deferred, keeps config import off the CLI's cold-start path
            backfill_install_mode_record()

        # RE-WIRED (Hal decision, 2026-07-30): these three post-upgrade
        # steps lost their caller when Stage 4 deleted _run_upgrade's
        # local-SQLite leg — collateral, not a decision. Same gating as
        # before the deletion; all three are substrate-independent and
        # best-effort.
        #
        # nexus-b03o: post-upgrade advisory — pre-4.32 local-mode installs
        # wrote 384d MiniLM vectors into collections named for voyage-*
        # (1024d); mislabeled collections persist until the operator runs
        # `nx collection rename`. Advisory only — never fails the upgrade.
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


@contextlib.contextmanager
def _standing_consent(assume_yes: bool):
    """Scope ``--yes`` to the ladder walk — the ONLY thing that reads it.

    The rung reads standing consent from the environment because the unattended
    callers (hooks, cron) never type a flag; the flag therefore sets what that
    channel reads, rather than growing a second gate to drift from it.

    NARROW BY CONSTRUCTION, not by frame ordering. An earlier draft set the env
    for the whole command and restored it in an outer finally — but `nx
    upgrade`'s own finally SPAWNS DAEMONS (`_cycle_supervised_daemons_to_current`
    and, earlier, `_converge_preconditions`), and those
    subprocesses pass no ``env=``, so they inherit this process's environment
    and ran BEFORE the outer restore. `--yes`, typed once for one invocation,
    reached every long-lived daemon as standing consent to spend money. The
    window now contains exactly the one call that consumes it, so there is no
    ordering left to get wrong.
    """
    import os  # noqa: PLC0415 — stdlib, branch-local

    if not assume_yes:
        yield
        return
    prior = os.environ.get("NX_ASSUME_YES")
    os.environ["NX_ASSUME_YES"] = "1"
    try:
        yield
    finally:
        if prior is None:
            os.environ.pop("NX_ASSUME_YES", None)
        else:
            os.environ["NX_ASSUME_YES"] = prior


def _converge_preconditions(*, auto_mode: bool, skip_t3: bool = False) -> None:
    """RDR-185 P3.1: converge the non-data axes before the ladder walk.

    Best-effort at the trigger level (a precondition failure is reported,
    remediated where derivable, and never blocks the T2 migration that
    already ran) — but the verdicts themselves are computed fresh every
    invocation and never stored.

    ``--skip-t3`` (P3 review Medium): the flag's contract is "fast T2-only"
    — it already gates the bottom-of-command storage-service cycle, so it
    equally gates this stage's engine install and process cycle. Verdicts
    are still computed and reported (they are sub-ms on-disk reads); only
    the CONVERGE actions are suppressed.
    """
    try:
        from nexus.config import nexus_config_dir  # noqa: PLC0415 — deferred to avoid import cycle / CLI startup cost
        from nexus.upgrade_ladder.preconditions import converge_preconditions  # noqa: PLC0415 — deferred to avoid import cost on cold CLI start

        reports = converge_preconditions(
            config_dir=nexus_config_dir(),
            allow_engine_install=not auto_mode and not skip_t3,
            allow_process_cycle=not skip_t3,
        )
        for report in reports:
            for line in report.actions:
                if auto_mode:
                    _log.info("upgrade_precondition_action", axis=report.name, action=line)
                else:
                    click.echo(f"Precondition [{report.name}]: {line}")
            if not report.current and not auto_mode:
                click.echo(f"Precondition [{report.name}] pending: {report.detail}")
    except Exception as exc:  # noqa: BLE001 — best-effort trigger stage; the walk and T2 migration must not be blocked by a precondition probe failure
        _log.warning("upgrade_preconditions_failed", error=str(exc))


def _run_ladder(
    *,
    dry_run: bool,
    auto_mode: bool,
    _ledger_fn: "Callable[[], object] | None" = None,  # noqa: F821 — lazy imports (module keeps cold-start cheap)
) -> None:
    """RDR-185 P0.4: walk the upgrade ladder (or report it, on --dry-run).

    Dry-run truth: the pending report comes from each rung's READ-ONLY
    ``detect()`` — the completion ledger is never even opened, zero writes
    (the ``resolve_pending_steps`` consumption pattern above at the T2
    layer). A failed rung raises ``ClickException`` — no silent fallbacks
    for correctness problems; ``--auto`` invocations are swallowed by
    ``upgrade()``'s existing auto-mode handler, not here.

    Keyword-only ``_ledger_fn`` is an injectable seam for unit tests: it
    returns the durable :class:`CompletionLedger` backend the holder
    fronts (production default: the engine-backed ``HttpLadderStore`` —
    RDR-186 .12, ladder.db retired; NO local substrate exists any more).
    """
    from nexus.upgrade_ladder.holder import InProcessCompletionHolder  # noqa: PLC0415 — deferred to avoid import cost on cold CLI start
    from nexus.upgrade_ladder.http_store import DeferredLadderLedger  # noqa: PLC0415 — deferred to avoid import cost on cold CLI start
    from nexus.upgrade_ladder.registry import default_registry  # noqa: PLC0415 — deferred to avoid import cost on cold CLI start
    from nexus.upgrade_ladder.runner import (  # noqa: PLC0415 — deferred to avoid import cost on cold CLI start
        LadderRunner,
        RungOutcome,
    )

    # RDR-155 P4b: the T2-schema and substrate-ETL rungs died with the
    # migration machinery — the ladder is rekey-only (D-D), so the old
    # _db_path / t2_apply_attempted plumbing into default_registry is gone.
    registry = default_registry()
    if dry_run:
        # Per-rung detect guard (critic P0.R2 finding 1): a real rung's
        # detect() does live reads that can fail (locked db, bad path); the
        # dry-run report must degrade per-rung like LadderRunner._run_rung
        # does, never crash `nx upgrade --dry-run` with a raw traceback.
        for rung in registry:
            try:
                status = rung.detect()
            except Exception as exc:  # noqa: BLE001 — dry-run truth: report the broken rung, keep reporting the rest
                _log.warning("ladder_dry_run_detect_failed", rung=rung.name, error=str(exc))
                click.echo(f"Upgrade ladder: rung '{rung.name}' detect failed — {exc}")
                continue
            if status.pending:
                click.echo(
                    f"Upgrade ladder: rung '{rung.name}' pending — {status.pending_detail or 'behind'}"
                )
        return

    # RDR-186 .12: the durable ledger is the ENGINE's ladder_completions
    # table (HttpLadderStore); ladder.db is retired — the pre-engine window
    # is served entirely by the in-process holder (nexus-146xx.11), and a
    # crash before the end-of-walk flush costs one idempotent re-derivation
    # (RF-186-2), never correctness. The holder's write-through covers the
    # normal path; flush() below retries whatever the engine-defer window
    # left owed, because the walk's own rungs may have brought the engine up.
    # DeferredLadderLedger resolves the engine endpoint on FIRST USE, not
    # here — the ladder must walk while the engine is still absent (its own
    # rungs install it); an unresolvable engine degrades to backend-down
    # inside the holder, never a crash before the walk starts.
    ledger = _ledger_fn() if _ledger_fn is not None else DeferredLadderLedger()
    try:
        holder = InProcessCompletionHolder(ledger)
        report = LadderRunner(registry, holder).run()
        holder.flush()
    finally:
        close = getattr(ledger, "close", None)
        if callable(close):
            close()

    for run in report.runs:
        if run.outcome is RungOutcome.RECORDED and not auto_mode:
            click.echo(f"Upgrade ladder: rung '{run.name}' converged and verified.")
    if report.hard_failed:
        failed = [
            run for run in report.runs
            if run.outcome in (RungOutcome.VERIFY_FAILED, RungOutcome.FAILED)
        ]
        summary = "; ".join(f"{run.name}: {run.outcome.value} ({run.detail})" for run in failed)
        raise click.ClickException(f"upgrade ladder did not converge — {summary}")
    if not report.converged and not auto_mode:
        # Deferred-only: the RDR-142 would-defer class is non-fatal by
        # design (precondition-blocked, retried on a later run) — notice,
        # not failure; nothing was recorded, the position stays pinned.
        for run in report.runs:
            if run.outcome is RungOutcome.DEFERRED:
                click.echo(f"Upgrade ladder: rung '{run.name}' deferred — {run.detail}")




# NO _quiesce_daemon / _cycle_daemon_to_current: both shelled out to the
# retired `nx daemon t2 stop` / `ensure-running` verbs (nexus-i711w Stage 2
# sub-stage B). RDR-128 P2 quiesced the daemon so it released its eight T2
# connections before a migration; with no daemon there are no connections to
# release, and the migration flock + busy_timeout that backstopped the
# best-effort quiesce are still in place. The storage-service sibling below
# (_cycle_storage_service_to_current) is the surviving version-skew cycle.


def _cycle_storage_service_to_current(
    *,
    _discover_fn=None,
    _run_fn=None,
    _nx_bin_fn=None,
    _installed_version_fn=None,
) -> None:
    """Bring a stale supervised storage service to the just-installed version
    (best-effort). RDR-149 P5.1 (nexus-gmiaf.30): the version-skew cycle
    for the Java storage-service + Postgres.

    The supervisor starts the Java JAR with the nexus Python code from the
    same Python install, so a nexus upgrade requires a supervisor restart to
    pick up the new StorageServiceSupervisor bytecode. Only acts on a running
    service (no auto-spawn during upgrade). Never raises.

    Version-gated (nexus-f0pmd, RDR-183 candidate 0 / GH #1405): a live
    supervisor whose lease ``version`` equals the installed package version
    is already current and is left alone — this function runs from
    ``nx upgrade --auto``'s finally block on EVERY SessionStart hook firing
    (startup|resume|clear|compact), and the ungated form stop+started a
    current supervisor on each one (20 ``stop_requested`` exits/day, a
    5-10s lease gap each). Match/mismatch semantics mirror the
    version-aware T2 sibling (``ensure-running``); the undeterminable-
    installed-version case DELIBERATELY diverges (review): T2 treats
    ``not installed`` as already-current (skip), this gate fails TOWARD
    cycling — for the storage service an unnecessary stop/start is a
    bounded blip, while a stale supervisor left running after a real
    upgrade is the #1112/RDR-149 bug class. Empty/legacy lease versions
    likewise cannot prove currency and still cycle.

    Keyword-only ``_discover_fn``, ``_run_fn``, ``_nx_bin_fn``,
    ``_installed_version_fn`` are injectable seams for unit tests (avoids
    patching local imports deep in try blocks). Default values reproduce
    production behaviour exactly.
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

        if _installed_version_fn is None:
            def _installed_version_fn() -> str:
                from importlib.metadata import version  # noqa: PLC0415 — deferred import — only needed on this path
                try:
                    return version("conexus")
                except Exception:  # noqa: BLE001 — ANY probe failure must yield "" (fail toward cycling); a narrower catch would escape to the outer handler and fail toward doing nothing (review)
                    return ""

        live_version = str(getattr(live, "version", "") or "")
        installed = _installed_version_fn() or ""
        if live_version and installed and live_version == installed:
            _log.info(
                "upgrade_storage_service_current_skip",
                version=installed,
            )
            return  # supervisor already runs the installed version
        _log.info(
            "upgrade_storage_service_stale_cycle",
            live_version=live_version,
            installed=installed,
        )

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
    perform. The orchestrator therefore survives its tiers: T3's supervised
    Chroma daemon went with RDR-155 P4b and T2's went with nexus-i711w Stage 2
    sub-stage B, leaving the Java storage service as the one supervised tier.
    Keeping the single seam is the point — a new supervised tier is added here
    once rather than risk being missed. Best-effort; never raises.
    """
    if not skip_t3:
        # RDR-155 P4b: the supervised Chroma T3 daemon is retired — the Java
        # storage service serves T3 in every mode.
        _cycle_storage_service_to_current()  # Java storage service + Postgres (P5.1)


def _run_upgrade(*, dry_run: bool, auto_mode: bool, skip_t3: bool = False) -> None:
    """The T2 schema stage of ``nx upgrade`` — a service-mode no-op.

    RDR-158 P4 Stage 4 (nexus-i711w): the local-SQLite migration leg that
    used to live below the service-mode return (bootstrap_version /
    apply_pending / the RDR-142 dry-run step resolver / the T3-step
    ladder-ledger carry against a local ``memory.db``) is DELETED with
    ``nexus/db/migrations.py`` — the engine owns its schema via Liquibase in
    every mode, and the local ``.db`` files are a frozen migration source
    (RDR-176 Gap 2) this command must never touch. On this version the local
    migration story is the two-hop redirect: install the last
    migration-capable 6.x release, migrate there, upgrade back.

    The resolver call is retained on purpose: a stranded shell's
    ``NX_STORAGE_BACKEND=sqlite`` export must fail LOUD with the
    stranded-install redirect (``StorageModeFlagError`` rendered by the CLI
    boundary as exit 2), never be silently ignored — pinned by
    ``TestUpgradeRetiredSqliteOptOut``.

    ``dry_run`` / ``skip_t3`` are accepted for call-site stability and
    unused here — the surviving pending-work reporting is the ladder's
    read-only ``detect()`` walk in :func:`_run_ladder`.
    """
    del dry_run, skip_t3  # signature stability; the local leg is gone
    from nexus.db.storage_mode import storage_backend_for  # noqa: PLC0415 — deferred import — keep CLI startup cheap

    storage_backend_for("memory")

    if not auto_mode:
        click.echo(
            "Service mode: the local SQLite/Chroma tiers are an immutable "
            "migration source — no local schema migration to run."
        )


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
