# SPDX-License-Identifier: AGPL-3.0-or-later
"""RDR-196 Phase 3 Step 0 (nexus-nyry9.19): derive the default
``budget_usd`` from recorded, POST-FLIP run history.

Pins (a) the post-flip predicate filters on the per-step canonical model
(R3 prevention, nx_plan_audit fold 2026-08-20): a fixture holding BOTH
pre-flip (strong-tier) and post-flip (cheap-tier) rows for a flipped
operator derives from the post-flip subset only; (b) nearest-rank
percentiles and the would-have-refused fraction; (c) the non-vacuity
floor: too few qualifying runs names NO value; (d) enforcement is OFF
after this bead and the old 0.25 literal no longer sits on
``nx_answer``'s signature.
"""
from __future__ import annotations

import inspect
import json

import pytest

from nexus.plans import budget_default as bd
from nexus.plans.budget_default import (
    BUDGET_ENFORCEMENT_ENABLED,
    CANDIDATE_PERCENTILES,
    DERIVED_BUDGET_USD,
    MIN_DERIVATION_RUNS,
    check_enforcement_invariant,
    derive_budget_default,
    is_post_flip_run,
)

# The CURRENT strong pin's canonical family (STRONG_DEFAULT_ALIAS="opus");
# PRE_PIN is the former default's id, used as the non-conforming example.
STRONG = "claude-opus-5"
CHEAP = "claude-haiku-4-5"
PRE_PIN = "claude-fable-5"


def _step(operator: str, model: str | None, cost: float | None, source: str = "llm",
          ok: bool = True) -> dict:
    return {"operator": operator, "model": model, "cost_usd": cost,
            "source": source, "ok": ok, "elapsed_ms": 1000}


def _run(steps: list[dict], *, final_text: str = "answer") -> dict:
    return {"step_count": len(steps), "steps": steps, "final_text": final_text,
            "plan_id": 1, "cost_usd": None}


def _store(rows: list[dict], *, steps_supported: bool = True):
    class _Fake:
        def query_nx_answer_runs(self, *, limit: int, include_steps: bool,
                                 since: str | None = None) -> dict:
            assert include_steps is True
            return {"rows": rows, "steps_supported": steps_supported}
    return _Fake()


class TestPostFlipPredicate:
    def test_flipped_operator_on_cheap_model_is_post_flip(self) -> None:
        assert is_post_flip_run([_step("operator_filter", CHEAP, 0.05)])
        assert is_post_flip_run([_step("filter", CHEAP, 0.05)])  # bare spelling

    def test_flipped_operator_on_strong_model_is_pre_flip(self) -> None:
        assert not is_post_flip_run([_step("operator_filter", STRONG, 0.8)])
        assert not is_post_flip_run([_step("rank", None, 0.8)])  # unknown model: not proven

    def test_hold_and_bundle_steps_must_match_the_strong_pin(self) -> None:
        """v2 (critique [23254]): HOLD/bundle steps are no longer
        tier-invariant — they must record the STRONG_DEFAULT family."""
        assert is_post_flip_run([_step("operator_generate", STRONG, 0.9)])
        assert is_post_flip_run([_step("operator_filter", None, None, source="sql")])
        assert is_post_flip_run([_step("operator_filter", STRONG, 0.9, source="bundle")])
        assert is_post_flip_run([])
        # a pre-repin fable bundle/HOLD step now DISQUALIFIES the run
        assert not is_post_flip_run([_step("operator_generate", PRE_PIN, 1.87, source="bundle")])
        assert not is_post_flip_run([_step("operator_generate", PRE_PIN, 0.9)])
        assert not is_post_flip_run([_step("operator_generate", None, 0.9)])  # unknown taints

    def test_cheap_alias_family_is_a_substring_of_the_recorded_canonical_id(self) -> None:
        """The predicate couples the tier ALIAS (model_tiers) to the
        canonical id a cheap dispatch records (.p2c measured
        claude-haiku-4-5, T2 nexus_rdr/196-phase2-ab-measurement). A
        re-point that breaks this pin must also revisit is_post_flip_run."""
        from nexus.operators.model_tiers import resolve_model_for_tier

        assert resolve_model_for_tier("cheap") in CHEAP

    def test_one_pre_flip_step_taints_the_run(self) -> None:
        steps = [_step("operator_extract", CHEAP, 0.03), _step("operator_rank", STRONG, 0.9)]
        assert not is_post_flip_run(steps)


