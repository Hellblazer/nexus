# SPDX-License-Identifier: AGPL-3.0-or-later
"""RDR-232 Phase 3 (nexus-jypmj.3.5): the run summary, its canonical digest and its redaction.

Pure unit tests over ``nexus.plans.run_summary``: what the summary carries, what ``trace=False`` removes,
that it stays under the engine's size cap, and the canonical-JSON digest the readback compares. The
engine-backed legs (record, complete, query, import, old engine, rollback) live in
``tests/db/test_rdr232_run_summary_integration.py``; the store's capability probe in
``tests/db/test_http_telemetry_store_run_summary.py``.
"""
from __future__ import annotations

import hashlib
import json
import math
from dataclasses import replace

import pytest

from nexus.plans import eligibility as eligibility_mod
from nexus.plans.coverage_ledger import (
    AssessmentReport,
    CoverageLedger,
    CoverageResult,
    FacetFinding,
)
from nexus.plans.request_coverage import (
    Constraint,
    ExtractionReport,
    Facet,
    RequestCoverage,
    SourceHint,
)
from nexus.plans.run_summary import (
    ENGINE_RUN_SUMMARY_LIMIT_BYTES,
    RUN_SUMMARY_MAX_BYTES,
    RUN_SUMMARY_VERSION,
    RunSummaryInputs,
    build_run_summary,
    canonical_json,
    fit_summary,
    summary_digest,
)

PRIVATE_QUESTION = "what did the SECRET-ACQUISITION plan decide about acme?"
PRIVATE_FACET = "PRIVATE-FACET-TEXT acme acquisition terms"
PRIVATE_HINT = "PRIVATE-HINT-TITLE"


def _request(*, facets: int = 2) -> RequestCoverage:
    return RequestCoverage(
        answer_kind="synthesis",
        kind_decision="heuristic",
        facets=tuple(
            Facet(id=f"f{i + 1}", text=f"{PRIVATE_FACET} {i}", source_hints=("RDR-084",))
            for i in range(facets)
        ),
        constraints=(Constraint(id="c1", text="PRIVATE-CONSTRAINT", kind="citation"),),
        source_origin="question",
        source_hints=(SourceHint(source="RDR-084", kind="title", text=PRIVATE_HINT),),
        subject=("PRIVATE-SUBJECT sentence",),
        extraction=ExtractionReport(outcome="ok", calls=1, latency_ms=840, cost_usd=0.0021),
        derive_ms=12,
    )


def _eligibility_wire() -> dict:
    flt = eligibility_mod.EligibilityFilter("synthesis", "heuristic", enforce=True)
    return flt.to_wire()


def _diagnostics() -> dict:
    return {
        "streams": {
            "vector": {"references": 9, "by_collection": {"docs__acme": 9}, "in_scope": 9, "out_of_scope": 0, "steps": [0]},
            "graph": {"references": 0, "by_collection": {}, "in_scope": 0, "out_of_scope": 0, "steps": []},
            "primary": {
                "references": 4, "by_collection": {"rdr__acme": 4}, "in_scope": 4, "out_of_scope": 0,
                "by_source": {"PRIVATE-SOURCE-LABEL": 4},
            },
            "t2": {"references": 2, "by_project": {"acme-private-project": 2}},
        },
    }


def _inputs(**over) -> RunSummaryInputs:
    base = dict(
        request=_request(), eligibility=_eligibility_wire(), diagnostics=_diagnostics(),
        scope_diagnostics=[{"outcome": "conflict"}, {"outcome": "narrowed"}, {"outcome": "conflict"}],
    )
    base.update(over)
    return RunSummaryInputs(**base)


def _coverage(state: str, *, rounds: int = 1) -> CoverageResult:
    findings = (
        FacetFinding("f1", "covered", ("e1",)),
        FacetFinding("f2", "unsupported", (), "no_supporting_evidence"),
    )
    return CoverageResult(
        state=state, reason="test", skip_generation=state == "unavailable",
        ledger=CoverageLedger(
            findings=findings,
            assessment=AssessmentReport(outcome="ok", calls=2, facets_per_call=(1, 1), latency_ms=900, rejected_records=1),
            rounds=rounds,
        ),
    )


