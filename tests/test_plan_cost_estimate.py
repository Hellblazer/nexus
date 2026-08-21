# SPDX-License-Identifier: AGPL-3.0-or-later
"""TDD tests for predicted plan-cost estimation (RDR-196 Phase 3 Step 1,
nexus-nyry9.20).

Design contract under test (see ``nexus.plans.cost_estimate`` module
docstring for the full rationale):

  * ``estimate_plan_cost`` prices a plan from its STEP SHAPE: retrieval
    tools and sql-fast-path-eligible operators (filter/groupby/aggregate)
    are $0; other operators price from ``OperatorPriceTable`` (recorded
    history when it has >= MIN_HISTORY_SAMPLES, else the single static
    fallback); a contiguous bundle of >= 2 operator steps prices as ONE
    dispatch (the most expensive non-sql-fast-path member), never N.
  * A plan with any unpriceable operator gets ``usd=None`` for the WHOLE
    plan -- never ``0``.
  * ``choose_within_band`` picks the cheapest candidate among those
    within ``PLAN_CHOICE_CONFIDENCE_BAND`` of the best raw confidence;
    outside the band, confidence wins outright; a ``None``-estimate
    candidate never wins a tie against a priced one.
  * ``get_cached_price_table``/``invalidate_price_table_cache`` cache the
    built table per process with an explicit invalidation hook.

matcher.py is untouched by this bead (RDR-100 hard fence) -- these tests
construct ``Match`` objects directly rather than routing through
``plan_match``.
"""
from __future__ import annotations

import json

import pytest

from nexus.plans.cost_estimate import (
    MIN_HISTORY_SAMPLES,
    PLAN_CHOICE_CONFIDENCE_BAND,
    STATIC_FALLBACK_COST_USD,
    OperatorPriceTable,
    build_operator_price_table,
    choose_within_band,
    estimate_plan_cost,
    get_cached_price_table,
    invalidate_price_table_cache,
)
from nexus.plans.match import Match


def _plan_json(*tools: str) -> str:
    return json.dumps({"steps": [{"tool": t, "args": {}} for t in tools]})


def _match(plan_id: int, confidence: float | None, *tools: str, name: str = "") -> Match:
    return Match(
        plan_id=plan_id,
        name=name or f"plan-{plan_id}",
        description="",
        confidence=confidence,
        dimensions={},
        tags="",
        plan_json=_plan_json(*tools),
        required_bindings=[],
        optional_bindings=[],
        default_bindings={},
        parent_dims=None,
    )


def _empty_table() -> OperatorPriceTable:
    """A price table with no recorded history -- every operator falls
    through to the static per-tier fallback."""
    return OperatorPriceTable({})


# ── estimate_plan_cost: basic shape classification ──────────────────────────


def test_retrieval_steps_cost_zero() -> None:
    est = estimate_plan_cost(_plan_json("search", "query", "traverse", "store_get_many"), _empty_table())
    assert est.usd == 0.0
    assert est.ms == 0
    assert "retrieval(zero-cost)" in est.basis


def test_sql_fast_path_eligible_operators_cost_zero_optimistically() -> None:
    est = estimate_plan_cost(_plan_json("search"), _empty_table())
    # single filter step, isolated (not bundled with a neighbor)
    est2 = estimate_plan_cost(json.dumps({"steps": [
        {"tool": "search"}, {"tool": "operator_filter"},
    ]}), _empty_table())
    assert est.usd == 0.0
    assert est2.usd == 0.0
    assert "sql-fast-path-eligible(optimistic-zero, source=auto)" in est2.basis


