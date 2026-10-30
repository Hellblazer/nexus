# SPDX-License-Identifier: AGPL-3.0-or-later
"""RDR-232 Phase 2 (nexus-jypmj.2.4, assumption A5), second half: an RDR id that appears only in the question.

nexus-jypmj.2.2 records the RDR ids a question names (``RequestCoverage.named_sources``, ``source_origin``
``question``); until this bead only a ``source`` binding reached named-source resolution. These tests pin
the call exactly as the incident was filed: the ART RDR-084 question, ``scope="rdr"``, ``content_type`` as
its only binding, no ``source``. They run ``nx_answer`` itself with the real resolver, the real
active-project tier and the strict fake catalog of ``test_rdr232_primary_evidence`` (the ART and the Nexus
RDR-084, both registered). Tenant-free.

The rules, and the test that holds each:

* a text source takes the same tiers as a binding (explicit owner/project, owner scope, active project);
* an explicit ``source`` binding replaces the text sources, a malformed one included;
* a text source never counts as an owner: only an ``owner``/``project`` binding narrows the secondary streams;
* at most :data:`~nexus.plans.primary_evidence.QUESTION_SOURCE_CAP` text sources are resolved per request and
  the rest are disclosed as not resolved;
* a resolved text source gets the primary stream; the T2 lifecycle stream still needs a gate, lifecycle or
  status facet.
"""

from __future__ import annotations

import inspect
from collections.abc import Callable
from contextlib import ExitStack
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import yaml

from nexus.mcp import core
from nexus.plans.active_project import detect_active_project
from nexus.plans.evidence import OwnerResolution, SourceRef, build_evidence_request
from nexus.plans.owner_narrowing import request_owners
from nexus.plans.primary_evidence import QUESTION_SOURCE_CAP, NamedSourceResolution
from nexus.plans.request_coverage import ACTIVE_PROJECT_TIER, derive_request_coverage
from tests.test_rdr232_active_project import SessionCatalog, _session
from tests.test_rdr232_caller_scope import MULTI_STEP_PLAN, _drive_nx_answer, _match, _plan_run_result
from tests.test_rdr232_primary_evidence import ART_DOC, ART_OWNER, NEXUS_DOC, NEXUS_OWNER, _doc
from tests.test_rdr232_request_coverage import G06, GOLDEN

#: The incident's bindings: the plan-visible ``content_type`` only. No ``source``, no ``owner``, no ``project``.
INCIDENT_BINDINGS = {"content_type": "rdr"}
#: A question that names an RDR, a project-free one, and no gate, lifecycle or status topic.
PLAIN_QUESTION = "Explain the architecture of RDR-084."


