# SPDX-License-Identifier: AGPL-3.0-or-later
"""nexus-dcbof: the overall ceiling on the detached ``nx upgrade --auto`` child.

The ceiling is checked BEFORE each rung starts, never mid-rung. Clocks are
fixed (a value the rungs advance), so nothing here sleeps.
"""
from __future__ import annotations

from dataclasses import dataclass, field

import pytest
import structlog
from click.testing import CliRunner

import nexus.commands.upgrade as upgrade_mod
from nexus.cli import main
from nexus.upgrade_ladder.completion import CompletionRecord
from nexus.upgrade_ladder.deadline import (
    AUTO_DEADLINE_ENV,
    DEFAULT_AUTO_DEADLINE_S,
    Deadline,
    auto_deadline_seconds,
)
from nexus.upgrade_ladder.protocol import ConvergeOutcome, ConvergeResult, RungStatus
from nexus.upgrade_ladder.registry import LadderRegistry
from nexus.upgrade_ladder.runner import LadderRunner, RungOutcome


class FakeClock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now


@dataclass
class TimedRung:
    """One-batch rung whose converge costs ``cost`` seconds on the clock."""

    name: str
    clock: FakeClock
    cost: float = 0.0
    done: bool = False
    converge_calls: int = 0

    def detect(self) -> RungStatus:
        return RungStatus(applicable=True, converged=self.done)

    def converge(self, report: object) -> ConvergeResult:
        self.converge_calls += 1
        self.clock.now += self.cost
        self.done = True
        return ConvergeResult(ConvergeOutcome.COMPLETED)

    def verify(self) -> bool:
        return self.done


@dataclass
class Ledger:
    records: dict[str, CompletionRecord] = field(default_factory=dict)

    def record_verified(self, rung_name: str, *, package_version: str, detail: str = "") -> None:
        self.records[rung_name] = CompletionRecord(
            rung_name=rung_name, verified_at="t0", package_version=package_version, detail=detail
        )

    def verified_rungs(self) -> frozenset[str]:
        return frozenset(self.records)

    def completions(self) -> dict[str, CompletionRecord]:
        return dict(self.records)


def _runner(rungs, ledger, deadline=None) -> LadderRunner:
    return LadderRunner(
        LadderRegistry(tuple(rungs)), ledger, package_version_fn=lambda: "9.9.9", deadline=deadline
    )


def _outcomes(report):
    return [(r.name, r.outcome) for r in report.runs]


class TestRunnerBoundary:
    def test_no_deadline_hit_every_rung_runs(self) -> None:
        clock = FakeClock()
        a, b, c = (TimedRung(n, clock, cost=10.0) for n in "abc")
        report = _runner((a, b, c), Ledger(), Deadline(900.0, clock=clock)).run()
        assert [r.outcome for r in report.runs] == [RungOutcome.RECORDED] * 3
        assert report.converged
        assert clock.now == 30.0

    def test_deadline_passing_mid_run_lets_the_current_rung_finish_and_starts_no_more(self) -> None:
        clock = FakeClock()
        # a's converge carries the clock past the 100 s ceiling: it must still
        # be verified and recorded; b and c must not start.
        a = TimedRung("a", clock, cost=150.0)
        b, c = TimedRung("b", clock), TimedRung("c", clock)
        ledger = Ledger()
        with structlog.testing.capture_logs() as logs:
            report = _runner((a, b, c), ledger, Deadline(100.0, clock=clock)).run()

        assert _outcomes(report) == [
            ("a", RungOutcome.RECORDED),
            ("b", RungOutcome.DEFERRED),
            ("c", RungOutcome.NOT_ATTEMPTED),
        ]
        assert a.converge_calls == 1 and a.done
        assert b.converge_calls == 0 and c.converge_calls == 0
        assert ledger.verified_rungs() == frozenset({"a"})
        assert not report.converged
        assert not report.hard_failed, "a deadline stop is resumable, not a failure"
        assert report.position == 1

        (event,) = [e for e in logs if e["event"] == "upgrade_auto_deadline_reached"]
        assert event["log_level"] == "warning"
        assert event["next_rung"] == "b"
        assert event["skipped"] == ["b", "c"]
        assert event["elapsed_s"] == 150.0
        assert event["deadline_s"] == 100.0

    def test_deadline_already_past_at_the_first_rung_runs_nothing(self) -> None:
        clock = FakeClock()
        deadline = Deadline(5.0, clock=clock)
        clock.now = 5.0  # exactly at the ceiling counts as passed
        a = TimedRung("a", clock)
        report = _runner((a,), Ledger(), deadline).run()
        assert _outcomes(report) == [("a", RungOutcome.DEFERRED)]
        assert a.converge_calls == 0

    def test_no_deadline_object_means_no_ceiling(self) -> None:
        clock = FakeClock()
        a = TimedRung("a", clock, cost=10**9)
        b = TimedRung("b", clock)
        report = _runner((a, b), Ledger()).run()
        assert [r.outcome for r in report.runs] == [RungOutcome.RECORDED] * 2

    def test_a_resumed_run_continues_from_the_skipped_rung(self) -> None:
        clock = FakeClock()
        a, b, c = TimedRung("a", clock, cost=150.0), TimedRung("b", clock), TimedRung("c", clock)
        ledger = Ledger()
        first = _runner((a, b, c), ledger, Deadline(100.0, clock=clock)).run()
        assert ledger.verified_rungs() == frozenset({"a"})
        assert not first.converged

        # Next session: a fresh deadline, same ledger and rung state.
        second = _runner((a, b, c), ledger, Deadline(100.0, clock=clock)).run()
        assert _outcomes(second) == [
            ("a", RungOutcome.ALREADY_RECORDED),
            ("b", RungOutcome.RECORDED),
            ("c", RungOutcome.RECORDED),
        ]
        assert second.converged
        assert a.converge_calls == 1, "the finished rung is not redone"
        assert ledger.verified_rungs() == frozenset({"a", "b", "c"})


