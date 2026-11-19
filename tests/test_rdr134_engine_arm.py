# SPDX-License-Identifier: AGPL-3.0-or-later
"""RDR-134 Step 5a-ii: the engine-route benchmark arm and the Minimum Viable Validation's bars."""
from __future__ import annotations

import logging
from types import SimpleNamespace
from typing import Any

import pytest
import structlog

from bench import topic_route_engine_arm as arm
from nexus.topic_route import EVENT

@pytest.fixture(autouse=True)
def _info_events_are_logged():
    """The arm captures the info-level search_topic_route event; the suite's default level filters it."""
    structlog.configure(wrapper_class=structlog.make_filtering_bound_logger(logging.INFO))


DOCS = "docs__1-1__bge-base-en-v1-5__v1"
RDR = "rdr__1-1__bge-base-en-v1-5__v1"


class _Catalog:
    def __init__(self, paths: dict[str, str]) -> None:
        self.paths = paths

    def docs_for_chashes(self, chashes):
        return {c: [c] for c in chashes if c in self.paths}

    def resolve_many(self, ids):
        return {i: SimpleNamespace(file_path=self.paths[i]) for i in ids}


_CAT = _Catalog({"good": "docs/rdr/rdr-001-x.md", "bad": "docs/other.md"})
_Q = {"qid": "R1", "kind": "rdr", "text": "q", "gt": {"rdr001": 3}, "owner": None, "exact": False}


def _hit(chash: str, coll: str, d: float) -> SimpleNamespace:
    return SimpleNamespace(id=chash, collection=coll, distance=d)


def _search(by_route: dict[Any, list], event: dict[str, Any] | None = None):
    """A search_cross_corpus stand-in: rows per topic_route (None, the 0.9 gate, the gate off); a routed call
    logs the event the client would."""
    calls: list = []

    def fn(query, collections, n, t3, *, cluster_by=None, taxonomy=None, sensitivity="include", topic_route=None):
        calls.append(topic_route)
        key = None if topic_route is None else topic_route.get("min_coverage", "default")
        if topic_route is not None:
            structlog.get_logger("tests.arm").info(EVENT, **(event or {"routed": 1, "statuses": {RDR: "routed"}}))
        return by_route[key]

    fn.calls = calls
    return fn


def _plain(rows: dict[str, list[dict]]):
    return lambda t3, query, c: rows.get(c, [])


_PLAIN = {RDR: [{"chash": "bad", "distance": 0.2, "collection": RDR}, {"chash": "good", "distance": 0.3,
                                                                        "collection": RDR}]}


def _measure(by_route, event=None, plain=None):
    return arm.measure(_Q, [RDR, DOCS], None, _CAT, search=_search(by_route, event),
                       plain_search=_plain(plain or _PLAIN))


def test_each_question_runs_p0_off_and_both_gates_and_the_routed_calls_name_their_threshold() -> None:
    search = _search({None: [_hit("bad", RDR, 0.2)], "default": [_hit("good", RDR, 0.1)], 0.0: [_hit("good", RDR, 0.1)]})
    row = arm.measure(_Q, [RDR, DOCS], None, _CAT, search=search, plain_search=_plain(_PLAIN))
    assert search.calls == [None, {"mode": "top1"}, {"mode": "top1", "min_coverage": 0.0}]
    assert set(row) >= {"P0", "OFF", "ENG", "ENG_gate_off"}
    assert row["ENG"]["ndcg3"] == 1.0 and row["P0"]["ndcg3"] == 0.0
    assert row["ENG"]["s"] >= 0 and row["P0"]["s"] >= 0


def test_the_route_event_is_kept_per_arm_with_statuses_coverage_and_short_rows() -> None:
    # the unrouted DOCS collection returned the request's full per-collection k (2)
    event = {"routed": 1, "no_topics": 1, "fallback": 0, "skipped": 0,
             "statuses": {RDR: "routed", DOCS: "no_topics"}, "coverage": {RDR: 0.95}, "raw_counts": {RDR: 1, DOCS: 2}}
    row = _measure({None: [_hit("bad", RDR, 0.2)], "default": [_hit("good", RDR, 0.1)],
                    0.0: [_hit("good", RDR, 0.1)]}, event)
    r = row["ENG"]["route"]
    assert r["statuses"] == {RDR: "routed", DOCS: "no_topics"} and r["coverage"] == {RDR: 0.95}
    # the exact scan returned 2 rows for that collection, the routed arm 1: one short collection
    assert r["short"] == [RDR]


def test_every_arm_is_scored_in_distance_order_as_134_research_13_scored_it() -> None:
    # research-13: search_cross_corpus "as returned, unordered, scores 0.02 ... the bench must sort"
    unsorted = [_hit("bad", RDR, 0.4), _hit("good", RDR, 0.1)]
    row = _measure({None: unsorted, "default": unsorted, 0.0: unsorted})
    assert row["P0"]["top3"][0] == row["ENG"]["top3"][0] == row["ENG_gate_off"]["top3"][0] == "rdr-001-x.md"


def test_a_routed_call_that_logs_no_event_is_vacuous_not_a_pass() -> None:
    def silent(query, collections, n, t3, **kw):
        return [_hit("good", RDR, 0.1)]

    with pytest.raises(arm.Vacuous):
        arm.measure(_Q, [RDR], None, _CAT, search=silent, plain_search=_plain(_PLAIN))


def _row(qid: str, kind: str, eng: float, off: float, *, eng_s: float = 1.0, p0_s: float = 1.0,
         gate_off: float | None = None) -> dict[str, Any]:
    def s(v, t=None):
        return {"ndcg3": v, "recall10": v, **({"s": t} if t is not None else {})}
    return {"qid": qid, "kind": kind, "P0": s(off, p0_s), "OFF": s(off), "ENG": s(eng, eng_s),
            "ENG_gate_off": s(eng if gate_off is None else gate_off, eng_s)}


def test_the_three_bars_are_the_rdrs_and_read_on_the_rdr_questions() -> None:
    rows = [_row(f"R{i}", "rdr", 0.5, 0.3) for i in range(4)] + [_row("T1", "title", 0.0, 1.0)]
    v = arm.bars(rows)
    assert (v["ndcg3_rdr"], v["checks"]["ndcg3"]) == (pytest.approx(0.5), True)
    assert v["checks"]["no_question_worse_than_off_by_more_than_0.1"] is False, "the title question lost 1.0"
    assert v["worse_than_off"] == ["T1"]
    assert v["checks"]["latency"] is True and v["passed"] is False
    assert arm.NDCG3_BAR == 0.44 and arm.WORSE_BY == 0.1 and arm.LATENCY_RATIO == 1.5


def test_the_latency_bar_compares_the_routed_request_with_the_unrouted_one() -> None:
    rows = [_row(f"R{i}", "rdr", 0.5, 0.3, eng_s=2.0, p0_s=1.0) for i in range(3)]
    v = arm.bars(rows)
    assert v["latency_ratio"] == pytest.approx(2.0) and v["checks"]["latency"] is False


def test_the_gate_comparison_is_reported_beside_the_bars() -> None:
    rows = [_row(f"R{i}", "rdr", 0.40, 0.3, gate_off=0.46) for i in range(3)]
    v = arm.bars(rows)
    assert v["gate_off"]["ndcg3_rdr"] == pytest.approx(0.46) and v["gate_off"]["checks"]["ndcg3"] is True
    assert v["checks"]["ndcg3"] is False
    assert "threshold rule" in v["gate_note"]
