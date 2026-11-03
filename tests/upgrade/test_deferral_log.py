# SPDX-License-Identifier: AGPL-3.0-or-later
"""nexus-0sm1t: a rung that defers on every run is counted, and ``nx doctor`` names it.

Since nexus-dfemn the walk goes on past a DEFERRED rung, so the deferring rung stays
pending, silently under ``--auto``. The deferral log (a T2 note, one per tenant) counts
consecutive deferrals per rung; the doctor row leads with REPEATEDLY DEFERRED (a warning
still) at ``ESCALATE_AFTER`` deferrals over ``ESCALATE_MIN_SPAN``. The counting rules run
on an in-memory note (each tenant a test mints costs the suite's storage-touch ceiling);
one test each keeps the real T2 round trip, the ``_run_ladder`` wiring and the doctor row.
Two logs sharing one note stand in for two machines on one tenant.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

import click
import pytest

import nexus.upgrade_ladder.deferral_log as deferral_log_mod
import nexus.upgrade_ladder.registry as ladder_registry
from nexus.commands.upgrade import _run_ladder
from nexus.hooks import upgrade_auto
from nexus.health import _check_pending_rungs
from nexus.upgrade_ladder.census_progress import PROGRESS_PROJECT, T2NoteStore
from nexus.upgrade_ladder.completion import CompletionRecord
from nexus.upgrade_ladder.deadline import Deadline
from nexus.upgrade_ladder.deferral_log import (
    CHILD_LOG_RELATIVE,
    DEFERRAL_TITLE,
    ESCALATE_AFTER,
    ESCALATE_MIN_SPAN,
    DeferralLog,
    counted_deferrals,
)
from nexus.upgrade_ladder.protocol import ConvergeOutcome, ConvergeResult, ProgressReporter, RungStatus
from nexus.upgrade_ladder.registry import LadderRegistry
from nexus.upgrade_ladder.runner import LadderRunReport, RungOutcome, RungRun
from tests.upgrade.conftest import InMemoryCompletionLedger


_T0 = datetime(2026, 11, 3, 9, 0, tzinfo=UTC)


class Clock:
    def __init__(self, start: datetime = _T0) -> None:
        self.now = start

    def __call__(self) -> datetime:
        return self.now

    def tick(self, hours: float = 1) -> None:
        self.now += timedelta(hours=hours)


def _report(**outcomes: tuple[RungOutcome, str]) -> LadderRunReport:
    return LadderRunReport(runs=tuple(RungRun(n, o, d) for n, (o, d) in outcomes.items()), position=0)


def _deferred(detail: str = "census timed out") -> tuple[RungOutcome, str]:
    return (RungOutcome.DEFERRED, detail)


def _done(rung: str, at: datetime) -> dict[str, CompletionRecord]:
    """The ledger's completions after *rung* converged at *at*."""
    return {rung: CompletionRecord(rung_name=rung, verified_at=at.isoformat(), package_version="v")}


class _MemoryStore:
    """The note store's three calls, in memory: the counting rules need no storage. Two logs on one store are two
    machines on one tenant."""

    def __init__(self) -> None:
        self.note: dict | None = None

    def load(self):
        return self.note

    def save(self, note):
        self.note = json.loads(json.dumps(note))  # what a T2 round trip would hand back

    def clear(self):
        self.note = None


class _RaisingStore:
    """A note store that must not be reached."""

    def load(self):
        raise AssertionError("the note was read")

    def save(self, note):
        raise AssertionError("the note was written")

    def clear(self):
        raise AssertionError("the note was cleared")


@pytest.fixture
def clock() -> Clock:
    return Clock()


@pytest.fixture
def mem_log(clock: Clock) -> DeferralLog:
    return DeferralLog(_MemoryStore(), now_fn=clock)


# ── the counting rules ───────────────────────────────────────────────────────


