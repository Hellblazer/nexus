# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""``nx upgrade`` — run pending T2 schema migrations and T3 upgrade steps.

RDR-076 (nexus-jda).
"""
from __future__ import annotations

import contextlib
from collections.abc import Callable

import click
import structlog

from nexus.bounded_subprocess import run_bounded

_log = structlog.get_logger()


@click.command("upgrade")
@click.option("--dry-run", is_flag=True, help="Report pending upgrade-ladder rungs (read-only detect() walk) without executing. Writes nothing.")
# --force REMOVED (RDR-158 P4 Stage 4, review Significant): it reset the
# local-SQLite version gate so apply_pending re-ran every migration — the
# machinery is deleted, the flag read nothing, and dead-but-documented
# flags actively mislead. 7.0.0 is a major; scripts passing --force now get
# Click's no-such-option error, which is the honest outcome.
@click.option("--auto", "auto_mode", is_flag=True, help="Quiet mode for hook invocation (exit 0 always).")
@click.option("--skip-t3", is_flag=True, help="Skip T3 upgrade steps (e.g., cross-collection projection backfill). Useful for fast T2-only migrations.")
@click.option("--yes", "assume_yes", is_flag=True, help="Assume yes to the billed re-embed consent prompt (equivalent to NX_ASSUME_YES=1). Nothing else prompts.")
@click.option(
    "--no-beads-prime",
    "no_beads_prime",
    is_flag=True,
    default=False,
    help="Do not install/refresh the user-level beads PRIME.md this run "
    "(nexus-cnzei.8). Explicit decline always wins; for a standing "
    "opt-out use `nx config set beads_prime.manage false`.",
)
def upgrade(
    dry_run: bool, auto_mode: bool, skip_t3: bool, assume_yes: bool, no_beads_prime: bool
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
    _say_if_the_engine_is_migrating(auto_mode=auto_mode)
    # NO pre-migration daemon quiesce: RDR-128 P2 stopped the T2 daemon here so
    # it released its serving connections before the migration DDL ran. The
    # daemon is retired (nexus-i711w Stage 2 sub-stage B), so there are no
    # serving connections to free; the migration flock that serialized the
    # MIGRATOR processes is untouched and still does its half.
    #
    #
    # nexus-dcbof: the detached SessionStart child stops STARTING new work
    # after a time limit (default 900 s), at safe points: between census
    # calls, backfill collections and ladder rungs. The clock starts here, so
    # the precondition stage counts against it. It does not bound the "finish"
    # pass the root CLI group runs before this function, a call already in
    # flight, or the service stop/start cycle in the `finally` below (about
    # 60 s each). An interactive `nx upgrade` has no limit. Built inside the
    # try: `--auto` promises exit 0, so even a failure here is swallowed.
    # nexus-mjmjl: every step is announced before it runs, heartbeats while
    # it runs, and reports its duration; the summary lists them all. --auto
    # is quiet on stdout (its contract) and writes the same lines to the log.
    from nexus.progress.step_report import StepReporter  # noqa: PLC0415 — deferred, keeps the CLI cold start light

    from nexus.upgrade_ladder.unattended import env_unattended  # noqa: PLC0415 — deferred, keeps the cold start light

    # P0 (nexus-f3yqd): --auto (the SessionStart hook) and the lockstep hook's unattended
    # run never restart the storage service; only a person's own `nx upgrade` does.
    restarts = not dry_run and not auto_mode and not env_unattended()
    # A restart that could not finish is reported at the end and fails an interactive
    # run (nexus-mjmjl item 14); the precondition stage records its failure here.
    failures: list[str] = []
    restart_failure: str | None = None
    reporter: StepReporter | None = None
    try:
        # Built inside the try: `--auto` promises exit 0, so even a failure
        # here is swallowed.
        steps = _upgrade_steps(
            dry_run=dry_run, auto_mode=auto_mode, skip_t3=skip_t3, assume_yes=assume_yes,
            no_beads_prime=no_beads_prime, failures=failures,
        )
        reporter = StepReporter(len(steps) + (1 if restarts else 0), quiet=auto_mode, name="nx upgrade")
        for what, detail, run in steps:
            # Every step is safe to repeat (the ladder records what converged), so
            # the remedy for any failed one is the same command again.
            with reporter.step(what, detail, remedy="Run `nx upgrade` again: every step is safe to repeat."):
                run()
        # nexus-emiky: a lockstep child that lost the upgrade lock left the retry sentinel in the lockstep
        # marker, saying this work (plugin convergence, agents and hooks refresh, the engine install, the
        # advisory) is owed. A run that did it settles the marker; `--auto` skips that work, `--skip-t3`
        # skips the engine install and the advisory, and `--dry-run` does none.
        if not auto_mode and not dry_run and not skip_t3:
            from nexus.lockstep_marker import running_version, settle_upgrade_retry  # noqa: PLC0415 — deferred, keeps the cold start light

            settle_upgrade_retry(running_version())
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
        if restarts:
            if reporter is None:
                restart_failure = _cycle_supervised_daemons_to_current(skip_t3=skip_t3, auto_mode=auto_mode)
            else:
                with reporter.step(
                    "Restart running services on the new version", "a stale service is cycled; about a minute each",
                    remedy="Start it by hand with `nx daemon service start`; `nx doctor` says what is running.",
                ):
                    restart_failure = _cycle_supervised_daemons_to_current(skip_t3=skip_t3, auto_mode=auto_mode)
        if reporter is not None:
            reporter.summary()
    # Reached only when the body finished: a failed restart is the last thing an
    # interactive upgrade says, and it fails the command, since the service may be
    # left stopped. Under --auto, and in the lockstep hook's unattended child
    # (NX_UPGRADE_UNATTENDED, whose exit the hook counts as a failed upgrade), it is
    # logged and the run stays silent and succeeds.
    failure = next((f for f in (*failures, restart_failure) if isinstance(f, str)), None)
    if failure is not None and not auto_mode and not env_unattended():
        raise click.ClickException(failure)


def _upgrade_steps(
    *, dry_run: bool, auto_mode: bool, skip_t3: bool, assume_yes: bool, no_beads_prime: bool,
    failures: list[str] | None = None,
) -> list[tuple[str, str | None, Callable[[], None]]]:
    """The steps one `nx upgrade` runs, in order, as (what, detail, run).

    Built up front so every step line can say "of M". The gating of each step
    is unchanged from the straight-line body it replaced (nexus-mjmjl)."""
    steps: list[tuple[str, str | None, Callable[[], None]]] = []
    # nexus-dcbof: the detached SessionStart child stops STARTING new work
    # after a time limit (default 900 s), at safe points: between census
    # calls, backfill collections and ladder rungs. The clock starts here, so
    # the precondition stage counts against it. An interactive `nx upgrade`
    # has no limit.
    deadline = _auto_deadline() if auto_mode else None
    steps.append((
        "Check for client-side migrations", None,
        lambda: _run_upgrade(dry_run=dry_run, auto_mode=auto_mode, skip_t3=skip_t3),
    ))
    if not dry_run:
        # RDR-185 P3.1 (nexus-n7u38.23): the non-data axes converge as
        # STATELESS preconditions before the ladder walks, re-derived from
        # on-disk state each invocation, never recorded.
        steps.append((
            "Bring the engine, the PDF stack and the runtime to this release",
            "can download the engine and its PostgreSQL bundle, rebuild the PDF stack and restart services "
            "still on the old version; minutes when it does",
            lambda: _note_failure(failures, _converge_preconditions(auto_mode=auto_mode, skip_t3=skip_t3)),
        ))

    def _ladder() -> None:
        # RDR-185 P0.4 (nexus-n7u38.4): `nx upgrade` is the SINGLE trigger for
        # the upgrade ladder; dry-run reports read-only from detect().
        # nexus-75w5d: `--auto` is UNATTENDED, whatever the deadline says.
        from nexus.upgrade_ladder.unattended import unattended_run  # noqa: PLC0415 — deferred to avoid import cost on cold CLI start

        with _standing_consent(assume_yes), unattended_run(auto_mode):
            _run_ladder(dry_run=dry_run, auto_mode=auto_mode, deadline=deadline)
        # nexus-g7ijj: an install that converged purely via `nx upgrade`
        # never got install.mode stamped; backfill it on the ladder-succeeded
        # path only. --dry-run writes nothing.
        if not dry_run:
            from nexus.config import backfill_install_mode_record  # noqa: PLC0415 — deferred, keeps config import off the CLI's cold-start path

            backfill_install_mode_record()

    steps.append((
        "Run the upgrade ladder's data steps",
        "a pending step can read every document in your store; minutes on a large one",
        _ladder,
    ))
    # nexus-b03o: advisory for collections named for a model whose width
    # their vectors do not have. Never fails the upgrade.
    if not auto_mode and not skip_t3:
        steps.append(("Check collection names against their embedding widths", None, _emit_name_vs_embed_dim_advisory))
    # RDR-137 Phase 5.2: one-shot migration of repos.json into the catalog; idempotent.
    if not auto_mode:
        steps.append(("Move repos.json into the catalog", None, lambda: _migrate_repos_json_to_catalog(dry_run=dry_run)))
    # Refresh nexus-managed git hooks across every registered repo.
    if not auto_mode and not dry_run:
        steps.append(("Refresh the nexus git hooks in registered repos", None, _refresh_all_git_hooks))
    # nexus-2uwag: the plugin is the third thing an upgrade converges. Not
    # under --auto (a git fetch inside the budgeted hook). Advisory.
    if not auto_mode:
        steps.append((
            "Update the plugins", "fetches the plugin marketplace",
            lambda: _converge_plugins(dry_run=dry_run),
        ))
        # Generated agents are composed FROM the installed plugins.
        steps.append(("Refresh the generated agents", None, lambda: _refresh_generated_agents(dry_run=dry_run)))
    # nexus-cnzei.8: the user-level beads PRIME.md when beads is detected.
    if not auto_mode and not dry_run:
        steps.append((
            "Install the beads PRIME file", None,
            lambda: _install_beads_prime_best_effort(no_beads_prime=no_beads_prime),
        ))
    return steps


def _note_failure(failures: list[str] | None, failure: object) -> None:
    """Keep a step's failure sentence for the end of the run (a stub that returns a mock is not one)."""
    if failures is not None and isinstance(failure, str):
        failures.append(failure)


