# SPDX-License-Identifier: AGPL-3.0-or-later
"""RDR-235 (bead nexus-5r2sc.3.2): the aspect-queue producer behind
``NX_ASPECT_QUEUE=tuples|table``.

Tuples mode turns ``HttpAspectQueue.enqueue`` and ``enqueue_many`` into
``out mode=replace`` on the ``aspects`` template with an identity body
``{collection, source_path, doc_id, content_hash}`` and nonce = ``doc_id``.
Table mode (the default) sends nothing to ``/v1/tuples``.

Two layers (nexus-9edxe, tenant consolidation round 2). Everything the producer
decides on its own runs over ``_MockEngine``, an ``httpx.MockTransport`` handler
behind the real ``HttpTupleStore`` (so the payload, the size cap and the typed
error mapping are the real client code) and mints no tenant: table mode, the
bounded fan-out, the skipped rows, the batch that stops at the tenant ceiling and
the missing template. Two tests reach the real engine, because their subject is
what the engine and the catalog do with the producer's writes: the producer
journey (identity bodies, replace by ``doc_id``, a retired name, the census) and
the refusal rows that land in ``hook_failures``.

Where a mock case injects a failure (the tenant's 10,000-row ceiling, a missing
template) the engine answers the typed error: filling the template to its
ceiling would take 10,000 writes, and the engine's own 429 is pinned by its
Java tests.
"""
from __future__ import annotations

import json
import threading
import time
import uuid

import httpx
import pytest
import structlog

import nexus.corpus as corpus
from nexus.collection_errors import SupersededCollectionWriteError
from nexus.db.t2.http_aspect_queue import (
    ASPECT_QUEUE_ENV,
    ASPECTS_KEYS,
    ASPECTS_SUBSPACE,
    AspectQueueEngineUnsupportedError,
    AspectTupleCounts,
    HttpAspectQueue,
    aspect_queue_mode,
)
from nexus.db.t2.http_tuple_store import (
    HttpTupleStore,
    MaxLiveRowsExceededError,
    TooLargeError,
    UnknownSubspaceError,
)

_KEYS = {"queue": "aspects"}
_BODY_CAP = 4096

# The tenant is opt-in (tests/AGENTS.md): only the two engine tests ask for it.
# The flag parsing, the blank-doc_id refusal and every mock-engine case run
# before any engine request and mint none.
engine = pytest.mark.usefixtures("t2_service_env")


def _coll() -> str:
    return f"docs__nx235p-{uuid.uuid4().hex[:8]}__bge-base-en-v15-768__v1"


def _tumbler() -> str:
    return f"1.{uuid.uuid4().int % 10**6}.{uuid.uuid4().int % 10**6}"


@pytest.fixture(autouse=True)
def _fresh_state(monkeypatch: pytest.MonkeyPatch):
    corpus._REGISTERED_COLLECTIONS.clear()
    corpus._REGISTERED_COLLECTIONS_SCOPED.clear()
    monkeypatch.setenv("NX_ASPECT_WORKER_AUTOSTART", "0")
    monkeypatch.setattr("nexus.aspect_worker.signal_aspect_wake", lambda *a, **k: None)
    monkeypatch.delenv(ASPECT_QUEUE_ENV, raising=False)
    import nexus.db.t2.http_aspect_queue as mod

    mod._warned_unknown_modes.clear()
    yield
    corpus._REGISTERED_COLLECTIONS.clear()
    corpus._REGISTERED_COLLECTIONS_SCOPED.clear()


