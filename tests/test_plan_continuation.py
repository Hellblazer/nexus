# SPDX-License-Identifier: AGPL-3.0-or-later
"""Unit tests for the continuation cut classifier (RDR-200 Phase 1a,
nexus-4e75w.3).

Table-driven over the RDR's own test plan: trailing operators (Shape A),
interleaved retrieval-after-operator, no operator suffix, single operator
(Shape B), empty plan, and the multi-segment/MULTI_UNIT case with its
reachability proof (bundling disabled — see
``nexus.plans.continuation.classify_continuation_cut``'s docstring for
why MULTI_UNIT is unreachable under the production bundling gate and
reachable only when it is closed).

Pure-Python, deterministic, no network, no ``claude -p`` dispatch.
"""
from __future__ import annotations

import pytest

from nexus.plans.continuation import (
    ContinuationCut,
    CutShape,
    classify_continuation_cut,
)


def _step(tool: str, **args) -> dict:
    return {"tool": tool, "args": args}


# ── Table-driven: shape + cut point per plan ────────────────────────────────


class TestClassifyContinuationCut:

    def test_two_plus_trailing_operators_is_shape_a(self):
        """search -> search -> extract -> generate: suffix [extract,
        generate] resolves to ONE OperatorBundleSlice (RDR worked case)."""
        steps = [
            _step("search", query="a"),
            _step("search", query="b"),
            _step("extract", inputs="$step2.ids", fields="title"),
            _step("generate", template="report", context="$step3.extractions"),
        ]
        cut = classify_continuation_cut(steps)
        assert cut == ContinuationCut(
            shape=CutShape.SHAPE_A,
            cut_at_step=2,
            plan_indices=(2, 3),
            operators=("extract", "generate"),
        )

    def test_interleaved_retrieval_after_operator_cuts_at_terminal_only(self):
        """search -> extract -> search($step2.ids) -> generate: the
        mid-plan extract runs server-side (its output feeds the second
        retrieval); only the trailing [generate] is the suffix (RDR
        worked case, finding F2)."""
        steps = [
            _step("search", query="a"),
            _step("extract", inputs="$step1.ids", fields="title"),
            _step("search", query="$step2.extractions"),
            _step("generate", template="report", context="$step3.ids"),
        ]
        cut = classify_continuation_cut(steps)
        assert cut == ContinuationCut(
            shape=CutShape.SHAPE_B,
            cut_at_step=3,
            plan_indices=(3,),
            operators=("generate",),
        )

    def test_no_operator_suffix_is_no_suffix(self):
        """A plan ending in a retrieval step has nothing to continue —
        today's behaviour, single_query fast path included (that path
        never reaches the classifier at all)."""
        steps = [
            _step("extract", inputs="$intent", fields="title"),
            _step("search", query="$step1.extractions"),
        ]
        cut = classify_continuation_cut(steps)
        assert cut == ContinuationCut(
            shape=CutShape.NO_SUFFIX,
            cut_at_step=None,
            plan_indices=(),
            operators=(),
        )

    def test_single_operator_is_shape_b(self):
        """A plan ending in exactly one operator — the likely-most-common
        continuation shape (RDR audit round 1 amendment)."""
        steps = [
            _step("search", query="a"),
            _step("summarize", content="$step1.ids"),
        ]
        cut = classify_continuation_cut(steps)
        assert cut == ContinuationCut(
            shape=CutShape.SHAPE_B,
            cut_at_step=1,
            plan_indices=(1,),
            operators=("summarize",),
        )

    def test_empty_plan_is_no_suffix(self):
        cut = classify_continuation_cut([])
        assert cut == ContinuationCut(
            shape=CutShape.NO_SUFFIX,
            cut_at_step=None,
            plan_indices=(),
            operators=(),
        )

    def test_whole_plan_all_operators_is_shape_a(self):
        """No retrieval prefix at all — every step is an operator. Still
        exactly one dispatch unit under the production bundling gate."""
        steps = [
            _step("extract", inputs="$intent", fields="title"),
            _step("summarize", content="$step1.extractions"),
        ]
        cut = classify_continuation_cut(steps)
        assert cut.shape == CutShape.SHAPE_A
        assert cut.cut_at_step == 0
        assert cut.plan_indices == (0, 1)
        assert cut.operators == ("extract", "summarize")

    def test_three_operator_trailing_run_is_still_one_shape_a_unit(self):
        """A run of 3 trailing operators is still ONE dispatch unit under
        the production bundling gate — segment_steps fuses a contiguous
        run of ANY length >= 2 into a single OperatorBundleSlice, not
        just runs of exactly 2."""
        steps = [
            _step("search", query="a"),
            _step("extract", inputs="$step1.ids", fields="title"),
            _step("rank", items="$step2.extractions", criterion="relevance"),
            _step("summarize", content="$step3.ranked"),
        ]
        cut = classify_continuation_cut(steps)
        assert cut.shape == CutShape.SHAPE_A
        assert cut.cut_at_step == 1
        assert cut.plan_indices == (1, 2, 3)
        assert cut.operators == ("extract", "rank", "summarize")

    def test_non_operator_tool_name_is_not_part_of_suffix(self):
        """An unrecognized/non-operator terminal tool never enters the
        suffix — is_operator_tool must be the sole gate, not a guess."""
        steps = [
            _step("summarize", content="$intent"),
            _step("traverse", start="x"),
        ]
        cut = classify_continuation_cut(steps)
        assert cut.shape == CutShape.NO_SUFFIX

    def test_bare_and_operator_prefixed_tool_names_both_recognized(self):
        """Plan YAMLs use either the bare verb or the operator_-prefixed
        form (bundle.is_operator_tool accepts both) — the classifier
        must not silently miss one spelling."""
        steps = [_step("search", query="a"), _step("operator_generate", template="t", context="$step1.ids")]
        cut = classify_continuation_cut(steps)
        assert cut.shape == CutShape.SHAPE_B
        assert cut.operators == ("generate",)  # _bare strips the prefix

    def test_mcp_prefixed_tool_name_recognized(self):
        """``mcp__...__operator_extract`` style names (as they appear on
        the wire) resolve the same as the bare form."""
        steps = [
            _step("search", query="a"),
            _step("mcp__plugin_conexus_nexus__operator_extract",
                  inputs="$step1.ids", fields="title"),
        ]
        cut = classify_continuation_cut(steps)
        assert cut.shape == CutShape.SHAPE_B
        assert cut.operators == ("extract",)


