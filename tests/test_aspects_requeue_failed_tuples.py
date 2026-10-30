# SPDX-License-Identifier: AGPL-3.0-or-later
"""RDR-235 (bead nexus-5r2sc.3.4): the readers of the old aspect-queue table
also read the ``aspects`` tuple subspace.

Covers the two readers that are not the drain gate (that is
``tests/test_aspect_drain_protocol.py``):

  * ``nx aspects requeue-failed`` lists the table's failed rows PLUS the dead
    tasks (``rd`` on ``aspects`` filtered on ``claim_state`` ``dead``, paged
    with ``since``) and revives each dead task through ``out`` ``mode=replace``
    (Test Plan scenarios 8 and 11, revive halves).
  * ``_best_effort_queue_depth`` is the table's pending count plus the
    subspace's ``available``.

The real-engine class boots a dead task the way production does: ``out``,
``inp`` and ``nack(dead=True)``. The stub classes pin what the engine cannot be
made to do cheaply (a read cap smaller than the data, a template the engine
lacks, one side failing). The depth sum's real-engine check is step 4 of the
dead-task journey.
"""
from __future__ import annotations

import json
import uuid
from pathlib import Path

import pytest
from click.testing import CliRunner

from nexus.commands.aspects import aspects_group
from nexus.db.t2.http_tuple_store import HttpTupleStore, UnknownSubspaceError
from nexus.db.t2.records import SubspaceCensus, TupleRow

_KEYS = {"queue": "aspects"}


def _doc() -> str:
    return f"9.{uuid.uuid4().int % 10**9}.{uuid.uuid4().int % 10**6}"


def _body(collection: str, doc_id: str, source_path: str = "a.pdf") -> str:
    return json.dumps(
        {
            "collection": collection, "source_path": source_path,
            "doc_id": doc_id, "content_hash": "h",
        },
        separators=(",", ":"),
    )


def _census(**kw) -> SubspaceCensus:
    base = dict(
        subspace="aspects", total=0, available=0, claimed=0, dead=0, consumed=0,
        expired_unpurged=0, oldest_created_at=None, newest_created_at=None, lapsed=0,
    )
    base.update(kw)
    return SubspaceCensus(**base)


# ── real engine ──────────────────────────────────────────────────────────────
#
# Two journeys (nexus-9edxe folded seven engine tests into them, one tenant
# each): the dead-task journey (paging, revive, scope, then the queue depth the
# hook reports) and the failed-table-row journey (the verb and the doctor over
# the table and the tuple side together). A step that needs the subspace empty
# drains the available tasks first. Each assertion names its step; a failure in
# an early step hides the later ones.


