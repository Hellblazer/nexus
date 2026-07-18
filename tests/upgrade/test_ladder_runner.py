# SPDX-License-Identifier: AGPL-3.0-or-later
"""RDR-185 P0.3 (nexus-n7u38.3): ladder runner + verify-before-record guard.

The runner generalizes RDR-142's stamp-only-on-full-success: a rung's
completion is recorded ONLY after its ``verify()`` passed. The walk is
level-triggered (K8s reconcile shape): every run re-detects; re-runs
converge and never duplicate; a crash between converge and record leaves
the derived position at the last VERIFIED rung and the next run heals by
verify-then-record, never by trusting the crash.
"""
from __future__ import annotations

import importlib.metadata
import pathlib
from dataclasses import dataclass, field

import pytest

from nexus.upgrade_ladder.completion import CompletionRecord
from nexus.upgrade_ladder.protocol import (
    ConvergeOutcome,
    ConvergeResult,
    ProgressReporter,
    RungStatus,
)
from nexus.upgrade_ladder.registry import LadderRegistry
from nexus.upgrade_ladder.runner import (
    LadderRunner,
    LadderRunReport,
    RungOutcome,
    _installed_package_version,
)


@dataclass
class ScriptedRung:
    """Purpose-built stub: converge does `work_units` batches, then verify
    returns `verify_result`. `applicable=False` models an N/A mode."""

    name: str
    work_units: int = 1
    applicable: bool = True
    verify_result: bool = True
    done: int = 0
    converge_calls: int = 0
    verify_calls: int = 0
    fail_converge: bool = False
    stuck: bool = False  # RESUMABLE forever without progress (buggy rung)
    defer: bool = False  # precondition-blocked: converge reports DEFERRED

    def detect(self) -> RungStatus:
        if not self.applicable:
            return RungStatus(applicable=False, converged=False)
        return RungStatus(
            applicable=True,
            converged=self.done >= self.work_units,
            pending_detail="pending" if self.done < self.work_units else "",
        )

    def converge(self, report: ProgressReporter) -> ConvergeResult:
        self.converge_calls += 1
        if self.fail_converge:
            raise RuntimeError("converge exploded")
        if self.stuck:
            return ConvergeResult(ConvergeOutcome.RESUMABLE, detail="no progress")
        if self.defer:
            return ConvergeResult(ConvergeOutcome.DEFERRED, detail="precondition blocked")
        if self.done < self.work_units:
            self.done += 1
        if self.done >= self.work_units:
            return ConvergeResult(ConvergeOutcome.COMPLETED)
        return ConvergeResult(ConvergeOutcome.RESUMABLE)

    def verify(self) -> bool:
        self.verify_calls += 1
        return self.verify_result and self.done >= self.work_units


@dataclass
class Recorder:
    events: list[tuple[str, dict[str, object]]] = field(default_factory=list)

    def emit(self, event: str, **fields: object) -> None:
        self.events.append((event, fields))


class InMemoryLedger:
    """Minimal CompletionLedger for runner tests (RDR-186 .12: ladder.db is
    retired; the runner's contract is substrate-independent)."""

    def __init__(self) -> None:
        self.records: dict[str, CompletionRecord] = {}

    def record_verified(self, rung_name: str, *, package_version: str, detail: str = "") -> None:
        self.records[rung_name] = CompletionRecord(
            rung_name=rung_name, verified_at="t0", package_version=package_version, detail=detail
        )

    def verified_rungs(self) -> frozenset[str]:
        return frozenset(self.records)

    def completions(self) -> dict[str, CompletionRecord]:
        return dict(self.records)


@pytest.fixture
def store() -> InMemoryLedger:
    return InMemoryLedger()


def _runner(rungs: tuple[ScriptedRung, ...], store: InMemoryLedger, **kwargs: object) -> LadderRunner:
    return LadderRunner(
        LadderRegistry(rungs),
        store,
        package_version_fn=lambda: "6.12.0",
        **kwargs,  # type: ignore[arg-type]
    )


def _outcomes(report: LadderRunReport) -> list[tuple[str, RungOutcome]]:
    return [(run.name, run.outcome) for run in report.runs]


# ── Happy path ───────────────────────────────────────────────────────────────


