# SPDX-License-Identifier: AGPL-3.0-or-later
"""RDR-232 Phase 2 step 2 (nexus-jypmj.2.8): the structured-extraction measurement, on a fake dispatch.

``scripts/bench/rdr232_extract_measure.py`` runs the production request-coverage path with the optional model
extraction on and reports what it changed. These tests drive its pieces without a model call: a fake dispatch
stands in for ``claude -p`` (and, for the wiring test, for ``claude_dispatch`` itself), so the send rules, the
bounds (concurrency, the consecutive-failure stop), the outcome accounting, the label agreement figures, the
redaction of question text and the gate shift on recorded runs are all checked on hand-built input.
"""
from __future__ import annotations

import asyncio
import json
import os
from types import SimpleNamespace
from typing import Any

import pytest

from bench import rdr232_extract_measure as em
from bench import rdr232_replay as replay
from nexus.plans.answer_kind import PlanAnswerKind, declaration_payload
from nexus.plans.request_coverage import (
    EXTRACT_ENV,
    derive_request_coverage,
    needs_structured_extract,
    structured_extract,
)

Q_LOOKUP = "What does the tuple_renew operation do to the attempts counter?"
Q_SYNTH = "Synthesize RDR-084, LIST PARSE Canonical Consolidation. State the locked architecture and the evidence supporting it."
Q_UNKNOWN_A = "What techniques improve memory efficiency in LLM agent systems?"
Q_UNKNOWN_B = "Tell me about plan matching."
Q_FACETS = "Check whether the traverse tool's result cap is described consistently across its docstring, the plan YAML comments, and the RDRs."


def _qs(*texts: str) -> dict[str, str]:
    return {replay.question_key(t): t for t in texts}


class FakeDispatch:
    """A stand-in for ``claude_dispatch``: answers by question, records every call, can fail on demand."""

    def __init__(self, answers: dict[str, Any] | None = None, *, fail: set[str] | None = None, delay: float = 0.0) -> None:
        self.answers = answers or {}
        self.fail = fail or set()
        self.delay = delay
        self.calls: list[str] = []
        self.in_flight = 0
        self.max_in_flight = 0

    async def __call__(self, prompt: str, schema: dict, *, timeout: float, usage_sink: list) -> dict:
        question = prompt.split("Question:\n", 1)[1]
        self.calls.append(question)
        self.in_flight += 1
        self.max_in_flight = max(self.max_in_flight, self.in_flight)
        try:
            await asyncio.sleep(self.delay)
            usage_sink.append(SimpleNamespace(cost_usd=0.01, input_tokens=100, output_tokens=20))
            if question in self.fail:
                raise RuntimeError("boom")
            return self.answers.get(question, {"answer_kind": "unknown", "facets": [], "constraints": []})
        finally:
            self.in_flight -= 1


def _extractor(dispatch: FakeDispatch) -> em.Extractor:
    async def extract(text: str) -> Any:
        return await structured_extract(
            derive_request_coverage(text), text, dispatch=dispatch, timeout=5.0, enabled=True, usage_sink=[],
        )

    return extract


def _says(kind: str, *facets: str) -> dict:
    return {"answer_kind": kind, "facets": list(facets), "constraints": []}


def test_fixture_questions_have_the_send_classes_the_tests_assume() -> None:
    """If the first pass drifts, this fails before the measurement tests misreport."""
    got = {}
    for q in (Q_LOOKUP, Q_SYNTH, Q_UNKNOWN_A, Q_UNKNOWN_B, Q_FACETS):
        c = derive_request_coverage(q)
        got[q] = (c.answer_kind, needs_structured_extract(c))
    assert got == {
        Q_LOOKUP: ("lookup", False), Q_SYNTH: ("synthesis", False), Q_UNKNOWN_A: ("unknown", True),
        Q_UNKNOWN_B: ("unknown", True), Q_FACETS: ("audit", True),
    }


