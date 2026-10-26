# SPDX-License-Identifier: AGPL-3.0-or-later
"""RDR-232 Phase 2 (nexus-jypmj.2.4), the review round: what the code review and the critique found.

Tenant-free, on the strict fake catalog of ``test_rdr232_primary_evidence`` (ART 1.2 and Nexus 1.1, each
holding an RDR-084; ART's is titled "list parse canonical consolidation", Nexus's "plan library growth").
``nx_answer`` itself runs with the real resolver and the real tier; the model-free plan runner is a recorder.

The rules, and what holds each:

* B2 a range ("RDR-078 through RDR-095") keeps its ends as sources and says the rest was not expanded;
* B1 the question's title VETOES an inference that contradicts it and never picks a document; with no title,
  a reliable project of another repo still resolves its own RDR, disclosed as inferred;
* B1 the Desktop and Cowork limit and the remedy are stated in the reason, the ``bindings`` text, the
  cli-reference and the CHANGELOG;
* I1 a collection-name scope declines the tier; F1/F2 only a registered owner name is a project hint;
* F4 a number another project holds keeps a ``source_inferred`` ledger line; F5 a plain unique resolution of
  a source read from the question prints no notice; I4/I5/F9 the stages never raise and share one budget;
* S1 to S3 and F6: the tier runs off the loop, checks the host's variable before the catalog, reports an
  inference the resolver set aside, and runs its default path unpatched.
"""

from __future__ import annotations

import asyncio
import threading
import time
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import yaml

from nexus.mcp import core
from nexus.plans.active_project import REMEDY, ActiveProject, apply_active_project
from nexus.plans.evidence import OwnerResolution, SourceRange, SourceRef, build_evidence_request
from nexus.plans.primary_evidence import (
    DEFAULT_CAPS,
    NamedSourceResolution,
    ResolvedSource,
    resolve_named_sources,
    title_hint_match,
)
from nexus.plans.request_coverage import derive_request_coverage, project_name_candidates
from tests.test_rdr232_active_project import SessionCatalog
from tests.test_rdr232_primary_evidence import (
    ART_COL,
    ART_DOC,
    ART_OWNER,
    NEXUS_COL,
    NEXUS_DOC,
    NEXUS_OWNER,
    FakeCatalog,
    FakeT3,
    _doc,
    _two_projects,
)
from tests.test_rdr232_question_sources import INCIDENT_BINDINGS, PLAIN_QUESTION, _run, _sources
from tests.test_rdr232_request_coverage import G06, GOLDEN

REPO = Path(__file__).resolve().parents[1]
G09 = next(q["text"] for q in yaml.safe_load(GOLDEN.read_text(encoding="utf-8"))["queries"] if q["qid"].startswith("G09"))
RDR084 = SourceRef.parse("RDR-084")
INFERRED_ART = OwnerResolution("inferred", ART_OWNER, (("active_project", ART_OWNER),))


def _notices(env: dict) -> list[str]:
    """The named-source notices of the answer text (the plan-shape notices of the fake plan are not these)."""
    return [ln for ln in env["final_text"].splitlines() if ln.startswith("[evidence request") and "named source" in ln]


def _codes(env: dict) -> set[str]:
    return {a["code"] for a in env["request_coverage"]["ambiguities"]}


# ── B2: a range is never silently cut to its ends ───────────────────────────────────────────────────────


RANGE_LINE = "range RDR-078 to RDR-095 not expanded: RDR-079 to RDR-094 not resolved"


def test_g09_keeps_its_two_ends_and_says_the_range_was_not_expanded(tmp_path: Path) -> None:
    docs = (("1.1.9078", "docs/rdr/rdr-078-plan-first.md"), ("1.1.9095", "docs/rdr/rdr-095-scope-aware.md"))
    env, _r, _l, _c = _run(tmp_path, question=G09, extra_docs=docs)
    ns = env["named_sources"]
    assert [s["source"]["value"] for s in ns["sources"]] == ["RDR-078", "RDR-095"], "the ends stay the sources"
    assert ns["ranges"] == [{"start": "RDR-078", "end": "RDR-095", "not_resolved": "RDR-079 to RDR-094"}]
    assert env["request_coverage"]["source_ranges"] == ns["ranges"]
    assert f"[evidence request: {RANGE_LINE}]" in env["final_text"].splitlines()
    assert ns["dropped"] == [], "the range is disclosed on its own, not as a cap overflow"


