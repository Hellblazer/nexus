# SPDX-License-Identifier: AGPL-3.0-or-later
"""RDR-232 Phase 2 (nexus-jypmj.2.2): RequestCoverage, derived before ``plan_match``.

Tenant-free: the derivation is pure, the structured step takes an injected dispatch (a fake; no model
call), and the ``nx_answer`` tests patch the matcher, the runner and T2 the way ``test_rdr232_caller_scope``
does. Each test names the claim it pins. The incident question is read from the golden set, verbatim.
"""
from __future__ import annotations

import ast
import asyncio
import inspect
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
import yaml
from structlog.testing import capture_logs

from nexus.plans import request_coverage as rc
from nexus.plans.evidence import EvidenceRequest, OwnerResolution, SourceRef, build_evidence_request
from nexus.plans.request_coverage import (
    REQUEST_KINDS,
    ExtractionReport,
    RequestCoverage,
    derive_request_coverage,
    log_fields,
    structured_extract,
)

GOLDEN = Path(__file__).resolve().parents[1] / "bench" / "queries" / "golden_v1.yaml"


def _golden() -> dict[str, dict]:
    doc = yaml.safe_load(GOLDEN.read_text(encoding="utf-8"))
    return {q["qid"].split("-")[0]: q for q in doc["queries"]}


G06 = _golden()["G06"]["text"]


def _request(bindings: dict | None = None, *, scope: str = "") -> EvidenceRequest:
    def _lookup(name: str) -> str:
        return {"ART": "1.2", "Nexus": "1.1"}[name]

    return build_evidence_request(bindings, scope=scope, name_lookup=_lookup)


# ── 1. the incident question ───────────────────────────────────────────────────────────────────────────────


def test_the_incident_question_is_a_synthesis_with_its_facets_and_three_constraints() -> None:
    """The ART RDR-084 question verbatim: synthesis, the requested topics as facets, the three treatment
    clauses as constraints and not as facets, no whole_question fallback."""
    cov = derive_request_coverage(G06, scope="rdr", evidence_request=_request({"project": "ART"}))
    assert (cov.answer_kind, cov.kind_decision) == ("synthesis", "heuristic")
    assert [f.id for f in cov.facets] == ["f1", "f2", "f3", "f4", "f5", "f6"]
    assert all(f.origin == "clause" for f in cov.facets), "the clause structure was read, not abandoned"
    wanted = ["locked architecture", "numerical conventions", "evidence", "gate history",
              "implementation work", "remains open by design"]
    for fragment in wanted:
        assert sum(fragment in f.text for f in cov.facets) == 1, (fragment, [f.text for f in cov.facets])
    assert [c.kind for c in cov.constraints] == ["temporal_qualification", "citation", "no_current_code"]
    assert [c.id for c in cov.constraints] == ["c1", "c2", "c3"]
    joined = " | ".join(f.text for f in cov.facets).lower()
    for constraint_words in ("acceptance", "later records", "cite", "do not infer"):
        assert constraint_words not in joined, f"{constraint_words!r} is a constraint, not a facet"
    assert [s.value for s in cov.named_sources] == ["RDR-084"] and cov.source_origin == "question"
    assert cov.current_code_requested is False, "a prohibition and an implementation-status facet are not a request"
    assert cov.explicit_scope.kind == "corpus_prefix" and cov.explicit_scope.value == "rdr"
    assert [h.text for h in cov.source_hints if h.kind == "title"] == ["LIST PARSE Canonical Consolidation"]
    assert cov.subject and "RDR-084" in cov.subject[0], "the topic sentence is kept, not dropped"


# ── 2. ambiguity is unknown, and verb_infer is not consulted ───────────────────────────────────────────────


@pytest.mark.parametrize("qid", ["G20", "G22", "G23", "G24"])
def test_ambiguous_phrasing_is_unknown_and_never_asks_verb_infer(qid: str) -> None:
    text = _golden()[qid]["text"]
    boom = AssertionError("RequestCoverage must not consult verb_infer")
    with patch("nexus.plans.verb_infer.infer_verb", side_effect=boom):
        cov = derive_request_coverage(text)
    assert cov.answer_kind == "unknown" and cov.kind_decision == "unknown"


def test_the_module_does_not_import_verb_infer() -> None:
    tree = ast.parse(inspect.getsource(rc))
    imported = {
        n.module if isinstance(n, ast.ImportFrom) else a.name
        for n in ast.walk(tree) if isinstance(n, (ast.Import, ast.ImportFrom))
        for a in (n.names if isinstance(n, ast.Import) else [SimpleNamespace(name=n.module)])
    }
    assert not any("verb_infer" in (m or "") for m in imported), imported