def _say_if_the_engine_is_migrating(*, auto_mode: bool) -> None:
    """nexus-mjmjl: the steps below wait on the engine while it migrates; say
    so first (to the log under ``--auto``, whose stdout stays quiet). Never raises."""
    try:
        import nexus.config as _config  # noqa: PLC0415 — deferred, keeps the CLI cold start light
        from nexus.progress.boot_work import notice  # noqa: PLC0415 — stdlib-only, deferred as above

        lines = notice(_config.nexus_config_dir())
    except Exception:  # noqa: BLE001 — advisory: the upgrade never fails on it
        return
    for line in lines:
        if auto_mode:
            _log.info("upgrade_engine_migrating", line=line)
        else:
            click.echo(line)


def _auto_deadline() -> "Deadline":  # noqa: F821 — lazy import (module keeps cold-start cheap)
    """The ``--auto`` ceiling, its clock started now (nexus-dcbof). A seam:
    tests monkeypatch it to hand the command a fixed-clock deadline."""
    from nexus.upgrade_ladder.deadline import Deadline, auto_deadline_seconds  # noqa: PLC0415 — deferred to avoid import cost on cold CLI start

    return Deadline(auto_deadline_seconds())


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


def _converge_preconditions(*, auto_mode: bool, skip_t3: bool = False) -> str | None:
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
    # A uv takeover is a precondition failure of its own kind: after a stray
    # `uv tool install --force conexus` every spawn resolves through uv's
    # rebuilt, [local]-less tree instead of `current`. The SessionStart
    # lockstep hook runs this command, so repairing here means the box heals
    # at the next session with nothing for the user to do (nexus-hibpr
    # follow-on). Best-effort like everything else in this stage.
    try:
        from nexus.commands.self_cmd import repair_uv_takeover  # noqa: PLC0415 — deferred to avoid import cost on cold CLI start

        for line in repair_uv_takeover():
            if auto_mode:
                _log.info("upgrade_precondition_action", axis="uv-takeover", action=line)
            else:
                click.echo(f"Precondition [uv-takeover]: {line}")
    except Exception as exc:  # noqa: BLE001 — best-effort trigger stage; a repair failure must not block the walk
        _log.warning("upgrade_uv_takeover_repair_failed", error=str(exc))

    # nexus-2k66t.4.2: the PDF stack carry-forward builds a generation and flips
    # `current`, so it runs BEFORE the process cycle below: the cycle then
    # restarts the service and MinerU onto the generation that has the stack,
    # not onto a slim one the carry replaces a minute later (and the stale
    # MinerU restart in upgrade_finish never has a stack-less generation to
    # start). It needs the layout the takeover repair above just settled.
    #
    # nexus-2k66t.4.7, nexus-vyy65: the VC++ runtime goes first. On Windows the
    # carry builds slim (and says so) when the DLLs the stack's extension modules
    # load are missing, and this step is the only thing that provisions them on a
    # cloud-mode box; run after the carry, the first upgrade would always go slim
    # and the stack wait for the next one.
    _converge_vc_runtime(auto_mode=auto_mode)
    _converge_pdf_stack(auto_mode=auto_mode, skip_t3=skip_t3)
    _converge_installer_rebuild(auto_mode=auto_mode, skip_t3=skip_t3)

    try:
        from nexus.config import nexus_config_dir  # noqa: PLC0415 — deferred to avoid import cycle / CLI startup cost
        from nexus.upgrade_ladder.preconditions import converge_preconditions  # noqa: PLC0415 — deferred to avoid import cost on cold CLI start

        from nexus.upgrade_ladder.unattended import env_unattended  # noqa: PLC0415 — deferred, keeps the cold start light

        # The lockstep hook's detached plain `nx upgrade` (NX_UPGRADE_UNATTENDED) is as
        # unattended as --auto for the process: it never restarts the service on a client
        # version string. It keeps its engine install (the real post-upgrade acquisition),
        # and installing a new engine does restart the service.
        unattended = auto_mode or env_unattended()
        # P0 (nexus-f3yqd): --auto is the SessionStart hook, spawned on every Claude session
        # start. Under version skew (two clients installed, the lease written by the other one)
        # the process axis saw a mismatch every time and cycled a healthy storage service, which
        # threw away the engine's index build. Under --auto the process axis is report-only, the
        # posture converge_engine(unattended=True) takes; a person's `nx upgrade` restarts it.
        reports = converge_preconditions(
            config_dir=nexus_config_dir(),
            allow_engine_install=not auto_mode and not skip_t3,
            allow_process_cycle=not unattended and not skip_t3,
            auto_mode=auto_mode,
        )
        for report in reports:
            for line in report.actions:
                if auto_mode:
                    _log.info("upgrade_precondition_action", axis=report.name, action=line)
                else:
                    click.echo(f"Precondition [{report.name}]: {line}")
            if not report.current and not auto_mode:
                click.echo(f"Precondition [{report.name}] pending: {report.detail}")
        return next((r.failure for r in reports if isinstance(getattr(r, "failure", None), str)), None)
    except Exception as exc:  # noqa: BLE001 — best-effort trigger stage; the walk and T2 migration must not be blocked by a precondition probe failure
        _log.warning("upgrade_preconditions_failed", error=str(exc))
        return None