@pytest.mark.parametrize("wording", [
    "RDR-078 through RDR-095", "RDR-078 to RDR-095", "RDR-078 until RDR-095", "RDR-078 thru RDR-095",
    "RDR-078 - RDR-095", "RDR-078–RDR-095", "rdr-078 — rdr-95", "RDR-078-RDR-095",
])
def test_every_wording_of_a_range_is_seen(wording: str) -> None:
    cov = derive_request_coverage(f"Trace the sequence {wording} and its gates.", scope="rdr")
    assert [(r.start, r.end) for r in cov.source_ranges] == [(78, 95)], wording
    assert [s.value for s in cov.named_sources] == ["RDR-078", "RDR-095"]
    assert cov.source_ranges[0].disclosure() == RANGE_LINE


@pytest.mark.parametrize("text", ["RDR-078 and RDR-095", "RDR-078, RDR-095", "RDR-078 to 10 phases", "RDR-078 only"])
def test_two_ids_that_are_not_a_range_are_not_reported_as_one(text: str) -> None:
    assert derive_request_coverage(f"Compare {text} in detail.", scope="rdr").source_ranges == ()


def test_a_range_with_nothing_between_its_ends_says_nothing_and_one_number_between_is_named() -> None:
    assert SourceRange(78, 79).disclosure() == "" and SourceRange(78, 78).disclosure() == ""
    assert SourceRange(78, 80).disclosure() == "range RDR-078 to RDR-080 not expanded: RDR-079 not resolved"
    assert SourceRange(95, 78).between() == "RDR-079 to RDR-094", "a reversed range names the same gap"


def test_a_source_binding_wins_over_the_text_so_its_ranges_are_not_reported() -> None:
    bound = build_evidence_request({"source": "RDR-155"}, scope="", name_lookup=str)
    assert derive_request_coverage(G09, scope="rdr", evidence_request=bound).source_ranges == ()


# ── B1: the title vetoes a contradicting inference, and never picks ──────────────────────────────────────


def test_the_incident_question_from_a_reliable_nexus_session_gives_candidates_not_nexus_rdr_084(
    tmp_path: Path,
) -> None:
    """The question's title ("LIST PARSE Canonical Consolidation") fits ART's document and not Nexus's. A
    reliable Nexus session must not turn that into Nexus's RDR-084: both documents are named and nothing is
    read as evidence. The title only vetoes: ART's document is not chosen either."""
    env, run, _l, _c = _run(tmp_path, project=NEXUS_OWNER)
    (src,) = _sources(env)
    assert src["status"] == "candidates" and src["tumbler"] is None and src["inferred"] is False
    assert sorted(c["tumbler"] for c in src["candidates"]) == [NEXUS_DOC, ART_DOC]
    assert "title" in src["detail"] and "not inferred" in src["detail"]
    wire = env["evidence_request"]
    assert wire["owner"]["status"] == "none", "an inference the title vetoed is not reported as made"
    assert wire["active_project"]["applied"] is False and "set aside" in wire["active_project"]["reason"]
    assert "reserved_stream" not in run.await_args.kwargs and env["primary_evidence"] is None
    assert "source_unqualified" in _codes(env)
    assert any("ambiguous" in ln and NEXUS_DOC in ln and ART_DOC in ln for ln in _notices(env))


def test_the_same_question_from_a_reliable_art_session_still_resolves_to_art(tmp_path: Path) -> None:
    env, _r, _l, _c = _run(tmp_path, project=ART_OWNER)
    (src,) = _sources(env)
    assert (src["status"], src["tumbler"], src["inferred"], src["foreign"]) == ("resolved", ART_DOC, True, 1)
    assert env["evidence_request"]["active_project"]["applied"] is True


