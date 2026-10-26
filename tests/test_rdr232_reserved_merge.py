# SPDX-License-Identifier: AGPL-3.0-or-later
"""RDR-232 Phase 1 (nexus-jypmj.1.6): the reserved primary stream merged at the runner.

Tenant-free: the runner seam (``_hydrate_operator_args``, ``plan_run`` with stubbed MCP tools and
the continuation composers) and the pure stream object. Each test names the claim it pins.
"""
from __future__ import annotations

import json
from unittest.mock import patch

import pytest

from nexus.plans.primary_evidence import PrimaryEvidence, SelectedChunk
from nexus.plans.reserved_stream import (
    RESERVED_SHARE_MAX,
    ReservedStream,
    reserved_scope,
    set_reserved_step,
)
from nexus.plans.runner import _OPERATOR_MAX_INPUTS, _hydrate_operator_args


def _chunk(i: int, text: str | None = None) -> SelectedChunk:
    return SelectedChunk(
        source="RDR-084", doc_id="1.2.3", position=i, chash=f"{i:02x}" * 32, collection="rdr__x",
        label=f"Section {i}", label_source="content", text=text or f"PRIMARY TEXT {i}",
    )


def _stream(n: int = 5) -> ReservedStream:
    stream = ReservedStream.from_evidence(PrimaryEvidence(items=tuple(_chunk(i) for i in range(n))))
    assert stream is not None
    return stream


def _hydrate(
    tool: str, args: dict, stream: ReservedStream | None, step: int | None = 0, feeds_from: tuple[str, ...] = (),
):
    """``feeds_from`` names the tools that produced the step's evidence args (``$stepN`` producers)."""
    with reserved_scope(stream):
        set_reserved_step(step, feeds_from=feeds_from)
        return _hydrate_operator_args(tool, args)


def _secondary_ids(n: int) -> list[str]:
    return [f"{0xA000 + i:064x}" for i in range(n)]


@pytest.fixture
def ids_hydration():
    """``store_get_many`` returns "secondary i" for the i-th id; no catalog lookups."""
    def fake(ids, collections=None, structured=False):  # noqa: ARG001
        return {"contents": [f"secondary {k}" for k, _ in enumerate(ids)]}
    with patch("nexus.mcp.core.store_get_many", side_effect=fake), \
         patch("nexus.plans.runner._resolve_non_evidentiary_chashes", return_value=set()):
        yield


# ── 1. only secondary items are cut; every reserved item reaches the operator ─────────────────


def test_share_stays_below_the_operator_cap() -> None:
    assert RESERVED_SHARE_MAX < _OPERATOR_MAX_INPUTS


def test_ids_hydration_cap_cuts_only_secondary_items(ids_hydration) -> None:
    stream = _stream(5)
    _, args = _hydrate("rank", {"ids": _secondary_ids(120), "criterion": "c"}, stream)
    items = json.loads(args["items"])
    assert len(items) == _OPERATOR_MAX_INPUTS
    assert [i for i in items[:5]] == [r.text for r in stream.items]
    assert items[5] == "secondary 0" and items[-1] == "secondary 94"
    assert args["_truncation_metadata"] == {"truncated": True, "original_count": 125, "kept_count": 100}
    assert stream.report()["secondary_cut"] == 25 and stream.report()["capped"] is True


def test_explicit_inputs_cap_cuts_only_secondary_items() -> None:
    stream = _stream(5)
    inputs = [f"secondary {k}" for k in range(120)]
    _, args = _hydrate("rank", {"inputs": inputs, "criterion": "c"}, stream)
    items = json.loads(args["items"])
    assert len(items) == _OPERATOR_MAX_INPUTS
    assert items[:5] == [r.text for r in stream.items]
    assert items[-1] == "secondary 94"
    assert args["_truncation_metadata"]["original_count"] == 125


# ── 2. S4: a run with no reserved stream keeps today's uncapped explicit inputs ───────────────


@pytest.mark.parametrize("stream", [None, "empty"])
def test_no_reserved_stream_leaves_explicit_inputs_uncapped(stream) -> None:
    inputs = [f"secondary {k}" for k in range(120)]
    active = None if stream is None else ReservedStream.from_evidence(PrimaryEvidence())
    assert active is None  # an empty evidence object builds no stream at all
    _, args = _hydrate("rank", {"inputs": inputs, "criterion": "c"}, active)
    assert json.loads(args["items"]) == inputs
    assert "_truncation_metadata" not in args