class TestDerivation:
    def test_pre_flip_rows_are_excluded_from_the_derivation(self) -> None:
        pre = [_run([_step("operator_filter", STRONG, 5.0)]) for _ in range(40)]
        post = [_run([_step("operator_filter", CHEAP, 0.01 * i)]) for i in range(1, 41)]
        d = derive_budget_default(_store(pre + post))
        assert d.n_rows_scanned == 80
        assert d.n_executed_ok == 80
        assert d.n_excluded_pre_flip == 40
        assert d.n_runs == 40
        assert d.flipped_step_models == {STRONG: 40, CHEAP: 40}
        assert max(d.costs) == pytest.approx(0.40)  # no 5.0 row leaked in
        assert d.sufficient is True
        assert d.percentiles[50] == pytest.approx(0.20)
        assert d.percentiles[90] == pytest.approx(0.36)
        assert d.percentiles[95] == pytest.approx(0.38)

    def test_would_refuse_fraction_is_strictly_above_the_value(self) -> None:
        post = [_run([_step("operator_rank", CHEAP, float(i))]) for i in range(1, 101)]
        d = derive_budget_default(_store(post))
        assert d.percentiles[90] == pytest.approx(90.0)
        assert d.would_refuse[90] == pytest.approx(0.10)
        assert d.would_refuse[50] == pytest.approx(0.50)
        assert d.would_refuse[95] == pytest.approx(0.05)

    def test_per_run_cost_sums_llm_and_bundle_steps_only(self) -> None:
        row = _run([
            _step("operator_filter", None, None, source="sql"),
            _step("operator_extract", CHEAP, 0.10),
            _step("operator_generate", STRONG, 0.70, source="bundle"),
        ])
        d = derive_budget_default(_store([row] * MIN_DERIVATION_RUNS))
        assert d.n_runs == MIN_DERIVATION_RUNS
        assert d.costs[0] == pytest.approx(0.80)

    def test_unknown_cost_step_excludes_the_run_never_a_zero(self) -> None:
        row = _run([_step("operator_extract", CHEAP, None)])
        d = derive_budget_default(_store([row] * 5))
        assert d.n_runs == 0 and d.n_excluded_unknown_cost == 5

    def test_failed_and_degenerate_rows_are_excluded(self) -> None:
        failed = _run([_step("operator_extract", CHEAP, 0.1)], final_text="Error: boom")
        degenerate = {"step_count": 0, "steps": [], "final_text": "x", "plan_id": 0}
        no_steps = {"step_count": 3, "steps": [], "final_text": "ok", "plan_id": 2}
        d = derive_budget_default(_store([failed, degenerate, no_steps]))
        assert d.n_runs == 0
        assert d.n_executed_ok == 1  # only the pre-.p1f row with no steps list
        assert d.n_excluded_no_steps == 1

    def test_continuation_handoff_and_report_rows_are_excluded(self) -> None:
        """RDR-200 (nexus-4e75w.5 fold): a handed-off row's step_count is
        > 0 and it is not `_row_is_failed` -- without an EXPLICIT marker
        check it would silently pass into this derivation's population
        and contaminate the headless cost average with a continuation
        run's prefix-only, LLM-free cost (the RDR's own "continuation
        runs are never folded into the headless cost average" rule). A
        report row already has step_count==0 (so the pre-existing
        step_count<=0 gate happens to exclude it too), but the exclusion
        must be an explicit marker check, not an accident of that
        field's value -- both are counted via the same
        n_excluded_continuation counter."""
        from nexus.mcp.core import (
            NX_ANSWER_CONTINUATION_MARKER_PREFIX,
            NX_ANSWER_CONTINUATION_REPORT_MARKER_PREFIX,
        )

        # Marker-collision hardening (critic-F3, T2 [23952]): the
        # classifiers now require a PARSEABLE (uuid4-shaped)
        # continuation_id, not a bare prefix match -- a placeholder like
        # "cid-1" would silently fail to classify post-hardening.
        cid = "11111111-1111-4111-8111-111111111111"
        handoff = _run(
            [_step("search", None, 0.0, source="sql")],
            final_text=f"{NX_ANSWER_CONTINUATION_MARKER_PREFIX} continuation_id={cid}]",
        )
        report = {
            "step_count": 0, "steps": [],
            "final_text": f"{NX_ANSWER_CONTINUATION_REPORT_MARKER_PREFIX} "
                          f"continuation_id={cid} ok=True]",
            "plan_id": None,
        }
        post = [_run([_step("operator_filter", CHEAP, 0.1 * i)]) for i in range(1, 31)]

        d = derive_budget_default(_store([handoff, report] + post))

        assert d.n_rows_scanned == 32
        assert d.n_excluded_continuation == 2
        assert d.n_executed_ok == 30, (
            "the handoff row must never be counted as executed-ok either "
            "-- excluded before that counter, not folded in and then "
            "filtered back out downstream"
        )
        assert d.n_runs == 30
        assert d.sufficient is True

    def test_below_floor_names_no_value(self) -> None:
        post = [_run([_step("operator_filter", CHEAP, 0.1)]) for _ in range(MIN_DERIVATION_RUNS - 1)]
        d = derive_budget_default(_store(post))
        assert d.sufficient is False
        assert d.percentiles == {} and d.would_refuse == {}
        assert d.n_runs == MIN_DERIVATION_RUNS - 1

    def test_empty_store_and_unsupported_engine_are_loud_not_zero(self) -> None:
        d = derive_budget_default(_store([]))
        assert d.n_runs == 0 and d.sufficient is False
        d2 = derive_budget_default(_store([_run([_step("rank", CHEAP, 1.0)])], steps_supported=False))
        assert d2.n_rows_scanned == 0 and d2.sufficient is False

        class _Broken:
            def query_nx_answer_runs(self, **kw):
                raise ConnectionError("down")
        d3 = derive_budget_default(_Broken())
        assert d3.n_runs == 0 and d3.sufficient is False

    def test_tier_config_names_the_flipped_set_and_cheap_alias(self) -> None:
        d = derive_budget_default(_store([]))
        for op in ("filter", "groupby", "extract", "rank", "check", "verify"):
            assert op in d.tier_config
        assert "haiku" in d.tier_config
        assert "opus" in d.tier_config  # the strong pin is part of the provenance

    def test_as_dict_round_trips_to_json(self) -> None:
        post = [_run([_step("operator_filter", CHEAP, 0.1 * i)]) for i in range(1, 31)]
        payload = derive_budget_default(_store(post)).as_dict()
        text = json.dumps(payload)
        back = json.loads(text)
        assert back["n_runs"] == 30 and back["sufficient"] is True
        assert set(back["percentiles"]) == {str(p) for p in CANDIDATE_PERCENTILES}