def test_with_no_title_a_reliable_session_of_another_project_resolves_its_own_rdr_disclosed_as_inferred(
    tmp_path: Path,
) -> None:
    """RDR-232 section 1 (line 618): no reliable active project means no inference; with one, and nothing in the
    question that contradicts it, the project's own RDR resolves and the envelope says it was inferred."""
    env, _r, _l, _c = _run(tmp_path, question=PLAIN_QUESTION, project=NEXUS_OWNER)
    (src,) = _sources(env)
    assert (src["status"], src["tumbler"], src["inferred"]) == ("resolved", NEXUS_DOC, True)
    assert env["evidence_request"]["owner"]["status"] == "inferred"
    assert any("inferred" in ln and NEXUS_DOC in ln for ln in _notices(env))


def test_a_title_that_fits_neither_document_vetoes_nothing(tmp_path: Path) -> None:
    question = "Explain RDR-084, Widget Frobnication Protocol. Cite the sources."
    assert [h.kind for h in derive_request_coverage(question, scope="rdr").source_hints] == ["title"], "the premise"
    env, _r, _l, _c = _run(tmp_path, question=question, project=NEXUS_OWNER)
    (src,) = _sources(env)
    assert (src["status"], src["tumbler"], src["inferred"]) == ("resolved", NEXUS_DOC, True)


def test_the_title_hint_scores_by_share_of_its_words() -> None:
    cat, t3 = FakeCatalog(), FakeT3()
    _two_projects(cat, t3)
    art, nexus = cat.entries[0], cat.entries[1]
    assert title_hint_match(art, ["LIST PARSE Canonical Consolidation"]) == 1.0
    assert title_hint_match(nexus, ["LIST PARSE Canonical Consolidation"]) == 0.0
    assert title_hint_match(art, ["Plan Library Growth", "list parse"]) == 1.0, "the best hint counts"
    assert title_hint_match(art, []) == 0.0 and title_hint_match(art, ["of the"]) == 0.0


def test_the_resolver_vetoes_only_an_inferred_qualifier_and_only_for_the_session_projects_own_miss() -> None:
    cat, t3 = FakeCatalog(), FakeT3()
    _two_projects(cat, t3)
    hints = {"RDR-084": ["LIST PARSE Canonical Consolidation"]}
    nexus_inferred = OwnerResolution("inferred", NEXUS_OWNER, (("active_project", NEXUS_OWNER),))
    (vetoed,) = resolve_named_sources([RDR084], nexus_inferred, cat=cat, title_hints=hints).sources
    assert vetoed.status == "candidates" and vetoed.tumbler is None and not vetoed.inferred
    # the title fits the session's own document: the inference stands
    (kept,) = resolve_named_sources([RDR084], INFERRED_ART, cat=cat, title_hints=hints).sources
    assert (kept.status, kept.tumbler, kept.inferred) == ("resolved", ART_DOC, True)
    # an explicit owner is never moved by a title, even one that fits the other project's document
    explicit = OwnerResolution("resolved", NEXUS_OWNER, (("owner", NEXUS_OWNER),))
    (held,) = resolve_named_sources([RDR084], explicit, cat=cat, title_hints=hints).sources
    assert (held.status, held.tumbler) == ("resolved", NEXUS_DOC)
    # no hint, no veto
    (plain,) = resolve_named_sources([RDR084], nexus_inferred, cat=cat).sources
    assert (plain.status, plain.tumbler, plain.inferred) == ("resolved", NEXUS_DOC, True)


def test_the_host_limit_and_the_remedy_are_stated_where_users_read_them(tmp_path: Path) -> None:
    """Four places carry the same facts: Claude Desktop and Cowork start the server without the project
    variable, so the session's project is never inferred there, and the remedy is owner or project."""
    env, _r, _l, _c = _run(tmp_path, declare=False)
    reason = env["evidence_request"]["active_project"]["reason"]
    assert "CLAUDE_PROJECT_DIR is unset" in reason and "Desktop" in reason and "Cowork" in reason and REMEDY in reason
    tools = asyncio.run(core.mcp.list_tools())
    (answer,) = [t for t in tools if t.name == "nx_answer"]
    bindings = answer.inputSchema["properties"]["bindings"]["description"]
    changelog = (REPO / "CHANGELOG.md").read_text(encoding="utf-8")
    cli = (REPO / "docs" / "cli-reference.md").read_text(encoding="utf-8")
    entry = changelog[changelog.index("qualified by the session's project"):]
    entry = entry[: entry.index("\n- **")]
    section = cli[cli.index("- `evidence_request` (nexus-jypmj.1.2)"):cli.index("- `named_sources` (nexus-jypmj.1.4)")]
    for place, text in (("bindings", bindings), ("CHANGELOG", entry), ("cli-reference", section)):
        assert "Cowork" in text and "Desktop" in text, place
        assert "owner" in text and "project" in text, place
    assert "inferred" in bindings and "CLAUDE_PROJECT_DIR" in bindings
    assert len(answer.description) < 2048


