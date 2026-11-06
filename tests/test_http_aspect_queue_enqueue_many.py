# SPDX-License-Identifier: AGPL-3.0-or-later
"""nexus-nj4ch: HttpAspectQueue.enqueue_many batches N document enqueues.

RDR-235: the batch is the bounded tuple fan-out (one ``out mode=replace`` per
document on the ``aspects`` template). The table route it once posted to, and the
per-row fallback that replayed a failed batch through ``enqueue``, are retired
(nexus-hbnml); those tests are gone. The producer's own batch pins live in
``tests/test_http_aspect_queue_tuples_producer.py``.
"""
from __future__ import annotations

import json
from unittest.mock import patch

import httpx
import pytest

from nexus.collection_errors import SupersededCollectionWriteError
from nexus.db.t2.http_aspect_queue import HttpAspectQueue
from nexus.db.t2.http_tuple_store import HttpTupleStore


@pytest.fixture(autouse=True)
def _no_real_catalog_registration(monkeypatch: pytest.MonkeyPatch):
    """The producer checks collection registration before each write (RDR-204,
    O4). This file's tests are pure HTTP-shape and fan-out tests that never
    configure a real catalog endpoint, so the registration step is a passthrough.
    """
    monkeypatch.setattr(
        "nexus.corpus.write_with_registration_retry",
        lambda name, write_fn, **kwargs: write_fn(),
    )
    monkeypatch.setattr("nexus.corpus.ensure_collection_registered", lambda *a, **k: None)


@pytest.fixture
def queue(monkeypatch) -> HttpAspectQueue:
    monkeypatch.setenv("NX_SERVICE_TOKEN", "test-token")
    return HttpAspectQueue(base_url="http://mock.test")


def _outs(out_mock) -> list[dict]:
    """The identity bodies of a patched ``out_replace``, with their nonces."""
    return [
        {"nonce": c.kwargs["nonce"], "subspace": c.args[0], "body": json.loads(c.args[3])}
        for c in out_mock.call_args_list
    ]


class TestEnqueueManyRealUrlConstruction:
    """Genuine end-to-end URL-construction check via httpx.MockTransport —
    NOT mocking the store's methods, which can pass while asserting the SAME
    bug it should catch (the doubled ``/queue/queue`` segment that 404'd against
    the live service). This test drives the real httpx.Client and the tuple
    store's real path join, so a future path regression 404s HERE, not only in
    production.
    """

    def test_enqueue_many_hits_the_real_service_path(self) -> None:
        requests: list[str] = []

        def handler(request: httpx.Request) -> httpx.Response:
            requests.append(request.url.path)
            return httpx.Response(200, json={"id": "task-1", "outcome": "created"})

        queue = HttpAspectQueue(
            base_url="http://mock.test", _token="test-token",
            client=httpx.Client(transport=httpx.MockTransport(handler)),
        )

        n = queue.enqueue_many([{"collection": "code__x", "source_path": "a.py", "doc_id": "1.1.1"}])

        assert requests == ["/v1/tuples/out"]
        assert n == 1


class TestEnqueueMany:
    def test_each_row_is_one_replace_on_the_aspects_template(self, queue: HttpAspectQueue) -> None:
        rows = [
            {"collection": "code__x", "source_path": "a.py", "doc_id": "1.1.1"},
            {"collection": "code__x", "source_path": "b.py", "doc_id": "1.1.2", "content_hash": "h"},
        ]
        with patch.object(HttpTupleStore, "out_replace", return_value=("t", "created")) as out:
            n = queue.enqueue_many(rows)

        assert n == 2
        outs = sorted(_outs(out), key=lambda o: o["nonce"])
        assert [o["nonce"] for o in outs] == ["1.1.1", "1.1.2"], "the nonce is the document"
        assert all(o["subspace"] == "aspects" for o in outs)
        assert outs[1]["body"] == {
            "collection": "code__x", "source_path": "b.py", "doc_id": "1.1.2", "content_hash": "h",
        }, "the body is the identity, nothing else"

    def test_empty_list_is_noop(self, queue: HttpAspectQueue) -> None:
        with patch.object(HttpTupleStore, "out_replace") as out:
            assert queue.enqueue_many([]) == 0
        out.assert_not_called()

    def test_superseded_collection_refusal_is_raised_before_any_task_is_written(
        self, queue: HttpAspectQueue, monkeypatch,
    ) -> None:
        """nexus-i15oc: a batch naming a retired collection is refused by the
        registration check (O4) before any ``out`` is sent, so the refusal is not
        replayed row by row. The typed error goes to the caller (the indexer's
        flush hook logs it and skips the worker spawn)."""

        def refuse(*_a, **_k):
            raise SupersededCollectionWriteError("code__x", "code__y")

        monkeypatch.setattr("nexus.corpus.ensure_collection_registered", refuse)
        rows = [
            {"collection": "code__x", "source_path": "a.py", "doc_id": "1.1.1"},
            {"collection": "code__x", "source_path": "b.py", "doc_id": "1.1.2"},
            {"collection": "code__x", "source_path": "c.py", "doc_id": "1.1.3"},
        ]

        with patch.object(HttpTupleStore, "out_replace") as out:
            with pytest.raises(SupersededCollectionWriteError) as caught:
                queue.enqueue_many(rows)

        assert caught.value.name == "code__x"
        assert caught.value.successor == "code__y"
        assert out.call_count == 0, "the refusal was replayed as writes: no task may be written"

    def test_malformed_rows_are_skipped_and_the_good_one_lands(self, queue: HttpAspectQueue) -> None:
        """A row with no collection, or no doc_id (the task's identity), is skipped
        and counted in one warning; its batch-mates still land."""
        rows = [
            {"collection": "", "source_path": "missing-collection.py", "doc_id": "1.1.1"},
            {"collection": "code__x", "source_path": "nodoc.py"},
            {"collection": "code__x", "source_path": "good.py", "doc_id": "1.1.2"},
        ]
        with patch.object(HttpTupleStore, "out_replace", return_value=("t", "created")) as out:
            n = queue.enqueue_many(rows)

        assert n == 1
        assert [o["nonce"] for o in _outs(out)] == ["1.1.2"]


class TestEnqueueManyWakeSignal:
    """nexus-59611 stage 2: enqueue_many's batch path does not go through
    enqueue(), so the wake signal has its own call site there — this pins it."""

    def test_batch_success_signals_wake_once(
        self, queue: HttpAspectQueue, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        calls: list[None] = []
        monkeypatch.setattr(
            "nexus.aspect_worker.signal_aspect_wake",
            lambda *a, **k: calls.append(None),
        )
        with patch.object(HttpTupleStore, "out_replace", return_value=("t", "created")):
            n = queue.enqueue_many([
                {"collection": "code__x", "source_path": "a.py", "doc_id": "1.1.1"},
                {"collection": "code__x", "source_path": "b.py", "doc_id": "1.1.2"},
            ])
        assert n == 2
        assert len(calls) == 1, "one wake for the whole batch, not one per document"

    def test_a_batch_that_writes_nothing_does_not_signal(
        self, queue: HttpAspectQueue, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        calls: list[None] = []
        monkeypatch.setattr(
            "nexus.aspect_worker.signal_aspect_wake",
            lambda *a, **k: calls.append(None),
        )
        with patch.object(HttpTupleStore, "out_replace") as out:
            n = queue.enqueue_many([{"collection": "code__x", "source_path": "a.py"}])
        assert n == 0
        out.assert_not_called()
        assert calls == []