def test_sql_fast_path_eligible_operator_forced_llm_via_explicit_source_is_priced() -> None:
    # An explicit args.source="llm" skips the SQL path entirely -> priced
    # like any other LLM operator, not the optimistic zero.
    est_auto = estimate_plan_cost(json.dumps({"steps": [
        {"tool": "operator_filter", "args": {}},
    ]}), _empty_table())
    est_llm = estimate_plan_cost(json.dumps({"steps": [
        {"tool": "operator_filter", "args": {"source": "llm"}},
    ]}), _empty_table())
    est_aspects = estimate_plan_cost(json.dumps({"steps": [
        {"tool": "operator_groupby", "args": {"source": "aspects"}},
    ]}), _empty_table())
    assert est_auto.usd == 0.0
    assert est_aspects.usd == 0.0
    assert "source=aspects" in est_aspects.basis
    assert est_llm.usd == pytest.approx(STATIC_FALLBACK_COST_USD)
    assert "sql-fast-path-eligible-forced-llm(" in est_llm.basis


def test_llm_operator_with_no_history_uses_static_fallback() -> None:
    est_generate = estimate_plan_cost(_plan_json("search", "operator_generate"), _empty_table())
    est_rank = estimate_plan_cost(_plan_json("search", "operator_rank"), _empty_table())
    assert est_generate.usd == pytest.approx(STATIC_FALLBACK_COST_USD)
    assert est_rank.usd == pytest.approx(STATIC_FALLBACK_COST_USD)
    assert "static-fallback(thin-history)" in est_generate.basis
    assert "static-fallback(thin-history)" in est_rank.basis


def test_unknown_operator_makes_whole_plan_unpriceable_not_zero() -> None:
    est = estimate_plan_cost(_plan_json("search", "totally_unknown_tool_xyz"), _empty_table())
    assert est.usd is None
    assert est.ms is None
    assert "unpriceable" in est.basis


def test_malformed_plan_json_is_unpriceable() -> None:
    est = estimate_plan_cost("{not json", _empty_table())
    assert est.usd is None
    assert "malformed-plan-json" in est.basis


def test_no_steps_is_unpriceable() -> None:
    est = estimate_plan_cost(json.dumps({"steps": []}), _empty_table())
    assert est.usd is None
    assert est.basis == "no-steps"


# ── estimate_plan_cost: bundling ─────────────────────────────────────────────


def test_bundled_operators_price_as_one_dispatch_not_n() -> None:
    # rank + summarize are adjacent operator steps -> ONE bundled
    # dispatch. Cost must equal ONE static-fallback dispatch, not two
    # (both members share the same static fallback price with no
    # per-operator history, so equality here specifically proves it is
    # NOT summing two dispatches -- see the history-backed variant below
    # for a bundle test that can't be satisfied by accidental doubling).
    bundled = estimate_plan_cost(
        _plan_json("search", "operator_rank", "operator_summarize"), _empty_table(),
    )
    single = estimate_plan_cost(_plan_json("search", "operator_rank"), _empty_table())
    assert bundled.usd == pytest.approx(single.usd)
    assert bundled.usd == pytest.approx(STATIC_FALLBACK_COST_USD)
    assert "bundle[" in bundled.basis


def test_bundle_prices_at_the_most_expensive_member() -> None:
    # operator_rank has real recorded history (cheap, $0.05); operator_generate
    # has none and falls to the static fallback ($0.23, more expensive).
    # Bundled together, the bundle must price at the MORE expensive member
    # (a fused prompt must satisfy the strongest step), not at the cheaper
    # history-backed one and not at their sum.
    rows = [
        {"steps": [
            {"operator": "operator_rank", "source": "llm", "model": "m",
             "cost_usd": 0.05, "elapsed_ms": 500, "ok": True}
        ]}
        for _ in range(MIN_HISTORY_SAMPLES)
    ]
    table = build_operator_price_table(_fake_store(rows))
    est = estimate_plan_cost(
        _plan_json("search", "operator_rank", "operator_generate"), table,
    )
    assert est.usd == pytest.approx(STATIC_FALLBACK_COST_USD)
    assert est.usd != pytest.approx(0.05)
    assert est.usd != pytest.approx(0.05 + STATIC_FALLBACK_COST_USD)


