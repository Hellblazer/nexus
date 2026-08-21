# SPDX-License-Identifier: AGPL-3.0-or-later
"""Unit tests for the RDR-196 .p2a operator quality proxy metrics
(nexus-nyry9.14). Pure functions, no dispatch, no network.

Covers the two non-vacuity assertions the bead's VERIFICATION demands:
the metric functions themselves score known-agreeing input as agreeing
and known-disagreeing input as disagreeing (the "the metric or the
threshold is wrong" failure mode from the bead description, caught here
at the unit level rather than only via a live control).
"""
from __future__ import annotations

from bench.operator_proxy_metrics import (
    OUT_OF_PROXY_SCOPE,
    THRESHOLDS,
    bool_plus_jaccard,
    citation_tokens,
    exact_membership_jaccard,
    field_value_bag_f1,
    rand_index,
    spearman_rank_correlation,
)

# The 10 real MCP operator names, read directly off src/nexus/mcp/core.py's
# @mcp.tool registrations (same list tests/test_operator_model_tiers.py
# verifies against) -- a real non-vacuity check, not a tautology against
# whatever this module happens to contain.
_REAL_OPERATOR_NAMES = frozenset({
    "operator_extract", "operator_rank", "operator_compare",
    "operator_summarize", "operator_generate", "operator_filter",
    "operator_check", "operator_verify", "operator_groupby",
    "operator_aggregate",
})


class TestScopeNonVacuity:
    def test_thresholds_and_out_of_scope_partition_all_ten_operators(self) -> None:
        covered = set(THRESHOLDS) | set(OUT_OF_PROXY_SCOPE)
        assert covered == _REAL_OPERATOR_NAMES, (
            f"missing: {_REAL_OPERATOR_NAMES - covered}, "
            f"extra: {covered - _REAL_OPERATOR_NAMES}"
        )

    def test_thresholds_and_out_of_scope_are_disjoint(self) -> None:
        assert set(THRESHOLDS) & OUT_OF_PROXY_SCOPE == set()

    def test_six_operators_get_a_metric(self) -> None:
        assert set(THRESHOLDS) == {
            "operator_filter", "operator_groupby", "operator_rank",
            "operator_extract", "operator_check", "operator_verify",
        }

    def test_four_operators_are_explicitly_out(self) -> None:
        assert OUT_OF_PROXY_SCOPE == {
            "operator_summarize", "operator_aggregate",
            "operator_compare", "operator_generate",
        }

    def test_all_thresholds_are_in_unit_interval(self) -> None:
        for name, threshold in THRESHOLDS.items():
            assert 0.0 < threshold <= 1.0, f"{name} threshold {threshold} out of range"


class TestExactMembershipJaccard:
    def test_identical_sets_score_1(self) -> None:
        assert exact_membership_jaccard({"a", "b"}, {"a", "b"}) == 1.0

    def test_disjoint_sets_score_0(self) -> None:
        assert exact_membership_jaccard({"a"}, {"b"}) == 0.0

    def test_partial_overlap(self) -> None:
        # intersection {b} = 1, union {a,b,c} = 3 -> 1/3
        assert exact_membership_jaccard({"a", "b"}, {"b", "c"}) == 1 / 3

    def test_both_empty_scores_1(self) -> None:
        assert exact_membership_jaccard(set(), set()) == 1.0

    def test_one_empty_scores_0(self) -> None:
        assert exact_membership_jaccard({"a"}, set()) == 0.0


class TestSpearmanRankCorrelation:
    def test_identical_order_scores_1(self) -> None:
        order = ["[a] x", "[b] y", "[c] z"]
        assert spearman_rank_correlation(order, order) == 1.0

    def test_reversed_order_scores_minus_1(self) -> None:
        order = ["[a] x", "[b] y", "[c] z", "[d] w"]
        reversed_order = list(reversed(order))
        result = spearman_rank_correlation(order, reversed_order)
        assert result is not None
        assert abs(result - (-1.0)) < 1e-9

    def test_partial_permutation_between_bounds(self) -> None:
        a = ["[a] x", "[b] y", "[c] z", "[d] w"]
        b = ["[a] x", "[c] z", "[b] y", "[d] w"]  # one adjacent swap
        result = spearman_rank_correlation(a, b)
        assert result is not None
        assert -1.0 < result < 1.0

    def test_fewer_than_two_common_ids_returns_none(self) -> None:
        assert spearman_rank_correlation(["[a] x"], ["[b] y"]) is None
        assert spearman_rank_correlation([], []) is None

    def test_untagged_items_are_ignored_not_crashed_on(self) -> None:
        a = ["[a] x", "no tag here", "[c] z"]
        b = ["[a] x", "[c] z"]
        result = spearman_rank_correlation(a, b)
        assert result == 1.0  # both common ids (a, c) in the same relative order


