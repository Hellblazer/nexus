# SPDX-License-Identifier: AGPL-3.0-or-later
"""Semantic content assertions for shipped seed plans.

Review critique Critical #2: ``test_e2e_scenario_seeds.py`` asserts only
``isinstance(step_out, dict)``. A seed that ranks tumbler address
strings against a criterion passes that invariant while producing
garbage — the model ranks `"1.2.3"` vs `"2.1.4"` and emits a
justification referencing address digits.

This module pins the STRONGER invariant: after the hydration step
``store_get_many`` lands, operator steps receive real document content,
and their structured outputs reference that content — not addresses.

The tests run against a deterministic stub dispatcher seeded with
fixed documents. No live claude; no network. They run in every CI
pass (NOT ``@pytest.mark.integration``) so a regression back to the
tumbler-as-content state fires immediately.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml


_SEED_DIR = Path(__file__).resolve().parents[2] / "nx" / "plans" / "builtin"


# ── Deterministic stub dispatcher ─────────────────────────────────────────


_FIXTURE_DOCS = {
    "doc-a": (
        "Consensus in Delos relies on a reconfigurable replication "
        "layer where the log segment is decoupled from the total order."
    ),
    "doc-b": (
        "The plan runner resolves $stepN.<field> references at dispatch "
        "time, letting operator steps compose over prior retrieval output."
    ),
    "doc-c": (
        "Reviewer agents must check drift against superseded RDRs before "
        "accepting a change; the decision-evolution traversal surfaces this."
    ),
}


def _stub_dispatcher():
    """Build a sync dispatcher that emits realistic structured shapes.

    Behaviour:
      * ``search``/``query`` — return a fixed {ids, tumblers, distances,
        collections} shape with 2 docs per call.
      * ``traverse`` — mirror the RDR-078 contract.
      * ``store_get_many`` — return real content for the known fixture
        IDs; empty string + missing entry otherwise.
      * Operator tools — emit contract shapes whose values REFERENCE
        the content the stub saw (so a test can assert "summarize.text
        contains Delos" when Delos was in step1's hydrated content).
    """
    captured_contents: list[list[str]] = []

    def dispatch(tool: str, args: dict):
        if tool == "search":
            return {
                "ids": ["doc-a", "doc-b"],
                "tumblers": ["1.1.1", "1.1.2"],
                "distances": [0.1, 0.15],
                "collections": ["knowledge__test", "knowledge__test"],
            }
        if tool == "query":
            return {
                "ids": ["doc-c"], "tumblers": ["1.2.1"],
                "distances": [0.2], "collections": ["docs__test"],
            }
        if tool == "traverse":
            return {
                "tumblers": ["1.1.3"], "ids": [],
                "collections": ["knowledge__test"],
            }
        if tool == "store_get_many":
            ids = args.get("ids") or []
            if not isinstance(ids, list):
                ids = [s.strip() for s in str(ids).split(",") if s.strip()]
            contents = [_FIXTURE_DOCS.get(i, "") for i in ids]
            missing = [i for i in ids if i not in _FIXTURE_DOCS]
            captured_contents.append(contents)
            return {"contents": contents, "missing": missing}
        if tool == "rank":
            # Contract returns {ranked}. Justification MUST reference
            # real content to prove the operator wasn't handed tumblers.
            incoming = args.get("inputs") or []
            sample = (incoming[0][:60] if incoming else "") if isinstance(incoming, list) else ""
            return {
                "ranked": [
                    {
                        "rank": 1, "score": 0.9, "input_index": 0,
                        "justification": (
                            f"top pick discusses content beginning "
                            f"'{sample}'" if sample
                            else "empty inputs — no content to rank"
                        ),
                    },
                ],
            }
        if tool == "summarize":
            incoming = args.get("inputs") or []
            if not isinstance(incoming, list):
                incoming = []
            joined = " / ".join(str(x)[:40] for x in incoming if x)
            return {
                "text": f"Summary of hydrated content: {joined}",
                "citations": [],
            }
        if tool == "compare":
            incoming = args.get("inputs") or []
            if not isinstance(incoming, list):
                incoming = []
            return {
                "agreements": [
                    f"both reference {str(incoming[0])[:40]}"
                    if incoming else "no inputs"
                ],
                "conflicts": [],
                "gaps": [],
            }
        if tool == "extract":
            incoming = args.get("inputs") or []
            if not isinstance(incoming, list):
                incoming = []
            fields = [f.strip() for f in str(args.get("fields") or "").split(",")]
            return {
                "extractions": [
                    {f: str(incoming[0])[:40] if incoming else None for f in fields},
                ],
            }
        if tool == "generate":
            incoming = args.get("inputs") or []
            if not isinstance(incoming, list):
                incoming = []
            joined = " ".join(str(x)[:30] for x in incoming if x)
            return {
                "text": f"Generated: {joined}",
                "citations": [],
            }
        if tool == "plan_search":
            return {"text": "no plans"}
        raise AssertionError(f"unexpected tool: {tool}")

    async def async_dispatch(tool: str, args: dict):
        return dispatch(tool, args)

    async_dispatch.captured_contents = captured_contents  # type: ignore[attr-defined]
    return async_dispatch


@pytest.fixture()
def library(tmp_path: Path):
    from nexus.db.migrations import _add_plan_dimensional_identity
    from nexus.db.t2.plan_library import PlanLibrary

    lib = PlanLibrary(tmp_path / "plans.db")
    _add_plan_dimensional_identity(lib.conn)
    lib.conn.commit()
    return lib


@pytest.fixture()
def loaded_library(library, tmp_path: Path):
    from nexus.plans.loader import load_all_tiers

    plugin_root = _SEED_DIR.parents[1]
    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    load_all_tiers(
        plugin_root=plugin_root, repo_root=repo_root, library=library,
    )
    return library


def _load_match(library, seed_name: str):
    """Look up the plan by (project, dimensions) identity and hand a
    Match back."""
    import json as _json

    from nexus.plans.match import Match
    from nexus.plans.schema import canonical_dimensions_json

    seed = yaml.safe_load((_SEED_DIR / seed_name).read_text()) or {}
    dims = seed.get("dimensions") or {}
    project_label = "" if dims.get("scope") == "global" else dims.get("scope", "")
    row = library.get_plan_by_dimensions(
        project=project_label,
        dimensions=canonical_dimensions_json(dims),
    )
    assert row is not None, f"seed {seed_name!r} not seeded"

    body = _json.loads(row["plan_json"])
    return Match(
        plan_id=row["id"], name=row.get("name") or seed_name,
        description=row.get("query") or "", confidence=1.0,
        dimensions=dims,
        tags=row.get("tags") or "", plan_json=row["plan_json"],
        required_bindings=list(body.get("required_bindings") or []),
        optional_bindings=list(body.get("optional_bindings") or []),
        default_bindings=_json.loads(row.get("default_bindings") or "null") or {},
        parent_dims=None,
    )


# ── Regression guard: operators receive content, not tumblers ─────────────


@pytest.mark.asyncio
async def test_analyze_seed_hydrates_before_rank_and_summarize(
    loaded_library,
) -> None:
    """Critical #1: analyze-default's rank/summarize steps receive
    real document content via the ``store_get_many`` hydration step
    — NOT the tumbler address strings they used to be fed."""
    from nexus.plans.runner import plan_run

    dispatcher = _stub_dispatcher()
    match = _load_match(loaded_library, "analyze-default.yml")
    result = await plan_run(
        match, {"area": "consensus", "criterion": "relevance"},
        dispatcher=dispatcher,
    )

    # Step 4 is store_get_many; its `contents` must be real doc text.
    hydrate = result.steps[3]
    assert "contents" in hydrate
    joined = " ".join(hydrate["contents"])
    assert "Delos" in joined or "plan runner" in joined, (
        "hydration step must return real document content"
    )

    # Step 5 is rank; justification must reference hydrated content.
    ranked_step = result.steps[4]
    assert ranked_step["ranked"], "rank must produce at least one row"
    justification = ranked_step["ranked"][0]["justification"]
    # A justification that quoted tumblers would contain digits/dots
    # like "1.1.1" or "2.1.4" as its primary content; one that quotes
    # hydrated text will contain recognizable vocabulary.
    assert "Delos" in justification or "plan runner" in justification or "reviewer" in justification.lower(), (
        f"rank.justification must reference hydrated content, got: {justification!r}"
    )

    # Step 6 is summarize; text must reference hydrated content.
    summary = result.steps[5]["text"]
    assert "Delos" in summary or "plan runner" in summary, (
        f"summarize.text must reference hydrated content, got: {summary!r}"
    )


@pytest.mark.asyncio
async def test_debug_seed_hydrates_before_summarize(loaded_library) -> None:
    """debug-default — query → hydrate → summarize; summary text must
    reference the hydrated RDR content, not the tumbler address."""
    from nexus.plans.runner import plan_run

    match = _load_match(loaded_library, "debug-default.yml")
    result = await plan_run(
        match,
        {"failing_path": "src/nexus/x.py", "symptom": "NullPointerException"},
        dispatcher=_stub_dispatcher(),
    )

    # Step 2 is store_get_many.
    assert "contents" in result.steps[1]
    # Step 3 is summarize; content must be real.
    summary = result.steps[2]["text"]
    assert "drift" in summary.lower() or "reviewer" in summary.lower(), (
        f"debug summary must reference hydrated RDR content, got: {summary!r}"
    )


@pytest.mark.asyncio
async def test_review_seed_extract_receives_content_not_tumblers(
    loaded_library,
) -> None:
    """review-default's extract step receives hydrated content; its
    extractions must carry field values derived from that content,
    not from tumbler-string parsing (which would yield digits)."""
    from nexus.plans.runner import plan_run

    match = _load_match(loaded_library, "review-default.yml")
    result = await plan_run(
        match, {"changed_paths": "src/x.py"},
        dispatcher=_stub_dispatcher(),
    )

    # Step 3 is store_get_many; step 4 is extract.
    assert "contents" in result.steps[2]
    extractions = result.steps[3]["extractions"]
    assert extractions
    values = extractions[0]
    # A tumbler-string input would produce values that look like
    # "1.2.3" fragments; a content input produces values that look
    # like the fixture text's opening words.
    any_real_content = any(
        isinstance(v, str) and ("reviewer" in v.lower() or "drift" in v.lower()
                                or "RDR" in v or "decision" in v.lower())
        for v in values.values()
    )
    assert any_real_content, (
        f"extract.extractions must carry content-derived values, got: {values!r}"
    )


@pytest.mark.asyncio
async def test_document_seed_compare_receives_content(loaded_library) -> None:
    """document-default: compare step must receive hydrated content
    and its agreements list must reference content, not tumblers."""
    from nexus.plans.runner import plan_run

    match = _load_match(loaded_library, "document-default.yml")
    result = await plan_run(
        match, {"area": "consensus"},
        dispatcher=_stub_dispatcher(),
    )

    assert "contents" in result.steps[3]
    compared = result.steps[4]
    assert compared["agreements"]
    agreement = compared["agreements"][0]
    assert "reviewer" in agreement.lower() or "consensus" in agreement.lower() or "plan runner" in agreement.lower(), (
        f"compare.agreements must reference hydrated content, got: {agreement!r}"
    )


@pytest.mark.asyncio
async def test_research_seed_summarize_receives_content(loaded_library) -> None:
    """research-default: summarize receives hydrated content from two
    search steps, not the tumbler lists."""
    from nexus.plans.runner import plan_run

    match = _load_match(loaded_library, "research-default.yml")
    result = await plan_run(
        match, {"concept": "plan runner"},
        dispatcher=_stub_dispatcher(),
    )

    assert "contents" in result.steps[3]
    summary = result.steps[4]["text"]
    assert "plan runner" in summary or "Delos" in summary or "reviewer" in summary.lower(), (
        f"research summary must reference hydrated content, got: {summary!r}"
    )


# ── Structural regression guard on store_get_many ─────────────────────────


def test_store_get_many_promoted_to_retrieval_tools() -> None:
    """The runner must auto-inject ``structured=True`` for
    ``store_get_many`` so plans never have to thread the flag through
    every hydration step."""
    from nexus.plans.runner import _RETRIEVAL_TOOLS

    assert "store_get_many" in _RETRIEVAL_TOOLS, (
        "hydration primitive must be in the runner's auto-structured set; "
        "without this, seed YAML would have to pass `structured=True` in "
        "every hydration step — boilerplate that every plan author would "
        "forget"
    )
