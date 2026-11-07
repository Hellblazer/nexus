# SPDX-License-Identifier: AGPL-3.0-or-later
"""RDR-244 Track A scorer (nexus-2k66t.2; residuals .4 and .5): Test Plan scenarios."""
from __future__ import annotations

import random as _r
import math

import pytest

from scripts.bench.coir_score import (
    EmptyKeyError,
    Row,
    bootstrap_interval,
    collapse,
    collapse_qrels,
    mrr,
    percentile,
    ndcg_at_k,
    rank_documents,
    recall_at_k,
    score_query,
)


def _keys(table: dict[str, list[str]]):
    return lambda chash: table.get(chash, [])


def test_hand_computed_three_queries() -> None:
    """Test Plan: NDCG@10, recall@10 and MRR match hand values exactly."""
    # q1: relevant at rank 1. q2: relevant at rank 3. q3: relevant absent.
    q1 = score_query(["d:a", "d:b"], {"d:a": 1})
    q2 = score_query(["d:x", "d:y", "d:b", "d:z"], {"d:b": 1})
    q3 = score_query(["d:x", "d:y"], {"d:q": 1})
    assert (q1.ndcg, q1.recall, q1.rr) == (1.0, 1.0, 1.0)
    assert q2.ndcg == pytest.approx(1 / math.log2(4))  # 0.5
    assert (q2.recall, q2.rr) == (1.0, pytest.approx(1 / 3))
    assert (q3.ndcg, q3.recall, q3.rr) == (0.0, 0.0, 0.0)
    assert sum(q.ndcg for q in (q1, q2, q3)) / 3 == pytest.approx(0.5)
    assert sum(q.rr for q in (q1, q2, q3)) / 3 == pytest.approx(4 / 9)


def test_graded_ndcg_uses_exponential_gain() -> None:
    assert ndcg_at_k(["a", "b"], {"b": 2, "a": 1}, k=10) == pytest.approx(
        (1 + 3 / math.log2(3)) / (3 + 1 / math.log2(3)))


def test_k10_dedupe_thirty_chunks_four_documents() -> None:
    """Test Plan: 30 chunks from 4 documents score as four documents, not padded."""
    rows = [Row(chash=f"c{i}", distance=i / 100) for i in range(30)]
    owner = {f"c{i}": [f"d:{i % 4}"] for i in range(30)}
    ranked = rank_documents(rows, _keys(owner))
    assert ranked.keys == ["d:0", "d:1", "d:2", "d:3"]
    assert ranked.short_by == 6


def test_empty_key_row_fails_and_counts() -> None:
    """Test Plan: a row with no key fails the run and reports the count; no bucket."""
    rows = [Row("c1", 0.1), Row("c2", 0.2), Row("c3", 0.3)]
    with pytest.raises(EmptyKeyError) as exc:
        rank_documents(rows, _keys({"c1": ["d:1"]}))
    assert exc.value.chashes == ["c2", "c3"]
    assert "2 result rows" in str(exc.value)


def test_out_of_collection_keys_are_empty(  ) -> None:
    """Residual .5: a row whose only documents are in another collection is the empty-key error."""
    rows = [Row("c1", 0.1)]
    keys = lambda c: []  # the caller's filter dropped the other collection's document  # noqa: E731
    with pytest.raises(EmptyKeyError):
        rank_documents(rows, keys)


def test_shared_row_credits_every_document_in_key_order() -> None:
    """Test Plan + residual .4: one shared row credits both documents, sorted, consecutive ranks."""
    rows = [Row("shared", 0.1), Row("c2", 0.2)]
    ranked = rank_documents(rows, _keys({"shared": ["d:z", "d:a"], "c2": ["d:a", "d:m"]}))
    assert ranked.keys == ["d:a", "d:z", "d:m"]  # c2's d:a is already ranked, so only d:m is new
    assert ranked.shared_rows == 1  # only the first row credited more than one new document


def test_group_straddling_the_cut_is_cut_in_key_order() -> None:
    rows = [Row(f"c{i}", i / 10) for i in range(9)] + [Row("shared", 1.0)]
    owner = {f"c{i}": [f"d:{i}"] for i in range(9)} | {"shared": ["d:y", "d:x", "d:w"]}
    ranked = rank_documents(rows, _keys(owner))
    assert len(ranked.keys) == 10
    assert ranked.keys[-1] == "d:w"


