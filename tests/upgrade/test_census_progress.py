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
from collections.abc import Callable
from datetime import UTC, datetime, timedelta

import pytest
import structlog

import nexus.db as nexus_db
from nexus.health import _check_pending_rungs
from nexus.upgrade_ladder import registry as ladder_registry
from nexus.upgrade_ladder.census_progress import (
    CENSUS_PROGRESS_MAX_AGE,
    CHECKPOINT_EVERY,
    STALLED_AFTER,
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
        self.dead = False  # a killed process writes nothing more

    def load(self):
        return json.loads(json.dumps(self.note)) if self.note is not None else None

    def save(self, note: dict) -> None:
        if self.dead:
            return
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
        self.fail_at: str | None = None  # this collection's census call always fails
        self.list_cost = 0.0
        self.omit_stored = False  # the listing carries no stored_count (nor count)
        self.on_call: Callable[[int], None] | None = None  # called with the number of calls so far

    def list_collections(self, strict: bool = False) -> list[dict]:
        self.clock.now += self.list_cost
        rows = [
            {"name": n, "lifecycle_state": "live", "stored_count": self.stored.get(n, 0)} for n in self.names
        ]
        if self.omit_stored:
            for row in rows:
                del row["stored_count"]
        return rows

    def manifest_less_census(self, collection: str, limit: int = 1, offset: int = 0) -> dict:
        if self.on_call is not None:
            self.on_call(len(self.censused))
        if collection == self.fail_at:
            raise RuntimeError("the census call for this collection timed out")
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

    def test_cuts_without_progress_count_and_a_progressed_session_resets(self) -> None:
        progress, _, _ = self._progress()
        assert progress.deferrals(_V) is None
        for _ in range(STARVED_AFTER):
            progress.end_session(_V, cut_at="during the census", stalled_at=None, progressed=False)
        cuts = progress.deferrals(_V)
        assert cuts is not None and cuts.count == STARVED_AFTER and cuts.starved
        assert "STARVED" in cuts.describe() and "NX_UPGRADE_AUTO_DEADLINE_SECONDS" in cuts.describe()
        progress.end_session(_V, cut_at="during the census", stalled_at=None, progressed=True)
        assert progress.deferrals(_V) is None, "a cut session that kept a reading or backfilled is not starved"

    def test_an_uncut_session_resets_the_count(self) -> None:
        progress, _, _ = self._progress()
        progress.end_session(_V, cut_at="x", stalled_at=None, progressed=False)
        progress.end_session(_V, cut_at=None, stalled_at=None, progressed=False)
        assert progress.deferrals(_V) is None

    def test_one_cut_is_reported_but_not_starved(self) -> None:
        progress, _, _ = self._progress()
        progress.end_session(_V, cut_at="during the census", stalled_at=None, progressed=False)
        cuts = progress.deferrals(_V)
        assert cuts is not None and cuts.count == 1 and not cuts.starved
        assert "STARVED" not in cuts.describe()

    def test_the_cut_text_never_claims_progress(self) -> None:
        progress, _, _ = self._progress()
        progress.end_session(_V, cut_at="during the census", stalled_at=None, progressed=False)
        assert "keeps its progress" not in progress.describe(_V)

    def test_a_failure_at_the_same_collection_in_consecutive_sessions_is_a_stall(self) -> None:
        progress, _, _ = self._progress()
        progress.end_session(_V, cut_at=None, stalled_at="c07", progressed=True)
        assert progress.stall(_V) is None and progress.describe(_V) == "", "one failure is an outage, not a stall"
        for _ in range(STALLED_AFTER - 1):
            progress.end_session(_V, cut_at=None, stalled_at="c07", progressed=False)
        stall = progress.stall(_V)
        assert stall is not None and stall.collection == "c07" and stall.count == STALLED_AFTER
        assert f"census stalled at c07 for {STALLED_AFTER} sessions" in progress.describe(_V)

    def test_a_failure_at_another_collection_or_a_session_that_gets_past_it_restarts_the_stall(self) -> None:
        progress, _, _ = self._progress()
        for _ in range(3):
            progress.end_session(_V, cut_at=None, stalled_at="c07", progressed=False)
        progress.end_session(_V, cut_at=None, stalled_at="c08", progressed=False)
        assert progress.stall(_V) is None, "a different collection starts at one"
        for _ in range(3):
            progress.end_session(_V, cut_at=None, stalled_at="c08", progressed=False)
        assert progress.stall(_V) is not None
        progress.end_session(_V, cut_at=None, stalled_at=None, progressed=True)
        assert progress.stall(_V) is None

    def test_commit_keeps_the_counters_and_invalidate_drops_one_reading(self) -> None:
        progress, _, _ = self._progress()
        progress.end_session(_V, cut_at="x", stalled_at="c01", progressed=False)
        self._commit(progress, a=5, b=7)
        progress.invalidate(_V, "a")
        assert set(progress.usable(_V, {"a": 5, "b": 7})) == {"b"}
        assert progress.deferrals(_V) is not None
        note = progress._store.note  # the stall is in the note beside the readings
        assert note["stall"]["collection"] == "c01"

    def test_a_disabled_progress_remembers_nothing_and_never_fails(self) -> None:
        progress = CensusProgress(None)
        progress.commit(_V, {"a": progress.entry(stored=1, reading=(0,) * 6)})
        progress.end_session(_V, cut_at="x", stalled_at="y", progressed=False)
        progress.invalidate(_V, "a")
        progress.clear()
        assert progress.usable(_V, {"a": 1}) == {} and progress.deferrals(_V) is None
        assert progress.stall(_V) is None and progress.describe(_V) == ""

    @pytest.mark.parametrize(
        "mutate",
        [
            pytest.param(lambda n: n["entries"]["a"].pop("s"), id="count-key-missing"),
            pytest.param(lambda n: n["entries"]["a"].update(s=-1), id="stored-count-negative"),
            pytest.param(lambda n: n["entries"]["a"].update(s="5"), id="stored-count-a-string"),
            pytest.param(lambda n: n["entries"]["a"].update(s=5.0), id="stored-count-a-float"),
            pytest.param(lambda n: n["entries"]["a"].update(s=True), id="stored-count-a-bool"),
            pytest.param(lambda n: n["entries"]["a"].update(r=["x", 0, 0, 0, 0, 0]), id="reading-not-int"),
            pytest.param(lambda n: n["entries"]["a"].update(r=[0, 0, 0, 0, 0, -1]), id="reading-negative"),
            pytest.param(lambda n: n["entries"]["a"].update(r=[0.5, 0, 0, 0, 0, 0]), id="reading-float"),
            pytest.param(lambda n: n["entries"]["a"].update(r="abcdef"), id="reading-a-six-char-string"),
            pytest.param(lambda n: n["entries"]["a"].update(r=None), id="reading-null"),
            pytest.param(lambda n: n["entries"]["a"].update(t=12345), id="stamp-not-a-string"),
            pytest.param(lambda n: n["entries"]["a"].update(t="2026-10-17T09:00:00"), id="stamp-naive"),
            pytest.param(lambda n: n["entries"].update(a=5), id="entry-not-an-object"),
        ],
    )
    def test_a_malformed_entry_is_recomputed_and_never_raises(self, mutate) -> None:
        progress, note, _ = self._progress()
        self._commit(progress, a=5, b=5)
        mutate(note.note)
        assert set(progress.usable(_V, {"a": 5, "b": 5})) == {"b"}

    def test_a_negative_listing_count_never_matches_a_kept_reading(self) -> None:
        progress, _, _ = self._progress()
        self._commit(progress, a=-1)  # recorded while the listing could not measure it
        assert progress.usable(_V, {"a": -1}) == {}

    @pytest.mark.parametrize(
        "mutate",
        [
            pytest.param(lambda n: n.update(entries=[]), id="entries-a-list"),
            pytest.param(lambda n: n.update(entries=None), id="entries-null"),
            pytest.param(lambda n: n.update(entries="x"), id="entries-a-string"),
            pytest.param(lambda n: n.update(cuts="x"), id="cuts-a-string"),
            pytest.param(lambda n: n.update(cuts=[1]), id="cuts-a-list"),
            pytest.param(lambda n: n.update(cuts={"n": "x"}), id="cuts-count-a-string"),
            pytest.param(lambda n: n.update(cuts={"n": -2}), id="cuts-count-negative"),
            pytest.param(lambda n: n.update(stall="x"), id="stall-a-string"),
            pytest.param(lambda n: n.update(stall={"collection": 3, "n": 2}), id="stall-collection-not-a-string"),
            pytest.param(lambda n: n.update(stall={"collection": "a", "n": "x"}), id="stall-count-a-string"),
        ],
    )
    def test_a_note_of_the_wrong_shape_is_no_note_for_every_method(self, mutate) -> None:
        progress, note, _ = self._progress()
        self._commit(progress, a=5)
        mutate(note.note)
        assert progress.usable(_V, {"a": 5}) == {}
        assert progress.deferrals(_V) is None and progress.stall(_V) is None and progress.describe(_V) == ""
        progress.invalidate(_V, "a")
        progress.end_session(_V, cut_at="x", stalled_at="y", progressed=False)  # does not raise
        assert progress.deferrals(_V).count == 1, "the bad note was replaced by a clean one"
        assert isinstance(note.note["entries"], dict)

    def test_the_next_commit_replaces_a_malformed_note(self) -> None:
        progress, note, _ = self._progress()
        note.note = {"version": _V, "entries": [], "cuts": "x"}
        self._commit(progress, a=5)
        assert note.note["entries"].keys() == {"a"} and note.note["cuts"] is None
        assert set(progress.usable(_V, {"a": 5})) == {"a"}


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

    def test_the_completion_record_says_how_much_of_the_census_was_kept_and_how_old(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        clock = FakeClock()
        names = _names(12)
        engine = SlowEngine(clock, names, cost=10.0, legacy={names[1]: 4, names[10]: 2})
        sessions = Sessions(monkeypatch, engine, clock)
        sessions.session(80.0)
        clock.now += 3600.0
        sessions.wall.advance(hours=1)
        sessions.session(80.0)

        record = sessions.ledger.completions()["rdr192-manifest-backfill"]

        # 12 collections, 2 backfilled and read again; the other 10 came from
        # the note, the oldest taken an hour earlier.
        assert "from_note=10 oldest_note_age=1.0h" in record.detail, record.detail

    def test_a_census_taken_in_one_session_says_it_used_no_kept_readings(self, monkeypatch: pytest.MonkeyPatch) -> None:
        clock = FakeClock()
        engine = SlowEngine(clock, _names(3), cost=1.0)
        sessions = Sessions(monkeypatch, engine, clock)
        registry, rung = sessions.rung()
        rung.progress = CensusProgress(None)
        LadderRunner(registry, sessions.ledger, package_version_fn=lambda: _V).run()

        detail = sessions.ledger.completions()["rdr192-manifest-backfill"].detail
        assert "from_note=0" in detail and "oldest_note_age" not in detail

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
        sessions.wall.advance(seconds=CENSUS_PROGRESS_MAX_AGE.total_seconds() + 3600)
        sessions.session(80.0)

        assert engine.censused[8:16] == names[:8], "the stale readings were not trusted"

    def test_sessions_hours_apart_each_cut_by_the_limit_still_converge_to_the_backfill(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The age bound must cover the user's real cadence. 20 collections at
        10 s a call need three 80 s sessions; a session every 8 hours would
        never converge under the original 6 h bound (each session would find
        the earlier readings expired)."""
        clock = FakeClock()
        names = _names(20)
        engine = SlowEngine(clock, names, cost=10.0, legacy={names[1]: 4})
        sessions = Sessions(monkeypatch, engine, clock)

        for _ in range(2):
            report, _ = sessions.session(80.0)
            assert _outcome(report) is RungOutcome.DEFERRED
            clock.now += 8 * 3600.0
            sessions.wall.advance(hours=8)
        report, _ = sessions.session(80.0)

        assert _outcome(report) is RungOutcome.RECORDED
        assert engine.censused == names + [names[1]], "every collection once, then only the backfilled one again"
        assert sessions.backfilled == [names[1]]

    def test_a_listing_with_no_stored_count_never_serves_a_kept_reading(self, monkeypatch: pytest.MonkeyPatch) -> None:
        clock = FakeClock()
        names = _names(12)
        engine = SlowEngine(clock, names, cost=10.0)
        engine.omit_stored = True
        sessions = Sessions(monkeypatch, engine, clock)

        sessions.session(80.0)
        sessions.session(80.0)

        assert engine.censused[8:16] == names[:8], "nothing could be matched, so nothing was reused"

    def test_a_session_killed_outright_keeps_the_readings_checkpointed_so_far(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """SIGKILL runs no `finally`. The note is written every
        CHECKPOINT_EVERY readings, so a kill after 15 keeps 10 of them."""

        class _Killed(BaseException):
            pass

        clock = FakeClock()
        names = _names(30)
        engine = SlowEngine(clock, names, cost=1.0)
        sessions = Sessions(monkeypatch, engine, clock)

        def die_at_fifteen(done: int) -> None:
            if done == 15:
                sessions.note.dead = True  # the process is gone: nothing it does after this reaches T2
                raise _Killed

        engine.on_call = die_at_fifteen
        with pytest.raises(_Killed):
            sessions.session(900.0)
        assert engine.censused == names[:15]
        assert len(sessions.note.note["entries"]) == CHECKPOINT_EVERY == 10

        sessions.note.dead = False
        engine.on_call = None
        report, _ = sessions.session(900.0)

        assert _outcome(report) is RungOutcome.RECORDED
        assert engine.censused[15:] == names[10:], "resumed at the last checkpoint, not at collection one"

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
    def _slow(self, monkeypatch: pytest.MonkeyPatch) -> tuple[Sessions, SlowEngine, list[str]]:
        """98 collections at 30 s a call: far past one 900 s session, but each
        session reads 30 of them, so the upgrade converges in four."""
        clock = FakeClock()
        names = _names(98)
        engine = SlowEngine(clock, names, cost=30.0)
        return Sessions(monkeypatch, engine, clock), engine, names

    def _stuck(self, monkeypatch: pytest.MonkeyPatch) -> tuple[Sessions, SlowEngine, list[str]]:
        """The collection listing alone outlasts the 900 s limit, so every
        session is cut before it reads or backfills anything."""
        sessions, engine, names = self._slow(monkeypatch)
        engine.list_cost = 1000.0
        return sessions, engine, names

    def test_a_box_that_never_ran_an_upgrade_reads_as_before(self, monkeypatch: pytest.MonkeyPatch) -> None:
        sessions, _, _ = self._stuck(monkeypatch)
        _, rung = sessions.rung()
        status = rung.detect()
        assert status.pending
        assert status.pending_detail == "no completion recorded; the census runs on `nx upgrade`"
        assert sessions.note.saves == 0, "detect() is read-only"

    def test_consecutive_cuts_that_made_no_progress_are_counted_and_shown_by_detect(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        sessions, engine, _ = self._stuck(monkeypatch)
        _, rung = sessions.rung()

        for expected in range(1, STARVED_AFTER + 1):
            sessions.session(900.0)
            detail = rung.detect().pending_detail
            assert f"ended {expected} consecutive session(s)" in detail
            assert ("STARVED" in detail) == (expected >= STARVED_AFTER)
        assert engine.censused == []
        assert "keeps its progress" not in detail

    def test_the_count_shows_in_the_doctor_row(self, monkeypatch: pytest.MonkeyPatch) -> None:
        sessions, _, _ = self._stuck(monkeypatch)
        for _ in range(STARVED_AFTER):
            sessions.session(900.0)
        registry, _ = sessions.rung()
        monkeypatch.setattr(ladder_registry, "default_registry", lambda **kw: registry)

        (row,) = _check_pending_rungs()

        assert row.warn is True and "STARVED" in row.detail
        assert f"ended {STARVED_AFTER} consecutive session(s)" in row.detail
        assert any("nx upgrade" in fix for fix in row.fix_suggestions)

    def test_a_slow_upgrade_that_keeps_progressing_is_never_labelled_starved(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Three sessions are cut by the limit; each reads 30 new collections.
        The old count called this STARVED from the third."""
        sessions, engine, names = self._slow(monkeypatch)
        _, rung = sessions.rung()
        for _ in range(3):
            sessions.session(900.0)
            detail = rung.detect().pending_detail
            assert "STARVED" not in detail and "consecutive" not in detail
        report, _ = sessions.session(900.0)
        assert _outcome(report) is RungOutcome.RECORDED
        assert engine.censused == names, "each of the 98 collections was read exactly once"

    def test_a_cut_session_that_progressed_resets_a_starved_count(self, monkeypatch: pytest.MonkeyPatch) -> None:
        sessions, engine, _ = self._stuck(monkeypatch)
        _, rung = sessions.rung()
        for _ in range(STARVED_AFTER):
            sessions.session(900.0)
        assert "STARVED" in rung.detect().pending_detail

        engine.list_cost = 0.0
        report, _ = sessions.session(900.0)  # cut again, but it read 30 collections

        assert _outcome(report) is RungOutcome.DEFERRED
        assert "consecutive" not in rung.detect().pending_detail

    def test_a_cut_that_finished_a_backfill_collection_is_progress(self, monkeypatch: pytest.MonkeyPatch) -> None:
        clock = FakeClock()
        names = _names(4)
        engine = SlowEngine(clock, names, cost=1.0, legacy={names[0]: 3, names[2]: 3})
        sessions = Sessions(monkeypatch, engine, clock)

        def slow_backfill(collection: str) -> int:
            clock.now += 500.0
            return Sessions.backfill(sessions, collection)

        monkeypatch.setattr(sessions, "backfill", slow_backfill)
        report, rung = sessions.session(400.0)

        assert _outcome(report) is RungOutcome.DEFERRED and sessions.backfilled == [names[0]]
        assert "consecutive" not in rung.detect().pending_detail

    def test_a_session_that_is_not_cut_resets_the_count(self, monkeypatch: pytest.MonkeyPatch) -> None:
        sessions, engine, names = self._stuck(monkeypatch)
        engine.legacy[names[0]] = 1
        sessions.session(900.0)
        _, rung = sessions.rung()
        assert "ended 1 consecutive" in rung.detect().pending_detail

        # An unlimited run (interactive `nx upgrade`) that leaves a residual.
        engine.list_cost = 0.0
        engine.cost = 0.0
        registry, rung = sessions.rung()
        rung.backfill_fn = lambda c: 0  # heals nothing: a residual, not a cut
        LadderRunner(registry, sessions.ledger, package_version_fn=lambda: _V).run()

        assert "consecutive" not in rung.detect().pending_detail

    def test_an_unreachable_engine_leaves_the_counts_alone(self, monkeypatch: pytest.MonkeyPatch) -> None:
        sessions, _, _ = self._stuck(monkeypatch)
        sessions.session(900.0)
        _, rung = sessions.rung()
        assert "ended 1 consecutive" in rung.detect().pending_detail

        def unreachable(**kw):
            raise RuntimeError("connection refused")

        monkeypatch.setattr(nexus_db, "make_t3", unreachable)
        report, _ = sessions.session(900.0)

        assert _outcome(report) is RungOutcome.DEFERRED
        assert "ended 1 consecutive" in rung.detect().pending_detail

    def test_a_recorded_completion_leaves_no_pending_row(self, monkeypatch: pytest.MonkeyPatch) -> None:
        sessions, engine, _ = self._slow(monkeypatch)
        sessions.session(900.0)
        engine.cost = 0.0
        sessions.session(900.0)
        _, rung = sessions.rung()
        assert rung.detect().converged

    def test_a_cut_is_logged_once_as_before(self, monkeypatch: pytest.MonkeyPatch) -> None:
        sessions, _, _ = self._slow(monkeypatch)
        with structlog.testing.capture_logs() as logs:
            sessions.session(900.0)
        (cut,) = [e for e in logs if e["event"] == "rdr192_backfill_deadline_reached"]
        assert cut["where"] == "during the census"


class TestACensusCallThatKeepsFailing:
    """One collection's census call fails every session (a call over the HTTP
    timeout): the walk never gets past it, the earlier readings are kept, and
    nothing used to say so."""

    def _sessions(self, monkeypatch: pytest.MonkeyPatch) -> tuple[Sessions, SlowEngine, list[str]]:
        clock = FakeClock()
        names = _names(8)
        engine = SlowEngine(clock, names, cost=1.0)
        engine.fail_at = names[5]
        return Sessions(monkeypatch, engine, clock), engine, names

    def test_the_collection_is_named_once_it_fails_in_consecutive_sessions(self, monkeypatch: pytest.MonkeyPatch) -> None:
        sessions, engine, names = self._sessions(monkeypatch)
        _, rung = sessions.rung()

        report, _ = sessions.session(900.0)
        assert _outcome(report) is RungOutcome.DEFERRED
        assert "stalled" not in rung.detect().pending_detail, "one failure is not yet a pattern"

        for expected in range(STALLED_AFTER, STALLED_AFTER + 2):
            sessions.session(900.0)
            assert f"census stalled at {names[5]} for {expected} sessions" in rung.detect().pending_detail
        assert engine.censused == names[:5], "the readings before the failing collection were kept, not re-read"

    def test_the_stall_shows_in_the_doctor_row(self, monkeypatch: pytest.MonkeyPatch) -> None:
        sessions, _, names = self._sessions(monkeypatch)
        for _ in range(STALLED_AFTER):
            sessions.session(900.0)
        registry, _ = sessions.rung()
        monkeypatch.setattr(ladder_registry, "default_registry", lambda **kw: registry)

        (row,) = _check_pending_rungs()

        assert f"census stalled at {names[5]}" in row.detail

    def test_a_session_that_gets_past_it_clears_the_stall(self, monkeypatch: pytest.MonkeyPatch) -> None:
        sessions, engine, names = self._sessions(monkeypatch)
        _, rung = sessions.rung()
        for _ in range(STALLED_AFTER):
            sessions.session(900.0)
        assert "stalled" in rung.detect().pending_detail

        engine.fail_at = None
        report, _ = sessions.session(900.0)

        assert _outcome(report) is RungOutcome.RECORDED
        assert rung.detect().converged
        assert engine.censused[5:] == names[5:], "only the failing collection and after were read"


# ── a note of the wrong shape never fails the rung ───────────────────────────

_STAMP = _T0.isoformat(timespec="seconds")


def _entry(**over) -> dict:
    return {"s": 0, "t": _STAMP, "r": [0, 0, 0, 0, 0, 1], **over}


def _malformed_notes(names: list[str]) -> dict[str, dict]:
    first = names[0]
    return {
        "entries-a-list": {"version": _V, "entries": [], "cuts": None},
        "entries-null": {"version": _V, "entries": None, "cuts": None},
        "cuts-a-string": {"version": _V, "entries": {}, "cuts": "x"},
        "cuts-count-a-string": {"version": _V, "entries": {}, "cuts": {"n": "x"}},
        "stall-garbage": {"version": _V, "entries": {}, "cuts": None, "stall": {"collection": 3, "n": "x"}},
        "entry-not-an-object": {"version": _V, "entries": {first: 7}, "cuts": None},
        "reading-not-int": {"version": _V, "entries": {first: _entry(r=["x", 0, 0, 0, 0, 1])}, "cuts": None},
        "reading-a-six-char-string": {"version": _V, "entries": {first: _entry(r="abcdef")}, "cuts": None},
        "reading-null": {"version": _V, "entries": {first: _entry(r=None)}, "cuts": None},
        "stored-count-not-int": {"version": _V, "entries": {first: _entry(s="x")}, "cuts": None},
        "stored-count-missing": {"version": _V, "entries": {first: {k: v for k, v in _entry().items() if k != "s"}}, "cuts": None},
        "stamp-not-a-string": {"version": _V, "entries": {first: _entry(t=7)}, "cuts": None},
    }


class TestAMalformedNoteNeverFailsTheRung:
    @pytest.mark.parametrize("shape", list(_malformed_notes(_names(12))))
    def test_the_rung_is_not_failed_and_the_next_write_replaces_the_note(
        self, monkeypatch: pytest.MonkeyPatch, shape: str
    ) -> None:
        clock = FakeClock()
        names = _names(12)
        engine = SlowEngine(clock, names, cost=10.0, legacy={names[1]: 4})
        sessions = Sessions(monkeypatch, engine, clock)
        sessions.note.note = _malformed_notes(names)[shape]

        first, rung = sessions.session(80.0)
        assert _outcome(first) is RungOutcome.DEFERRED, "cut by the limit, as with no note; not FAILED"
        note = sessions.note.note
        assert isinstance(note["entries"], dict) and len(note["entries"]) == 8, "the bad note was replaced by the readings"
        assert note["cuts"] is None or isinstance(note["cuts"], dict)
        assert "consecutive" not in rung.detect().pending_detail  # progressed: not a no-progress cut

        sessions.wall.advance(hours=1)
        second, _ = sessions.session(80.0)
        assert _outcome(second) is RungOutcome.RECORDED
        assert sessions.backfilled == [names[1]]

    def test_detect_never_raises_on_a_malformed_note(self, monkeypatch: pytest.MonkeyPatch) -> None:
        clock = FakeClock()
        names = _names(4)
        sessions = Sessions(monkeypatch, SlowEngine(clock, names, cost=1.0), clock)
        for note in _malformed_notes(names).values():
            sessions.note.note = note
            _, rung = sessions.rung()
            assert rung.detect().pending

    def test_a_progress_object_that_raises_everywhere_costs_the_note_and_nothing_else(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Whatever a progress object does, the rung's outcome is what it
        would be with no note, and each failure is a logged warning."""

        class Exploding:
            def __getattr__(self, name: str):
                def boom(*a, **kw):
                    raise RuntimeError(f"{name} blew up")

                return boom

        clock = FakeClock()
        names = _names(4)
        engine = SlowEngine(clock, names, cost=1.0, legacy={names[1]: 2})
        sessions = Sessions(monkeypatch, engine, clock)
        registry, rung = sessions.rung()
        rung.progress = Exploding()

        assert rung.detect().pending
        with structlog.testing.capture_logs() as logs:
            report = LadderRunner(registry, sessions.ledger, package_version_fn=lambda: _V).run()

        assert _outcome(report) is RungOutcome.RECORDED
        assert sessions.backfilled == [names[1]]
        failed = {e["operation"] for e in logs if e["event"] == "census_progress_failed"}
        assert {"usable", "invalidate", "clear", "end_session"} <= failed


    def test_an_entry_usable_vouched_for_but_that_cannot_be_rebuilt_is_read_again(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        class Careless(CensusProgress):
            def usable(self, version, stored):
                return {n: {"s": 0, "t": _STAMP, "r": ["x"] * 6} for n in stored}

        clock = FakeClock()
        names = _names(3)
        engine = SlowEngine(clock, names, cost=1.0)
        sessions = Sessions(monkeypatch, engine, clock)
        registry, rung = sessions.rung()
        rung.progress = Careless(sessions.note, now_fn=sessions.wall)

        report = LadderRunner(registry, sessions.ledger, package_version_fn=lambda: _V).run()

        assert _outcome(report) is RungOutcome.RECORDED
        assert engine.censused == names, "nothing unreadable was served; the engine answered instead"


class TestTheProgressDefaultFollowsTheMemo:
    def test_setting_the_memo_after_construction_cannot_leave_a_live_t2_note(self) -> None:
        rung = Rdr192ManifestBackfillRung()
        assert rung._progress()._store is not None, "the default memo is the T2 one, so the note is T2 too"
        rung.memo = MemoryNote()
        assert rung._progress()._store is None, "an injected memo disables the note without touching rung.progress"