def test_walk_converges_and_records_in_order(store: InMemoryLedger) -> None:
    a, b = ScriptedRung("a"), ScriptedRung("b")
    report = _runner((a, b), store).run()
    assert _outcomes(report) == [("a", RungOutcome.RECORDED), ("b", RungOutcome.RECORDED)]
    assert report.converged
    assert report.position == 2
    assert store.verified_rungs() == frozenset({"a", "b"})
    assert store.completions()["a"].package_version == "6.12.0"


def test_resumable_rung_is_driven_to_completion_in_one_run(store: InMemoryLedger) -> None:
    rung = ScriptedRung("multi", work_units=3)
    report = _runner((rung,), store).run()
    assert _outcomes(report) == [("multi", RungOutcome.RECORDED)]
    assert rung.converge_calls == 3


def test_rerun_is_idempotent(store: InMemoryLedger) -> None:
    rung = ScriptedRung("a")
    runner = _runner((rung,), store)
    first = runner.run()
    assert _outcomes(first) == [("a", RungOutcome.RECORDED)]
    converges_after_first = rung.converge_calls

    second = runner.run()
    assert _outcomes(second) == [("a", RungOutcome.ALREADY_RECORDED)]
    assert rung.converge_calls == converges_after_first  # no duplicate work
    assert second.position == 1


# ── The RDR-142 guard: verify-fail must NOT advance position ────────────────


def test_verify_fail_records_nothing_and_stops_the_walk(store: InMemoryLedger) -> None:
    bad = ScriptedRung("bad", verify_result=False)
    later = ScriptedRung("later")
    report = _runner((bad, later), store).run()
    assert _outcomes(report) == [
        ("bad", RungOutcome.VERIFY_FAILED),
        ("later", RungOutcome.NOT_ATTEMPTED),
    ]
    assert not report.converged
    assert report.position == 0
    assert store.verified_rungs() == frozenset()
    assert later.converge_calls == 0  # the ladder never walks past failed work


def test_verify_fail_after_earlier_success_pins_position(store: InMemoryLedger) -> None:
    good = ScriptedRung("good")
    bad = ScriptedRung("bad", verify_result=False)
    report = _runner((good, bad), store).run()
    assert report.position == 1
    assert store.verified_rungs() == frozenset({"good"})


def test_converge_error_records_nothing_and_stops(store: InMemoryLedger) -> None:
    boom = ScriptedRung("boom", fail_converge=True)
    later = ScriptedRung("later")
    report = _runner((boom, later), store).run()
    assert _outcomes(report) == [
        ("boom", RungOutcome.FAILED),
        ("later", RungOutcome.NOT_ATTEMPTED),
    ]
    assert "converge exploded" in report.runs[0].detail
    assert store.verified_rungs() == frozenset()


def test_stuck_resumable_rung_exhausts_step_budget(store: InMemoryLedger) -> None:
    """A buggy rung claiming RESUMABLE forever must not spin the runner —
    the step budget fails it loudly, records nothing, stops the walk."""
    stuck = ScriptedRung("stuck", stuck=True)
    report = _runner((stuck,), store, max_steps_per_rung=5).run()
    assert _outcomes(report) == [("stuck", RungOutcome.FAILED)]
    assert "step budget" in report.runs[0].detail
    assert stuck.converge_calls == 5
    assert store.verified_rungs() == frozenset()


def test_deferred_rung_stops_walk_without_failing(store: InMemoryLedger) -> None:
    """RDR-142 would-defer class: a deferred rung records nothing and stops
    the walk (hard edges), but the report is NOT hard-failed — deferral is
    designed to retry on a later run, never to fail the trigger."""
    deferred = ScriptedRung("deferred", defer=True)
    later = ScriptedRung("later")
    report = _runner((deferred, later), store).run()
    assert _outcomes(report) == [
        ("deferred", RungOutcome.DEFERRED),
        ("later", RungOutcome.NOT_ATTEMPTED),
    ]
    assert store.verified_rungs() == frozenset()
    assert not report.converged
    assert not report.hard_failed
    assert deferred.verify_calls == 0  # deferral bypasses verify: nothing claims completion


# ── Crash resume: converged-but-unrecorded heals by verify-then-record ──────