class TestEnforcementStillOff:
    def test_derived_value_set_and_enforcement_now_on(self) -> None:
        """.p3a produced the number (p90, n=30, 2026-08-21); .p3c
        (nexus-nyry9.21) flips enforcement ON now that a real derived
        default exists."""
        assert DERIVED_BUDGET_USD == pytest.approx(1.0530)
        assert BUDGET_ENFORCEMENT_ENABLED is True
        assert MIN_DERIVATION_RUNS >= 30

    def test_enforcement_cannot_be_on_with_a_none_default(self) -> None:
        with pytest.raises(RuntimeError, match="DERIVED_BUDGET_USD is None"):
            check_enforcement_invariant(True, None)
        check_enforcement_invariant(False, None)
        check_enforcement_invariant(True, 0.5)
        # The module calls it at import on its own constants.
        src = inspect.getsource(bd)
        assert "check_enforcement_invariant(BUDGET_ENFORCEMENT_ENABLED, DERIVED_BUDGET_USD)" in src

    def test_nx_answer_signature_no_longer_carries_the_unmeasured_literal(self) -> None:
        from nexus.mcp.core import nx_answer

        sig = inspect.signature(nx_answer)
        assert "budget_usd" in sig.parameters
        assert sig.parameters["budget_usd"].default is None

    @staticmethod
    def _provenance_block() -> str:
        lines = inspect.getsource(bd).splitlines()
        idx = next(i for i, ln in enumerate(lines) if ln.startswith("DERIVED_BUDGET_USD"))
        block: list[str] = []
        for ln in reversed(lines[:idx]):
            if not ln.startswith("#"):
                break
            block.append(ln)
        return "\n".join(reversed(block))

    @staticmethod
    def _check_provenance(value: float | None, comment: str) -> None:
        import re

        assert comment, "DERIVED_BUDGET_USD has no provenance comment directly above it"
        if value is None:
            assert "UNDERIVED" in comment, "a None default must say UNDERIVED"
            return
        assert "UNDERIVED" not in comment, "stale UNDERIVED note on a derived value"
        assert re.search(r"\bn=\d+", comment), "derivation record must state n="
        assert re.search(r"\bp(50|75|90|95)\b", comment), "must name the percentile"
        assert re.search(r"20\d\d-\d\d-\d\d", comment), "must carry the derivation date"
        assert "flipped=" in comment, "must record the tier configuration"

    def test_provenance_comment_binds_to_the_constant_value(self) -> None:
        """The comment block directly above ``DERIVED_BUDGET_USD`` must
        agree with its value: ``None`` requires an UNDERIVED note; a real
        number requires a derivation record (date, n=, percentile, tier
        config) and NO stale UNDERIVED text."""
        self._check_provenance(DERIVED_BUDGET_USD, self._provenance_block())

    def test_provenance_guard_is_falsifiable(self) -> None:
        """A value under a stale UNDERIVED comment trips the guard; a
        proper record passes; a record missing n= fails; the CURRENT
        block satisfies the CURRENT (derived) value's requirements."""
        stale = "# PROVENANCE: UNDERIVED as of 2026-08-21. no rows yet."
        with pytest.raises(AssertionError, match="stale UNDERIVED"):
            self._check_provenance(0.42, stale)
        self._check_provenance(DERIVED_BUDGET_USD, self._provenance_block())
        good = ("# derived 2026-09-01 from n=41 post-flip runs, p90, would refuse 9.8%,\n"
                "# flipped={extract,filter,groupby,rank}@haiku; others=HOLD")
        self._check_provenance(0.42, good)
        with pytest.raises(AssertionError, match="n="):
            self._check_provenance(0.42, good.replace("n=41", "forty-one"))
        with pytest.raises(AssertionError, match="UNDERIVED"):
            self._check_provenance(None, good)


