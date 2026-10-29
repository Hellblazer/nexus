# SPDX-License-Identifier: AGPL-3.0-or-later
"""RDR-235 Phase 1 step 3 (bead nexus-5r2sc.3.6): the upgrade-ladder rung that
moves the aspect queue table's waiting rows to ``aspects`` tuples.

Placed in ``tests/upgrade/`` beside the other rung tests (the bead named
``tests/test_aspect_queue_migration_rung.py``; the convention won).

Three layers:

* unit cases (RDR-235 Test Plan scenario 23), with the table, the tuple store and
  the flag injected through the rung's constructor, so each call and its ORDER is
  observable: out first, then the delete;
* the registry and runner pins (the skips are not recorded, R2);
* real-engine cases against the substrate the unit suite boots: the new
  ``list_waiting`` read on ``HttpAspectQueue``, and the whole move.
"""
from __future__ import annotations

import json
import re
import uuid
import itertools
from dataclasses import dataclass, field
from pathlib import Path

import httpx
import pytest

import nexus.corpus as corpus
import nexus.upgrade_ladder.rungs.aspect_queue_tuples as rung_module
from nexus.db.t2.http_aspect_queue import (
    ASPECT_QUEUE_ENV,
    ASPECTS_KEYS,
    ASPECTS_SUBSPACE,
    LIST_WAITING_ENGINE_RELEASE,
    AspectQueueListWaitingUnsupportedError,
    HttpAspectQueue,
    WaitingRow,
    _identity_body,
)
from nexus.db.t2.http_tuple_store import HttpTupleStore, UnknownSubspaceError
from nexus.upgrade_ladder.deadline import Deadline, bound
from nexus.upgrade_ladder.protocol import ConvergeOutcome, Rung, RungStatus
from nexus.upgrade_ladder.registry import (
    RUNG_ASPECT_QUEUE_TUPLES,
    RUNG_ORDER,
    LadderRegistry,
    default_registry,
)
from nexus.upgrade_ladder.runner import LadderRunner, RungOutcome
from nexus.upgrade_ladder.rungs.aspect_queue_tuples import (
    DEFAULT_BATCH_LIMIT,
    TEMPLATE_RETENTION_S,
    AspectQueueTuplesRung,
    clamp_delay,
)
from tests._catalog_fixture_ops import register_real_doc_id
from tests.upgrade.conftest import InMemoryCompletionLedger

_REPO = Path(__file__).resolve().parents[2]


# ── fakes: one shared event log, so call ORDER is assertable ────────────────


class Boom(RuntimeError):
    pass


@dataclass
class World:
    """The table and the tuple space the rung moves rows between."""

    rows: list[WaitingRow] = field(default_factory=list)
    failed: list[WaitingRow] = field(default_factory=list)
    template: bool = True
    route: bool = True
    read_error: Exception | None = None
    events: list[tuple] = field(default_factory=list)
    tasks: dict[str, dict] = field(default_factory=dict)
    crash_on_delete: bool = False
    flag: str = "tuples"
    # a read that also returns failed rows: the rung must not trust the route's filter alone
    leak_failed: bool = False
    # nonce -> an exception the out_replace for that task raises
    out_errors: dict[str, Exception] = field(default_factory=dict)

    # table side ----------------------------------------------------------
    def list_waiting(self, limit: int | None = None) -> list[WaitingRow]:
        self.events.append(("list_waiting",))
        if self.read_error is not None:
            raise self.read_error
        if not self.route:
            raise AspectQueueListWaitingUnsupportedError("no route")
        out = list(self.rows)
        if self.leak_failed:
            out += self.failed
        return out

    def mark_done(self, collection: str = "", source_path: str = "", *, doc_id: str = "") -> int:
        self.events.append(("mark_done", collection, source_path, doc_id))
        if self.crash_on_delete:
            raise Boom("crash between out and delete")
        before = len(self.rows)
        self.rows = [r for r in self.rows if (r.collection, r.source_path) != (collection, source_path)]
        return before - len(self.rows)

    # tuple side ----------------------------------------------------------
    def subspace_stats(self, subspace: str, *, timeout: float | None = None):
        self.events.append(("subspace_stats", subspace))
        if not self.template:
            raise UnknownSubspaceError("no aspects template")
        return object()

    def out_replace(self, subspace, keys, dims, body, *, nonce, ttl_seconds=None, delay_s=None):
        self.events.append(("out_replace", subspace, dict(keys), body, nonce, delay_s))
        if nonce in self.out_errors:
            raise self.out_errors[nonce]
        existed = nonce in self.tasks
        self.tasks[nonce] = {"body": body, "delay_s": delay_s, "keys": dict(keys)}
        return "id-" + nonce, "replaced" if existed else "created"

    def close(self) -> None:
        pass

    # wiring --------------------------------------------------------------
    def rung(self, **kw) -> AspectQueueTuplesRung:
        return AspectQueueTuplesRung(
            mode_fn=lambda: self.flag,
            queue_factory=lambda: self,
            tuples_factory=lambda: self,
            **kw,
        )

    def calls(self, name: str) -> list[tuple]:
        return [e for e in self.events if e[0] == name]