# ── I1: a collection-name scope declines the tier ──────────────────────────────────────────────────────


def test_a_collection_name_scope_declines_the_tier_even_with_a_reliable_project(tmp_path: Path) -> None:
    tier = apply_active_project(
        OwnerResolution("none"), ActiveProject(True, owner=ART_OWNER, name="ART"), scope=ART_COL,
    )
    assert tier.applied is False and tier.owner.status == "none" and "collection" in tier.reason
    env, _r, _l, _c = _run(tmp_path, scope=ART_COL, project=ART_OWNER)
    (src,) = _sources(env)
    assert src["status"] == "candidates" and src["inferred"] is False, "the project was not inferred"
    assert env["evidence_request"]["owner"]["status"] == "none"


@pytest.mark.asyncio
async def test_the_stage_does_not_ask_the_catalog_for_a_collection_name_scope() -> None:
    request = build_evidence_request({"source": "RDR-084"}, scope=ART_COL, name_lookup=str)
    with patch.object(core, "_get_catalog", side_effect=AssertionError("the catalog must not be asked")), \
         patch("nexus.plans.active_project.detect_active_project", side_effect=AssertionError("not consulted")):
        assert await core._nx_answer_active_project(request, (), scope=ART_COL, deadline=None) is request


# ── F1 and F2: a project hint counts only if it names a registered owner ───────────────────────────────


@pytest.mark.parametrize("phrase", [
    "in detail", "for review", "from the gate record", "in depth", "for the migration plan",
])
def test_a_phrase_after_the_id_is_not_a_project_and_does_not_block_the_inference(tmp_path: Path, phrase: str) -> None:
    question = f"What does RDR-084 say {phrase}?"
    assert project_name_candidates(question), "the premise: the phrase does read like a project name"
    env, _r, _l, _c = _run(tmp_path, question=question, project=ART_OWNER)
    (src,) = _sources(env)
    assert (src["status"], src["tumbler"], src["inferred"]) == ("resolved", ART_DOC, True), phrase


@pytest.mark.parametrize("question", [
    "Summarize Nexus RDR-084.", "Summarize the nexus RDR-084.", "What does NEXUS RDR-084 decide?",
    "What does RDR-084 in nexus decide?", "What does nexus's RDR-084 decide?",
])
def test_a_registered_project_the_question_names_in_any_case_vetoes_the_inference(tmp_path: Path, question: str) -> None:
    env, _r, _l, _c = _run(tmp_path, question=question, project=ART_OWNER)
    (src,) = _sources(env)
    assert src["status"] == "candidates" and src["tumbler"] is None, question
    assert "nexus" in env["evidence_request"]["active_project"]["reason"].lower()


def test_a_name_registered_under_another_spelling_counts_too(tmp_path: Path) -> None:
    """``the conexus RDR-084`` names a project when the catalog has an owner called conexus."""
    def register(cat: SessionCatalog) -> None:
        cat.extra_owners["1.3"] = "conexus"

    env, _r, _l, _c = _run(tmp_path, question="Summarize the conexus RDR-084.", project=ART_OWNER, configure=register)
    assert _sources(env)[0]["status"] == "candidates"
    env2, _r2, _l2, _c2 = _run(tmp_path / "unregistered", question="Summarize the conexus RDR-084.", project=ART_OWNER)
    assert _sources(env2)[0]["status"] == "resolved", "an unregistered word is not a project"