@pytest.fixture
def tuples_mode(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(ASPECT_QUEUE_ENV, "tuples")


@pytest.fixture
def queue():
    q = HttpAspectQueue()
    yield q
    q.close()


@pytest.fixture
def store() -> HttpTupleStore:
    return HttpTupleStore()


def _tasks(store: HttpTupleStore) -> list:
    return store.rd("aspects", _KEYS, n=300)


def _spy_out_replace(monkeypatch: pytest.MonkeyPatch, fail_after: int | None = None,
                     exc: Exception | None = None) -> list[tuple]:
    """Count ``out_replace`` calls; after *fail_after* real ones, raise *exc*."""
    calls: list[tuple] = []
    lock = threading.Lock()
    real = HttpTupleStore.out_replace

    def wrapper(self, *args, **kwargs):
        with lock:
            calls.append((args, kwargs))
            n = len(calls)
        if fail_after is not None and n > fail_after:
            raise exc
        return real(self, *args, **kwargs)

    monkeypatch.setattr(HttpTupleStore, "out_replace", wrapper)
    return calls


class _MockEngine:
    """The engine's ``/v1/tuples/out`` and the table queue's two enqueue routes,
    as an ``httpx.MockTransport`` handler.

    ``out`` models the ``aspects`` template: one task per nonce (``created`` the
    first time, ``replaced`` after). After *fail_after* accepted outs every
    further ``out`` is answered with the typed error ``fail`` = (status, code), the
    shape ``_raise_typed`` reads. *hold_s* keeps each ``out`` in the handler long
    enough for a fan-out to overlap, so ``peak`` is a real concurrency reading."""

    def __init__(self, *, fail_after: int | None = None, fail: tuple[int, str] = (429, "MaxLiveRowsExceeded"),
                 hold_s: float = 0.0) -> None:
        self.tasks: dict[str, dict] = {}
        self.outs: list[dict] = []
        self.paths: list[str] = []
        self.attempts = 0
        self.live = 0
        self.peak = 0
        self._fail_after, self._fail, self._hold_s = fail_after, fail, hold_s
        self._lock = threading.Lock()

    def __call__(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        payload = json.loads(request.content) if request.content else {}
        with self._lock:
            self.paths.append(path)
        if path == "/v1/aspects/queue/enqueue_many":
            return httpx.Response(200, json={"enqueued": len(payload["rows"])})
        if path != "/v1/tuples/out":
            return httpx.Response(200, json={})
        with self._lock:
            self.attempts += 1
            self.live += 1
            self.peak = max(self.peak, self.live)
        try:
            if self._hold_s:
                time.sleep(self._hold_s)
            with self._lock:
                if self._fail_after is not None and len(self.outs) >= self._fail_after:
                    status, code = self._fail
                    return httpx.Response(status, json={"error": code, "detail": f"{code} (mock engine)"})
                nonce = payload["nonce"]
                outcome = "replaced" if nonce in self.tasks else "created"
                self.tasks[nonce] = payload
                self.outs.append(payload)
                return httpx.Response(200, json={"id": f"task-{nonce}", "outcome": outcome})
        finally:
            with self._lock:
                self.live -= 1

    def queue(self) -> HttpAspectQueue:
        return HttpAspectQueue(
            base_url="http://mock.test", _token="test-token",
            client=httpx.Client(transport=httpx.MockTransport(self)),
        )

    def tuple_posts(self) -> list[str]:
        return [p for p in self.paths if p.startswith("/v1/tuples")]


@pytest.fixture
def no_registration(monkeypatch: pytest.MonkeyPatch) -> None:
    """The collection registration that precedes a write reads the real catalog,
    which would need an engine and a tenant; the mock-engine cases pin what the
    producer sends, not registration. The engine journey keeps the real one."""
    monkeypatch.setattr("nexus.corpus.ensure_collection_registered", lambda *a, **k: None)
    monkeypatch.setattr("nexus.corpus.write_with_registration_retry", lambda coll, fn, **k: fn())


class TestFlag:
    def test_default_is_tuples(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Unset is the tuple producer (RDR-235 Activation Step 1, nexus-lnwn1);
        ``table`` is the explicit opt-out. Set explicitly to unset, so a shell that
        exports the variable cannot make the test read its own environment."""
        monkeypatch.delenv(ASPECT_QUEUE_ENV, raising=False)
        assert aspect_queue_mode() == "tuples"

    @pytest.mark.parametrize("value, expected", [
        ("tuples", "tuples"), ("TUPLES", "tuples"), (" table ", "table"), ("TABLE", "table"),
        ("", "tuples"), ("   ", "tuples"),
    ])
    def test_values(self, monkeypatch: pytest.MonkeyPatch, value: str, expected: str) -> None:
        monkeypatch.setenv(ASPECT_QUEUE_ENV, value)
        assert aspect_queue_mode() == expected

    def test_an_unknown_value_warns_once_and_means_table(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """A typo falls back to the queue every released engine supports, and
        says so once per process; it does not fail every ingest hook. That the
        enqueue then sends nothing to the tuple space is pinned with the table
        mode test below, which runs under this same value."""
        monkeypatch.setenv(ASPECT_QUEUE_ENV, "tuple")
        with structlog.testing.capture_logs() as cap:
            assert aspect_queue_mode() == "table"
            assert aspect_queue_mode() == "table"
        warnings = [e for e in cap if e["event"] == "aspect_queue_mode_unknown_falling_back_to_table"]
        assert len(warnings) == 1 and warnings[0]["value"] == "tuple"


@pytest.mark.usefixtures("no_registration")
class TestTableModeSendsNothingToTheTupleSpace:
    def test_enqueue_and_enqueue_many_leave_the_tuple_space_alone(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Under the explicit opt-out ("table") and under an unknown value (a typo
        means table), nothing goes to ``/v1/tuples``, and the table queue's own
        routes carry the write."""
        eng = _MockEngine()
        queue = eng.queue()
        coll = _coll()
        doc = _tumbler()
        for flag in ("table", "tuple"):
            monkeypatch.setenv(ASPECT_QUEUE_ENV, flag)
            queue.enqueue(coll, "/p/a.md", content="text", doc_id=doc)
            assert queue.enqueue_many([
                {"collection": coll, "source_path": f"/p/{i}.md", "doc_id": doc} for i in range(3)
            ]) == 3
            assert eng.tuple_posts() == [], f"flag={flag!r}"
        assert eng.paths.count("/v1/aspects/queue/enqueue") == 2, "the table queue took the single writes"
        assert eng.paths.count("/v1/aspects/queue/enqueue_many") == 2, "and the batches"


@pytest.mark.usefixtures("tuples_mode")
class TestBlankDocIdIsRefused:
    def test_a_blank_doc_id_is_refused_before_anything_is_sent(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Refused before the registration check and the send: no engine, no tenant."""
        calls = _spy_out_replace(monkeypatch)
        q = HttpAspectQueue(base_url="http://mock.test", _token="test-token")
        with pytest.raises(ValueError, match="doc_id"):
            q.enqueue(_coll(), "/p/a.md", doc_id="")
        assert calls == []


@pytest.mark.usefixtures("tuples_mode", "no_registration")
class TestBatchOverAMockEngine:
    """The batch form's own decisions: what it sends, how wide, what it skips,
    and where it stops. Mock engine, no tenant (nexus-9edxe)."""

    def test_a_batch_lands_every_row_signals_the_wake_once_and_fans_out_bounded(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        import nexus.db.t2.http_aspect_queue as mod

        wakes: list[None] = []
        monkeypatch.setattr("nexus.aspect_worker.signal_aspect_wake", lambda *a, **k: wakes.append(None))
        eng = _MockEngine(hold_s=0.01)
        coll = _coll()
        rows = [{"collection": coll, "source_path": f"/p/{i}.md", "doc_id": _tumbler()} for i in range(48)]
        assert eng.queue().enqueue_many(rows) == 48
        assert len(eng.tasks) == 48, "every row landed"
        assert len(wakes) == 1, "one wake for the batch"
        assert 1 <= eng.peak <= mod.ENQUEUE_FANOUT_WORKERS, f"fan-out peak {eng.peak}"
        # The wire contract of every out: a replace on the aspects template whose
        # nonce is the document (a nonce built from the path would leave two tasks
        # per renamed document), the identity body and nothing else.
        for row, out in zip(sorted(rows, key=lambda r: r["doc_id"]), sorted(eng.outs, key=lambda o: o["nonce"])):
            assert out["subspace"] == ASPECTS_SUBSPACE and out["keys"] == dict(ASPECTS_KEYS), "the template"
            assert out["mode"] == "replace" and out["nonce"] == row["doc_id"], "replace by doc_id"
            assert set(json.loads(out["body"])) == {"collection", "source_path", "doc_id", "content_hash"}

    def test_bad_rows_are_skipped_and_not_counted_and_a_single_over_long_path_raises(self) -> None:
        eng = _MockEngine()
        queue = eng.queue()
        coll, bad_doc = _coll(), _tumbler()
        rows = [
            {"collection": coll, "source_path": "/p/ok.md", "doc_id": _tumbler()},
            {"collection": "", "source_path": "/p/x.md", "doc_id": _tumbler()},
            {"collection": coll, "source_path": "/p/nodoc.md"},
            {"collection": coll, "source_path": "/p/" + "x" * 5000, "doc_id": bad_doc},
            {"collection": coll, "source_path": "/p/b.md", "doc_id": _tumbler()},
        ]
        with structlog.testing.capture_logs() as cap:
            assert queue.enqueue_many(rows) == 2
        assert len(eng.tasks) == 2
        skipped = [e for e in cap if e["event"] == "aspect_enqueue_many_rows_skipped_malformed"]
        assert skipped and skipped[0]["skipped"] == 2, "malformed rows are counted in one warning"
        refusals = [e for e in cap if e["event"] == "aspect_enqueue_row_skipped_too_large"]
        assert [e["doc_id"] for e in refusals] == [bad_doc], "the over-long row names its doc_id"
        # The single form raises where the batch skips.
        with pytest.raises(TooLargeError):
            queue.enqueue(_coll(), "/p/" + "x" * 5000, doc_id=_tumbler())
        assert len(eng.tasks) == 2 and eng.attempts == 2, "the refused single sent nothing"

    def test_a_mid_batch_ceiling_stops_the_batch_and_raises(self) -> None:
        """The tenant reaches ``max_live_rows`` part way through a batch (the
        engine answers 429 ``MaxLiveRowsExceeded`` from the seventh ``out``). That
        is not one bad row: the typed error reaches the caller, an error log names
        the counts, the remaining rows are not sent, and nothing is replayed
        through the per-row table path."""
        eng = _MockEngine(fail_after=6)
        queue = eng.queue()
        coll = _coll()
        rows = [{"collection": coll, "source_path": f"/p/{i}.md", "doc_id": _tumbler()} for i in range(60)]
        with structlog.testing.capture_logs() as cap:
            with pytest.raises(MaxLiveRowsExceededError):
                queue.enqueue_many(rows)
        aborted = [e for e in cap if e["event"] == "aspect_enqueue_many_aborted"]
        assert len(aborted) == 1
        assert aborted[0]["log_level"] == "error"
        assert aborted[0]["collections"] == [coll]
        assert aborted[0]["error_type"] == "MaxLiveRowsExceededError"
        assert aborted[0]["enqueued"] == 6
        assert aborted[0]["unsent"] > 0 and eng.attempts < len(rows), "the batch stopped sending"
        assert not [p for p in eng.paths if p.startswith("/v1/aspects/queue")], (
            "never replayed per row through the table"
        )
        assert len(eng.tasks) == 6, "rows that landed stay"
        # The single form raises it too (the engine is still at its ceiling).
        with pytest.raises(MaxLiveRowsExceededError):
            queue.enqueue(coll, "/p/single.md", doc_id=_tumbler())
        assert len(eng.tasks) == 6

    def test_an_engine_without_the_template_is_refused_naming_the_release(self) -> None:
        eng = _MockEngine(fail_after=0, fail=(404, "UnknownSubspace"))
        queue = eng.queue()
        with pytest.raises(AspectQueueEngineUnsupportedError, match="engine-service-v0.1.169") as one:
            queue.enqueue(_coll(), "/p/a.md", doc_id=_tumbler())
        assert ASPECT_QUEUE_ENV in str(one.value)
        assert isinstance(one.value.__cause__, UnknownSubspaceError)
        with pytest.raises(AspectQueueEngineUnsupportedError):
            queue.enqueue_many([
                {"collection": _coll(), "source_path": "/p/a.md", "doc_id": _tumbler()},
                {"collection": _coll(), "source_path": "/p/b.md", "doc_id": _tumbler()},
            ])


@engine
@pytest.mark.usefixtures("tuples_mode")
class TestTheProducerOnTheRealEngine:
    def _retire(self, old: str, new: str) -> None:
        from nexus.catalog.factory import make_catalog_writer

        w = make_catalog_writer()
        for name in (old, new):
            kw = corpus.collection_registration_kwargs(name)
            w.register_collection(name, **kw)
        w.supersede_collection(old, new)
        corpus._REGISTERED_COLLECTIONS.clear()
        corpus._REGISTERED_COLLECTIONS_SCOPED.clear()

    def test_the_producer_journey_identity_replace_a_retired_name_and_the_census(
        self, monkeypatch: pytest.MonkeyPatch, queue: HttpAspectQueue, store: HttpTupleStore,
    ) -> None:
        """One engine state, six steps, each assertion naming its branch (nexus-9edxe
        folded the identity, replace, retired-name and census engine tests of
        nexus-5r2sc.3.2 and .3.5 into this journey; an early failure hides the
        later steps, and the message says which step it was)."""
        # 1. The census control: an empty queue is zero on every side, from the real engine.
        assert queue.tuple_counts() == AspectTupleCounts(0, 0, 0), "step 1: an empty queue"

        # 2. A CLI row, then an MCP note. The MCP path passes the whole note as
        # ``content``: sent, 200 KB would be over the cap; dropped, the task is the
        # same few hundred bytes as a CLI row, and the worker reassembles the text
        # (RDR-235 E2). A non-ASCII path rides in the CLI row: it must come back as written.
        coll, doc, note_doc = _coll(), _tumbler(), _tumbler()
        path = "/repo/docs/été-ノート.md"
        queue.enqueue(coll, path, content_hash="h1", content="", doc_id=doc)
        queue.enqueue(coll, "note-title", content_hash="h2", content="word " * 40_000, doc_id=note_doc)
        by_doc = {json.loads(t.body)["doc_id"]: t for t in _tasks(store)}
        assert set(by_doc) == {doc, note_doc}, "step 2: one task per document"
        task = by_doc[doc]
        assert task.keys == _KEYS
        assert json.loads(task.body) == {
            "collection": coll, "source_path": path,
            "doc_id": doc, "content_hash": "h1",
        }, "step 2: the CLI row"
        assert len(task.body.encode()) < _BODY_CAP
        note = by_doc[note_doc]
        assert set(json.loads(note.body)) == {"collection", "source_path", "doc_id", "content_hash"}, (
            "step 2: the MCP note"
        )
        assert len(note.body.encode()) < 600
        assert "word word" not in note.body

        # 3. nonce = doc_id: the second enqueue names a RENAMED path, so a nonce
        # built from the path would leave two tasks (and two ``created``).
        outcomes: list[str] = []
        real = HttpAspectQueue._out_task

        def spy(self, *a):
            outcome = real(self, *a)
            outcomes.append(outcome)
            return outcome

        monkeypatch.setattr(HttpAspectQueue, "_out_task", spy)
        renamed = _tumbler()
        queue.enqueue(coll, "/p/before.md", content_hash="old", doc_id=renamed)
        queue.enqueue(coll, "/p/after.md", content_hash="new", doc_id=renamed)
        assert outcomes == ["created", "replaced"], "step 3: the document, not its path, is the identity"
        (replaced,) = [t for t in _tasks(store) if json.loads(t.body)["doc_id"] == renamed]
        body = json.loads(replaced.body)
        assert body["content_hash"] == "new", "step 3: the replace carries the new body"
        assert body["source_path"] == "/p/after.md", "step 3: the old path does not linger"

        # 4. enqueue_many replaces too (its own path: a bounded fan-out of replaces).
        many = _coll()
        rows = [
            {"collection": many, "source_path": f"/p/{i}.md", "doc_id": _tumbler(), "content_hash": "a"}
            for i in range(5)
        ]
        assert queue.enqueue_many(rows) == 5
        assert queue.enqueue_many([{**r, "content_hash": "b"} for r in rows]) == 5
        tasks = [t for t in _tasks(store) if json.loads(t.body)["collection"] == many]
        assert len(tasks) == 5, "step 4: enqueue_many: one task per document"
        assert {json.loads(t.body)["content_hash"] for t in tasks} == {"b"}, "step 4: enqueue_many: the new body"

        # 5. A retired name is refused before any out (the real catalog's verdict).
        landed = len(_tasks(store))
        assert landed == 2 + 1 + 5
        owner = uuid.uuid4().hex[:8]
        old = f"docs__nx235r-{owner}__bge-base-en-v15-768__v1"
        new = f"docs__nx235r-{owner}__bge-base-en-v15-768__v2"
        self._retire(old, new)
        calls = _spy_out_replace(monkeypatch)
        with pytest.raises(SupersededCollectionWriteError) as one:
            queue.enqueue(old, "/p/a.md", doc_id=_tumbler())
        assert one.value.successor == new
        corpus._REGISTERED_COLLECTIONS.clear()
        corpus._REGISTERED_COLLECTIONS_SCOPED.clear()
        live = {"collection": new, "source_path": "/p/live.md", "doc_id": _tumbler()}
        with pytest.raises(SupersededCollectionWriteError):
            queue.enqueue_many([
                live, {"collection": old, "source_path": "/p/b.md", "doc_id": _tumbler()},
            ])
        assert calls == [], "step 5: a retired name in the batch sends the whole batch nowhere"
        assert len(_tasks(store)) == landed, "step 5: and nothing landed"

        # 6. The census the doctor and the console read: one task in flight, one
        # dead-lettered, the rest available.
        (_row, in_flight) = store.inp(ASPECTS_SUBSPACE, dict(ASPECTS_KEYS), claimant="c1", lease_s=600)
        (_row, doomed) = store.inp(ASPECTS_SUBSPACE, dict(ASPECTS_KEYS), claimant="c2", lease_s=600)
        store.nack(doomed, "c2", dead=True)
        counts = queue.tuple_counts()
        assert counts == AspectTupleCounts(available=landed - 2, claimed=1, dead=1), (
            f"step 6: claimed is in flight, not pending; dead is failed: {counts}, claim {in_flight}"
        )


@engine
@pytest.mark.usefixtures("tuples_mode")
class TestRefusalsAreCountedRows:
    def test_the_hook_and_the_batch_flush_each_leave_a_hook_failures_row(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Two writers of ``hook_failures`` rows for ``aspect_extraction_enqueue_hook``,
        counted one after the other on one tenant: the enqueue hook (best-effort,
        but its swallowed refusal is a warning naming the document and the error
        class, plus a row) and the indexer's flush hook (it swallows a failed
        batch; in tuples mode it hands the failure to ``record_batch_enqueue_failure``
        so a ``MaxLiveRowsExceeded`` is a counted row, not only a log line)."""
        import nexus.aspect_worker as mod
        from nexus.aspect_worker import aspect_extraction_enqueue_hook, record_batch_enqueue_failure
        from nexus.mcp_infra import t2_index_write

        def failures() -> dict:
            return t2_index_write(
                lambda t2: t2.telemetry.list_hook_failures(hook_names=["aspect_extraction_enqueue_hook"])
            )

        monkeypatch.setattr(mod, "_resolve_catalog_reader", lambda: None)
        monkeypatch.setattr(mod, "_ensure_aspect_worker", lambda: None)
        _spy_out_replace(monkeypatch, fail_after=0, exc=MaxLiveRowsExceededError("full"))
        hook_coll = f"knowledge__nx235h-{uuid.uuid4().hex[:8]}__bge-base-en-v15-768__v1"
        doc = _tumbler()
        with structlog.testing.capture_logs() as cap:
            aspect_extraction_enqueue_hook("note-title", hook_coll, "text", doc_id=doc)
        warned = [e for e in cap if e["event"] == "aspect_extraction_enqueue_failed"]
        assert len(warned) == 1, "hook: one warning"
        assert warned[0]["log_level"] == "warning"
        assert (warned[0]["doc_id"], warned[0]["error_type"]) == (doc, "MaxLiveRowsExceededError")
        after_hook = failures()
        assert after_hook["total"] == 1 and hook_coll in str(after_hook["rows"]), f"hook: its row: {after_hook}"

        flush_coll = _coll()
        record_batch_enqueue_failure(flush_coll, 17, MaxLiveRowsExceededError("tenant at max_live_rows"))
        after_flush = failures()
        assert after_flush["total"] == 2, f"flush: a second row beside the hook's: {after_flush}"
        text = str(after_flush["rows"])
        assert flush_coll in text and "MaxLiveRowsExceededError" in text and "17" in text, "flush: its row"