def test_no_reserved_stream_leaves_ids_hydration_byte_identical(ids_hydration) -> None:
    ids = _secondary_ids(7)
    _, merged_off = _hydrate("summarize", {"ids": ids}, None)
    assert merged_off["content"] == "\n\n".join(f"secondary {k}" for k in range(7))


# ── dedupe by chash (ids) and by text (inputs): the primary copy and its attribution stay ─────


def test_ids_hydration_dedupes_a_vector_hit_that_is_a_reserved_chunk(ids_hydration) -> None:
    stream = _stream(3)
    ids = _secondary_ids(2) + [stream.items[1].chash]
    _, args = _hydrate("summarize", {"ids": ids}, stream)
    parts = args["content"].split("\n\n")
    assert sum("PRIMARY TEXT 1" in p for p in parts) == 1
    assert "[primary source RDR-084 (1.2.3) chunk 1, section Section 1; primary:" in args["content"]
    assert "secondary 2" not in args["content"]  # the hit that duplicated the primary chunk
    assert stream.report()["deduped_secondary"] == 1


def test_explicit_inputs_dedupes_by_text_including_a_source_note() -> None:
    stream = _stream(3)
    inputs = ["secondary a", "[source: rdr; indexed 2026]\nPRIMARY TEXT 2", "PRIMARY TEXT 0", "secondary b"]
    _, args = _hydrate("compare", {"inputs": inputs}, stream)
    items = json.loads(args["items"])
    assert items == [r.text for r in stream.items] + ["secondary a", "secondary b"]
    assert stream.report()["deduped_secondary"] == 2


# ── merge once, at the first evidence-consuming step; a re-hydration of that step merges again ─


def test_merge_happens_at_one_step_and_rehydration_of_it_is_idempotent() -> None:
    stream = _stream(2)
    first = _hydrate("rank", {"inputs": ["s1"], "criterion": "c"}, stream, step=2)[1]
    again = _hydrate("rank", {"inputs": ["s1"], "criterion": "c"}, stream, step=2)[1]
    assert first["items"] == again["items"]
    later = _hydrate("rank", {"inputs": ["s1"], "criterion": "c"}, stream, step=3)[1]
    assert json.loads(later["items"]) == ["s1"]  # not merged a second time
    assert stream.report()["merged_at_step"] == 2


# ── 4. a reserved item an intermediate operator dropped is counted ────────────────────────────


def test_dropped_reserved_items_are_counted_at_the_terminal_reduction() -> None:
    stream = _stream(4)
    _, first = _hydrate("rank", {"inputs": ["s"], "criterion": "c"}, stream, step=2)
    ranked = json.loads(first["items"])
    survivors = ranked[:2]  # the rank kept two reserved items and dropped two
    _hydrate("generate", {"context": survivors, "instruction": "i"}, stream, step=3, feeds_from=("rank",))
    report = stream.report()
    assert report["dropped_before_terminal"] == {"step": 3, "missing": 2, "of": 4}
    assert any("2 of 4 reserved primary items did not reach the final reduction" in d for d in stream.disclosures())


def test_nothing_dropped_reports_none() -> None:
    stream = _stream(2)
    _, first = _hydrate("rank", {"inputs": ["s"], "criterion": "c"}, stream, step=0)
    _hydrate("generate", {"context": json.loads(first["items"])}, stream, step=1, feeds_from=("rank",))
    assert stream.report()["dropped_before_terminal"] is None
    assert stream.disclosures() == []


def test_a_reducing_producer_is_not_a_drop() -> None:
    """A summarize or generate that condenses the reserved items is a reduction, not a loss: the
    next operator's input is the prose, which holds no item tokens. Only rank and filter, which
    keep or reorder items, can drop one."""
    for reducer in ("summarize", "generate", "extract", "operator_summarize"):
        stream = _stream(3)
        _hydrate("rank", {"inputs": ["s"], "criterion": "c"}, stream, step=0)
        _hydrate("generate", {"context": "a condensed paragraph", "instruction": "i"}, stream, step=1,
                 feeds_from=(reducer,))
        assert stream.report()["dropped_before_terminal"] is None, reducer
        assert stream.disclosures() == [], reducer


def test_a_filter_producer_that_loses_items_is_a_drop() -> None:
    stream = _stream(3)
    _hydrate("rank", {"inputs": ["s"], "criterion": "c"}, stream, step=0)
    _hydrate("generate", {"context": [stream.items[0].text], "instruction": "i"}, stream, step=1,
             feeds_from=("operator_filter",))
    assert stream.report()["dropped_before_terminal"] == {"step": 1, "missing": 2, "of": 3}