def test_bundle_of_all_sql_fast_path_eligible_members_is_zero() -> None:
    est = estimate_plan_cost(
        json.dumps({"steps": [
            {"tool": "operator_filter"}, {"tool": "operator_groupby"},
        ]}), _empty_table(),
    )
    assert est.usd == 0.0
    assert "bundle-all-sql-fast-path-eligible" in est.basis


def test_isolated_unknown_operator_makes_plan_unpriceable_even_next_to_known_operator() -> None:
    # totally_unknown_tool_xyz is not `is_operator_tool` -> segment_steps
    # does NOT bundle it with the adjacent operator_rank (only recognized
    # operator tools fuse); it flushes as its own IsolatedStep. This
    # exercises the isolated-unpriceable path with a real operator
    # neighbor present, asserting the whole-plan-None rule holds
    # regardless of what else is in the plan.
    est = estimate_plan_cost(
        json.dumps({"steps": [
            {"tool": "operator_rank"}, {"tool": "totally_unknown_tool_xyz"},
        ]}), _empty_table(),
    )
    assert est.usd is None


def test_price_bundle_helper_reports_unpriceable_member() -> None:
    # OperatorPriceTable.price_for() only ever returns None for an
    # operator outside `is_operator_tool`'s known set -- estimate_plan_cost
    # can never naturally hand `_price_bundle` a bundleable-but-unpriceable
    # member via the real price table. Exercise the defensive branch
    # directly against a fake price table that reports one member as
    # unpriceable anyway.
    class _ForcedUnpriceableTable:
        def price_for(self, operator: str) -> tuple[float | None, int | None, str]:
            if operator == "operator_generate":
                return None, None, "forced-unpriceable-for-test"
            return 0.05, 900, "history(n=5, model=m)"

    from nexus.plans.cost_estimate import _price_bundle  # noqa: PLC0415 — private-symbol unit test, deliberate

    usd, ms, basis, unpriceable = _price_bundle(
        [{"tool": "operator_rank"}, {"tool": "operator_generate"}], _ForcedUnpriceableTable(),
    )
    assert usd is None
    assert ms is None
    assert unpriceable is True
    assert "unpriceable" in basis


def test_bundle_member_forced_llm_via_explicit_source_is_priced_not_zero() -> None:
    # filter (sql-fast-path-eligible, source="llm" forced) bundled with
    # rank (plain LLM operator, static fallback) -> BOTH need pricing;
    # the forced-llm filter must not silently contribute $0 to the max.
    est = estimate_plan_cost(json.dumps({"steps": [
        {"tool": "operator_filter", "args": {"source": "llm"}},
        {"tool": "operator_rank"},
    ]}), _empty_table())
    assert est.usd == pytest.approx(STATIC_FALLBACK_COST_USD)
    assert "bundle[operator_filter,operator_rank]" in est.basis
    assert "bundle-all-sql-fast-path-eligible" not in est.basis


# ── OperatorPriceTable / build_operator_price_table: history vs fallback ────


def _fake_store(rows: list[dict]) -> object:
    """Wraps *rows* in a fake telemetry store. Auto-fills ``step_count``
    from each row's own ``steps`` list when absent (test rows only need
    to write ``steps``) so build_operator_price_table's row-level
    executed-ok gate (``step_count > 0`` + NOT ``_row_is_failed``) sees a
    realistic row shape without every test having to spell out the field.
    """
    filled = []
    for row in rows:
        row = dict(row)
        row.setdefault("step_count", len(row.get("steps") or []))
        filled.append(row)

    class _FakeStore:
        def query_nx_answer_runs(self, *, limit: int, include_steps: bool) -> dict:
            assert include_steps is True
            return {"rows": filled, "steps_supported": True}

    return _FakeStore()


def test_price_table_uses_history_when_samples_meet_floor() -> None:
    rows = [
        {"steps": [
            {"operator": "operator_extract", "source": "llm", "model": "claude-haiku-4-5",
             "cost_usd": cost, "elapsed_ms": 900, "ok": True}
        ]}
        for cost in (0.01, 0.02, 0.03)
    ]
    assert len(rows) == MIN_HISTORY_SAMPLES
    table = build_operator_price_table(_fake_store(rows))
    usd, ms, basis = table.price_for("operator_extract")
    assert usd == pytest.approx(0.02)  # median of 0.01/0.02/0.03
    assert ms == 900
    assert "history(n=3, model=claude-haiku-4-5)" in basis