def test_equal_distance_ties_break_by_key() -> None:
    rows = [Row("cb", 0.5), Row("ca", 0.5)]
    ranked = rank_documents(rows, _keys({"cb": ["d:b"], "ca": ["d:a"]}))
    assert ranked.keys == ["d:a", "d:b"]


def test_returned_order_is_kept_over_distance() -> None:
    """nx search's own order (boosts, diversity cap) is the system under test; it is not re-sorted."""
    rows = [Row("c2", 0.3), Row("c1", 0.1), Row("c3", 0.3), Row("c0", 0.3)]
    owner = {"c0": ["d:0"], "c1": ["d:1"], "c2": ["d:2"], "c3": ["d:3"]}
    assert rank_documents(rows, _keys(owner)).keys == ["d:2", "d:1", "d:0", "d:3"]


def test_recall_and_mrr_cut_at_ten() -> None:
    keys = [f"d:{i}" for i in range(12)]
    assert recall_at_k(keys, {"d:11": 1}) == 0.0
    assert mrr(keys, {"d:11": 1}) == 0.0
    assert mrr(keys, {"d:9": 1}) == pytest.approx(0.1)


def test_bootstrap_interval_is_seeded_and_brackets_the_mean() -> None:
    vals = [0.0, 1.0, 0.5, 1.0, 0.0, 1.0, 1.0, 0.25]
    lo, hi = bootstrap_interval(vals, resamples=2000, seed=2026)
    assert (lo, hi) == bootstrap_interval(vals, resamples=2000, seed=2026)
    assert lo <= sum(vals) / len(vals) <= hi
    assert bootstrap_interval([0.7] * 5, resamples=100, seed=1) == (0.7, 0.7)


def test_duplicate_copies_rank_and_judge_as_one_class() -> None:
    """ask rdr244-duplicate-text: a retrieved twin of the relevant document is a hit at its own rank,
    independent of id order, and the twins fill one rank, not two."""
    cls = {"d:b": "d:a", "d:c": "d:a"}.get
    class_of = lambda k: cls(k) or k  # noqa: E731
    rows = [Row("x", 0.1), Row("twin", 0.2), Row("y", 0.3)]
    keys = collapse(_keys({"x": ["d:x"], "twin": ["d:c", "d:b"], "y": ["d:y"]}), class_of)
    ranked = rank_documents(rows, keys)
    assert ranked.keys == ["d:x", "d:a", "d:y"] and ranked.shared_rows == 0
    qrels = collapse_qrels({"d:b": 1}, class_of)
    assert qrels == {"d:a": 1}
    assert score_query(ranked.keys, qrels).rr == pytest.approx(0.5)


def test_collapse_qrels_keeps_the_highest_grade() -> None:
    same = lambda k: "d:a" if k in ("d:a", "d:b") else k  # noqa: E731
    assert collapse_qrels({"d:a": 2, "d:b": 1, "d:q": 1}, same) == {"d:a": 2, "d:q": 1}
    assert collapse_qrels({"d:b": 1, "d:a": 2}, same) == {"d:a": 2}


def test_percentile_is_nearest_rank_and_bootstraps() -> None:
    vals = [float(i) for i in range(1, 21)]
    assert (percentile(vals, 50), percentile(vals, 95), percentile([3.0], 95)) == (10.0, 19.0, 3.0)
    lo, hi = bootstrap_interval(vals, resamples=500, seed=2026, stat=lambda v: percentile(v, 95))
    assert lo <= 19.0 <= hi


def test_shared_rows_count_only_credit_inside_the_cut() -> None:
    rows = [Row(f"c{i}", i / 10) for i in range(10)] + [Row("late", 2.0)]
    owner = {f"c{i}": [f"d:{i}"] for i in range(10)} | {"late": ["d:x", "d:y"]}
    assert rank_documents(rows, _keys(owner)).shared_rows == 0


def test_bootstrap_endpoints_are_nearest_rank_of_the_resampled_means() -> None:
    """Pin the quantile, not just the bracket, at the run's own B, where 0.025 * B is not exact in floats."""
    src = _r.Random(1)
    vals = [src.random() for _ in range(40)]
    n, b = len(vals), 10_000
    rng = _r.Random(9)
    means = sorted(sum(vals[rng.randrange(n)] for _ in range(n)) / n for _ in range(b))
    assert means[248] != means[249] != means[250] and means[9748] != means[9749] != means[9750]
    assert bootstrap_interval(vals, resamples=b, seed=9) == (means[249], means[9749])
