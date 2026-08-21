# SPDX-License-Identifier: AGPL-3.0-or-later
"""TDD tests for scripts/bench/operator_proxy_ab.py (RDR-196 .p2c A/B,
nexus-nyry9.16 round-2 review fix, T2 [23144] Critical #3).

Fixtures below are trimmed, faithful reproductions of the THREE real
shapes seen in the actual nyry9_16_ab_results.json artifact: a planner
run whose downstream work fused into a bundle (ambient_usage fully
captures both dispatches), a planner run whose downstream work was an
ISOLATED step (ambient_usage silently drops the second dispatch — the
bug this module fixes), and a plain operator-tiering run (no planner
dispatch at all).
"""
from __future__ import annotations

import sys as _sys
from pathlib import Path as _Path

_BENCH_PARENT = _Path(__file__).resolve().parent.parent / "scripts"
if str(_BENCH_PARENT) not in _sys.path:
    _sys.path.insert(0, str(_BENCH_PARENT))

from bench.operator_proxy_ab import stitch_run_id, total_dispatch_cost, total_dispatch_count


# ── Real-shaped fixtures (trimmed from nyry9_16_ab_results.json) ───────────

# Planner baseline run_index=0: downstream fused into a bundle. Both
# dispatches reach the outer ambient sink (bundle dispatch uses an
# explicit usage_sink kwarg, additive with the ambient one).
PLANNER_BUNDLE_DOWNSTREAM = {
    "cost_usd": 2.0503460000000002,  # result["cost_usd"]: sums the ONE bundle StepRecord
    "steps": [
        {"operator": "search", "model": None, "cost_usd": 0.0},
        {"operator": "store_get_many", "model": None, "cost_usd": 0.0},
        {"operator": "extract+summarize", "model": "claude-fable-5", "cost_usd": 2.0503460000000002},
    ],
    "ambient_usage": [
        {"model": "claude-fable-5", "cost_usd": 1.50259},          # planner's own dispatch
        {"model": "claude-fable-5", "cost_usd": 2.0503460000000002},  # the bundle dispatch (also captured)
    ],
}

# Planner baseline run_index=1: downstream is an ISOLATED "summarize"
# step. The runner's OWN ambient_usage_sink(...) around that isolated
# dispatch SHADOWS the harness's outer sink, so ambient_usage never
# learns about it — only the planner's own dispatch shows up there.
PLANNER_ISOLATED_DOWNSTREAM = {
    "cost_usd": 1.1357760000000001,  # result["cost_usd"]: the isolated summarize StepRecord, correct
    "steps": [
        {"operator": "search", "model": None, "cost_usd": 0.0},
        {"operator": "store_get_many", "model": None, "cost_usd": 0.0},
        {"operator": "summarize", "model": "claude-fable-5", "cost_usd": 1.1357760000000001},
    ],
    "ambient_usage": [
        {"model": "claude-fable-5", "cost_usd": 1.1360059999999998},  # ONLY the planner's own dispatch
    ],
}

# A plain operator-tiering run (not force_dynamic) — no separate planner
# dispatch exists at all; result["cost_usd"] alone is the whole story.
OPERATOR_ROW = {
    "cost_usd": 0.097308,
    "steps": [
        {"operator": "filter", "model": "claude-fable-5", "cost_usd": 0.097308},
    ],
    "ambient_usage": [],
}