def test_known_kind_is_not_sent_but_uncertain_facets_are() -> None:
    fake = FakeDispatch({Q_UNKNOWN_A: _says("lookup", Q_UNKNOWN_A)})
    probes, facts = asyncio.run(em.measure(_qs(Q_LOOKUP, Q_UNKNOWN_A, Q_FACETS), _extractor(fake)))
    by = {p.det_kind + "/" + p.send_class: p for p in probes.values()}
    assert set(by) == {"lookup/not_sent", "unknown/kind_unknown", "audit/facets_uncertain"}
    # the settled request never reached the dispatch; the two open ones did
    assert sorted(fake.calls) == sorted([Q_UNKNOWN_A, Q_FACETS])
    assert by["lookup/not_sent"].outcome == "not_needed" and by["lookup/not_sent"].calls == 0
    # only the unknown kind can change, and it did; the known kind with uncertain facets keeps its kind
    assert by["unknown/kind_unknown"].final_kind == "lookup" and by["unknown/kind_unknown"].final_decision == "model-assisted"
    assert by["audit/facets_uncertain"].final_kind == "audit"
    assert facts["aborted"] is False


def test_every_distinct_question_lands_in_exactly_one_outcome() -> None:
    fake = FakeDispatch(
        {Q_UNKNOWN_A: _says("synthesis", Q_UNKNOWN_A), Q_UNKNOWN_B: {"answer_kind": "not-a-kind", "facets": [], "constraints": []}},
        fail={Q_FACETS},
    )
    probes, _ = asyncio.run(em.measure(_qs(Q_LOOKUP, Q_UNKNOWN_A, Q_UNKNOWN_B, Q_FACETS), _extractor(fake)))
    stats = em.extraction_stats(probes.values())
    assert stats["outcomes"]["ok"] == 1
    assert stats["outcomes"]["invalid"] == 1
    assert stats["outcomes"]["error"] == 1
    assert stats["outcomes"]["not_needed"] == 1
    assert set(stats["outcomes"]) == set(em.OUTCOMES)  # zero outcomes are present, not absent
    assert stats["outcomes_sum_equals_distinct"] is True
    assert stats["calls_made"] == len(fake.calls) == 3
    assert stats["send_classes"] == {"kind_unknown": 2, "facets_uncertain": 1, "not_sent": 1}
    assert stats["kind_unknown_resolved_to_a_kind"] == 1
    assert stats["kind_changed_without_ok_outcome"] == 0
    # cost and tokens come from the dispatch's usage objects through the production report
    assert stats["cost_usd"]["total"] == pytest.approx(0.03)


def test_concurrency_is_clamped_to_two() -> None:
    many = [f"What approaches exist for topic number {n} in agent systems?" for n in range(8)]
    assert all(derive_request_coverage(q).answer_kind == "unknown" for q in many)
    fake = FakeDispatch(delay=0.01)
    _, facts = asyncio.run(em.measure(_qs(*many), _extractor(fake), concurrency=8))
    assert facts["concurrency_limit"] == 2
    assert fake.max_in_flight == 2  # the bound was reached, not just respected vacuously
    assert len(fake.calls) == 8


def _unknowns(n: int) -> list[str]:
    qs = [f"What approaches exist for topic number {i} in agent systems?" for i in range(n)]
    assert all(derive_request_coverage(q).answer_kind == "unknown" for q in qs)
    return qs


def test_more_than_ten_consecutive_failures_stops_sending() -> None:
    qs = _unknowns(15)
    fake = FakeDispatch(fail=set(qs))
    probes, facts = asyncio.run(em.measure(_qs(*qs), _extractor(fake), concurrency=1))
    assert len(fake.calls) == 11  # the 11th consecutive failure trips the rule; none after it is sent
    assert facts["aborted"] is True and "consecutive" in facts["stop_reason"]
    stats = em.extraction_stats(probes.values())
    assert stats["outcomes"]["error"] == 11 and stats["outcomes"]["aborted_not_sent"] == 4
    assert stats["outcomes_sum_equals_distinct"] is True


def test_a_success_resets_the_consecutive_failure_count() -> None:
    qs = _unknowns(24)
    failing = {q for i, q in enumerate(qs) if i % 3 != 2}  # fail, fail, ok, fail, fail, ok ...
    fake = FakeDispatch(fail=failing)
    _, facts = asyncio.run(em.measure(_qs(*qs), _extractor(fake), concurrency=1))
    assert facts["aborted"] is False
    assert len(fake.calls) == 24


def test_max_calls_bounds_the_sends_and_accounts_for_the_rest() -> None:
    qs = _unknowns(6)
    fake = FakeDispatch()
    probes, facts = asyncio.run(em.measure(_qs(*qs), _extractor(fake), concurrency=2, max_calls=2))
    assert len(fake.calls) == 2
    assert em.extraction_stats(probes.values())["outcomes"]["aborted_not_sent"] == 4
    assert facts["aborted"] is True and "max_calls" in facts["stop_reason"]