def _converge_pdf_stack(*, auto_mode: bool, skip_t3: bool) -> None:
    """nexus-2k66t.4.2: an existing install ALWAYS carries the PDF stack forward.

    The old installer that moved this box to the first post-split release built
    a slim generation, since the new package no longer requires the PDF stack
    (MinerU) and the old installer cannot know the rule. This is the first code of
    the new release to run; it sees a receipt written before the split and
    builds the generation that adds ``pdf``. Only a fresh install starts slim.
    ``--skip-t3`` is the fast T2-only run and skips it, as it skips the engine
    install: the download is minutes. On a Windows machine that cannot load the stack the
    build goes slim and says so (nexus-vyy65): interactive ``nx upgrade`` prints the line,
    ``nx upgrade --auto`` only logs it (``upgrade_pdf_stack``). Never raises."""
    if skip_t3:
        return
    try:
        from nexus import pdf_stack  # noqa: PLC0415 — deferred to keep the CLI cold-start light

        line = pdf_stack.carry_forward(auto_mode=auto_mode)
    except Exception as exc:  # noqa: BLE001 — best-effort trigger stage; the ladder must not be blocked by it
        _log.warning("upgrade_pdf_stack_failed", error=str(exc))
        return
    if line is None:
        return
    if auto_mode:
        _log.info("upgrade_pdf_stack", detail=line)
    else:
        click.echo(f"PDF support: {line}")


