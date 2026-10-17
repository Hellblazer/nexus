# SPDX-License-Identifier: AGPL-3.0-or-later
"""nexus-y67n5: the RDR-192 census keeps its progress across ``nx upgrade --auto``
sessions, and a starved upgrade is visible.

Before this, the ``--auto`` time limit (nexus-dcbof) stopped the census between
calls and the next session began again at collection one, so an engine slower
than about 9.2 s per call across 98 collections was cut inside the census in
every session and never reached the backfill.

Clocks are fixed values the fakes advance; nothing sleeps. The sessions drive
the production rung and its real ``_default_census`` against a fake engine
client. The progress note is the one thing shared between sessions, exactly as
the T2 note is in production.
"""
from __future__ import annotations

import contextlib
import json
from datetime import UTC, datetime, timedelta

import pytest
import structlog

import nexus.db as nexus_db
from nexus.health import _check_pending_rungs
from nexus.upgrade_ladder import registry as ladder_registry
from nexus.upgrade_ladder.census_progress import (
    CENSUS_PROGRESS_MAX_AGE,
    STARVED_AFTER,
    CensusProgress,
    T2NoteStore,
)
from nexus.upgrade_ladder.deadline import Deadline
from nexus.upgrade_ladder.registry import LadderRegistry, default_registry
from nexus.upgrade_ladder.rungs.rdr192_manifest_backfill import (
    PROGRESS_TITLE,
    Rdr192ManifestBackfillRung,
)
from nexus.upgrade_ladder.runner import LadderRunner, RungOutcome
from tests.upgrade.conftest import InMemoryCompletionLedger

_V = "9.9.9"
_T0 = datetime(2026, 10, 17, 9, 0, tzinfo=UTC)
_BUCKETS = ("legacy-unmanifested", "unclassified", "superseded", "dead-owner", "no-owner")


class FakeClock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now


class WallClock:
    """The datetime the progress note ages by; advanced by the test."""

    def __init__(self) -> None:
        self.now = _T0

    def __call__(self) -> datetime:
        return self.now

    def advance(self, **delta: float) -> None:
        self.now += timedelta(**delta)


class MemoryNote:
    """The shared note: what T2 gives the rung across sessions."""

    def __init__(self) -> None:
        self.note: dict | None = None
        self.saves = 0

    def load(self):
        return json.loads(json.dumps(self.note)) if self.note is not None else None

    def save(self, note: dict) -> None:
        self.saves += 1
        self.note = json.loads(json.dumps(note))

    def clear(self) -> None:
        self.note = None


class SlowEngine:
    """Every census call costs ``cost`` seconds on the injected clock.
    ``stored`` is the listing's per-collection chunk count; ``legacy`` is the
    database state a backfill heals."""

    def __init__(self, clock: FakeClock, names: list[str], *, cost: float, legacy: dict[str, int] | None = None) -> None:
        self.clock = clock
        self.names = list(names)
        self.cost = cost
        self.legacy = dict(legacy or {})
        self.stored: dict[str, int] = {}
        self.censused: list[str] = []
        self.fail_after: int | None = None

    def list_collections(self, strict: bool = False) -> list[dict]:
        return [
            {"name": n, "lifecycle_state": "live", "stored_count": self.stored.get(n, 0)} for n in self.names
        ]

    def manifest_less_census(self, collection: str, limit: int = 1, offset: int = 0) -> dict:
        if self.fail_after is not None and len(self.censused) >= self.fail_after:
            raise RuntimeError("engine went away")
        self.censused.append(collection)
        self.clock.now += self.cost
        totals = dict.fromkeys(_BUCKETS, 0)
        totals["legacy-unmanifested"] = self.legacy.get(collection, 0)
        return {"totals": totals, "scope_chunk_total": max(self.stored.get(collection, 0), 1)}


@contextlib.contextmanager
def _always_locked():
    yield True


