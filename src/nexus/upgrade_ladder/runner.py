# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""RDR-185 P0.3: the ladder runner — the generalized RDR-142 guard.

The runner walks the registry in order, level-triggered (the Kubernetes
reconcile shape — every run re-detects from actual state, so re-runs
converge and never duplicate). Per rung: detect → converge (looping
resumable batches) → verify → record — and a completion is recorded ONLY
when ``verify()`` passed. This generalizes the three proven precedents:
``apply_pending``'s ``_upgrade_done``-only-on-full-success, the
sequencer's validate-then-``mark_migrated`` with partial-leg refusal, and
``bootstrap_version``'s guarded stamp (RDR-142: the position must never
advance past deferred or failed work).

Failure semantics: the ladder's hard edges make later rungs depend on
earlier ones, so any failure (converge error, verify fail, step-budget
exhaustion) STOPS the walk; remaining rungs report ``NOT_ATTEMPTED``.

Crash resume: a crash between converge and record leaves the derived
position at the last VERIFIED rung. The next run detects the rung as
converged-but-unrecorded and heals it through the same guard —
verify-then-record, never trust-the-crash.

Rungs that are N/A for this install/mode detect-and-skip (the
nexus-f0pmd gate pattern) without a completion record; the read-only
``pending()`` sweep (the ``nx doctor`` surface, P0.4) likewise reports
only from ``detect()``.

Long-rung background dispatch (the GitLab batched-migration model) is
deliberately NOT here yet: no long rung exists until the P2 substrate-ETL
rung lands, and rungs are already resumable at the protocol level — the
walk can be interrupted and re-run at any batch boundary.
"""
from __future__ import annotations

from collections.abc import Callable, Iterator
from dataclasses import dataclass
from enum import Enum

import structlog

from nexus.upgrade_ladder.completion import derive_ladder_position
from nexus.upgrade_ladder.deadline import Deadline, bound
from nexus.upgrade_ladder.protocol import CompletionLedger, ProgressReporter, Rung, RungStatus
from nexus.upgrade_ladder.registry import LadderRegistry

_log = structlog.get_logger(__name__)

#: Ceiling on converge() calls per rung per run — a buggy rung claiming
#: RESUMABLE forever without progress must fail loudly, not spin.
DEFAULT_MAX_STEPS_PER_RUNG = 10_000


def installed_package_version() -> str:
    """The installed ``conexus`` version, stamped on every completion record.
    Public so a rung that compares its record's ``package_version`` to the
    installed one (``rdr192-manifest-backfill``) uses the SAME probe the
    runner records with."""
    from importlib.metadata import version  # noqa: PLC0415 — deferred; only needed when recording

    try:
        return version("conexus")
    except Exception:  # noqa: BLE001 — completion records must survive a broken metadata probe; "unknown" is honest and non-blocking
        return "unknown"


_installed_package_version = installed_package_version


def _optional_text(rung: object, method: str) -> str:
    """Call an OPTIONAL structural ``() -> str`` method a rung may expose
    (``verify_detail``, ``record_detail``). Absent, not callable, or returning
    a non-string yields ``""``, so the ledger's ``str`` parameters stay honest."""
    fn = getattr(rung, method, None)
    if not callable(fn):
        return ""
    value = fn()
    return value if isinstance(value, str) else ""


def _optional_flag(rung: object, method: str) -> bool:
    """Call an OPTIONAL structural ``() -> bool`` method (``verify_deferred``)."""
    fn = getattr(rung, method, None)
    return bool(fn()) if callable(fn) else False


class StructlogReporter:
    """Default :class:`ProgressReporter`: batch events go to structlog."""

    def emit(self, event: str, **fields: object) -> None:
        _log.info(event, **fields)