def test_percentile_is_nearest_rank() -> None:
    assert em.percentile([], 50) is None
    vals = [float(v) for v in range(1, 21)]
    assert em.percentile(vals, 50) == 10.0
    assert em.percentile(vals, 95) == 19.0
    assert em.percentile(vals, 100) == 20.0
    assert em.percentile([7.0], 95) == 7.0


def test_latency_stats_cover_called_probes_only() -> None:
    def probe(key: str, calls: int, ms: int | None) -> em.Probe:
        return em.Probe(key, "unknown", "unknown", True, "kind_unknown", calls=calls, latency_ms=ms, outcome="ok")

    stats = em.extraction_stats([probe("a", 1, 100), probe("b", 1, 300), probe("c", 0, None)])
    assert stats["latency_ms"] == {"n": 2, "p50": 100.0, "p95": 300.0, "max": 300.0, "mean": 200.0}


def test_label_agreement_separates_agree_unknown_and_wrong() -> None:
    rows = [
        ("A", "lookup", "lookup"),        # agree
        ("B", "unknown", "unknown"),      # agree (an unknown label)
        ("C", "synthesis", "unknown"),    # unknown instead
        ("D", "audit", "lookup"),         # wrong kind
        ("E", "unknown", "comparison"),   # a kind predicted where the label is unknown: wrong kind
    ]
    got = em.label_agreement(rows)
    assert (got["n"], got["agree"], got["unknown_instead"], got["wrong_kind"]) == (5, 2, 1, 2)
    assert got["unknown_instead_qids"] == [{"qid": "C", "label": "synthesis"}]
    assert got["wrong_kind_qids"] == [
        {"qid": "D", "label": "audit", "predicted": "lookup"},
        {"qid": "E", "label": "unknown", "predicted": "comparison"},
    ]
    assert got["agree_rate"] == 0.4
    assert em.label_agreement([])["agree_rate"] is None


def _queries() -> list[Any]:
    return [
        SimpleNamespace(qid="L1", text=Q_LOOKUP, answer_kind="lookup"),            # deterministic agrees
        SimpleNamespace(qid="U1", text=Q_UNKNOWN_A, answer_kind="lookup"),          # unknown; the model gets it right
        SimpleNamespace(qid="U2", text=Q_UNKNOWN_B, answer_kind="synthesis"),       # unknown; the model gets it wrong
        SimpleNamespace(qid="D1", text="Tell me about plan matching.", answer_kind="unknown"),  # a repeat text: one probe
    ]


def test_labelled_report_figures_and_no_question_text() -> None:
    queries = _queries()
    fake = FakeDispatch({Q_UNKNOWN_A: _says("lookup", Q_UNKNOWN_A), Q_UNKNOWN_B: _says("comparison", Q_UNKNOWN_B)})
    questions = {replay.question_key(q.text): q.text for q in queries}
    probes, facts = asyncio.run(em.measure(questions, _extractor(fake)))
    rep = em.labelled_report("heldout", queries, probes, facts)
    det, ext = rep["deterministic"]["agreement"], rep["with_extraction"]["agreement"]
    # deterministic: L1 agrees, D1 agrees (unknown label), U1 and U2 are unknown instead
    assert (det["agree"], det["unknown_instead"], det["wrong_kind"]) == (2, 2, 0)
    # with extraction: U1 is now right; U2 is a wrong kind; D1 is the same text as U2 so it shares U2's reading
    assert (ext["agree"], ext["unknown_instead"], ext["wrong_kind"]) == (2, 0, 2)
    assert [w["qid"] for w in ext["wrong_kind_qids"]] == ["U2", "D1"]
    assert rep["in_sample"] is False and rep["questions"] == 4 and rep["distinct_questions"] == 3
    assert rep["extraction"]["calls_made"] == 2
    dumped = json.dumps(rep)
    for q in (Q_LOOKUP, Q_UNKNOWN_A, Q_UNKNOWN_B):
        assert q not in dumped and q[:25] not in dumped


# ── the gate shift on recorded runs ─────────────────────────────────────────────────────────────────────


def _curated(kind: PlanAnswerKind) -> str:
    return json.dumps({"steps": [], "answer_kind": declaration_payload(kind, "curated")})


