# SPDX-License-Identifier: AGPL-3.0-or-later
"""nexus-w032x: the display-path catalog lookup overlaps the topic grouping.

``_attach_display_paths`` ran ``catalog.resolve_many`` last, after the topic
grouping and boost, although it needs only the doc_ids the catalog attach has
already added: 0.15-0.17 s on a cold cloud search, serial behind ~0.8 s of
topic-label reads (T2 ``nexus/w032x-listing-prefetch-cloud-ab-2026-10-10``).
"""

from __future__ import annotations

import contextvars
import threading
from types import SimpleNamespace

from nexus.search_engine import (
    _attach_display_paths,
    _DisplayLookup,
    search_cross_corpus,
)
from nexus.types import SearchResult

_COL = "knowledge__w032x"
_RENDEZVOUS_S = 5.0


class _FakeT3:
    _voyage_client = None

    def search(self, query, collection_names, n_results=10, where=None):
        return [
            {"id": f"c{i}", "content": f"text {i}", "distance": 0.1 + 0.01 * i,
             "chunk_text_hash": f"{i:064x}"}
            for i in range(4)
        ]


def _entry(did: str) -> SimpleNamespace:
    return SimpleNamespace(file_path=f"/docs/{did}.md", title=f"Doc {did}", physical_collection=_COL)


class _Catalog:
    def __init__(self, resolve_started: threading.Event | None = None,
                 labels_started: threading.Event | None = None) -> None:
        self._resolve_started = resolve_started
        self._labels_started = labels_started
        self.saw_labels_in_flight = False
        self.resolved: list[set[str]] = []

    def chash_positions(self, chashes):
        return [
            {"chash": c, "doc_id": f"1.1.{i}", "position": 0, "chunk_count": 1}
            for i, c in enumerate(chashes)
        ]

    def resolve_many(self, doc_ids):
        self.resolved.append(set(doc_ids))
        if self._resolve_started is not None:
            self._resolve_started.set()
            self.saw_labels_in_flight = self._labels_started.wait(_RENDEZVOUS_S)
        return {d: _entry(d) for d in doc_ids}


class _Taxonomy:
    def __init__(self, resolve_started: threading.Event, labels_started: threading.Event) -> None:
        self._resolve_started = resolve_started
        self._labels_started = labels_started
        self.saw_resolve_in_flight = False

    def get_assignments_for_docs(self, ids):
        return {i: 7 for i in ids}

    def get_topic_link_pairs(self, topic_ids):
        return {}

    def get_labels_for_ids(self, topic_ids):
        self._labels_started.set()
        self.saw_resolve_in_flight = self._resolve_started.wait(_RENDEZVOUS_S)
        return {t: "Topic" for t in topic_ids}


def test_display_lookup_overlaps_the_topic_labels() -> None:
    resolve_started, labels_started = threading.Event(), threading.Event()
    catalog = _Catalog(resolve_started, labels_started)
    taxonomy = _Taxonomy(resolve_started, labels_started)

    results = search_cross_corpus(
        "q", [_COL], n_results=4, t3=_FakeT3(), catalog=catalog,
        taxonomy=taxonomy, cluster_by="semantic", link_boost=False,
    )

    # Serial code would leave one side waiting out the full timeout.
    assert catalog.saw_labels_in_flight
    assert taxonomy.saw_resolve_in_flight
    assert len(catalog.resolved) == 1, "the lookup ran twice"
    assert {r.metadata.get("_display_path") for r in results} == {
        f"/docs/1.1.{i}.md" for i in range(4)
    }
    assert all(r.metadata.get("_display_title") for r in results)


def _result(did: str) -> SearchResult:
    return SearchResult(id=did, content="x", distance=0.1, collection=_COL,
                        metadata={"doc_id": did})


def test_prefetched_maps_are_used_when_they_cover_the_results() -> None:
    catalog = _Catalog()
    results = [_result("1.1.0"), _result("1.1.1")]
    lookup = _DisplayLookup.start(results, catalog)

    _attach_display_paths(results, catalog, prefetched=lookup)

    assert catalog.resolved == [{"1.1.0", "1.1.1"}]
    assert results[1].metadata["_display_path"] == "/docs/1.1.1.md"


def test_a_doc_id_the_prefetch_missed_is_resolved_at_the_end() -> None:
    catalog = _Catalog()
    early = [_result("1.1.0")]
    lookup = _DisplayLookup.start(early, catalog)
    lookup.maps()
    final = [_result("1.1.0"), _result("1.1.9")]

    _attach_display_paths(final, catalog, prefetched=lookup)

    assert catalog.resolved == [{"1.1.0"}, {"1.1.0", "1.1.9"}]
    assert final[1].metadata["_display_path"] == "/docs/1.1.9.md"


def test_no_doc_ids_starts_no_lookup() -> None:
    results = [SearchResult(id="c0", content="x", distance=0.1, collection=_COL, metadata={})]
    assert _DisplayLookup.start(results, _Catalog()) is None


class _FailingCatalog(_Catalog):
    def resolve_many(self, doc_ids):
        self.resolved.append(set(doc_ids))
        raise RuntimeError("catalog down")


def test_a_failed_lookup_leaves_results_without_display_paths_as_before() -> None:
    catalog = _FailingCatalog()

    results = search_cross_corpus(
        "q", [_COL], n_results=4, t3=_FakeT3(), catalog=catalog,
        taxonomy=None, cluster_by=None, link_boost=False,
    )

    assert len(results) == 4
    assert len(catalog.resolved) == 1, "a failed lookup must not be retried"
    assert not any(r.metadata.get("_display_path") for r in results)
    assert {r.metadata.get("doc_id") for r in results} == {f"1.1.{i}" for i in range(4)}


_PROBE_VAR: contextvars.ContextVar[str] = contextvars.ContextVar("w032x_probe", default="unset")


def test_the_lookup_runs_in_the_callers_context() -> None:
    seen: list[str] = []

    class _CtxCatalog(_Catalog):
        def resolve_many(self, doc_ids):
            seen.append(_PROBE_VAR.get())
            return super().resolve_many(doc_ids)

    token = _PROBE_VAR.set("caller")
    try:
        lookup = _DisplayLookup.start([_result("1.1.0")], _CtxCatalog())
        lookup.maps()
    finally:
        _PROBE_VAR.reset(token)
    assert seen == ["caller"]
