# SPDX-License-Identifier: AGPL-3.0-or-later
"""nexus-0sm1t: a rung that defers on every run is counted, and ``nx doctor`` escalates it.

Since nexus-dfemn the walk goes on past a DEFERRED rung, so the deferring rung stays
pending, silently under ``--auto``. The deferral log (T2 note) counts consecutive
deferrals per rung; the doctor row leads with STUCK (a warning still) at
``ESCALATE_AFTER``. Persistence goes through the real T2 substrate (``t2_tenant``).
"""
from __future__ import annotations

import json
from pathlib import Path
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

import click
import pytest

import nexus.upgrade_ladder.registry as ladder_registry
from nexus import config as _config
from nexus.commands.upgrade import _run_ladder
from nexus.health import _check_pending_rungs
from nexus.upgrade_ladder.census_progress import PROGRESS_PROJECT, T2NoteStore
from nexus.upgrade_ladder.deferral_log import (
    DEFERRAL_TITLE,
    ESCALATE_AFTER,
    FLAG_NAME,
    DeferralLog,
    t2_deferral_log,
)
from nexus.upgrade_ladder.protocol import ConvergeOutcome, ConvergeResult, ProgressReporter, RungStatus
from nexus.upgrade_ladder.registry import LadderRegistry
from nexus.upgrade_ladder.runner import LadderRunReport, RungOutcome, RungRun
from tests.upgrade.conftest import InMemoryCompletionLedger


_T0 = datetime(2026, 11, 3, 9, 0, tzinfo=UTC)


class Clock:
    def __init__(self) -> None:
        self.now = _T0

    def __call__(self) -> datetime:
        return self.now

    def tick(self) -> None:
        self.now += timedelta(hours=1)


def _report(**outcomes: tuple[RungOutcome, str]) -> LadderRunReport:
    return LadderRunReport(runs=tuple(RungRun(n, o, d) for n, (o, d) in outcomes.items()), position=0)


def _deferred(detail: str = "census timed out") -> tuple[RungOutcome, str]:
    return (RungOutcome.DEFERRED, detail)


@pytest.fixture
def clock() -> Clock:
    return Clock()


class _MemoryStore:
    """The note store's three calls, in memory: the counting rules need no storage, and each test that mints a
    tenant costs the suite's storage-touch ceiling. The T2 round trip, the walk and the doctor row keep the real note."""

    def __init__(self) -> None:
        self.note: dict | None = None

    def load(self):
        return self.note

    def save(self, note):
        self.note = json.loads(json.dumps(note))  # what a T2 round trip would hand back

    def clear(self):
        self.note = None


@pytest.fixture
def mem_log(clock: Clock, tmp_path: Path) -> DeferralLog:
    """The counting rules on an in-memory note, with a flag file of its own."""
    return DeferralLog(_MemoryStore(), flag=tmp_path / FLAG_NAME, now_fn=clock)


@pytest.fixture
def log(clock: Clock) -> DeferralLog:
    """A log on the real T2 note, a fixed clock, and an empty note to start."""
    store = T2NoteStore(DEFERRAL_TITLE, project=PROGRESS_PROJECT)
    store.clear()
    flag = Path(_config.nexus_config_dir()) / FLAG_NAME  # the same flag t2_deferral_log() reads
    flag.unlink(missing_ok=True)
    return DeferralLog(store, flag=flag, now_fn=clock)


# ── the log ──────────────────────────────────────────────────────────────────


@pytest.mark.t2_tenant
def test_a_deferral_counts_up_keeping_the_first_stamp_and_the_latest_detail(log: DeferralLog, clock: Clock) -> None:
    log.record(_report(slow=_deferred("first")))
    clock.tick()
    log.record(_report(slow=_deferred("second")))
    clock.tick()
    log.record(_report(slow=_deferred("third")))

    entry = log.deferrals()["slow"]
    assert entry.count == 3
    assert entry.since == _T0.isoformat(timespec="seconds")
    assert entry.last == (_T0 + timedelta(hours=2)).isoformat(timespec="seconds")
    assert entry.detail == "third"


@pytest.mark.parametrize(
    "outcome",
    [RungOutcome.RECORDED, RungOutcome.ALREADY_RECORDED, RungOutcome.SKIPPED_NOT_APPLICABLE],
    ids=lambda o: o.value,
)
def test_a_clearing_outcome_ends_only_that_rungs_streak(mem_log: DeferralLog, outcome: RungOutcome) -> None:
    mem_log.record(_report(slow=_deferred(), other=_deferred()))
    mem_log.record(_report(slow=_deferred(), other=_deferred()))
    assert mem_log.deferrals()["slow"].count == 2

    mem_log.record(_report(slow=(outcome, ""), other=_deferred()))

    assert "slow" not in mem_log.deferrals()
    assert mem_log.deferrals()["other"].count == 3