class TestFieldValueBagF1:
    def test_identical_extractions_score_1(self) -> None:
        recs = [{"id": "a", "year": 2020}, {"id": "b", "year": 2021}]
        assert field_value_bag_f1(recs, recs) == 1.0

    def test_disjoint_extractions_score_0(self) -> None:
        a = [{"id": "a"}]
        b = [{"id": "b"}]
        assert field_value_bag_f1(a, b) == 0.0

    def test_partial_overlap_between_bounds(self) -> None:
        a = [{"id": "a", "year": 2020, "category": "x"}]
        b = [{"id": "a", "year": 2020, "category": "y"}]
        result = field_value_bag_f1(a, b)
        assert 0.0 < result < 1.0

    def test_both_empty_scores_1(self) -> None:
        assert field_value_bag_f1([], []) == 1.0

    def test_one_empty_scores_0(self) -> None:
        assert field_value_bag_f1([{"id": "a"}], []) == 0.0

    def test_case_and_whitespace_normalized(self) -> None:
        a = [{"category": " Hooks "}]
        b = [{"category": "hooks"}]
        assert field_value_bag_f1(a, b) == 1.0


class TestRandIndex:
    def test_identical_clustering_scores_1(self) -> None:
        a = {"1": "g1", "2": "g1", "3": "g2"}
        b = {"1": "gX", "2": "gX", "3": "gY"}  # different label strings, same partition
        assert rand_index(a, b) == 1.0

    def test_fully_inverted_clustering_scores_low(self) -> None:
        # true: two groups of 2 (4 items) -> merging into one group
        # disagrees on every cross-group pair.
        true_assign = {"1": "g1", "2": "g1", "3": "g2", "4": "g2"}
        merged_assign = {"1": "all", "2": "all", "3": "all", "4": "all"}
        result = rand_index(true_assign, merged_assign)
        # pairs: (1,2) same/same agree; (3,4) same/same agree;
        # (1,3)(1,4)(2,3)(2,4) diff/same disagree -> 2 agree / 6 total
        assert result == 2 / 6

    def test_fewer_than_two_common_ids_scores_1(self) -> None:
        assert rand_index({"1": "g1"}, {"1": "g2"}) == 1.0
        assert rand_index({}, {}) == 1.0


class TestCitationTokens:
    """2026-08-21 revision (see T2 nexus_rdr/196-phase2-quality-proxy
    ## Revision): token-level Jaccard on citations, forgiving of
    paraphrasing that broke the original exact-string design."""

    def test_paraphrased_citations_still_overlap(self) -> None:
        a = citation_tokens(["rdr-086 chash section"])
        b = citation_tokens(["the chash index paragraph in rdr-086"])
        # shares "rdr-086" and "chash" tokens -> nonzero overlap
        assert a & b == {"rdr-086", "chash"}

    def test_unrelated_citations_share_nothing(self) -> None:
        a = citation_tokens(["rdr-086 chash section"])
        b = citation_tokens(["taxonomy clustering discussion"])
        assert a & b == set()

    def test_empty_list_yields_empty_set(self) -> None:
        assert citation_tokens([]) == set()

    def test_non_string_entries_ignored(self) -> None:
        assert citation_tokens([123, None, "rdr-086"]) == {"rdr-086"}

    def test_case_insensitive(self) -> None:
        assert citation_tokens(["RDR-086 Chash"]) == citation_tokens(["rdr-086 chash"])

    def test_stopwords_are_dropped(self) -> None:
        tokens = citation_tokens(["the chash section of the rdr-086 index"])
        assert "the" not in tokens
        assert "of" not in tokens
        assert tokens == {"chash", "section", "rdr-086", "index"}

    def test_stopword_only_overlap_scores_no_agreement(self) -> None:
        """Code-review finding (2026-08-21): two citations about UNRELATED
        passages sharing only filler words must NOT register partial
        agreement -- this is the gap the stopword filter closes."""
        a = citation_tokens(["the chash section of rdr-086"])
        b = citation_tokens(["the taxonomy clustering of rdr-070"])
        assert a & b == set(), (
            f"unrelated citations shared tokens after stopword filtering: {a & b}"
        )


class TestBoolPlusJaccard:
    def test_full_agreement_scores_1(self) -> None:
        assert bool_plus_jaccard(True, True, {"a"}, {"a"}) == 1.0

    def test_bool_disagreement_caps_below_half_weight_complement(self) -> None:
        # bool mismatch -> bool term 0; even perfect set overlap can only
        # reach (1 - bool_weight) = 0.5 with the default weight.
        result = bool_plus_jaccard(True, False, {"a"}, {"a"}, bool_weight=0.5)
        assert result == 0.5

    def test_bool_agreement_set_disagreement(self) -> None:
        result = bool_plus_jaccard(True, True, {"a"}, {"b"}, bool_weight=0.5)
        assert result == 0.5

    def test_total_disagreement_scores_0(self) -> None:
        assert bool_plus_jaccard(True, False, {"a"}, {"b"}, bool_weight=0.5) == 0.0

    def test_threshold_unreachable_on_bool_mismatch(self) -> None:
        """The composite-metric design claim from T2: a bool mismatch caps
        the score at 0.5, which is below both check's and verify's 0.70
        threshold regardless of evidence/citation overlap."""
        capped = bool_plus_jaccard(True, False, {"a", "b"}, {"a", "b"}, bool_weight=0.5)
        assert capped < THRESHOLDS["operator_check"]
        assert capped < THRESHOLDS["operator_verify"]