def test_an_unknown_or_mixed_producer_makes_no_drop_claim() -> None:
    for feeds in ((), ("store_get_many",), ("rank", "summarize")):
        stream = _stream(3)
        _hydrate("rank", {"inputs": ["s"], "criterion": "c"}, stream, step=0)
        _hydrate("generate", {"context": ["x"], "instruction": "i"}, stream, step=1, feeds_from=feeds)
        assert stream.report()["dropped_before_terminal"] is None, feeds


def test_a_deferred_ref_argument_is_not_observed() -> None:
    """In a bundle the later step's evidence arg is a ``$stepN`` sentinel, not the evidence."""
    from nexus.plans.bundle import DEFERRED_REF_KEY

    stream = _stream(3)
    _hydrate("rank", {"inputs": ["s"], "criterion": "c"}, stream, step=0)
    sentinel = {DEFERRED_REF_KEY: True, "step_index": 0, "field": "items"}
    _hydrate("generate", {"context": sentinel, "instruction": "i"}, stream, step=1, feeds_from=("rank",))
    _hydrate("compare", {"inputs": [sentinel]}, stream, step=2, feeds_from=("rank",))
    assert stream.report()["dropped_before_terminal"] is None
    assert stream.disclosures() == []


def test_a_stream_no_operator_consumed_is_disclosed_as_unmerged() -> None:
    stream = _stream(2)
    assert stream.report()["merged"] is False
    assert any("not merged" in d for d in stream.disclosures())


# ── through plan_run: the unbundled rank drop is reported on the result ───────────────────────


def _match(plan: dict):
    from nexus.plans.match import Match
    return Match(
        plan_id=1, name="t", description="", confidence=0.9, dimensions={}, tags="",
        plan_json=json.dumps(plan), required_bindings=[], optional_bindings=[],
        default_bindings={}, parent_dims=None,
    )


@pytest.mark.asyncio
async def test_plan_run_merges_ahead_of_inputs_and_reports_an_unbundled_rank_drop() -> None:
    from nexus.mcp import core as mcp_core
    from nexus.plans.runner import plan_run

    stream = _stream(4)
    seen: dict[str, object] = {}

    async def stub_search(**kwargs):  # noqa: ARG001
        return {"ids": _secondary_ids(3), "tumblers": [], "distances": [], "collections": []}

    def stub_get_many(ids, collections=None, structured=False):  # noqa: ARG001
        return {"contents": [f"secondary {k}" for k, _ in enumerate(ids)]}

    async def stub_rank(**kwargs):
        seen["rank_items"] = json.loads(kwargs["items"])
        return {"items": kwargs["items"] and json.loads(kwargs["items"])[:2]}  # drops all but two

    async def stub_generate(**kwargs):
        seen["generate_context"] = kwargs["context"]
        return {"text": "ok"}

    plan = {"steps": [
        {"tool": "search", "args": {"query": "q", "corpus": "rdr"}},
        {"tool": "store_get_many", "args": {"ids": "$step1.ids", "collections": "rdr"}},
        {"tool": "rank", "args": {"criterion": "c", "inputs": "$step2.contents"}},
        {"tool": "generate", "args": {"instruction": "i", "context": "$step3.items"}},
    ]}
    with patch.object(mcp_core, "search", stub_search), \
         patch.object(mcp_core, "store_get_many", stub_get_many), \
         patch.object(mcp_core, "operator_rank", stub_rank), \
         patch.object(mcp_core, "operator_generate", stub_generate), \
         patch("nexus.plans.runner._resolve_non_evidentiary_chashes", return_value=set()):
        result = await plan_run(_match(plan), bundle_operators=False, reserved_stream=stream)

    ranked_in = seen["rank_items"]
    assert ranked_in[:4] == [r.text for r in stream.items]
    assert len(ranked_in) == 7
    report = result.reserved_report
    assert report is not None and report["merged_at_step"] == 2  # 0-based plan index of the rank
    assert report["dropped_before_terminal"] == {"step": 3, "missing": 2, "of": 4}


@pytest.mark.asyncio
async def test_plan_run_without_a_stream_has_no_report_and_no_cap() -> None:
    from nexus.mcp import core as mcp_core
    from nexus.plans.runner import plan_run

    seen: dict[str, object] = {}

    async def stub_rank(**kwargs):
        seen["items"] = json.loads(kwargs["items"])
        return {"items": []}

    plan = {"steps": [{"tool": "rank", "args": {"criterion": "c", "inputs": [f"s{k}" for k in range(130)]}}]}
    with patch.object(mcp_core, "operator_rank", stub_rank):
        result = await plan_run(_match(plan), bundle_operators=False)
    assert len(seen["items"]) == 130
    assert result.reserved_report is None


