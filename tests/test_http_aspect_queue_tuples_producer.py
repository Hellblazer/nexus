# SPDX-License-Identifier: AGPL-3.0-or-later
"""RDR-235 (bead nexus-5r2sc.3.2): the aspect-queue producer behind
``NX_ASPECT_QUEUE=tuples|table``, against the real engine substrate.

Tuples mode turns ``HttpAspectQueue.enqueue`` and ``enqueue_many`` into
``out mode=replace`` on the ``aspects`` template with an identity body
``{collection, source_path, doc_id, content_hash}`` and nonce = ``doc_id``.
Table mode (the default) sends nothing to ``/v1/tuples``.

Where a test injects a failure (the tenant's 10,000-row ceiling, a missing
template) it wraps the one ``out_replace`` seam and lets every other call reach
the real engine: filling the template to its ceiling would take 10,000 writes,
and the engine's own 429 is pinned by its Java tests.
"""
from __future__ import annotations

import json
import threading
import uuid

import pytest
import structlog

import nexus.corpus as corpus
from nexus.collection_errors import SupersededCollectionWriteError
from nexus.db.t2.http_aspect_queue import (
    ASPECT_QUEUE_ENV,
    AspectQueueEngineUnsupportedError,
    HttpAspectQueue,
    aspect_queue_mode,
)
from tests._catalog_fixture_ops import register_real_doc_id
from nexus.db.t2.http_tuple_store import (
    HttpTupleStore,
    MaxLiveRowsExceededError,
    TooLargeError,
    UnknownSubspaceError,
)

_KEYS = {"queue": "aspects"}
_BODY_CAP = 4096

pytestmark = pytest.mark.usefixtures("t2_service_env")


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


class TestFlag:
    def test_default_is_table(self) -> None:
        assert aspect_queue_mode() == "table"

    @pytest.mark.parametrize("value, expected", [
        ("tuples", "tuples"), ("TUPLES", "tuples"), (" table ", "table"), ("", "table"),
    ])
    def test_values(self, monkeypatch: pytest.MonkeyPatch, value: str, expected: str) -> None:
        monkeypatch.setenv(ASPECT_QUEUE_ENV, value)
        assert aspect_queue_mode() == expected

    def test_an_unknown_value_warns_once_and_means_table(
        self, monkeypatch: pytest.MonkeyPatch, queue: HttpAspectQueue,
    ) -> None:
        """A typo falls back to the queue every released engine supports, and
        says so once per process; it does not fail every ingest hook."""
        monkeypatch.setenv(ASPECT_QUEUE_ENV, "tuple")
        tuple_posts: list[str] = []
        monkeypatch.setattr(
            HttpTupleStore, "_post",
            lambda self, path, payload, **kw: tuple_posts.append(path),
        )
        with structlog.testing.capture_logs() as cap:
            assert aspect_queue_mode() == "table"
            assert aspect_queue_mode() == "table"
        warnings = [e for e in cap if e["event"] == "aspect_queue_mode_unknown_falling_back_to_table"]
        assert len(warnings) == 1 and warnings[0]["value"] == "tuple"
        queue.enqueue(_coll(), "/p/a.md", doc_id=register_real_doc_id())
        assert tuple_posts == []


class TestTableModeSendsNothingToTheTupleSpace:
    def test_enqueue_and_enqueue_many_leave_the_tuple_space_alone(
        self, monkeypatch: pytest.MonkeyPatch, queue: HttpAspectQueue, store: HttpTupleStore,
    ) -> None:
        tuple_posts: list[str] = []
        monkeypatch.setattr(
            HttpTupleStore, "_post",
            lambda self, path, payload, **kw: tuple_posts.append(path),
        )
        coll = _coll()
        doc = register_real_doc_id()
        queue.enqueue(coll, "/p/a.md", content="text", doc_id=doc)
        assert queue.enqueue_many([
            {"collection": coll, "source_path": f"/p/{i}.md", "doc_id": doc} for i in range(3)
        ]) == 3
        assert tuple_posts == []
        assert queue.pending_count() >= 1
        monkeypatch.undo()
        assert _tasks(store) == []