def _row(path: str, doc: str, *, status="pending", delay=0, retries=0, coll="docs__x__m__v1", h="h") -> WaitingRow:
    return WaitingRow(
        collection=coll, source_path=path, content_hash=h, doc_id=doc,
        retry_count=retries, status=status, delay_s=delay,
    )


class LogRecorder:
    """Stands in for the rung's logger: the suite filters structlog at WARNING,
    so an info line is only observable through the logger object itself."""

    def __init__(self) -> None:
        self.lines: list[dict] = []

    def _rec(self, level: str):
        def record(event: str, **kw) -> None:
            self.lines.append({"event": event, "level": level, **kw})
        return record

    def __getattr__(self, level: str):
        return self._rec(level)

    def events(self, name: str) -> list[dict]:
        return [ln for ln in self.lines if ln["event"] == name]


@pytest.fixture
def logs(monkeypatch: pytest.MonkeyPatch) -> LogRecorder:
    rec = LogRecorder()
    monkeypatch.setattr(rung_module, "_log", rec)
    return rec


class Report:
    def __init__(self) -> None:
        self.events: list[tuple] = []

    def emit(self, event: str, **fields: object) -> None:
        self.events.append((event, fields))


# ── scenario 23: what a converge does ───────────────────────────────────────


class TestTheMove:
    def test_a_pending_row_becomes_one_task_out_first_then_delete(self) -> None:
        w = World(rows=[_row("/a.md", "1.1.1", delay=90, h="hh")])
        result = w.rung().converge(Report())
        assert result.outcome is ConvergeOutcome.COMPLETED
        steps = [e for e in w.events if e[0] in ("out_replace", "mark_done")]
        assert [s[0] for s in steps] == ["out_replace", "mark_done"], "the task is written BEFORE the row is deleted"
        _, subspace, keys, body, nonce, delay = steps[0]
        assert (subspace, keys, nonce, delay) == (ASPECTS_SUBSPACE, ASPECTS_KEYS, "1.1.1", 90)
        assert steps[1] == ("mark_done", "docs__x__m__v1", "/a.md", "")
        assert w.rows == []
        assert len(w.tasks) == 1

    def test_the_body_is_the_producers_identity_body_with_no_content(self) -> None:
        w = World(rows=[_row("/a.md", "1.1.1", h="hh")])
        w.rung().converge(Report())
        body = w.tasks["1.1.1"]["body"]
        assert body == _identity_body("docs__x__m__v1", "/a.md", "1.1.1", "hh")
        assert json.loads(body) == {
            "collection": "docs__x__m__v1", "source_path": "/a.md",
            "doc_id": "1.1.1", "content_hash": "hh",
        }

    def test_an_in_progress_row_moves_as_pending_with_no_delay(self) -> None:
        # A stale delay on an in_progress row (it carries the next_retry_at of a
        # past retry) must not hold a task a worker was already running.
        w = World(rows=[_row("/b.md", "1.1.2", status="in_progress", delay=300)])
        w.rung().converge(Report())
        (call,) = w.calls("out_replace")
        assert call[5] == 0
        assert w.rows == []

    def test_a_failed_row_never_moves_even_if_the_read_returned_it(self) -> None:
        failed = _row("/f.md", "1.1.3", status="failed")
        w = World(rows=[_row("/a.md", "1.1.1")], failed=[failed], leak_failed=True)
        w.rung().converge(Report())
        assert [c[4] for c in w.calls("out_replace")] == ["1.1.1"]
        assert ("mark_done", failed.collection, "/f.md", "") not in w.events

    def test_the_retry_count_has_no_effect_on_the_out(self) -> None:
        a = World(rows=[_row("/a.md", "1.1.1", retries=0)])
        b = World(rows=[_row("/a.md", "1.1.1", retries=4)])
        a.rung().converge(Report())
        b.rung().converge(Report())
        assert a.calls("out_replace") == b.calls("out_replace"), "attempts reset to 0 whatever retry_count was (O3)"

    @pytest.mark.parametrize("given, sent", [
        (0, 0), (-5, 0), (1, 1), (59, 59), (TEMPLATE_RETENTION_S, TEMPLATE_RETENTION_S),
        (TEMPLATE_RETENTION_S + 1, TEMPLATE_RETENTION_S), (10**12, TEMPLATE_RETENTION_S),
        (None, 0), (2.2, 3), (float("inf"), TEMPLATE_RETENTION_S), (float("-inf"), 0), (float("nan"), 0),
    ])
    def test_clamp_delay_is_whole_seconds_inside_the_template_retention(self, given, sent) -> None:
        assert clamp_delay(given) == sent

    @pytest.mark.parametrize("given, sent", [
        (0, 0), (90, 90), (-5, 0), (TEMPLATE_RETENTION_S + 1, TEMPLATE_RETENTION_S),
    ])
    def test_the_out_carries_the_clamped_delay(self, given, sent) -> None:
        w = World(rows=[_row("/a.md", "1.1.1", delay=given)])
        w.rung().converge(Report())
        assert w.calls("out_replace")[0][5] == sent

    def test_one_oversized_delay_does_not_abort_the_batch(self) -> None:
        w = World(rows=[
            _row("/a.md", "1.1.1", delay=10**9), _row("/b.md", "1.1.2", delay=-3), _row("/c.md", "1.1.3"),
        ])
        result = w.rung().converge(Report())
        assert result.outcome is ConvergeOutcome.COMPLETED
        assert [c[5] for c in w.calls("out_replace")] == [TEMPLATE_RETENTION_S, 0, 0]
        assert w.rows == []

    def test_a_row_without_a_doc_id_stays_in_the_table_and_is_counted_once(self, logs) -> None:
        w = World(rows=[_row("/a.md", "1.1.1"), _row("/legacy.md", "")])
        result = w.rung().converge(Report())
        assert result.outcome is ConvergeOutcome.COMPLETED
        assert [r.source_path for r in w.rows] == ["/legacy.md"]
        assert [c[4] for c in w.calls("out_replace")] == ["1.1.1"], "no nonce is invented"
        counted = logs.events("aspect_queue_rung_rows_without_doc_id_left_in_table")
        assert len(counted) == 1 and counted[0]["count"] == 1
        # and it does not keep the rung from converging
        rung = w.rung()
        assert rung.detect() == RungStatus(applicable=True, converged=True)
        assert rung.verify() is True

    def test_a_full_tuple_space_defers_with_a_detail_instead_of_failing(self) -> None:
        """nexus-5r2sc.3.9 M1: MaxLiveRowsExceeded is not a bug in the rung; it clears
        as tasks are consumed. DEFERRED, with the rows already moved kept and the
        rest untouched, never an exception that reads as FAILED on every run."""
        from nexus.db.t2.http_tuple_store import MaxLiveRowsExceededError

        w = World(
            rows=[_row(f"/{i}.md", f"1.1.{i}") for i in range(3)],
            out_errors={"1.1.1": MaxLiveRowsExceededError("ceiling")},
        )
        result = w.rung().converge(Report())
        assert result.outcome is ConvergeOutcome.DEFERRED
        assert "ceiling" in result.detail or "live" in result.detail, result.detail
        assert [r.source_path for r in w.rows] == ["/1.md", "/2.md"], "row 0 moved; the refused row and the rest stay"
        assert list(w.tasks) == ["1.1.0"]

    def test_a_row_too_large_for_a_task_is_left_counted_and_the_rest_move(self, logs) -> None:
        """nexus-5r2sc.3.9 M1: an identity body over the 4,096-byte cap raised on every
        run and wedged the rung on the first FIFO row. The producer skips such a
        row; the rung now logs it, counts it, leaves it in the table and moves
        its neighbours. It is excluded for the rest of this run, so it is not
        retried per batch and does not keep the rung from converging."""
        from nexus.db.t2.http_tuple_store import TooLargeError

        w = World(
            rows=[_row("/big.md", "1.1.0"), _row("/a.md", "1.1.1"), _row("/b.md", "1.1.2")],
            out_errors={"1.1.0": TooLargeError("body over the cap")},
        )
        rung = w.rung(batch_limit=2)
        first = rung.converge(Report())
        assert first.outcome is ConvergeOutcome.RESUMABLE
        second = rung.converge(Report())
        assert second.outcome is ConvergeOutcome.COMPLETED, second
        assert [r.source_path for r in w.rows] == ["/big.md"], "only the oversize row stays"
        assert sorted(w.tasks) == ["1.1.1", "1.1.2"]
        assert [c[4] for c in w.calls("out_replace")].count("1.1.0") == 1, "tried once, then excluded this run"
        (line,) = logs.events("aspect_queue_rung_row_too_large")
        assert line["doc_id"] == "1.1.0" and line["source_path"] == "/big.md"
        assert rung.detect() == RungStatus(applicable=True, converged=True)
        assert rung.verify() is True

    def test_a_crash_between_out_and_delete_leaves_the_row_and_one_task(self) -> None:
        w = World(rows=[_row("/a.md", "1.1.1", delay=5)], crash_on_delete=True)
        rung = w.rung()
        with pytest.raises(Boom):
            rung.converge(Report())
        assert len(w.rows) == 1, "the row is still in the table: in both places, never in neither"
        assert len(w.tasks) == 1
        # the re-run replaces the same task and deletes the row
        w.crash_on_delete = False
        result = rung.converge(Report())
        assert result.outcome is ConvergeOutcome.COMPLETED
        assert w.rows == []
        assert len(w.tasks) == 1, "one task, not two"
        assert [c[4] for c in w.calls("out_replace")] == ["1.1.1", "1.1.1"]

    def test_a_second_run_changes_nothing(self) -> None:
        w = World(rows=[_row("/a.md", "1.1.1"), _row("/b.md", "1.1.2", status="in_progress")])
        rung = w.rung()
        rung.converge(Report())
        w.events.clear()
        snapshot = dict(w.tasks)
        assert rung.detect() == RungStatus(applicable=True, converged=True)
        again = rung.converge(Report())
        assert again.outcome is ConvergeOutcome.COMPLETED
        assert w.calls("out_replace") == [] and w.calls("mark_done") == []
        assert w.tasks == snapshot

    def test_batches_are_resumable_and_the_last_one_completes(self) -> None:
        w = World(rows=[_row(f"/{i}.md", f"1.1.{i}") for i in range(5)])
        rung = w.rung(batch_limit=2)
        first = rung.converge(Report())
        assert first.outcome is ConvergeOutcome.RESUMABLE and len(w.rows) == 3
        assert rung.converge(Report()).outcome is ConvergeOutcome.RESUMABLE and len(w.rows) == 1
        assert rung.converge(Report()).outcome is ConvergeOutcome.COMPLETED and w.rows == []
        assert len(w.tasks) == 5

    def test_the_default_batch_is_bounded(self) -> None:
        assert 0 < DEFAULT_BATCH_LIMIT <= 1000

    def test_an_expired_deadline_stops_between_rows_and_defers(self) -> None:
        w = World(rows=[_row(f"/{i}.md", f"1.1.{i}") for i in range(3)])
        clock = itertools.chain([0.0], itertools.repeat(99.0))  # started at 0, past the limit from then on
        with bound(Deadline(60, clock=lambda: next(clock))):
            result = w.rung().converge(Report())
        assert result.outcome is ConvergeOutcome.DEFERRED
        assert len(w.tasks) == 1, "the first row moves; the limit is checked before the second"
        assert len(w.rows) == 2, "and the moved row was also deleted: stopped at a row boundary"

    def test_converge_without_the_flag_moves_nothing(self) -> None:
        w = World(rows=[_row("/a.md", "1.1.1")], flag="table")
        result = w.rung().converge(Report())
        assert result.outcome is ConvergeOutcome.DEFERRED
        assert w.calls("out_replace") == [] and len(w.rows) == 1