def _run(
    tmp_path: Path, *, question: str = G06, scope: str = "rdr", bindings: dict[str, Any] | None = INCIDENT_BINDINGS,
    project: str | None = ART_OWNER, declare: bool = True, extra_docs: tuple[tuple[str, ...], ...] = (),
    configure: Callable[[SessionCatalog], None] | None = None, monkeypatch: pytest.MonkeyPatch | None = None,
    budget_seconds: float | None = None, trace: bool = True,
) -> tuple[dict, AsyncMock, AsyncMock, SessionCatalog]:
    """``nx_answer`` on the fake catalog, the model-free plan runner replaced by a recorder. *project* is the
    owner the session's directory is registered under (``None``: never indexed); *declare* False leaves
    ``CLAUDE_PROJECT_DIR`` unset (a host that did not name the session's project). *extra_docs* are
    ``(tumbler, path[, collection])`` (the Nexus collection by default); *configure* edits the catalog before the
    call. With *monkeypatch* the tier's detection is NOT replaced: the process's own environment and working
    directory are set to the session's (or the variable removed), and the default path runs. Returns
    ``(envelope, plan_run, lifecycle_stream, catalog)``."""
    tmp_path.mkdir(parents=True, exist_ok=True)
    cat, t3, environ, cwd = _session(tmp_path, project)
    for tumbler, path, *rest in extra_docs:
        _doc(cat, t3, tumbler, path, [f"# {path}\nbody"], collection=rest[0] if rest else "rdr__1-1__bge-base-en-v15-768__v1")
    if configure is not None:
        configure(cat)
    steps = [{"ids": ["c1"], "tumblers": ["1.2.1"]}, {"text": "The decision is X [1] after review of the evidence."}]
    run = AsyncMock(return_value=_plan_run_result(list(steps), []))
    life = AsyncMock(return_value=None)
    seen = environ if declare else {}
    with ExitStack() as stack:
        if monkeypatch is None:
            stack.enter_context(patch(
                "nexus.plans.active_project.detect_active_project",
                side_effect=lambda c=None, **kw: detect_active_project(
                    c, get_catalog=kw.get("get_catalog"), environ=seen, getcwd=lambda: cwd),
            ))
        else:
            monkeypatch.chdir(cwd)
            if declare:
                monkeypatch.setenv("CLAUDE_PROJECT_DIR", cwd)
            else:
                monkeypatch.delenv("CLAUDE_PROJECT_DIR", raising=False)
        stack.enter_context(patch("nexus.mcp.core._nx_answer_lifecycle_stream", life))
        stack.enter_context(patch("nexus.plans.runner.plan_run", run))
        stack.enter_context(patch("nexus.plans.primary_evidence.t3_chunk_fetcher", return_value=t3.fetch))
        stack.enter_context(patch("nexus.mcp.core._get_t3", return_value=MagicMock()))
        env, _ = _drive_nx_answer(
            _match(MULTI_STEP_PLAN, default_bindings={"intent": "q"}), scope=scope, structured=True,
            question=question, bindings=bindings, catalog=cat, budget_seconds=budget_seconds,
            trace=trace,
        )
    return env, run, life, cat


def _sources(env: dict) -> list[dict]:
    return env["named_sources"]["sources"]


# ── the incident call, unmodified ────────────────────────────────────────────────────────────────────────


def test_the_unmodified_art_call_with_a_reliable_art_project_resolves_art_rdr_084(tmp_path: Path) -> None:
    """The ART RDR-084 question, ``scope="rdr"``, no source binding, the session in the ART project: the
    RDR id read from the question resolves to ART's document, inferred, and its sections are the primary
    stream. Nexus's RDR-084 is not a candidate."""
    env, run, _life, _cat = _run(tmp_path, project=ART_OWNER)
    (src,) = _sources(env)
    assert (src["status"], src["tumbler"], src["inferred"]) == ("resolved", ART_DOC, True), src
    assert src["origin"] == "question" and env["named_sources"]["origin"] == "question"
    assert NEXUS_DOC not in {c["tumbler"] for c in src["candidates"]}
    assert env["request_coverage"]["source_origin"] == "question"
    wire = env["evidence_request"]
    assert wire["source_origin"] == "question" and [s["value"] for s in wire["sources"]] == ["RDR-084"]
    assert wire["owner"]["status"] == "inferred" and wire["owner"]["tumbler"] == ART_OWNER
    assert wire["active_project"]["applied"] is True and wire["active_project"]["name"] == "ART"
    # the primary stream is the resolved document's text, and it is handed to the runner
    (call,) = run.await_args_list
    stream = call.kwargs["reserved_stream"]
    assert stream.items and any("ART RDR-084" in i.text for i in stream.items), stream
    assert not any("Nexus RDR-084" in i.text for i in stream.items)
    assert env["primary_evidence"]["selected_items"] >= 1
    assert any("resolved to " + ART_DOC in line and "active project" in line for line in env["final_text"].splitlines())
    assert "source_unqualified" not in {a["code"] for a in env["request_coverage"]["ambiguities"]}, (
        "a source resolved to one document has nothing left to tell apart"
    )


