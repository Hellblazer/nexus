# SPDX-License-Identifier: AGPL-3.0-or-later
"""nexus-dcbof: the time limit on the detached ``nx upgrade --auto`` child.

The child stops STARTING work at safe points: before each rung that has work,
and, inside the production RDR-192 rung, between census calls and between
backfill collections. A call in flight is never interrupted. Clocks are fixed
(a value the fakes advance), so nothing here sleeps.

``TestRealRegistryShape`` is the test that matters most: production
``default_registry()`` holds ONE long-running rung (the RDR-192 census; the
aspect queue rung skips at once under the default flag), so a between-rungs
check alone bounds nothing. It drives the real rung class and the real
``_default_census`` against a fake engine client whose calls advance the injected clock.
"""
from __future__ import annotations

import contextlib
from dataclasses import dataclass, field

import pytest
import structlog
from click.testing import CliRunner

import nexus.commands.upgrade as upgrade_mod
from nexus.cli import main
from nexus.upgrade_ladder.completion import CompletionRecord
import nexus.db as nexus_db
from nexus.upgrade_ladder.deadline import (
    AUTO_DEADLINE_ENV,
    DEFAULT_AUTO_DEADLINE_S,
    MIN_AUTO_DEADLINE_S,
    Deadline,
    active_deadline,
    active_expired,
    auto_deadline_seconds,
    bound,
)
from nexus.upgrade_ladder.protocol import ConvergeOutcome, ConvergeResult, RungStatus
from nexus.upgrade_ladder.registry import LadderRegistry, default_registry
from nexus.upgrade_ladder.rungs.rdr192_manifest_backfill import (
    Rdr192ManifestBackfillRung,
)
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


class RaisingDetectRung:
    name = "raiser"

    def detect(self) -> RungStatus:
        raise RuntimeError("detect blew up")

    def converge(self, report: object) -> ConvergeResult:  # pragma: no cover - never reached
        raise AssertionError("converge must not run")

    def verify(self) -> bool:  # pragma: no cover - never reached
        return False


class TestRaisingDetectAfterDeadline:
    def test_a_raising_detect_is_reported_failed_not_deferred(self) -> None:
        clock = FakeClock()
        deadline = Deadline(5.0, clock=clock)
        clock.now = 5.0  # already past the ceiling
        report = _runner((RaisingDetectRung(),), Ledger(), deadline).run()
        assert _outcomes(report) == [("raiser", RungOutcome.FAILED)]
        assert "detect raised" in report.runs[0].detail
        assert report.hard_failed