@pytest.mark.parametrize(
    "outcome",
    [RungOutcome.NOT_ATTEMPTED, RungOutcome.FAILED, RungOutcome.VERIFY_FAILED],
    ids=lambda o: o.value,
)
def test_a_rung_that_was_not_walked_or_failed_keeps_its_streak(mem_log: DeferralLog, outcome: RungOutcome) -> None:
    mem_log.record(_report(slow=_deferred("kept")))
    mem_log.record(_report(slow=_deferred("kept")))
    before = mem_log.deferrals()["slow"]

    mem_log.record(_report(slow=(outcome, "something else")))

    assert mem_log.deferrals()["slow"] == before


def test_an_emptied_log_leaves_no_note(mem_log: DeferralLog) -> None:
    mem_log.record(_report(slow=_deferred()))
    mem_log.record(_report(slow=(RungOutcome.RECORDED, "")))
    assert mem_log._store.load() is None and mem_log.deferrals() == {}


def test_the_streak_escalates_at_the_threshold_and_not_before(mem_log: DeferralLog) -> None:
    assert ESCALATE_AFTER == 3
    mem_log.record(_report(slow=_deferred()))
    mem_log.record(_report(slow=_deferred()))
    assert mem_log.escalated() == {}
    mem_log.record(_report(slow=_deferred()))
    assert set(mem_log.escalated()) == {"slow"}


@pytest.mark.t2_tenant
def test_a_malformed_note_reads_as_no_streaks_and_the_next_walk_replaces_it(log: DeferralLog) -> None:
    store = T2NoteStore(DEFERRAL_TITLE, project=PROGRESS_PROJECT)
    store.save({"rungs": {"slow": {"n": "many"}, "bad": 7, "worse": {"n": True, "since": "x", "last": "y", "detail": ""}}})
    assert log.deferrals() == {}
    log.record(_report(slow=_deferred()))
    assert set(log.deferrals()) == {"slow"} and log.deferrals()["slow"].count == 1


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


def _walk(auto_mode: bool, ledger: InMemoryCompletionLedger | None = None) -> None:
    ledger = ledger or InMemoryCompletionLedger()
    _run_ladder(dry_run=False, auto_mode=auto_mode, _ledger_fn=lambda: ledger)


@pytest.mark.t2_tenant
@pytest.mark.parametrize("auto_mode", [True, False], ids=["auto", "interactive"])
def test_a_real_walk_counts_the_deferral_and_a_converged_one_clears_it(
    monkeypatch: pytest.MonkeyPatch, log: DeferralLog, auto_mode: bool
) -> None:
    rung = _Rung("slow")
    _use(monkeypatch, rung)
    _walk(auto_mode)
    _walk(auto_mode)
    assert t2_deferral_log().deferrals()["slow"].count == 2

    rung.outcome = "complete"
    _walk(auto_mode)
    assert t2_deferral_log().deferrals() == {}


@pytest.mark.t2_tenant
def test_a_dry_run_records_nothing(monkeypatch: pytest.MonkeyPatch, log: DeferralLog) -> None:
    _use(monkeypatch, _Rung("slow"))
    _run_ladder(dry_run=True, auto_mode=False)
    assert t2_deferral_log().deferrals() == {}


@pytest.mark.t2_tenant
def test_a_failed_rung_in_a_walk_leaves_another_rungs_streak(monkeypatch: pytest.MonkeyPatch, log: DeferralLog) -> None:
    slow, bad = _Rung("slow"), _Rung("bad", outcome="fail")
    _use(monkeypatch, slow)
    _walk(True)
    _use(monkeypatch, bad, slow)
    with pytest.raises(click.ClickException):
        _walk(False)
    assert t2_deferral_log().deferrals()["slow"].count == 1  # NOT_ATTEMPTED behind the failure: unchanged


@pytest.mark.t2_tenant
def test_a_log_that_raises_changes_neither_the_outcome_nor_the_error(
    monkeypatch: pytest.MonkeyPatch, log: DeferralLog
) -> None:
    def _boom(self: DeferralLog, report: LadderRunReport) -> None:
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


def _seed(log: DeferralLog, rung: str, times: int, detail: str = "census timed out") -> None:
    for _ in range(times):
        log.record(_report(**{rung: _deferred(detail)}))