@pytest.mark.asyncio
async def test_bundle_and_continuation_composers_see_the_same_evidence() -> None:
    """The bundled prompt (plan_run's bundle path) and the continuation Shape A composer both
    carry the reserved items, and carry them in the same order."""
    from unittest.mock import AsyncMock

    from nexus.plans.continuation import classify_continuation_cut
    from nexus.plans.continuation_envelope import _compose_shape_a
    from nexus.plans.runner import plan_run

    stream = _stream(3)
    steps = [
        {"tool": "rank", "args": {"criterion": "c", "inputs": ["s1", "s2"]}},
        {"tool": "summarize", "args": {"cited": False, "content": "x"}},
    ]
    captured: list = []

    async def fake_bundle(bundle, **kwargs):  # noqa: ARG001
        captured.append(bundle)
        return {"summary": "x"}

    with patch("nexus.plans.bundle.dispatch_bundle", AsyncMock(side_effect=fake_bundle)):
        await plan_run(_match({"steps": steps}), reserved_stream=stream)
    assert len(captured) == 1
    bundle_inputs = captured[0].steps[0].args["items"]
    assert json.loads(bundle_inputs)[:3] == [r.text for r in stream.items]

    cut = classify_continuation_cut(steps)
    other = _stream(3)
    with reserved_scope(other):
        _, _, bundle_steps = _compose_shape_a(cut, steps, {}, [])
    assert json.loads(bundle_steps[0].args["items"]) == json.loads(bundle_inputs)
    assert other.report()["merged_at_step"] == 0


# ── nothing dropped is never reported as dropped: the bundle routes hold deferred refs ────────


def _bundled_rank_generate_plan() -> dict:
    return {"steps": [
        {"tool": "search", "args": {"query": "q", "corpus": "rdr"}},
        {"tool": "store_get_many", "args": {"ids": "$step1.ids", "collections": "rdr"}},
        {"tool": "rank", "args": {"criterion": "c", "inputs": "$step2.contents"}},
        {"tool": "generate", "args": {"instruction": "i", "context": "$step3.items"}},
    ]}


@pytest.mark.asyncio
async def test_plan_run_bundled_rank_then_generate_reports_no_drop() -> None:
    """Default path: rank and generate bundle, so generate's context is a deferred ``$step3``
    sentinel. Nothing was dropped, so nothing may be reported dropped and no line may reach the
    answer."""
    from unittest.mock import AsyncMock

    from nexus.mcp import core as mcp_core
    from nexus.plans.runner import plan_run

    stream = _stream(4)
    captured: list = []

    async def stub_search(**kwargs):  # noqa: ARG001
        return {"ids": _secondary_ids(3), "tumblers": [], "distances": [], "collections": []}

    def stub_get_many(ids, collections=None, structured=False):  # noqa: ARG001
        return {"contents": [f"secondary {k}" for k, _ in enumerate(ids)]}

    async def fake_bundle(bundle, **kwargs):  # noqa: ARG001
        captured.append(bundle)
        return {"text": "ok"}

    with patch.object(mcp_core, "search", stub_search), \
         patch.object(mcp_core, "store_get_many", stub_get_many), \
         patch("nexus.plans.bundle.dispatch_bundle", AsyncMock(side_effect=fake_bundle)), \
         patch("nexus.plans.runner._resolve_non_evidentiary_chashes", return_value=set()):
        result = await plan_run(_match(_bundled_rank_generate_plan()), reserved_stream=stream)

    assert len(captured) == 1, "rank and generate ran as one bundle"
    assert json.loads(captured[0].steps[0].args["items"])[:4] == [r.text for r in stream.items]
    report = result.reserved_report
    assert report is not None and report["merged_at_step"] == 2
    assert report["dropped_before_terminal"] is None
    assert stream.disclosures() == []


@pytest.mark.asyncio
async def test_each_bundle_step_hydrates_under_its_own_index() -> None:
    """The reserved stream merges at ONE plan step. If a bundle left the current step at the
    bundle's first index, every later bundle step would also look like the merge step and merge
    the stream a second time."""
    from unittest.mock import AsyncMock

    from nexus.plans.runner import plan_run

    stream = _stream(3)
    captured: list = []

    async def fake_bundle(bundle, **kwargs):  # noqa: ARG001
        captured.append(bundle)
        return {"text": "ok"}

    steps = [
        {"tool": "rank", "args": {"criterion": "c", "inputs": ["s1", "s2"]}},
        {"tool": "compare", "args": {"focus": "f", "inputs": ["t1", "t2"]}},
    ]
    with patch("nexus.plans.bundle.dispatch_bundle", AsyncMock(side_effect=fake_bundle)):
        await plan_run(_match({"steps": steps}), reserved_stream=stream)
    (bundle,) = captured
    first, second = (json.loads(st.args["items"]) for st in bundle.steps)
    assert first[:3] == [r.text for r in stream.items]
    assert second == ["t1", "t2"], "the second bundle step is not the merge step"
    assert stream.report()["merged_at_step"] == 0