class TestAnswerRunsDeriveBudgetFlag:
    def test_flag_emits_the_derivation(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from click.testing import CliRunner

        import nexus.db.t2.http_telemetry_store as hts
        from nexus.commands.answer_runs import answer_runs_cmd

        rows = [_run([_step("operator_filter", CHEAP, 0.1 * i)]) for i in range(1, 41)]
        rows += [_run([_step("operator_filter", STRONG, 9.0)]) for _ in range(10)]

        class _FakeStore:
            def __init__(self, *a, **k):
                pass

            def query_nx_answer_runs(self, *, since=None, limit=20, include_steps=False):
                assert include_steps is True
                return {"rows": rows, "steps_supported": True}

        monkeypatch.setattr(hts, "HttpTelemetryStore", _FakeStore)
        res = CliRunner().invoke(answer_runs_cmd, ["--derive-budget", "--json"])
        assert res.exit_code == 0, res.output
        out = json.loads(res.stdout)
        assert out["n_runs"] == 40 and out["n_excluded_pre_flip"] == 10
        assert out["sufficient"] is True
        assert out["percentiles"]["90"] == pytest.approx(3.6)

        res2 = CliRunner().invoke(answer_runs_cmd, ["--derive-budget"])
        assert res2.exit_code == 0, res2.output
        assert "post-flip" in res2.output and "p90" in res2.output
        assert "would refuse" in res2.output
        assert "claude-opus-5 (10)" in res2.output and "claude-haiku-4-5 (40)" in res2.output