def test_two_different_strong_signals_are_ambiguous_and_say_so() -> None:
    cov = derive_request_coverage("Why does the matcher fail on this input, and how does it differ from the old matcher?")
    assert cov.answer_kind == "unknown"
    assert [a.code for a in cov.ambiguities if a.code == "kind_ambiguous"] == ["kind_ambiguous"]


def test_golden_kinds_are_never_contradicted() -> None:
    """Measured against the hand labels: a kind is right or unknown, never another kind. At least 20 of 24
    agree outright (a non-vacuity floor, not a target)."""
    exact = wrong = 0
    unknowns: list[str] = []
    for qid, q in _golden().items():
        label = rc.normalize_kind(q["answer_kind"])
        got = derive_request_coverage(q["text"], scope=q.get("scope", "")).answer_kind
        if got == label:
            exact += 1
        elif got == "unknown":
            unknowns.append(qid)
        else:
            wrong += 1
            print(f"WRONG {qid}: label {label} got {got}")
    print(f"RDR232-COVERAGE-KIND golden={len(_golden())} exact={exact} unknown_instead={unknowns} wrong={wrong}")
    assert wrong == 0
    assert exact >= 20


# ── 3. extraction failure: unknown plus one whole_question facet, nothing dropped ─────────────────────────

_TWO_SENTENCE = "Tell me about plan matching, the cache, and the matcher cost. Cite your sources."


def _uncertain() -> RequestCoverage:
    cov = derive_request_coverage(_TWO_SENTENCE)
    assert cov.answer_kind == "unknown" and not cov.facets_certain, "the fixture must leave the shape open"
    return cov


def test_the_uncertain_first_pass_keeps_the_whole_question_and_the_constraints() -> None:
    cov = _uncertain()
    assert [(f.id, f.origin) for f in cov.facets] == [("f1", "whole_question")]
    assert cov.facets[0].text == _TWO_SENTENCE, "every sentence of the question is in the one facet"
    assert [c.kind for c in cov.constraints] == ["citation"], "the constraint is kept as well"


@pytest.mark.parametrize("failure", ["timeout", "error", "invalid"])
def test_a_failed_structured_extraction_leaves_unknown_and_whole_question(failure: str) -> None:
    async def dispatch(prompt, schema, *, timeout, usage_sink):  # noqa: ARG001
        if failure == "timeout":
            await asyncio.sleep(5)
        if failure == "error":
            raise RuntimeError("model unavailable")
        return {"answer_kind": "not-a-kind", "facets": "nope", "constraints": []}

    before = _uncertain()
    after = asyncio.run(structured_extract(before, _TWO_SENTENCE, dispatch=dispatch, timeout=0.05, enabled=True))
    assert after.extraction.outcome == failure and after.extraction.calls == 1
    assert (after.answer_kind, after.kind_decision) == ("unknown", "unknown")
    assert after.facets == before.facets and after.constraints == before.constraints, "no clause was dropped"
    assert after.facets[0].origin == "whole_question"


# ── 4. the structured call: at most once, reported separately, bounded ───────────────────────────────────


class _Dispatch:
    def __init__(self, result: dict | None = None, *, cost: float | None = 0.0123) -> None:
        self.calls: list[tuple[str, dict, float]] = []
        self.extra: list[dict] = []
        self.result = result if result is not None else {
            "answer_kind": "synthesis",
            "facets": ["how plan matching works", "what the cache does", "what the matcher costs"],
            "constraints": [{"text": "cite your sources", "kind": "citation"}],
        }
        self.cost = cost

    async def __call__(self, prompt, schema, *, timeout, usage_sink, **extra):
        self.calls.append((prompt, schema, timeout))
        self.extra.append(extra)
        if self.cost is not None:
            usage_sink.append(SimpleNamespace(cost_usd=self.cost))
        return self.result