def test_price_table_falls_back_to_static_when_history_is_thin() -> None:
    rows = [
        {"steps": [
            {"operator": "operator_extract", "source": "llm", "model": "claude-haiku-4-5",
             "cost_usd": 0.01, "elapsed_ms": 900, "ok": True}
        ]}
    ]  # only 1 sample, below MIN_HISTORY_SAMPLES
    table = build_operator_price_table(_fake_store(rows))
    usd, _ms, basis = table.price_for("operator_extract")
    assert usd == pytest.approx(STATIC_FALLBACK_COST_USD)
    assert "static-fallback" in basis


def test_price_table_excludes_non_ok_and_non_llm_source_steps() -> None:
    rows = [
        {"steps": [
            {"operator": "operator_extract", "source": "llm", "model": "m", "cost_usd": 0.5, "ok": False},
            {"operator": "operator_extract", "source": "sql", "model": None, "cost_usd": 0.0, "ok": True},
            {"operator": "operator_extract", "source": "bundle", "model": "m", "cost_usd": 0.9, "ok": True},
        ]},
    ]
    table = build_operator_price_table(_fake_store(rows))
    # none of the 3 rows qualify (not-ok, sql-source, bundle-source) ->
    # empty history -> static fallback engages.
    usd, _ms, basis = table.price_for("operator_extract")
    assert usd == pytest.approx(STATIC_FALLBACK_COST_USD)
    assert "static-fallback" in basis


def test_price_table_canonicalizes_bare_and_resolved_operator_spellings() -> None:
    # plans/runner.py records StepRecord.operator VERBATIM from whatever
    # the plan JSON's tool field said -- plan authors use either the bare
    # ("rank") or resolved ("operator_rank") spelling. History recorded
    # under ONE spelling must still be found via a lookup in the OTHER,
    # and mixed-spelling rows for the same operator must pool into one
    # bucket (not split across two, each individually starved below
    # MIN_HISTORY_SAMPLES).
    rows = [
        {"steps": [{"operator": "rank", "source": "llm", "model": "m",
                     "cost_usd": 0.02, "elapsed_ms": 400, "ok": True}]},
        {"steps": [{"operator": "operator_rank", "source": "llm", "model": "m",
                     "cost_usd": 0.04, "elapsed_ms": 600, "ok": True}]},
        {"steps": [{"operator": "rank", "source": "llm", "model": "m",
                     "cost_usd": 0.06, "elapsed_ms": 800, "ok": True}]},
    ]
    table = build_operator_price_table(_fake_store(rows))
    usd_bare, ms_bare, basis_bare = table.price_for("rank")
    usd_resolved, ms_resolved, basis_resolved = table.price_for("operator_rank")
    # price_for itself is a pure dict lookup keyed by whatever string is
    # passed -- estimate_plan_cost always canonicalizes before calling it
    # (see _resolved usage in _price_step/_price_bundle), so the pooled
    # history lives under the resolved key; a bare-spelling direct call
    # here demonstrates it is NOT independently bucketed (falls to
    # static, proving no separate "rank" bucket silently absorbed any of
    # the 3 samples).
    assert usd_bare == pytest.approx(STATIC_FALLBACK_COST_USD)
    assert "static-fallback" in basis_bare
    assert usd_resolved == pytest.approx(0.04)  # median of 0.02/0.04/0.06, pooled
    assert ms_resolved == 600
    assert "history(n=3" in basis_resolved


def test_price_table_query_failure_degrades_to_empty_table() -> None:
    class _BrokenStore:
        def query_nx_answer_runs(self, *, limit: int, include_steps: bool) -> dict:
            raise RuntimeError("engine unreachable")

    table = build_operator_price_table(_BrokenStore())
    usd, _ms, basis = table.price_for("operator_generate")
    assert usd == pytest.approx(STATIC_FALLBACK_COST_USD)
    assert "static-fallback" in basis