def test_the_tier_filters_hints_by_the_registry_case_insensitively() -> None:
    nexus_session = ActiveProject(True, owner=NEXUS_OWNER, name="nexus")
    registry = {"art", "nexus", "conexus"}
    none = OwnerResolution("none")
    assert apply_active_project(none, nexus_session, scope="", project_names=("NEXUS",), registered=registry).applied
    assert apply_active_project(none, nexus_session, scope="", project_names=("detail", "Summarize"), registered=registry).applied
    declined = apply_active_project(none, nexus_session, scope="", project_names=("Art",), registered=registry)
    assert declined.applied is False and "Art" in declined.reason and REMEDY in declined.reason
    assert apply_active_project(none, nexus_session, scope="", project_names=("conexus",), registered=registry).applied is False
    assert apply_active_project(none, nexus_session, scope="", project_names=("Art",), registered=set()).applied is True


# ── F4: the inference over a number another project holds stays in the ledger ──────────────────────────


def test_an_inference_over_a_shared_number_keeps_a_source_inferred_ambiguity(tmp_path: Path) -> None:
    env, _r, _l, _c = _run(tmp_path, project=ART_OWNER)
    assert _sources(env)[0]["inferred"] is True
    amb = [a for a in env["request_coverage"]["ambiguities"] if a["code"] == "source_inferred"]
    assert len(amb) == 1 and amb[0]["subject"] == "RDR-084" and "1 document(s) in other projects" in amb[0]["detail"]
    assert "source_unqualified" not in _codes(env), "the doubt is restated as inferred, not left twice"


def test_an_inference_over_a_number_no_other_project_holds_leaves_no_ambiguity(tmp_path: Path) -> None:
    env, _r, _l, _c = _run(
        tmp_path, question="What does RDR-161 say?", project=ART_OWNER,
        extra_docs=(("1.2.5161", "docs/rdr/rdr-161-solo.md", ART_COL),),
    )
    (src,) = _sources(env)
    assert (src["status"], src["tumbler"], src["inferred"], src["foreign"]) == ("resolved", "1.2.5161", True, 0)
    assert not ({"source_unqualified", "source_inferred"} & _codes(env))
    assert _notices(env) == [], "inferred over a number no other project holds: nothing is in doubt, nothing is printed"


# ── F5 and I3: a plain resolution of a source read from the question prints nothing ───────────────────


def test_a_plain_unique_resolution_of_a_question_source_prepends_no_notice(tmp_path: Path) -> None:
    env, _r, _l, _c = _run(
        tmp_path, question="What does RDR-160 say?", project=ART_OWNER,
        extra_docs=(("1.1.5160", "docs/rdr/rdr-160-solo.md"),),
    )
    (src,) = _sources(env)
    assert src["status"] == "resolved" and src["origin"] == "question", "the premise: it resolved"
    assert _notices(env) == [], env["final_text"]


def test_a_plain_resolution_with_no_reliable_project_prints_no_notice_either(tmp_path: Path) -> None:
    """The number is held by one project only, so there is nothing to tell apart even with no project known."""
    env, _r, _l, _c = _run(
        tmp_path, question="What does RDR-160 say?", declare=False,
        extra_docs=(("1.1.5160", "docs/rdr/rdr-160-solo.md"),),
    )
    (src,) = _sources(env)
    assert (src["status"], src["tumbler"], src["qualified"], src["inferred"]) == ("resolved", "1.1.5160", False, False)
    assert _notices(env) == []


def test_a_question_source_the_inference_chose_among_projects_keeps_its_notice(tmp_path: Path) -> None:
    env, _r, _l, _c = _run(tmp_path, project=ART_OWNER)
    (line,) = [ln for ln in _notices(env) if "inferred" in ln]
    assert "1 document(s) in other projects carry the number" in line and ART_DOC in line


def test_a_source_the_caller_bound_keeps_its_notice_even_when_the_resolution_is_plain(tmp_path: Path) -> None:
    env, _r, _l, _c = _run(
        tmp_path, question="What does it say?", project=ART_OWNER,
        bindings={**INCIDENT_BINDINGS, "source": "RDR-160"}, extra_docs=(("1.1.5160", "docs/rdr/rdr-160-solo.md"),),
    )
    assert _sources(env)[0]["status"] == "resolved" and _sources(env)[0]["origin"] == "explicit"
    assert _notices(env), "a binding's notices are unchanged"


# ── I4: the catalog failing never fails the answer ──────────────────────────────────────────────────────