def _row(monkeypatch: pytest.MonkeyPatch, *rungs: _Rung):
    _use(monkeypatch, *rungs)
    (row,) = _check_pending_rungs()
    return row


@pytest.mark.t2_tenant
def test_the_row_stays_soft_below_the_threshold(monkeypatch: pytest.MonkeyPatch, log: DeferralLog) -> None:
    _seed(log, "slow", ESCALATE_AFTER - 1)
    row = _row(monkeypatch, _Rung("slow"))
    assert row.ok is False and row.warn is True
    assert "STUCK" not in row.detail


@pytest.mark.t2_tenant
def test_the_row_becomes_a_full_warning_at_the_threshold_and_names_the_rung(
    monkeypatch: pytest.MonkeyPatch, log: DeferralLog, clock: Clock
) -> None:
    _seed(log, "slow", 1, "first")
    clock.tick()
    _seed(log, "slow", ESCALATE_AFTER - 1, "census timed out at aspects")
    row = _row(monkeypatch, _Rung("slow"))

    assert row.ok is False and row.warn is True, "a stuck rung stays a warning: doctor's exit code must not move"
    assert row.detail.startswith("STUCK: "), row.detail
    assert "slow" in row.detail and f"{ESCALATE_AFTER} consecutive" in row.detail
    assert _T0.isoformat(timespec="seconds") in row.detail  # since when
    assert "census timed out at aspects" in row.detail  # the last detail
    assert "nx upgrade" in row.detail  # the remedy
    assert any("nx upgrade" in fix for fix in row.fix_suggestions)


@pytest.mark.t2_tenant
def test_only_a_still_pending_rung_escalates(monkeypatch: pytest.MonkeyPatch, log: DeferralLog) -> None:
    _seed(log, "slow", ESCALATE_AFTER)
    row = _row(monkeypatch, _Rung("slow", pending=False))  # converged since: its old streak says nothing
    assert row.ok is True

    # Another rung pending, the stuck one converged since: the row is the ordinary soft one.
    row = _row(monkeypatch, _Rung("slow", pending=False), _Rung("fresh"))
    assert row.ok is False and row.warn is True and "STUCK" not in row.detail

    row = _row(monkeypatch, _Rung("slow"), _Rung("fresh"))
    assert row.detail.startswith("STUCK: ") and "rung 'slow'" in row.detail and "rung 'fresh'" not in row.detail


@pytest.mark.t2_tenant
def test_a_log_that_cannot_be_read_leaves_the_row_as_it_is(monkeypatch: pytest.MonkeyPatch, log: DeferralLog) -> None:
    _seed(log, "slow", ESCALATE_AFTER)

    def _boom(self: DeferralLog) -> dict:
        raise RuntimeError("log unreadable")

    monkeypatch.setattr(DeferralLog, "escalated", _boom)
    row = _row(monkeypatch, _Rung("slow"))
    assert row.ok is False and row.warn is True
    assert "STUCK" not in row.detail and "1 pending upgrade rung(s)" in row.detail


@pytest.mark.t2_tenant
def test_a_malformed_note_leaves_the_row_soft(monkeypatch: pytest.MonkeyPatch, log: DeferralLog) -> None:
    T2NoteStore(DEFERRAL_TITLE, project=PROGRESS_PROJECT).save({"rungs": json.loads('{"slow": {"n": 99}}')})
    row = _row(monkeypatch, _Rung("slow"))
    assert row.warn is True and "STUCK" not in row.detail



class _RaisingStore:
    """A note store that must not be reached."""

    def load(self):
        raise AssertionError("the note was read")

    def save(self, note):
        raise AssertionError("the note was written")

    def clear(self):
        raise AssertionError("the note was cleared")


def test_with_no_flag_a_clean_walk_and_the_doctor_read_never_touch_the_note(tmp_path: Path) -> None:
    """The flag file keeps a clean upgrade (and every fresh config directory) off storage."""
    log = DeferralLog(_RaisingStore(), flag=tmp_path / FLAG_NAME)
    log.record(_report(a=(RungOutcome.RECORDED, ""), b=(RungOutcome.SKIPPED_NOT_APPLICABLE, "")))
    assert log.deferrals() == {} and log.escalated() == {}


def test_the_flag_exists_exactly_while_the_note_holds_a_streak(mem_log: DeferralLog) -> None:
    flag = mem_log._flag
    assert flag is not None and not flag.exists()
    mem_log.record(_report(slow=_deferred()))
    assert flag.exists()
    mem_log.record(_report(slow=(RungOutcome.RECORDED, "")))
    assert not flag.exists() and mem_log.deferrals() == {}