def _build(*, trace: bool = True, coverage: CoverageResult | None = None, terminal: bool = True,
           inputs: RunSummaryInputs | None = None, **over) -> dict:
    kw = dict(
        outcome="success", plan_id=7, step_count=3, duration_ms=4200, cost_usd=0.0314,
        trace=trace, coverage=coverage, terminal_reached=terminal,
    )
    kw.update(over)
    return build_run_summary(inputs or _inputs(), **kw)


class TestCanonicalDigest:
    """The digest the readback compares: canonical JSON, sorted keys, one integer-versus-float rule."""

    def test_key_order_does_not_change_the_digest(self):
        a = {"b": 1, "a": {"y": [1, 2], "x": None}}
        b = {"a": {"x": None, "y": [1, 2]}, "b": 1}
        assert summary_digest(a) == summary_digest(b)
        assert canonical_json(a) == '{"a":{"x":null,"y":[1,2]},"b":1}'

    def test_integral_floats_and_ints_are_one_number(self):
        # jsonb returns 3 as 3 or 3.0 depending on the path; the rule makes them one value.
        assert summary_digest({"n": 3}) == summary_digest({"n": 3.0})
        assert canonical_json({"n": 100.0}) == '{"n":100}'
        assert canonical_json({"n": -0.0}) == '{"n":0}'

    def test_non_integral_floats_stay_distinct_from_neighbours(self):
        assert summary_digest({"c": 0.1}) != summary_digest({"c": 0.2})
        assert summary_digest({"c": 0.1}) != summary_digest({"c": 0})
        assert canonical_json({"c": 0.0314}) == '{"c":0.0314}'

    def test_booleans_are_not_numbers(self):
        assert summary_digest({"ok": True}) != summary_digest({"ok": 1})
        assert summary_digest({"ok": False}) != summary_digest({"ok": 0})

    def test_list_order_is_significant(self):
        assert summary_digest({"l": [1, 2]}) != summary_digest({"l": [2, 1]})

    def test_non_finite_numbers_are_refused(self):
        for bad in (math.nan, math.inf, -math.inf):
            with pytest.raises(ValueError, match="finite"):
                canonical_json({"x": bad})

    def test_non_string_keys_are_refused(self):
        with pytest.raises(ValueError, match="string"):
            canonical_json({1: "a"})

    def test_a_float_that_went_through_json_digests_the_same(self):
        # What the engine does to a stored summary: serialise, parse (floats come back BigDecimal-parsed),
        # serialise. The digest is over the parsed value, so the round trip cannot move it.
        s = _build(coverage=_coverage("partial"))
        again = json.loads(json.dumps(s))
        assert summary_digest(again) == summary_digest(s)

    def test_digest_is_pinned_to_the_literal_canonical_text(self):
        # Pins the canonical form itself, built by hand rather than by the implementation: a change to
        # separators, key order, the number rule or the encoding changes it.
        literal = '{"cost_usd":0.5,"n":3,"s":"\u00e9","version":1}'.replace("\\u00e9", "\u00e9")
        want = "sha256:" + hashlib.sha256(literal.encode("utf-8")).hexdigest()
        assert summary_digest({"version": 1, "cost_usd": 0.5, "n": 3.0, "s": "\u00e9"}) == want