class TestEnvParsing:
    def test_default_is_fifteen_minutes(self) -> None:
        assert DEFAULT_AUTO_DEADLINE_S == 900.0
        assert auto_deadline_seconds({}) == 900.0

    def test_override_is_read(self) -> None:
        assert auto_deadline_seconds({AUTO_DEADLINE_ENV: "120"}) == 120.0
        assert auto_deadline_seconds({AUTO_DEADLINE_ENV: "0.5"}) == 0.5

    @pytest.mark.parametrize("bad", ["abc", "0", "-5", "nan", "inf", "1e999"])
    def test_a_bad_value_falls_back_with_a_warning(self, bad: str) -> None:
        with structlog.testing.capture_logs() as logs:
            assert auto_deadline_seconds({AUTO_DEADLINE_ENV: bad}) == DEFAULT_AUTO_DEADLINE_S
        (event,) = [e for e in logs if e["event"] == "upgrade_auto_deadline_env_invalid"]
        assert event["value"] == bad and event["log_level"] == "warning"

    def test_blank_value_is_unset_without_a_warning(self) -> None:
        with structlog.testing.capture_logs() as logs:
            assert auto_deadline_seconds({AUTO_DEADLINE_ENV: " "}) == DEFAULT_AUTO_DEADLINE_S
        assert logs == []

    def test_reads_the_process_environment_by_default(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv(AUTO_DEADLINE_ENV, "42")
        assert auto_deadline_seconds() == 42.0


class TestCommandWiring:
    """``nx upgrade`` builds the deadline for ``--auto`` only."""

    @pytest.fixture(autouse=True)
    def _isolate(self, tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("NEXUS_CONFIG_DIR", str(tmp_path))
        monkeypatch.setattr(upgrade_mod, "_cycle_supervised_daemons_to_current", lambda **kw: None)
        monkeypatch.setattr(upgrade_mod, "_converge_preconditions", lambda **kw: None)
        monkeypatch.setattr(upgrade_mod, "_install_beads_prime_best_effort", lambda **kw: None)

    def _capture(self, monkeypatch: pytest.MonkeyPatch) -> list[object]:
        seen: list[object] = []
        monkeypatch.setattr(upgrade_mod, "_run_ladder", lambda **kw: seen.append(kw["deadline"]))
        return seen

    def test_auto_builds_a_deadline_from_the_env(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv(AUTO_DEADLINE_ENV, "77")
        seen = self._capture(monkeypatch)
        result = CliRunner().invoke(main, ["upgrade", "--auto"])
        assert result.exit_code == 0, result.output
        (deadline,) = seen
        assert isinstance(deadline, Deadline) and deadline.seconds == 77.0

    def test_interactive_upgrade_has_no_deadline(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv(AUTO_DEADLINE_ENV, "1")
        called: list[bool] = []
        monkeypatch.setattr(upgrade_mod, "_auto_deadline", lambda: called.append(True))
        seen = self._capture(monkeypatch)
        result = CliRunner().invoke(main, ["upgrade"])
        assert result.exit_code == 0, result.output
        assert seen == [None]
        assert called == [], "the interactive path must not even build a deadline"

    def test_dry_run_has_no_deadline(self, monkeypatch: pytest.MonkeyPatch) -> None:
        seen = self._capture(monkeypatch)
        result = CliRunner().invoke(main, ["upgrade", "--dry-run"])
        assert result.exit_code == 0, result.output
        assert seen == [None]

    def test_auto_run_stops_at_the_boundary_and_still_exits_zero(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Through ``nx upgrade --auto`` and the real ``_run_ladder``: the
        deadline passes during rung ``a``; ``b`` is not started; exit 0."""
        from nexus.upgrade_ladder import registry as ladder_registry

        clock = FakeClock()
        a, b = TimedRung("a", clock, cost=500.0), TimedRung("b", clock)
        ledger = Ledger()
        monkeypatch.setattr(ladder_registry, "default_registry", lambda **kw: LadderRegistry((a, b)))
        monkeypatch.setattr(upgrade_mod, "_auto_deadline", lambda: Deadline(100.0, clock=clock))
        real_run_ladder = upgrade_mod._run_ladder
        monkeypatch.setattr(
            upgrade_mod, "_run_ladder", lambda **kw: real_run_ladder(**kw, _ledger_fn=lambda: ledger)
        )

        # The event itself is pinned in TestRunnerBoundary: the root CLI group
        # reconfigures structlog, so capture_logs cannot see it from here.
        result = CliRunner().invoke(main, ["upgrade", "--auto"])

        assert result.exit_code == 0, result.output
        assert a.converge_calls == 1 and b.converge_calls == 0
        assert ledger.verified_rungs() == frozenset({"a"})
