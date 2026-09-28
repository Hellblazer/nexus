# SPDX-License-Identifier: AGPL-3.0-or-later
"""Search enrichment must not repeat catalog round trips.

nexus-w032x. A default cloud search (knowledge,code,docs,rdr) spent its
enrichment time in client round trips. The catalog reverse lookup
(docs_for_chashes) fetches every referencing document's manifest to rebuild
chash -> doc_id edges, and the doc-id attach then fetched a subset of the
SAME manifests again: two /manifest/get_many calls where one carries the
data. These count the calls, so a regression shows up as a number,
not as cloud latency that varies run to run.
"""
from __future__ import annotations

import threading
import time

from nexus.search_engine import SearchResult, _attach_doc_ids_from_catalog, search_cross_corpus


class _CountingCatalog:
    """The HttpCatalogClient reverse-lookup surface, counting round trips."""

    def __init__(self) -> None:
        self.calls: list[str] = []

    def docs_and_manifests_for_chashes(self, chashes):
        from types import SimpleNamespace

        self.calls.append("docs_and_manifests_for_chashes")
        docs = {c: [f"1.1.{i}"] for i, c in enumerate(chashes)}
        manifests = {f"1.1.{i}": [SimpleNamespace(chash=c, position=0),
                                  SimpleNamespace(chash="x" * 64, position=1)]
                     for i, c in enumerate(chashes)}
        return docs, manifests

    def get_manifests(self, doc_ids):
        self.calls.append("get_manifests")
        return {}

    def docs_for_chashes(self, chashes):
        self.calls.append("docs_for_chashes")
        return self.docs_and_manifests_for_chashes(chashes)[0]


def test_the_doc_id_attach_fetches_manifests_once() -> None:
    rows = [SearchResult(id=f"r{i}", content="", distance=0.1, collection="knowledge__x",
                         metadata={"chunk_text_hash": f"{i:064d}"}) for i in range(5)]
    cat = _CountingCatalog()
    _attach_doc_ids_from_catalog(rows, cat)

    assert cat.calls == ["docs_and_manifests_for_chashes"], cat.calls
    assert [r.metadata["doc_id"] for r in rows] == [f"1.1.{i}" for i in range(5)]
    assert {r.metadata["chunk_count"] for r in rows} == {2}
    assert {r.metadata["chunk_index"] for r in rows} == {0}


def test_a_legacy_doc_id_the_reverse_lookup_missed_still_gets_its_manifest() -> None:
    """Review of f80ce4e0a: a chunk that already carries its own doc_id may
    name a doc the reverse lookup never found; taking manifests only from
    the prefetch dropped its chunk_count and chunk_index."""
    from types import SimpleNamespace

    cat = _CountingCatalog()
    fetched: list[list[str]] = []

    def get_manifests(doc_ids):
        fetched.append(list(doc_ids))
        return {d: [SimpleNamespace(chash="l" * 64, position=3)] for d in doc_ids}

    cat.get_manifests = get_manifests
    rows = [
        SearchResult(id="new", content="", distance=0.1, collection="knowledge__x",
                     metadata={"chunk_text_hash": f"{0:064d}"}),
        SearchResult(id="legacy", content="", distance=0.2, collection="knowledge__x",
                     metadata={"chunk_text_hash": "l" * 64, "doc_id": "9.9.9"}),
    ]
    _attach_doc_ids_from_catalog(rows, cat)

    assert fetched == [["9.9.9"]], "only the doc the prefetch missed is fetched"
    assert rows[1].metadata["chunk_count"] == 1
    assert rows[1].metadata["chunk_index"] == 3
    assert rows[0].metadata["chunk_count"] == 2


def test_topic_labels_are_fetched_concurrently_and_completely() -> None:
    """The engine has no batched by-id topic route; search's topic grouping
    (the CLI default) looked labels up one serial GET at a time: 9 in the
    shakeout profile."""
    from nexus.db.t2.http_taxonomy_store import HttpTaxonomyStore

    store = object.__new__(HttpTaxonomyStore)
    lock = threading.Lock()
    state = {"active": 0, "peak": 0}

    def get_topic_by_id(tid):
        with lock:
            state["active"] += 1
            state["peak"] = max(state["peak"], state["active"])
        time.sleep(0.05)
        with lock:
            state["active"] -= 1
        return None if tid == 7 else {"id": tid, "label": f"topic {tid}"}

    store.get_topic_by_id = get_topic_by_id
    labels = store.get_labels_for_ids([3, 1, 7, 2, 3, 9, 4, 5, 6])

    assert labels == {3: "topic 3", 1: "topic 1", 2: "topic 2", 9: "topic 9",
                      4: "topic 4", 5: "topic 5", 6: "topic 6"}
    assert list(labels) == [3, 1, 2, 9, 4, 5, 6]
    assert state["peak"] > 1, "labels fetched one at a time"


def test_a_failing_concurrent_label_fetch_falls_back_to_the_serial_loop() -> None:
    """Review of 791bc1a81: concurrent workers share one client whose
    self-heal is unlocked; when the pool fails, the serial loop runs."""
    from nexus.db.t2.http_taxonomy_store import HttpTaxonomyStore

    store = object.__new__(HttpTaxonomyStore)
    calls = {"n": 0}
    lock = threading.Lock()

    def get_topic_by_id(tid):
        with lock:
            calls["n"] += 1
            first_wave = calls["n"] <= 3
        if first_wave and tid == 2:
            raise RuntimeError("401 mid token rotation")
        return {"id": tid, "label": f"topic {tid}"}

    store.get_topic_by_id = get_topic_by_id
    assert store.get_labels_for_ids([1, 2, 3]) == {1: "topic 1", 2: "topic 2", 3: "topic 3"}