def pending_rungs(registry: LadderRegistry) -> list[tuple[str, RungStatus]]:
    """Read-only detect sweep over every rung — zero work, zero records.

    The dry-run-truth surface (``resolve_pending_steps`` precedent): the
    ``nx doctor`` pending-rungs check and ``nx upgrade --dry-run`` report
    from this without writing anything. The runner itself does not open the
    completion store here, but a rung's ``detect()`` may READ it (and other
    engine state); it must stay cheap, since this sweep also runs from the
    root CLI's version-transition callout.

    PER-RUNG degradation, like :meth:`LadderRunner._run_rung` and the
    dry-run loop (nexus-fffey). A bare comprehension let one rung's raising
    ``detect()`` escape the whole sweep into ``nx doctor``'s blanket handler,
    which reports the ladder row as ``ok=True, "check failed
    (non-critical)"`` — so an install `nx upgrade` REFUSES read as healthy,
    and the refusal's message (the only text naming the remedy) appeared
    nowhere. A rung that cannot answer is PENDING and says why: it is
    certainly not converged, and Gap-4 makes this row the authority on
    pending work.
    """
    out: list[tuple[str, RungStatus]] = []
    for rung in registry:
        try:
            out.append((rung.name, rung.detect()))
        except Exception as exc:  # noqa: BLE001 — one broken rung must not blind the sweep, and must not read as converged
            _log.warning("ladder_detect_failed", rung=rung.name, error=str(exc))
            out.append((
                rung.name,
                RungStatus(
                    applicable=True,
                    converged=False,
                    pending_detail=f"cannot determine state: {exc}",
                ),
            ))
    return out


class RungOutcome(str, Enum):
    """What happened to one rung during a ladder walk."""

    SKIPPED_NOT_APPLICABLE = "skipped-not-applicable"  # N/A for this mode
    ALREADY_RECORDED = "already-recorded"              # converged + fact on file
    RECORDED = "recorded"                              # verify passed; fact written
    DEFERRED = "deferred"                              # precondition blocks remainder —
                                                       # non-fatal, NOT recorded (RDR-142
                                                       # MigrationRetry class)
    VERIFY_FAILED = "verify-failed"                    # NOT recorded (RDR-142 guard)
    FAILED = "failed"                                  # converge raised / budget out
    NOT_ATTEMPTED = "not-attempted"                    # walk stopped earlier


@dataclass(frozen=True)
class RungRun:
    """One rung's outcome in a :class:`LadderRunReport`."""

    name: str
    outcome: RungOutcome
    detail: str = ""


@dataclass(frozen=True)
class LadderRunReport:
    """Outcome of one ladder walk plus the position derived AFTER it."""

    runs: tuple[RungRun, ...]
    position: int

    _OK = frozenset(
        {RungOutcome.SKIPPED_NOT_APPLICABLE, RungOutcome.ALREADY_RECORDED, RungOutcome.RECORDED}
    )

    @property
    def converged(self) -> bool:
        """True when every rung ended in a non-failure, non-deferred outcome."""
        return all(run.outcome in self._OK for run in self.runs)

    @property
    def hard_failed(self) -> bool:
        """True when any rung genuinely FAILED (converge error, budget out,
        verify fail) — as opposed to merely deferring. Deferred-only runs are
        not converged but are NOT failures: the deferral class is designed
        to retry on a later run (RDR-142 would-defer, non-fatal)."""
        return any(
            run.outcome in (RungOutcome.VERIFY_FAILED, RungOutcome.FAILED)
            for run in self.runs
        )


