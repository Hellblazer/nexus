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


@pytest.mark.parametrize("qid", ["G22", "G23", "G24"])
def test_ambiguous_phrasing_is_unknown_and_never_asks_verb_infer(qid: str) -> None:
    text = _golden()[qid]["text"]
    boom = AssertionError("RequestCoverage must not consult verb_infer")
    with patch("nexus.plans.verb_infer.infer_verb", side_effect=boom):
        cov = derive_request_coverage(text)
    assert cov.answer_kind == "unknown" and cov.kind_decision == "unknown"


def test_a_why_question_about_what_named_code_decided_is_debug() -> None:
    """nexus-jypmj.2.3 (G20): canonical debug phrasing with no failure word in it. The subject is a snake_case
    identifier and the verb a selection or return verb."""
    text = _golden()["G20"]["text"]
    cov = derive_request_coverage(text)
    assert (cov.answer_kind, cov.kind_decision) == ("debug", "heuristic")
    for q in (
        "Why does plan_match return no plans for this question?",
        "why does `plan_match()` skip grown plans",
        "Why did nx_answer choose the research plan?",
    ):
        assert derive_request_coverage(q).answer_kind == "debug", q


@pytest.mark.parametrize(
    "question",
    [
        "Why does the RDR choose tumblers?",  # no code subject: a design-rationale question
        "Why did the team pick nx_answer?",  # the subject is people, not code
        "Why does nx_answer exist?",  # not a selection or return verb
        "Why do snake_case names matter?",
        "Which paper explains why nx_answer picks lookup plans?",  # does not open with why
        "Why does nx_answer pick the lookup plan, and which RDR introduced it?",  # a second question joined on
        "Why does nx_answer pick the lookup plan, and how does it differ from plan_match?",  # two kinds
    ],
)
def test_the_code_behaviour_why_pattern_stays_conservative(question: str) -> None:
    assert derive_request_coverage(question).answer_kind != "debug", question


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


def test_golden_kind_regression_floor() -> None:
    """A regression floor over the hand labels, NOT a measure of the classifier. The patterns in
    ``request_coverage.py`` were written with these 24 golden requests in view, so the kind agreement printed
    below, the unknown rate, and the keep/lose lists of the lifecycle test are IN-SAMPLE: they say the
    patterns still fit the requests they were fitted to, and must not be read as a rate on requests the
    author has not seen. A held-out set is nexus-jypmj.2.8's job. The floor is low on purpose: it trips on a
    change that makes a golden request read as another kind, or that loses most of the agreement."""
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
    print(f"RDR232-COVERAGE-KIND in-sample golden={len(_golden())} exact={exact} unknown_instead={unknowns} wrong={wrong}")
    assert wrong == 0
    assert exact >= 16
    # In-sample tripwire, stricter than the floor above: the patterns were fitted to these 24 requests, so the
    # observed 23 exact is where they stand today, and a drop to the floor of 16 would otherwise pass unseen.
    # This is not a quality claim; a change to these numbers is a deliberate update, made here with the reason.
    assert unknowns == ["G05"] and wrong == 0, (
        f"in-sample kind agreement moved: unknown_instead={unknowns} (was ['G05']) wrong={wrong} (was 0); "
        "if the change is intended, update this assertion and say why in the commit")


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
    awaited: list[str] = []

    async def dispatch(prompt, schema, *, timeout, usage_sink):  # noqa: ARG001
        awaited.append(failure)
        if failure == "timeout":
            await asyncio.sleep(5)
        if failure == "error":
            raise RuntimeError("model unavailable")
        return {"answer_kind": "not-a-kind", "facets": "nope", "constraints": []}

    before = _uncertain()
    after = asyncio.run(structured_extract(
        before, _TWO_SENTENCE, dispatch=dispatch, timeout=0.05, enabled=True, cleanup_grace=0.05))
    assert after.extraction.outcome == failure and after.extraction.calls == 1
    # the report's calls=1 is a literal in report(); the stub's own count is the evidence of one attempt (RDR-232:
    # never retried, on a failure path too)
    assert len(awaited) == 1, f"{failure}: the dispatch was awaited {len(awaited)} times, expected exactly one"
    assert (after.answer_kind, after.kind_decision) == ("unknown", "unknown")
    assert after.facets == before.facets and after.constraints == before.constraints, "no clause was dropped"
    assert after.facets[0].origin == "whole_question"


# ── 4. the structured call: at most once, reported separately, bounded ───────────────────────────────────


class _Dispatch:
    def __init__(self, result: dict | None = None, *, cost: float | None = 0.0123) -> None:
        self.calls: list[tuple[str, dict, float]] = []
        self.extra: list[dict] = []
        self.raw: list[tuple[tuple, dict]] = []
        self.result = result if result is not None else {
            "answer_kind": "synthesis",
            "facets": ["how plan matching works", "what the cache does", "what the matcher costs"],
            "constraints": [{"text": "cite your sources", "kind": "citation"}],
        }
        self.cost = cost

    async def __call__(self, prompt, schema, *, timeout, usage_sink, **extra):
        self.calls.append((prompt, schema, timeout))
        self.extra.append(extra)
        self.raw.append(((prompt, schema), {"timeout": timeout, "usage_sink": usage_sink, **extra}))
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
    assert (after.answer_kind, after.kind_decision) == ("synthesis", "model-assisted")
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