def test_price_table_unsupported_engine_degrades_to_empty_table() -> None:
    class _OldStore:
        def query_nx_answer_runs(self, *, limit: int, include_steps: bool) -> dict:
            return {"rows": [], "steps_supported": False}

    table = build_operator_price_table(_OldStore())
    usd, _ms, _basis = table.price_for("operator_extract")
    assert usd == pytest.approx(STATIC_FALLBACK_COST_USD)


def test_unknown_operator_has_no_price_at_all() -> None:
    usd, ms, basis = _empty_table().price_for("not_a_real_operator")
    assert usd is None
    assert ms is None
    assert "unpriceable" in basis


# ── choose_within_band ───────────────────────────────────────────────────────


def test_two_in_band_candidates_different_shapes_cheaper_wins() -> None:
    # both at confidence 0.80/0.79 -> within the 0.05 band. Different
    # SHAPES: plan 1 is a single sql-fast-path-eligible operator step
    # ($0, per the bead's own "operator name -> (llm |
    # sql-fast-path-eligible | retrieval)" classification axis); plan 2
    # is a single LLM operator step (static-fallback cost, > 0).
    cheap = _match(1, 0.79, "search", "operator_filter")
    expensive = _match(2, 0.80, "search", "operator_generate")
    matches = [expensive, cheap]  # matcher-ordered: expensive scored higher
    chosen, log = choose_within_band(matches, _empty_table())
    assert chosen.plan_id == 1
    assert len(log) == 2
    assert all("predicted_cost_usd" in row for row in log)


def test_two_out_of_band_candidates_higher_confidence_wins_regardless_of_cost() -> None:
    strong_match = _match(1, 0.90, "search", "operator_generate")  # priced, expensive shape
    weak_match = _match(2, 0.50, "search", "operator_filter")      # $0 shape, but far below band
    matches = [strong_match, weak_match]
    assert (matches[0].confidence - matches[1].confidence) > PLAN_CHOICE_CONFIDENCE_BAND
    chosen, _log = choose_within_band(matches, _empty_table())
    assert chosen.plan_id == 1


def test_none_estimate_candidate_never_wins_a_tie() -> None:
    priced = _match(1, 0.80, "search", "operator_rank")
    unpriceable = _match(2, 0.79, "search", "unknown_tool_zzz")
    matches = [priced, unpriceable]
    chosen, log = choose_within_band(matches, _empty_table())
    assert chosen.plan_id == 1
    # sanity: the unpriceable candidate really was in-band and really did
    # carry a None estimate, so this is a genuine test of the tie rule.
    unpriceable_row = next(r for r in log if r["plan_id"] == 2)
    assert unpriceable_row["in_band"] is True
    assert unpriceable_row["predicted_cost_usd"] is None


# ── choose_within_band: contiguous-prefix blocker regression (code review
# round 1 BLOCKER, T2 nyry9.20-code-review-2026-08-21; direction-symmetry
# fix, round 2 CRITICAL, T2 review-nexus-nyry9.20-round2 /
# nyry9.20-code-review-round2-2026-08-21) ────────────────────────────────
#
# matcher.py's RDR-091 scope-fit re-ranking reorders `matches` by an
# ADJUSTED score while Match.confidence stays raw cosine, so `matches` is
# NOT sorted by raw confidence. These five tests pin the fix: the
# eligible set is the CONTIGUOUS PREFIX of `matches` (matcher order)
# within `band` of matches[0]'s raw confidence IN EITHER DIRECTION,
# stopping permanently at the first out-of-band candidate. (a)-(c) cover
# the round-1 prefix mechanism; (d)-(e) cover round-2's direction
# symmetry -- a later candidate can be out-of-band by having raw
# confidence far ABOVE matches[0]'s, not just below.