# ── the skips: reported through applicable=False (R2) ────────────────────────


class TestSkips:
    def test_the_flag_at_table_is_not_applicable_and_sends_nothing(self, logs) -> None:
        w = World(rows=[_row("/a.md", "1.1.1")], flag="table")
        status = w.rung().detect()
        assert status.applicable is False and status.converged is False
        assert w.events == [], "a table-mode install makes no request at all"
        assert len(logs.events("aspect_queue_rung_skipped")) == 1
        assert ASPECT_QUEUE_ENV in status.pending_detail

    def test_an_engine_without_the_aspects_template_is_not_applicable(self, logs) -> None:
        w = World(rows=[_row("/a.md", "1.1.1")], template=False)
        status = w.rung().detect()
        assert status.applicable is False
        assert w.calls("list_waiting") == [] and w.calls("out_replace") == []
        assert len(logs.events("aspect_queue_rung_skipped")) == 1

    def test_an_engine_without_list_waiting_is_not_applicable_naming_the_release(self) -> None:
        w = World(rows=[_row("/a.md", "1.1.1")], route=False)
        status = w.rung().detect()
        assert status.applicable is False
        assert LIST_WAITING_ENGINE_RELEASE in status.pending_detail
        assert w.calls("out_replace") == []

    def test_an_unreadable_table_is_not_a_skip_and_not_converged(self) -> None:
        w = World(read_error=httpx.ConnectError("down"))
        with pytest.raises(httpx.ConnectError):
            w.rung().detect()

    def test_a_skip_is_not_recorded_and_the_rung_applies_after_the_flag_flips(self) -> None:
        """R2: the runner records nothing for a not-applicable rung, so it is
        detected again, and moves the rows, once the flag is 'tuples'."""
        w = World(rows=[_row("/a.md", "1.1.1", delay=7)], flag="table")
        ledger = InMemoryCompletionLedger()
        registry = LadderRegistry((w.rung(),))
        runner = LadderRunner(registry, ledger, package_version_fn=lambda: "9.9.9")

        run1 = runner.run()
        assert [r.outcome for r in run1.runs] == [RungOutcome.SKIPPED_NOT_APPLICABLE]
        assert ledger.verified_rungs() == frozenset(), "a skip leaves no completion record"
        assert w.calls("out_replace") == []

        w.flag = "tuples"  # the Phase 2 default flip
        run2 = runner.run()
        assert [r.outcome for r in run2.runs] == [RungOutcome.RECORDED]
        assert ledger.verified_rungs() == frozenset({RUNG_ASPECT_QUEUE_TUPLES})
        assert len(w.tasks) == 1 and w.rows == []

    def test_a_recorded_rung_converges_again_when_an_old_client_enqueues_later(self) -> None:
        w = World(rows=[])
        ledger = InMemoryCompletionLedger()
        runner = LadderRunner(LadderRegistry((w.rung(),)), ledger, package_version_fn=lambda: "9.9.9")
        assert [r.outcome for r in runner.run().runs] == [RungOutcome.RECORDED], "recorded with zero rows"
        w.rows.append(_row("/late.md", "1.1.9"))
        status = w.rung().detect()
        assert status.applicable and not status.converged, "detect re-reads the table each run"
        runner.run()
        assert w.rows == [] and "1.1.9" in w.tasks


