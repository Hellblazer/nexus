# SPDX-License-Identifier: AGPL-3.0-or-later
"""RDR-232 Phase 1 (nexus-jypmj.1.20): the reserved-stream merge and the reducer-input record on the
planner's documented Pattern A, where an operator's evidence arg (``content``, ``context``, ``items``)
resolves from ``$stepN.contents`` to a list of fetched texts.

Tenant-free: the runner seam and ``plan_run`` / the continuation composer with stubbed MCP tools.
"""
from __future__ import annotations

import json
from unittest.mock import AsyncMock, patch

import pytest

from nexus.plans.evidence_diagnostics import ReducerInputRecorder, reducer_input_scope
from nexus.plans.primary_evidence import PrimaryEvidence, SelectedChunk
from nexus.plans.reserved_stream import ReservedStream, reserved_scope, set_reserved_step
from nexus.plans.runner import _hydrate_operator_args


def _chunk(i: int) -> SelectedChunk:
    return SelectedChunk(
        source="RDR-155", doc_id="1.2.3", position=i, chash=f"{i + 1:02x}" * 32, collection="rdr__x",
        label=f"Section {i}", label_source="content", text=f"PRIMARY TEXT {i}",
    )


def _stream(n: int = 3) -> ReservedStream:
    stream = ReservedStream.from_evidence(PrimaryEvidence(items=tuple(_chunk(i) for i in range(n))))
    assert stream is not None
    return stream


def _match(plan: dict):
    from nexus.plans.match import Match

    return Match(
        plan_id=0, name="ad-hoc", description="", confidence=None, dimensions={}, tags="",
        plan_json=json.dumps(plan), required_bindings=[], optional_bindings=[],
        default_bindings={}, parent_dims=None,
    )


def _prefix() -> list[dict]:
    return [
        {"tool": "search", "args": {"query": "q", "corpus": "rdr"}},
        {"tool": "store_get_many", "args": {"ids": "$step1.ids", "collections": "rdr"}},
    ]


def _stubs():
    """Patch search / store_get_many so step 2 yields ``secondary 0..2``; no catalog lookups."""
    from nexus.mcp import core as mcp_core

    async def stub_search(**kwargs):  # noqa: ARG001
        return {"ids": [f"{0xA000 + i:064x}" for i in range(3)], "tumblers": [], "distances": [], "collections": []}

    def stub_get_many(ids, collections=None, structured=False):  # noqa: ARG001
        return {"contents": [f"secondary {k}" for k, _ in enumerate(ids)]}

    return (
        patch.object(mcp_core, "search", stub_search),
        patch.object(mcp_core, "store_get_many", stub_get_many),
        patch("nexus.plans.runner._resolve_non_evidentiary_chashes", return_value=set()),
    )


def _primary_text(stream: ReservedStream) -> list[str]:
    return [i.text for i in stream.items]


# ── the seam: every shape of an evidence arg that holds a list of fetched texts ───────────────────────────


SHAPES = {
    "summarize_content_list": ("summarize", "content", ["secondary 0", "secondary 1"], "join"),
    "generate_context_list": ("generate", "context", ["secondary 0", "secondary 1"], "join"),
    "rank_items_list": ("rank", "items", ["secondary 0", "secondary 1"], "json"),
    "compare_items_list": ("compare", "items", ["secondary 0", "secondary 1"], "json"),
}


def _decode(kind: str, value) -> list[str]:
    return json.loads(value) if kind == "json" else value.split("\n\n")


@pytest.mark.parametrize("shape", sorted(SHAPES))
def test_a_list_valued_evidence_arg_merges_the_stream_and_records_the_reducer_input(shape: str) -> None:
    tool, key, texts, kind = SHAPES[shape]
    stream, rec = _stream(), ReducerInputRecorder()
    with reducer_input_scope(rec), reserved_scope(stream):
        rec.set_step(4)
        set_reserved_step(4)
        _, args = _hydrate_operator_args(tool, {key: list(texts)})
    sent = _decode(kind, args[key])
    assert sent[:3] == _primary_text(stream), "the primary stream leads the operator's input"
    assert sent[3:] == texts
    assert stream.report()["merged"] is True and stream.report()["merged_at_step"] == 4
    assert list(rec.items or ()) == sent and rec.step == 4
    assert all(f"primary:{c.chash[:12]}" in " ".join(rec.items or ()) for c in (_chunk(i) for i in range(3)))