@pytest.mark.parametrize("why", ["undeclared", "never_indexed", "wrong_project_named_in_text"])
def test_the_same_call_with_no_reliable_active_project_is_the_candidates_ambiguity_naming_both(
    tmp_path: Path, why: str,
) -> None:
    kw: dict[str, Any] = {}
    if why == "undeclared":
        kw["declare"] = False
    elif why == "never_indexed":
        kw["project"] = None
    else:  # a reliable Nexus session, and a question that says ART: the text is not a qualifier, but it vetoes the inference
        kw["project"] = NEXUS_OWNER
        kw["question"] = G06.replace("Synthesize RDR-084", "Synthesize ART RDR-084")
    env, run, _life, _cat = _run(tmp_path, **kw)
    (src,) = _sources(env)
    assert src["status"] == "candidates" and src["tumbler"] is None, src
    assert sorted(c["tumbler"] for c in src["candidates"]) == [NEXUS_DOC, ART_DOC]
    assert src["origin"] == "question"
    assert env["evidence_request"]["owner"]["status"] == "none"
    assert env["evidence_request"]["active_project"]["applied"] is False
    assert ART_DOC in env["final_text"] and NEXUS_DOC in env["final_text"] and "ambiguous" in env["final_text"]
    assert "reserved_stream" not in run.await_args.kwargs, "nothing was picked, so nothing is read as primary evidence"
    assert env["primary_evidence"] is None


def test_the_unmodified_call_matches_the_golden_incident_question_verbatim() -> None:
    """The recorded incident question (bench/queries/golden_v1.yaml, run 722), read from the file here, is the
    exact string these tests send, and it names no project."""
    recorded = yaml.safe_load(GOLDEN.read_text(encoding="utf-8"))["queries"]
    (g06,) = [q["text"] for q in recorded if q["qid"] == "G06-synthesis-art-rdr084"]
    assert G06 == g06, "the question under test is not the recorded one"
    assert inspect.signature(_run).parameters["question"].default == g06, "_run's default call is not the filed one"
    assert g06.startswith("Synthesize RDR-084, LIST PARSE Canonical Consolidation.")
    assert [h.kind for h in derive_request_coverage(g06, scope="rdr").source_hints] == ["title"], "no project is named"


# ── precedence: an explicit binding wins and replaces the text ───────────────────────────────────────────


def test_an_explicit_source_binding_replaces_the_sources_read_from_the_question(tmp_path: Path) -> None:
    env, _run_, _life, _cat = _run(tmp_path, bindings={**INCIDENT_BINDINGS, "source": NEXUS_DOC})
    (src,) = _sources(env)
    assert (src["status"], src["tumbler"]) == ("resolved", NEXUS_DOC) and env["named_sources"]["origin"] == "explicit"
    assert "origin" not in env["evidence_request"].get("sources", [{}])[0] and "source_origin" not in env["evidence_request"]
    assert env["request_coverage"]["source_origin"] == "explicit"


def test_a_malformed_source_binding_is_still_a_binding_and_the_text_is_not_fed_in_its_place(tmp_path: Path) -> None:
    env, run, _life, _cat = _run(tmp_path, bindings={**INCIDENT_BINDINGS, "source": "not-a-source"})
    assert env["named_sources"] is None, "the caller named a source; it was refused and disclosed, not replaced"
    assert env["evidence_request"]["source_error"]
    assert "reserved_stream" not in run.await_args.kwargs


def test_an_explicit_project_binding_outranks_the_active_project_for_a_question_source(tmp_path: Path) -> None:
    env, run, _life, _cat = _run(tmp_path, bindings={**INCIDENT_BINDINGS, "owner": NEXUS_OWNER}, project=ART_OWNER)
    (src,) = _sources(env)
    assert (src["status"], src["tumbler"], src["qualified"], src["inferred"]) == ("resolved", NEXUS_DOC, True, False)
    assert env["evidence_request"]["owner"]["status"] == "resolved"
    assert "active_project" not in env["evidence_request"], "an explicit tier qualified the source; the project was not consulted"
    assert "reserved_stream" in run.await_args.kwargs


# ── a text source never narrows by owner ─────────────────────────────────────────────────────────────────