def _converge_installer_rebuild(*, auto_mode: bool, skip_t3: bool) -> None:
    """nexus-au0s3: a generation built by an installer older than this package's
    is rebuilt with this one, at the same version.

    The hop out of a release whose installer is older (7.85.0 to the first release
    that pins dependencies to the lock) is built by the OLD installer, which cannot
    fetch the target's. This is the first code of the new release to run, and it
    sees the receipt's installer_schema. One build with the running installer,
    keeping extras, flipping, reaping by the normal keep-3 rule; the new receipt
    is current, so it does not repeat. Not while the PDF carry-forward is owed
    (that build is this one). ``--skip-t3`` skips it with the other downloads.
    Never raises."""
    if skip_t3:
        return
    try:
        from nexus import pdf_stack  # noqa: PLC0415 — deferred to keep the CLI cold-start light

        line = pdf_stack.rebuild_stale_installer(auto_mode=auto_mode)
    except Exception as exc:  # noqa: BLE001 — best-effort trigger stage; the ladder must not be blocked by it
        _log.warning("upgrade_installer_rebuild_failed", error=str(exc))
        return
    if line is None:
        return
    if auto_mode:
        _log.info("upgrade_installer_rebuild", detail=line)
    else:
        click.echo(f"Installer: {line}")


#: How long ``nx upgrade --auto`` waits after a failed VC++ runtime download.
_VC_RUNTIME_AUTO_BACKOFF_S: float = 6 * 3600.0


def _converge_vc_runtime(*, auto_mode: bool) -> None:
    """nexus-lqjll: on Windows without Microsoft's VC++ redistributable, place
    ``msvcp140.dll`` and ``msvcp140_1.dll`` where the client's extension modules
    find them. This is the automatic leg for installs that predate ``nx init``'s
    step (a cloud-mode box has no engine or PG bundle carrying them): it rides the
    SessionStart ``nx upgrade --auto``, which runs detached with a limit on starting new
    ladder work (nexus-dcbof; this step is not bounded by it), so the download is never
    interrupted. A cheap three-directory stat on every run once
    the DLLs are in place, a no-op off Windows. It also tops up ``msvcp140_atomic_wait.dll``
    (PDF support needs it, nexus-vyy65) from the pinned bundle onto a box that has the base
    pair but not that file, at most once per pinned engine tag. Runs before the PDF
    carry-forward (nexus-2k66t.4.7), which builds without PDF support when they are
    missing (it never refuses), and so before the engine
    axis: a local-mode box that would have taken the DLLs from its engine gets
    them from this download instead, and the engine's copy is then redundant,
    not missing. Never raises."""
    try:
        import nexus.config as _config  # noqa: PLC0415 — deferred to keep config import off the CLI's cold-start path; module attribute, not by-value (nexus-78blw ratchet)
        from nexus.daemon.binary_install import ensure_vc_runtime  # noqa: PLC0415 — deferred to avoid import cost on cold CLI start

        result = ensure_vc_runtime(
            _config.nexus_config_dir(),
            installed_by="nx upgrade",
            # The hook runs this every session: after a failure wait before
            # downloading the 7.4 MB PG bundle again. A person running
            # `nx upgrade` retries.
            failure_backoff_s=_VC_RUNTIME_AUTO_BACKOFF_S if auto_mode else 0.0,
        )
    except Exception as exc:  # noqa: BLE001 — best-effort trigger stage; ensure_vc_runtime itself never raises, this guards the imports
        _log.warning("upgrade_vc_runtime_failed", error=str(exc))
        return
    if result.status not in ("provisioned", "failed"):
        return
    if auto_mode:
        _log.info("upgrade_vc_runtime", status=result.status, detail=result.detail)
    elif result.status == "provisioned":
        click.echo(f"VC++ runtime [client extension modules]: {result.detail}")
    else:
        click.echo(f"VC++ runtime [client extension modules] pending: {result.detail}")