@pytest.mark.asyncio
async def test_an_oversized_bundle_falls_back_per_step_with_each_step_under_its_own_index() -> None:
    """The per-step fallback of a bundle too large for one prompt dispatches each step through
    the default dispatcher; the stream merges at the first step only."""
    from nexus.mcp import core as mcp_core
    from nexus.plans.runner import plan_run

    stream = _stream(3)
    seen: dict[str, list] = {}

    async def stub_rank(**kwargs):
        seen["rank"] = json.loads(kwargs["items"])
        return {"items": seen["rank"]}

    async def stub_compare(**kwargs):
        seen["compare"] = json.loads(kwargs["items"])
        return {"text": "ok"}

    steps = [
        {"tool": "rank", "args": {"criterion": "c", "inputs": ["s1", "s2"]}},
        {"tool": "compare", "args": {"focus": "f", "inputs": ["t1", "t2"]}},
    ]
    with patch("nexus.plans.bundle.MAX_BUNDLE_PROMPT_CHARS", 1), \
         patch.object(mcp_core, "operator_rank", stub_rank), \
         patch.object(mcp_core, "operator_compare", stub_compare):
        result = await plan_run(_match({"steps": steps}), reserved_stream=stream)
    assert seen["rank"][:3] == [r.text for r in stream.items]
    assert seen["compare"] == ["t1", "t2"], "the second step of the fallback is not the merge step"
    assert result.reserved_report["merged_at_step"] == 0


def test_continuation_shape_a_reports_no_drop_when_nothing_was_dropped() -> None:
    from nexus.plans.continuation import classify_continuation_cut
    from nexus.plans.continuation_envelope import _compose_shape_a

    steps = [
        {"tool": "rank", "args": {"criterion": "c", "inputs": ["s1", "s2"]}},
        {"tool": "summarize", "args": {"content": "$step1.items"}},
    ]
    stream = _stream(3)
    cut = classify_continuation_cut(steps)
    with reserved_scope(stream):
        _compose_shape_a(cut, steps, {}, [])
    assert stream.report()["merged_at_step"] == 0
    assert stream.report()["dropped_before_terminal"] is None
    assert stream.disclosures() == []


def test_continuation_shape_b_still_counts_a_real_drop_by_the_prefix_rank() -> None:
    """The cut step reads a ``$step1`` output the prefix already produced: a materialised list,
    so a rank that kept two of four reserved items is a real, observable drop."""
    from nexus.plans.continuation import ContinuationCut, CutShape
    from nexus.plans.continuation_envelope import _compose_shape_b

    steps = [
        {"tool": "rank", "args": {"criterion": "c", "inputs": ["s1"]}},
        {"tool": "summarize", "args": {"content": "$step1.items"}},
    ]
    stream = _stream(4)
    _, first = _hydrate("rank", steps[0]["args"], stream, step=0)
    survivors = json.loads(first["items"])[:2]
    cut = ContinuationCut(shape=CutShape.SHAPE_B, cut_at_step=1, plan_indices=(1,), operators=("summarize",))
    with reserved_scope(stream):
        _compose_shape_b(cut, steps, {}, [{"items": survivors}])
    assert stream.report()["dropped_before_terminal"] == {"step": 1, "missing": 2, "of": 4}


# ── the review-default shape: extract and compare both read the retrieved contents ─────────────


def test_extract_with_explicit_inputs_receives_the_reserved_stream() -> None:
    """``review-default`` runs extract on ``$step3.contents`` before compare. The merge belongs at
    the first operator that consumes retrieved evidence, and that is extract: it must carry the
    reserved items ahead of the plan's inputs, and compare must not merge them a second time."""
    stream = _stream(3)
    inputs = [f"secondary {k}" for k in range(4)]
    _, extract_args = _hydrate("extract", {"inputs": inputs, "fields": "decision"}, stream, step=3)
    assert extract_args["inputs"][:3] == [r.text for r in stream.items]
    assert extract_args["inputs"][3:] == inputs
    _, compare_args = _hydrate("compare", {"inputs": inputs, "focus": "f"}, stream, step=4)
    assert json.loads(compare_args["items"]) == inputs, "compare is not the merge step"
    assert stream.report()["merged_at_step"] == 3