def test_a_source_read_from_the_question_never_narrows_the_secondary_streams(tmp_path: Path) -> None:
    """The resolved ART document is not an owner qualifier: only an ``owner``/``project`` binding is explicit, and the
    envelope's ``owner_narrowing.applied`` stays False. The one-source rule (nexus-4xmpv, nexus-jypmj) is a separate
    narrowing, and it narrows an INFERRED source only when no other project carries the number (Sam, 2026-10-30):
    the Nexus RDR-084 exists beside the ART one here, so the run is not narrowed and ``single_source_narrowing`` says
    why (the unit-level cases with no foreign document are in ``test_rdr232_primary_evidence.py``)."""
    env, run, _life, _cat = _run(tmp_path, project=ART_OWNER)
    assert _sources(env)[0]["status"] == "resolved"
    assert env["evidence_diagnostics"]["scope_check"]["owner_narrowing"]["applied"] is False
    (src,) = _sources(env)
    assert src["inferred"] is True and src["foreign"] == 1, "non-vacuity: another project carries the number"
    narrowing = env["single_source_narrowing"]
    assert narrowing["applied"] is False and narrowing["confinement"] is None and narrowing["document"] == ""
    assert "1 document(s) in other projects carry the same number" in narrowing["reason"]
    assert "retrieval was not narrowed to it" in env["final_text"]
    bindings = dict(run.await_args.args[1])
    assert "_nx_owners" not in bindings and bindings["_nx_scope"] == "rdr", "the caller's own scope, not the collection"
    assert "document_confinement" not in run.await_args.kwargs
    # control: the same call with an explicit owner binding does narrow, to that owner alone
    env2, run2, _l, _c = _run(tmp_path / "again", bindings={**INCIDENT_BINDINGS, "owner": NEXUS_OWNER})
    assert dict(run2.await_args.args[1])["_nx_owners"] == NEXUS_OWNER
    assert env2["evidence_diagnostics"]["scope_check"]["owner_narrowing"]["applied"] is True


@pytest.mark.parametrize("origin", ["question", "source", "text", "active_project", "named_source"])
def test_an_owner_contributed_by_the_question_is_not_an_explicit_owner(origin: str) -> None:
    """Only the ``owner`` and ``project`` origins narrow, even for a resolved qualifier."""
    request = MagicMock(owner=OwnerResolution("resolved", "1.2", ((origin, "1.2"),)))
    assert request_owners(request) == ()
    explicit = MagicMock(owner=OwnerResolution("resolved", "1.2", (("project", "1.2"),)))
    assert request_owners(explicit) == ("1.2",)


# ── the fan-out is bounded and the overflow disclosed ────────────────────────────────────────────────────


def test_at_most_the_cap_of_question_sources_is_resolved_and_the_rest_are_disclosed(tmp_path: Path) -> None:
    docs = tuple((f"1.1.{9000 + n}", f"docs/rdr/rdr-{n:03d}-topic.md") for n in (78, 80, 88, 91, 95))
    question = "Trace the evolution across RDR-078, RDR-080, RDR-088, RDR-091 and RDR-095, and cite the gate for each."
    env, _run_, _life, cat = _run(tmp_path, question=question, extra_docs=docs)
    ns = env["named_sources"]
    assert QUESTION_SOURCE_CAP == 3
    assert [s["source"]["value"] for s in ns["sources"]] == ["RDR-078", "RDR-080", "RDR-088"]
    assert ns["dropped"] == ["RDR-091", "RDR-095"], "the overflow is named, not silently dropped"
    assert all(s["origin"] == "question" for s in ns["sources"])
    assert any("RDR-091" in line and "RDR-095" in line and "beyond the cap of 3" in line and "question" in line
               for line in env["final_text"].splitlines()), env["final_text"]
    asked = {c[1].get("query") or c[1].get("title") or str(c[1]) for c in cat.calls if c[0] == "find"}
    assert not any("091" in q or "095" in q for q in asked), "an over-cap id costs no catalog lookup"


def test_the_cap_applies_to_question_sources_only_an_explicit_binding_keeps_the_resolver_cap(tmp_path: Path) -> None:
    docs = tuple((f"1.1.{9000 + n}", f"docs/rdr/rdr-{n:03d}-topic.md") for n in (78, 80, 88, 91, 95))
    bound = ",".join(f"RDR-{n:03d}" for n in (78, 80, 88, 91, 95))
    env, _r, _l, _c = _run(tmp_path, bindings={**INCIDENT_BINDINGS, "source": bound, "owner": NEXUS_OWNER}, extra_docs=docs)
    assert len(env["named_sources"]["sources"]) == 5 and env["named_sources"]["dropped"] == []