class TestTotalDispatchCost:
    def test_bundle_downstream_matches_naive_ambient_sum(self) -> None:
        """For the bundle case the naive sum(ambient) happened to be
        right — the fix must not change this answer."""
        total = total_dispatch_cost(PLANNER_BUNDLE_DOWNSTREAM, is_planner_row=True)
        assert total == 1.50259 + 2.0503460000000002

    def test_isolated_downstream_recovers_the_dropped_dispatch(self) -> None:
        """This is the regression this module exists to fix: the naive
        sum(ambient) for this fixture is 1.1360059999999998 (WRONG — the
        code-review-flagged bug); the correct total adds the downstream
        StepRecord's cost that the ambient sink silently dropped."""
        total = total_dispatch_cost(PLANNER_ISOLATED_DOWNSTREAM, is_planner_row=True)
        naive_wrong_sum = sum(
            u["cost_usd"] for u in PLANNER_ISOLATED_DOWNSTREAM["ambient_usage"]
        )
        assert naive_wrong_sum == 1.1360059999999998  # the bug, pinned
        assert total != naive_wrong_sum
        assert total == 1.1360059999999998 + 1.1357760000000001

    def test_operator_row_uses_cost_usd_directly(self) -> None:
        total = total_dispatch_cost(OPERATOR_ROW, is_planner_row=False)
        assert total == 0.097308

    def test_planner_row_with_no_ambient_usage_falls_back_to_step_cost(self) -> None:
        record = {"cost_usd": 0.5, "steps": [], "ambient_usage": []}
        assert total_dispatch_cost(record, is_planner_row=True) == 0.5

    def test_all_unknown_returns_none_not_fabricated_zero(self) -> None:
        record = {"cost_usd": None, "steps": [], "ambient_usage": []}
        assert total_dispatch_cost(record, is_planner_row=True) is None

    def test_recomputed_planner_baseline_mean_matches_corrected_figure(self) -> None:
        """End-to-end regression pin: the corrected 3-run planner
        baseline mean is ~$2.86, not the ~$2.48 the buggy naive-sum
        script originally reported (code-review T2 [23144] Critical #3)."""
        run0 = PLANNER_BUNDLE_DOWNSTREAM
        run1 = PLANNER_ISOLATED_DOWNSTREAM
        run2 = {
            "cost_usd": 1.6236739999999998,
            "steps": [
                {"operator": "search", "model": None, "cost_usd": 0.0},
                {"operator": "store_get_many", "model": None, "cost_usd": 0.0},
                {"operator": "extract+summarize+generate", "model": "claude-fable-5", "cost_usd": 1.6236739999999998},
            ],
            "ambient_usage": [
                {"model": "claude-fable-5", "cost_usd": 1.137006},
                {"model": "claude-fable-5", "cost_usd": 1.6236739999999998},
            ],
        }
        totals = [
            total_dispatch_cost(r, is_planner_row=True) for r in (run0, run1, run2)
        ]
        mean = sum(totals) / 3
        assert round(mean, 2) == 2.86


class TestStitchRunId:
    """Round-2 review gap (T2 [23144] Significant #4): same-question
    dispatches across arms/indices need a run_id resolved by process
    order, since the question text alone repeats."""

    def test_single_new_row_matching_question_is_returned(self) -> None:
        before = {1, 2, 3}
        after = [
            {"id": 4, "question": "extract operator run"},
            {"id": 3, "question": "extract operator run"},
            {"id": 2, "question": "some other question"},
        ]
        assert stitch_run_id(before, after, "extract operator run") == 4

    def test_zero_new_rows_returns_none_not_a_guess(self) -> None:
        before = {1, 2, 3}
        after = [{"id": 3, "question": "extract operator run"}]
        assert stitch_run_id(before, after, "extract operator run") is None

    def test_multiple_new_rows_same_question_is_ambiguous_returns_none(self) -> None:
        """Two same-question dispatches racing in the same window (should
        not happen in the harness's strictly-sequential await loop, but
        the function must not silently pick one)."""
        before = {1}
        after = [
            {"id": 3, "question": "extract operator run"},
            {"id": 2, "question": "extract operator run"},
        ]
        assert stitch_run_id(before, after, "extract operator run") is None

    def test_new_row_with_different_question_is_not_a_candidate(self) -> None:
        before = {1}
        after = [
            {"id": 2, "question": "unrelated question"},
        ]
        assert stitch_run_id(before, after, "extract operator run") is None


class TestTotalDispatchCount:
    def test_operator_row_counts_one(self) -> None:
        assert total_dispatch_count(OPERATOR_ROW, is_planner_row=False) == 1

    def test_planner_bundle_row_counts_planner_plus_one_bundled_step(self) -> None:
        # 2 real dispatches: the planner's own, and the ONE fused bundle
        # dispatch (bundle StepRecord still carries a single non-None
        # model — one claude -p call regardless of how many operators
        # fused into it).
        assert total_dispatch_count(PLANNER_BUNDLE_DOWNSTREAM, is_planner_row=True) == 2

    def test_sql_only_steps_contribute_zero(self) -> None:
        record = {
            "steps": [
                {"operator": "search", "model": None, "cost_usd": 0.0},
                {"operator": "store_get_many", "model": None, "cost_usd": 0.0},
            ],
            "ambient_usage": [],
        }
        assert total_dispatch_count(record, is_planner_row=False) == 0