# ── verify: a positive signal only ───────────────────────────────────────────


class TestVerify:
    def test_true_when_the_read_succeeded_and_shows_nothing_to_move(self) -> None:
        assert World().rung().verify() is True

    def test_false_while_a_movable_row_remains(self) -> None:
        w = World(rows=[_row("/a.md", "1.1.1")])
        rung = w.rung()
        assert rung.verify() is False
        assert "1 queued row" in rung.verify_detail()

    def test_an_unreachable_read_is_false_with_a_detail_never_zero_rows(self) -> None:
        w = World(read_error=httpx.ConnectError("down"))
        rung = w.rung()
        assert rung.verify() is False
        assert "could not read" in rung.verify_detail() and "down" in rung.verify_detail()

    def test_a_skip_reason_is_false_not_true(self) -> None:
        rung = World(route=False).rung()
        assert rung.verify() is False
        assert LIST_WAITING_ENGINE_RELEASE in rung.verify_detail()

    def test_the_runner_reports_a_verify_that_could_not_read_as_verify_failed(self) -> None:
        w = World(rows=[_row("/a.md", "1.1.1")])
        rung = w.rung()
        orig = w.list_waiting
        calls = {"n": 0}

        def flaky(limit=None):
            calls["n"] += 1
            if calls["n"] >= 3:  # detect, converge read fine; verify cannot read
                raise httpx.ConnectError("down")
            return orig(limit)

        w.list_waiting = flaky  # type: ignore[method-assign]
        ledger = InMemoryCompletionLedger()
        report = LadderRunner(LadderRegistry((rung,)), ledger, package_version_fn=lambda: "9").run()
        assert [r.outcome for r in report.runs] == [RungOutcome.VERIFY_FAILED]
        assert "could not read" in report.runs[0].detail
        assert ledger.verified_rungs() == frozenset()