@pytest.mark.parametrize("shape", sorted(SHAPES))
def test_a_run_without_a_stream_hands_the_operator_exactly_what_it_did_before(shape: str) -> None:
    tool, key, texts, kind = SHAPES[shape]
    with_empty = [*texts[:1], "", *texts[1:]]
    _, bare = _hydrate_operator_args(tool, {key: list(with_empty)})
    rec = ReducerInputRecorder()
    with reducer_input_scope(rec):
        rec.set_step(2)
        _, recorded = _hydrate_operator_args(tool, {key: list(with_empty)})
    assert recorded == bare, "no stream: byte-identical, empty entries and all"
    assert list(rec.items or ()) == texts, "the reducer input is recorded without the empty entries"


@pytest.mark.parametrize("value", ["a prior summary", "$step3.summary"])
def test_a_string_evidence_arg_is_not_merged(value: str) -> None:
    """generate(context=$step5.summary) is a condensed prior output, not retrieved evidence."""
    stream, rec = _stream(), ReducerInputRecorder()
    with reducer_input_scope(rec), reserved_scope(stream):
        rec.set_step(5)
        set_reserved_step(5)
        _, args = _hydrate_operator_args("generate", {"context": value})
    assert args["context"] == value
    assert stream.report()["merged"] is False
    assert rec.items is None


def test_a_deferred_ref_list_is_not_merged() -> None:
    from nexus.plans.bundle import DEFERRED_REF_KEY

    stream = _stream()
    sentinel = {DEFERRED_REF_KEY: True, "step_index": 3, "field": "summary"}
    with reserved_scope(stream):
        set_reserved_step(4)
        _, args = _hydrate_operator_args("generate", {"context": [sentinel]})
    assert stream.report()["merged"] is False
    assert args["context"] == "" or "primary" not in str(args["context"])


def test_a_later_list_valued_step_does_not_merge_again() -> None:
    stream, rec = _stream(), ReducerInputRecorder()
    with reducer_input_scope(rec), reserved_scope(stream):
        rec.set_step(2)
        set_reserved_step(2)
        _hydrate_operator_args("summarize", {"content": ["a", "b"]})
        first = list(rec.items or ())
        rec.set_step(3)
        set_reserved_step(3)
        _, later = _hydrate_operator_args("generate", {"context": ["c", "d"]})
    assert later["context"] == "c\n\nd", "the stream is not given to a second operator"
    assert stream.report()["merged_at_step"] == 2
    assert list(rec.items or ()) == first and rec.step == 2


@pytest.mark.parametrize("tool,key", [("summarize", "content"), ("generate", "context"), ("rank", "items")])
def test_inputs_and_ids_stay_merged_once_and_recorded_once(tool: str, key: str) -> None:
    """Positive controls: the pass-through branch writes a list into the target key; the new site
    must not merge it a second time."""
    stream, rec = _stream(), ReducerInputRecorder()
    with reducer_input_scope(rec), reserved_scope(stream):
        rec.set_step(4)
        set_reserved_step(4)
        _, args = _hydrate_operator_args(tool, {"inputs": ["secondary 0", "secondary 1"]})
    sent = json.loads(args[key]) if key == "items" else args[key].split("\n\n")
    assert sent == [*_primary_text(stream), "secondary 0", "secondary 1"], "reserved items appear exactly once"
    assert list(rec.items or ()) == sent
    assert stream.report()["deduped_secondary"] == 0, "the primary items were not merged a second time"


def test_ids_hydration_still_merges_once() -> None:
    stream = _stream()
    ids = [f"{0xB000 + i:064x}" for i in range(2)]

    def fake(ids, collections=None, structured=False):  # noqa: ARG001
        return {"contents": [f"secondary {k}" for k, _ in enumerate(ids)]}

    with patch("nexus.mcp.core.store_get_many", side_effect=fake), \
         patch("nexus.plans.runner._resolve_non_evidentiary_chashes", return_value=set()), \
         reserved_scope(stream):
        set_reserved_step(4)
        _, args = _hydrate_operator_args("summarize", {"ids": ids, "collections": "rdr"})
    assert args["content"].split("\n\n") == [*_primary_text(stream), "secondary 0", "secondary 1"]


# ── through plan_run: the isolated path, the bundle path and the oversized-bundle fallback ────────────────


PATTERN_A_PLANS = {
    "summarize_content_list": {"tool": "summarize", "args": {"content": ["$step2.contents"]}},
    "summarize_content_single_ref": {"tool": "summarize", "args": {"content": "$step2.contents"}},
    "generate_context_first": {"tool": "generate", "args": {"instruction": "i", "context": ["$step2.contents"]}},
    "rank_items_ref": {"tool": "rank", "args": {"criterion": "c", "items": "$step2.contents"}},
}


def _arg_text(args: dict) -> str:
    return " ".join(str(args.get(k)) for k in ("content", "context", "items"))