class Sessions:
    """Everything that outlives a session (the engine, the note, the ledger,
    the wall clock) plus a way to start the next session with a fresh rung."""

    def __init__(self, monkeypatch: pytest.MonkeyPatch, engine: SlowEngine, clock: FakeClock) -> None:
        self.monkeypatch = monkeypatch
        self.engine = engine
        self.clock = clock
        self.wall = WallClock()
        self.note = MemoryNote()
        self.residual = MemoryNote()
        self.ledger = InMemoryCompletionLedger()
        self.backfilled: list[str] = []
        monkeypatch.setattr(nexus_db, "make_t3", lambda **kw: engine)

    def backfill(self, collection: str) -> int:
        self.backfilled.append(collection)
        healed, self.engine.legacy[collection] = self.engine.legacy.get(collection, 0), 0
        return healed

    def rung(self) -> tuple[LadderRegistry, Rdr192ManifestBackfillRung]:
        registry = default_registry()
        (rung,) = list(registry)
        rung.record_fn = lambda: self.ledger.completions().get(rung.name)
        rung.installed_version_fn = lambda: _V
        rung.memo = self.residual
        rung.now_fn = self.wall
        rung.progress = CensusProgress(self.note, now_fn=self.wall)
        rung.lock_factory = _always_locked
        rung.backfill_fn = self.backfill
        return registry, rung

    def session(self, seconds: float):
        registry, rung = self.rung()
        report = LadderRunner(
            registry, self.ledger, package_version_fn=lambda: _V,
            deadline=Deadline(seconds, clock=self.clock),
        ).run()
        return report, rung


def _outcome(report) -> RungOutcome:
    (run,) = report.runs
    return run.outcome


def _names(n: int) -> list[str]:
    return [f"knowledge__c{i:02d}" for i in range(n)]


# ── the progress note on its own ─────────────────────────────────────────────


class TestCensusProgress:
    def _progress(self, wall: WallClock | None = None) -> tuple[CensusProgress, MemoryNote, WallClock]:
        wall = wall or WallClock()
        note = MemoryNote()
        return CensusProgress(note, now_fn=wall), note, wall

    def _commit(self, progress: CensusProgress, **stored: int) -> None:
        progress.commit(_V, {
            name: progress.entry(stored=count, reading=(0, 0, 1, 2, 3, count)) for name, count in stored.items()
        })

    def test_a_fresh_matching_reading_is_usable(self) -> None:
        progress, _, _ = self._progress()
        self._commit(progress, a=5, b=7)
        assert set(progress.usable(_V, {"a": 5, "b": 7})) == {"a", "b"}

    def test_a_reading_older_than_the_bound_is_recomputed(self) -> None:
        progress, _, wall = self._progress()
        self._commit(progress, a=5)
        wall.advance(seconds=CENSUS_PROGRESS_MAX_AGE.total_seconds() - 1)
        assert "a" in progress.usable(_V, {"a": 5})
        wall.advance(seconds=2)
        assert progress.usable(_V, {"a": 5}) == {}

    def test_a_reading_dated_in_the_future_is_not_trusted(self) -> None:
        progress, _, wall = self._progress()
        self._commit(progress, a=5)
        wall.advance(hours=-1)
        assert progress.usable(_V, {"a": 5}) == {}

    def test_a_changed_stored_count_recomputes_only_that_collection(self) -> None:
        progress, _, _ = self._progress()
        self._commit(progress, a=5, b=7)
        assert set(progress.usable(_V, {"a": 5, "b": 8})) == {"a"}

    def test_a_different_collection_set_only_answers_for_listed_names(self) -> None:
        progress, _, _ = self._progress()
        self._commit(progress, a=5, gone=1)
        assert set(progress.usable(_V, {"a": 5, "new": 3})) == {"a"}

    def test_a_note_from_another_package_version_is_empty(self) -> None:
        progress, _, _ = self._progress()
        self._commit(progress, a=5)
        assert progress.usable("10.0.0", {"a": 5}) == {}
        assert progress.deferrals("10.0.0") is None

    def test_an_unreadable_entry_is_recomputed(self) -> None:
        progress, note, _ = self._progress()
        self._commit(progress, a=5, b=5)
        note.note["entries"]["a"]["r"] = [1, 2]
        note.note["entries"]["b"]["t"] = "not a date"
        assert progress.usable(_V, {"a": 5, "b": 5}) == {}

    def test_cuts_count_and_reset(self) -> None:
        progress, _, _ = self._progress()
        assert progress.deferrals(_V) is None
        for _ in range(STARVED_AFTER):
            progress.note_cut(_V, "during the census")
        cuts = progress.deferrals(_V)
        assert cuts is not None and cuts.count == STARVED_AFTER and cuts.starved
        assert "STARVED" in cuts.describe() and "NX_UPGRADE_AUTO_DEADLINE_SECONDS" in cuts.describe()
        progress.reset_cuts(_V)
        assert progress.deferrals(_V) is None

    def test_one_cut_is_reported_but_not_starved(self) -> None:
        progress, _, _ = self._progress()
        progress.note_cut(_V, "during the census")
        cuts = progress.deferrals(_V)
        assert cuts is not None and cuts.count == 1 and not cuts.starved
        assert "STARVED" not in cuts.describe()

    def test_commit_keeps_the_cut_count_and_invalidate_drops_one_reading(self) -> None:
        progress, _, _ = self._progress()
        progress.note_cut(_V, "x")
        self._commit(progress, a=5, b=7)
        progress.invalidate(_V, "a")
        assert set(progress.usable(_V, {"a": 5, "b": 7})) == {"b"}
        assert progress.deferrals(_V) is not None

    def test_a_disabled_progress_remembers_nothing_and_never_fails(self) -> None:
        progress = CensusProgress(None)
        progress.commit(_V, {"a": progress.entry(stored=1, reading=(0,) * 6)})
        progress.note_cut(_V, "x")
        progress.invalidate(_V, "a")
        progress.reset_cuts(_V)
        progress.clear()
        assert progress.usable(_V, {"a": 1}) == {} and progress.deferrals(_V) is None


