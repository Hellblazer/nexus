# SPDX-License-Identifier: AGPL-3.0-or-later
"""search_cross_corpus enrichment must not scale with an unbounded pool.

nexus-5ezgn / nexus-w032x. nx_answer's plan 473 searches
``knowledge,code,docs,rdr`` with ``threshold=2.0``. On the live cloud that
pooled 7,805 candidates from 80 collections for a 10-row answer, and the
contradiction check then fetched embeddings for every one of them, one
serial round trip per collection: 80 x 1.3 s = 104 s of a 146 s search
(profiled 2026-09-28), and 310 s in the shakeout's nx_answer run.

Earlier tests used a handful of rows in one or two collections, where
neither the pool size nor the serial loop costs anything. These build the
production shape: 80 collections, 100 rows each, no threshold.
"""
from __future__ import annotations

import threading
import time

import numpy as np

from nexus.search_engine import SearchResult, _cap_enrichment_pool, search_cross_corpus

N_COLS = 80
ROWS_PER_COL = 100


class _WideT3:
    """80 collections, 100 rows each; records every embedding fetch."""

    def __init__(self) -> None:
        self.embedding_ids: list[str] = []
        self.active = 0
        self.max_active = 0
        self._lock = threading.Lock()

    def search(self, query, collection_names, n_results=10, where=None):
        col = collection_names[0]
        i = int(col.rsplit("c", 1)[1])
        return [
            {
                "id": f"{col}-{j}",
                "content": f"row {j}",
                # Interleaved so the 300 best rows span every collection.
                "distance": 0.3 + j * 0.01 + i * 0.0001,
            }
            for j in range(ROWS_PER_COL)
        ]

    def get_embeddings(self, collection_name, ids):
        with self._lock:
            self.active += 1
            self.max_active = max(self.max_active, self.active)
            self.embedding_ids.extend(ids)
        time.sleep(0.02)
        with self._lock:
            self.active -= 1
        return np.ones((len(ids), 4), dtype=np.float32)


def _cols() -> list[str]:
    return [f"knowledge__c{i}" for i in range(N_COLS)]


def test_embedding_fetch_is_bounded_by_the_page_cap_not_the_pool() -> None:
    t3 = _WideT3()
    results = search_cross_corpus("q", _cols(), n_results=10, t3=t3)

    assert len(results) == 300
    assert len(t3.embedding_ids) == 300, (
        f"fetched embeddings for {len(t3.embedding_ids)} rows of a "
        f"{N_COLS * ROWS_PER_COL}-row pool"
    )
    assert "knowledge__c0-0" in {r.id for r in results}, "the nearest row was cut"


def test_embedding_fetch_runs_collections_in_parallel() -> None:
    t3 = _WideT3()
    search_cross_corpus("q", _cols(), n_results=10, t3=t3)
    assert t3.max_active > 1, "one embedding round trip at a time"


def _row(rid: str, distance: float, **meta) -> SearchResult:
    return SearchResult(id=rid, content="", distance=distance,
                        collection="knowledge__x", metadata=dict(meta))


def test_cap_keeps_every_lexical_row_and_input_order() -> None:
    rows = [_row("far-lexical", 0.99), _row("a", 0.10), _row("b", 0.20), _row("c", 0.30)]
    kept = _cap_enrichment_pool(rows, {"far-lexical"}, cap=2)
    assert [r.id for r in kept] == ["far-lexical", "a", "b"]


def test_cap_ranks_rerank_scored_rows_ahead_of_unscored() -> None:
    rows = [
        _row("near-unscored", 0.05),
        _row("low-score", 0.40, rerank_score=0.1),
        _row("high-score", 0.50, rerank_score=0.9),
    ]
    kept = _cap_enrichment_pool(rows, set(), cap=2)
    assert [r.id for r in kept] == ["low-score", "high-score"]


def test_cap_is_a_no_op_under_the_limit() -> None:
    rows = [_row("a", 0.1), _row("b", 0.2)]
    assert _cap_enrichment_pool(rows, set(), cap=2) is rows


def test_pool_cap_scales_with_the_requested_page() -> None:
    """A caller asking for more than MAX_QUERY_RESULTS (nx search -m 1000)
    must get a pool at least that large (critic finding on 8f31f90f9)."""
    t3 = _WideT3()
    results = search_cross_corpus("q", _cols(), n_results=100, t3=t3)
    assert len(results) == 400


def test_search_stops_at_a_stage_boundary_once_its_deadline_passes() -> None:
    """An abandoned search (nx_answer's budget cut) stops instead of making
    its remaining round trips: no embedding fetch runs after the deadline."""
    import pytest

    from nexus import call_deadline

    t3 = _WideT3()
    token = call_deadline.set_deadline(time.monotonic() - 1.0)
    try:
        with pytest.raises(call_deadline.DeadlineExceeded):
            search_cross_corpus("q", _cols(), n_results=10, t3=t3)
    finally:
        call_deadline.reset(token)
    assert t3.embedding_ids == []