def test_a_deferral_counts_up_keeping_the_first_stamp_and_the_latest_detail(mem_log: DeferralLog, clock: Clock) -> None:
    mem_log.record(_report(slow=_deferred("first")), {})
    clock.tick()
    mem_log.record(_report(slow=_deferred("second")), {})
    clock.tick()
    mem_log.record(_report(slow=_deferred("third")), {})

    entry = mem_log.deferrals()["slow"]
    assert entry.count == 3
    assert entry.since == _T0.isoformat(timespec="seconds")
    assert entry.last == (_T0 + timedelta(hours=2)).isoformat()
    assert entry.detail == "third"


def test_the_deadline_stop_of_a_rung_that_never_started_is_not_a_deferral() -> None:
    stop = RungRun("b", RungOutcome.DEFERRED, "auto deadline of 900s reached after 901.0s; resumes on the next run", deadline_stop=True)
    real = RungRun("a", RungOutcome.DEFERRED, "census timed out")
    assert counted_deferrals(LadderRunReport(runs=(real, stop), position=0)) == [real]

    # a walk that holds only the stop leaves the note alone: nothing is read, written or cleared
    DeferralLog(_RaisingStore()).record(LadderRunReport(runs=(stop,), position=0), _done("b", _T0))

    log = DeferralLog(_MemoryStore())
    log.record(LadderRunReport(runs=(real, stop), position=0), {})
    assert set(log.deferrals()) == {"a"}  # the rung the budget ran out ahead of is not blamed


@pytest.mark.parametrize(
    "outcome",
    [
        RungOutcome.RECORDED, RungOutcome.ALREADY_RECORDED, RungOutcome.SKIPPED_NOT_APPLICABLE,
        RungOutcome.NOT_ATTEMPTED, RungOutcome.FAILED, RungOutcome.VERIFY_FAILED,
    ],
    ids=lambda o: o.value,
)
def test_a_walk_with_no_counted_deferral_touches_no_storage(outcome: RungOutcome) -> None:
    DeferralLog(_RaisingStore()).record(_report(a=(outcome, "x"), b=(RungOutcome.RECORDED, "")), _done("a", _T0))


@pytest.mark.parametrize(
    "outcome", [RungOutcome.NOT_ATTEMPTED, RungOutcome.FAILED, RungOutcome.VERIFY_FAILED], ids=lambda o: o.value
)
def test_a_rung_that_was_not_walked_or_failed_keeps_its_streak(mem_log: DeferralLog, outcome: RungOutcome) -> None:
    mem_log.record(_report(slow=_deferred("kept")), {})
    mem_log.record(_report(slow=_deferred("kept")), {})
    before = mem_log.deferrals()["slow"]

    mem_log.record(_report(slow=(outcome, "something else"), other=_deferred()), {})

    assert mem_log.deferrals()["slow"] == before
    assert mem_log.deferrals()["other"].count == 1


def test_a_completion_newer_than_the_last_deferral_restarts_the_streak(mem_log: DeferralLog, clock: Clock) -> None:
    mem_log.record(_report(slow=_deferred()), {})
    clock.tick()
    mem_log.record(_report(slow=_deferred()), {})
    last = clock.now  # the second deferral
    clock.tick(5)

    mem_log.record(_report(slow=_deferred("again")), _done("slow", last + timedelta(hours=1)))

    entry = mem_log.deferrals()["slow"]
    assert entry.count == 1 and entry.since == clock.now.isoformat(timespec="seconds") and entry.detail == "again"


@pytest.mark.parametrize(
    "verified_at",
    ["earlier", "equal", "unparseable"],
)
def test_a_completion_that_is_not_newer_leaves_the_streak_going(mem_log: DeferralLog, clock: Clock, verified_at: str) -> None:
    mem_log.record(_report(slow=_deferred()), {})
    clock.tick()
    mem_log.record(_report(slow=_deferred()), {})
    last = clock.now
    clock.tick(5)
    stamp = {"earlier": (last - timedelta(hours=3)).isoformat(), "equal": last.isoformat(), "unparseable": "t0"}[verified_at]
    completion = {"slow": CompletionRecord(rung_name="slow", verified_at=stamp, package_version="v")}

    mem_log.record(_report(slow=_deferred()), completion)

    entry = mem_log.deferrals()["slow"]
    assert entry.count == 3 and entry.since == _T0.isoformat(timespec="seconds")