def _library() -> list[dict]:
    builtin = "builtin-template,rdr-097"
    return [
        {"id": 473, "name": "lookup", "tags": builtin, "plan_json": _curated(PlanAnswerKind.LOOKUP), "disabled_at": None},
        {"id": 2, "name": "synth", "tags": builtin, "plan_json": _curated(PlanAnswerKind.SYNTHESIS), "disabled_at": None},
    ]


def _run(rid: int, question: str, plan_id: int) -> dict:
    return {"id": rid, "question": question, "plan_id": plan_id, "matched_confidence": 0.9, "step_count": 2,
            "final_text": "ok", "created_at": "2026-10-20T00:00:00Z"}


def test_kind_override_changes_the_gate_verdict_and_default_is_untouched() -> None:
    rows = [_run(1, Q_UNKNOWN_A, 473)]
    base = replay.classify_rows(rows, _library())
    assert base["runs"][0]["request_kind"] == "unknown" and base["runs"][0]["reason"] == "unknown_request_narrow_plan"
    assert replay.classify_rows(rows, _library(), kind_override=None) == base
    over = replay.classify_rows(rows, _library(), kind_override={replay.question_key(Q_UNKNOWN_A): "lookup"})
    run = over["runs"][0]
    assert (run["request_kind"], run["kind_decision"], run["admitted"]) == ("lookup", "model-assisted", True)
    # an override for another question is not applied
    other = replay.classify_rows(rows, _library(), kind_override={replay.question_key(Q_UNKNOWN_B): "lookup"})
    assert other["runs"][0]["admitted"] is False


def test_real_report_shifts_plan_473_admissions_and_exclusions() -> None:
    rows = [
        _run(1, Q_UNKNOWN_A, 473), _run(2, Q_UNKNOWN_A, 473),   # unknown -> lookup: admitted after
        _run(3, Q_UNKNOWN_B, 473),                               # unknown -> synthesis: excluded after, reason changes
        _run(4, Q_LOOKUP, 473),                                  # settled lookup: admitted both
        _run(5, Q_SYNTH, 2),                                     # settled synthesis on a synthesis plan
    ]
    questions, run_ids = em.real_questions(rows, _library())
    assert sorted(len(v) for v in run_ids.values()) == [1, 1, 1, 2]  # distinct questions, runs per question
    fake = FakeDispatch({Q_UNKNOWN_A: _says("lookup", Q_UNKNOWN_A), Q_UNKNOWN_B: _says("synthesis", Q_UNKNOWN_B)})
    probes, facts = asyncio.run(em.measure(questions, _extractor(fake)))
    for key, ids in run_ids.items():
        probes[key].run_ids = ids
    rep = em.real_report(rows, _library(), probes, facts)
    g = rep["gate"]
    assert g["before"]["plan_473"] == {
        "runs": 4, "admitted": 1, "excluded": 3, "excluded_by_reason": {"unknown_request_narrow_plan": 3},
        "request_kinds": {"lookup": 1, "unknown": 3},
    }
    assert g["after"]["plan_473"]["admitted"] == 3 and g["after"]["plan_473"]["excluded"] == 1
    assert g["after"]["plan_473"]["excluded_by_reason"] == {"kind_mismatch": 1}
    assert g["runs_flipped_excluded_to_admitted"] == 2 and g["runs_flipped_admitted_to_excluded"] == 0
    assert g["flipped_to_admitted_run_ids"] == [1, 2]
    assert g["before"]["excluded_share"] == 0.6 and g["after"]["excluded_share"] == 0.2
    assert g["before"]["unknown_rate"] == 0.6 and g["after"]["unknown_rate"] == 0.0
    assert rep["extraction"]["distinct_questions"] == 4
    dumped = json.dumps(rep)
    for q in (Q_LOOKUP, Q_UNKNOWN_A, Q_UNKNOWN_B, Q_SYNTH):
        assert q not in dumped and q[:25] not in dumped


def test_real_questions_skips_what_the_replay_skips() -> None:
    rows = [_run(1, Q_UNKNOWN_A, 473), _run(2, Q_UNKNOWN_B, 0), _run(3, Q_UNKNOWN_B, 999), _run(4, "  ", 473)]
    questions, run_ids = em.real_questions(rows, _library())
    assert list(questions.values()) == [Q_UNKNOWN_A] and run_ids == {replay.question_key(Q_UNKNOWN_A): [1]}