def test_in_band_cheaper_candidate_wins_when_contiguous_from_top() -> None:
    # (a) matches[0] holds LOWER raw confidence than matches[1] (as a
    # scope-fit-boosted top pick would) but they are within band of each
    # other AND matches[1] is CONTIGUOUS (position 1, immediately after
    # matches[0]) -> eligible, and the cheaper shape wins.
    top = _match(1, 0.75, "search", "operator_generate")   # priced, expensive shape
    second = _match(2, 0.79, "search", "operator_filter")  # $0 shape, higher raw confidence
    matches = [top, second]
    assert (matches[0].confidence - matches[1].confidence) <= PLAN_CHOICE_CONFIDENCE_BAND
    chosen, log = choose_within_band(matches, _empty_table())
    assert chosen.plan_id == 2
    assert log[1]["in_band"] is True


def test_far_out_of_band_candidate_at_position_two_never_chosen_regardless_of_cost() -> None:
    # (b) a candidate 0.45 below matches[0] in POSITION 2 (immediately
    # adjacent) is never chosen no matter how cheap its shape is -- it is
    # simply outside the band, full stop.
    top = _match(1, 0.90, "search", "operator_generate")      # priced, expensive
    far_below = _match(2, 0.45, "search", "operator_filter")  # $0 shape, but far below band
    matches = [top, far_below]
    assert (matches[0].confidence - matches[1].confidence) == pytest.approx(0.45)
    chosen, log = choose_within_band(matches, _empty_table())
    assert chosen.plan_id == 1
    assert log[1]["in_band"] is False


def test_prefix_stops_at_first_out_of_band_even_if_a_later_candidate_is_individually_in_band() -> None:
    # (c) the prefix STOPS PERMANENTLY at the first out-of-band candidate
    # (position 1, gap 0.10 > band) -- a LATER candidate (position 2, gap
    # 0.01 <= band relative to matches[0]) is never re-admitted even
    # though it would individually pass the band check, and even though
    # it is the cheapest shape in the list.
    top = _match(1, 0.80, "search", "operator_generate")               # priced, expensive
    out_of_band_middle = _match(2, 0.70, "search", "operator_filter")  # gap 0.10 -> breaks the prefix
    would_be_in_band_last = _match(3, 0.79, "search", "operator_filter")  # gap 0.01, cheap -- must NOT be reached
    matches = [top, out_of_band_middle, would_be_in_band_last]
    chosen, log = choose_within_band(matches, _empty_table())
    assert chosen.plan_id == 1  # prefix = [top] only
    assert [row["in_band"] for row in log] == [True, False, False]


def test_higher_confidence_candidate_demoted_by_scope_fit_never_admitted_when_gap_exceeds_band() -> None:
    # (d) round-2 CRITICAL (T2 review-nexus-nyry9.20-round2 /
    # nyry9.20-code-review-round2-2026-08-21), reproduced exactly here:
    # matches[1]'s RAW confidence is well ABOVE matches[0]'s -- the shape
    # matcher.py's RDR-091 scope-fit re-ranking produces when it demotes a
    # higher-raw-confidence candidate below the matcher's actual top pick.
    # The one-sided `best_conf - m.confidence <= band` test treated this
    # as trivially in-band (negative subtraction) regardless of gap size,
    # letting cost-ranking reach past the matcher's own demotion. The
    # symmetric `abs(...)` test must reject it: gap 0.45 >> band, so
    # matches[1] breaks the prefix and matches[0] wins even though
    # matches[1]'s shape is $0 and matches[0]'s is priced/expensive.
    scope_fit_winner = _match(1, 0.50, "search", "operator_generate")  # matcher's actual top pick, expensive shape
    demoted_higher_conf = _match(2, 0.95, "search", "operator_filter")  # higher raw confidence, $0 shape
    matches = [scope_fit_winner, demoted_higher_conf]
    assert abs(matches[0].confidence - matches[1].confidence) > PLAN_CHOICE_CONFIDENCE_BAND
    chosen, log = choose_within_band(matches, _empty_table())
    assert chosen.plan_id == 1
    assert log[1]["in_band"] is False