def _record_deferrals(report: "LadderRunReport", completions: "Callable[[], dict[str, CompletionRecord]]") -> None:  # noqa: F821 — lazy imports (module keeps cold-start cheap)
    """Fold one finished ladder walk into the per-rung deferral log (nexus-0sm1t). *completions* reads the ledger's
    records and is called only when the walk has a counted deferral. Whatever goes wrong is a logged warning: the
    log only informs `nx doctor`, it must never change the upgrade's outcome or exit code."""
    from nexus.upgrade_ladder.census_progress import guarded  # noqa: PLC0415 — deferred to avoid import cost on cold CLI start
    from nexus.upgrade_ladder.deferral_log import counted_deferrals, t2_deferral_log  # noqa: PLC0415 — deferred to avoid import cost on cold CLI start

    def _record() -> None:
        if counted_deferrals(report):
            t2_deferral_log().record(report, completions())

    guarded("deferral_log_record", _record, None)


class _BatchProgress:
    """The ladder's :class:`~nexus.upgrade_ladder.protocol.ProgressReporter` for ``nx upgrade`` (nexus-mjmjl).

    Every event still goes to structlog. A rung's ``*_batch`` event also prints one line, at most every
    :attr:`EVERY_S` per rung: ``  manifest span fill: documents 3,200, filled 410, ...`` (the batch's own counts).
    *clock* is injectable for tests."""

    EVERY_S: float = 5.0

    def __init__(self, *, to_stderr: bool = False, clock: "Callable[[], float] | None" = None) -> None:
        import time  # noqa: PLC0415 — stdlib

        self._err = to_stderr
        self._clock = clock or time.monotonic
        self._last: dict[str, float] = {}

    def emit(self, event: str, **fields: object) -> None:
        _log.info(event, **fields)
        if not event.endswith("_batch"):
            return
        rung = event.removesuffix("_batch")
        now = self._clock()
        if now - self._last.get(rung, float("-inf")) < self.EVERY_S:
            return
        self._last[rung] = now
        shown = ", ".join(
            f"{key.replace('_', ' ')} {value:,}" if isinstance(value, int) and not isinstance(value, bool)
            else f"{key.replace('_', ' ')} {value}"
            for key, value in fields.items()
        )
        try:
            click.echo(f"  {rung.replace('_', ' ')}: {shown}", err=self._err)
        except Exception:  # noqa: BLE001 — a progress line never fails the walk
            pass