# ── the production wiring ───────────────────────────────────────────────────────────────────────────────


def test_core_extractor_drives_the_production_path_and_restores_state(monkeypatch) -> None:
    """The real extractor must hit ``nx_answer``'s own function with the setting on, pass its pin and operator
    name to ``claude_dispatch``, tap the usage objects, and leave the environment and the module as found."""
    from nexus.operators import dispatch as op

    monkeypatch.delenv(EXTRACT_ENV, raising=False)
    seen: list[dict[str, Any]] = []

    async def fake_claude_dispatch(prompt: str, schema: dict, timeout: float = 300.0, *, usage_sink=None, **kw: Any) -> dict:
        seen.append({"timeout": timeout, "kw": kw, "env": os.environ.get(EXTRACT_ENV)})
        usage_sink.append(SimpleNamespace(cost_usd=0.02, input_tokens=111, output_tokens=22))
        return _says("lookup", Q_UNKNOWN_A)

    monkeypatch.setattr(op, "claude_dispatch", fake_claude_dispatch)
    captured: dict[str, list[Any]] = {}

    async def go() -> tuple[dict[str, em.Probe], dict]:
        with em.core_extractor(captured) as extract:
            assert os.environ[EXTRACT_ENV] == "1"
            assert op.claude_dispatch is not fake_claude_dispatch  # tapped while in the block
            return await em.measure(_qs(Q_UNKNOWN_A, Q_LOOKUP), extract, token_sink=captured)

    probes, _ = asyncio.run(go())
    assert EXTRACT_ENV not in os.environ and op.claude_dispatch is fake_claude_dispatch
    assert len(seen) == 1  # the settled lookup never reached the dispatch
    assert seen[0]["kw"]["operator"] == "request_coverage_extract"
    assert seen[0]["kw"]["model"]  # the pinned default alias, as nx_answer passes it
    assert seen[0]["timeout"] == 20.0  # the default extraction timeout, no override
    p = probes[replay.question_key(Q_UNKNOWN_A)]
    assert (p.outcome, p.final_kind, p.calls) == ("ok", "lookup", 1)
    assert (p.input_tokens, p.output_tokens, p.cost_usd) == (111, 22, 0.02)
    assert probes[replay.question_key(Q_LOOKUP)].outcome == "not_needed"


def test_core_extractor_restores_a_preexisting_setting(monkeypatch) -> None:
    monkeypatch.setenv(EXTRACT_ENV, "0")
    with em.core_extractor({}):
        assert os.environ[EXTRACT_ENV] == "1"
    assert os.environ[EXTRACT_ENV] == "0"


def test_a_write_opt_in_in_the_environment_is_refused() -> None:
    from nexus.db.service_endpoint import PROD_WRITE_OPT_IN_ENV

    em.refuse_prod_write_opt_in({})
    with pytest.raises(SystemExit):
        em.refuse_prod_write_opt_in({PROD_WRITE_OPT_IN_ENV: "a reason"})


def test_model_report_names_the_requested_setting_and_the_models_that_answered() -> None:
    from nexus.plans.request_coverage import EXTRACT_MODEL_ENV

    captured = {"a": [SimpleNamespace(model="claude-haiku-4-5")], "b": [SimpleNamespace(model="claude-haiku-4-5"), SimpleNamespace(model=None)]}
    rep = em.model_report({EXTRACT_MODEL_ENV: " haiku "}, captured)
    assert rep == {"requested": "haiku", "canonical_models": {"None": 1, "claude-haiku-4-5": 2}}
    assert em.model_report({}, {}) == {"requested": None, "canonical_models": {}}


def test_dry_run_main_writes_a_report_without_any_model_call(tmp_path, capsys) -> None:
    out = tmp_path / "r.json"
    assert em.main(["--set", "golden", "--dry-run", "--out", str(out)]) == 0
    rep = json.loads(out.read_text(encoding="utf-8"))
    assert rep["dry_run"] is True and rep["extraction"]["calls_made"] == 0
    assert rep["extract_model"]["canonical_models"] == {}
    assert rep["questions"] == 24 and rep["in_sample"] is True
    assert rep["extraction"]["send_classes"]["kind_unknown"] + rep["extraction"]["send_classes"]["facets_uncertain"] > 0
    from bench.schema import load_golden_set

    text = out.read_text(encoding="utf-8")
    for q in load_golden_set(em.GOLDEN_PATH):
        assert q.text not in text
