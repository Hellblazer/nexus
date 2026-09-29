# SPDX-License-Identifier: AGPL-3.0-or-later
"""The topic boost must not evict the nearest hit or make the page depend on -m.

nexus-2yshe (7.64.1 shakeout, surface A, A1). ``nx search 'SchemaMigrator
dedup changeset identity' --corpus code__1-1__voyage-code-3__v1``: the best
chunk (d=0.239) was absent at -m 1 and -m 5, third at -m 3, first at -m 10.
The engine returned it every time. apply_topic_boost gave a flat 0.1
distance credit to every row with a same-topic peer in the window, the
voyage-code-3 top-10 spans about 0.1, and 0.239's topic had no peer, so it
fell to rank 8 of 10 and [:n] cut it.

The earlier topic-boost tests used windows spread 0.3 wide with the credit
landing on the right rows, where a flat 0.1 changes nothing that matters.
This builds the measured shape: a 0.1-wide window, the nearest row alone in
its topic, every other row paired.
"""
from __future__ import annotations

from nexus.scoring import apply_hybrid_scoring, apply_topic_boost
from nexus.types import SearchResult

#: d=0.239 alone in topic 99; nine rows 0.26-0.34 in four paired topics.
_ROWS = [("best", 0.239, 99)] + [
    (f"r{i}", 0.26 + i * 0.01, i // 2) for i in range(9)
]


def _ranked(n: int) -> list[str]:
    window = [
        SearchResult(id=rid, content="", distance=d, collection="code__x",
                     metadata={"frecency_score": 0.0})
        for rid, d, _ in _ROWS[: max(n, 1) * 2]  # a window that grows with -m
    ]
    topics = {rid: t for rid, _, t in _ROWS}
    apply_topic_boost(window, topics)
    scored = apply_hybrid_scoring(window, hybrid=False)
    return [r.id for r in scored][:n]


def test_nearest_hit_stays_first_in_a_narrow_window() -> None:
    assert _ranked(10)[0] == "best"


def test_the_page_does_not_depend_on_its_size() -> None:
    for n in (1, 3, 5, 10):
        assert _ranked(n)[0] == "best", f"-m {n}: {_ranked(n)}"


def test_a_near_tie_is_still_broken_toward_topical_coherence() -> None:
    """The credit is kept as a tie-break: of two rows 0.001 apart, the one
    with a same-topic peer wins."""
    rows = [
        SearchResult(id="alone", content="", distance=0.300, collection="code__x",
                     metadata={"frecency_score": 0.0}),
        SearchResult(id="paired", content="", distance=0.301, collection="code__x",
                     metadata={"frecency_score": 0.0}),
        SearchResult(id="peer", content="", distance=0.400, collection="code__x",
                     metadata={"frecency_score": 0.0}),
    ]
    apply_topic_boost(rows, {"alone": 1, "paired": 2, "peer": 2})
    scored = apply_hybrid_scoring(rows, hybrid=False)
    assert [r.id for r in scored][0] == "paired"