@pytest.mark.asyncio
async def test_the_cap_and_origin_hold_when_the_lookup_hits_the_deadline() -> None:
    """The deadline path builds its own resolution: it keeps the question cap, names the overflow, and says
    the sources were read from the question."""
    request = build_evidence_request(None, scope="rdr", name_lookup=str).with_question_sources(
        tuple(SourceRef.parse(f"RDR-{n:03d}") for n in (70, 71, 72, 73, 74))
    )
    with patch.object(core, "_nx_answer_seconds_left", return_value=0.0), \
         patch.object(core, "_get_catalog", return_value=None):
        res = await core._nx_answer_named_sources(request, deadline=None)
    assert res.origin == "question" and len(res.sources) == QUESTION_SOURCE_CAP
    assert res.dropped == ("RDR-073", "RDR-074") and all(s.status == "unresolved" for s in res.sources)
    assert "beyond the cap of 3" in " ".join(res.disclosures())


# ── streams ──────────────────────────────────────────────────────────────────────────────────────────────


def test_the_lifecycle_stream_still_needs_a_gate_lifecycle_or_status_facet(tmp_path: Path) -> None:
    """The 2.2 rule is unchanged for a text source: the gate-history question reads the T2 stream, the plain
    architecture question does not, though both resolved their source."""
    env, _r, life, _c = _run(tmp_path, question=G06)
    assert env["request_coverage"]["wants_lifecycle_stream"] is True
    life.assert_awaited_once()
    assert [s.tumbler for s in life.await_args.args[0].resolved()] == [ART_DOC]

    env2, _r2, life2, _c2 = _run(tmp_path / "plain", question=PLAIN_QUESTION)
    assert env2["request_coverage"]["wants_lifecycle_stream"] is False
    assert _sources(env2)[0]["status"] == "resolved" and _sources(env2)[0]["tumbler"] == ART_DOC
    life2.assert_not_awaited()


def test_a_question_that_names_no_rdr_resolves_nothing_and_reads_nothing(tmp_path: Path) -> None:
    env, run, life, cat = _run(tmp_path, question="Which documents discuss widgets?")
    assert env["named_sources"] is None and env["evidence_request"] is None
    assert "reserved_stream" not in run.await_args.kwargs
    assert [c for c in cat.calls if c[0] in ("find", "owner_for_repo")] == []


# ── the unit seams ───────────────────────────────────────────────────────────────────────────────────────


def test_the_tier_is_wired() -> None:
    assert ACTIVE_PROJECT_TIER == "wired"


def test_a_question_source_request_wire_carries_its_origin_and_a_bound_one_does_not() -> None:
    bound = build_evidence_request({"source": "RDR-084"}, scope="", name_lookup=str)
    assert "source_origin" not in bound.to_wire() and bound.source_origin == "explicit"
    bare = build_evidence_request(None, scope="rdr", name_lookup=str)
    assert bare.to_wire() is None, "no source and no binding: the envelope field stays absent"
    fed = bare.with_question_sources((SourceRef.parse("RDR-084"),))
    assert fed.to_wire()["source_origin"] == "question" and fed.requested is False
    assert [s.value for s in fed.sources] == ["RDR-084"] and fed.source == SourceRef.parse("RDR-084")


def test_question_sources_are_only_fed_to_a_request_with_no_binding() -> None:
    refs = (SourceRef.parse("RDR-084"),)
    bound = build_evidence_request({"source": "RDR-155"}, scope="", name_lookup=str)
    assert bound.with_question_sources(refs) is bound
    broken = build_evidence_request({"source": "nope"}, scope="", name_lookup=str)
    assert broken.with_question_sources(refs) is broken
    assert build_evidence_request(None, scope="", name_lookup=str).with_question_sources(()).sources == ()
    tumbler = (SourceRef.parse(NEXUS_DOC),)
    assert build_evidence_request(None, scope="", name_lookup=str).with_question_sources(tumbler).sources == ()


def test_the_resolution_wire_names_its_origin() -> None:
    assert NamedSourceResolution().origin == "explicit"
    assert NamedSourceResolution(origin="question").to_wire()["origin"] == "question"