@pytest.mark.t2_tenant
class TestRequeueFailedAgainstTheEngine:
    @pytest.fixture
    def _t2(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
        from nexus.db.t2 import T2Database

        db_path = tmp_path / "t2.db"
        monkeypatch.setattr("nexus.commands._helpers.default_db_path", lambda: db_path)
        import nexus.mcp_infra as infra

        def _direct_index_write(write_fn, **_kw):  # noqa: ANN001
            with T2Database(db_path) as db:
                return write_fn(db)

        monkeypatch.setattr(infra, "t2_index_write", _direct_index_write)
        return db_path

    @staticmethod
    def _make_dead(store: HttpTupleStore, collection: str, source_path: str = "a.pdf") -> str:
        """Needs the subspace to hold no available task: ``inp`` claims the oldest."""
        doc = _doc()
        tid = store.out("aspects", _KEYS, None, _body(collection, doc, source_path), nonce=doc)
        got = store.inp("aspects", _KEYS, claimant="t-dead", lease_s=60)
        assert got is not None and got[0].id == tid
        store.nack(got[1], "t-dead", dead=True)
        return tid

    @staticmethod
    def _drain_cli() -> int:
        """``nx aspects drain``'s exit code: 0 only when the table and the tuple side
        are both drained."""
        res = CliRunner().invoke(
            aspects_group, ["drain", "--timeout", "0.4", "--poll-interval", "0.05"],
        )
        return res.exit_code

    @staticmethod
    def _drain(store: HttpTupleStore) -> None:
        """Consume every available task (a consumed row is invisible to ``rd``), so
        the next step starts with nothing available. Dead tasks stay."""
        while (got := store.inp("aspects", _KEYS, claimant="t-drain", lease_s=60)) is not None:
            store.ack(got[1], "t-drain")

    def test_the_dead_task_journey_paging_revive_collection_scope_and_the_queue_depth(
        self, _t2: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        import nexus.commands.aspects as mod
        from nexus.aspect_worker import _best_effort_queue_depth
        from nexus.db.t2.http_aspect_queue import HttpAspectQueue
        from tests._catalog_fixture_ops import register_real_doc_id

        monkeypatch.setenv("NX_ASPECT_WORKER_AUTOSTART", "0")
        store = HttpTupleStore()
        assert _best_effort_queue_depth() == 0, "step 0: an empty queue reports zero on both sides"
        assert self._drain_cli() == 0, "step 0: drain: empty on both sides"

        # Step 1, since paging: five dead tasks interleaved with available ones,
        # read two rows per page: every dead task is found, so the loop pages by
        # ``since`` and does not stop at the first page.
        with monkeypatch.context() as page:
            page.setattr(mod, "_TASK_PAGE", 2)
            coll = "docs__nx235r-f__bge-base-en-v15-768__v1"
            for i in range(10):
                doc = _doc()
                store.out("aspects", _KEYS, None, _body(coll, doc, f"{i}.pdf"), nonce=doc)
            # Claim all ten oldest-first, then kill the even ones and return the
            # odd ones: dead and available tasks alternate in created_at order.
            claims = []
            for i in range(10):
                got = store.inp("aspects", _KEYS, claimant=f"t-page-{i}", lease_s=60)
                assert got is not None
                claims.append((i, got[1]))
            for i, claim in claims:
                store.nack(claim, f"t-page-{i}", dead=(i % 2 == 0))
            assert store.subspace_stats("aspects").dead == 5, "step 1: five dead before the verb"
            res = CliRunner().invoke(aspects_group, ["requeue-failed"])
            assert res.exit_code == 0, res.output
            assert "Revived 5 dead task(s)" in res.output, f"step 1: paging: {res.output}"
        census = store.subspace_stats("aspects")
        assert (census.dead, census.available) == (0, 10), f"step 1: {census}"
        self._drain(store)

        # Step 2, a dead task is listed, revived and comes back available.
        coll = "docs__nx235r-a__bge-base-en-v15-768__v1"
        tid = self._make_dead(store, coll)
        before = next(r for r in store.rd("aspects", _KEYS, n=300) if r.id == tid)
        assert before.claim_state == "dead"
        assert store.subspace_stats("aspects").dead == 1
        assert self._drain_cli() == 0, "step 2: drain: a dead task alone never blocks"

        def snapshot() -> list[tuple]:
            return [
                (r.id, r.claim_state, r.attempts, r.expires_at, r.created_at)
                for r in store.rd("aspects", _KEYS, n=300)
            ]

        pre_dry = snapshot()
        dry = CliRunner().invoke(aspects_group, ["requeue-failed", "--dry-run"])
        assert dry.exit_code == 0, dry.output
        assert "Would re-enqueue 1 dead task(s)" in dry.output, f"step 2: {dry.output}"
        assert coll in dry.output and "a.pdf" in dry.output
        assert snapshot() == pre_dry, "step 2: dry-run wrote"

        res = CliRunner().invoke(aspects_group, ["requeue-failed"])
        assert res.exit_code == 0, res.output
        assert "Revived 1 dead task(s)" in res.output, f"step 2: {res.output}"

        census = store.subspace_stats("aspects")
        assert (census.dead, census.available) == (0, 1), f"step 2: {census}"
        after = next(r for r in store.rd("aspects", _KEYS, n=300) if r.id == tid)
        assert after.claim_state is None and after.attempts == 0, after
        assert after.id == before.id, "step 2: one document stays one task id"
        assert after.body == before.body
        assert after.expires_at > before.expires_at, "step 2: the retention clock restarted"
        assert after.created_at >= before.created_at

        # The revived task is now an available one: a second run finds nothing
        # dead and never touches it.
        pre_again = snapshot()
        again = CliRunner().invoke(aspects_group, ["requeue-failed"])
        assert again.exit_code == 0, again.output
        assert "no failed rows" in again.output, f"step 2: {again.output}"
        assert snapshot() == pre_again, "step 2: an available task was touched"
        assert store.subspace_stats("aspects").available == 1
        # The drain gate (Test Plan scenario 21, drain half), on the revived task: an
        # available task blocks it, a claimed one blocks it, an acked one does not.
        assert self._drain_cli() == 1, "step 2: drain: an available task blocks"
        got = store.inp("aspects", _KEYS, claimant="drain-live", lease_s=60)
        assert got is not None
        assert self._drain_cli() == 1, "step 2: drain: a claimed task blocks"
        store.ack(got[1], "drain-live")
        assert self._drain_cli() == 0, "step 2: drain: acked, drained"

        # Step 3, collection scope filters on the body and the limit applies after it.
        a = "docs__nx235r-c1__bge-base-en-v15-768__v1"
        b = "docs__nx235r-c2__bge-base-en-v15-768__v1"
        # b's task is the OLDEST: a limit applied before the collection filter
        # would spend a slot on it and revive only one of a's.
        self._make_dead(store, b, "b.pdf")
        for i in range(3):
            self._make_dead(store, a, f"{i}.pdf")
        res = CliRunner().invoke(aspects_group, ["requeue-failed", "--collection", a, "--limit", "2"])
        assert res.exit_code == 0, res.output
        assert f"Revived 2 dead task(s) in {a}" in res.output, f"step 3: {res.output}"
        census = store.subspace_stats("aspects")
        assert (census.dead, census.available) == (2, 2), (
            f"step 3: b stays dead, one of a stays dead, two of a are back: {census}"
        )

        # Step 4, the depth the hook reports: the tuple side alone, then both sides.
        assert _best_effort_queue_depth() == 2, "step 4: tuples side alone"
        q = HttpAspectQueue()
        try:
            for i in range(3):
                q.enqueue("knowledge__nx235r", f"p{i}.pdf", content="c", doc_id=register_real_doc_id())
        finally:
            q.close()
        assert _best_effort_queue_depth() == 5, "step 4: both sides"

    @staticmethod
    def _failed_table_row(db, collection: str, source_path: str, doc_id: str) -> None:  # noqa: ANN001
        q = db.aspect_queue
        q.enqueue(collection, source_path, content="c", doc_id=doc_id)
        assert q.claim_next() is not None
        q.mark_failed(collection, source_path, "boom")

    def test_the_failed_table_row_journey_table_mode_tuples_mode_and_the_doctor(
        self, _t2: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Three steps over one tenant: the verb in table mode reports a table
        failure beside a dead task; in tuples mode it clears the failed table
        row and the doctor goes green; and with a legacy blank-doc_id row in the
        listing it reports and skips that row, never aborts (nexus-5r2sc.3.10
        Critical 2, Test Plan scenario 23's tail)."""
        import click

        from nexus.commands.doctor import _report_aspect_queue_service
        from nexus.db.t2 import T2Database
        from nexus.db.t2.http_aspect_queue import HttpAspectQueue
        from nexus.db.t2.records import QueueRow
        from tests._catalog_fixture_ops import register_real_doc_id

        store = HttpTupleStore()

        # Step 1, table mode: a table failed row and a dead task are both reported.
        with T2Database(_t2) as db:
            q = db.aspect_queue
            q.enqueue("knowledge__a", "x.pdf", content="c", doc_id=register_real_doc_id())
            assert q.claim_next() is not None
            q.mark_failed("knowledge__a", "x.pdf", "boom")
        self._make_dead(store, "docs__nx235r-g__bge-base-en-v15-768__v1")
        assert self._drain_cli() == 0, "step 1: drain: a failed table row and a dead task never block"
        dry =CliRunner().invoke(aspects_group, ["requeue-failed", "--dry-run"])
        assert dry.exit_code == 0, dry.output
        assert "Would re-enqueue 1 failed row(s)" in dry.output, f"step 1: {dry.output}"
        assert "Would re-enqueue 1 dead task(s)" in dry.output, f"step 1: {dry.output}"
        res = CliRunner().invoke(aspects_group, ["requeue-failed"])
        assert res.exit_code == 0, res.output
        assert "Re-enqueued 1 failed row(s)" in res.output, f"step 1: {res.output}"
        assert "Revived 1 dead task(s)" in res.output, f"step 1: {res.output}"

        # Step 2, tuples mode, no blank row: the doctor exits 0 after the verb and a
        # second run has nothing to do.
        with T2Database(_t2) as db:
            # step 1 re-enqueued its failed row as a pending table row; clear it so the
            # claim below is the failed row's own
            while (pending := db.aspect_queue.claim_next()) is not None:
                db.aspect_queue.mark_done(pending.collection, pending.source_path)
            self._failed_table_row(db, "knowledge__nx235rt2", "only.pdf", register_real_doc_id())
        monkeypatch.setenv("NX_ASPECT_QUEUE", "tuples")
        res = CliRunner().invoke(aspects_group, ["requeue-failed"])
        assert res.exit_code == 0, res.output
        _report_aspect_queue_service()  # exits 0: no raise
        again = CliRunner().invoke(aspects_group, ["requeue-failed"])
        assert again.exit_code == 0 and "no failed rows" in again.output, f"step 2: {again.output}"

        # Step 3, tuples mode with a blank-doc_id row. Nothing available first, so the
        # dead task made below is the one the claim returns.
        self._drain(store)
        monkeypatch.delenv("NX_ASPECT_QUEUE", raising=False)  # the table rows below are written in table mode
        with T2Database(_t2) as db:
            pending_before = db.aspect_queue.pending_count()
        good_doc = register_real_doc_id()
        other_doc = register_real_doc_id()
        coll = "knowledge__nx235rt"
        with T2Database(_t2) as db:
            self._failed_table_row(db, coll, "good.pdf", good_doc)
            self._failed_table_row(db, coll, "sibling.pdf", other_doc)
            # a PENDING row sharing good_doc's doc_id: deleting by doc_id would take it too
            db.aspect_queue.enqueue(coll, "pending.pdf", content="c", doc_id=good_doc)
        monkeypatch.setenv("NX_ASPECT_QUEUE", "tuples")
        # The engine refuses a table row with no doc_id, so the legacy blank row
        # is injected into the listing (the shape an old failed row would have).
        real_list_failed = HttpAspectQueue.list_failed
        blank = QueueRow(collection=coll, source_path="blank.pdf", content_hash="h",
                         content="", retry_count=0, doc_id="")

        def with_blank(self, collection=None):  # noqa: ANN001, ANN202
            return [*real_list_failed(self, collection), blank]

        monkeypatch.setattr(HttpAspectQueue, "list_failed", with_blank)
        dead_tid = self._make_dead(store, "docs__nx235rt-dead__bge-base-en-v15-768__v1")

        with pytest.raises(click.exceptions.Exit):
            _report_aspect_queue_service()  # step 3 before: red (failed rows exist)

        dry = CliRunner().invoke(aspects_group, ["requeue-failed", "--dry-run"])
        assert dry.exit_code == 0, dry.output
        with T2Database(_t2) as db:
            assert len(real_list_failed(db.aspect_queue, coll)) == 2, "step 3: dry-run wrote"
        assert [r.claim_state for r in store.rd("aspects", _KEYS, n=300)] == ["dead"], "step 3: dry-run wrote"
        # The preview says which row the tuples-mode run will skip, so it and
        # the run agree (critique r2): the blank row carries the note, the
        # others do not.
        assert "blank.pdf" in dry.output and "[would skip: no doc_id]" in dry.output, dry.output
        assert dry.output.count("[would skip: no doc_id]") == 1, dry.output

        res = CliRunner().invoke(aspects_group, ["requeue-failed"])
        assert res.exit_code == 0, res.output
        assert "blank.pdf" in res.output and "no doc_id" in res.output, "step 3: the blank row is reported"
        assert "Re-enqueued 2 failed row(s)" in res.output and "skipped 1" in res.output, res.output
        assert "Revived 1 dead task(s)" in res.output

        with T2Database(_t2) as db:
            left = [r.source_path for r in real_list_failed(db.aspect_queue, coll)]
        assert left == [], f"step 3: the two re-enqueued table rows are gone: {left}"
        with T2Database(_t2) as db:
            assert db.aspect_queue.pending_count() == pending_before + 1, (
                "step 3: the row is deleted by (collection, source_path): a pending sibling with the same "
                "doc_id stays"
            )
        tasks = store.rd("aspects", _KEYS, n=300)
        docs = {json.loads(t.body)["doc_id"]: t for t in tasks}
        assert good_doc in docs and other_doc in docs, "step 3: each re-enqueued row has its task"
        assert all(t.claim_state is None for t in tasks), "step 3: all available; the dead one is revived"
        assert dead_tid in {t.id for t in tasks}

        # A second run: only the blank row is listed, and it is reported, not retried.
        again = CliRunner().invoke(aspects_group, ["requeue-failed"])
        assert again.exit_code == 0, again.output
        assert "Re-enqueued 0 failed row(s)" in again.output and "skipped 1" in again.output, again.output


# ── stubs ────────────────────────────────────────────────────────────────────


class _StubTuples:
    """A tuple store with a read cap, a census and a recorder for revives."""

    def __init__(self, rows: list[TupleRow], cap: int | None = None,
                 unknown: bool = False) -> None:
        self.rows = rows
        self.cap = cap
        self.unknown = unknown
        self.out_calls: list[tuple] = []
        self.rd_calls = 0

    def subspace_stats(self, subspace: str, **_kw) -> SubspaceCensus:
        if self.unknown:
            raise UnknownSubspaceError("no template")
        return _census(
            dead=sum(1 for r in self.rows if r.claim_state == "dead"),
            available=sum(1 for r in self.rows if r.claim_state is None),
        )

    def rd(self, subspace, keys_pattern=None, *, n=1, since=None, **_kw):
        self.rd_calls += 1
        if self.unknown:
            raise UnknownSubspaceError("no template")
        n = min(n, self.cap) if self.cap else n
        ordered = sorted(self.rows, key=lambda r: (r.created_at, r.id))
        if since is not None:
            ordered = [r for r in ordered if (r.created_at, r.id) > tuple(since)]
        return ordered[:n]

    def out_replace(self, subspace, keys, dims, body, *, nonce, **_kw):
        self.out_calls.append((subspace, dict(keys), body, nonce))
        return "id", "replaced"


def _row(i: int, state: str | None, collection: str = "c1") -> TupleRow:
    doc = f"1.{i}.1"
    return TupleRow(
        id=f"{i:04x}", subspace="aspects", template="aspects", keys=dict(_KEYS), dims={},
        body=_body(collection, doc, f"{i}.pdf"), claim_state=state, claimant=None,
        lease_until=None, attempts=0, consumed_at=None, consumed_by=None,
        expires_at="2027-01-01T00:00:00Z", created_at=f"2026-10-29T00:00:{i:02d}Z",
        announced_at=None,
    )


class TestRequeueWithStubs:
    @pytest.fixture
    def run(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
        class _Failed:
            def __init__(self, rows):
                self._rows = rows

            def list_failed(self, collection=None):
                return [r for r in self._rows if not collection or r.collection == collection]

        class _Db:
            def __init__(self, tuples, failed):
                self.tuples = tuples
                self.aspect_queue = _Failed(failed)

            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

        def build(tuples: _StubTuples, failed=()):
            db = _Db(tuples, list(failed))
            monkeypatch.setattr("nexus.db.t2.T2Database", lambda *a, **k: db)
            monkeypatch.setattr(
                "nexus.commands._helpers.default_db_path", lambda: tmp_path / "t2.db",
            )
            import nexus.mcp_infra as infra

            monkeypatch.setattr(infra, "t2_index_write", lambda fn, **k: fn(db))
            return db

        return build

    def test_paging_past_a_read_cap_smaller_than_the_page(self, run, monkeypatch) -> None:
        """The engine clamps ``n``: a page shorter than requested is not the
        last page. Twelve rows, cap 3, every dead task still found."""
        rows = [_row(i, "dead" if i % 2 == 0 else None) for i in range(12)]
        stub = _StubTuples(rows, cap=3)
        run(stub)
        res = CliRunner().invoke(aspects_group, ["requeue-failed"])
        assert res.exit_code == 0, res.output
        assert "Revived 6 dead task(s)" in res.output
        assert len(stub.out_calls) == 6
        assert stub.rd_calls >= 4, "must have paged"

    def test_revive_is_out_replace_on_aspects_with_the_doc_id_nonce(self, run) -> None:
        row = _row(1, "dead")
        stub = _StubTuples([row])
        run(stub)
        res = CliRunner().invoke(aspects_group, ["requeue-failed"])
        assert res.exit_code == 0, res.output
        assert stub.out_calls == [("aspects", _KEYS, row.body, "1.1.1")]

    def test_dry_run_calls_neither_out_nor_the_write_path(self, run) -> None:
        stub = _StubTuples([_row(1, "dead"), _row(2, "dead")])
        run(stub)
        res = CliRunner().invoke(aspects_group, ["requeue-failed", "--dry-run"])
        assert res.exit_code == 0, res.output
        assert "Would re-enqueue 2 dead task(s)" in res.output
        assert stub.out_calls == []

    def test_no_dead_tasks_skips_the_scan(self, run) -> None:
        stub = _StubTuples([_row(1, None)])
        run(stub)
        res = CliRunner().invoke(aspects_group, ["requeue-failed"])
        assert "no failed rows" in res.output
        assert stub.rd_calls == 0, "the census says dead=0, so no rd scan"

    def test_a_table_only_engine_behaves_as_before(self, run) -> None:
        """No ``aspects`` template: the tuple side reads as nothing, no error."""
        from nexus.db.t2.records import QueueRow

        failed = [QueueRow(
            collection="knowledge__a", source_path="x.pdf", content_hash="", content="",
            retry_count=3, doc_id="1.1.1",
        )]
        stub = _StubTuples([], unknown=True)
        run(stub, failed)
        res = CliRunner().invoke(aspects_group, ["requeue-failed", "--dry-run"])
        assert res.exit_code == 0, res.output
        assert "Would re-enqueue 1 failed row(s)" in res.output
        assert "dead task" not in res.output

    def test_a_table_only_engine_with_nothing_failed(self, run) -> None:
        run(_StubTuples([], unknown=True))
        res = CliRunner().invoke(aspects_group, ["requeue-failed"])
        assert res.exit_code == 0, res.output
        assert "aspect_extraction_queue: no failed rows." in res.output

    def test_limit_takes_table_rows_first_then_dead_tasks(self, run) -> None:
        from nexus.db.t2.records import QueueRow

        failed = [QueueRow(
            collection="knowledge__a", source_path=f"f{i}.pdf", content_hash="", content="",
            retry_count=3, doc_id=f"1.{i}.1",
        ) for i in range(2)]
        stub = _StubTuples([_row(5, "dead"), _row(6, "dead")])
        db = run(stub, failed)
        enq: list[str] = []
        db.aspect_queue.enqueue = lambda c, s, **k: enq.append(s)
        res = CliRunner().invoke(aspects_group, ["requeue-failed", "--limit", "3"])
        assert res.exit_code == 0, res.output
        assert enq == ["f0.pdf", "f1.pdf"]
        assert len(stub.out_calls) == 1


# ── the depth hook ───────────────────────────────────────────────────────────


class _StubQueue:
    def __init__(self, *a, count: int = 0, exc: Exception | None = None, **k) -> None:
        self.count, self.exc = count, exc

    def pending_count(self, *, timeout=None) -> int:
        if self.exc:
            raise self.exc
        return self.count

    def close(self) -> None:
        pass


class TestQueueDepthSum:
    @pytest.fixture
    def wire(self, monkeypatch: pytest.MonkeyPatch):
        import nexus.db.t2.http_aspect_queue as aq
        import nexus.db.t2.http_tuple_store as ts

        def go(*, table: int = 0, table_exc=None, available: int = 0, tuple_exc=None):
            seen: dict = {}

            class Q(_StubQueue):
                def __init__(self, *a, **k):
                    super().__init__(count=table, exc=table_exc)

            class T:
                def __init__(self, *a, **k):
                    seen["ctor"] = k

                def subspace_stats(self, subspace, **kw):
                    seen["stats_kw"] = kw
                    if tuple_exc:
                        raise tuple_exc
                    return _census(available=available)

                def close(self):
                    pass

            monkeypatch.setattr(aq, "HttpAspectQueue", Q)
            monkeypatch.setattr(ts, "HttpTupleStore", T)
            return seen

        return go

    @pytest.mark.parametrize("table, avail, want", [(4, 0, 4), (0, 3, 3), (4, 3, 7), (0, 0, 0)])
    def test_the_depth_is_the_sum_of_both_sides(self, wire, table, avail, want) -> None:
        from nexus.aspect_worker import _best_effort_queue_depth

        wire(table=table, available=avail)
        assert _best_effort_queue_depth() == want

    def test_both_calls_keep_the_two_second_cap(self, wire) -> None:
        from nexus.aspect_worker import _best_effort_queue_depth

        seen = wire(table=1, available=1)
        _best_effort_queue_depth()
        assert seen["ctor"].get("timeout") == 2.0
        assert seen["stats_kw"].get("timeout") in (None, 2.0)

    def test_inside_an_index_run_the_tuple_store_shares_the_run_client_and_caps_the_call(
        self, wire,
    ) -> None:
        from nexus.aspect_worker import _best_effort_queue_depth
        from nexus.db.t2._refreshable_client import build_shared_t2_client
        from nexus.mcp_infra import use_shared_t2_client_for_index_run

        seen = wire(table=1, available=1)
        shared = build_shared_t2_client()
        try:
            with use_shared_t2_client_for_index_run(shared):
                assert _best_effort_queue_depth() == 2
        finally:
            shared.close()
        assert seen["ctor"].get("client") is shared
        assert "timeout" not in seen["ctor"], "client= and a constructor timeout= are rejected together"
        assert seen["stats_kw"] == {"timeout": 2.0}

    def test_a_table_only_engine_counts_the_tuple_side_as_zero(self, wire) -> None:
        from nexus.aspect_worker import _best_effort_queue_depth

        wire(table=5, tuple_exc=UnknownSubspaceError("no template"))
        assert _best_effort_queue_depth() == 5

    def test_the_tuple_side_failing_otherwise_is_unknown_not_a_partial_count(self, wire) -> None:
        from nexus.aspect_worker import _best_effort_queue_depth

        wire(table=5, tuple_exc=RuntimeError("503"))
        assert _best_effort_queue_depth() is None

    def test_the_table_side_failing_is_unknown_not_a_partial_count(self, wire) -> None:
        from nexus.aspect_worker import _best_effort_queue_depth

        wire(table_exc=RuntimeError("503"), available=9)
        assert _best_effort_queue_depth() is None