def test_the_structured_call_runs_at_most_once_and_is_reported_on_its_own() -> None:
    fake = _Dispatch()
    before = _uncertain()
    ticks = iter([100.0, 100.25])
    after = asyncio.run(structured_extract(
        before, _TWO_SENTENCE, dispatch=fake, timeout=7.0, enabled=True, clock=lambda: next(ticks)))
    assert len(fake.calls) == 1, "one direct call, never a retry or a second"
    assert _TWO_SENTENCE in fake.calls[0][0] and fake.calls[0][1] is rc.EXTRACT_SCHEMA
    assert after.extraction == ExtractionReport("ok", calls=1, timeout_s=7.0, latency_ms=250, cost_usd=0.0123)
    assert (after.answer_kind, after.kind_decision) == ("synthesis", "model_assisted")
    assert [f.text for f in after.facets] == ["how plan matching works", "what the cache does", "what the matcher costs"]
    assert [c.kind for c in after.constraints] == ["citation"], "first-pass constraints are never replaced"
    assert after.derive_ms == before.derive_ms, "the first pass's own time is not mixed into the model call's"


def test_the_structured_call_is_off_unless_enabled_and_skipped_when_the_first_pass_is_sure() -> None:
    off, sure = _Dispatch(), _Dispatch()
    cov = asyncio.run(structured_extract(_uncertain(), _TWO_SENTENCE, dispatch=off, timeout=5, enabled=False))
    assert off.calls == [] and cov.extraction.outcome == "disabled"
    established = derive_request_coverage(G06)
    done = asyncio.run(structured_extract(established, G06, dispatch=sure, timeout=5, enabled=True))
    assert sure.calls == [] and done.extraction.outcome == "not_needed" and done.extraction.calls == 0
    assert done.answer_kind == "synthesis" and done.facets == established.facets


def test_the_structured_call_never_overrides_an_explicit_kind_or_adds_a_call_for_one() -> None:
    cov = derive_request_coverage(_TWO_SENTENCE, explicit_kind="debug")
    assert (cov.answer_kind, cov.kind_decision) == ("debug", "explicit")
    fake = _Dispatch()
    after = asyncio.run(structured_extract(cov, _TWO_SENTENCE, dispatch=fake, timeout=5, enabled=True))
    assert after.answer_kind == "debug" and after.kind_decision == "explicit"


def test_the_setting_is_off_by_default_and_its_timeout_is_bounded() -> None:
    assert rc.extraction_enabled({}) is False
    assert rc.extraction_enabled({rc.EXTRACT_ENV: "1"}) is True
    assert rc.extraction_enabled({rc.EXTRACT_ENV: "off"}) is False
    assert rc.extraction_timeout({}) == rc.DEFAULT_EXTRACT_TIMEOUT_S
    assert rc.extraction_timeout({rc.EXTRACT_TIMEOUT_ENV: "9999"}) == 60.0
    assert rc.extraction_timeout({rc.EXTRACT_TIMEOUT_ENV: "junk"}) == rc.DEFAULT_EXTRACT_TIMEOUT_S


# ── 5. ambiguity is disclosed, never settled by choosing a project ────────────────────────────────────────


def test_an_unqualified_rdr_in_a_multi_facet_request_discloses_the_ambiguity() -> None:
    cov = derive_request_coverage(G06, scope="rdr")
    codes = [(a.code, a.subject) for a in cov.ambiguities]
    assert codes == [("source_unqualified", "RDR-084")]
    assert "no project qualifier" in cov.ambiguities[0].detail and "'rdr' is a corpus" in cov.ambiguities[0].detail
    assert cov.to_wire()["active_project_tier"] == "not_wired"


def test_an_explicit_project_binding_is_not_ambiguous() -> None:
    cov = derive_request_coverage(G06, scope="rdr", evidence_request=_request({"project": "ART"}))
    assert cov.ambiguities == ()


@pytest.mark.parametrize(
    ("bindings", "scope", "code"),
    [
        ({"owner": "1.2", "project": "Nexus"}, "", "owner_conflict"),
        ({"project": "NoSuchProject"}, "", "owner_unresolved"),
    ],
)
def test_a_conflicting_or_unresolved_qualifier_is_disclosed(bindings, scope, code) -> None:
    def _lookup(name: str) -> str:
        from nexus.plans.evidence import OwnerNameError
        if name == "Nexus":
            return "1.1"
        raise OwnerNameError("no such owner")

    request = build_evidence_request({"source": "RDR-084", **bindings}, scope=scope, name_lookup=_lookup)
    cov = derive_request_coverage(G06, evidence_request=request)
    assert code in [a.code for a in cov.ambiguities]


def test_two_projects_named_in_the_text_for_one_rdr_are_an_ambiguity() -> None:
    cov = derive_request_coverage("Compare ART's RDR-084 with RDR-084 in Nexus.")
    assert "project_hint_conflict" in [a.code for a in cov.ambiguities]
    assert cov.source_origin == "question" and [s.value for s in cov.named_sources] == ["RDR-084"]


