# SPDX-License-Identifier: AGPL-3.0-or-later
"""Search enrichment must not repeat catalog round trips, and the fan-out
must use the concurrent-read quota.

nexus-w032x. A default cloud search (knowledge,code,docs,rdr) spent its
enrichment time in client round trips. The catalog reverse lookup
(docs_for_chashes) fetches every referencing document's manifest to rebuild
chash -> doc_id edges, and the doc-id attach then fetched a subset of the
SAME manifests again: two /manifest/get_many calls where one carries the
data. The search fan-out ran ~28 batches 8 at a time though the quota
allows 10. These count the calls, so a regression shows up as a number,
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


class _SlowT3:
    """Singleton-group collections (names without a model token), so every
    collection is its own batch; records peak concurrent search calls."""

    def __init__(self) -> None:
        self.active = self.peak = 0
        self._lock = threading.Lock()

    def search(self, query, collection_names, n_results=10, where=None):
        with self._lock:
            self.active += 1
            self.peak = max(self.peak, self.active)
        time.sleep(0.05)
        with self._lock:
            self.active -= 1
        return []


def test_the_search_fan_out_uses_the_concurrent_read_quota() -> None:
    from nexus.db.limits import QUOTAS

    t3 = _SlowT3()
    search_cross_corpus("q", [f"knowledge__c{i}" for i in range(28)], n_results=10, t3=t3)
    assert t3.peak == QUOTAS.MAX_CONCURRENT_READS