@pytest.mark.usefixtures("tuples_mode")
class TestIdentityBody:
    def test_a_cli_row_is_an_identity_body_with_no_content(
        self, queue: HttpAspectQueue, store: HttpTupleStore,
    ) -> None:
        coll, doc = _coll(), _tumbler()
        queue.enqueue(coll, "/repo/docs/a.md", content_hash="h1", content="", doc_id=doc)
        (task,) = _tasks(store)
        assert task.keys == _KEYS
        body = json.loads(task.body)
        assert body == {
            "collection": coll, "source_path": "/repo/docs/a.md",
            "doc_id": doc, "content_hash": "h1",
        }
        assert len(task.body.encode()) < _BODY_CAP

    def test_an_mcp_note_never_carries_its_text(
        self, queue: HttpAspectQueue, store: HttpTupleStore,
    ) -> None:
        """The MCP path passes the whole note as ``content``. Sent, 200 KB would
        be over the cap; dropped, the task is the same few hundred bytes as a
        CLI row, and the worker reassembles the text (RDR-235 E2)."""
        coll, doc = _coll(), _tumbler()
        queue.enqueue(coll, "note-title", content_hash="h2", content="word " * 40_000, doc_id=doc)
        (task,) = _tasks(store)
        body = json.loads(task.body)
        assert set(body) == {"collection", "source_path", "doc_id", "content_hash"}
        assert len(task.body.encode()) < 600
        assert "word word" not in task.body

    def test_a_non_ascii_path_round_trips_unescaped(
        self, queue: HttpAspectQueue, store: HttpTupleStore,
    ) -> None:
        coll, doc = _coll(), _tumbler()
        queue.enqueue(coll, "/repo/docs/été-ノート.md", doc_id=doc)
        (task,) = _tasks(store)
        assert json.loads(task.body)["source_path"] == "/repo/docs/été-ノート.md"

    def test_a_blank_doc_id_is_refused(self, queue: HttpAspectQueue, store: HttpTupleStore) -> None:
        with pytest.raises(ValueError, match="doc_id"):
            queue.enqueue(_coll(), "/p/a.md", doc_id="")
        assert _tasks(store) == []


