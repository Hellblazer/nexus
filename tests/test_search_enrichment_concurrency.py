"""Search enrichment overlaps the catalog and taxonomy round trips (nexus-vpa9q).

After retrieval, ``search_cross_corpus`` attached catalog doc_ids (one engine
round trip), then read topic assignments and topic link pairs (two more), one
after another on the caller's thread: 0.4-0.6 s of catalog then 0.5-0.7 s of
taxonomy on a first cloud search (2026-10-09, engine v0.1.154). The taxonomy
reads key on chunk ids, not on the doc_ids the catalog attaches, so they run
on a worker while the catalog attach runs. The MCP tools also stop building a
``T2Database`` per call for the taxonomy store: a shared reader sends each
call through the pooled process-lifetime T2 client.
"""

from __future__ import annotations

import threading
from types import SimpleNamespace

from nexus import mcp_infra
from nexus.search_engine import search_cross_corpus

_COL = "knowledge__vpa9q"
_RENDEZVOUS_S = 5.0


class _FakeT3:
    _voyage_client = None

    def search(self, query, collection_names, n_results=10, where=None):
        return [
            {"id": f"c{i}", "content": f"text {i}", "distance": 0.1 + 0.01 * i,
             "chunk_text_hash": f"{i:064x}"}
            for i in range(4)
        ]


class _RendezvousCatalog:
    """chash_positions blocks until the taxonomy read has started."""

    def __init__(self, taxonomy_started: threading.Event, catalog_started: threading.Event):
        self._taxonomy_started = taxonomy_started
        self._catalog_started = catalog_started
        self.saw_taxonomy_in_flight = False

    def chash_positions(self, chashes):
        self._catalog_started.set()
        self.saw_taxonomy_in_flight = self._taxonomy_started.wait(_RENDEZVOUS_S)
        return [
            {"chash": c, "doc_id": f"1.1.{i}", "position": 0, "chunk_count": 1}
            for i, c in enumerate(chashes)
        ]


class _RendezvousTaxonomy:
    """get_assignments_for_docs blocks until the catalog lookup has started."""

    def __init__(self, taxonomy_started: threading.Event, catalog_started: threading.Event,
                 *, assignments_error: Exception | None = None,
                 links_error: Exception | None = None):
        self._taxonomy_started = taxonomy_started
        self._catalog_started = catalog_started
        self._assignments_error = assignments_error
        self._links_error = links_error
        self.saw_catalog_in_flight = False
        self.calls: list[tuple[str, str]] = []

    def get_assignments_for_docs(self, ids):
        self.calls.append(("assignments", threading.current_thread().name))
        self._taxonomy_started.set()
        self.saw_catalog_in_flight = self._catalog_started.wait(_RENDEZVOUS_S)
        if self._assignments_error is not None:
            raise self._assignments_error
        return {doc_id: 7 for doc_id in ids}

    def get_topic_link_pairs(self, topic_ids):
        self.calls.append(("links", threading.current_thread().name))
        if self._links_error is not None:
            raise self._links_error
        return {}


def _wired(**taxonomy_kw):
    taxonomy_started, catalog_started = threading.Event(), threading.Event()
    catalog = _RendezvousCatalog(taxonomy_started, catalog_started)
    taxonomy = _RendezvousTaxonomy(taxonomy_started, catalog_started, **taxonomy_kw)
    return catalog, taxonomy


def test_catalog_attach_and_topic_reads_overlap() -> None:
    catalog, taxonomy = _wired()
    results = search_cross_corpus(
        "q", [_COL], n_results=4, t3=_FakeT3(), catalog=catalog,
        taxonomy=taxonomy, cluster_by=None, link_boost=False,
    )
    # A serial implementation would leave one side waiting the full timeout.
    assert catalog.saw_taxonomy_in_flight
    assert taxonomy.saw_catalog_in_flight
    assert taxonomy.calls and all(t != threading.main_thread().name for _, t in taxonomy.calls)
    assert {r.metadata.get("doc_id") for r in results} == {"1.1.0", "1.1.1", "1.1.2", "1.1.3"}
    assert all(r.topic_boost != 0.0 for r in results)  # the boost still landed


def test_a_failed_assignments_read_leaves_results_unboosted() -> None:
    catalog, taxonomy = _wired(assignments_error=RuntimeError("taxonomy down"))
    results = search_cross_corpus(
        "q", [_COL], n_results=4, t3=_FakeT3(), catalog=catalog,
        taxonomy=taxonomy, cluster_by=None, link_boost=False,
    )
    assert len(results) == 4
    assert all(r.topic_boost == 0.0 for r in results)
    assert [m for m, _ in taxonomy.calls] == ["assignments"]


def test_a_failed_link_read_skips_the_boost_as_before() -> None:
    catalog, taxonomy = _wired(links_error=RuntimeError("links down"))
    results = search_cross_corpus(
        "q", [_COL], n_results=4, t3=_FakeT3(), catalog=catalog,
        taxonomy=taxonomy, cluster_by=None, link_boost=False,
    )
    assert len(results) == 4
    assert all(r.topic_boost == 0.0 for r in results)


def test_no_taxonomy_still_attaches_doc_ids() -> None:
    catalog = _RendezvousCatalog(threading.Event(), threading.Event())
    catalog._taxonomy_started.set()  # nothing to wait for
    results = search_cross_corpus(
        "q", [_COL], n_results=4, t3=_FakeT3(), catalog=catalog,
        taxonomy=None, cluster_by=None, link_boost=False,
    )
    assert {r.metadata.get("doc_id") for r in results} == {"1.1.0", "1.1.1", "1.1.2", "1.1.3"}


def test_shared_taxonomy_reader_routes_through_the_pooled_t2_writer(monkeypatch) -> None:
    calls: list[tuple[str, tuple]] = []
    ops: list[str] = []

    class _Taxonomy:
        def get_assignments_for_docs(self, ids):
            calls.append(("get_assignments_for_docs", (ids,)))
            return {"c0": 3}

    def _fake_write(fn, *, op="t2_write"):
        ops.append(op)
        return fn(SimpleNamespace(taxonomy=_Taxonomy()))

    monkeypatch.setattr(mcp_infra, "t2_index_write", _fake_write)
    reader = mcp_infra.search_taxonomy()
    assert reader.get_assignments_for_docs(["c0"]) == {"c0": 3}
    assert calls == [("get_assignments_for_docs", (["c0"],))]
    assert ops == ["taxonomy.get_assignments_for_docs"]
    assert mcp_infra.search_taxonomy() is reader