# ── registration and constants ───────────────────────────────────────────────


class TestRegistration:
    def test_the_rung_is_in_the_canonical_order_and_the_production_registry(self) -> None:
        assert RUNG_ASPECT_QUEUE_TUPLES == "aspect-queue-tuples"
        assert RUNG_ASPECT_QUEUE_TUPLES in RUNG_ORDER
        names = [r.name for r in default_registry()]
        assert names == list(RUNG_ORDER)
        assert RUNG_ASPECT_QUEUE_TUPLES in names

    def test_the_rung_conforms_to_the_protocol(self) -> None:
        assert isinstance(World().rung(), Rung)

    def test_the_default_rung_is_not_applicable_with_the_flag_unset(self, monkeypatch) -> None:
        monkeypatch.delenv(ASPECT_QUEUE_ENV, raising=False)
        rung = AspectQueueTuplesRung()
        status = rung.detect()
        assert status.applicable is False

    def test_the_retention_constant_equals_the_template(self) -> None:
        text = (_REPO / "service/src/main/resources/tuples/templates/aspects.yaml").read_text()
        (m,) = re.findall(r"^retention_seconds:\s*(\d+)\s*$", text, flags=re.M)
        assert int(m) == TEMPLATE_RETENTION_S

    def test_the_list_waiting_release_is_the_next_engine_tag(self) -> None:
        assert LIST_WAITING_ENGINE_RELEASE == "engine-service-v0.1.170"