@pytest.mark.asyncio
@pytest.mark.parametrize("name", sorted(PATTERN_A_PLANS))
async def test_plan_run_isolated_merges_at_the_operator_step(name: str) -> None:
    from nexus.mcp import core as mcp_core
    from nexus.plans.runner import plan_run

    stream, rec = _stream(), ReducerInputRecorder()
    seen: dict[str, str] = {}

    def _capture(label):
        async def stub(**kwargs):
            seen[label] = _arg_text(kwargs)
            return {"summary": "s", "text": "t", "items": []}
        return stub

    s1, s2, s3 = _stubs()
    step = PATTERN_A_PLANS[name]
    with s1, s2, s3, reducer_input_scope(rec), \
         patch.object(mcp_core, f"operator_{step['tool']}", _capture(step["tool"])):
        result = await plan_run(_match({"steps": [*_prefix(), step]}), bundle_operators=False, reserved_stream=stream)
    report = result.reserved_report
    assert report is not None and report["merged"] is True and report["merged_at_step"] == 2
    assert "PRIMARY TEXT 0" in seen[step["tool"]] and "secondary 0" in seen[step["tool"]]
    assert rec.items is not None and rec.step == 2
    assert any("PRIMARY TEXT 0" in i for i in rec.items) and "secondary 0" in rec.items


@pytest.mark.asyncio
async def test_plan_run_bundle_path_merges_pattern_a_summarize_then_generate() -> None:
    """The G07 shape: a summarize(content=$step2.contents) + generate(context=$step3.summary) bundle."""
    from nexus.plans.runner import plan_run

    stream, rec = _stream(), ReducerInputRecorder()
    captured: list = []

    async def fake_bundle(bundle, **kwargs):  # noqa: ARG001
        captured.append(bundle)
        return {"text": "ok"}

    steps = [
        *_prefix(),
        {"tool": "summarize", "args": {"content": ["$step2.contents"]}},
        {"tool": "generate", "args": {"instruction": "i", "context": "$step3.summary"}},
    ]
    s1, s2, s3 = _stubs()
    with s1, s2, s3, reducer_input_scope(rec), \
         patch("nexus.plans.bundle.dispatch_bundle", AsyncMock(side_effect=fake_bundle)):
        result = await plan_run(_match({"steps": steps}), reserved_stream=stream)
    (bundle,) = captured
    assert "PRIMARY TEXT 0" in bundle.steps[0].args["content"]
    assert "PRIMARY TEXT" not in str(bundle.steps[1].args.get("context")), "generate reads the summary, no second merge"
    assert result.reserved_report["merged_at_step"] == 2
    assert rec.items is not None and rec.step == 2 and any("PRIMARY TEXT 0" in i for i in rec.items)


@pytest.mark.asyncio
async def test_plan_run_oversized_bundle_fallback_merges_pattern_a() -> None:
    from nexus.mcp import core as mcp_core
    from nexus.plans.runner import plan_run

    stream, rec = _stream(), ReducerInputRecorder()
    seen: dict[str, str] = {}

    async def stub_summarize(**kwargs):
        seen["summarize"] = _arg_text(kwargs)
        return {"summary": "s"}

    async def stub_generate(**kwargs):
        seen["generate"] = _arg_text(kwargs)
        return {"text": "t"}

    steps = [
        *_prefix(),
        {"tool": "summarize", "args": {"content": ["$step2.contents"]}},
        {"tool": "generate", "args": {"instruction": "i", "context": "$step3.summary"}},
    ]
    s1, s2, s3 = _stubs()
    with s1, s2, s3, reducer_input_scope(rec), patch("nexus.plans.bundle.MAX_BUNDLE_PROMPT_CHARS", 1), \
         patch.object(mcp_core, "operator_summarize", stub_summarize), \
         patch.object(mcp_core, "operator_generate", stub_generate):
        result = await plan_run(_match({"steps": steps}), reserved_stream=stream)
    assert "PRIMARY TEXT 0" in seen["summarize"]
    assert "PRIMARY TEXT" not in seen["generate"]
    assert result.reserved_report["merged_at_step"] == 2
    assert rec.items is not None and rec.step == 2


def test_continuation_shape_a_merges_pattern_a() -> None:
    from nexus.plans.continuation import classify_continuation_cut
    from nexus.plans.continuation_envelope import _compose_shape_a

    steps = [
        {"tool": "summarize", "args": {"content": ["x", "y"]}},
        {"tool": "generate", "args": {"instruction": "i", "context": "$step1.summary"}},
    ]
    stream, rec = _stream(), ReducerInputRecorder()
    cut = classify_continuation_cut(steps)
    with reducer_input_scope(rec), reserved_scope(stream):
        _, _, bundle_steps = _compose_shape_a(cut, steps, {}, [])
    assert "PRIMARY TEXT 0" in bundle_steps[0].args["content"]
    assert stream.report()["merged_at_step"] == 0
    assert rec.items is not None and any("PRIMARY TEXT 0" in i for i in rec.items)