def test_a_project_named_in_the_text_is_a_hint_and_not_a_qualifier() -> None:
    cov = derive_request_coverage("Synthesize ART RDR-084 and state its open items.")
    assert [(h.kind, h.text) for h in cov.source_hints if h.kind == "project"] == [("project", "ART")]
    codes = [a.code for a in cov.ambiguities]
    assert "source_unqualified" in codes and "project_hint_unapplied" in codes
    assert cov.ambiguities[0].detail.count("'ART'") == 1


# ── 6. the T2 lifecycle stream follows a gate, lifecycle or status facet ──────────────────────────────────


@pytest.mark.parametrize(
    ("question", "wanted"),
    [
        ("State the gate history of RDR-084, and what remains open.", True),
        ("Which decisions in RDR-210 are implemented, and which are still pending?", True),
        ("Synthesize RDR-155: when it was accepted, what it removed, and what remains open.", True),
        ("Which RDR introduced catalog tumblers as hierarchical document addresses?", False),
        ("Which RDR proposed the BERTopic-based taxonomy with HDBSCAN clustering?", False),
        ("Synthesize RDR-155: what the local and cloud storage modes are, what was removed, and what remains open.",
         False),
        # a constraint that mentions acceptance or status is not a facet
        ("State the migration path of RDR-155, and distinguish facts at the 2026-07-30 acceptance from later records.",
         False),
        ("Explain RDR-084's architecture, and do not infer implementation status beyond the evidence.", False),
    ],
)
def test_the_lifecycle_stream_is_wanted_only_for_a_gate_lifecycle_or_status_facet(question: str, wanted: bool) -> None:
    assert derive_request_coverage(question).wants_lifecycle_stream is wanted


def test_golden_requests_that_keep_and_lose_the_lifecycle_stream() -> None:
    """The change this bead makes to plan-visible behaviour, over the golden set: of the requests that name an
    RDR source, only those with a gate, lifecycle or status facet still get the T2 stream."""
    keep, lose = [], []
    for qid, q in _golden().items():
        if not any(str(s).startswith("RDR-") for s in q["required_sources"]):
            continue
        (keep if derive_request_coverage(q["text"]).wants_lifecycle_stream else lose).append(qid)
    print(f"RDR232-LIFECYCLE-GATING keep={keep} lose={lose}")
    assert keep == ["G06", "G16"]
    assert lose == ["G01", "G02", "G03", "G04", "G07", "G09", "G13", "G15"]


# ── 7. explicit bindings and hints outrank extracted values ───────────────────────────────────────────────


def test_an_explicit_source_binding_outranks_an_rdr_id_in_the_text() -> None:
    request = _request({"source": "RDR-155", "project": "ART"})
    cov = derive_request_coverage("Synthesize RDR-084, and state the gate history.", evidence_request=request)
    assert [s.value for s in cov.named_sources] == ["RDR-155"] and cov.source_origin == "explicit"
    assert cov.ambiguities == ()


def test_an_explicit_kind_hint_outranks_the_patterns_and_a_bad_hint_is_ignored() -> None:
    q = "Which RDR introduced catalog tumblers?"
    assert derive_request_coverage(q, explicit_kind="synthesis").answer_kind == "synthesis"
    assert derive_request_coverage(q, explicit_kind="synthesis").kind_decision == "explicit"
    assert derive_request_coverage(q, explicit_kind="discovery").answer_kind == "discovery/listing"
    bad = derive_request_coverage(q, explicit_kind="nonsense")
    assert (bad.answer_kind, bad.kind_decision) == ("lookup", "heuristic")


def test_the_active_project_seam_is_recorded_and_never_qualifies_a_source() -> None:
    cov = derive_request_coverage(G06, active_project=lambda: "ART")
    assert cov.active_project == "ART"
    assert [a.code for a in cov.ambiguities] == ["source_unqualified"], "the tier is nexus-jypmj.2.4's, not this bead's"


# ── 8. current code is requested only by explicit wording ─────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("question", "wanted"),
    [
        ("Audit RDR-210 against its implementation: which decisions are implemented?", True),
        ("Is the documentation for nx_answer up to date with the code? List the gaps.", True),
        ("How is the plan matcher implemented in the code?", True),
        ("Check the current code for the retry limit.", True),
        (G06, False),
        ("State the implementation work recorded as complete or pending in RDR-084.", False),
        ("Explain the design, and do not read the current code.", False),
    ],
)
def test_current_code_is_requested_only_by_explicit_wording(question: str, wanted: bool) -> None:
    assert derive_request_coverage(question).current_code_requested is wanted