class TestSummaryContent:
    def test_version_and_top_level_keys(self):
        s = _build(coverage=_coverage("partial"))
        assert s["version"] == RUN_SUMMARY_VERSION == 1
        assert s["kind"] == "nx_answer"
        assert s["outcome"] == "success"
        for key in (
            "coverage_complete", "coverage_partial", "coverage_unavailable", "coverage_unassessed",
            "coverage_state", "coverage", "request", "eligibility", "streams", "scope", "repair", "run", "trace",
        ):
            assert key in s, key

    @pytest.mark.parametrize("state", ["complete", "partial", "unavailable", "unassessed"])
    def test_exactly_one_coverage_count_is_set_from_the_terminal_assessment(self, state):
        s = _build(coverage=_coverage(state))
        counts = {k: s[f"coverage_{k}"] for k in ("complete", "partial", "unavailable", "unassessed")}
        assert counts == {k: int(k == state) for k in counts}
        assert s["coverage_state"] == state

    def test_a_run_that_reached_reduction_without_a_boundary_is_unassessed(self):
        s = _build(coverage=None, terminal=True)
        assert (s["coverage_unassessed"], s["coverage_complete"], s["coverage_partial"], s["coverage_unavailable"]) == (1, 0, 0, 0)
        assert s["coverage_state"] == "unassessed"
        assert s["coverage"] is None

    def test_a_run_that_never_reached_reduction_has_no_coverage_state(self):
        s = _build(coverage=None, terminal=False, outcome="planner_failure", plan_id=None, step_count=0)
        assert [s[f"coverage_{k}"] for k in ("complete", "partial", "unavailable", "unassessed")] == [0, 0, 0, 0]
        assert s["coverage_state"] == "none"

    def test_assessment_calls_facets_per_call_and_repair_usage(self):
        s = _build(coverage=_coverage("partial", rounds=2))
        assert s["coverage"]["assessment_calls"] == 2
        assert s["coverage"]["facets_per_call"] == [1, 1]
        assert s["coverage"]["assessment_latency_ms"] == 900
        # `entered`: nexus-jypmj.3.10; `reason` and `unread_tail_facets`: nexus-z6yvk; all additive
        assert s["repair"] == {
            "attempts": 1, "facets_repairable": 1, "entered": False, "reason": "", "unread_tail_facets": 0,
        }
        assert _build(coverage=_coverage("partial", rounds=1))["repair"]["attempts"] == 0

    def test_request_kind_exclusions_streams_cost_and_latency(self):
        s = _build(coverage=_coverage("complete"))
        assert s["request"]["answer_kind"] == "synthesis"
        assert s["request"]["kind_decision"] == "heuristic"
        assert s["request"]["facet_count"] == 2
        assert s["request"]["extraction"] == {"outcome": "ok", "calls": 1, "latency_ms": 840, "cost_usd": 0.0021}
        assert s["eligibility"]["enforced"] is True
        assert set(s["eligibility"]["branches"]) == {"cosine", "category_route", "fts"}
        assert s["streams"]["vector"] == {"references": 9, "in_scope": 9, "out_of_scope": 0}
        assert s["streams"]["primary"] == {"references": 4, "in_scope": 4, "out_of_scope": 0}
        assert s["streams"]["t2"] == {"references": 2}
        assert s["scope"] == {"conflicts": 2, "diagnosed_steps": 3}
        assert s["run"] == {"plan_id": 7, "step_count": 3, "duration_ms": 4200, "cost_usd": 0.0314}

    def test_missing_inputs_still_yield_a_summary_with_the_fields_that_exist(self):
        s = build_run_summary(
            RunSummaryInputs(), outcome="failure", plan_id=None, step_count=0, duration_ms=5, cost_usd=None,
            trace=True, coverage=None, terminal_reached=False,
        )
        assert s["version"] == 1 and s["outcome"] == "failure"
        assert s["request"] is None and s["eligibility"] is None and s["streams"] is None
        assert s["run"]["cost_usd"] is None
        json.dumps(s)  # serialisable

    def test_every_value_is_json_plain(self):
        s = _build(coverage=_coverage("partial", rounds=2))
        assert json.loads(json.dumps(s)) == json.loads(canonical_json(s))

    def test_a_nan_cost_is_dropped_not_sent(self):
        s = _build(cost_usd=math.nan)
        assert s["run"]["cost_usd"] is None
        canonical_json(s)