def test_higher_confidence_candidate_within_band_still_eligible_and_cheaper_wins() -> None:
    # Mirror of (d): the symmetric fix must not become OVER-restrictive --
    # a candidate whose raw confidence is above matches[0]'s but within
    # band stays eligible, and the prefix cost-ranking rule still applies
    # (cheaper shape wins among the eligible prefix).
    top = _match(1, 0.90, "search", "operator_generate")   # priced, expensive shape
    higher_in_band = _match(2, 0.93, "search", "operator_filter")  # $0 shape, higher raw confidence but within band
    matches = [top, higher_in_band]
    assert abs(matches[0].confidence - matches[1].confidence) <= PLAN_CHOICE_CONFIDENCE_BAND
    chosen, log = choose_within_band(matches, _empty_table())
    assert chosen.plan_id == 2
    assert log[1]["in_band"] is True


def test_all_in_band_candidates_unpriceable_falls_back_to_top_confidence() -> None:
    best = _match(1, 0.80, "search", "unknown_tool_a")
    other = _match(2, 0.79, "search", "unknown_tool_b")
    chosen, _log = choose_within_band([best, other], _empty_table())
    assert chosen.plan_id == 1


def test_single_candidate_returns_unchanged() -> None:
    only = _match(1, 0.80, "search")
    chosen, log = choose_within_band([only], _empty_table())
    assert chosen is only
    assert len(log) == 1


def test_fts5_sentinel_confidence_skips_band_math_returns_top() -> None:
    top = _match(1, None, "search")
    second = _match(2, None, "search", "operator_generate")
    chosen, log = choose_within_band([top, second], _empty_table())
    assert chosen is top
    assert log[0]["in_band"] is True
    assert log[1]["in_band"] is False


def test_choose_within_band_raises_on_empty_matches() -> None:
    with pytest.raises(ValueError):
        choose_within_band([], _empty_table())


# ── process-local cache ──────────────────────────────────────────────────────


def test_cache_reuses_table_until_invalidated() -> None:
    calls = {"n": 0}

    class _CountingStore:
        def query_nx_answer_runs(self, *, limit: int, include_steps: bool) -> dict:
            calls["n"] += 1
            return {"rows": [], "steps_supported": True}

    invalidate_price_table_cache()
    store = _CountingStore()
    t1 = get_cached_price_table(store)
    t2 = get_cached_price_table(store)
    assert calls["n"] == 1
    assert t1 is t2

    invalidate_price_table_cache()
    t3 = get_cached_price_table(store)
    assert calls["n"] == 2
    assert t3 is not t1


def test_cache_stale_scenario_invalidate_picks_up_new_history() -> None:
    """A run lands (history changes); without invalidation the cache would
    keep serving the old (thin/empty) table and miss the now-available
    median."""
    state = {"rows": []}

    class _MutableStore:
        def query_nx_answer_runs(self, *, limit: int, include_steps: bool) -> dict:
            return {"rows": state["rows"], "steps_supported": True}

    invalidate_price_table_cache()
    store = _MutableStore()
    stale = get_cached_price_table(store)
    usd_before, _ms, basis_before = stale.price_for("operator_extract")
    assert "static-fallback" in basis_before

    # New history "lands" -- three real samples for operator_extract.
    state["rows"] = [
        {"step_count": 1, "steps": [{"operator": "operator_extract", "source": "llm",
                     "model": "claude-haiku-4-5", "cost_usd": 0.05,
                     "elapsed_ms": 800, "ok": True}]}
        for _ in range(MIN_HISTORY_SAMPLES)
    ]

    # Without invalidation, the cache is still stale within the TTL.
    still_stale = get_cached_price_table(store)
    assert still_stale is stale

    invalidate_price_table_cache()
    fresh = get_cached_price_table(store)
    assert fresh is not stale
    usd_after, _ms, basis_after = fresh.price_for("operator_extract")
    assert "history(n=3" in basis_after
    assert usd_after == pytest.approx(0.05)
    assert usd_after != usd_before