class LadderRunner:
    """Walks a :class:`LadderRegistry` against a :class:`CompletionLedger`.

    Constructor injection throughout: the completion store, the progress
    reporter, and the package-version probe are all seams.
    """

    def __init__(
        self,
        registry: LadderRegistry,
        store: CompletionLedger,
        *,
        reporter: ProgressReporter | None = None,
        package_version_fn: Callable[[], str] | None = None,
        max_steps_per_rung: int = DEFAULT_MAX_STEPS_PER_RUNG,
        deadline: Deadline | None = None,
    ) -> None:
        self._registry = registry
        self._store = store
        self._reporter = reporter if reporter is not None else StructlogReporter()
        self._package_version_fn = (
            package_version_fn if package_version_fn is not None else _installed_package_version
        )
        self._max_steps_per_rung = max_steps_per_rung
        self._deadline = deadline

    # ── read-only surface (nx doctor, P0.4) ─────────────────────────────────

    def pending(self) -> list[tuple[str, RungStatus]]:
        """Read-only detect sweep — delegates to :func:`pending_rungs`."""
        return pending_rungs(self._registry)

    # ── the walk ─────────────────────────────────────────────────────────────

    def run(self) -> LadderRunReport:
        """Walk the ladder: converge every pending rung in order, recording
        each ONLY after its verify passes. Stops at the first failure.

        With a ``deadline`` (the detached ``--auto`` child only, nexus-dcbof)
        the walk stops STARTING work at safe points. Here, before each rung:
        once the deadline has passed, a rung that has nothing to do
        (not applicable, or converged and recorded) still reports its own
        outcome, and the first rung that WOULD do work is reported DEFERRED
        (non-fatal, the class that retries on a later run) with the rest
        NOT_ATTEMPTED. The deadline is also bound for the running rung, so a
        rung can stop at its own safe points (see
        :mod:`nexus.upgrade_ladder.deadline`); nothing is killed mid-call and
        the next run resumes from the first rung not yet verified."""
        runs: list[RungRun] = []
        rungs = iter(self._registry)
        for rung in rungs:
            if (
                self._deadline is not None
                and self._deadline.expired()
                and self._would_do_work(rung)
            ):
                runs.extend(self._stop_at_deadline(self._deadline, rung, rungs))
                break
            with bound(self._deadline):
                run = self._run_rung(rung)
            runs.append(run)
            # A failed OR deferred rung stops the walk: the hard edges make
            # later rungs depend on earlier ones. Deferred differs only in
            # severity (non-fatal, retried later), not in walk semantics.
            if run.outcome in (
                RungOutcome.VERIFY_FAILED,
                RungOutcome.FAILED,
                RungOutcome.DEFERRED,
            ):
                runs.extend(self._not_attempted(rungs))
                break
        return LadderRunReport(
            runs=tuple(runs),
            position=derive_ladder_position(
                self._store.verified_rungs(), [r.name for r in self._registry]
            ),
        )

    def _would_do_work(self, rung: Rung) -> bool:
        """Read-only: would walking *rung* now converge or verify something?
        False for a rung that is not applicable or already converged AND
        recorded. A detect that raises is not work to defer: it returns False so
        the walk does not stop at the deadline, and :meth:`_run_rung` calls
        detect again and reports the failure as FAILED instead of hiding it
        behind a deferral."""
        try:
            status = rung.detect()
        except Exception:  # noqa: BLE001 — surfaced by _run_rung, which reports the detect failure
            return False
        if not status.applicable:
            return False
        return not (status.converged and rung.name in self._store.verified_rungs())

    def _stop_at_deadline(
        self, deadline: Deadline, next_rung: Rung, remaining: Iterator[Rung]
    ) -> list[RungRun]:
        rest = list(remaining)
        later = self._not_attempted(iter(rest), reason="auto deadline reached")
        skipped = [next_rung.name, *(r.name for r in rest if self._would_do_work(r))]
        elapsed = deadline.elapsed()
        _log.warning(
            "upgrade_auto_deadline_reached",
            elapsed_s=round(elapsed, 1),
            deadline_s=deadline.seconds,
            next_rung=next_rung.name,
            skipped=skipped,
        )
        deferred = RungRun(
            next_rung.name,
            RungOutcome.DEFERRED,
            detail=(
                f"auto deadline of {deadline.seconds:g}s reached after "
                f"{elapsed:.1f}s; resumes on the next run"
            ),
        )
        return [deferred, *later]

    def _run_rung(self, rung: Rung) -> RungRun:
        try:
            status = rung.detect()
        except Exception as exc:  # noqa: BLE001 — a rung's detect failure must become a reported walk stop, not a crash of the single upgrade trigger
            _log.warning("ladder_rung_detect_failed", rung=rung.name, error=str(exc))
            return RungRun(rung.name, RungOutcome.FAILED, detail=f"detect raised: {exc}")

        if not status.applicable:
            self._reporter.emit("ladder_rung_skipped_na", rung=rung.name)
            return RungRun(rung.name, RungOutcome.SKIPPED_NOT_APPLICABLE)

        if status.converged and rung.name in self._store.verified_rungs():
            return RungRun(rung.name, RungOutcome.ALREADY_RECORDED)

        if not status.converged:
            non_completion = self._converge(rung)
            if non_completion is not None:
                return non_completion

        # Converged (either just now or found converged-but-unrecorded after a
        # crash): the RDR-142 guard — record ONLY on a passing verify.
        if not rung.verify():
            # nexus-hdumg: a refusing verify() may carry WHY. Without this the
            # only text distinguishing "poison rows remain" from "the
            # conformance diagnostic could not run at all" lived in a log
            # line, and the operator-facing message was a bare "verify
            # failed" — RDR-182 requires the unavailable note itself to reach
            # the operator. OPTIONAL structural surface: rungs without it
            # keep the generic detail.
            detail = _optional_text(rung, "verify_detail")
            # OPTIONAL structural surface: a rung whose verify() refused only
            # because its evidence could not be read (an outage) says so, and
            # the walk reports a deferral instead of a hard failure. A real
            # refusal (evidence read, and it was bad) stays VERIFY_FAILED.
            if _optional_flag(rung, "verify_deferred"):
                _log.warning("ladder_rung_verify_deferred", rung=rung.name, detail=detail)
                return RungRun(rung.name, RungOutcome.DEFERRED, detail=detail)
            _log.warning("ladder_rung_verify_failed", rung=rung.name, detail=detail)
            return RungRun(
                rung.name,
                RungOutcome.VERIFY_FAILED,
                detail=detail or "verify failed after converge; completion NOT recorded",
            )
        # OPTIONAL structural surface (like ``verify_detail``): a rung may
        # carry the evidence its verify() just read, so the durable record
        # says what was examined and not only that something passed.
        record_detail = _optional_text(rung, "record_detail")
        if record_detail:
            self._store.record_verified(
                rung.name,
                package_version=self._package_version_fn(),
                detail=record_detail,
            )
        else:
            self._store.record_verified(
                rung.name, package_version=self._package_version_fn(),
            )
        self._reporter.emit("ladder_rung_recorded", rung=rung.name)
        return RungRun(rung.name, RungOutcome.RECORDED)

    def _converge(self, rung: Rung) -> RungRun | None:
        """Drive converge to completion (resumable batches). Returns a FAILED
        or DEFERRED RungRun on non-completion, None on completion."""
        for _ in range(self._max_steps_per_rung):
            try:
                result = rung.converge(self._reporter)
            except Exception as exc:  # noqa: BLE001 — any converge failure stops the walk with a reported outcome; the rung resumes from its persisted floor next run
                _log.warning("ladder_rung_converge_failed", rung=rung.name, error=str(exc))
                return RungRun(rung.name, RungOutcome.FAILED, detail=f"converge raised: {exc}")
            if result.deferred:
                # WARNING, not info (P1 critique Medium): DEFERRED bypasses
                # verify/record entirely, so a rung abusing it to dodge
                # failure visibility must at least be loud in the logs. The
                # contract: DEFERRED is ONLY for the documented precondition-
                # retry class (MigrationRetry shape); genuine failures raise.
                _log.warning("ladder_rung_deferred", rung=rung.name, detail=result.detail)
                return RungRun(rung.name, RungOutcome.DEFERRED, detail=result.detail)
            if result.completed:
                return None
        _log.warning(
            "ladder_rung_step_budget_exhausted",
            rung=rung.name,
            budget=self._max_steps_per_rung,
        )
        return RungRun(
            rung.name,
            RungOutcome.FAILED,
            detail=f"step budget exhausted after {self._max_steps_per_rung} converge calls",
        )

    @staticmethod
    def _not_attempted(
        remaining: Iterator[Rung], *, reason: str = "walk stopped at earlier failure"
    ) -> list[RungRun]:
        return [
            RungRun(rung.name, RungOutcome.NOT_ATTEMPTED, detail=reason)
            for rung in remaining
        ]