def test_extract_explicit_inputs_cap_cuts_only_secondary_items() -> None:
    stream = _stream(5)
    inputs = [f"secondary {k}" for k in range(120)]
    _, args = _hydrate("extract", {"inputs": inputs, "fields": "f"}, stream, step=0)
    assert len(args["inputs"]) == _OPERATOR_MAX_INPUTS
    assert args["inputs"][:5] == [r.text for r in stream.items]
    assert args["_truncation_metadata"]["original_count"] == 125


def test_extract_explicit_inputs_without_a_stream_are_untouched() -> None:
    inputs = [f"secondary {k}" for k in range(120)]
    _, args = _hydrate("extract", {"inputs": inputs, "fields": "f"}, None, step=0)
    assert args["inputs"] == inputs and "_truncation_metadata" not in args


@pytest.mark.asyncio
async def test_plan_run_review_default_shape_merges_at_extract_not_compare() -> None:
    from nexus.mcp import core as mcp_core
    from nexus.plans.runner import plan_run

    stream = _stream(3)
    seen: dict[str, object] = {}

    async def stub_search(**kwargs):  # noqa: ARG001
        return {"ids": _secondary_ids(2), "tumblers": [], "distances": [], "collections": []}

    def stub_get_many(ids, collections=None, structured=False):  # noqa: ARG001
        return {"contents": [f"secondary {k}" for k, _ in enumerate(ids)]}

    async def stub_extract(**kwargs):
        seen["extract"] = kwargs["inputs"]
        return {"extractions": []}

    async def stub_compare(**kwargs):
        seen["compare"] = json.loads(kwargs["items"])
        return {"text": "ok"}

    plan = {"steps": [
        {"tool": "search", "args": {"query": "q", "corpus": "rdr"}},
        {"tool": "store_get_many", "args": {"ids": "$step1.ids", "collections": "rdr"}},
        {"tool": "extract", "args": {"inputs": "$step2.contents", "fields": "decision"}},
        {"tool": "compare", "args": {"focus": "f", "inputs": "$step2.contents"}},
    ]}
    with patch.object(mcp_core, "search", stub_search), \
         patch.object(mcp_core, "store_get_many", stub_get_many), \
         patch.object(mcp_core, "operator_extract", stub_extract), \
         patch.object(mcp_core, "operator_compare", stub_compare), \
         patch("nexus.plans.runner._resolve_non_evidentiary_chashes", return_value=set()):
        result = await plan_run(_match(plan), bundle_operators=False, reserved_stream=stream)

    extract_in = seen["extract"]
    texts = extract_in if isinstance(extract_in, list) else json.loads(extract_in)
    assert texts[:3] == [r.text for r in stream.items]
    assert seen["compare"] == ["secondary 0", "secondary 1"]
    assert result.reserved_report["merged_at_step"] == 2
    assert result.reserved_report["dropped_before_terminal"] is None, "compare reads retrieval output, not an operator's"


# ── nx_answer wiring: who gets a stream ───────────────────────────────────────────────────────


def _resolution_for_rdr084(*, qualified: bool = True):
    from nexus.plans.evidence import SourceRef
    from nexus.plans.primary_evidence import NamedSourceResolution, ResolvedSource

    return NamedSourceResolution(sources=(ResolvedSource(
        ref=SourceRef.parse("RDR-084"), status="resolved", tumbler="1.2.3", title="RDR-084", qualified=qualified,
    ),))


def _drive(match, *, resolution, evidence, structured=True):
    """``nx_answer`` with the resolution fixed, the primary read replaced by *evidence*, and
    ``plan_run`` replaced by a recorder. Returns ``(envelope, plan_run_mock, select_mock)``."""
    from unittest.mock import AsyncMock, MagicMock

    from tests.test_rdr232_caller_scope import _drive_nx_answer
    from tests.test_rdr232_p1_review_fixes import _MULTI_PLAN, _plan_run_result  # noqa: F401

    run = AsyncMock(return_value=_plan_run_result([{"text": "answer"}], []))
    select = MagicMock(side_effect=evidence) if isinstance(evidence, Exception) else MagicMock(return_value=evidence)
    with patch("nexus.mcp.core._nx_answer_named_sources", return_value=resolution), \
         patch("nexus.plans.runner.plan_run", run), \
         patch("nexus.plans.primary_evidence.select_primary_evidence", select), \
         patch("nexus.plans.primary_evidence.t3_chunk_fetcher", return_value=lambda c, h: {}), \
         patch("nexus.mcp.core._get_t3", return_value=MagicMock()):
        env, _ = _drive_nx_answer(match, scope="", structured=structured, bindings={"source": "RDR-084"})
    return env, run, select


