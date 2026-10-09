# SPDX-License-Identifier: AGPL-3.0-or-later
"""``get_labels_for_ids`` uses ``POST /v1/taxonomy/topics/by_ids``.

nexus-w032x part 2. Search's topic grouping (the CLI default) asked for the
label of every topic in the result window with one ``GET /topics/by_id`` each:
about 39 requests, 0.54-0.98 s of a default managed search. The engine now
serves them in one request. These drive the real ``HttpTaxonomyStore`` over a
recording ``httpx.MockTransport`` and count requests by path, so a regression
shows up as a number. The pooled per-id path itself is pinned by
``tests/test_w032x_search_round_trips.py``.
"""
from __future__ import annotations

import json
import re
from pathlib import Path

import httpx
import pytest

from nexus.db.t2 import http_taxonomy_store as mod
from nexus.db.t2.http_taxonomy_store import HttpTaxonomyStore


class _TaxonomyEngine:
    """Answers the topic routes; ``by_ids`` selects how the batched route behaves."""

    def __init__(self, by_ids: str = "serve", known: set[int] | None = None) -> None:
        self.by_ids = by_ids  # serve | absent | edge | error | malformed
        self.known = known
        self.requests: list[tuple[str, str]] = []
        self.batches: list[list[int]] = []

    def _topic(self, tid: int) -> dict | None:
        if self.known is not None and tid not in self.known:
            return None
        return {"id": tid, "label": f"topic {tid}", "collection": "c"}

    def handler(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path.removeprefix("/v1/taxonomy")
        self.requests.append((request.method, path))
        if path == "/topics/by_ids":
            if self.by_ids == "absent":
                return httpx.Response(404, json={"error": "not found"})
            if self.by_ids == "edge":
                return httpx.Response(403, json={"error": "forbidden"})
            if self.by_ids == "error":
                return httpx.Response(500, json={"error": "boom"})
            if self.by_ids == "malformed":
                return httpx.Response(200, json={"not": "a list"})
            ids = json.loads(request.content)["ids"]
            self.batches.append(ids)
            return httpx.Response(200, json=[t for i in ids if (t := self._topic(i))])
        if path == "/topics/by_id":
            t = self._topic(int(request.url.params["id"]))
            if t is None:
                return httpx.Response(404, json={"error": "not found"})
            return httpx.Response(200, json=t)
        return httpx.Response(404, json={"error": f"unexpected {path}"})

    def count(self, path: str) -> int:
        return sum(1 for _m, p in self.requests if p == path)


def _wired(engine: _TaxonomyEngine, handler=None) -> HttpTaxonomyStore:
    return HttpTaxonomyStore(
        base_url="http://engine.invalid",
        _token="t",
        client=httpx.Client(transport=httpx.MockTransport(handler or engine.handler)),
    )


def test_labels_come_from_one_batched_request_and_match_the_per_id_path() -> None:
    ids = [5, 3, 9, 3, 7, 1, 2, 8, 4, 6, 10]
    known = set(range(1, 10))  # 10 has no topic

    batched_engine = _TaxonomyEngine(known=known)
    batched = _wired(batched_engine).get_labels_for_ids(ids)
    assert batched_engine.requests == [("POST", "/topics/by_ids")], batched_engine.requests
    assert batched_engine.batches == [[5, 3, 9, 7, 1, 2, 8, 4, 6, 10]], "deduped, in input order"

    per_id_engine = _TaxonomyEngine(by_ids="absent", known=known)
    per_id = _wired(per_id_engine).get_labels_for_ids(ids)

    assert batched == per_id
    assert list(batched) == [5, 3, 9, 7, 1, 2, 8, 4, 6], "input order; the id with no topic is omitted"


def test_one_id_is_a_plain_get_and_never_probes_the_batched_route() -> None:
    engine = _TaxonomyEngine(by_ids="absent")
    store = _wired(engine)

    assert store.get_labels_for_ids([4, 4]) == {4: "topic 4"}
    assert store.get_labels_for_ids([]) == {}
    assert engine.requests == [("GET", "/topics/by_id")], engine.requests


def test_an_engine_without_the_route_is_probed_once_per_window_then_served_per_id(monkeypatch) -> None:
    clock = {"t": 1000.0}
    monkeypatch.setattr(mod, "_monotonic", lambda: clock["t"])
    engine = _TaxonomyEngine(by_ids="absent")
    store = _wired(engine)

    assert store.get_labels_for_ids([1, 2, 3]) == {1: "topic 1", 2: "topic 2", 3: "topic 3"}
    assert engine.count("/topics/by_ids") == 1
    assert engine.count("/topics/by_id") == 3

    # Inside the window: no second probe, however many searches follow.
    clock["t"] += mod._TOPICS_BY_IDS_ROUTE_RETRY_S - 1
    store.get_labels_for_ids([1, 2, 3])
    assert engine.count("/topics/by_ids") == 1

    # After the window the route is probed again; an upgraded engine is picked
    # up without a restart.
    clock["t"] += 2
    engine.by_ids = "serve"
    assert store.get_labels_for_ids([1, 2, 3]) == {1: "topic 1", 2: "topic 2", 3: "topic 3"}
    assert engine.count("/topics/by_ids") == 2
    assert engine.count("/topics/by_id") == 6, "the upgraded call used the batched route"


@pytest.mark.parametrize("mode", ["edge", "error", "malformed"])
def test_a_refused_or_failing_route_falls_back_to_per_id_with_a_backoff(mode, monkeypatch) -> None:
    clock = {"t": 50.0}
    monkeypatch.setattr(mod, "_monotonic", lambda: clock["t"])
    engine = _TaxonomyEngine(by_ids=mode)
    store = _wired(engine)

    assert store.get_labels_for_ids([1, 2]) == {1: "topic 1", 2: "topic 2"}
    assert engine.count("/topics/by_ids") == 1
    assert engine.count("/topics/by_id") == 2

    window = (
        mod._TOPICS_BY_IDS_ROUTE_RETRY_S if mode == "edge" else mod._TOPICS_BY_IDS_FAILURE_RETRY_S
    )
    clock["t"] += window - 1
    store.get_labels_for_ids([1, 2])
    assert engine.count("/topics/by_ids") == 1, "inside the window"
    clock["t"] += 2
    store.get_labels_for_ids([1, 2])
    assert engine.count("/topics/by_ids") == 2, "after the window"


def test_more_than_300_ids_are_split_into_pages_of_300() -> None:
    engine = _TaxonomyEngine()
    ids = list(range(1, 651))

    labels = _wired(engine).get_labels_for_ids(ids)

    assert [len(b) for b in engine.batches] == [300, 300, 50]
    assert list(labels) == ids
    assert engine.count("/topics/by_id") == 0


def test_a_later_page_failing_serves_the_whole_call_per_id() -> None:
    """A page that fails after earlier pages succeeded must not return a
    partial map: the per-id path answers the whole call."""
    engine = _TaxonomyEngine()

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/topics/by_ids") and engine.batches:
            engine.requests.append((request.method, "/topics/by_ids"))
            return httpx.Response(500, json={"error": "boom"})
        return engine.handler(request)

    ids = list(range(1, 401))
    labels = _wired(engine, handler).get_labels_for_ids(ids)

    assert list(labels) == ids
    assert engine.count("/topics/by_id") == 400


def test_the_page_size_matches_the_engine_cap() -> None:
    """Parity pin, as for the last-discover batch: a client page above the
    engine's cap draws a 400; one below it under-uses a raised cap."""
    java = (
        Path(__file__).resolve().parent.parent
        / "service/src/main/java/dev/nexus/service/db/TaxonomyRepository.java"
    )
    m = re.search(r"MAX_TOPICS_BY_IDS\s*=\s*(\d+)", java.read_text(encoding="utf-8"))
    assert m is not None, "MAX_TOPICS_BY_IDS not found in TaxonomyRepository.java"
    assert mod._TOPICS_BY_IDS_PAGE == int(m.group(1))
