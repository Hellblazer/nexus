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

import pathlib
from dataclasses import dataclass, field

import pytest

from nexus.upgrade_ladder.completion import CompletionStore
from nexus.upgrade_ladder.protocol import (
    ConvergeOutcome,
    ConvergeResult,
    ProgressReporter,
    RungStatus,
)
from nexus.upgrade_ladder.registry import LadderRegistry
from nexus.upgrade_ladder.runner import LadderRunner, LadderRunReport, RungOutcome


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


@pytest.fixture
def store(tmp_path: pathlib.Path) -> CompletionStore:
    with CompletionStore(tmp_path / "ladder.db", now_fn=lambda: "t0") as s:
        yield s


def _runner(rungs: tuple[ScriptedRung, ...], store: CompletionStore, **kwargs: object) -> LadderRunner:
    return LadderRunner(
        LadderRegistry(rungs),
        store,
        package_version_fn=lambda: "6.12.0",
        **kwargs,  # type: ignore[arg-type]
    )


def _outcomes(report: LadderRunReport) -> list[tuple[str, RungOutcome]]:
    return [(run.name, run.outcome) for run in report.runs]


# ── Happy path ───────────────────────────────────────────────────────────────


def test_walk_converges_and_records_in_order(store: CompletionStore) -> None:
    a, b = ScriptedRung("a"), ScriptedRung("b")
    report = _runner((a, b), store).run()
    assert _outcomes(report) == [("a", RungOutcome.RECORDED), ("b", RungOutcome.RECORDED)]
    assert report.converged
    assert report.position == 2
    assert store.verified_rungs() == frozenset({"a", "b"})
    assert store.completions()["a"].package_version == "6.12.0"


def test_resumable_rung_is_driven_to_completion_in_one_run(store: CompletionStore) -> None:
    rung = ScriptedRung("multi", work_units=3)
    report = _runner((rung,), store).run()
    assert _outcomes(report) == [("multi", RungOutcome.RECORDED)]
    assert rung.converge_calls == 3


def test_rerun_is_idempotent(store: CompletionStore) -> None:
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


def test_verify_fail_records_nothing_and_stops_the_walk(store: CompletionStore) -> None:
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


def test_verify_fail_after_earlier_success_pins_position(store: CompletionStore) -> None:
    good = ScriptedRung("good")
    bad = ScriptedRung("bad", verify_result=False)
    report = _runner((good, bad), store).run()
    assert report.position == 1
    assert store.verified_rungs() == frozenset({"good"})


def test_converge_error_records_nothing_and_stops(store: CompletionStore) -> None:
    boom = ScriptedRung("boom", fail_converge=True)
    later = ScriptedRung("later")
    report = _runner((boom, later), store).run()
    assert _outcomes(report) == [
        ("boom", RungOutcome.FAILED),
        ("later", RungOutcome.NOT_ATTEMPTED),
    ]
    assert "converge exploded" in report.runs[0].detail
    assert store.verified_rungs() == frozenset()


def test_stuck_resumable_rung_exhausts_step_budget(store: CompletionStore) -> None:
    """A buggy rung claiming RESUMABLE forever must not spin the runner —
    the step budget fails it loudly, records nothing, stops the walk."""
    stuck = ScriptedRung("stuck", stuck=True)
    report = _runner((stuck,), store, max_steps_per_rung=5).run()
    assert _outcomes(report) == [("stuck", RungOutcome.FAILED)]
    assert "step budget" in report.runs[0].detail
    assert stuck.converge_calls == 5
    assert store.verified_rungs() == frozenset()


# ── Crash resume: converged-but-unrecorded heals by verify-then-record ──────


def test_crash_between_converge_and_record_heals_on_next_run(store: CompletionStore) -> None:
    """Simulate a crash after converge but before record: the rung's state is
    converged, the store has no row. Position stays at the last VERIFIED rung
    until the next run verifies and records — never trusting the crash."""
    crashed = ScriptedRung("crashed", done=1)  # converged on disk, unrecorded
    assert store.ladder_position(("crashed",)) == 0  # pre-heal: not verified

    report = _runner((crashed,), store).run()
    assert _outcomes(report) == [("crashed", RungOutcome.RECORDED)]
    assert crashed.verify_calls == 1  # healed via verify, not assumed
    assert crashed.converge_calls == 0  # nothing to redo
    assert report.position == 1


def test_converged_but_unrecorded_with_failing_verify_stops(store: CompletionStore) -> None:
    """The heal path still runs through the guard: a converged-looking rung
    whose verify fails is VERIFY_FAILED, not silently recorded."""
    liar = ScriptedRung("liar", done=1, verify_result=False)
    report = _runner((liar,), store).run()
    assert _outcomes(report) == [("liar", RungOutcome.VERIFY_FAILED)]
    assert store.verified_rungs() == frozenset()


def test_recorded_rung_that_goes_pending_again_reconverges(store: CompletionStore) -> None:
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


def test_not_applicable_rung_is_skipped_without_record(store: CompletionStore) -> None:
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


def test_pending_sweep_is_read_only(store: CompletionStore) -> None:
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
    import importlib.metadata

    from nexus.upgrade_ladder.runner import _installed_package_version

    def _boom(_name: str) -> str:
        raise RuntimeError("metadata exploded")

    monkeypatch.setattr(importlib.metadata, "version", _boom)
    assert _installed_package_version() == "unknown"


# ── Progress reporting ───────────────────────────────────────────────────────


def test_runner_emits_walk_events(store: CompletionStore) -> None:
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