class _FakeMemoryStore:
    """Stands in for ``HttpMemoryStore``: one dict, shared by every instance."""

    rows: dict[tuple[str, str], dict] = {}
    raises = False
    puts: list[dict] = []

    def __init__(self, *a, **kw) -> None:
        if type(self).raises:
            raise RuntimeError("no engine")

    def get(self, *, project: str, title: str):
        return type(self).rows.get((project, title))

    def put(self, project, title, content, **kw):
        type(self).rows[(project, title)] = {"content": content}
        type(self).puts.append({"project": project, "title": title, **kw})

    def delete(self, *, project: str, title: str):
        type(self).rows.pop((project, title), None)

    def close(self) -> None:
        pass


class TestT2NoteStore:
    @pytest.fixture(autouse=True)
    def _fake_store(self, monkeypatch: pytest.MonkeyPatch):
        _FakeMemoryStore.rows = {}
        _FakeMemoryStore.raises = False
        _FakeMemoryStore.puts = []
        monkeypatch.setattr("nexus.db.t2.http_memory_store.HttpMemoryStore", _FakeMemoryStore)

    def test_it_is_one_t2_memory_note_with_a_ttl(self) -> None:
        store = T2NoteStore(PROGRESS_TITLE)
        store.save({"version": _V, "entries": {}})
        assert store.load() == {"version": _V, "entries": {}}
        (put,) = _FakeMemoryStore.puts
        assert put["project"] == "upgrade_ladder_state" and put["title"] == PROGRESS_TITLE
        assert put["ttl"] and put["ttl"] > 0
        store.clear()
        assert store.load() is None

    def test_every_failure_degrades_to_no_note(self) -> None:
        _FakeMemoryStore.raises = True
        store = T2NoteStore(PROGRESS_TITLE)
        assert store.load() is None
        store.save({"a": 1})
        store.clear()

    def test_a_note_that_is_not_an_object_is_no_note(self) -> None:
        _FakeMemoryStore.rows[("upgrade_ladder_state", PROGRESS_TITLE)] = {"content": "[1, 2]"}
        assert T2NoteStore(PROGRESS_TITLE).load() is None


# ── sessions cut by the time limit ───────────────────────────────────────────