# ── 9. trace=False redacts the caller's wording ───────────────────────────────────────────────────────────

_PRIVATE = ("locked architecture", "numerical conventions", "gate history", "LIST PARSE Canonical", "RDR-084", "ART")


def test_a_redacted_wire_and_a_trace_off_log_carry_no_facet_text_or_source_hint() -> None:
    cov = derive_request_coverage(G06, evidence_request=_request({"project": "ART"}))
    wire = json.dumps(cov.to_wire(redact=True))
    off = json.dumps(log_fields(cov, trace=False), default=str)
    for fragment in _PRIVATE:
        assert fragment not in wire, f"{fragment!r} leaked into the redacted wire"
        assert fragment not in off, f"{fragment!r} leaked into the trace=False log fields"
    assert '"facets": 6' in off and '"constraints": 3' in off, "counts remain"
    on = log_fields(cov, trace=True)
    assert any("gate history" in t for t in on["facet_texts"]) and on["source_hints"]
    assert "gate history" in json.dumps(cov.to_wire()), "the unredacted wire (the caller's own envelope) keeps them"


# ── 10. nx_answer: built before plan_match, reported, and shadow-only ─────────────────────────────────────


def _core():
    import nexus.mcp.core as core

    return core


def _drive(question: str, **kw):
    from tests.test_rdr232_caller_scope import _MULTI_PLAN, _drive_nx_answer
    from tests.test_rdr232_p1_review_fixes import _plan_run_result

    kw.setdefault("scope", "")
    kw.setdefault("structured", True)
    kw.setdefault("plan_run_result", _plan_run_result([{"text": "an answer long enough to count as one."}], []))
    return _drive_nx_answer(_MULTI_PLAN, question=question, **kw)


def test_coverage_is_built_before_plan_match_and_reported_in_the_envelope_and_the_diagnostics() -> None:
    from tests.test_rdr232_caller_scope import _MULTI_PLAN

    core = _core()
    events: list[str] = []
    real = core._nx_answer_request_coverage

    async def spy(**kwargs):
        events.append("coverage")
        return await real(**kwargs)

    def matcher(*_a, **_k):
        events.append("plan_match")
        return [_MULTI_PLAN]

    with patch("nexus.mcp.core._nx_answer_request_coverage", spy):
        env, _ = _drive(G06, plan_match=matcher, bindings={"project": "ART"}, scope="rdr")
    assert events[:2] == ["coverage", "plan_match"]
    wire = env["request_coverage"]
    assert wire["answer_kind"] == "synthesis" and len(wire["facets"]) == 6 and len(wire["constraints"]) == 3
    assert wire["wants_lifecycle_stream"] is True and wire["derive_ms"] is not None
    assert wire["extraction"]["outcome"] == "disabled" and wire["extraction"]["calls"] == 0
    summary = env["evidence_diagnostics"]["request_coverage"]
    assert summary["facets"] == 6 and summary["answer_kind"] == "synthesis"
    assert "text" not in json.dumps(summary) and "gate" not in json.dumps(summary).replace("wants_lifecycle", "")


@pytest.mark.parametrize("qid", sorted(_golden()))
def test_the_matcher_is_handed_the_same_arguments_whatever_the_coverage_says(qid: str) -> None:
    """Shadow mode: the matcher's call does not depend on the coverage. Swap the real derivation for a
    coverage that claims the opposite kind and a gate facet; the matcher sees identical arguments."""
    from tests.test_rdr232_caller_scope import _MULTI_PLAN

    q = _golden()[qid]
    seen: list[dict] = []

    def matcher(question, **kwargs):
        seen.append({"question": question, **{k: v for k, v in kwargs.items() if k != "library"}})
        return [_MULTI_PLAN]

    real_run = _drive(q["text"], plan_match=matcher, scope="")
    skew = rc.RequestCoverage(
        answer_kind="debug", kind_decision="explicit",
        facets=(rc.Facet("f1", "x", "clause", ("gate",)),),
    )
    with patch("nexus.mcp.core._nx_answer_request_coverage", AsyncMock(return_value=skew)):
        _drive(q["text"], plan_match=matcher, scope="")
    assert len(seen) == 2 and seen[0] == seen[1], (seen[0], seen[1])
    assert real_run[0]["request_coverage"]["answer_kind"] in REQUEST_KINDS