# ── MULTI_UNIT reachability proof ───────────────────────────────────────────


class TestMultiUnitReachability:
    """MULTI_UNIT is UNREACHABLE for a pure trailing operator suffix under
    the production bundling gate (bundle_operators=True and a bundling-
    capable dispatcher) — segment_steps fuses every contiguous run of
    >=2 operators into exactly one OperatorBundleSlice, so the trailing
    walk in classify_continuation_cut can never see more than one
    operator-composed segment when that gate is open. It is reachable
    through exactly one real path: the bundling gate closed, which
    flattens a >=2-operator run into N separate per-step dispatches.
    This is a real, non-vacuous construction (nexus-moht0 doctrine),
    proved by actually driving classify_continuation_cut through
    bundle_operators=False — not asserted."""

    def test_bundling_disabled_makes_trailing_run_multi_unit(self):
        steps = [
            _step("search", query="a"),
            _step("extract", inputs="$step1.ids", fields="title"),
            _step("generate", template="report", context="$step2.extractions"),
        ]
        cut = classify_continuation_cut(steps, bundle_operators=False)
        assert cut.shape == CutShape.MULTI_UNIT
        assert cut.plan_indices == (1, 2)
        assert cut.operators == ("extract", "generate")

    def test_dispatcher_without_supports_bundling_also_multi_units(self):
        """The gate is bundle_operators AND supports_bundling — either
        being closed produces the same flattened, multi-unit shape."""
        steps = [
            _step("extract", inputs="$intent", fields="title"),
            _step("summarize", content="$step1.extractions"),
        ]
        cut = classify_continuation_cut(
            steps, bundle_operators=True, supports_bundling=False,
        )
        assert cut.shape == CutShape.MULTI_UNIT

    def test_bundling_disabled_single_operator_stays_shape_b(self):
        """A lone trailing operator is NEVER part of an OperatorBundleSlice
        (segment_steps only fuses runs of length >= 2) — the bundling
        gate has no effect on it. This confirms Shape B's fidelity
        reference (the standalone operator_* tool / Phase 0 builder) is
        unconditional, not contingent on the bundling gate being open."""
        steps = [
            _step("search", query="a"),
            _step("generate", template="t", context="$step1.ids"),
        ]
        cut_bundled = classify_continuation_cut(steps, bundle_operators=True)
        cut_unbundled = classify_continuation_cut(steps, bundle_operators=False)
        assert cut_bundled.shape == CutShape.SHAPE_B
        assert cut_unbundled.shape == CutShape.SHAPE_B
        assert cut_bundled == cut_unbundled

    def test_production_default_never_produces_multi_unit_for_any_trailing_run(self):
        """Sweep run lengths 1..6 under the PRODUCTION default gate
        (bundle_operators=True, supports_bundling=True, both defaults) —
        none resolve to MULTI_UNIT. This is the reachability proof's
        converse: exhaustive over the shapes that actually occur in
        nx_answer's real call site."""
        for run_length in range(1, 7):
            steps = [_step("search", query="a")] + [
                _step("extract", inputs=f"$stepN.ids", fields="title")
                for _ in range(run_length)
            ]
            cut = classify_continuation_cut(steps)
            assert cut.shape != CutShape.MULTI_UNIT, (
                f"run_length={run_length} unexpectedly produced MULTI_UNIT "
                "under the production bundling gate"
            )


# ── Enum / dataclass surface ─────────────────────────────────────────────────


class TestContinuationCutShape:

    def test_cut_shape_values_are_stable_strings(self):
        """Logged via structlog (nx_answer's decision log) — the string
        value is the on-the-wire representation, so pin it."""
        assert CutShape.NO_SUFFIX.value == "no_suffix"
        assert CutShape.SHAPE_A.value == "shape_a"
        assert CutShape.SHAPE_B.value == "shape_b"
        assert CutShape.MULTI_UNIT.value == "multi_unit"

    def test_continuation_cut_is_frozen(self):
        cut = classify_continuation_cut([])
        with pytest.raises(AttributeError):
            cut.shape = CutShape.SHAPE_A  # type: ignore[misc]