def test_a_catalog_that_raises_on_find_and_on_the_project_lookup_still_returns_an_answer(tmp_path: Path) -> None:
    def break_catalog(cat: SessionCatalog) -> None:
        cat.fail_find = True
        cat.fail_owner_lookup = True

    env, _r, _l, _c = _run(tmp_path, configure=break_catalog)
    (src,) = _sources(env)
    assert src["status"] == "unresolved"
    assert env["evidence_request"]["active_project"]["applied"] is False
    assert "RuntimeError" in env["evidence_request"]["active_project"]["reason"]
    assert any("unresolved" in ln for ln in _notices(env)) and env["final_text"].strip()


@pytest.mark.asyncio
async def test_a_failure_anywhere_in_the_named_source_stage_degrades_to_unresolved_with_the_type_only() -> None:
    request = build_evidence_request(None, scope="rdr", name_lookup=str).with_question_sources(
        (RDR084,), (SourceRange(78, 95),),
    )
    with patch.object(core, "_nx_answer_named_sources_stage", side_effect=RuntimeError("secret /private/path")):
        res = await core._nx_answer_named_sources(request, deadline=None)
    (src,) = res.sources
    assert src.status == "unresolved" and "RuntimeError" in src.detail and "secret" not in src.detail
    assert res.origin == "question" and res.ranges == (SourceRange(78, 95),)
    assert any("unresolved" in ln for ln in res.disclosures())
    assert await core._nx_answer_named_sources(build_evidence_request(None, scope="", name_lookup=str)) is None


def test_the_answer_returns_when_the_named_source_stage_itself_raises(tmp_path: Path) -> None:
    with patch.object(core, "_nx_answer_named_sources_stage", side_effect=ValueError("boom")):
        env, _r, _l, _c = _run(tmp_path)
    assert _sources(env)[0]["status"] == "unresolved" and "ValueError" in _sources(env)[0]["detail"]


# ── I5 and F9: the two stages share one budget ─────────────────────────────────────────────────────────


def test_the_stage_deadline_is_one_ceiling_from_now_or_the_answers_own_if_sooner() -> None:
    now = time.monotonic()
    ceiling = DEFAULT_CAPS.max_seconds
    assert now + ceiling - 1 <= core._nx_answer_source_stage_deadline(None) <= time.monotonic() + ceiling
    assert core._nx_answer_source_stage_deadline(now + 3) == now + 3
    assert core._nx_answer_source_stage_deadline(now + 1000) <= time.monotonic() + ceiling


@pytest.mark.parametrize("budget", [None, 4.0])
def test_both_stages_get_the_same_deadline_so_they_cannot_each_take_a_full_ceiling(
    tmp_path: Path, budget: float | None,
) -> None:
    active = AsyncMock(wraps=core._nx_answer_active_project)
    named = AsyncMock(wraps=core._nx_answer_named_sources)
    start = time.monotonic()
    with patch.object(core, "_nx_answer_active_project", active), patch.object(core, "_nx_answer_named_sources", named):
        _run(tmp_path, budget_seconds=budget)
    first, second = active.await_args.kwargs["deadline"], named.await_args.kwargs["deadline"]
    assert first is not None and first == second, (first, second)
    cap = DEFAULT_CAPS.max_seconds if budget is None else budget
    assert first <= time.monotonic() + cap and first > start, "never past one ceiling (or the answer's own budget)"
    if budget is not None:
        assert first <= start + budget + 0.5


# ── S1: off the loop, and the deadline branch ──────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_the_detection_runs_in_a_worker_thread_not_on_the_loop() -> None:
    seen: list[int] = []

    def detect(_c: object = None, **_kw: object) -> ActiveProject:
        seen.append(threading.get_ident())
        return ActiveProject(False, reason="recorded")

    request = build_evidence_request({"source": "RDR-084"}, scope="rdr", name_lookup=str)
    with patch("nexus.plans.active_project.detect_active_project", detect):
        got = await core._nx_answer_active_project(request, (), scope="rdr", deadline=None)
    assert seen and seen[0] != threading.get_ident(), "detection blocked the event loop's own thread"
    assert got.active_project["reason"] == "recorded"