# ── HttpAspectQueue.list_waiting over a mock transport ───────────────────────


def _queue_over(handler) -> HttpAspectQueue:
    client = httpx.Client(transport=httpx.MockTransport(handler), base_url="http://engine.test")
    return HttpAspectQueue("http://engine.test", _token="t", client=client)


class TestListWaitingClient:
    def test_it_gets_the_route_and_returns_rows_with_status_and_delay(self) -> None:
        seen: list[httpx.Request] = []

        def handler(req: httpx.Request) -> httpx.Response:
            seen.append(req)
            return httpx.Response(200, json=[
                {"collection": "c", "source_path": "/a", "content_hash": "h", "retry_count": 2,
                 "doc_id": "1.1.1", "status": "pending", "delay_s": 41},
                {"collection": "c", "source_path": "/b", "content_hash": None, "retry_count": 0,
                 "doc_id": "1.1.2", "status": "in_progress", "delay_s": 0},
            ])

        q = _queue_over(handler)
        rows = q.list_waiting(limit=50)
        assert seen[0].method == "GET" and seen[0].url.path == "/v1/aspects/queue/list_waiting"
        assert seen[0].url.params["limit"] == "50"
        assert rows == [
            WaitingRow("c", "/a", "h", "1.1.1", 2, "pending", 41),
            WaitingRow("c", "/b", "", "1.1.2", 0, "in_progress", 0),
        ]

    def test_without_a_limit_none_is_sent(self) -> None:
        seen: list[httpx.Request] = []

        def handler(req: httpx.Request) -> httpx.Response:
            seen.append(req)
            return httpx.Response(200, json=[])

        _queue_over(handler).list_waiting()
        assert "limit" not in seen[0].url.params

    def test_a_404_is_the_typed_unsupported_error_naming_the_release(self) -> None:
        def handler(req: httpx.Request) -> httpx.Response:
            return httpx.Response(404, json={"error": "unknown aspects op: /queue/list_waiting"})

        with pytest.raises(AspectQueueListWaitingUnsupportedError, match=re.escape(LIST_WAITING_ENGINE_RELEASE)):
            _queue_over(handler).list_waiting()

    def test_any_other_status_propagates(self) -> None:
        def handler(req: httpx.Request) -> httpx.Response:
            return httpx.Response(500, json={"error": "internal server error"})

        with pytest.raises(httpx.HTTPStatusError):
            _queue_over(handler).list_waiting()


