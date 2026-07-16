# SPDX-License-Identifier: AGPL-3.0-or-later
"""RDR-185 P0.1 (nexus-n7u38.1): Rung Protocol conformance.

A synthetic fixture rung proves the detect → converge → verify seam
end-to-end: read-only detection (the dry-run-truth precedent,
``resolve_pending_steps``), batched/resumable convergence (RDR-178
unattended-capable constraint), and a read-only verify whose pass is the
ONLY thing that lets a runner record completion (RDR-142 — the runner
guard itself is bead .3; this file pins the protocol semantics the guard
relies on).
"""
from __future__ import annotations

from dataclasses import dataclass, field

import pytest

from nexus.upgrade_ladder.protocol import (
    ConvergeOutcome,
    ConvergeResult,
    ProgressReporter,
    Rung,
    RungStatus,
)


@dataclass
class RecordingReporter:
    """Test double for :class:`ProgressReporter` — records every emit."""

    events: list[tuple[str, dict[str, object]]] = field(default_factory=list)

    def emit(self, event: str, **fields: object) -> None:
        self.events.append((event, fields))


@dataclass
class FixtureRung:
    """Synthetic rung: converge N work units toward a target, one batch per
    ``converge`` call — the smallest shape that exercises resumability.

    ``done`` is the persisted progress floor (survives "crashes": a fresh
    ``converge`` call continues from it rather than redoing work).
    ``work_log`` records each unit exactly once so tests can assert no unit
    is ever re-done (idempotency) and that ``detect``/``verify`` never work.
    """

    name: str = "fixture"
    target: int = 3
    batch_size: int = 1
    applicable: bool = True
    done: int = 0
    work_log: list[int] = field(default_factory=list)

    def detect(self) -> RungStatus:
        if not self.applicable:
            return RungStatus(applicable=False, converged=False)
        if self.done >= self.target:
            return RungStatus(applicable=True, converged=True)
        return RungStatus(
            applicable=True,
            converged=False,
            pending_detail=f"{self.target - self.done} units remaining",
        )

    def converge(self, report: ProgressReporter) -> ConvergeResult:
        if self.done >= self.target:
            return ConvergeResult(ConvergeOutcome.COMPLETED, detail="already converged")
        batch_end = min(self.done + self.batch_size, self.target)
        for unit in range(self.done, batch_end):
            self.work_log.append(unit)
        self.done = batch_end
        report.emit("fixture_batch_converged", done=self.done, target=self.target)
        if self.done >= self.target:
            return ConvergeResult(ConvergeOutcome.COMPLETED)
        return ConvergeResult(ConvergeOutcome.RESUMABLE, detail=f"at {self.done}/{self.target}")

    def verify(self) -> bool:
        return self.done >= self.target


def test_fixture_rung_satisfies_protocol() -> None:
    """The Protocol is runtime-checkable: structural conformance, no base class."""
    assert isinstance(FixtureRung(), Rung)
    assert isinstance(RecordingReporter(), ProgressReporter)


def test_non_conforming_object_is_not_a_rung() -> None:
    """Non-vacuity: an object missing the seam methods must NOT pass."""

    class NotARung:
        name = "nope"

    assert not isinstance(NotARung(), Rung)


def test_detect_converge_verify_end_to_end() -> None:
    """The full seam: pending → converge to completion → verify → converged."""
    rung = FixtureRung(target=3, batch_size=3)
    reporter = RecordingReporter()

    status = rung.detect()
    assert status.applicable and not status.converged
    assert status.pending
    assert "remaining" in status.pending_detail

    result = rung.converge(reporter)
    assert result.outcome is ConvergeOutcome.COMPLETED
    assert result.completed

    assert rung.verify() is True
    after = rung.detect()
    assert after.converged and not after.pending


def test_converge_is_resumable_across_interruptions() -> None:
    """RDR-178: a long rung converges in batches; a fresh converge call after
    an 'interruption' resumes from persisted progress and never redoes a unit."""
    rung = FixtureRung(target=3, batch_size=1)
    reporter = RecordingReporter()

    first = rung.converge(reporter)
    assert first.outcome is ConvergeOutcome.RESUMABLE
    assert not first.completed
    assert rung.verify() is False  # mid-flight: verify must NOT pass

    # "Crash" = drop everything except the rung's persisted state, then re-run.
    outcomes = [rung.converge(RecordingReporter()).outcome for _ in range(2)]
    assert outcomes == [ConvergeOutcome.RESUMABLE, ConvergeOutcome.COMPLETED]

    # Every unit done exactly once — resumption never duplicated work.
    assert rung.work_log == [0, 1, 2]
    assert rung.verify() is True


def test_converge_on_converged_rung_is_a_noop() -> None:
    """Idempotency: re-running a converged rung completes without new work."""
    rung = FixtureRung(target=2, batch_size=2)
    rung.converge(RecordingReporter())
    assert rung.verify() is True

    again = rung.converge(RecordingReporter())
    assert again.outcome is ConvergeOutcome.COMPLETED
    assert rung.work_log == [0, 1]  # no duplicate units


def test_detect_is_read_only() -> None:
    """detect() is the doctor / dry-run surface — it must never do work."""
    rung = FixtureRung(target=2)
    for _ in range(3):
        rung.detect()
    assert rung.work_log == []
    assert rung.done == 0


def test_not_applicable_rung_is_never_pending() -> None:
    """The f0pmd detect→skip gate: a rung N/A for this install/mode reports
    not-pending so the walk skips it without consent or work."""
    rung = FixtureRung(applicable=False)
    status = rung.detect()
    assert not status.applicable
    assert not status.pending


def test_progress_reporter_receives_batch_events() -> None:
    rung = FixtureRung(target=2, batch_size=1)
    reporter = RecordingReporter()
    rung.converge(reporter)
    rung.converge(reporter)
    events = [name for name, _ in reporter.events]
    assert events == ["fixture_batch_converged", "fixture_batch_converged"]
    assert reporter.events[-1][1] == {"done": 2, "target": 2}


@pytest.mark.parametrize(
    ("applicable", "converged", "pending"),
    [
        (True, False, True),   # applicable and behind → pending
        (True, True, False),   # applicable and current → nothing to do
        (False, False, False), # N/A → never pending (detect→skip)
        (False, True, False),  # N/A → converged flag is irrelevant
    ],
)
def test_rung_status_pending_semantics(applicable: bool, converged: bool, pending: bool) -> None:
    assert RungStatus(applicable=applicable, converged=converged).pending is pending


def test_status_and_result_are_immutable() -> None:
    """Verdict objects are frozen — a consumer can never 'fix up' a verdict
    (the settable-position bug class RDR-185 bans at every layer)."""
    status = RungStatus(applicable=True, converged=False)
    with pytest.raises(AttributeError):
        status.converged = True  # type: ignore[misc]
    result = ConvergeResult(ConvergeOutcome.COMPLETED)
    with pytest.raises(AttributeError):
        result.outcome = ConvergeOutcome.RESUMABLE  # type: ignore[misc]
