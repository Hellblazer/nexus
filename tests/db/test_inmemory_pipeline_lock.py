# SPDX-License-Identifier: AGPL-3.0-or-later
"""The in-memory pipeline twin serves one request at a time.

The PDF pipeline's extractor thread writes pages while its chunker thread reads
them. Unguarded, a read iterated the page dict while a write grew it and raised
``RuntimeError: dictionary changed size during iteration``
(tests/integration/test_rdr223_pdf_journey.py on develop 2ea176075).
"""
from __future__ import annotations

import threading

import httpx

from nexus.db.inmemory_pipeline import InMemoryPipelineEngine


def test_a_second_request_waits_for_the_first_to_finish() -> None:
    engine = InMemoryPipelineEngine()
    inside = threading.Event()
    let_go = threading.Event()
    order: list[str] = []

    def slow_list(_payload: dict) -> dict:
        order.append("first-in")
        inside.set()
        assert let_go.wait(10), "the test never released the first request"
        order.append("first-out")
        return {"pipelines": []}

    def counts(_payload: dict) -> dict:
        order.append("second")
        return {}

    engine.list_pipelines = slow_list  # type: ignore[method-assign]
    engine.counts = counts  # type: ignore[method-assign]
    first = threading.Thread(target=engine.handler, args=(httpx.Request("GET", "http://twin/v1/pipeline/list"),))
    second = threading.Thread(target=engine.handler, args=(httpx.Request("GET", "http://twin/v1/pipeline/counts"),))
    first.start()
    assert inside.wait(10)
    second.start()
    second.join(0.3)
    assert second.is_alive(), "the second request ran while the first was still inside the twin"
    assert order == ["first-in"]
    let_go.set()
    first.join(10)
    second.join(10)
    assert order == ["first-in", "first-out", "second"]


def test_a_handler_may_reenter_the_twin_from_its_own_thread() -> None:
    """The lock is reentrant: a route method that dispatches through ``handler`` does not deadlock."""
    engine = InMemoryPipelineEngine()

    def nested(_payload: dict) -> dict:
        inner = engine.handler(httpx.Request("GET", "http://twin/v1/pipeline/list"))
        return {"inner_status": inner.status_code}

    engine.counts = nested  # type: ignore[method-assign]
    done: list[httpx.Response] = []
    worker = threading.Thread(
        target=lambda: done.append(engine.handler(httpx.Request("GET", "http://twin/v1/pipeline/counts"))))
    worker.start()
    worker.join(10)
    assert not worker.is_alive(), "the twin deadlocked on its own lock"
    assert done[0].json() == {"inner_status": 200}