# ── real engine ──────────────────────────────────────────────────────────────


def _coll() -> str:
    return f"docs__nx235m-{uuid.uuid4().hex[:8]}__bge-base-en-v15-768__v1"


@pytest.fixture
def real(monkeypatch: pytest.MonkeyPatch):
    corpus._REGISTERED_COLLECTIONS.clear()
    corpus._REGISTERED_COLLECTIONS_SCOPED.clear()
    monkeypatch.setenv("NX_ASPECT_WORKER_AUTOSTART", "0")
    monkeypatch.setattr("nexus.aspect_worker.signal_aspect_wake", lambda *a, **k: None)
    monkeypatch.delenv(ASPECT_QUEUE_ENV, raising=False)
    queue = HttpAspectQueue()
    store = HttpTupleStore()
    yield queue, store
    queue.close()
    store.close()
    corpus._REGISTERED_COLLECTIONS.clear()
    corpus._REGISTERED_COLLECTIONS_SCOPED.clear()


def _claim_all(store: HttpTupleStore) -> list[dict]:
    out = []
    for i in range(50):  # bounded: a claim that never runs dry must fail, not spin
        # one claimant per claim: a claimant that already holds a live claim is handed it again
        got = store.inp(ASPECTS_SUBSPACE, dict(ASPECTS_KEYS), claimant=f"t:{i}", lease_s=60)
        if got is None:
            return out
        row, _claim = got
        out.append(json.loads(row.body))
    raise AssertionError(f"50 claims and the queue is not dry: {out[:6]}")


@pytest.mark.usefixtures("t2_service_env")
class TestListWaitingRealEngine:
    def test_pending_and_in_progress_rows_come_back_with_status_delay_and_no_content(self, real) -> None:
        queue, _ = real
        coll = _coll()
        docs = [register_real_doc_id(title=f"d{i}") for i in range(4)]
        for i, d in enumerate(docs):
            queue.enqueue(coll, f"/p{i}.md", content_hash=f"h{i}", content="text must not return", doc_id=d)
        assert queue.claim_next().source_path == "/p0.md"          # p0 -> in_progress
        queue.mark_retry(coll, "/p1.md", interval_seconds=600)     # p1 -> pending, backed off
        queue.mark_failed(coll, "/p3.md", "boom")                  # p3 -> failed

        rows = {r.source_path: r for r in queue.list_waiting()}
        assert sorted(rows) == ["/p0.md", "/p1.md", "/p2.md"], "failed rows are not waiting"
        assert rows["/p0.md"].status == "in_progress" and rows["/p0.md"].delay_s == 0
        assert rows["/p1.md"].status == "pending" and rows["/p1.md"].retry_count == 1
        assert 590 <= rows["/p1.md"].delay_s <= 600, "seconds to next_retry_at, on the database clock"
        assert rows["/p2.md"].status == "pending" and rows["/p2.md"].delay_s == 0
        assert rows["/p2.md"].doc_id == docs[2] and rows["/p2.md"].content_hash == "h2"
        assert len(queue.list_waiting(limit=2)) == 2
        # the read changed nothing: p0 is still in_progress
        assert {r.source_path: r.status for r in queue.list_waiting()}["/p0.md"] == "in_progress"