class TestRedaction:
    """Scenario 5: with trace=False the summary carries ids and counts, never the question or its words."""

    TEXTS = (PRIVATE_FACET, PRIVATE_HINT, "PRIVATE-CONSTRAINT", "PRIVATE-SUBJECT", "PRIVATE-SOURCE-LABEL",
             "acme-private-project", "RDR-084", PRIVATE_QUESTION)

    def test_trace_false_carries_no_facet_text_hint_or_label(self):
        text = canonical_json(_build(trace=False, coverage=_coverage("partial")))
        for needle in self.TEXTS:
            assert needle not in text, needle

    def test_trace_false_still_carries_ids_and_counts(self):
        s = _build(trace=False, coverage=_coverage("partial"))
        assert [f["id"] for f in s["request"]["facets"]] == ["f1", "f2"]
        assert s["request"]["facet_count"] == 2
        assert s["request"]["source_hint_count"] == 1
        assert s["request"]["constraint_count"] == 1
        assert s["trace"] is False
        assert all("text" not in f for f in s["request"]["facets"])

    def test_trace_true_carries_the_facet_text_so_the_redaction_test_is_not_vacuous(self):
        s = _build(trace=True, coverage=_coverage("partial"))
        text = canonical_json(s)
        assert PRIVATE_FACET in text and PRIVATE_HINT in text
        assert s["trace"] is True

    def test_the_question_is_never_in_the_summary_at_either_setting(self):
        for trace in (True, False):
            assert PRIVATE_QUESTION not in canonical_json(_build(trace=trace, coverage=_coverage("complete")))

    def test_a_whole_question_facet_carries_no_text_at_either_setting(self):
        """Shakedown 2026-10-28 finding 5 (nexus-jypmj.3.16): the whole_question facet's text IS the question, so
        a trace=True summary (row 743) carried the question verbatim against "no question text"."""
        request = replace(_request(facets=1), facets=(Facet("f1", PRIVATE_QUESTION, "whole_question"),))
        for trace in (True, False):
            s = _build(trace=trace, coverage=_coverage("complete"), inputs=_inputs(request=request))
            assert PRIVATE_QUESTION not in canonical_json(s), trace
            assert s["request"]["facets"] == [{"id": "f1", "origin": "whole_question", "topics": []}
                                              | ({"source_hints": []} if trace else {})]


class TestSizeCap:
    def test_the_client_cap_sits_below_the_engine_limit(self):
        assert RUN_SUMMARY_MAX_BYTES < ENGINE_RUN_SUMMARY_LIMIT_BYTES == 65_536

    def test_a_huge_request_is_bounded_at_build_and_keeps_its_counts(self):
        huge = RequestCoverage(
            answer_kind="synthesis",
            facets=tuple(Facet(id=f"f{i}", text="x" * 4000, source_hints=("RDR-084",) * 20) for i in range(400)),
            source_hints=tuple(SourceHint("RDR-084", "title", "t" * 500) for _ in range(300)),
        )
        s = _build(inputs=_inputs(request=huge), coverage=_coverage("partial"))
        assert len(canonical_json(s).encode("utf-8")) <= RUN_SUMMARY_MAX_BYTES
        assert s["request"]["facet_count"] == 400, "the count is the whole, the listing is capped"
        assert len(s["request"]["facets"]) < 400
        assert s["request"]["source_hint_count"] == 300
        assert all(len(f["text"]) <= 200 for f in s["request"]["facets"])

    def test_fit_summary_cuts_optional_blocks_in_order_and_names_them(self):
        s = _build(coverage=_coverage("partial"))
        s["request"]["facets"] = [{"id": f"f{i}", "text": "y" * 2000} for i in range(60)]
        s["request"]["source_hints"] = ["h" * 2000] * 20
        out = fit_summary(s)
        assert len(canonical_json(out).encode("utf-8")) <= RUN_SUMMARY_MAX_BYTES
        assert out["truncated"], "a cut summary names what it dropped"
        assert out["truncated"][0] == "request.source_hints", "the cheapest-to-lose block goes first"
        assert out["coverage_partial"] == 1 and out["request"]["facet_count"] == 2, "counts survive the cut"

    def test_fit_summary_leaves_a_small_summary_untouched(self):
        s = _build(coverage=_coverage("complete"))
        assert fit_summary(dict(s)) == s
        assert "truncated" not in s

    def test_fit_summary_always_reaches_the_cap_even_for_a_pathological_shape(self):
        s = {"version": 1, "kind": "nx_answer", "outcome": "success", "junk": ["z" * 100_000]}
        out = fit_summary(s)
        assert len(canonical_json(out).encode("utf-8")) <= RUN_SUMMARY_MAX_BYTES
        assert out["version"] == 1

    def test_multibyte_text_is_measured_in_bytes_not_characters(self):
        s = {"version": 1, "kind": "nx_answer", "outcome": "success", "junk": ["é" * 40_000]}
        out = fit_summary(s)
        assert len(canonical_json(out).encode("utf-8")) <= RUN_SUMMARY_MAX_BYTES