@pytest.mark.asyncio
async def test_a_detection_still_running_at_the_deadline_is_not_reliable_and_says_so() -> None:
    release = threading.Event()

    def slow(_c: object = None, **_kw: object) -> ActiveProject:
        release.wait(5)
        return ActiveProject(True, owner=ART_OWNER, name="ART")

    request = build_evidence_request({"source": "RDR-084"}, scope="rdr", name_lookup=str)
    try:
        with patch("nexus.plans.active_project.detect_active_project", slow), \
             patch.object(core, "_nx_answer_seconds_left", return_value=0.0):
            got = await core._nx_answer_active_project(request, (), scope="rdr", deadline=None)
    finally:
        release.set()
    assert got.owner.status == "none" and got.active_project["applied"] is False
    assert "stopped at the deadline" in got.active_project["reason"]


# ── S2 and F6: the default path, unpatched, and the variable is checked before the catalog ────────────


@pytest.mark.asyncio
async def test_a_host_that_named_no_project_never_touches_the_catalog(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("CLAUDE_PROJECT_DIR", raising=False)
    get_catalog = MagicMock(name="get_catalog")
    request = build_evidence_request({"source": "RDR-084"}, scope="rdr", name_lookup=str)
    with patch.object(core, "_get_catalog", get_catalog):
        got = await core._nx_answer_active_project(request, ("Nexus",), scope="rdr", deadline=None)
    assert get_catalog.call_count == 0, "the catalog was fetched before the host's variable was read"
    assert got.active_project["applied"] is False and "is unset" in got.active_project["reason"]


def test_the_default_detection_path_resolves_a_reliable_session_unpatched(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No seam is replaced: ``os.environ``, the working directory, git identity and the catalog's owner lookup
    are the production ones, over a tmp repo and the fake catalog."""
    env, _r, _l, cat = _run(tmp_path, monkeypatch=monkeypatch)
    (src,) = _sources(env)
    assert (src["status"], src["tumbler"], src["inferred"]) == ("resolved", ART_DOC, True)
    assert env["evidence_request"]["active_project"]["applied"] is True
    assert any(c[0] == "owner_for_repo" for c in cat.calls), "the owner was looked up by the repository's identity"


def test_the_default_detection_path_without_the_variable_is_the_candidates_ambiguity(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    env, _r, _l, cat = _run(tmp_path, monkeypatch=monkeypatch, declare=False)
    (src,) = _sources(env)
    assert src["status"] == "candidates"
    assert "is unset" in env["evidence_request"]["active_project"]["reason"]
    assert not any(c[0] in ("owner_for_repo", "list_owners") for c in cat.calls)


# ── S3: an inference the resolver set aside is not reported as made ────────────────────────────────────


def test_an_owner_probe_that_fails_after_detection_leaves_the_envelope_saying_no_inference(tmp_path: Path) -> None:
    def fail_second_probe(cat: SessionCatalog) -> None:
        cat.fail_owner_probe_from = 2  # detection's probe passes; the resolver's own fails

    env, _r, _l, _c = _run(tmp_path, configure=fail_second_probe, question=PLAIN_QUESTION)
    (src,) = _sources(env)
    assert src["inferred"] is False and src["status"] == "candidates"
    wire = env["evidence_request"]
    assert wire["owner"]["status"] == "none"
    assert wire["active_project"]["applied"] is False and "set aside" in wire["active_project"]["reason"]


def test_the_envelope_keeps_the_inference_when_a_source_used_it() -> None:
    request = build_evidence_request({"source": "RDR-084"}, scope="rdr", name_lookup=str)
    inferred = request.__class__(**{**request.__dict__, "owner": INFERRED_ART, "active_project": {"applied": True}})
    used = NamedSourceResolution(sources=(ResolvedSource(ref=RDR084, status="resolved", tumbler=ART_DOC, inferred=True),))
    unused = NamedSourceResolution(sources=(ResolvedSource(ref=RDR084, status="candidates"),))
    assert core._nx_answer_settle_inference(inferred, used) is inferred
    assert core._nx_answer_settle_inference(inferred, None) is inferred
    settled = core._nx_answer_settle_inference(inferred, unused)
    assert settled.owner.status == "none" and settled.active_project["applied"] is False
    assert core._nx_answer_settle_inference(request, unused) is request, "no inference was made, nothing to settle"