class TestEnvParsing:
    def test_default_is_fifteen_minutes(self) -> None:
        assert DEFAULT_AUTO_DEADLINE_S == 900.0
        assert auto_deadline_seconds({}) == 900.0

    def test_override_is_read(self) -> None:
        assert auto_deadline_seconds({AUTO_DEADLINE_ENV: "120"}) == 120.0
        assert auto_deadline_seconds({AUTO_DEADLINE_ENV: "60"}) == MIN_AUTO_DEADLINE_S == 60.0

    @pytest.mark.parametrize(
        "bad", ["abc", "0", "-5", "nan", "inf", "1e999", "0.5", "1", "59.9"]
    )
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
        monkeypatch.setenv(AUTO_DEADLINE_ENV, "90")
        assert auto_deadline_seconds() == 90.0


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

    @pytest.mark.t2_tenant
    def test_interactive_upgrade_has_no_deadline(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv(AUTO_DEADLINE_ENV, "1")
        called: list[bool] = []
        monkeypatch.setattr(upgrade_mod, "_auto_deadline", lambda: called.append(True))
        seen = self._capture(monkeypatch)
        result = CliRunner().invoke(main, ["upgrade"])
        assert result.exit_code == 0, result.output
        assert seen == [None]
        assert called == [], "the interactive path must not even build a deadline"

    def test_a_failure_building_the_deadline_still_exits_zero(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """``--auto`` promises exit 0 always, so the deadline is built inside
        the command's try and a failure there is swallowed like any other."""

        def boom() -> None:
            raise RuntimeError("deadline construction failed")

        monkeypatch.setattr(upgrade_mod, "_auto_deadline", boom)
        seen = self._capture(monkeypatch)
        result = CliRunner().invoke(main, ["upgrade", "--auto"])
        assert result.exit_code == 0, result.output
        assert seen == []

    @pytest.mark.t2_tenant
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


class NoWorkRung:
    """A rung with nothing to do: not applicable, or converged and recorded."""

    def __init__(self, name: str, *, applicable: bool = True) -> None:
        self.name = name
        self.applicable = applicable
        self.converge_calls = 0

    def detect(self) -> RungStatus:
        return RungStatus(applicable=self.applicable, converged=True)

    def converge(self, report: object) -> ConvergeResult:  # pragma: no cover - must not run
        self.converge_calls += 1
        return ConvergeResult(ConvergeOutcome.COMPLETED)

    def verify(self) -> bool:
        return True


class TestRungsWithNothingToDo:
    def test_an_expired_deadline_does_not_defer_a_rung_that_needs_no_work(self) -> None:
        clock = FakeClock()
        deadline = Deadline(10.0, clock=clock)
        clock.now = 11.0
        recorded = NoWorkRung("recorded")
        na = NoWorkRung("na", applicable=False)
        work, later_noop = TimedRung("work", clock), NoWorkRung("later")
        ledger = Ledger()
        ledger.record_verified("recorded", package_version="9.9.9")
        ledger.record_verified("later", package_version="9.9.9")
        with structlog.testing.capture_logs() as logs:
            report = _runner((recorded, na, work, later_noop), ledger, deadline).run()
        assert _outcomes(report) == [
            ("recorded", RungOutcome.ALREADY_RECORDED),
            ("na", RungOutcome.SKIPPED_NOT_APPLICABLE),
            ("work", RungOutcome.DEFERRED),
            ("later", RungOutcome.NOT_ATTEMPTED),
        ]
        assert work.converge_calls == 0
        (event,) = [e for e in logs if e["event"] == "upgrade_auto_deadline_reached"]
        assert event["next_rung"] == "work"
        assert event["skipped"] == ["work"], "a rung that needs no work is not 'skipped'"

    def test_an_expired_deadline_with_only_finished_rungs_is_a_converged_walk(self) -> None:
        clock = FakeClock()
        deadline = Deadline(10.0, clock=clock)
        clock.now = 11.0
        ledger = Ledger()
        ledger.record_verified("a", package_version="9.9.9")
        report = _runner((NoWorkRung("a"), NoWorkRung("b", applicable=False)), ledger, deadline).run()
        assert report.converged and not report.hard_failed


class TestBoundDeadline:
    def test_the_runner_binds_the_deadline_for_the_rung_and_unbinds_it(self) -> None:
        clock = FakeClock()
        seen: list[object] = []

        class Probe(TimedRung):
            def converge(self, report: object) -> ConvergeResult:
                seen.append(active_deadline())
                return super().converge(report)

        deadline = Deadline(900.0, clock=clock)
        _runner((Probe("p", clock),), Ledger(), deadline).run()
        assert seen == [deadline]
        assert active_deadline() is None and not active_expired()

    def test_no_deadline_binds_nothing(self) -> None:
        with bound(None):
            assert active_deadline() is None and not active_expired()

    def test_active_expired_follows_the_bound_clock(self) -> None:
        clock = FakeClock()
        with bound(Deadline(5.0, clock=clock)):
            assert not active_expired()
            clock.now = 5.0
            assert active_expired()
        assert not active_expired()


# ── the production shape ─────────────────────────────────────────────────────

_BUCKETS = ("legacy-unmanifested", "unclassified", "superseded", "dead-owner", "no-owner")


class SlowEngine:
    """A fake engine client. Every census call costs ``census_cost`` seconds on
    the injected clock; ``legacy`` is the mutable DB state a backfill heals."""

    def __init__(
        self, clock: FakeClock, names: list[str], *, census_cost: float, legacy: dict[str, int] | None = None
    ) -> None:
        self.clock = clock
        self.names = names
        self.census_cost = census_cost
        self.legacy = dict(legacy or {})
        self.censused: list[str] = []

    def list_collections(self, strict: bool = False) -> list[dict]:
        return [{"name": n, "lifecycle_state": "live"} for n in self.names]

    def manifest_less_census(self, collection: str, limit: int = 1, offset: int = 0) -> dict:
        self.censused.append(collection)
        self.clock.now += self.census_cost
        totals = dict.fromkeys(_BUCKETS, 0)
        totals["legacy-unmanifested"] = self.legacy.get(collection, 0)
        return {"totals": totals, "scope_chunk_total": 10}


class MemoryNote:
    def __init__(self) -> None:
        self.note: dict | None = None
        self.saves = 0

    def load(self):
        return self.note

    def save(self, note: dict) -> None:
        self.saves += 1
        self.note = dict(note)

    def clear(self) -> None:
        self.note = None


@contextlib.contextmanager
def _always_locked():
    yield True


def _real_rung(monkeypatch: pytest.MonkeyPatch, engine: SlowEngine, backfill_fn=None):
    """The rung PRODUCTION builds (``default_registry()``), its real
    ``_default_census``, with only the engine client, the completion record,
    the memo, the lock and the backfill call replaced."""
    monkeypatch.setattr(nexus_db, "make_t3", lambda **kw: engine)
    (rung,) = [r for r in default_registry() if isinstance(r, Rdr192ManifestBackfillRung)]
    registry = LadderRegistry((rung,))  # this class is about the RDR-192 rung alone
    assert type(rung) is Rdr192ManifestBackfillRung, "production ladder holds one RDR-192 rung"
    rung.record_fn = lambda: None
    rung.installed_version_fn = lambda: "9.9.9"
    rung.memo = MemoryNote()
    rung.lock_factory = _always_locked
    if backfill_fn is not None:
        rung.backfill_fn = backfill_fn
    return registry, rung


def _walk(registry, ledger, clock: FakeClock, seconds: float):
    return LadderRunner(
        registry, ledger, package_version_fn=lambda: "9.9.9",
        deadline=Deadline(seconds, clock=clock),
    ).run()


class TestRealRegistryShape:
    def test_slow_census_calls_stop_within_one_call_of_the_deadline(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        clock = FakeClock()
        names = [f"knowledge__c{i:02d}" for i in range(98)]
        engine = SlowEngine(clock, names, census_cost=30.0)
        backfilled: list[str] = []
        registry, rung = _real_rung(monkeypatch, engine, lambda c: backfilled.append(c) or 0)
        ledger = Ledger()

        with structlog.testing.capture_logs() as logs:
            report = _walk(registry, ledger, clock, 900.0)

        assert _outcomes(report) == [("rdr192-manifest-backfill", RungOutcome.DEFERRED)]
        assert not report.hard_failed
        (cut,) = [e for e in logs if e["event"] == "rdr192_backfill_deadline_reached"]
        assert cut["log_level"] == "warning"
        assert cut["where"] == "during the census"
        assert 900.0 <= cut["elapsed_s"] <= 930.0
        # 98 calls at 30 s is 2940 s uninterrupted; the walk stopped at the
        # first check at or past 900 s, so it made exactly 900 / 30 calls.
        assert len(engine.censused) == 30
        assert 900.0 <= clock.now <= 900.0 + 30.0
        assert backfilled == [] and ledger.verified_rungs() == frozenset()
        assert rung.memo.saves == 0, "a deadline stop remembers nothing"
        assert "time limit" in report.runs[0].detail

    def test_fast_calls_complete_and_record(self, monkeypatch: pytest.MonkeyPatch) -> None:
        clock = FakeClock()
        engine = SlowEngine(clock, [f"knowledge__c{i:02d}" for i in range(98)], census_cost=0.0)
        registry, _ = _real_rung(monkeypatch, engine)
        ledger = Ledger()

        report = _walk(registry, ledger, clock, 900.0)

        assert _outcomes(report) == [("rdr192-manifest-backfill", RungOutcome.RECORDED)]
        assert report.converged
        assert len(engine.censused) == 98
        assert ledger.verified_rungs() == frozenset({"rdr192-manifest-backfill"})

    def test_backfill_stops_between_collections_and_the_next_session_resumes(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        clock = FakeClock()
        engine = SlowEngine(
            clock, ["knowledge__a", "knowledge__b", "knowledge__c"], census_cost=0.0,
            legacy={"knowledge__a": 5, "knowledge__b": 5, "knowledge__c": 5},
        )
        backfilled: list[str] = []

        def backfill(collection: str) -> int:
            """Each collection costs 500 s and, once finished, is healed in the
            'database' (the engine's state), which outlives the session."""
            backfilled.append(collection)
            clock.now += 500.0
            healed, engine.legacy[collection] = engine.legacy[collection], 0
            return healed

        registry, rung = _real_rung(monkeypatch, engine, backfill)
        ledger = Ledger()

        with structlog.testing.capture_logs() as logs:
            first = _walk(registry, ledger, clock, 900.0)

        assert _outcomes(first) == [("rdr192-manifest-backfill", RungOutcome.DEFERRED)]
        (cut,) = [e for e in logs if e["event"] == "rdr192_backfill_deadline_reached"]
        assert cut["where"] == "in pass 1 before backfilling knowledge__c"
        assert cut["elapsed_s"] == 1000.0
        assert backfilled == ["knowledge__a", "knowledge__b"], "c is not started once 1000 s > 900 s"
        assert engine.legacy == {"knowledge__a": 0, "knowledge__b": 0, "knowledge__c": 5}
        assert rung.memo.saves == 0 and ledger.verified_rungs() == frozenset()

        # Next session: a fresh deadline, same database. The re-census sees only c.
        second = _walk(registry, ledger, clock, 900.0)

        assert _outcomes(second) == [("rdr192-manifest-backfill", RungOutcome.RECORDED)]
        assert backfilled == ["knowledge__a", "knowledge__b", "knowledge__c"], "a and b are not redone"
        assert engine.legacy == dict.fromkeys(engine.legacy, 0)

    def test_the_rung_itself_defers_at_its_safe_points_under_a_bound_deadline(self) -> None:
        """Called directly (no runner), with injected seams: an expired bound
        deadline defers before the census and before any backfill."""
        clock = FakeClock()
        calls: list[str] = []
        rung = Rdr192ManifestBackfillRung(
            census_fn=lambda: calls.append("census"),  # type: ignore[arg-type,return-value]
            backfill_fn=lambda c: calls.append(c) or 0,
            record_fn=lambda: None,
            installed_version_fn=lambda: "9.9.9",
            memo=MemoryNote(),
            lock_factory=_always_locked,
        )
        deadline = Deadline(5.0, clock=clock)
        clock.now = 5.0
        with bound(deadline):
            result = rung.converge(object())  # type: ignore[arg-type]
        assert result.deferred and calls == []