@pytest.mark.usefixtures("t2_service_env")
class TestTheMoveRealEngine:
    def test_three_table_rows_become_three_tasks_and_the_table_empties(
        self, real, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        queue, store = real
        coll = _coll()
        d = [register_real_doc_id(title=f"m{i}") for i in range(5)]
        queue.enqueue(coll, "/due.md", content_hash="h-due", content="big note text", doc_id=d[0])
        queue.enqueue(coll, "/later.md", content_hash="h-later", doc_id=d[1])
        queue.enqueue(coll, "/held.md", content_hash="h-held", doc_id=d[2])
        # a failed row and a pending sibling of it that share one doc_id
        queue.enqueue(coll, "/dead.md", content_hash="h-dead", doc_id=d[3])
        queue.enqueue(coll, "/dead-sibling.md", content_hash="h-sib", doc_id=d[3])
        assert queue.claim_next().source_path == "/due.md"      # due -> in_progress (oldest)
        queue.mark_retry(coll, "/due.md", interval_seconds=0)    # back to pending, due now (retry 1)
        assert queue.claim_next().source_path == "/due.md"      # claim it again: in_progress
        queue.mark_retry(coll, "/later.md", interval_seconds=900)
        assert queue.claim_next().source_path == "/held.md"     # held -> in_progress, a worker holds it
        queue.mark_failed(coll, "/dead.md", "boom")

        monkeypatch.setenv(ASPECT_QUEUE_ENV, "tuples")
        rung = AspectQueueTuplesRung()
        status = rung.detect()
        assert status.applicable and not status.converged, status
        result = rung.converge(Report())
        assert result.outcome is ConvergeOutcome.COMPLETED, result
        assert rung.verify() is True, rung.verify_detail()
        assert rung.detect() == RungStatus(applicable=True, converged=True)

        # table: only the failed row is left
        monkeypatch.delenv(ASPECT_QUEUE_ENV, raising=False)
        assert queue.list_waiting() == []
        assert [r.source_path for r in queue.list_failed()] == ["/dead.md"], "a failed row never moves, nor is deleted"

        # tasks: one per document (the sibling shares d[3]'s task), identity bodies only
        tasks = store.rd(ASPECTS_SUBSPACE, dict(ASPECTS_KEYS), n=300)
        bodies = {json.loads(t.body)["doc_id"]: json.loads(t.body) for t in tasks}
        assert sorted(bodies) == sorted([d[0], d[1], d[2], d[3]])
        assert bodies[d[0]] == {"collection": coll, "source_path": "/due.md", "doc_id": d[0], "content_hash": "h-due"}
        assert all(set(b) == {"collection", "source_path", "doc_id", "content_hash"} for b in bodies.values())
        assert len(tasks) == 4

        # the delay: the 900 s row is not claimable now, the others are
        claimed = _claim_all(store)
        assert sorted(b["doc_id"] for b in claimed) == sorted([d[0], d[2], d[3]]), "the backed-off row waits"

        # a second run changes nothing
        monkeypatch.setenv(ASPECT_QUEUE_ENV, "tuples")
        again = AspectQueueTuplesRung()
        assert again.detect() == RungStatus(applicable=True, converged=True)
        assert again.converge(Report()).outcome is ConvergeOutcome.COMPLETED
        assert len(store.rd(ASPECTS_SUBSPACE, dict(ASPECTS_KEYS), n=300)) == 4

    def test_the_flag_at_table_moves_nothing(self, real) -> None:
        queue, store = real
        coll = _coll()
        queue.enqueue(coll, "/a.md", doc_id=register_real_doc_id(title="t"))
        status = AspectQueueTuplesRung().detect()
        assert status.applicable is False
        assert store.rd(ASPECTS_SUBSPACE, dict(ASPECTS_KEYS), n=300) == []
        assert len(queue.list_waiting()) == 1

    def test_a_real_crash_between_out_and_delete_then_a_rerun_leaves_one_task(
        self, real, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        queue, store = real
        coll = _coll()
        doc = register_real_doc_id(title="crash")
        queue.enqueue(coll, "/a.md", content_hash="h", doc_id=doc)
        monkeypatch.setenv(ASPECT_QUEUE_ENV, "tuples")

        class Crashing(HttpAspectQueue):
            def mark_done(self, *a, **k):
                raise Boom("killed after the out")

        rung = AspectQueueTuplesRung(queue_factory=Crashing)
        with pytest.raises(Boom):
            rung.converge(Report())
        assert len(queue.list_waiting()) == 1, "the row is still in the table"
        assert len(store.rd(ASPECTS_SUBSPACE, dict(ASPECTS_KEYS), n=300)) == 1, "and the task is out"

        assert AspectQueueTuplesRung().converge(Report()).outcome is ConvergeOutcome.COMPLETED
        assert queue.list_waiting() == []
        assert len(store.rd(ASPECTS_SUBSPACE, dict(ASPECTS_KEYS), n=300)) == 1, "one task, not two"