def test_nx_answer_hands_the_runner_a_stream_built_from_the_resolution_it_already_holds() -> None:
    from tests.test_rdr232_p1_review_fixes import _MULTI_PLAN

    evidence = PrimaryEvidence(items=(_chunk(0), _chunk(1)))
    res = _resolution_for_rdr084()
    env, run, select = _drive(_MULTI_PLAN, resolution=res, evidence=evidence)
    (call,) = run.await_args_list
    stream = call.kwargs["reserved_stream"]
    assert [i.chash for i in stream.items] == [_chunk(0).chash, _chunk(1).chash]
    assert select.call_args.args == (res,), "select_primary_evidence reads the resolution; nothing is resolved twice"
    assert env["primary_evidence"]["selected_items"] == 2
    assert env["reserved_stream"]["reserved_items"] == 2 and env["reserved_stream"]["merged"] is False


def test_nx_answer_without_a_resolved_source_passes_no_stream_and_reads_nothing() -> None:
    from tests.test_rdr232_p1_review_fixes import _MULTI_PLAN

    env, run, select = _drive(_MULTI_PLAN, resolution=None, evidence=PrimaryEvidence(items=(_chunk(0),)))
    (call,) = run.await_args_list
    assert "reserved_stream" not in call.kwargs
    select.assert_not_called()
    assert env["primary_evidence"] is None and env["reserved_stream"] is None


def test_the_single_query_fast_path_has_no_primary_stream_and_says_so() -> None:
    from tests.test_rdr232_caller_scope import _single_query_match

    env, run, select = _drive(
        _single_query_match("knowledge,code,docs,rdr"), resolution=_resolution_for_rdr084(qualified=False),
        evidence=PrimaryEvidence(items=(_chunk(0),)))
    select.assert_not_called()
    run.assert_not_awaited()
    assert env["primary_evidence"] is None and env["reserved_stream"] is None
    assert "no primary evidence stream" in env["final_text"], "the fast-path notice stays honest"
    assert env["named_sources"]["sources"][0]["status"] == "resolved"


def test_an_unreadable_source_does_not_fail_the_answer() -> None:
    from tests.test_rdr232_p1_review_fixes import _MULTI_PLAN

    env, run, select = _drive(_MULTI_PLAN, resolution=_resolution_for_rdr084(), evidence=RuntimeError("down"))
    select.assert_called_once()
    assert run.await_count == 1 and env["final_text"].endswith("answer")
    assert "reserved_stream" not in run.await_args.kwargs
    # nexus-jypmj.1.9: the failure is on the envelope (error) and in the text, not a silent absence.
    assert env["primary_evidence"]["error"] == "RuntimeError" and env["primary_evidence"]["items"] == []
    assert "could not be read (RuntimeError)" in env["final_text"]


# ── the manifest read is capped before the selection is consumed (nexus-jypmj.1.17 note) ──────


def test_a_manifest_longer_than_the_read_cap_is_spread_sampled_before_anything_consumes_it() -> None:
    from dataclasses import replace

    from tests.test_rdr232_primary_evidence import (
        ART_DOC, ART_PATH, DEFAULT_CAPS, RDR084, FakeCatalog, FakeT3, _doc, _sectioned, _select,
    )

    cat, t3 = FakeCatalog(), FakeT3()
    _doc(cat, t3, ART_DOC, ART_PATH, _sectioned(2000))
    caps = replace(DEFAULT_CAPS, max_manifest_rows_read=400, max_manifest_rows=200)
    _res, primary = _select(cat, t3, [RDR084], caps=caps)
    (doc,) = primary.documents
    assert doc.manifest_rows == 2000, "the document's true length is still reported"
    assert doc.manifest_rows_expected == 0, "a read cap is not a catalog count mismatch"
    assert doc.rows_hydrated <= 200 and doc.rows_sampled is True
    assert "max_manifest_rows_read" in primary.caps_hit
    assert max(i.position for i in primary.items) > 1500, "the read cap samples across the document, not its prefix"
    assert any("max_manifest_rows_read" in line for line in primary.disclosures())


def test_the_read_cap_is_a_cap_in_the_one_calibration_object() -> None:
    from nexus.plans.primary_evidence import DEFAULT_CAPS

    assert DEFAULT_CAPS.max_manifest_rows_read > DEFAULT_CAPS.max_manifest_rows
    assert "max_manifest_rows_read" in DEFAULT_CAPS.to_wire()


