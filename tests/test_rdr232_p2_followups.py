# SPDX-License-Identifier: AGPL-3.0-or-later
"""RDR-232 Phase 2 follow-ups (epic nexus-jypmj.2): the findings of the phase code review and critique.

Each test names the finding it holds. They run ``nx_answer`` itself on the strict fake catalog of
``test_rdr232_question_sources`` (tenant-free) and, where the stage under test is a helper, the helper.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest
from structlog.testing import capture_logs

from nexus.plans.request_coverage import RequestCoverage
from tests.test_rdr232_primary_evidence import ART_DOC
from tests.test_rdr232_question_sources import _run, _sources

# ── I2: the pre-match stages are isolated at their call site ─────────────────────────────────────────────

_SECRET = "private-question-words-xyzzy"


@pytest.fixture
def debug_logs():
    """Every level reaches the capture (nexus filters structlog at WARNING by default, so an INFO or DEBUG log
    of private text would otherwise pass unseen); the previous configuration is put back afterwards."""
    import structlog

    saved = structlog.get_config()["wrapper_class"]
    structlog.configure(wrapper_class=structlog.make_filtering_bound_logger(0))
    try:
        yield
    finally:
        structlog.configure(wrapper_class=saved)


def _boom(*_a: Any, **_k: Any) -> Any:
    raise RuntimeError(_SECRET)


#: stage -> (patch target, the logged stage name)
_STAGES = {
    "project_names": ("nexus.plans.request_coverage.project_name_candidates", "project_names"),
    "question_sources": ("nexus.plans.evidence.EvidenceRequest.with_question_sources", "question_sources"),
    "withdraw_unqualified": (
        "nexus.plans.request_coverage.RequestCoverage.without_unqualified_for", "withdraw_unqualified",
    ),
    "coverage_log": ("nexus.plans.request_coverage.log_fields", "coverage_log"),
    "active_project": ("nexus.plans.active_project.apply_active_project", "active_project"),
    "fallback_reason": ("nexus.plans.eligibility.fallback_reason", "fallback_reason"),
}


@pytest.mark.parametrize("stage", list(_STAGES))
def test_a_fault_in_a_pre_match_stage_leaves_the_stage_at_its_default_and_the_answer_goes_on(
    tmp_path: Path, stage: str,
) -> None:
    target, logged = _STAGES[stage]
    with capture_logs() as logs, patch(target, _boom):
        env, run, _life, _cat = _run(tmp_path)
    assert "error" not in env or not str(env.get("error", "")).startswith("Error"), env.get("error")
    assert env["final_text"], "the answer was produced"
    run.assert_awaited()
    failed = [e for e in logs if e["event"] == "nx_answer_pre_match_stage_failed"]
    assert [e["stage"] for e in failed] == [logged], failed
    assert failed[0]["error"] == "RuntimeError"
    assert _SECRET not in repr(logs), "the log carries the exception type only"


def test_the_stage_defaults_are_the_documented_ones(tmp_path: Path) -> None:
    # question_sources: no source is fed from the question
    with patch(_STAGES["question_sources"][0], _boom):
        env, *_ = _run(tmp_path / "a")
    assert env["evidence_request"] is None and env["named_sources"] is None
    # withdraw_unqualified: the ambiguity the coverage derived stays (nothing withdrew it)
    with patch(_STAGES["withdraw_unqualified"][0], _boom):
        env, *_ = _run(tmp_path / "b")
    assert (_sources(env)[0]["status"], _sources(env)[0]["tumbler"]) == ("resolved", ART_DOC)
    assert "source_unqualified" in {a["code"] for a in env["request_coverage"]["ambiguities"]}
    # active_project: no tier applied, so the RDR number is a candidates ambiguity
    with patch(_STAGES["active_project"][0], _boom):
        env, *_ = _run(tmp_path / "c")
    assert _sources(env)[0]["status"] == "candidates"
    # fallback_reason: the envelope reports no fallback reason
    with patch(_STAGES["fallback_reason"][0], _boom):
        env, *_ = _run(tmp_path / "d")
    assert env["plan_eligibility"].get("fallback_reason") is None


# ── critique S2: the inline planner is handed the coverage ───────────────────────────────────────────────

import nexus.mcp.core as core  # noqa: E402
from nexus.plans.request_coverage import Constraint, Facet  # noqa: E402


async def _planner_prompt(coverage: RequestCoverage | None, question: str = "q") -> str:
    seen: list[str] = []

    async def fake_dispatch(prompt, schema, timeout=300.0, model=None, **kw):  # noqa: ARG001
        seen.append(prompt)
        return {"steps": [{"tool": "search", "args": {"query": "x"}}]}

    with patch("nexus.operators.dispatch.claude_dispatch", fake_dispatch), \
         patch("nexus.mcp_infra.get_collection_names", lambda: []):
        await core._nx_answer_plan_miss(question, coverage=coverage)
    return seen[0]


@pytest.mark.asyncio
async def test_the_planner_prompt_names_the_requests_kind_facets_and_constraints() -> None:
    cov = RequestCoverage(
        answer_kind="synthesis", kind_decision="heuristic",
        facets=(Facet("f1", "the locked architecture"), Facet("f2", "the gate history")),
        constraints=(Constraint("c1", "do not cite current code", "no_current_code"),),
    )
    prompt = await _planner_prompt(cov)
    assert "The request asks for a synthesis answer." in prompt
    assert "Cover each of these: the locked architecture; the gate history" in prompt
    assert "The answer must also respect: do not cite current code" in prompt
    assert prompt.index("Question: q") < prompt.index("Cover each of these") < prompt.index("Return the plan as")


@pytest.mark.asyncio
@pytest.mark.parametrize("cov", [None, RequestCoverage(), RequestCoverage(kind_decision="heuristic")],
                         ids=["none", "empty", "unknown-kind-only"])
async def test_a_request_with_no_coverage_leaves_the_planner_prompt_unchanged(cov) -> None:
    baseline = await _planner_prompt(None)
    assert await _planner_prompt(cov) == baseline
    assert "The request asks for" not in baseline and "Cover each of these" not in baseline


@pytest.mark.asyncio
async def test_the_added_prompt_text_is_capped() -> None:
    facets = tuple(Facet(f"f{i}", "w" * 400) for i in range(40))
    constraints = tuple(Constraint(f"c{i}", "x" * 400, "format") for i in range(40))
    cov = RequestCoverage(answer_kind="audit", facets=facets, constraints=constraints)
    baseline = await _planner_prompt(None)
    prompt = await _planner_prompt(cov)
    assert len(prompt) - len(baseline) <= core._PLANNER_REQUEST_MAX_CHARS
    assert core._nx_answer_planner_request_block(cov).count("w" * 91) == 0, "each item is clipped"
    few = RequestCoverage(facets=tuple(Facet(f"f{i}", f"topic{i}") for i in range(core._PLANNER_REQUEST_MAX_ITEMS + 3)))
    assert "(and 3 more)" in core._nx_answer_planner_request_block(few)


def test_a_broken_coverage_object_adds_nothing_and_never_raises() -> None:
    class Broken:
        answer_kind = "lookup"
        facets = None  # tuple(None) raises
        constraints = ()

    assert core._nx_answer_planner_request_block(Broken()) == ""  # type: ignore[arg-type]


@pytest.mark.asyncio
async def test_the_planner_prompt_is_never_logged(debug_logs) -> None:
    cov = RequestCoverage(answer_kind="synthesis", facets=(Facet("f1", "private-facet-words-qwerty"),))
    with capture_logs() as logs:
        prompt = await _planner_prompt(cov, question="private-question-words-asdf")
    assert "private-facet-words-qwerty" in prompt
    assert "private-facet-words-qwerty" not in repr(logs) and "private-question-words-asdf" not in repr(logs)


# ── critique S5: a title written anywhere after the id reaches the veto ──────────────────────────────────

import time  # noqa: E402

from nexus.plans.request_coverage import (  # noqa: E402
    MAX_LISTED_SOURCES,
    MAX_SCAN_CHARS,
    derive_request_coverage,
    project_name_candidates,
)
from tests.test_rdr232_primary_evidence import NEXUS_DOC, NEXUS_OWNER  # noqa: E402

TITLE = "LIST PARSE Canonical Consolidation"


def _titles(question: str) -> list[tuple[str, str]]:
    return [(h.source, h.text) for h in derive_request_coverage(question, scope="rdr").source_hints if h.kind == "title"]


@pytest.mark.parametrize("question", [
    f"What did RDR-084 ({TITLE}) decide about the grammar?",
    f'What did RDR-084 "{TITLE}" decide about the grammar?',
    f"What did RDR-084 “{TITLE}” decide about the grammar?",
    f'What did RDR-084 titled "{TITLE}" decide about the grammar?',
    f"First read the background. Then explain what RDR-084: ({TITLE}) settled.",
    f"Synthesize RDR-084 ({TITLE}). State the gate history.",
    f"Synthesize RDR-084, {TITLE}. State the gate history.",
], ids=["paren", "quote", "curly-quote", "titled", "mid-question", "topic-paren", "topic-comma"])
def test_a_title_in_parentheses_or_quotes_after_the_id_is_read_wherever_it_stands(question: str) -> None:
    assert _titles(question) == [("RDR-084", TITLE)]


@pytest.mark.parametrize("question", [
    "What did RDR-084 (see RDR-090) decide?",
    "What did RDR-084 (draft) decide?",
    "What did RDR-084 (i.e. the gate history) decide?",
    "What did RDR-084 decide about the Plan Library?",
    "What did RDR-084 decide? (The Plan Library matters here.)",
    "What did RDR-084's 'Plan Library Growth' decide?",
], ids=["other-rdr", "one-word", "aside", "no-title", "paren-not-after-id", "single-quote"])
def test_what_is_not_a_title_is_not_read_as_one(question: str) -> None:
    assert _titles(question) == []


def test_a_title_after_the_id_vetoes_an_inference_from_a_nexus_session(tmp_path: Path) -> None:
    """The incident question with its title written in parentheses instead of after a cue verb: still ART's
    title, still not Nexus's, so a reliable Nexus session gets the candidates, not Nexus's RDR-084."""
    question = f"What did RDR-084 ({TITLE}) decide about the locked architecture?"
    env, run, *_ = _run(tmp_path, question=question, project=NEXUS_OWNER)
    (src,) = _sources(env)
    assert src["status"] == "candidates" and src["tumbler"] is None and "title" in src["detail"], src
    assert NEXUS_DOC in {c["tumbler"] for c in src["candidates"]}
    assert env["evidence_request"]["owner"]["status"] == "none"


# ── code S1: the text scans are bounded ──────────────────────────────────────────────────────────────────

HUGE = "".join(f"Compare RDR-{i % 900:03d} to RDR-{(i + 1) % 900:03d} in the nexus project. " for i in range(16_000))[:750_000]


def _best_scan_seconds(question: str) -> float:
    best = float("inf")
    for _ in range(3):
        started = time.perf_counter()
        derive_request_coverage(question, scope="rdr")
        project_name_candidates(question)
        best = min(best, time.perf_counter() - started)
    return best


def test_a_750_kb_question_is_scanned_in_bounded_time() -> None:
    """The scan cost stops growing at MAX_SCAN_CHARS. Measured against the same question cut to the limit,
    on the same host and load, rather than against a wall-clock bound: an unbounded scan costs about 12x
    the cut one at 750 KB, a bounded one about the same, and a fixed 1.0 s ceiling failed at 1.02 s on a
    busy CI runner (develop 2bb124e98, shard 6)."""
    assert len(HUGE) == 750_000
    capped = HUGE[:MAX_SCAN_CHARS]
    huge_s, capped_s = _best_scan_seconds(HUGE), _best_scan_seconds(capped)
    assert huge_s < 3 * capped_s + 0.05, (
        f"{huge_s:.3f}s against {capped_s:.3f}s at the limit: the scans must not grow with the question "
        f"past {MAX_SCAN_CHARS} characters")
    cov = derive_request_coverage(HUGE, scope="rdr")
    assert project_name_candidates(HUGE) and cov.named_sources
    assert "scan_limit" in {a.code for a in cov.ambiguities}, "the unread tail is disclosed"


def test_a_question_within_the_scan_limit_reports_no_scan_limit() -> None:
    cov = derive_request_coverage("Explain RDR-084 in detail. " * 500, scope="rdr")
    assert "scan_limit" not in {a.code for a in cov.ambiguities}


def test_ids_past_the_scan_limit_are_not_read_and_those_before_it_are() -> None:
    q = "Explain RDR-001. " + ("Background filler text. " * 4000) + "Then RDR-777."
    assert len(q) > MAX_SCAN_CHARS
    cov = derive_request_coverage(q, scope="rdr")
    assert [s.value for s in cov.named_sources] == ["RDR-001"]
    assert any(a.code == "scan_limit" and a.subject == "question" for a in cov.ambiguities)


def test_the_envelope_lists_a_bounded_number_of_ids_and_counts_the_rest(tmp_path: Path) -> None:
    ids = [f"RDR-{i:03d}" for i in range(100, 100 + 120)]
    question = "Explain " + ", ".join(ids) + "."
    env, *_ = _run(tmp_path, question=question)
    cov = env["request_coverage"]
    assert cov["named_source_count"] == len(ids)
    assert len(cov["named_sources"]) == MAX_LISTED_SOURCES
    unq = [a for a in cov["ambiguities"] if a["code"] == "source_unqualified"]
    assert len(unq) <= MAX_LISTED_SOURCES
    assert [a for a in cov["ambiguities"] if a["code"] == "scan_limit" and a["subject"] == "sources"]
    wire = env["evidence_request"]
    assert len(wire["sources"]) == MAX_LISTED_SOURCES and wire["sources_omitted"] == len(ids) - MAX_LISTED_SOURCES
    ns = env["named_sources"]
    assert len(ns["dropped"]) + ns["dropped_omitted"] == len(ids) - 3
    assert len(ns["dropped"]) <= 20
    notice = next(ln for ln in env["final_text"].splitlines() if "beyond the cap" in ln)
    assert f"and {ns['dropped_omitted']} more" in notice


# ── code S2: the lifecycle stream fails open for a source read from the question too ─────────────────────

UNCERTAIN = "RDR-084, with care, as thoroughly as possible, please."


def test_uncertain_facets_and_a_question_named_rdr_fail_open_on_the_lifecycle_stream() -> None:
    cov = derive_request_coverage(UNCERTAIN, scope="rdr")
    assert (cov.facets_certain, cov.source_origin) == (False, "question")
    assert cov.wants_lifecycle_stream is True
    # certain facets with no gate, lifecycle or status topic: still not read
    assert derive_request_coverage("Explain RDR-084.", scope="rdr").wants_lifecycle_stream is False
    # uncertain facets and no RDR source: nothing to read the lifecycle of
    assert derive_request_coverage("Be careful, be clear, be brief, please.", scope="rdr").wants_lifecycle_stream is False


def test_nx_answer_reads_the_lifecycle_of_a_question_named_rdr_when_the_facets_are_uncertain(tmp_path: Path) -> None:
    env, _run_mock, life, _cat = _run(tmp_path, question=UNCERTAIN)
    assert env["request_coverage"]["wants_lifecycle_stream"] is True
    life.assert_awaited()
    env2, _r2, life2, _c2 = _run(tmp_path / "certain", question="Explain RDR-084.")
    assert env2["request_coverage"]["wants_lifecycle_stream"] is False
    life2.assert_not_awaited()


# ── code S4: active-project detection and the owner names are reused within the process ──────────────────

from nexus.plans import active_project as ap  # noqa: E402
from tests.test_rdr232_active_project import _request, _session  # noqa: E402


@pytest.fixture
def fresh_ap_caches():
    ap.reset_active_project_caches_for_tests()
    yield
    ap.reset_active_project_caches_for_tests()


def _catalog_gets(cat) -> tuple[int, int, int]:
    """``(owner_for_repo, get_owner_by_prefix, list_owners)`` calls the fake catalog has served."""
    kinds = [c[0] for c in cat.calls]
    return kinds.count("owner_for_repo"), cat._probes, kinds.count("list_owners")


@pytest.mark.asyncio
async def test_a_second_rdr_naming_call_makes_no_extra_owner_gets(tmp_path: Path, monkeypatch, fresh_ap_caches) -> None:
    cat, _t3, environ, cwd = _session(tmp_path)
    monkeypatch.setenv("CLAUDE_PROJECT_DIR", cwd)
    monkeypatch.chdir(cwd)
    with patch.object(core, "_get_catalog", return_value=cat):
        first = await core._nx_answer_active_project(_request(scope="rdr"), ("Summarize",), scope="rdr", deadline=None)
        gets_after_first = _catalog_gets(cat)
        second = await core._nx_answer_active_project(_request(scope="rdr"), ("Summarize",), scope="rdr", deadline=None)
    assert first.active_project["applied"] is True and second.owner == first.owner
    assert all(n >= 1 for n in gets_after_first), ("the first call must have asked", gets_after_first)
    assert _catalog_gets(cat) == gets_after_first, "the second call asked the catalog again"


@pytest.mark.asyncio
async def test_a_changed_project_directory_or_catalog_is_looked_up_again(tmp_path: Path, monkeypatch, fresh_ap_caches) -> None:
    (tmp_path / "one").mkdir()
    (tmp_path / "two").mkdir()
    cat, _t3, environ, cwd = _session(tmp_path / "one")
    cat2, _t32, environ2, cwd2 = _session(tmp_path / "two")
    monkeypatch.setenv("CLAUDE_PROJECT_DIR", cwd)
    monkeypatch.chdir(cwd)
    with patch.object(core, "_get_catalog", return_value=cat):
        await core._nx_answer_active_project(_request(scope="rdr"), (), scope="rdr", deadline=None)
        before = _catalog_gets(cat)[:2]
        # the host moved the session: both the variable and the directory change
        monkeypatch.setenv("CLAUDE_PROJECT_DIR", cwd2)
        monkeypatch.chdir(cwd2)
        await core._nx_answer_active_project(_request(scope="rdr"), (), scope="rdr", deadline=None)
    assert _catalog_gets(cat)[:2] != before, "a new project directory is a new lookup"
    with patch.object(core, "_get_catalog", return_value=cat2):
        await core._nx_answer_active_project(_request(scope="rdr"), (), scope="rdr", deadline=None)
    assert _catalog_gets(cat2)[0] >= 1, "another catalog handle is a new lookup"


@pytest.mark.asyncio
async def test_an_unreliable_detection_is_never_kept(tmp_path: Path, monkeypatch, fresh_ap_caches) -> None:
    """A project that was not yet indexed must be found once it is: only a reliable result is reused."""
    cat, _t3, environ, cwd = _session(tmp_path, owner=None)
    monkeypatch.setenv("CLAUDE_PROJECT_DIR", cwd)
    monkeypatch.chdir(cwd)
    with patch.object(core, "_get_catalog", return_value=cat):
        first = await core._nx_answer_active_project(_request(scope="rdr"), (), scope="rdr", deadline=None)
        n = _catalog_gets(cat)[0]
        second = await core._nx_answer_active_project(_request(scope="rdr"), (), scope="rdr", deadline=None)
    assert first.active_project["applied"] is False and second.active_project["applied"] is False
    assert _catalog_gets(cat)[0] > n


def test_the_owner_names_are_reused_for_the_ttl_and_a_failed_read_is_not_kept(fresh_ap_caches, monkeypatch) -> None:
    class Cat:
        reads = 0
        fail = False

        def list_owners(self, **_kw):
            Cat.reads += 1
            if Cat.fail:
                raise RuntimeError("x")
            return [{"name": "ART"}]

    cat = Cat()
    assert ap.registered_owner_names(cat, ttl=60) == frozenset({"art"})
    assert ap.registered_owner_names(cat, ttl=60) == frozenset({"art"}) and Cat.reads == 1
    assert ap.registered_owner_names(cat) == frozenset({"art"}) and Cat.reads == 2, "no ttl, no reuse"
    clock = {"t": 1000.0}
    monkeypatch.setattr(ap, "_clock", lambda: clock["t"])
    ap.reset_active_project_caches_for_tests()
    ap.registered_owner_names(cat, ttl=60)
    clock["t"] += 61
    ap.registered_owner_names(cat, ttl=60)
    assert Cat.reads == 4, "past the ttl it is read again"
    ap.reset_active_project_caches_for_tests()
    Cat.fail = True
    assert ap.registered_owner_names(cat, ttl=60) is None
    Cat.fail = False
    assert ap.registered_owner_names(cat, ttl=60) == frozenset({"art"}), "the failure was not kept"


# ── code S5: trace=False and the Phase 1 failure logs ────────────────────────────────────────────────────

_FAILED_LOGS = ("named_source_find_failed", "named_source_resolve_failed", "named_source_frontmatter_id_failed")


def _failing_find(cat) -> None:
    def find(*_a: Any, **_k: Any) -> Any:
        raise RuntimeError("catalog down while reading RDR-084")

    cat.find = find  # type: ignore[method-assign]


def _source_failure_logs(tmp_path: Path, *, trace: bool, bound: bool) -> list[dict]:
    kw: dict[str, Any] = {"question": PLAIN, "configure": _failing_find, "trace": trace}
    if bound:
        kw["bindings"] = {"content_type": "rdr", "source": "RDR-084"}
    with capture_logs() as logs:
        env, *_ = _run(tmp_path, **kw)
    assert env["named_sources"]["sources"][0]["status"] == "unresolved", "the premise: the lookup failed"
    return [e for e in logs if e["event"] in _FAILED_LOGS]


PLAIN = "Explain the architecture of RDR-084."


def test_a_source_read_from_the_question_is_not_in_the_failure_log_under_trace_false(tmp_path: Path) -> None:
    (event,) = _source_failure_logs(tmp_path, trace=False, bound=False)
    assert event["source"] == "[from the question]" and event["error"] == "RuntimeError"
    assert "RDR-084" not in repr(event), event


def test_a_source_read_from_the_question_is_logged_under_trace_true(tmp_path: Path) -> None:
    (event,) = _source_failure_logs(tmp_path, trace=True, bound=False)
    assert event["source"] == "RDR-084" and "catalog down" in event["error"]


@pytest.mark.parametrize("trace", [False, True])
def test_a_source_the_caller_bound_is_logged_with_its_id_either_way(tmp_path: Path, trace: bool) -> None:
    (event,) = _source_failure_logs(tmp_path, trace=trace, bound=True)
    assert event["source"] == "RDR-084"


def test_the_redaction_does_not_outlive_the_stage(tmp_path: Path) -> None:
    from nexus.plans import primary_evidence as pe

    _source_failure_logs(tmp_path, trace=False, bound=False)
    assert pe._REDACT_SOURCE_LOGS.get() is False


# ── observations: an invalid kind hint is recorded; the parameter text says discovery/listing ────────────

@pytest.mark.parametrize("hint", ["summary", "", 5, ["lookup"], "discovery listing"])
def test_an_invalid_answer_kind_hint_is_ignored_and_recorded(hint: Any) -> None:
    cov = derive_request_coverage("Which paper reported the value 0.82?", scope="", explicit_kind=hint)
    assert cov.kind_decision != "explicit"
    (amb,) = [a for a in cov.ambiguities if a.code == "kind_hint_invalid"]
    assert amb.subject == "answer_kind" and "discovery/listing" in amb.detail
    assert str(hint) not in amb.detail or hint in ("", 5), "the value is input; it is not echoed"
    assert {"code": "kind_hint_invalid"} == amb.to_wire(redact=True)
    assert "kind_hint_invalid" in [a["code"] for a in cov.to_wire()["ambiguities"]]


@pytest.mark.parametrize("hint", [None, "synthesis", "Synthesis ", "discovery", "discovery/listing", "unknown"])
def test_a_valid_or_absent_answer_kind_hint_records_nothing(hint: Any) -> None:
    cov = derive_request_coverage("Which paper reported the value 0.82?", scope="", explicit_kind=hint)
    assert "kind_hint_invalid" not in [a.code for a in cov.ambiguities]


def test_nx_answer_carries_an_invalid_hint_to_the_envelope_and_out_of_the_plan_filter() -> None:
    dims, hint = core._nx_answer_split_kind_hint({"verb": "research", "answer_kind": "summary"})
    assert dims == {"verb": "research"} and hint == "summary"
    assert core._nx_answer_split_kind_hint({"answer_kind": 5}) == (None, 5), "a non-string hint reaches the coverage too"


def test_the_dimensions_parameter_text_says_discovery_listing() -> None:
    import inspect

    from nexus.mcp.core import nx_answer

    text = repr(inspect.signature(nx_answer).parameters["dimensions"].annotation)
    assert "discovery/listing" in text