class TestResumeAcrossSessions:
    def test_two_sessions_each_cut_by_the_limit_complete_the_census_and_reach_the_backfill(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The proof that progress is kept. 12 collections at 10 s a call is
        120 s of census; each session has 80 s, so neither can finish it alone."""
        clock = FakeClock()
        names = _names(12)
        engine = SlowEngine(clock, names, cost=10.0, legacy={names[1]: 4, names[10]: 2})
        sessions = Sessions(monkeypatch, engine, clock)

        first, _ = sessions.session(80.0)

        assert _outcome(first) is RungOutcome.DEFERRED
        assert engine.censused == names[:8], "stopped at the first check at or past 80 s"
        assert sessions.backfilled == [] and sessions.ledger.verified_rungs() == frozenset()

        clock.now += 3600.0  # the next session start; wall time moves with it
        sessions.wall.advance(hours=1)
        second, _ = sessions.session(80.0)

        assert _outcome(second) is RungOutcome.RECORDED
        # The second session read only what the first had not, then re-read the
        # two collections it backfilled (their readings were dropped).
        assert engine.censused[8:] == names[8:] + [names[1], names[10]]
        assert len(set(engine.censused[:12])) == 12, "no collection was censused twice before the backfill"
        assert sessions.backfilled == [names[1], names[10]], "the backfill was reached"
        assert engine.legacy == {names[1]: 0, names[10]: 0}
        assert sessions.ledger.verified_rungs() == frozenset({"rdr192-manifest-backfill"})
        assert sessions.note.note is None, "a recorded completion leaves nothing behind"

    def test_without_progress_the_same_two_sessions_never_get_past_the_census(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The control: what this bead fixes. With the note disabled every
        session starts the census again and is cut at the same place."""
        clock = FakeClock()
        names = _names(12)
        engine = SlowEngine(clock, names, cost=10.0, legacy={names[1]: 4})
        sessions = Sessions(monkeypatch, engine, clock)

        for _ in range(3):
            registry, rung = sessions.rung()
            rung.progress = CensusProgress(None)
            report = LadderRunner(
                registry, sessions.ledger, package_version_fn=lambda: _V, deadline=Deadline(80.0, clock=clock),
            ).run()
            assert _outcome(report) is RungOutcome.DEFERRED
        assert engine.censused == names[:8] * 3 and sessions.backfilled == []

    def test_a_reading_past_the_age_bound_is_taken_again(self, monkeypatch: pytest.MonkeyPatch) -> None:
        clock = FakeClock()
        names = _names(12)
        engine = SlowEngine(clock, names, cost=10.0)
        sessions = Sessions(monkeypatch, engine, clock)

        sessions.session(80.0)
        assert engine.censused == names[:8]
        sessions.wall.advance(hours=7)
        sessions.session(80.0)

        assert engine.censused[8:16] == names[:8], "the stale readings were not trusted"

    def test_a_collection_whose_stored_count_changed_is_taken_again_and_the_rest_is_not(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        clock = FakeClock()
        names = _names(12)
        engine = SlowEngine(clock, names, cost=10.0)
        sessions = Sessions(monkeypatch, engine, clock)

        sessions.session(80.0)
        engine.stored[names[3]] = 99  # someone wrote to it between sessions
        sessions.session(80.0)

        assert engine.censused[8:] == [names[3]] + names[8:]
        assert engine.censused.count(names[2]) == 1

    def test_progress_survives_an_engine_failure_mid_census(self, monkeypatch: pytest.MonkeyPatch) -> None:
        clock = FakeClock()
        names = _names(6)
        engine = SlowEngine(clock, names, cost=1.0)
        engine.fail_after = 4
        sessions = Sessions(monkeypatch, engine, clock)

        report, _ = sessions.session(900.0)
        assert _outcome(report) is RungOutcome.DEFERRED
        assert engine.censused == names[:4]

        engine.fail_after = None
        report, _ = sessions.session(900.0)
        assert _outcome(report) is RungOutcome.RECORDED
        assert engine.censused[4:] == names[4:], "the four read before the failure were kept"

    def test_a_cut_during_the_backfill_resumes_without_a_new_census(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """A complete census is also progress: the next session goes straight
        to the backfill instead of paying 98 calls to find the same targets."""
        clock = FakeClock()
        names = _names(4)
        engine = SlowEngine(clock, names, cost=1.0, legacy={names[0]: 3, names[2]: 3})
        sessions = Sessions(monkeypatch, engine, clock)

        def slow_backfill(collection: str) -> int:
            if not sessions.backfilled:
                clock.now += 500.0  # only the first one is slow
            return Sessions.backfill(sessions, collection)

        monkeypatch.setattr(sessions, "backfill", slow_backfill)

        first, _ = sessions.session(400.0)
        assert _outcome(first) is RungOutcome.DEFERRED
        assert sessions.backfilled == [names[0]]
        censused_so_far = len(engine.censused)

        second, _ = sessions.session(400.0)
        assert sessions.backfilled == [names[0], names[2]]
        assert _outcome(second) is RungOutcome.RECORDED
        assert engine.censused[censused_so_far:] == [names[0], names[2]], (
            "only the collections the first session backfilled were read again"
        )


# ── a starved upgrade is visible ─────────────────────────────────────────────


class TestStarvationIsVisible:
    def _starving(self, monkeypatch: pytest.MonkeyPatch) -> tuple[Sessions, SlowEngine, list[str]]:
        clock = FakeClock()
        names = _names(98)
        engine = SlowEngine(clock, names, cost=30.0)  # 98 * 30 s is far past 900 s
        return Sessions(monkeypatch, engine, clock), engine, names

    def test_a_box_that_never_ran_an_upgrade_reads_as_before(self, monkeypatch: pytest.MonkeyPatch) -> None:
        sessions, _, _ = self._starving(monkeypatch)
        _, rung = sessions.rung()
        status = rung.detect()
        assert status.pending
        assert status.pending_detail == "no completion recorded; the census runs on `nx upgrade`"
        assert sessions.note.saves == 0, "detect() is read-only"

    def test_consecutive_cut_sessions_are_counted_and_shown_by_detect(self, monkeypatch: pytest.MonkeyPatch) -> None:
        sessions, _, _ = self._starving(monkeypatch)
        _, rung = sessions.rung()

        for expected in range(1, STARVED_AFTER + 1):
            sessions.session(900.0)
            detail = rung.detect().pending_detail
            assert f"cut {expected} consecutive session(s)" in detail
            assert ("STARVED" in detail) == (expected >= STARVED_AFTER)

    def test_the_count_shows_in_the_doctor_row(self, monkeypatch: pytest.MonkeyPatch) -> None:
        sessions, _, _ = self._starving(monkeypatch)
        for _ in range(STARVED_AFTER):
            sessions.session(900.0)
        registry, _ = sessions.rung()
        monkeypatch.setattr(ladder_registry, "default_registry", lambda **kw: registry)

        (row,) = _check_pending_rungs()

        assert row.warn is True and "STARVED" in row.detail
        assert f"cut {STARVED_AFTER} consecutive session(s)" in row.detail
        assert any("nx upgrade" in fix for fix in row.fix_suggestions)

    def test_the_starved_census_still_finishes_in_the_fourth_session(self, monkeypatch: pytest.MonkeyPatch) -> None:
        sessions, engine, names = self._starving(monkeypatch)
        for _ in range(3):
            sessions.session(900.0)
        report, _ = sessions.session(900.0)
        assert _outcome(report) is RungOutcome.RECORDED
        assert sorted(set(engine.censused)) == sorted(names) and len(engine.censused) == 98

    def test_a_session_that_is_not_cut_resets_the_count(self, monkeypatch: pytest.MonkeyPatch) -> None:
        clock = FakeClock()
        names = _names(12)
        engine = SlowEngine(clock, names, cost=10.0, legacy={names[0]: 1})
        sessions = Sessions(monkeypatch, engine, clock)
        sessions.session(80.0)
        _, rung = sessions.rung()
        assert "cut 1 consecutive" in rung.detect().pending_detail

        # An unlimited run (interactive `nx upgrade`) that leaves a residual.
        sessions.engine.cost = 0.0
        registry, rung = sessions.rung()
        rung.backfill_fn = lambda c: 0  # heals nothing: a residual, not a cut
        LadderRunner(registry, sessions.ledger, package_version_fn=lambda: _V).run()

        assert "consecutive" not in rung.detect().pending_detail

    def test_a_recorded_completion_leaves_no_pending_row(self, monkeypatch: pytest.MonkeyPatch) -> None:
        sessions, engine, _ = self._starving(monkeypatch)
        sessions.session(900.0)
        engine.cost = 0.0
        sessions.session(900.0)
        _, rung = sessions.rung()
        assert rung.detect().converged

    def test_a_cut_is_logged_once_as_before(self, monkeypatch: pytest.MonkeyPatch) -> None:
        sessions, _, _ = self._starving(monkeypatch)
        with structlog.testing.capture_logs() as logs:
            sessions.session(900.0)
        (cut,) = [e for e in logs if e["event"] == "rdr192_backfill_deadline_reached"]
        assert cut["where"] == "during the census"