def test_a_completion_from_the_same_second_but_before_the_deferral_is_not_newer(mem_log: DeferralLog, clock: Clock) -> None:
    """``last`` keeps its fraction: cut to whole seconds it would sit before a completion that preceded the
    deferral inside the same second, and read that completion as newer."""
    clock.now = _T0 + timedelta(seconds=0.9)
    mem_log.record(_report(slow=_deferred()), {})
    clock.now = _T0 + timedelta(minutes=5)

    mem_log.record(_report(slow=_deferred()), _done("slow", _T0 + timedelta(seconds=0.5)))

    assert mem_log.deferrals()["slow"].count == 2


def test_an_entry_whose_rung_converged_since_is_dropped_in_the_same_write(mem_log: DeferralLog, clock: Clock) -> None:
    mem_log.record(_report(slow=_deferred(), other=_deferred()), {})
    clock.tick()
    converged_at = clock.now
    clock.tick()

    mem_log.record(_report(slow=_deferred()), _done("other", converged_at))

    assert set(mem_log.deferrals()) == {"slow"}
    assert mem_log.deferrals()["slow"].count == 2


def test_two_machines_on_one_tenant_the_other_ones_convergence_resets_the_streak(clock: Clock) -> None:
    """Machine A defers twice; machine B converges the rung (a completion, no deferral); A defers again. A flag
    kept on A alone could not know, and A showed a streak of three over the 24 hours."""
    note = _MemoryStore()
    machine_a, machine_b = DeferralLog(note, now_fn=clock), DeferralLog(note, now_fn=clock)

    machine_a.record(_report(slow=_deferred()), {})
    clock.tick()
    machine_a.record(_report(slow=_deferred()), {})
    clock.tick()
    machine_b.record(_report(other=(RungOutcome.RECORDED, "")), {})  # B's walk: nothing to count, nothing touched
    converged_at = clock.now
    clock.tick(30)

    # control: without B's completion in the ledger A would count on to 3 over 31 hours, and escalate
    control = DeferralLog(_MemoryStore(), now_fn=clock)
    note_copy = json.loads(json.dumps(note.note))
    control._store.save(note_copy)
    control.record(_report(slow=_deferred()), {})
    assert set(control.escalated()) == {"slow"}

    machine_a.record(_report(slow=_deferred()), _done("slow", converged_at))

    assert machine_a.deferrals()["slow"].count == 1
    assert machine_a.escalated() == {} and machine_b.escalated() == {}


def test_the_streak_escalates_at_the_count_and_the_span_and_not_before(mem_log: DeferralLog, clock: Clock) -> None:
    assert ESCALATE_AFTER == 3 and ESCALATE_MIN_SPAN == timedelta(hours=24)
    for _ in range(ESCALATE_AFTER):
        mem_log.record(_report(slow=_deferred()), {})
        clock.tick(0.5)
    assert mem_log.deferrals()["slow"].count == ESCALATE_AFTER
    assert mem_log.escalated() == {}, "three deferrals inside a couple of hours are a slow rung, not a stuck one"

    clock.now = _T0 + ESCALATE_MIN_SPAN - timedelta(minutes=1)
    assert mem_log.escalated() == {}
    clock.now = _T0 + ESCALATE_MIN_SPAN
    assert set(mem_log.escalated()) == {"slow"}

    # and the span alone is not enough: two deferrals over a week are below the count
    other = DeferralLog(_MemoryStore(), now_fn=clock)
    other.record(_report(slow=_deferred()), {})
    clock.tick(24 * 7)
    other.record(_report(slow=_deferred()), {})
    assert other.escalated() == {}