def test_crash_between_converge_and_record_heals_on_next_run(store: InMemoryLedger) -> None:
    """Converged-but-unrecorded state: the rung's state is converged, the
    ledger has no row. Originally framed as a crash window; since RDR-186
    .12 this is ALSO the normal pre-flush transient (engine-defer window) —
    either way, position stays at the last VERIFIED rung until the next run
    verifies and records, never trusting the unrecorded convergence."""
    crashed = ScriptedRung("crashed", done=1)  # converged on disk, unrecorded
    from nexus.upgrade_ladder.completion import derive_ladder_position

    assert derive_ladder_position(store.verified_rungs(), ("crashed",)) == 0  # pre-heal: not verified

    report = _runner((crashed,), store).run()
    assert _outcomes(report) == [("crashed", RungOutcome.RECORDED)]
    assert crashed.verify_calls == 1  # healed via verify, not assumed
    assert crashed.converge_calls == 0  # nothing to redo
    assert report.position == 1


def test_converged_but_unrecorded_with_failing_verify_stops(store: InMemoryLedger) -> None:
    """The heal path still runs through the guard: a converged-looking rung
    whose verify fails is VERIFY_FAILED, not silently recorded."""
    liar = ScriptedRung("liar", done=1, verify_result=False)
    report = _runner((liar,), store).run()
    assert _outcomes(report) == [("liar", RungOutcome.VERIFY_FAILED)]
    assert store.verified_rungs() == frozenset()


def test_recorded_rung_that_goes_pending_again_reconverges(store: InMemoryLedger) -> None:
    """Critic P0.R2 finding 2 (the first thing P1's t2-schema rung exercises):
    a rung RECORDED in a prior run whose detect() later reports not-converged
    again (new package version added new work) must re-converge, re-verify,
    and UPSERT — never short-circuit to ALREADY_RECORDED on the stale record."""
    rung = ScriptedRung("t2", work_units=1)
    runner = LadderRunner(
        LadderRegistry((rung,)),
        store,
        package_version_fn=lambda: "6.12.0",
    )
    assert _outcomes(runner.run()) == [("t2", RungOutcome.RECORDED)]

    # New release ships another migration: the rung is pending again.
    rung.work_units = 2
    second = LadderRunner(
        LadderRegistry((rung,)),
        store,
        package_version_fn=lambda: "6.13.0",
    ).run()
    assert _outcomes(second) == [("t2", RungOutcome.RECORDED)]
    assert rung.done == 2  # genuinely re-converged

    records = store.completions()
    assert len(records) == 1  # upsert, not a duplicate row
    assert records["t2"].package_version == "6.13.0"  # the fact was replaced


# ── N/A rungs: detect→skip (f0pmd pattern) ──────────────────────────────────


def test_not_applicable_rung_is_skipped_without_record(store: InMemoryLedger) -> None:
    na = ScriptedRung("na", applicable=False)
    after = ScriptedRung("after")
    report = _runner((na, after), store).run()
    assert _outcomes(report) == [
        ("na", RungOutcome.SKIPPED_NOT_APPLICABLE),
        ("after", RungOutcome.RECORDED),
    ]
    assert na.converge_calls == 0
    assert na.verify_calls == 0
    assert store.verified_rungs() == frozenset({"after"})


# ── Read-only pending sweep (the doctor surface, consumed by P0.4) ──────────


def test_pending_sweep_is_read_only(store: InMemoryLedger) -> None:
    pending_rung = ScriptedRung("pending-rung")
    converged_rung = ScriptedRung("converged-rung", done=1)
    na = ScriptedRung("na", applicable=False)
    runner = _runner((pending_rung, converged_rung, na), store)

    pending = runner.pending()
    assert [(name, status.pending) for name, status in pending] == [
        ("pending-rung", True),
        ("converged-rung", False),
        ("na", False),
    ]
    # Zero work, zero records: detect-only.
    assert pending_rung.converge_calls == 0
    assert pending_rung.verify_calls == 0
    assert store.verified_rungs() == frozenset()


def test_installed_package_version_falls_back_to_unknown(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Validator gap 2: a broken metadata probe must degrade to 'unknown',
    never crash the runner's default record path."""

    def _boom(_name: str) -> str:
        raise RuntimeError("metadata exploded")

    monkeypatch.setattr(importlib.metadata, "version", _boom)
    assert _installed_package_version() == "unknown"


# ── Progress reporting ───────────────────────────────────────────────────────


def test_runner_emits_walk_events(store: InMemoryLedger) -> None:
    recorder = Recorder()
    rung = ScriptedRung("a")
    LadderRunner(
        LadderRegistry((rung,)),
        store,
        package_version_fn=lambda: "6.12.0",
        reporter=recorder,
    ).run()
    events = [name for name, _ in recorder.events]
    assert "ladder_rung_recorded" in events