def test_the_setting_is_on_by_default_and_its_timeout_is_bounded() -> None:
    # nexus-jypmj.2.8: on unless the kill switch (0/false/off/no) says otherwise; this pinned the old "off by default".
    assert rc.extraction_enabled({}) is True
    assert rc.extraction_enabled({rc.EXTRACT_ENV: ""}) is True
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
    assert cov.to_wire()["active_project_tier"] == "wired"


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
    RDR source, only those with a gate, lifecycle or status facet still get the T2 stream. IN-SAMPLE (the
    patterns were written with these requests in view; see the kind test above). Of the "lose" list, G01-G04 never had the stream:
    their ``required_sources`` are the RDRs the answer must find, the question names none, and the stream
    follows the named-source resolver, which reads the caller's bindings only. For them the list shows what
    the facet gate decides, not a behaviour that changed; the others (G07, G09, G13, G15) name their RDRs in
    the text and lose the stream only for a caller that binds the source."""
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
    # nexus-jypmj.2.8: extraction is on by default; this question's kind is settled by the first pass, so no call
    assert wire["extraction"]["outcome"] == "not_needed" and wire["extraction"]["calls"] == 0
    summary = env["evidence_diagnostics"]["request_coverage"]
    assert summary["facets"] == 6 and summary["answer_kind"] == "synthesis"
    assert "text" not in json.dumps(summary) and "gate" not in json.dumps(summary).replace("wants_lifecycle", "")


@pytest.mark.parametrize("qid", sorted(_golden()))
def test_the_matcher_is_handed_the_same_arguments_whatever_the_coverage_says(qid: str, monkeypatch) -> None:
    """The matcher's selection inputs do not depend on the coverage while the eligibility gate is off (the test
    sets the kill switch: the gate is on by default since nexus-jypmj.2.8). Swap the
    real derivation for a coverage that claims the opposite kind and a gate facet; the matcher sees identical
    arguments. The one new argument, the eligibility filter (nexus-jypmj.2.3), names the request kind it judges
    for, so it is compared by what it can change: it is not enforcing in either run."""
    from tests.test_rdr232_caller_scope import _MULTI_PLAN

    monkeypatch.setenv("NX_ANSWER_KIND_ELIGIBILITY", "0")
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
    assert len(seen) == 2
    filters = [call.pop("eligibility") for call in seen]
    assert [f.enforce for f in filters] == [False, False], "the kill switch is set: the filter only reports"
    assert seen[0] == seen[1], (seen[0], seen[1])
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


def test_nx_answer_makes_the_structured_call_unless_the_setting_is_off(monkeypatch) -> None:
    # nexus-jypmj.2.8: the call is on by default; "0" is the kill switch (the old test treated unset as off).
    fake = _Dispatch({"answer_kind": "synthesis", "facets": ["plan matching"], "constraints": []})

    def run(env_value: str | None) -> dict:
        if env_value is None:
            monkeypatch.delenv(rc.EXTRACT_ENV, raising=False)
        else:
            monkeypatch.setenv(rc.EXTRACT_ENV, env_value)
        with patch("nexus.operators.dispatch.claude_dispatch", fake):
            env, _ = _drive("Tell me about plan matching.")
        return env["request_coverage"]

    off = run("0")
    assert fake.calls == [] and off["extraction"]["outcome"] == "disabled"
    on = run(None)
    assert len(fake.calls) == 1, "one call with the setting unset"
    assert fake.extra[0]["operator"] == "request_coverage_extract"
    assert on["extraction"]["outcome"] == "ok" and on["extraction"]["calls"] == 1
    assert on["extraction"]["latency_ms"] is not None and on["extraction"]["timeout_s"] is not None
    assert on["answer_kind"] == "synthesis" and on["kind_decision"] == "model-assisted"


# ═══ nexus-jypmj.2.2 fix round ═════════════════════════════════════════════════════════════════════════════
#
# Every pin below was shown failing with its change removed (mutation record: the hand-back of the fix round).


# ── F0. the in-sample limit travels with the numbers ──────────────────────────────────────────────────────


def test_the_in_sample_limit_is_stated_in_the_module_docstring_and_the_changelog() -> None:
    changelog = (Path(__file__).resolve().parents[1] / "CHANGELOG.md").read_text(encoding="utf-8")
    entry = next(line for line in changelog.splitlines() if "derives a `RequestCoverage` before plan matching" in line)
    for where, text in (("module docstring", rc.__doc__ or ""), ("CHANGELOG entry", entry)):
        assert "in-sample" in text.lower() and "24 golden" in text, f"the {where} must say the patterns were written with the 24 golden requests in view"
    assert "in-sample" in (test_golden_kind_regression_floor.__doc__ or "").lower(), "the golden-kind test must say its figures are in-sample"


# ── F1 (S1). a weak "what/which" opening is lookup only for a single-facet question ───────────────────────


@pytest.mark.parametrize(
    "question",
    [
        "What are the locked architecture, the numerical conventions, the gate history and the open items of RDR-084?",
        "What did RDR-155 remove, what is the migration path, and what remains open?",
        "Which paper reported the context-precision tradeoff for per-source caps in hybrid retrieval, and what split did it recommend?",
    ],
)
def test_a_multi_facet_question_is_never_typed_lookup(question: str) -> None:
    cov = derive_request_coverage(question)
    assert len(cov.facets) > 1 or not cov.facets_certain
    assert (cov.answer_kind, cov.kind_decision) == ("unknown", "unknown")


@pytest.mark.parametrize(
    "question",
    [
        "What is the migration path for RDR-155 and what remains open?",
        "What is the T2 schema and how is it migrated?",
        "Who rejected RDR-100 and when was it superseded?",
        "Who rejected RDR-100 and why?",
    ],
)
def test_a_wh_clause_joined_by_and_is_a_second_question_and_not_lookup(question: str) -> None:
    """The facet parse keeps ``and <wh-word>`` inside one item (a wh-clause is never split), so the kind typing
    counts it itself: one certain facet is not enough for ``lookup`` when a second question is joined on."""
    cov = derive_request_coverage(question)
    assert cov.facets_certain, "the premise: one item, read as certain"
    assert (cov.answer_kind, cov.kind_decision) == ("unknown", "unknown")


@pytest.mark.parametrize(
    "question",
    [
        "What is causing the timeout in the catalog sync?",
        "Which is faster, hybrid search or plain vector search?",
    ],
)
def test_a_cause_or_a_comparative_question_is_not_lookup(question: str) -> None:
    assert derive_request_coverage(question).answer_kind != "lookup"


def test_a_single_facet_which_question_is_still_lookup() -> None:
    cov = derive_request_coverage("Which RDR introduced catalog tumblers as hierarchical document addresses?")
    assert (cov.answer_kind, cov.kind_decision) == ("lookup", "heuristic")


# ── F2 (S2, Important 3). no clause is dropped silently ───────────────────────────────────────────────────

_FILLER = "The plan matcher cache keeps entries by canonical dimensions and counts hits and misses per plan. "


def _long_question(tail: str) -> str:
    return "Describe how the plan matcher cache works. " + _FILLER * 90 + tail


def test_a_question_over_the_cap_keeps_its_full_text_is_uncertain_and_its_tail_is_scanned() -> None:
    tail = "What is the acceptance status of RDR-090 and how did its gate end?"
    question = _long_question(tail)
    assert len(question) > rc._MAX_QUESTION_CHARS
    cov = derive_request_coverage(question)
    assert cov.facets_certain is False
    (facet,) = cov.facets
    assert facet.origin == "whole_question" and facet.text == " ".join(question.split()), "the full text, uncut"
    assert tail in facet.text and "RDR-090" in facet.source_hints
    assert cov.wants_lifecycle_stream is True, "a gate/status clause past the cap still asks for the T2 stream"
    assert [s.value for s in cov.named_sources] == ["RDR-090"], "an RDR named past the cap is still named"


def test_more_constraints_than_the_cap_keep_every_clause_in_the_whole_question_facet() -> None:
    clauses = [f"Cite source number {i}" for i in range(1, 11)]
    question = "Describe the plan matcher cache. " + ". ".join(clauses) + "."
    cov = derive_request_coverage(question)
    assert len(cov.constraints) == rc._MAX_CONSTRAINTS and cov.facets_certain is False
    (facet,) = cov.facets
    assert facet.origin == "whole_question"
    assert "Cite source number 9" in facet.text and "Cite source number 10" in facet.text


def test_one_long_sentence_over_the_cap_is_uncertain_on_length_alone() -> None:
    question = "Describe the " + "plan matcher cache " * 600
    assert len(question) > rc._MAX_QUESTION_CHARS and "," not in question and "." not in question
    cov = derive_request_coverage(question)
    assert cov.facets_certain is False and cov.facets[0].text == question.strip()


_ADVERSARIAL = [
    _long_question("Also list the closed RDRs, and state the gate history."),
    "Describe the cache. " + " ".join(f"Cite source number {i}." for i in range(1, 11)),
    "List the cache, list the queue, list the planner; describe the matcher and the runner, state the budget.",
    "State the locked architecture, and also the numerical conventions, plus the gate history, but not the rest.",
    "Explain: the first tier, the second tier, the third tier, the fourth tier, the fifth tier, the sixth tier, "
    "the seventh tier, the eighth tier, the ninth tier, the tenth tier, the eleventh tier, the twelfth tier, "
    "the thirteenth tier, the fourteenth tier.",
    "Compare how ART and Nexus handle RDR-084, then contrast their current and original designs.",
    "Differentiate the T1 and T2 tiers, and describe each tier now.",
    "Do not infer current code. Cite sources. Be brief. Answer in two sentences. Distinguish acceptance from later records.",
]

_JOINERS = {"and", "also", "then", "plus", "but", "please"}
_LIST_VERBS = {"state", "list", "describe", "explain", "identify", "summarize", "summarise", "cover", "include",
               "provide", "name", "outline", "trace"}


def _tokens(text: str) -> list[str]:
    import re

    return [t.lower() for t in re.findall(r"[A-Za-z0-9][A-Za-z0-9_'’-]*", text)]


def _lost_tokens(question: str, cov: RequestCoverage) -> list[str]:
    from collections import Counter

    kept = Counter(
        t for text in [f.text for f in cov.facets] + [c.text for c in cov.constraints] + list(cov.subject)
        for t in _tokens(text)
    )
    lost = Counter(_tokens(question)) - kept
    return sorted(lost.elements())


@pytest.mark.parametrize("question", [q["text"] for q in _golden().values()] + _ADVERSARIAL)
def test_every_word_of_the_question_is_in_a_facet_a_constraint_or_the_subject(question: str) -> None:
    """The only words the structure reading may drop are the joiners it splits on (and, also, then, plus,
    but, please) and the leading listing verb of a clause (State the ..., List the ...); anything else
    missing from every facet, constraint and subject text is a dropped clause."""
    cov = derive_request_coverage(question)
    stray = [t for t in _lost_tokens(question, cov) if t not in _JOINERS and t not in _LIST_VERBS]
    assert stray == [], f"dropped from the structure: {stray}"


def test_a_listing_verb_is_dropped_only_at_the_head_of_a_clause() -> None:
    q = "State the budget. Describe how the matcher works, and explain why it is slow."
    cov = derive_request_coverage(q)
    lost = _lost_tokens(q, cov)
    assert all(t in _LIST_VERBS or t in _JOINERS for t in lost) and len(lost) <= 3


# ── F3 (S3). a requested clause is not demoted to a constraint ────────────────────────────────────────────


def test_contrast_and_differentiate_clauses_are_facets_not_temporal_constraints() -> None:
    cov = derive_request_coverage("Compare how ART and Nexus handle RDR-084, then contrast their current and original designs.")
    assert cov.constraints == ()
    joined = " | ".join(f.text for f in cov.facets)
    assert "Compare how ART and Nexus handle RDR-084" in joined, "the ' and ' inside a clause is not a facet boundary"
    assert "contrast their current and original designs" in joined
    cov2 = derive_request_coverage("Differentiate the T1 and T2 tiers, and describe each tier now.")
    assert cov2.constraints == ()
    assert "describe each tier now" in " | ".join(f.text for f in cov2.facets)


def test_a_verb_led_clause_with_an_and_inside_it_is_not_split_into_noun_items() -> None:
    cov = derive_request_coverage("Compare how ART and Nexus handle RDR-084, and cite the sources.")
    assert [c.kind for c in cov.constraints] == ["citation"]
    assert [f.text for f in cov.facets] == ["Compare how ART and Nexus handle RDR-084"]


@pytest.mark.parametrize(
    "question",
    [
        "Contrast the design at acceptance with the later records of RDR-084.",
        "Explain RDR-084, differentiate the accepted design from later records.",
    ],
)
def test_contrast_or_differentiate_stays_a_facet_even_with_an_acceptance_cue(question: str) -> None:
    assert [c for c in derive_request_coverage(question).constraints if c.kind == "temporal_qualification"] == []


@pytest.mark.parametrize(
    "clause",
    [
        "distinguish acceptance-date facts from later records",
        "Distinguish facts at the RDR's 2026-07-30 acceptance from later records",
        "separate what the RDR decided at acceptance from what was recorded since",
    ],
)
def test_a_distinguish_from_clause_with_a_date_or_acceptance_cue_is_a_temporal_constraint(clause: str) -> None:
    cov = derive_request_coverage(f"Explain the cache design of RDR-084, {clause}.")
    assert [c.kind for c in cov.constraints] == ["temporal_qualification"]


@pytest.mark.parametrize(
    "clause",
    [
        "distinguish the current T1 design from the T2 design",
        "separate what the cache does now from what the matcher does then",
    ],
)
def test_a_distinguish_from_clause_without_a_date_acceptance_or_record_cue_is_a_facet(clause: str) -> None:
    cov = derive_request_coverage(f"Explain the cache design of RDR-084, {clause}.")
    assert [c for c in cov.constraints if c.kind == "temporal_qualification"] == []
    assert any(clause in f.text for f in cov.facets) or any(clause in f.text for f in cov.facets if f.origin == "whole_question")


# ── F4 (S4). current code needs explicit wording ──────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "question",
    [
        "Describe the findings from the source RDR and their status.",
        "Summarize the source document for RDR-084.",
        "What is the source of the claim in RDR-084?",
        "Explain the findings in the source RDR.",
    ],
)
def test_source_in_another_sense_is_not_a_current_code_request(question: str) -> None:
    assert derive_request_coverage(question).current_code_requested is False


@pytest.mark.parametrize(
    "question",
    [
        "Does the source code match the RDR?",
        "Read the codebase and list the retry limit.",
        "Check the current implementation of the plan matcher.",
    ],
)
def test_explicit_code_wording_is_a_current_code_request(question: str) -> None:
    assert derive_request_coverage(question).current_code_requested is True


# ── F5 (S5, Important 4). the lifecycle vocabulary is the lifecycle table's ───────────────────────────────


def _table():
    from nexus.tables.load import load_packaged_table

    return load_packaged_table("rdr-lifecycle.toml")


def test_the_lifecycle_vocabulary_is_read_from_rdr_lifecycle_toml() -> None:
    table = _table()
    assert rc.LIFECYCLE_STATES == tuple(table.dimensions["status"].domain)
    assert rc.LIFECYCLE_EVENTS == tuple(table.dimensions["event"].domain)
    for state in rc.LIFECYCLE_STATES:
        assert "lifecycle" in rc._topics(f"Is RDR-155 {state}?"), state
    for event in rc.LIFECYCLE_EVENTS:
        if "-" not in event:
            assert "lifecycle" in rc._topics(f"Which RDR {event}s RDR-040?"), event


def test_a_state_added_to_the_table_becomes_vocabulary_without_a_code_edit() -> None:
    rx = rc._lifecycle_pattern(rc.LIFECYCLE_STATES + ("parked",), rc.LIFECYCLE_EVENTS)
    assert rx.search("Is RDR-155 parked?") and not rc._lifecycle_pattern(rc.LIFECYCLE_STATES, rc.LIFECYCLE_EVENTS).search("Is RDR-155 parked?")


def test_the_fallback_vocabulary_for_an_unreadable_table_matches_the_table() -> None:
    table = _table()
    assert rc._FALLBACK_LIFECYCLE == (tuple(table.dimensions["status"].domain), tuple(table.dimensions["event"].domain))


@pytest.mark.parametrize(
    "question",
    [
        "Is RDR-155 still a draft?",
        "Which RDR supersedes RDR-040?",
        "What state is RDR-210 in?",
        "Was RDR-088 rejected?",
        "Was RDR-196 deferred or abandoned?",
        "What is done in RDR-232?",
        "What phase is RDR-232 in?",
        "Where does RDR-084 stand?",
        "What progress has RDR-210 made?",
        "What did the critique of RDR-155 find?",
        "Has RDR-155 been accepted?",
        "Is RDR-210 closed?",
        "What is the status of RDR-084?",
    ],
)
def test_a_state_question_about_an_rdr_reads_the_lifecycle_stream(question: str) -> None:
    assert derive_request_coverage(question).wants_lifecycle_stream is True


@pytest.mark.parametrize(
    "question",
    [
        "Which RDR introduced catalog tumblers as hierarchical document addresses?",
        "State what RDR-084 decided about list parsing.",
        "Review the chunking pipeline design.",
        "How does the state machine checker prove coverage?",
        "Explain the retry limit in the plan matcher.",
    ],
)
def test_a_question_about_something_else_does_not(question: str) -> None:
    assert derive_request_coverage(question).wants_lifecycle_stream is False


@pytest.mark.parametrize(
    ("question", "wanted"),
    [
        ("Summarize RDR-210 status. List the open bugs.", True),
        ("Review RDR-210 gate history. List the open bugs.", True),
        ("Tell me about RDR-210 lifecycle. List the open bugs.", True),
        # the cue verb is not a topic, and a subject sentence with no lifecycle word has none
        ("Review RDR-210. List the open bugs.", False),
        ("Summarize RDR-210 design. List the open bugs.", False),
        ("Synthesize RDR-155. List the storage modes.", False),
    ],
)
def test_a_lifecycle_topic_in_the_subject_sentence_reaches_the_stream(question: str, wanted: bool) -> None:
    cov = derive_request_coverage(question)
    assert cov.subject, "the premise: the first sentence is read as the subject, not as a facet"
    assert cov.wants_lifecycle_stream is wanted


def test_an_uncertain_clause_structure_with_a_bound_rdr_source_reads_the_stream() -> None:
    q = "Tell me about plan matching, the cache, and the matcher cost. Cite your sources."
    bound = derive_request_coverage(q, evidence_request=_request({"source": "RDR-084", "project": "ART"}))
    assert bound.facets_certain is False and bound.wants_lifecycle_stream is True
    assert derive_request_coverage(q).wants_lifecycle_stream is False, "no bound source: nothing to read"
    sure = derive_request_coverage("Which RDR introduced catalog tumblers?", evidence_request=_request({"source": "RDR-084", "project": "ART"}))
    assert sure.facets_certain is True and sure.wants_lifecycle_stream is False


def test_a_failed_derivation_with_a_bound_rdr_source_reads_the_stream() -> None:
    with patch.object(rc, "_derive", side_effect=RuntimeError("boom")):
        bound = derive_request_coverage("anything", evidence_request=_request({"source": "RDR-084", "project": "ART"}))
        unbound = derive_request_coverage("anything")
    assert bound.facets_certain is False and bound.wants_lifecycle_stream is True
    assert [s.value for s in bound.named_sources] == ["RDR-084"]
    assert unbound.wants_lifecycle_stream is False


# ── F6 (S6, Important 1 and 2). the structured call ───────────────────────────────────────────────────────


def test_the_outer_wait_leaves_dispatch_its_own_kill_path() -> None:
    """Dispatch kills its claude -p tree in its own timeout path. An outer wait_for that fires first cancels
    dispatch before that path runs and orphans the child, so the outer bound is dispatch's timeout plus its
    cleanup grace. A dispatch that is still inside its grace when the nominal timeout passes is not cancelled."""
    async def slow_but_inside_grace(prompt, schema, *, timeout, usage_sink):  # noqa: ARG001
        await asyncio.sleep(0.3)
        return {"answer_kind": "synthesis", "facets": ["plan matching"], "constraints": []}

    after = asyncio.run(structured_extract(
        _uncertain(), _TWO_SENTENCE, dispatch=slow_but_inside_grace, timeout=0.05, enabled=True, cleanup_grace=1.0))
    assert after.extraction.outcome != "timeout" and after.extraction.calls == 1


def test_a_dispatch_that_outlives_timeout_plus_grace_is_a_timeout() -> None:
    async def hung(prompt, schema, *, timeout, usage_sink):  # noqa: ARG001
        await asyncio.sleep(5)

    after = asyncio.run(structured_extract(
        _uncertain(), _TWO_SENTENCE, dispatch=hung, timeout=0.05, enabled=True, cleanup_grace=0.05))
    assert after.extraction.outcome == "timeout"


def test_the_default_grace_covers_dispatchs_cleanup_grace_plus_one_second() -> None:
    from nexus.operators import dispatch as d

    assert rc.DEFAULT_CLEANUP_GRACE_S >= d._TIMEOUT_CLEANUP_GRACE_SECONDS + 1.0


def test_dispatchs_own_timeout_error_is_the_timeout_outcome() -> None:
    from nexus.operators.llm_types import OperatorTimeoutError

    async def killed(prompt, schema, *, timeout, usage_sink):  # noqa: ARG001
        raise OperatorTimeoutError("claude -p timed out after 20s")

    after = asyncio.run(structured_extract(_uncertain(), _TWO_SENTENCE, dispatch=killed, timeout=5, enabled=True))
    assert (after.extraction.outcome, after.extraction.calls) == ("timeout", 1)


def test_less_than_a_second_of_deadline_skips_the_call() -> None:
    fake = _Dispatch()
    after = asyncio.run(structured_extract(
        _uncertain(), _TWO_SENTENCE, dispatch=fake, timeout=0.4, enabled=True, seconds_left=0.4))
    assert fake.calls == [] and after.extraction.outcome == "skipped_deadline" and after.extraction.calls == 0
    ok = asyncio.run(structured_extract(
        _uncertain(), _TWO_SENTENCE, dispatch=fake, timeout=5, enabled=True, seconds_left=5.0))
    assert len(fake.calls) == 1 and ok.extraction.calls == 1


def _extract_env(monkeypatch, fake) -> None:
    monkeypatch.setenv(rc.EXTRACT_ENV, "1")
    monkeypatch.setattr("nexus.operators.dispatch.claude_dispatch", fake)


def test_nx_answer_clamps_the_call_to_the_real_budget_and_skips_it_when_the_budget_is_spent(monkeypatch) -> None:
    fake = _Dispatch({"answer_kind": "synthesis", "facets": ["plan matching"], "constraints": []})
    _extract_env(monkeypatch, fake)
    env, _ = _drive("Tell me about plan matching.", budget_seconds=8.0)
    ext = env["request_coverage"]["extraction"]
    assert ext["calls"] == 1 and 1.0 < ext["timeout_s"] <= 8.0, "the 20 s default is clamped to the budget that is left"
    fake.calls.clear()
    env, _ = _drive("Tell me about plan matching.", budget_seconds=0.5)
    ext = env["request_coverage"]["extraction"]
    assert fake.calls == [] and ext["outcome"] == "skipped_deadline" and ext["calls"] == 0


def test_nx_answer_gives_dispatch_a_deadline_inside_its_own_timeout(monkeypatch) -> None:
    """Two phases of the dispatch timeout would let the call run for twice the setting; the deadline it is
    handed caps the whole call at the timeout."""
    import time

    fake = _Dispatch({"answer_kind": "synthesis", "facets": ["plan matching"], "constraints": []})
    _extract_env(monkeypatch, fake)
    monkeypatch.setenv(rc.EXTRACT_TIMEOUT_ENV, "3")
    before = time.monotonic()
    env, _ = _drive("Tell me about plan matching.", budget_seconds=600.0)
    deadline = fake.extra[0]["deadline"]
    assert deadline is not None and deadline <= before + 3.0 + 5.0
    assert deadline <= before + 20, "never past the call's own timeout, even with a long budget"
    assert env["request_coverage"]["extraction"]["timeout_s"] == 3.0


def test_the_extraction_model_setting_defaults_to_haiku_and_is_trimmed() -> None:
    # nexus-jypmj.2.8: unset or blank is haiku (this pinned None, the default operator pin).
    assert rc.EXTRACT_MODEL_ENV == "NX_ANSWER_STRUCTURED_EXTRACT_MODEL"
    assert rc.DEFAULT_EXTRACT_MODEL == "haiku"
    assert rc.extraction_model({}) == "haiku"
    assert rc.extraction_model({rc.EXTRACT_MODEL_ENV: ""}) == "haiku"
    assert rc.extraction_model({rc.EXTRACT_MODEL_ENV: "   "}) == "haiku"
    assert rc.extraction_model({rc.EXTRACT_MODEL_ENV: " haiku "}) == "haiku"
    assert rc.extraction_model({rc.EXTRACT_MODEL_ENV: "claude-haiku-4-5"}) == "claude-haiku-4-5"


@pytest.mark.parametrize("value", [None, "", "  "])
def test_nx_answer_extraction_model_is_haiku_when_the_setting_is_unset_or_empty(monkeypatch, value) -> None:
    """nexus-jypmj.2.8: unset or empty asks for haiku, and ``NX_OPERATOR_MODEL_TIERING=0`` does not change that: the
    model is always passed explicitly (the old test pinned the strong operator pin and a bare dispatch under =0)."""
    fake = _Dispatch({"answer_kind": "synthesis", "facets": ["plan matching"], "constraints": []})
    _extract_env(monkeypatch, fake)
    if value is None:
        monkeypatch.delenv(rc.EXTRACT_MODEL_ENV, raising=False)
    else:
        monkeypatch.setenv(rc.EXTRACT_MODEL_ENV, value)
    _drive("Tell me about plan matching.")
    assert fake.extra[0]["model"] == "haiku"
    fake.extra.clear()
    monkeypatch.setenv("NX_OPERATOR_MODEL_TIERING", "0")
    _drive("Tell me about plan matching.")
    assert fake.extra[0]["model"] == "haiku", "the tiering kill switch does not strip an explicit model"


@pytest.mark.parametrize("value,expected", [("haiku", "haiku"), (" sonnet ", "sonnet"), ("claude-haiku-4-5", "claude-haiku-4-5")])
def test_nx_answer_passes_the_extraction_model_setting_to_the_dispatch(monkeypatch, value, expected) -> None:
    fake = _Dispatch({"answer_kind": "synthesis", "facets": ["plan matching"], "constraints": []})
    _extract_env(monkeypatch, fake)
    monkeypatch.setenv(rc.EXTRACT_MODEL_ENV, value)
    _drive("Tell me about plan matching.")
    assert fake.extra[0]["model"] == expected
    assert fake.extra[0]["operator"] == "request_coverage_extract"
    fake.extra.clear()
    monkeypatch.setenv("NX_OPERATOR_MODEL_TIERING", "0")
    _drive("Tell me about plan matching.")
    assert fake.extra[0]["model"] == expected, "a caller-supplied model wins over the kill switch, as for every dispatch"


def test_the_real_dispatch_signature_accepts_the_call_nx_answer_makes(monkeypatch) -> None:
    from nexus.operators.dispatch import claude_dispatch

    fake = _Dispatch({"answer_kind": "synthesis", "facets": ["plan matching"], "constraints": []})
    _extract_env(monkeypatch, fake)
    _drive("Tell me about plan matching.")
    (args, kwargs), = fake.raw
    inspect.signature(claude_dispatch).bind(*args, **kwargs)


def test_model_facets_that_miss_a_clause_do_not_replace_the_whole_question() -> None:
    fake = _Dispatch({"answer_kind": "synthesis", "facets": ["plan matching"], "constraints": []})
    before = _uncertain()
    after = asyncio.run(structured_extract(before, _TWO_SENTENCE, dispatch=fake, timeout=5, enabled=True))
    origins = [f.origin for f in after.facets]
    assert "whole_question" in origins and "clause" in origins
    assert next(f for f in after.facets if f.origin == "whole_question").text == _TWO_SENTENCE
    assert after.facets_certain is False, "the model's reading did not cover the question"


def test_model_facets_that_cover_the_question_replace_the_whole_question() -> None:
    fake = _Dispatch()
    after = asyncio.run(structured_extract(_uncertain(), _TWO_SENTENCE, dispatch=fake, timeout=5, enabled=True))
    assert [f.origin for f in after.facets] == ["clause"] * 3 and after.facets_certain is True


def _over_cap_question_cut_on_a_word(tail: str) -> tuple[str, str]:
    """``(question, head)``: the head is exactly the cap long (the cut lands on the end of a word), then the tail."""
    words = ("Describe how the plan matcher cache works. " + _FILLER * 90).split()
    head = ""
    for w in words:
        if len(head) + len(w) + 1 > rc._MAX_QUESTION_CHARS:
            break
        head = f"{head} {w}" if head else w
    head += " " + "p" * (rc._MAX_QUESTION_CHARS - len(head) - 1)
    assert len(head) == rc._MAX_QUESTION_CHARS
    return f"{head} {tail}", head


_N1_TAIL = "What is the acceptance status of RDR-090 and how did its gate end?"


@pytest.mark.parametrize("covers_the_tail", [False, True], ids=["model-read-the-head-only", "model-facets-name-the-tail"])
def test_model_facets_never_replace_the_whole_question_of_an_over_cap_question(covers_the_tail: bool) -> None:
    """The model is sent the first cap characters. Its facets that cover those characters (or even name the tail)
    must not replace the whole_question facet, which holds the tail, nor make the structure certain: a
    gate/status clause past the cap must still reach the lifecycle stream."""
    question, head = _over_cap_question_cut_on_a_word(_N1_TAIL)
    facet_texts = [" ".join(sorted(set(head.split())))[:300]]
    if covers_the_tail:
        facet_texts.append(_N1_TAIL)
    before = derive_request_coverage(question)
    assert before.facets_certain is False and before.wants_lifecycle_stream is True
    assert rc.uncovered_words(question[: rc._MAX_QUESTION_CHARS], facet_texts) == [], "the premise: the head is covered"
    fake = _Dispatch({"answer_kind": "synthesis", "facets": facet_texts, "constraints": []})
    after = asyncio.run(structured_extract(before, question, dispatch=fake, timeout=5, enabled=True))
    assert after.extraction.outcome == "ok"
    assert after.facets_certain is False
    whole = [f for f in after.facets if f.origin == "whole_question"]
    assert len(whole) == 1 and _N1_TAIL in whole[0].text, "the tail survives in the whole_question facet"
    assert after.wants_lifecycle_stream is True


def test_the_prompt_text_is_capped_at_the_question_cap() -> None:
    fake = _Dispatch({"answer_kind": "synthesis", "facets": ["x"], "constraints": []})
    question = "Tell me about plan matching. " * 2000
    asyncio.run(structured_extract(derive_request_coverage(question), question, dispatch=fake, timeout=5, enabled=True))
    assert len(fake.calls[0][0]) <= len(rc._EXTRACT_PROMPT) + rc._MAX_QUESTION_CHARS


# ── F7 (S7). the vocabulary is answer_kind.py's ──────────────────────────────────────────────────────────


def test_request_kinds_are_the_plan_answer_kinds() -> None:
    from nexus.plans.answer_kind import PlanAnswerKind

    assert set(REQUEST_KINDS) == {k.value for k in PlanAnswerKind}
    assert set(rc.EXTRACT_SCHEMA["properties"]["answer_kind"]["enum"]) == set(REQUEST_KINDS)


def test_kind_decisions_are_spelled_as_the_route_sources() -> None:
    from nexus.plans.answer_kind import ROUTE_SOURCES

    assert set(rc.KIND_DECISIONS) - {"unknown"} == set(ROUTE_SOURCES)
    for q, hint in (("Which RDR introduced tumblers?", None), ("anything", "synthesis")):
        assert derive_request_coverage(q, explicit_kind=hint).kind_decision in rc.KIND_DECISIONS


def test_normalize_kind_is_the_golden_label_map() -> None:
    from nexus.plans.answer_kind import GOLDEN_LABEL_TO_KIND, PlanAnswerKind

    for label, kind in GOLDEN_LABEL_TO_KIND.items():
        assert rc.normalize_kind(label) == kind.value
    assert all(rc.normalize_kind(k.value) == k.value for k in PlanAnswerKind)
    assert rc.normalize_kind("nonsense") is None and rc.normalize_kind(7) is None


def test_the_active_project_seam_is_disclosure_only_and_points_at_the_wired_tier() -> None:
    """2.4 wired the tier (``nexus.plans.active_project``, an ``inferred`` ``OwnerResolution``); the seam
    here stays a recorded name and the docstring says so."""
    doc = " ".join((derive_request_coverage.__doc__ or "").split())
    assert "disclosure seam" in doc and "never used here" in doc
    assert "nexus.plans.active_project" in doc and "inferred" in doc and "OwnerResolution" in doc


# ── F8. smaller items ─────────────────────────────────────────────────────────────────────────────────────


def test_the_format_constraint_pattern_is_not_quadratic() -> None:
    import time

    text = "Answer " + "in " * 2600
    started = time.perf_counter()
    rc._constraint_kind(text)
    derive_request_coverage(text)
    assert time.perf_counter() - started < 0.25


def test_a_format_constraint_still_reads_as_one() -> None:
    cov = derive_request_coverage("Explain the cache. Answer concisely in about three short sentences.")
    assert [c.kind for c in cov.constraints] == ["format"]


def test_a_literal_control_character_in_the_question_survives_the_sentence_split() -> None:
    parts = rc._sentences("Handle the \u0001 marker. Then explain e.g. the cache.")
    assert parts[0] == "Handle the \u0001 marker." and "." not in parts[0].replace("marker.", "")
    assert any("e.g." in p for p in parts)


def _resolved(status: str):
    from tests.test_rdr232_lifecycle_stream import _resolution, _src

    return _resolution(_src(status=status))


def _unqualified_codes(status: str) -> list[str]:
    with patch("nexus.mcp.core._nx_answer_named_sources", AsyncMock(return_value=_resolved(status))), \
         patch("nexus.mcp.core._nx_answer_lifecycle_stream", AsyncMock(return_value=None)), \
         patch("nexus.mcp.core._nx_answer_primary_stream", AsyncMock(return_value=(None, None))):
        env, _ = _drive("State the gate history of RDR-084.", bindings={"source": "RDR-084"})
    return [a["code"] for a in env["request_coverage"]["ambiguities"]]


def test_a_uniquely_resolved_source_is_not_reported_unqualified() -> None:
    assert "source_unqualified" not in _unqualified_codes("resolved")


def test_an_unresolved_or_ambiguous_source_stays_reported_unqualified() -> None:
    assert "source_unqualified" in _unqualified_codes("candidates")


def test_the_cli_reference_documents_the_request_coverage_key() -> None:
    docs = (Path(__file__).resolve().parents[1] / "docs" / "cli-reference.md").read_text(encoding="utf-8")
    assert "ten RDR-232 keys" in docs and "nine RDR-232 keys" not in docs
    assert "- `request_coverage` (nexus-jypmj.2.2" in docs
    life = docs[docs.index("- `lifecycle_stream` (nexus-jypmj.1.7)"):]
    assert "gate, lifecycle or status facet" in life[: life.index("\n- `", 10)]