@pytest.mark.usefixtures("tuples_mode")
class TestReEnqueueReplaces:
    def test_a_second_enqueue_of_one_document_is_one_task_with_the_new_body(
        self, queue: HttpAspectQueue, store: HttpTupleStore, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        coll, doc = _coll(), _tumbler()
        outcomes: list[str] = []
        real = HttpAspectQueue._out_task

        def spy(self, *a):
            outcome = real(self, *a)
            outcomes.append(outcome)
            return outcome

        monkeypatch.setattr(HttpAspectQueue, "_out_task", spy)
        queue.enqueue(coll, "/p/a.md", content_hash="old", doc_id=doc)
        queue.enqueue(coll, "/p/a.md", content_hash="new", doc_id=doc)
        assert outcomes == ["created", "replaced"]
        (task,) = _tasks(store)
        assert json.loads(task.body)["content_hash"] == "new"

    def test_the_document_is_the_identity_not_its_path(
        self, queue: HttpAspectQueue, store: HttpTupleStore,
    ) -> None:
        """nonce = doc_id: a file renamed between two index runs is the same
        document, so its task is replaced and the old path does not linger."""
        coll, doc = _coll(), _tumbler()
        queue.enqueue(coll, "/p/before.md", doc_id=doc)
        queue.enqueue(coll, "/p/after.md", doc_id=doc)
        (task,) = _tasks(store)
        assert json.loads(task.body)["source_path"] == "/p/after.md"

    def test_enqueue_many_replaces_too(self, queue: HttpAspectQueue, store: HttpTupleStore) -> None:
        coll = _coll()
        rows = [
            {"collection": coll, "source_path": f"/p/{i}.md", "doc_id": _tumbler(), "content_hash": "a"}
            for i in range(5)
        ]
        assert queue.enqueue_many(rows) == 5
        assert queue.enqueue_many([{**r, "content_hash": "b"} for r in rows]) == 5
        tasks = _tasks(store)
        assert len(tasks) == 5
        assert {json.loads(t.body)["content_hash"] for t in tasks} == {"b"}


@pytest.mark.usefixtures("tuples_mode")
class TestEnqueueManyFanOut:
    def test_a_batch_lands_every_row_and_signals_the_wake_once(
        self, monkeypatch: pytest.MonkeyPatch, queue: HttpAspectQueue, store: HttpTupleStore,
    ) -> None:
        wakes: list[None] = []
        monkeypatch.setattr("nexus.aspect_worker.signal_aspect_wake", lambda *a, **k: wakes.append(None))
        coll = _coll()
        rows = [{"collection": coll, "source_path": f"/p/{i}.md", "doc_id": _tumbler()} for i in range(40)]
        assert queue.enqueue_many(rows) == 40
        assert len(_tasks(store)) == 40
        assert len(wakes) == 1

    def test_the_fan_out_is_bounded(self, monkeypatch: pytest.MonkeyPatch, queue: HttpAspectQueue) -> None:
        import nexus.db.t2.http_aspect_queue as mod

        live = peak = 0
        lock = threading.Lock()
        real = HttpTupleStore.out_replace

        def wrapper(self, *a, **k):
            nonlocal live, peak
            with lock:
                live += 1
                peak = max(peak, live)
            try:
                return real(self, *a, **k)
            finally:
                with lock:
                    live -= 1

        monkeypatch.setattr(HttpTupleStore, "out_replace", wrapper)
        coll = _coll()
        queue.enqueue_many([{"collection": coll, "source_path": f"/p/{i}.md", "doc_id": _tumbler()} for i in range(48)])
        assert 1 <= peak <= mod.ENQUEUE_FANOUT_WORKERS

    def test_malformed_rows_are_skipped_and_not_counted(
        self, queue: HttpAspectQueue, store: HttpTupleStore,
    ) -> None:
        coll = _coll()
        with structlog.testing.capture_logs() as cap:
            n = queue.enqueue_many([
                {"collection": coll, "source_path": "/p/ok.md", "doc_id": _tumbler()},
                {"collection": "", "source_path": "/p/x.md", "doc_id": _tumbler()},
                {"collection": coll, "source_path": "/p/nodoc.md"},
            ])
        assert n == 1 and len(_tasks(store)) == 1
        skipped = [e for e in cap if e["event"] == "aspect_enqueue_many_rows_skipped_malformed"]
        assert skipped and skipped[0]["skipped"] == 2

    def test_an_over_long_path_skips_that_row_and_names_its_doc_id(
        self, queue: HttpAspectQueue, store: HttpTupleStore,
    ) -> None:
        coll, bad_doc = _coll(), _tumbler()
        rows = [
            {"collection": coll, "source_path": "/p/a.md", "doc_id": _tumbler()},
            {"collection": coll, "source_path": "/p/" + "x" * 5000, "doc_id": bad_doc},
            {"collection": coll, "source_path": "/p/b.md", "doc_id": _tumbler()},
        ]
        with structlog.testing.capture_logs() as cap:
            assert queue.enqueue_many(rows) == 2
        assert len(_tasks(store)) == 2
        refusals = [e for e in cap if e["event"] == "aspect_enqueue_row_skipped_too_large"]
        assert [e["doc_id"] for e in refusals] == [bad_doc]

    def test_a_single_over_long_path_raises(self, queue: HttpAspectQueue, store: HttpTupleStore) -> None:
        with pytest.raises(TooLargeError):
            queue.enqueue(_coll(), "/p/" + "x" * 5000, doc_id=_tumbler())
        assert _tasks(store) == []


@pytest.mark.usefixtures("tuples_mode")
class TestMaxLiveRowsExceededIsLoud:
    def test_a_mid_batch_ceiling_stops_the_batch_and_raises(
        self, monkeypatch: pytest.MonkeyPatch, queue: HttpAspectQueue, store: HttpTupleStore,
    ) -> None:
        """The tenant reaches ``max_live_rows`` part way through a batch. That is
        not one bad row: the typed error reaches the caller, an error log names
        the counts, the remaining rows are not sent, and nothing is replayed
        through the per-row table path."""
        coll = _coll()
        table_posts: list[str] = []
        real_post = HttpAspectQueue._post

        def post_spy(self, path, body, **kw):
            table_posts.append(path)
            return real_post(self, path, body, **kw)

        monkeypatch.setattr(HttpAspectQueue, "_post", post_spy)
        calls = _spy_out_replace(
            monkeypatch, fail_after=6, exc=MaxLiveRowsExceededError("tenant at max_live_rows"),
        )
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
        assert aborted[0]["unsent"] > 0 and len(calls) < len(rows), "the batch stopped sending"
        assert table_posts == [], "never replayed per row through the table"
        assert len(_tasks(store)) == 6, "rows that landed stay"

    def test_a_single_enqueue_raises_it_too(self, monkeypatch: pytest.MonkeyPatch, queue: HttpAspectQueue) -> None:
        _spy_out_replace(monkeypatch, fail_after=0, exc=MaxLiveRowsExceededError("full"))
        with pytest.raises(MaxLiveRowsExceededError):
            queue.enqueue(_coll(), "/p/a.md", doc_id=_tumbler())

    def test_the_hook_logs_the_refusal_with_its_doc_id_and_counts_it(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """The enqueue hook stays best-effort, but its swallowed refusal is a
        warning that names the document and the error class, and a
        ``hook_failures`` row is the persistent count."""
        import nexus.aspect_worker as mod
        from nexus.aspect_worker import aspect_extraction_enqueue_hook
        from nexus.mcp_infra import t2_index_write

        monkeypatch.setattr(mod, "_resolve_catalog_reader", lambda: None)
        monkeypatch.setattr(mod, "_ensure_aspect_worker", lambda: None)
        _spy_out_replace(monkeypatch, fail_after=0, exc=MaxLiveRowsExceededError("full"))
        coll = f"knowledge__nx235h-{uuid.uuid4().hex[:8]}__bge-base-en-v15-768__v1"
        doc = _tumbler()
        with structlog.testing.capture_logs() as cap:
            aspect_extraction_enqueue_hook("note-title", coll, "text", doc_id=doc)
        warned = [e for e in cap if e["event"] == "aspect_extraction_enqueue_failed"]
        assert len(warned) == 1
        assert warned[0]["log_level"] == "warning"
        assert (warned[0]["doc_id"], warned[0]["error_type"]) == (doc, "MaxLiveRowsExceededError")
        failures = t2_index_write(
            lambda t2: t2.telemetry.list_hook_failures(hook_names=["aspect_extraction_enqueue_hook"])
        )
        assert failures["total"] == 1 and coll in str(failures["rows"]), failures


@pytest.mark.usefixtures("tuples_mode")
class TestRetiredNameIsRefusedBeforeAnyOut:
    def _retire(self, old: str, new: str) -> None:
        from nexus.catalog.factory import make_catalog_writer

        w = make_catalog_writer()
        for name in (old, new):
            kw = corpus.collection_registration_kwargs(name)
            w.register_collection(name, **kw)
        w.supersede_collection(old, new)
        corpus._REGISTERED_COLLECTIONS.clear()
        corpus._REGISTERED_COLLECTIONS_SCOPED.clear()

    def test_enqueue_and_enqueue_many_refuse_a_retired_name_with_no_out(
        self, monkeypatch: pytest.MonkeyPatch, queue: HttpAspectQueue, store: HttpTupleStore,
    ) -> None:
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
        assert calls == [], "a retired name in the batch sends the whole batch nowhere"
        assert _tasks(store) == []


@pytest.mark.usefixtures("tuples_mode")
class TestCapabilityCheck:
    def test_an_engine_without_the_template_is_refused_naming_the_release(
        self, monkeypatch: pytest.MonkeyPatch, queue: HttpAspectQueue,
    ) -> None:
        _spy_out_replace(monkeypatch, fail_after=0, exc=UnknownSubspaceError("no template for aspects"))
        with pytest.raises(AspectQueueEngineUnsupportedError, match="engine-service-v0.1.169") as one:
            queue.enqueue(_coll(), "/p/a.md", doc_id=_tumbler())
        assert ASPECT_QUEUE_ENV in str(one.value)
        assert isinstance(one.value.__cause__, UnknownSubspaceError)
        with pytest.raises(AspectQueueEngineUnsupportedError):
            queue.enqueue_many([
                {"collection": _coll(), "source_path": "/p/a.md", "doc_id": _tumbler()},
                {"collection": _coll(), "source_path": "/p/b.md", "doc_id": _tumbler()},
            ])


class TestBatchFailureIsCounted:
    def test_record_batch_enqueue_failure_writes_a_hook_failures_row(self) -> None:
        """The indexer's flush hook swallows a failed batch; in tuples mode it
        hands the failure here so a ``MaxLiveRowsExceeded`` is a counted row,
        not only a log line."""
        from nexus.aspect_worker import record_batch_enqueue_failure
        from nexus.mcp_infra import t2_index_write

        coll = _coll()
        record_batch_enqueue_failure(coll, 17, MaxLiveRowsExceededError("tenant at max_live_rows"))
        failures = t2_index_write(
            lambda t2: t2.telemetry.list_hook_failures(hook_names=["aspect_extraction_enqueue_hook"])
        )
        assert failures["total"] == 1
        text = str(failures["rows"])
        assert coll in text and "MaxLiveRowsExceededError" in text and "17" in text