def test_the_doctor_text_names_the_count_the_span_the_detail_and_the_child_log(
    mem_log: DeferralLog, clock: Clock, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("NEXUS_CONFIG_DIR", str(tmp_path))
    for _ in range(ESCALATE_AFTER):
        mem_log.record(_report(slow=_deferred("census timed out at aspects")), {})
        clock.tick(10)

    text = mem_log.describe(mem_log.escalated()["slow"])

    assert "rung 'slow'" in text and f"{ESCALATE_AFTER} consecutive" in text
    assert "over 30 h" in text  # three deferrals 10 h apart, read 10 h after the last
    assert "census timed out at aspects" in text
    assert "nx upgrade" in text and "interactively" in text
    assert str(tmp_path / CHILD_LOG_RELATIVE) in text
    assert tmp_path / CHILD_LOG_RELATIVE == upgrade_auto._child_log_path()  # the path the --auto child really writes


def test_a_malformed_note_reads_as_no_streaks_and_the_next_walk_replaces_it(mem_log: DeferralLog) -> None:
    mem_log._store.save({"rungs": {"slow": {"n": "many"}, "bad": 7, "worse": {"n": True, "since": "x", "last": "y", "detail": ""}}})
    assert mem_log.deferrals() == {}
    mem_log.record(_report(slow=_deferred()), {})
    assert set(mem_log.deferrals()) == {"slow"} and mem_log.deferrals()["slow"].count == 1


# ── the real T2 note ─────────────────────────────────────────────────────────


@pytest.mark.t2_tenant
def test_the_streak_round_trips_through_the_real_t2_note(clock: Clock) -> None:
    log = DeferralLog(T2NoteStore(DEFERRAL_TITLE, project=PROGRESS_PROJECT), now_fn=clock)
    for _ in range(ESCALATE_AFTER):
        log.record(_report(slow=_deferred("census timed out")), {})
        clock.tick(10)

    entry = log.deferrals()["slow"]
    assert entry.count == ESCALATE_AFTER and entry.since == _T0.isoformat(timespec="seconds")
    assert set(log.escalated()) == {"slow"}  # 30 h after the first

    log.record(_report(slow=_deferred()), _done("slow", clock.now + timedelta(hours=1)))  # the completion is newer
    assert log.deferrals()["slow"].count == 1


# ── nx upgrade: _run_ladder records the walk ─────────────────────────────────


@dataclass
class _Rung:
    name: str
    outcome: str = "defer"  # defer | complete | fail
    pending: bool = True

    def detect(self) -> RungStatus:
        return RungStatus(applicable=True, converged=not self.pending, pending_detail="3 collections behind")

    def converge(self, report: ProgressReporter) -> ConvergeResult:
        if self.outcome == "defer":
            return ConvergeResult(ConvergeOutcome.DEFERRED, detail="census timed out")
        if self.outcome == "fail":
            raise RuntimeError("boom")
        self.pending = False
        return ConvergeResult(ConvergeOutcome.COMPLETED)

    def verify(self) -> bool:
        return True


def _use(monkeypatch: pytest.MonkeyPatch, *rungs: _Rung) -> None:
    monkeypatch.setattr(ladder_registry, "default_registry", lambda **kw: LadderRegistry(tuple(rungs)))


class _StampedLedger(InMemoryCompletionLedger):
    """A ledger that stamps a completion with the real time, as the engine does."""

    def record_verified(self, rung_name: str, *, package_version: str, detail: str = "") -> None:
        self.records[rung_name] = CompletionRecord(
            rung_name=rung_name, verified_at=datetime.now(UTC).isoformat(), package_version=package_version, detail=detail
        )


def _walk(auto_mode: bool, ledger: InMemoryCompletionLedger | None = None) -> None:
    ledger = ledger or InMemoryCompletionLedger()
    _run_ladder(dry_run=False, auto_mode=auto_mode, _ledger_fn=lambda: ledger)


@pytest.fixture
def wired(monkeypatch: pytest.MonkeyPatch) -> tuple[DeferralLog, list[str]]:
    """``t2_deferral_log()`` handed an in-memory log, and the list of times anything asked for it."""
    log, asked = DeferralLog(_MemoryStore()), []

    def _factory() -> DeferralLog:
        asked.append("asked")
        return log

    monkeypatch.setattr(deferral_log_mod, "t2_deferral_log", _factory)
    return log, asked


@pytest.mark.t2_tenant
def test_a_real_walk_counts_the_deferral_and_a_newer_completion_restarts_it(monkeypatch: pytest.MonkeyPatch) -> None:
    rung = _Rung("slow")
    _use(monkeypatch, rung)
    ledger = _StampedLedger()
    _walk(True, ledger)
    _walk(False, ledger)  # interactive and --auto alike
    assert deferral_log_mod.t2_deferral_log().deferrals()["slow"].count == 2

    rung.outcome = "complete"
    _walk(True, ledger)  # converges: the ledger now holds a completion newer than the last deferral
    assert ledger.verified_rungs() == frozenset({"slow"})
    rung.pending, rung.outcome = True, "defer"
    _walk(True, ledger)
    assert deferral_log_mod.t2_deferral_log().deferrals()["slow"].count == 1


def test_a_clean_walk_and_a_dry_run_do_not_even_ask_for_the_log(
    monkeypatch: pytest.MonkeyPatch, wired: tuple[DeferralLog, list[str]]
) -> None:
    _log, asked = wired
    _use(monkeypatch, _Rung("slow", outcome="complete"))
    _walk(True)
    _use(monkeypatch, _Rung("slow"))
    _run_ladder(dry_run=True, auto_mode=False)
    assert asked == []


def test_a_walk_stopped_by_the_deadline_alone_counts_nothing(
    monkeypatch: pytest.MonkeyPatch, wired: tuple[DeferralLog, list[str]]
) -> None:
    log, asked = wired
    _use(monkeypatch, _Rung("slow"))
    ledger, now = InMemoryCompletionLedger(), [0.0]
    deadline = Deadline(5.0, clock=lambda: now[0])
    now[0] = 10.0  # already past: the first rung with work is the deadline stop
    _run_ladder(dry_run=False, auto_mode=True, _ledger_fn=lambda: ledger, deadline=deadline)
    assert asked == [] and log.deferrals() == {}


def test_a_failed_rung_in_a_walk_leaves_another_rungs_streak(
    monkeypatch: pytest.MonkeyPatch, wired: tuple[DeferralLog, list[str]]
) -> None:
    log, _asked = wired
    slow, bad = _Rung("slow"), _Rung("bad", outcome="fail")
    _use(monkeypatch, slow)
    _walk(True)
    _use(monkeypatch, bad, slow)
    with pytest.raises(click.ClickException):
        _walk(False)
    assert log.deferrals()["slow"].count == 1  # NOT_ATTEMPTED behind the failure: unchanged


def test_a_log_that_raises_changes_neither_the_outcome_nor_the_error(monkeypatch: pytest.MonkeyPatch) -> None:
    def _boom(self: DeferralLog, report: LadderRunReport, completions: object) -> None:
        raise RuntimeError("log exploded")

    monkeypatch.setattr(DeferralLog, "record", _boom)

    _use(monkeypatch, _Rung("slow"))
    _walk(False)  # a deferred-only walk still exits cleanly
    _walk(True)

    _use(monkeypatch, _Rung("bad", outcome="fail"))
    with pytest.raises(click.ClickException, match="bad"):  # a failing walk still raises its own error
        _walk(False)

    ledger = InMemoryCompletionLedger()
    _use(monkeypatch, _Rung("fine", outcome="complete"))
    _walk(True, ledger)
    assert ledger.verified_rungs() == frozenset({"fine"})  # and a converging one is still recorded


# ── nx doctor: the Upgrade ladder row ────────────────────────────────────────


def _ago(**span: float) -> Clock:
    return Clock(datetime.now(UTC) - timedelta(**span))


def _seed(store: object, rung: str, times: int, *, started_ago: timedelta, detail: str = "census timed out") -> None:
    """*times* deferrals of *rung*, an hour apart, the first one *started_ago* before now."""
    clock = Clock(datetime.now(UTC) - started_ago)
    log = DeferralLog(store, now_fn=clock)  # type: ignore[arg-type]
    for _ in range(times):
        log.record(_report(**{rung: _deferred(detail)}), {})
        clock.tick()


def _row(monkeypatch: pytest.MonkeyPatch, *rungs: _Rung):
    _use(monkeypatch, *rungs)
    (row,) = _check_pending_rungs()
    return row


#: The completions the doctor's ledger stand-in returns (the doctor reads them only when a streak would escalate).
_LEDGER: dict[str, CompletionRecord] = {}


class _LedgerStandIn:
    def completions(self) -> dict[str, CompletionRecord]:
        return dict(_LEDGER)

    def close(self) -> None:
        pass


@pytest.fixture
def doctor_note(monkeypatch: pytest.MonkeyPatch) -> _MemoryStore:
    """The note the doctor row reads, in memory, with the real clock, and a ledger with no completions."""
    store = _MemoryStore()
    monkeypatch.setattr(deferral_log_mod, "t2_deferral_log", lambda: DeferralLog(store))
    _LEDGER.clear()
    monkeypatch.setattr("nexus.upgrade_ladder.http_store.DeferredLadderLedger", _LedgerStandIn)
    return store


def test_a_rung_that_converged_since_its_streak_does_not_escalate_when_it_pends_again(
    monkeypatch: pytest.MonkeyPatch, doctor_note: _MemoryStore,
) -> None:
    """A clean walk writes nothing, so a rung that converged keeps its old streak in the note; after a client upgrade
    it is pending again with no new deferral. Its completion, later than the streak's last deferral, rules it out
    (confirmation r2 reproduced the false REPEATEDLY DEFERRED this pins)."""
    _seed(doctor_note, "slow", ESCALATE_AFTER, started_ago=timedelta(days=3))
    row = _row(monkeypatch, _Rung("slow"))
    assert row.detail.startswith("REPEATEDLY DEFERRED: "), "control: the streak escalates with no completion"
    _LEDGER.update(_done("slow", datetime.now(UTC) - timedelta(hours=1)))  # converged after the last deferral
    row = _row(monkeypatch, _Rung("slow"))
    assert row.warn is True and "REPEATEDLY DEFERRED" not in row.detail
    _LEDGER.update(_done("slow", datetime.now(UTC) - timedelta(days=10)))  # a completion before the streak: still due
    row = _row(monkeypatch, _Rung("slow"))
    assert row.detail.startswith("REPEATEDLY DEFERRED: ")


def test_a_ledger_that_cannot_be_read_is_no_escalation(monkeypatch: pytest.MonkeyPatch, doctor_note: _MemoryStore) -> None:
    class _Down(_LedgerStandIn):
        def completions(self):
            raise ConnectionError("engine down")

    monkeypatch.setattr("nexus.upgrade_ladder.http_store.DeferredLadderLedger", _Down)
    _seed(doctor_note, "slow", ESCALATE_AFTER, started_ago=timedelta(days=3))
    row = _row(monkeypatch, _Rung("slow"))
    assert row.warn is True and "REPEATEDLY DEFERRED" not in row.detail, "fail toward silence, never a false report"


def test_the_row_stays_soft_below_the_count(monkeypatch: pytest.MonkeyPatch, doctor_note: _MemoryStore) -> None:
    _seed(doctor_note, "slow", ESCALATE_AFTER - 1, started_ago=timedelta(days=3))
    row = _row(monkeypatch, _Rung("slow"))
    assert row.ok is False and row.warn is True
    assert "REPEATEDLY DEFERRED" not in row.detail


def test_the_row_stays_soft_inside_the_span(monkeypatch: pytest.MonkeyPatch, doctor_note: _MemoryStore) -> None:
    _seed(doctor_note, "slow", ESCALATE_AFTER, started_ago=timedelta(hours=5))
    row = _row(monkeypatch, _Rung("slow"))
    assert row.ok is False and row.warn is True
    assert "REPEATEDLY DEFERRED" not in row.detail


def test_the_row_leads_with_the_rung_and_stays_a_warning(monkeypatch: pytest.MonkeyPatch, doctor_note: _MemoryStore) -> None:
    _seed(doctor_note, "slow", ESCALATE_AFTER, started_ago=timedelta(days=3), detail="census timed out at aspects")
    row = _row(monkeypatch, _Rung("slow"))

    assert row.ok is False and row.warn is True, "the exit code must not move: a deferral is non-fatal by design"
    assert row.detail.startswith("REPEATEDLY DEFERRED: "), row.detail
    assert "STUCK" not in row.detail
    assert "rung 'slow'" in row.detail and f"{ESCALATE_AFTER} consecutive" in row.detail
    assert "over 3 days" in row.detail
    assert "census timed out at aspects" in row.detail  # the last detail
    assert "upgrade-auto-child.log" in row.detail
    assert any("nx upgrade" in fix for fix in row.fix_suggestions)


def test_only_a_still_pending_rung_escalates(monkeypatch: pytest.MonkeyPatch, doctor_note: _MemoryStore) -> None:
    _seed(doctor_note, "slow", ESCALATE_AFTER, started_ago=timedelta(days=3))
    row = _row(monkeypatch, _Rung("slow", pending=False))  # converged since: its old streak says nothing
    assert row.ok is True

    row = _row(monkeypatch, _Rung("slow", pending=False), _Rung("fresh"))
    assert row.ok is False and row.warn is True and "REPEATEDLY DEFERRED" not in row.detail

    row = _row(monkeypatch, _Rung("slow"), _Rung("fresh"))
    assert row.detail.startswith("REPEATEDLY DEFERRED: ")
    assert "rung 'slow'" in row.detail and "rung 'fresh'" not in row.detail


def test_with_nothing_pending_the_doctor_does_not_read_the_log(monkeypatch: pytest.MonkeyPatch) -> None:
    asked: list[str] = []
    monkeypatch.setattr(deferral_log_mod, "t2_deferral_log", lambda: asked.append("asked") or DeferralLog(_RaisingStore()))
    row = _row(monkeypatch, _Rung("slow", pending=False))
    assert row.ok is True and asked == []


def test_a_log_that_cannot_be_read_leaves_the_row_as_it_is(monkeypatch: pytest.MonkeyPatch, doctor_note: _MemoryStore) -> None:
    _seed(doctor_note, "slow", ESCALATE_AFTER, started_ago=timedelta(days=3))

    def _boom(self: DeferralLog) -> dict:
        raise RuntimeError("log unreadable")

    monkeypatch.setattr(DeferralLog, "escalated", _boom)
    row = _row(monkeypatch, _Rung("slow"))
    assert row.ok is False and row.warn is True
    assert "REPEATEDLY DEFERRED" not in row.detail and "1 pending upgrade rung(s)" in row.detail


def _entry(n: object, started_ago: timedelta) -> dict:
    since = (datetime.now(UTC) - started_ago).isoformat(timespec="seconds")
    return {"n": n, "since": since, "last": since, "detail": "census timed out"}


def test_a_malformed_entry_is_dropped_so_it_cannot_escalate(monkeypatch: pytest.MonkeyPatch, doctor_note: _MemoryStore) -> None:
    """The same stale entry, once with a count that is a number and once with one that is not an int: only the
    second is malformed, and only the well-formed one may name the rung."""
    doctor_note.save({"rungs": {"slow": _entry(99.0, timedelta(days=3))}})  # a float count: not the documented shape
    row = _row(monkeypatch, _Rung("slow"))
    assert row.warn is True and "REPEATEDLY DEFERRED" not in row.detail

    doctor_note.save({"rungs": {"slow": _entry(99, timedelta(days=3))}})  # control: the documented shape escalates
    row = _row(monkeypatch, _Rung("slow"))
    assert row.detail.startswith("REPEATEDLY DEFERRED: ") and "99 consecutive" in row.detail


@pytest.mark.t2_tenant
def test_the_doctor_row_reads_the_real_t2_note(monkeypatch: pytest.MonkeyPatch) -> None:
    store = T2NoteStore(DEFERRAL_TITLE, project=PROGRESS_PROJECT)
    store.save({"rungs": {"slow": _entry("many", timedelta(days=3))}})  # malformed: the row stays soft
    row = _row(monkeypatch, _Rung("slow"))
    assert row.warn is True and "REPEATEDLY DEFERRED" not in row.detail

    store.save({"rungs": {"slow": _entry(4, timedelta(days=3))}})
    row = _row(monkeypatch, _Rung("slow"))
    assert row.warn is True and row.detail.startswith("REPEATEDLY DEFERRED: ") and "4 consecutive" in row.detail