def _run_ladder(
    *,
    dry_run: bool,
    auto_mode: bool,
    _ledger_fn: "Callable[[], object] | None" = None,  # noqa: F821 — lazy imports (module keeps cold-start cheap)
    deadline: "Deadline | None" = None,  # noqa: F821 — lazy imports (module keeps cold-start cheap)
) -> None:
    """RDR-185 P0.4: walk the upgrade ladder (or report it, on --dry-run).

    Dry-run truth: the pending report comes from each rung's READ-ONLY
    ``detect()`` — this function never opens the completion ledger and
    performs zero writes (the ``resolve_pending_steps`` consumption pattern
    above at the T2 layer). A rung's own ``detect()`` may still READ the
    ledger: ``rdr192-manifest-backfill`` does, to skip its census once the
    completion is on file. A failed rung raises ``ClickException`` — no silent fallbacks
    for correctness problems; ``--auto`` invocations are swallowed by
    ``upgrade()``'s existing auto-mode handler, not here.

    ``deadline`` (``--auto`` only, nexus-dcbof) stops the walk from starting
    new work at safe points (before a rung that has work to do, and inside the
    rung between census calls and backfill collections) once passed; the
    stopped rung reports DEFERRED, so it is non-fatal and the next run resumes.

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
        from nexus.upgrade_ladder.deadline import bound  # noqa: PLC0415 — deferred to avoid import cost on cold CLI start

        for rung in registry:
            try:
                # `--auto --dry-run` binds the deadline too, so a rung's detect() keeps its clock; whether the
                # run is unattended is the contextvar upgrade() set, not this. Interactive `--dry-run` binds none.
                with bound(deadline):
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
        # nexus-mjmjl: a rung's detect() answers without a scan (record or bounded count, else PENDING), so its
        # converge makes the one scan instead of a dry run followed by the same scan; the scan's batch events
        # become progress lines (stderr under --auto, which the hook logs) instead of INFO events nobody sees.
        from nexus.upgrade_ladder.unattended import cheap_detect  # noqa: PLC0415 — deferred to avoid import cost on cold CLI start

        with cheap_detect():
            report = LadderRunner(
                registry, holder, deadline=deadline, reporter=_BatchProgress(to_stderr=auto_mode),
            ).run()
        holder.flush()
        # nexus-0sm1t: count each rung's consecutive deferrals so `nx doctor` can name one that defers on every run,
        # which --auto otherwise leaves silent. Interactive and --auto alike; a log failure never changes the outcome.
        # Inside the try: the completion records are read from the ledger, which closes below.
        _record_deferrals(report, holder.completions)
    finally:
        close = getattr(ledger, "close", None)
        if callable(close):
            close()

    for run in report.runs:
        if run.outcome is RungOutcome.RECORDED and not auto_mode:
            click.echo(f"Upgrade ladder: rung '{run.name}' converged and verified.")
    if not report.runs and not auto_mode:
        # Empty registry (every rung retired — first true at nexus-lgdel.l1,
        # which deleted the last rung). The affirmative line must still print:
        # the fresh-install MVV's 9xfx5 assert greps for "converged and
        # verified", and silence-as-converged is exactly the vacuity that
        # assert exists to ban. An empty ladder is converged BY CONSTRUCTION,
        # stated out loud, never inferred from the absence of output.
        click.echo("Upgrade ladder: converged and verified (no rungs registered).")
    if report.hard_failed:
        # A deferral earlier in the walk is listed too: since nexus-dfemn the walk goes on past it, and a later
        # rung failing on the same outage must not hide the first cause.
        failed = [
            run for run in report.runs
            if run.outcome in (RungOutcome.DEFERRED, RungOutcome.VERIFY_FAILED, RungOutcome.FAILED)
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
    auto_mode: bool = True,
    _discover_fn=None,
    _run_fn=None,
    _nx_bin_fn=None,
    _installed_version_fn=None,
) -> str | None:
    """Bring a stale supervised storage service to the just-installed version
    (best-effort). RDR-149 P5.1 (nexus-gmiaf.30): the version-skew cycle
    for the Java storage-service + Postgres.

    Returns None when there was nothing to do, the restart worked, or the new
    service is still starting; else the user's text: what failed, what to run, and
    what ``nx daemon service start`` itself said (first-run audit item 14,
    nexus-mjmjl). A start that exits non-zero for a dead supervisor, or cannot be
    spawned, is a failure: the service may be left stopped. A start that reports the
    service is up but not ready yet, or outlives our own timeout, is still starting
    (the first start after an upgrade can be a long database migration), which is
    said and is not a failure. A stop that fails before a start that works is a
    failure only if the old version still serves. Interactive runs (``auto_mode``
    False) print a line before the restart and one after it; under ``--auto``
    nothing is printed and a failure is logged.

    The supervisor starts the Java JAR with the nexus Python code from the
    same Python install, so a nexus upgrade requires a supervisor restart to
    pick up the new StorageServiceSupervisor bytecode. Only acts on a running
    service (no auto-spawn during upgrade). Never raises.

    Version-gated (nexus-f0pmd, RDR-183 candidate 0 / GH #1405): a live
    supervisor whose lease ``version`` equals the installed package version
    is already current and is left alone — this function used to run from
    ``nx upgrade --auto``'s finally block on EVERY SessionStart hook firing
    (startup|resume|clear|compact), and the ungated form stop+started a
    current supervisor on each one (20 ``stop_requested`` exits/day, a
    5-10s lease gap each). Since P0 (nexus-f3yqd) neither ``--auto`` nor the
    lockstep hook's unattended run calls it, and a lease NEWER than this client
    (another installed client started the service) is never cycled. Match/mismatch semantics mirror the
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
    import subprocess  # noqa: PLC0415 — stdlib import kept branch-local

    try:
        from nexus.config import nexus_config_dir  # noqa: PLC0415 — deferred to avoid import cycle / CLI startup cost
        from nexus.daemon.service_registry import ServiceRegistry, service_identity  # noqa: PLC0415 — deferred to avoid import cycle / CLI startup cost

        # Discover via the storage_service tier (matches what the supervisor
        # publishes and health._resolve_service_endpoint reads).
        if _discover_fn is None:
            registry = ServiceRegistry(dir=nexus_config_dir(), tier="storage_service")
            scope = service_identity()
            live = registry.discover(scope)
        else:
            live = _discover_fn()

        if live is None:
            return None  # nothing running to cycle

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
            return None  # supervisor already runs the installed version
        from nexus.upgrade_ladder.preconditions import lease_is_newer  # noqa: PLC0415 — deferred to avoid import cost on cold CLI start

        if live_version and installed and lease_is_newer(live_version, installed):
            # P0 (nexus-f3yqd): another, newer client started it; never downgrade it.
            _log.info("upgrade_storage_service_newer_skip", live_version=live_version, installed=installed)
            return None
        _log.info(
            "upgrade_storage_service_stale_cycle",
            live_version=live_version,
            installed=installed,
        )

        from nexus.commands.daemon import _resolve_nx_bin as _real_nx_bin  # noqa: PLC0415 — deferred to avoid import cycle / CLI startup cost
        nx = _nx_bin_fn() if _nx_bin_fn is not None else _real_nx_bin()
        discover = _discover_fn if _discover_fn is not None else (lambda: registry.discover(scope))
        _run = _run_fn if _run_fn is not None else run_bounded
        return _restart(nx, _run, discover, installed, auto_mode=auto_mode)
    except Exception as exc:  # noqa: BLE001 — best-effort storage-service cycle; never raises, the caller reports
        return _restart_failed(f"{type(exc).__name__}: {exc}", "", auto_mode=auto_mode)


#: Above ``nx daemon service start``'s own 60 s wait for the lease plus its spawn and the
#: PG start, so a slow start reaches us as start's own verdict, not as our timeout.
_START_TIMEOUT_S = 150

#: What ``nx daemon service start`` says when the service has not published a lease in time.
#: Both cases exit 2, so the text decides, following the rule ``start`` itself uses for
#: StorageServiceStartError(still_starting=...) in nexus/commands/daemon.py: the detached
#: supervisor timing out, and an autostart unit whose supervisor reported nothing, are still
#: starting (a first boot can migrate for many minutes); a supervisor that exited, or that
#: reported an error (a crash loop, a halted migration), is a failure.
_STILL_STARTING = ("did not become ready within", "may still be migrating")
_FAILED_START = ("The supervisor reported:", "exited with code")


def _restart(nx, run, discover, installed: str, *, auto_mode: bool) -> str | None:
    """Stop the stale service and start it on the installed version; see
    :func:`_cycle_storage_service_to_current` for what it returns and prints."""
    import subprocess  # noqa: PLC0415 — stdlib import kept branch-local

    if not auto_mode:
        # halcyon 2026-11-10: a person's own upgrade restarts a supervisor whose client
        # changed (stale supervisor code is a real bug), and says so in these words.
        click.echo(
            f"Restarting the storage service so it runs conexus {installed or 'the installed version'}"
            " (background index builds continue)"
        )
    stop_problem = None
    try:
        # nexus-t10nc: stdout= plus timeout= is the watched shape; an aliased spawner
        # hides it from the lint's AST scan, so the streams are named here.
        stopped = run(
            [*nx, "daemon", "service", "stop"],
            timeout=60,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        if stopped.returncode != 0:
            stop_problem = f"stopping the old service exited {stopped.returncode}"
    except subprocess.TimeoutExpired:
        stop_problem = "stopping the old service did not finish within 60 s"
    except Exception as exc:  # noqa: BLE001 — a stop that cannot be spawned is reported; the start still runs
        stop_problem = f"stopping the old service failed ({type(exc).__name__}: {exc})"
    try:
        started = run(
            [*nx, "daemon", "service", "start"],
            timeout=_START_TIMEOUT_S,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            text=True,
            # nexus-9pc2f: `start` detach-spawns the supervisor, which must outlive
            # this call. Contained, the child sits in a kill-on-close Windows job that
            # run_bounded closes on return, and that kills the new supervisor and its
            # engine. `stop` stays contained: nothing it starts is meant to survive.
            contained=False,
        )
    except subprocess.TimeoutExpired:
        # The supervisor is detached and survives our timeout: still starting, not dead.
        return _still_starting(installed, auto_mode=auto_mode)
    except Exception as exc:  # noqa: BLE001 — a start that cannot be spawned is a failed restart
        return _restart_failed(_join(stop_problem, f"starting it failed ({type(exc).__name__}: {exc})"), "",
                               auto_mode=auto_mode)
    said = (getattr(started, "stderr", "") or "").strip()
    if started.returncode != 0:
        if any(m in said for m in _STILL_STARTING) and not any(m in said for m in _FAILED_START):
            return _still_starting(installed, auto_mode=auto_mode)
        return _restart_failed(_join(stop_problem, f"starting it exited {started.returncode}"), said,
                               auto_mode=auto_mode)
    if stop_problem:
        # `start` exits 0 on an already-live lease, so a stop that failed can leave the old
        # service serving: the restart worked only if the lease now names the new version.
        live = discover()
        version = str(getattr(live, "version", "") or "")
        if live is None or (installed and version and version != installed):
            return _restart_failed(
                _join(stop_problem, f"the service still runs {version or 'an unknown version'}"),
                "", auto_mode=auto_mode, remedy="`nx daemon service stop`, then `nx daemon service start`",
            )
        _log.warning("upgrade_storage_service_stop_nonzero", problem=stop_problem)
    if not auto_mode:
        click.echo("Storage service restarted.")
    return None


def _join(first: str | None, second: str) -> str:
    return f"{first}, then {second}" if first else second


def _still_starting(installed: str, *, auto_mode: bool) -> None:
    """The service is up but not answering yet: say so and do not fail the upgrade."""
    _log.info("upgrade_storage_service_still_starting", installed=installed)
    if not auto_mode:
        click.echo(
            "The storage service is still starting on the new version. The first start after an"
            " upgrade can migrate its database, which can take several minutes; nexus commands"
            " wait for it. Check on it with `nx daemon service status`."
        )
    return None


def _restart_failed(problem: str, said: str, *, auto_mode: bool,
                    remedy: str = "`nx daemon service start`") -> str:
    """Log a failed storage-service restart and return what the user is told: what failed,
    what to run, and what ``nx daemon service start`` itself said (it names the logs)."""
    _log.warning("upgrade_storage_service_cycle_failed", error=problem, auto_mode=auto_mode)
    text = (
        f"The storage service did not restart after the upgrade: {problem}. It may be stopped;"
        f" run {remedy}."
    )
    if said:
        text += "\n`nx daemon service start` said:\n" + said
    else:
        try:
            from nexus import config as _config  # noqa: PLC0415 — deferred; a module import keeps the by-value import census where it is

            logs = _config.nexus_config_dir() / "logs"
            text += f" The service's logs are {logs / 'storage_service.log'} and {logs / 'storage_service.crash.log'}."
        except Exception:  # noqa: BLE001 — the paths are advisory
            pass
    return text


def _cycle_supervised_daemons_to_current(*, skip_t3: bool = False, auto_mode: bool = True) -> str | None:
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
        return _cycle_storage_service_to_current(auto_mode=auto_mode)  # Java storage service + Postgres (P5.1)
    return None


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
            "Service mode: no local schema migration to run; the service "
            "applies its own schema when it starts."
        )


def _refresh_all_git_hooks() -> None:
    """Refresh nexus-managed git hooks across all registered repos.

    Best-effort: never raises — a hook-refresh failure must not fail the
    upgrade. Silent when no managed hooks exist anywhere.
    """
    try:
        from nexus.commands.hooks import _sweep_tail, refresh_all_managed_hooks  # noqa: PLC0415 — deferred to avoid import cycle / CLI startup cost

        summary = refresh_all_managed_hooks(echo=False)
        # Skipped repos and malformed hooks are worded apart (nexus-sis0m.6):
        # a partly refreshed repo is not "skipped".
        tail = _sweep_tail(summary)
        if tail != ".":
            tail = tail[:-1] + " (see `nx hooks update-all`)."
        if summary["refreshed"]:
            click.echo(
                f"\nRefreshed {summary['refreshed']} git hook(s) across "
                f"{summary['repos']} repo(s)" + tail
            )
        elif summary.get("malformed"):
            click.echo(
                f"\n{summary['malformed']} malformed hook(s) left for hand "
                "repair (see `nx hooks update-all`)."
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
                f"skipping migration (the catalog is empty; run 'nx index repo' first)."
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
    # nexus-5z0us: this probes one chunk per collection (~15s on a
    # 100-collection tenant); say so rather than sit silent.
    click.echo("Checking collection names against their vector dimensions…", err=True)
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


def _install_beads_prime_best_effort(*, no_beads_prime: bool = False) -> None:
    """Install/refresh the user-level ``beads`` PRIME.md when beads is
    detected on this machine (nexus-cnzei.8). Mirrors ``nx init``'s helper
    of the same name — best-effort, never fails the upgrade. ``nx doctor``'s
    "Beads PRIME.md (user-level)" row is the durable, always-visible signal
    if this ever fails silently.

    *no_beads_prime* is ``--no-beads-prime``'s value: an explicit decline
    that always wins. A persisted ``beads_prime.manage: false`` is
    consulted inside ``install_and_describe`` itself either way.
    """
    from nexus.beads_prime import install_and_describe  # noqa: PLC0415 — deferred local import — avoids import-time cost / circular deps

    try:
        message = install_and_describe(disabled=no_beads_prime)
    except Exception as exc:  # noqa: BLE001 — best-effort upgrade step; never fails the upgrade
        click.echo(f"Beads PRIME.md install skipped ({exc}).", err=True)
        _log.warning("upgrade_beads_prime_failed", error=str(exc))
        return
    if message:
        click.echo(message)


def _refresh_generated_agents(*, dry_run: bool) -> None:
    """Regenerate the opt-in generated agents that lag the plugins lockstep
    just converged (worktree-developer today). Best-effort, never raised:
    the plugin advance already happened and is the thing that matters."""
    from nexus.commands.agents_cmd import refresh_worktree_developer  # noqa: PLC0415 — deferred, CLI cold start

    try:
        line = refresh_worktree_developer(dry_run=dry_run)
    except Exception:  # noqa: BLE001 — advisory step, same posture as _converge_plugins
        _log.warning("generated_agent_refresh_error", exc_info=True)
        click.echo("Generated agents: could not check worktree-developer; run nx agents install worktree-developer")
        return
    if line:
        click.echo(line)


def _converge_plugins(*, dry_run: bool) -> None:
    """Bring a behind conexus/sn plugin to the marketplace's pinned version
    (nexus-2uwag; contract in :mod:`nexus.plugin_lockstep`). Best-effort:
    a failure is printed with the manual command and never raised."""
    from nexus.plugin_lockstep import converge_plugins, render  # noqa: PLC0415 — deferred, CLI cold start

    try:
        render(converge_plugins(dry_run=dry_run), click.echo)
    except Exception:  # noqa: BLE001 — advisory step; the data convergence above already happened
        _log.warning("plugin_lockstep_error", exc_info=True)
        click.echo("Plugin lockstep: could not check the installed plugins; run /plugin update in Claude Code")
    # nexus-qocnk: nx upgrade is the path for old installs, exactly where a
    # retired nx plugin (with its own live hooks) is likeliest to remain.
    try:
        from nexus.plugin_lockstep import retired_plugin_installs  # noqa: PLC0415 — deferred, CLI cold start

        for key in retired_plugin_installs():
            click.echo(
                f"Retired plugin {key} is still installed and its hooks are still live "
                f"(GHSA-mc84-6gjq-vm2p). In Claude Code: /plugin uninstall {key}, then /reload-plugins"
            )
    except Exception:  # noqa: BLE001 — advisory step, same as the lockstep above
        _log.warning("retired_plugin_check_error", exc_info=True)