def test_a_kind_hint_in_dimensions_is_taken_by_coverage_and_never_reaches_the_matcher() -> None:
    from tests.test_rdr232_caller_scope import _MULTI_PLAN

    seen: list[dict] = []

    def matcher(question, **kwargs):
        seen.append(kwargs)
        return [_MULTI_PLAN]

    env, _ = _drive("Tell me about plan matching.", plan_match=matcher,
                    dimensions={"answer_kind": "synthesis", "verb": "research"})
    assert seen[0]["dimensions"] == {"verb": "research"}, "an unknown dimension would filter every plan out"
    assert env["request_coverage"]["answer_kind"] == "synthesis"
    assert env["request_coverage"]["kind_decision"] == "explicit"


# ── 11. the lifecycle stream follows the facet in nx_answer ───────────────────────────────────────────────


def _lifecycle_calls(question: str) -> int:
    from tests.test_rdr232_lifecycle_stream import _resolution, _src

    life = AsyncMock(return_value=None)
    with patch("nexus.mcp.core._nx_answer_named_sources", AsyncMock(return_value=_resolution(_src()))), \
         patch("nexus.mcp.core._nx_answer_lifecycle_stream", life), \
         patch("nexus.mcp.core._nx_answer_primary_stream", AsyncMock(return_value=(None, None))):
        _drive(question, bindings={"source": "RDR-084", "project": "ART"})
    return life.await_count


def test_nx_answer_reads_the_lifecycle_stream_only_for_a_gate_lifecycle_or_status_facet() -> None:
    assert _lifecycle_calls(G06) == 1
    assert _lifecycle_calls("Which RDR introduced catalog tumblers as hierarchical document addresses?") == 0
    assert _lifecycle_calls("State what RDR-084 decided about list parsing.") == 0


# ── 12. trace=False: nothing the caller wrote reaches the log ─────────────────────────────────────────────


@pytest.fixture
def info_logs():
    """INFO events reach the capture: nexus filters structlog at WARNING by default. The wrapper class is put
    back afterwards, so the setting does not outlive the test."""
    import logging

    import structlog

    saved = structlog.get_config()["wrapper_class"]
    structlog.configure(wrapper_class=structlog.make_filtering_bound_logger(logging.INFO))
    try:
        yield
    finally:
        structlog.configure(wrapper_class=saved)


def _coverage_events(trace: bool) -> list[dict]:
    with capture_logs() as logs:
        _drive(G06, trace=trace, bindings={"project": "ART"}, scope="rdr")
    return [e for e in logs if e.get("event") == "nx_answer_request_coverage"]


def test_trace_off_logs_counts_and_no_wording_and_trace_on_logs_the_facets(info_logs) -> None:
    (off,) = _coverage_events(False)
    text = json.dumps(off, default=str)
    for fragment in _PRIVATE:
        assert fragment not in text, f"{fragment!r} reached the log with trace=False"
    assert off["facets"] == 6 and off["constraints"] == 3 and off["answer_kind"] == "synthesis"
    (on,) = _coverage_events(True)
    assert any("gate history" in t for t in on["facet_texts"])


# ── 13. the structured call through nx_answer ─────────────────────────────────────────────────────────────


def test_nx_answer_makes_the_structured_call_only_when_the_setting_is_on(monkeypatch) -> None:
    fake = _Dispatch({"answer_kind": "synthesis", "facets": ["plan matching"], "constraints": []})

    def run(env_value: str | None) -> dict:
        if env_value is None:
            monkeypatch.delenv(rc.EXTRACT_ENV, raising=False)
        else:
            monkeypatch.setenv(rc.EXTRACT_ENV, env_value)
        with patch("nexus.operators.dispatch.claude_dispatch", fake):
            env, _ = _drive("Tell me about plan matching.")
        return env["request_coverage"]

    off = run(None)
    assert fake.calls == [] and off["extraction"]["outcome"] == "disabled"
    on = run("1")
    assert len(fake.calls) == 1, "one call, on the caller's opt-in"
    assert fake.extra[0]["operator"] == "request_coverage_extract"
    assert on["extraction"]["outcome"] == "ok" and on["extraction"]["calls"] == 1
    assert on["extraction"]["latency_ms"] is not None and on["extraction"]["timeout_s"] is not None
    assert on["answer_kind"] == "synthesis" and on["kind_decision"] == "model_assisted"