@pytest.mark.asyncio
async def test_the_continuation_envelope_carries_the_reserved_primary_evidence() -> None:
    """The caller-side reduction route (RDR-200 handoff) is fed the same stream: the real
    ``plan_run`` stops before the summarize step, ``nx_answer`` assembles the envelope under the
    stream's scope, and the envelope's request holds the primary chunks ahead of the hit."""
    from unittest.mock import MagicMock

    from nexus.mcp import core as mcp_core
    from nexus.mcp.core import nx_answer

    evidence = PrimaryEvidence(items=(_chunk(0), _chunk(1)))
    stream = ReservedStream.from_evidence(evidence)

    async def stub_search(**kwargs):  # noqa: ARG001
        return {"ids": ["a" * 64], "tumblers": ["1.1"], "distances": [0.1], "collections": ["knowledge"],
                "chunk_text_hash": ["h1"], "chunk_collections": ["knowledge"]}

    def stub_get_many(ids, collections=None, structured=False):  # noqa: ARG001
        return {"contents": ["the secondary hit" for _ in ids]}

    db_stub = MagicMock()
    db_stub.plans.save_plan = MagicMock(return_value=1)
    db_stub.plans.get_plan = MagicMock(return_value={"id": 1})
    match = _match({"steps": [
        {"tool": "search", "args": {"query": "$intent", "corpus": "knowledge"}},
        {"tool": "summarize", "args": {"ids": "$step1.ids", "collections": "knowledge"}},
    ]})
    with patch("nexus.plans.matcher.plan_match", return_value=[match]), \
         patch("nexus.mcp.core._t2_ctx") as t2_ctx, \
         patch("nexus.mcp.core._t2_index_write", lambda fn, **_kw: fn(db_stub)), \
         patch("nexus.mcp.core.scratch", return_value="ok"), \
         patch("nexus.mcp_infra.get_t1_plan_cache", return_value=None), \
         patch("nexus.plans.continuation_envelope._CONTINUATION_GO_LIVE", True), \
         patch("nexus.mcp.core._nx_answer_primary_stream", return_value=(evidence, stream)), \
         patch("nexus.plans.runner._resolve_non_evidentiary_chashes", return_value=set()), \
         patch.object(mcp_core, "search", stub_search), \
         patch.object(mcp_core, "store_get_many", stub_get_many):
        t2_ctx.return_value.__enter__.return_value = db_stub
        env = await nx_answer(question="q", continuation=True, structured=True)

    assert env["continuation"] is not None and env["continuation"]["cut_at_step"] == 1
    blob = json.dumps(env["continuation"])
    first, second, hit = blob.find("PRIMARY TEXT 0"), blob.find("PRIMARY TEXT 1"), blob.find("the secondary hit")
    assert 0 <= first < second < hit, "the primary chunks come first, in order, ahead of the hit"
    assert env["reserved_stream"]["merged"] is True and env["reserved_stream"]["merged_at_step"] == 1


# ── what the T2 lifecycle stream (nexus-jypmj.1.7) will add: non-chunk items inside the share ──


def test_extra_records_lead_the_share_and_the_share_cap_never_evicts_them() -> None:
    from nexus.plans.reserved_stream import ReservedItem

    extra = (ReservedItem.record("rdr-084-gate", "gate: PASSED", label="RDR-084 gate record"),)
    evidence = PrimaryEvidence(items=tuple(_chunk(i) for i in range(RESERVED_SHARE_MAX + 5)))
    stream = ReservedStream.from_evidence(evidence, extra=extra)
    assert stream is not None and len(stream.items) == RESERVED_SHARE_MAX
    assert stream.items[0].raw == "gate: PASSED" and stream.items[0].token == "primary:rdr-084-gate#"
    assert stream.over_share_dropped == 6, "five over the share plus the one the record displaced"
    _, args = _hydrate("generate", {"inputs": ["s"], "instruction": "i"}, stream)
    assert args["context"].startswith("[primary record RDR-084 gate record; primary:rdr-084-gate#]\ngate: PASSED")


def test_a_record_only_stream_is_built_and_an_empty_one_is_not() -> None:
    from nexus.plans.reserved_stream import ReservedItem

    assert ReservedStream.from_evidence(None) is None
    assert ReservedStream.from_evidence(PrimaryEvidence()) is None
    only = ReservedStream.from_evidence(None, extra=(ReservedItem.record("x", "body"),))
    assert only is not None and [i.raw for i in only.items] == ["body"]
    assert not only.is_primary_chash(""), "a record has no chash to dedupe a vector hit against"
