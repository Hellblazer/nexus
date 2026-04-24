# SPDX-License-Identifier: AGPL-3.0-or-later
"""Unit tests for operator-bundle execution (nexus-nxa-perf).

Pure-Python tests: segmentation, prompt composition, schema selection,
dispatch wrapper. End-to-end tests that spawn ``claude -p`` live in the
sandbox harness, not here — this file must run offline.
"""
from __future__ import annotations

import json
from unittest.mock import AsyncMock, patch

import pytest

from nexus.plans.bundle import (
    BUNDLED_INTERMEDIATE,
    IsolatedStep,
    OPERATOR_BUNDLE_NAMES,
    OperatorBundle,
    OperatorBundleSlice,
    OperatorBundleStep,
    compose_bundle_prompt,
    dispatch_bundle,
    is_operator_tool,
    segment_steps,
)


# ── is_operator_tool ──────────────────────────────────────────────────────────


class TestIsOperatorTool:

    @pytest.mark.parametrize("tool", sorted(OPERATOR_BUNDLE_NAMES))
    def test_known_operators_match(self, tool):
        assert is_operator_tool(tool) is True

    @pytest.mark.parametrize("tool", ["search", "query", "traverse",
                                       "store_get_many", "", "unknown"])
    def test_non_operators_reject(self, tool):
        assert is_operator_tool(tool) is False


# ── Segmentation ──────────────────────────────────────────────────────────────


class TestSegmentation:
    """``segment_steps`` is pure structural analysis — no arg resolution.

    The runner resolves args at execution time where ``step_outputs``
    and ``deferred_step_indices`` are available. Segmentation only names
    where the bundle boundaries are.
    """

    def test_pure_retrieval_plan_is_all_isolated(self):
        steps = [
            {"tool": "search", "args": {"query": "x"}},
            {"tool": "traverse", "args": {"start": "y"}},
        ]
        segs = segment_steps(steps)
        assert len(segs) == 2
        assert all(isinstance(s, IsolatedStep) for s in segs)

    def test_single_operator_stays_isolated(self):
        """A lone operator step does NOT trigger a bundle (needs ≥ 2)."""
        steps = [
            {"tool": "search", "args": {"query": "x"}},
            {"tool": "extract", "args": {"fields": "a,b"}},
        ]
        segs = segment_steps(steps)
        assert len(segs) == 2
        assert all(isinstance(s, IsolatedStep) for s in segs)

    def test_two_consecutive_operators_bundle(self):
        steps = [
            {"tool": "search", "args": {"query": "x"}},
            {"tool": "extract", "args": {"fields": "title,author"}},
            {"tool": "summarize", "args": {"cited": True}},
        ]
        segs = segment_steps(steps)
        assert len(segs) == 2
        assert isinstance(segs[0], IsolatedStep)
        assert segs[0].plan_index == 0
        assert isinstance(segs[1], OperatorBundleSlice)
        assert segs[1].start_index == 1
        assert segs[1].end_index == 2
        assert segs[1].plan_indices == (1, 2)

    def test_three_consecutive_operators_bundle(self):
        steps = [
            {"tool": "search", "args": {"query": "x"}},
            {"tool": "extract", "args": {"fields": "a"}},
            {"tool": "rank", "args": {"criterion": "recency"}},
            {"tool": "summarize", "args": {"cited": False}},
        ]
        segs = segment_steps(steps)
        assert len(segs) == 2
        slice_ = segs[1]
        assert isinstance(slice_, OperatorBundleSlice)
        assert slice_.plan_indices == (1, 2, 3)

    def test_non_operator_between_operators_breaks_bundle(self):
        """A traverse step between two operators forces two isolated ops,
        not one bundle."""
        steps = [
            {"tool": "extract", "args": {"fields": "a"}},
            {"tool": "traverse", "args": {"start": "t"}},
            {"tool": "summarize", "args": {}},
        ]
        segs = segment_steps(steps)
        assert len(segs) == 3
        assert all(isinstance(s, IsolatedStep) for s in segs)

    def test_mcp_prefixed_tool_names_normalize(self):
        """``mcp__plugin_nx_nexus__operator_extract`` should match ``extract``."""
        steps = [
            {"tool": "mcp__plugin_nx_nexus__operator_extract", "args": {}},
            {"tool": "mcp__plugin_nx_nexus__operator_summarize", "args": {}},
        ]
        segs = segment_steps(steps)
        assert len(segs) == 1
        assert isinstance(segs[0], OperatorBundleSlice)

    def test_op_field_variants_accepted(self):
        """Plan YAML uses ``tool:``; inline-planner output uses ``op:`` or
        ``operation:``."""
        steps = [
            {"op": "extract", "args": {}},
            {"operation": "summarize", "args": {}},
        ]
        segs = segment_steps(steps)
        assert isinstance(segs[0], OperatorBundleSlice)

    def test_empty_plan(self):
        assert segment_steps([]) == []


# ── Prompt composition ────────────────────────────────────────────────────────


class TestComposeBundlePrompt:

    def test_two_op_prompt_contains_both_steps(self):
        bundle = OperatorBundle(steps=(
            OperatorBundleStep(1, "extract", {"fields": "title,author",
                                              "inputs": "[{...}]"}),
            OperatorBundleStep(2, "summarize", {"cited": True}),
        ))
        prompt, schema = compose_bundle_prompt(bundle)
        assert "STEP 1 — extract" in prompt
        assert "STEP 2 — summarize" in prompt
        assert "title,author" in prompt
        # Step 2 should reference step 1's output, not duplicate its data.
        assert "output from STEP 1" in prompt

    def test_schema_is_terminal_steps_schema(self):
        bundle_ending_in_summarize = OperatorBundle(steps=(
            OperatorBundleStep(1, "extract", {"fields": "a"}),
            OperatorBundleStep(2, "summarize", {"cited": False}),
        ))
        _, schema = compose_bundle_prompt(bundle_ending_in_summarize)
        assert "summary" in schema["required"]

        bundle_ending_in_rank = OperatorBundle(steps=(
            OperatorBundleStep(1, "extract", {}),
            OperatorBundleStep(2, "rank", {"criterion": "x"}),
        ))
        _, schema = compose_bundle_prompt(bundle_ending_in_rank)
        assert "ranked" in schema["required"]

    def test_first_step_inlines_inputs_subsequent_steps_chain(self):
        """The first step's prompt carries the literal input data;
        subsequent steps reference prior-step output conceptually."""
        bundle = OperatorBundle(steps=(
            OperatorBundleStep(1, "extract", {"fields": "f",
                                              "inputs": "concrete-input-data"}),
            OperatorBundleStep(2, "summarize", {}),
            OperatorBundleStep(3, "rank", {"criterion": "length"}),
        ))
        prompt, _ = compose_bundle_prompt(bundle)
        assert "concrete-input-data" in prompt
        # Step 2 references step 1
        assert "output from STEP 1" in prompt
        # Step 3 references step 2
        assert "output list from STEP 2" in prompt

    def test_prompt_instructs_final_output_only(self):
        bundle = OperatorBundle(steps=(
            OperatorBundleStep(1, "extract", {}),
            OperatorBundleStep(2, "summarize", {}),
        ))
        prompt, _ = compose_bundle_prompt(bundle)
        # The header must explicitly forbid intermediate emission.
        assert "ONLY the output of the final step" in prompt
        assert "Do not emit" in prompt or "not emit intermediate" in prompt

    def test_compare_two_sided_mode_renders_both_sets(self):
        bundle = OperatorBundle(steps=(
            OperatorBundleStep(1, "compare", {"items_a": "alpha",
                                              "items_b": "beta",
                                              "focus": "alignment"}),
            OperatorBundleStep(2, "summarize", {}),
        ))
        prompt, _ = compose_bundle_prompt(bundle)
        assert "set A" in prompt
        assert "set B" in prompt
        assert "alpha" in prompt
        assert "beta" in prompt
        assert "alignment" in prompt

    def test_operator_prefix_stripped_in_prompt(self):
        """``operator_extract`` should render as plain ``extract`` in the prompt."""
        bundle = OperatorBundle(steps=(
            OperatorBundleStep(1, "operator_extract", {"fields": "x"}),
            OperatorBundleStep(2, "operator_summarize", {}),
        ))
        prompt, _ = compose_bundle_prompt(bundle)
        assert "STEP 1 — extract" in prompt  # not "operator_extract"
        assert "STEP 2 — summarize" in prompt

    def test_filter_step_renders_criterion_and_subset_guidance(self):
        """RDR-088 Phase 1 Step 2: a bundled filter step must render its
        criterion verbatim, inherit the prior step's output list, and
        carry the 'subset, never synthesize' invariant into the prompt."""
        bundle = OperatorBundle(steps=(
            OperatorBundleStep(
                1, "extract", {"fields": "id,title",
                               "inputs": "corpus-payload"},
            ),
            OperatorBundleStep(
                2, "filter",
                {"criterion": "peer-reviewed-only-sentinel"},
            ),
        ))
        prompt, schema = compose_bundle_prompt(bundle)
        assert "STEP 2 — filter" in prompt
        assert "peer-reviewed-only-sentinel" in prompt
        assert "output list from STEP 1" in prompt
        assert "subset" in prompt.lower()
        assert "never synthesize" in prompt.lower()
        # Terminal schema covers filter's {items, rationale} contract.
        assert "items" in schema["required"]
        assert "rationale" in schema["required"]
        rationale_schema = schema["properties"]["rationale"]["items"]
        assert "id" in rationale_schema["required"]
        assert "reason" in rationale_schema["required"]

    def test_check_step_renders_instruction_and_evidence_schema(self):
        """RDR-088 Phase 2: a bundled check step must render its
        check_instruction verbatim and the terminal schema must pin the
        {item_id, quote, role} evidence shape with role enum."""
        bundle = OperatorBundle(steps=(
            OperatorBundleStep(
                1, "extract", {"fields": "id,body",
                               "inputs": "papers-payload"},
            ),
            OperatorBundleStep(
                2, "check",
                {"check_instruction": "all-papers-agree-baseline-xyz"},
            ),
        ))
        prompt, schema = compose_bundle_prompt(bundle)
        assert "STEP 2 — check" in prompt
        assert "all-papers-agree-baseline-xyz" in prompt
        assert "output list from STEP 1" in prompt
        # Terminal schema covers check's {ok, evidence} contract.
        assert "ok" in schema["required"]
        assert "evidence" in schema["required"]
        evidence_item_schema = schema["properties"]["evidence"]["items"]
        assert {"item_id", "quote", "role"}.issubset(
            set(evidence_item_schema["required"]),
        )
        assert set(evidence_item_schema["properties"]["role"]["enum"]) == {
            "supports", "contradicts", "neutral",
        }

    def test_verify_step_renders_claim_and_verification_schema(self):
        """RDR-088 Phase 2: a bundled verify step must render its claim
        verbatim and the terminal schema must declare verified + reason +
        citations as required keys."""
        bundle = OperatorBundle(steps=(
            OperatorBundleStep(
                1, "extract", {"fields": "passage",
                               "inputs": "doc-payload"},
            ),
            OperatorBundleStep(
                2, "verify",
                {"claim": "claim-sentinel-verify-abc"},
            ),
        ))
        prompt, schema = compose_bundle_prompt(bundle)
        assert "STEP 2 — verify" in prompt
        assert "claim-sentinel-verify-abc" in prompt
        # The downstream input for verify is evidence, not items.
        assert "output text from STEP 1" in prompt
        assert {"verified", "reason", "citations"}.issubset(
            set(schema["required"]),
        )
        assert schema["properties"]["verified"]["type"] == "boolean"

    def test_groupby_step_renders_key_and_inline_items_schema(self):
        """RDR-093 Phase 1: a bundled groupby step must render its
        partition ``key`` verbatim, chain from a prior step's output,
        and the terminal schema must declare the C-1 inline-items
        contract (groups[].items as array of objects, NOT id strings)."""
        bundle = OperatorBundle(steps=(
            OperatorBundleStep(
                1, "filter", {"criterion": "peer-reviewed",
                              "inputs": "papers-payload"},
            ),
            OperatorBundleStep(
                2, "groupby",
                {"key": "publication-year-sentinel-key"},
            ),
        ))
        prompt, schema = compose_bundle_prompt(bundle)
        assert "STEP 2 — groupby" in prompt
        assert "publication-year-sentinel-key" in prompt
        assert "output list from STEP 1" in prompt
        # C-1 inline-items contract: groups carry items inline.
        assert "inline" in prompt.lower() or "carry" in prompt.lower()
        # Terminal schema covers groupby's {groups: [{key_value, items}]}
        # contract, with items as array of OBJECTS (not strings).
        assert "groups" in schema["required"]
        groups_item_schema = schema["properties"]["groups"]["items"]
        assert {"key_value", "items"}.issubset(set(groups_item_schema["required"]))
        assert groups_item_schema["properties"]["key_value"]["type"] == "string"
        assert groups_item_schema["properties"]["items"]["type"] == "array"
        # The C-1 inline-items regression guard at the bundle scope.
        assert (
            groups_item_schema["properties"]["items"]["items"]["type"]
            == "object"
        ), "C-1 contract: bundled groupby's terminal schema must require " \
           "items as array of objects (inline), not strings (id-only)"

    def test_operator_groupby_resolved_form_renders_as_groupby(self):
        """``operator_groupby`` should render as bare ``groupby`` in the
        prompt, mirroring the existing prefix-stripping for the other
        operators (per ``test_operator_prefix_stripped_in_prompt``)."""
        bundle = OperatorBundle(steps=(
            OperatorBundleStep(1, "operator_filter",
                               {"criterion": "x", "inputs": "p"}),
            OperatorBundleStep(2, "operator_groupby", {"key": "year"}),
        ))
        prompt, _ = compose_bundle_prompt(bundle)
        assert "STEP 2 — groupby" in prompt

    def test_aggregate_step_renders_reducer_and_per_group_schema(self):
        """RDR-093 Phase 2: a bundled aggregate step must render its
        reducer expression verbatim, chain from the prior groupby step,
        carry the per-group isolation directive, and the terminal
        schema must declare {aggregates: [{key_value, summary}]}."""
        bundle = OperatorBundle(steps=(
            OperatorBundleStep(
                1, "groupby", {"key": "method-family",
                               "inputs": "papers-payload"},
            ),
            OperatorBundleStep(
                2, "aggregate",
                {"reducer": "most-cited-method-sentinel"},
            ),
        ))
        prompt, schema = compose_bundle_prompt(bundle)
        assert "STEP 2 — aggregate" in prompt
        assert "most-cited-method-sentinel" in prompt
        assert "from STEP 1" in prompt
        # Per-group isolation directive must be present (mirrors the
        # standalone operator_aggregate prompt).
        prompt_lower = prompt.lower()
        assert "only" in prompt_lower and "group" in prompt_lower, (
            "RDR-093 §Risks and Mitigations: bundled aggregate must "
            "carry the same group-isolation directive as the standalone "
            "operator_aggregate prompt"
        )
        # Terminal schema covers aggregate's {aggregates: [{key_value, summary}]}.
        assert "aggregates" in schema["required"]
        agg_item = schema["properties"]["aggregates"]["items"]
        assert {"key_value", "summary"}.issubset(set(agg_item["required"]))
        assert agg_item["properties"]["key_value"]["type"] == "string"
        assert agg_item["properties"]["summary"]["type"] == "string"

    def test_operator_aggregate_resolved_form_renders_as_aggregate(self):
        """``operator_aggregate`` should render as bare ``aggregate``
        in the bundled prompt, mirroring the prefix-stripping pattern
        used by the other operators."""
        bundle = OperatorBundle(steps=(
            OperatorBundleStep(1, "operator_groupby",
                               {"key": "year", "inputs": "papers-payload"}),
            OperatorBundleStep(2, "operator_aggregate",
                               {"reducer": "most cited"}),
        ))
        prompt, _ = compose_bundle_prompt(bundle)
        assert "STEP 2 — aggregate" in prompt

    def test_groupby_aggregate_pair_bundles_into_one_dispatch(self):
        """RDR-093 §Decision Rationale: the canonical ``... -> groupby
        -> aggregate`` chain bundles into a single ``claude -p``
        dispatch. Verify both operators appear in the prompt and the
        terminal schema is aggregate's (not groupby's). The C-1
        contract makes this work — groupby's inline-items output is
        consumed by aggregate inside the same dispatch with no host-
        side retrieval."""
        bundle = OperatorBundle(steps=(
            OperatorBundleStep(1, "filter",
                               {"criterion": "peer-reviewed",
                                "inputs": "papers-payload"}),
            OperatorBundleStep(2, "groupby", {"key": "method family"}),
            OperatorBundleStep(3, "aggregate", {"reducer": "best baseline"}),
        ))
        prompt, schema = compose_bundle_prompt(bundle)
        assert "STEP 1 — filter" in prompt
        assert "STEP 2 — groupby" in prompt
        assert "STEP 3 — aggregate" in prompt
        # Terminal schema is the LAST step's = aggregate's.
        assert "aggregates" in schema["required"]
        # NOT the intermediate groupby's schema.
        assert "groups" not in schema.get("required", [])


# ── Dispatch wrapper ──────────────────────────────────────────────────────────


class TestDispatchBundle:

    @pytest.mark.asyncio
    async def test_dispatch_calls_claude_dispatch_once(self):
        """The whole point: one bundle → one subprocess spawn."""
        bundle = OperatorBundle(steps=(
            OperatorBundleStep(1, "extract", {"fields": "title"}),
            OperatorBundleStep(2, "summarize", {"cited": False}),
        ))
        fake = AsyncMock(return_value={"summary": "the answer"})
        with patch("nexus.operators.dispatch.claude_dispatch", fake):
            result = await dispatch_bundle(bundle, timeout=42.0)

        assert result == {"summary": "the answer"}
        assert fake.call_count == 1
        args, kwargs = fake.call_args
        # Prompt is first positional
        assert "STEP 1 — extract" in args[0]
        assert "STEP 2 — summarize" in args[0]
        # Schema passed and timeout forwarded
        assert args[1]["required"] == ["summary"]
        assert kwargs["timeout"] == 42.0

    @pytest.mark.asyncio
    async def test_dispatch_propagates_subprocess_errors(self):
        """A failing claude_dispatch must surface, not get swallowed."""
        bundle = OperatorBundle(steps=(
            OperatorBundleStep(1, "extract", {}),
            OperatorBundleStep(2, "summarize", {}),
        ))
        fake = AsyncMock(side_effect=RuntimeError("subprocess exploded"))
        with patch("nexus.operators.dispatch.claude_dispatch", fake):
            with pytest.raises(RuntimeError, match="subprocess exploded"):
                await dispatch_bundle(bundle)


# ── Sentinel exposure ─────────────────────────────────────────────────────────


class TestBundledIntermediateSentinel:

    def test_sentinel_is_explicit(self):
        """The runner stamps this into step_outputs for bundled intermediates;
        any ``$stepN.<field>`` against a bundled intermediate trips the
        normal 'field missing' path in the runner's resolver."""
        assert BUNDLED_INTERMEDIATE["_bundled_intermediate"] is True
        assert "note" in BUNDLED_INTERMEDIATE or any(
            "intermediate" in str(v) for v in BUNDLED_INTERMEDIATE.values()
        )


# ── plan_run integration ──────────────────────────────────────────────────────


def _make_match_from_plan(plan_json: str):
    """Build a Match backed by a plain plan_json, no default bindings."""
    from nexus.plans.match import Match
    return Match(
        plan_id=1, name="test", description="",
        confidence=0.9, dimensions={}, tags="",
        plan_json=plan_json,
        required_bindings=[], optional_bindings=[],
        default_bindings={}, parent_dims=None,
    )


class TestPlanRunBundleIntegration:
    """Verify plan_run routes contiguous operators through dispatch_bundle
    when ``bundle_operators=True`` (default) and falls back to per-step
    dispatch when the flag is off."""

    @pytest.mark.asyncio
    async def test_two_op_bundle_dispatches_once(self):
        """search + extract + summarize → 1 search call + 1 bundle call."""
        from nexus.plans.runner import plan_run

        plan_json = json.dumps({"steps": [
            {"tool": "search", "args": {"query": "x", "corpus": "knowledge"}},
            {"tool": "extract", "args": {"fields": "title", "inputs": "[]"}},
            {"tool": "summarize", "args": {"cited": False, "content": "x"}},
        ]})

        fake_bundle = AsyncMock(return_value={"summary": "synthesis"})

        async def fake_dispatch(tool, args):
            return {"ids": ["a", "b"], "tumblers": [], "distances": [],
                    "collections": []}

        # Patch claude_dispatch indirectly by patching dispatch_bundle's
        # underlying call path.
        with patch("nexus.plans.bundle.dispatch_bundle", fake_bundle), \
             patch("nexus.plans.runner._default_dispatcher", fake_dispatch):
            # bundle_operators check keys off `dispatch is _default_dispatcher`
            # — the inline patch above makes that identity check fail, so we
            # have to invoke through the plan_run code path that preserves it.
            # Instead: use the real _default_dispatcher but mock the inner
            # MCP-tool call so search returns a stub dict.
            pass

        # Re-run using a clean strategy: pass dispatch=None (uses default),
        # mock dispatch_bundle directly, and also mock the search MCP tool
        # so we don't hit real T3.
        async def stub_search(**kwargs):
            return {"ids": ["a", "b"], "tumblers": ["1.1", "1.2"],
                    "distances": [0.1, 0.2], "collections": ["kn"]}

        from nexus.mcp import core as mcp_core
        fake_bundle.reset_mock()
        with patch("nexus.plans.bundle.dispatch_bundle", fake_bundle), \
             patch.object(mcp_core, "search", stub_search):
            result = await plan_run(_make_match_from_plan(plan_json))

        # Exactly ONE bundle call for the extract+summarize pair.
        assert fake_bundle.call_count == 1
        # Three step_outputs: search result, bundled-intermediate, terminal.
        assert len(result.steps) == 3
        assert result.steps[1].get("_bundled_intermediate") is True
        assert result.steps[2] == {"summary": "synthesis"}
        # final alias points at the last slot.
        assert result.final == {"summary": "synthesis"}

    @pytest.mark.asyncio
    async def test_bundle_flag_false_falls_through_to_per_step(self):
        """bundle_operators=False must use the per-step dispatcher."""
        from nexus.plans.runner import plan_run

        plan_json = json.dumps({"steps": [
            {"tool": "extract", "args": {"fields": "a", "inputs": "[]"}},
            {"tool": "summarize", "args": {"cited": False, "content": "x"}},
        ]})

        fake_bundle = AsyncMock(return_value={"summary": "x"})
        async def stub_extract(**kwargs):
            return {"extractions": [{"a": 1}]}
        async def stub_summarize(**kwargs):
            return {"summary": "per-step"}
        from nexus.mcp import core as mcp_core
        with patch("nexus.plans.bundle.dispatch_bundle", fake_bundle), \
             patch.object(mcp_core, "operator_extract", stub_extract), \
             patch.object(mcp_core, "operator_summarize", stub_summarize):
            result = await plan_run(
                _make_match_from_plan(plan_json),
                bundle_operators=False,
            )

        assert fake_bundle.call_count == 0, "bundle dispatched despite flag off"
        assert len(result.steps) == 2
        assert result.steps[0] == {"extractions": [{"a": 1}]}
        assert result.steps[1] == {"summary": "per-step"}

    @pytest.mark.asyncio
    async def test_single_operator_is_not_bundled(self):
        """Lone extract step goes through per-step dispatch, not bundle."""
        from nexus.plans.runner import plan_run

        plan_json = json.dumps({"steps": [
            {"tool": "extract", "args": {"fields": "x", "inputs": "[]"}},
        ]})

        fake_bundle = AsyncMock(return_value={"extractions": []})
        async def stub_extract(**kwargs):
            return {"extractions": [{"x": 1}]}
        from nexus.mcp import core as mcp_core
        with patch("nexus.plans.bundle.dispatch_bundle", fake_bundle), \
             patch.object(mcp_core, "operator_extract", stub_extract):
            result = await plan_run(_make_match_from_plan(plan_json))

        assert fake_bundle.call_count == 0
        assert result.steps == [{"extractions": [{"x": 1}]}]

    @pytest.mark.asyncio
    async def test_custom_dispatcher_disables_bundling(self):
        """A caller-supplied dispatcher means per-step regardless of flag.

        Tests + legacy fixtures use custom dispatchers to inject synthetic
        outputs; we don't want the bundle path intercepting those.
        """
        from nexus.plans.runner import plan_run

        plan_json = json.dumps({"steps": [
            {"tool": "extract", "args": {"fields": "a"}},
            {"tool": "summarize", "args": {}},
        ]})

        fake_bundle = AsyncMock(return_value={"summary": "x"})
        calls: list[str] = []

        async def custom_dispatch(tool, args):
            calls.append(tool)
            return {"echo": tool}

        with patch("nexus.plans.bundle.dispatch_bundle", fake_bundle):
            result = await plan_run(
                _make_match_from_plan(plan_json),
                dispatcher=custom_dispatch,
                bundle_operators=True,  # on, but should be overridden
            )

        assert fake_bundle.call_count == 0
        assert calls == ["extract", "summarize"]
        assert len(result.steps) == 2

    @pytest.mark.asyncio
    async def test_bundle_intra_step_reference_renders_as_prose(self):
        """A bundled step referencing an earlier bundled step's output
        via ``$stepN.field`` should render the reference as "STEP M's
        <field> output" in the composite prompt, NOT raise."""
        from nexus.plans.runner import plan_run

        # Two bundled operators: step2 references step1.extractions.
        plan_json = json.dumps({"steps": [
            {"tool": "extract", "args": {"fields": "a", "inputs": "[]"}},
            {"tool": "rank",
             "args": {"criterion": "y",
                      "items": "$step1.extractions"}},
        ]})

        captured_bundle: list = []

        async def capture(bundle, **kwargs):
            captured_bundle.append(bundle)
            return {"ranked": ["x"]}

        with patch("nexus.plans.bundle.dispatch_bundle", capture):
            result = await plan_run(_make_match_from_plan(plan_json))

        assert len(captured_bundle) == 1
        bundle = captured_bundle[0]
        # Inspect the rank step's args — `items` should be the deferred
        # sentinel, NOT the literal string "$step1.extractions".
        rank_step = bundle.steps[1]
        assert isinstance(rank_step.args["items"], dict)
        assert rank_step.args["items"].get("__nexus_deferred_step_ref__") is True
        assert rank_step.args["items"]["step_index"] == 0
        assert rank_step.args["items"]["field"] == "extractions"

        # The terminal output lands in step_outputs[1].
        assert result.steps[0]["_bundled_intermediate"] is True
        assert result.steps[1] == {"ranked": ["x"]}

    @pytest.mark.asyncio
    async def test_parallel_branch_extracts_both_inline_with_source(self):
        """Cross-repo bundle: two extracts hydrating from different corpora
        must BOTH inline their concrete inputs in the prompt AND carry a
        'source:' line so the LLM can attribute extractions to the right
        corpus. Without this, the bundled LLM hallucinates identical
        sources and concludes 'no divergence'."""
        from nexus.plans.bundle import OperatorBundle, OperatorBundleStep, \
            compose_bundle_prompt

        bundle = OperatorBundle(steps=(
            OperatorBundleStep(
                plan_index=2, tool="extract",
                args={"fields": "tradeoff",
                      "inputs": '["Arcaneum doc: Qdrant HNSW tuning"]'},
                source_collections="rdr__arcaneum-2ad2825c",
            ),
            OperatorBundleStep(
                plan_index=3, tool="extract",
                args={"fields": "tradeoff",
                      "inputs": '["Nexus doc: ChromaDB quota management"]'},
                source_collections="rdr__nexus-571b8edd",
            ),
            OperatorBundleStep(
                plan_index=4, tool="compare",
                args={
                    "focus": "philosophy",
                    "items_a": {"__nexus_deferred_step_ref__": True,
                                "step_index": 2, "field": "extractions"},
                    "items_b": {"__nexus_deferred_step_ref__": True,
                                "step_index": 3, "field": "extractions"},
                    "label_a": "Arcaneum",
                    "label_b": "Nexus",
                },
            ),
        ))
        prompt, _ = compose_bundle_prompt(bundle)

        # Both extracts must inline their concrete content
        assert '"Arcaneum doc: Qdrant HNSW tuning"' in prompt
        assert '"Nexus doc: ChromaDB quota management"' in prompt
        # Both extracts must carry source attribution
        assert "source: rdr__arcaneum-2ad2825c" in prompt
        assert "source: rdr__nexus-571b8edd" in prompt
        # The header must frame concrete-input steps as parallel branches
        assert "independent branches" in prompt or "not chained" in prompt
        # Compare still references them via bundle-local step numbers
        assert "extractions` output from STEP 1" in prompt
        assert "extractions` output from STEP 2" in prompt

    @pytest.mark.asyncio
    async def test_non_first_concrete_value_is_inlined(self):
        """Regression: a second bundled step with concrete `inputs` must
        inline the inputs, NOT fall through to 'chain from STEP N-1'."""
        from nexus.plans.bundle import OperatorBundle, OperatorBundleStep, \
            compose_bundle_prompt

        bundle = OperatorBundle(steps=(
            OperatorBundleStep(1, "extract",
                               {"fields": "a", "inputs": "[DOC_ALPHA]"}),
            OperatorBundleStep(2, "extract",
                               {"fields": "b", "inputs": "[DOC_BETA]"}),
        ))
        prompt, _ = compose_bundle_prompt(bundle)
        # Both inputs must appear; the second one must NOT be replaced
        # by "extractions array from STEP 1" default prose.
        assert "[DOC_ALPHA]" in prompt
        assert "[DOC_BETA]" in prompt
        # The default-prose fallback should NOT fire here since both
        # values are concrete. Check that STEP 2 doesn't fall back to
        # the "extractions from STEP 1" chain prose.
        step2_start = prompt.index("STEP 2")
        step2_block = prompt[step2_start:step2_start + 300]
        assert "extractions` array from STEP 1" not in step2_block, (
            "STEP 2 fell back to chain prose despite having concrete inputs"
        )

    @pytest.mark.asyncio
    async def test_bundle_prompt_plan_to_local_mapping(self):
        """When a bundle starts mid-plan (e.g. after two retrieval steps),
        the deferred-ref sentinel's plan_index must translate to the
        bundle-local 1-based position, not to the plan index.

        Example: plan has [search, search, extract, extract, compare].
        Bundle = [extract(plan_idx=2), extract(plan_idx=3), compare(plan_idx=4)].
        compare's `items_a = $step3.extractions` points at plan_index=2,
        which is the FIRST bundled step → must render as "STEP 1".
        """
        from nexus.plans.bundle import OperatorBundle, OperatorBundleStep, \
            compose_bundle_prompt

        bundle = OperatorBundle(steps=(
            OperatorBundleStep(2, "extract", {"fields": "a", "inputs": "[A]"}),
            OperatorBundleStep(3, "extract", {"fields": "a", "inputs": "[B]"}),
            OperatorBundleStep(4, "compare", {
                "focus": "philosophy",
                "items_a": {
                    "__nexus_deferred_step_ref__": True,
                    "step_index": 2, "field": "extractions",
                },
                "items_b": {
                    "__nexus_deferred_step_ref__": True,
                    "step_index": 3, "field": "extractions",
                },
                "label_a": "Arcaneum",
                "label_b": "Nexus",
            }),
        ))
        prompt, _ = compose_bundle_prompt(bundle)
        # plan_index=2 (first bundled step) should render as "STEP 1"
        assert "extractions` output from STEP 1" in prompt
        # plan_index=3 (second bundled step) should render as "STEP 2"
        assert "extractions` output from STEP 2" in prompt
        # And custom labels must carry into the two-sided compare prompt.
        assert "set Arcaneum:" in prompt
        assert "set Nexus:" in prompt

    @pytest.mark.asyncio
    async def test_bundle_prompt_renders_deferred_ref_as_prose(self):
        """End-to-end: compose_bundle_prompt MUST translate the deferred
        sentinel into human-readable chain prose (so claude -p understands
        what to chain), not into a bare JSON dump."""
        from nexus.plans.bundle import OperatorBundle, OperatorBundleStep, \
            compose_bundle_prompt

        bundle = OperatorBundle(steps=(
            OperatorBundleStep(0, "extract",
                               {"fields": "a", "inputs": "[paper-1]"}),
            OperatorBundleStep(1, "rank", {
                "criterion": "recency",
                "items": {
                    "__nexus_deferred_step_ref__": True,
                    "step_index": 0,
                    "field": "extractions",
                },
            }),
        ))
        prompt, _ = compose_bundle_prompt(bundle)
        # The rank step's `items` must render as "from STEP 1" prose,
        # not as a literal dict.
        assert "extractions` output from STEP 1" in prompt
        assert "__nexus_deferred_step_ref__" not in prompt  # sentinel leaked

    @pytest.mark.asyncio
    async def test_oversized_bundle_falls_back_to_per_step_dispatch(self):
        """When the composite bundle prompt exceeds MAX_BUNDLE_PROMPT_CHARS,
        the runner must fall back to per-step dispatch for that segment
        rather than letting an oversized prompt hit claude -p. The fallback
        re-resolves args without deferred indices so intra-bundle refs
        resolve against real step_outputs. (substantive-critic Obs B)"""
        from nexus.plans.runner import plan_run
        from nexus.plans import bundle as _bundle

        # Craft a plan where the FIRST bundled step has a huge literal
        # `inputs` value — big enough that compose_bundle_prompt will
        # exceed the char budget.
        huge = "X" * 250_000  # > MAX_BUNDLE_PROMPT_CHARS (200k)
        plan_json = json.dumps({"steps": [
            {"tool": "extract",
             "args": {"fields": "a", "inputs": huge}},
            {"tool": "summarize",
             "args": {"cited": False, "content": "x"}},
        ]})

        bundle_call = AsyncMock(return_value={"summary": "won't fire"})
        extract_call = AsyncMock(return_value={"extractions": [{"a": 1}]})
        summarize_call = AsyncMock(return_value={"summary": "per-step"})

        from nexus.mcp import core as mcp_core
        with patch.object(_bundle, "dispatch_bundle", bundle_call), \
             patch.object(mcp_core, "operator_extract", extract_call), \
             patch.object(mcp_core, "operator_summarize", summarize_call):
            result = await plan_run(_make_match_from_plan(plan_json))

        # Bundle dispatcher must NOT fire — fell back to per-step.
        assert bundle_call.call_count == 0
        # Both operators were dispatched individually via the default
        # dispatcher path.
        assert extract_call.call_count == 1
        assert summarize_call.call_count == 1
        # step_outputs contains real dicts (no BUNDLED_INTERMEDIATE sentinel).
        assert len(result.steps) == 2
        assert result.steps[0] == {"extractions": [{"a": 1}]}
        assert result.steps[1] == {"summary": "per-step"}
        assert "_bundled_intermediate" not in result.steps[0]
        assert "_bundled_intermediate" not in result.steps[1]

    @pytest.mark.asyncio
    async def test_supports_bundling_marker_attr_on_default_dispatcher(self):
        """The default dispatcher carries the ``supports_bundling``
        attribute so plan_run can gate on capability, not identity.
        A decorator wrapping the default would need to propagate the
        attribute (or set its own) to keep bundling enabled.
        (substantive-critic Obs D)"""
        from nexus.plans.runner import _default_dispatcher

        assert getattr(_default_dispatcher, "supports_bundling", False) is True

    @pytest.mark.asyncio
    async def test_wrapped_dispatcher_without_marker_skips_bundling(self):
        """A callable that forwards to the default but DOESN'T carry the
        ``supports_bundling`` marker must not be routed through the
        bundle path. Prevents subtle regressions from wrappers added
        in future refactors."""
        from nexus.plans.runner import plan_run, _default_dispatcher

        plan_json = json.dumps({"steps": [
            {"tool": "extract", "args": {"fields": "a"}},
            {"tool": "summarize", "args": {}},
        ]})

        fake_bundle = AsyncMock(return_value={"summary": "x"})
        calls: list[str] = []

        async def untagged_wrapper(tool, args):
            """Wrapper that forwards but doesn't inherit supports_bundling."""
            calls.append(tool)
            return {"echo": tool}

        # Deliberately do NOT set supports_bundling on untagged_wrapper.
        with patch("nexus.plans.bundle.dispatch_bundle", fake_bundle):
            await plan_run(
                _make_match_from_plan(plan_json),
                dispatcher=untagged_wrapper,
                bundle_operators=True,  # on, but wrapper doesn't opt in
            )

        assert fake_bundle.call_count == 0
        assert calls == ["extract", "summarize"]

    @pytest.mark.asyncio
    async def test_wrapped_dispatcher_with_marker_still_bundles(self):
        """A wrapper that DOES carry the marker keeps bundling enabled —
        the design's extensibility story."""
        from nexus.plans.runner import plan_run, _default_dispatcher

        plan_json = json.dumps({"steps": [
            {"tool": "extract", "args": {"fields": "a", "inputs": "[]"}},
            {"tool": "summarize", "args": {"cited": False, "content": "x"}},
        ]})

        fake_bundle = AsyncMock(return_value={"summary": "bundled"})

        async def tagged_wrapper(tool, args):
            return await _default_dispatcher(tool, args)
        tagged_wrapper.supports_bundling = True  # opt in

        with patch("nexus.plans.bundle.dispatch_bundle", fake_bundle):
            await plan_run(
                _make_match_from_plan(plan_json),
                dispatcher=tagged_wrapper,
            )

        # Bundling fired exactly once.
        assert fake_bundle.call_count == 1

    @pytest.mark.asyncio
    async def test_post_bundle_step_referencing_intermediate_raises_named_error(self):
        """A step AFTER the bundle that references a bundled intermediate
        slot must raise PlanRunStepRefError with a message that explicitly
        names bundling as the cause — not the generic
        ``have: ['_bundled_intermediate', '_note']`` surface.
        (substantive-critic S6)"""
        from nexus.plans.runner import plan_run, PlanRunStepRefError

        # Plan: extract → summarize (bundled) → search (post-bundle, tries
        # to reference $step1.extractions — an intermediate inside the bundle).
        plan_json = json.dumps({"steps": [
            {"tool": "extract", "args": {"fields": "a", "inputs": "[]"}},
            {"tool": "summarize", "args": {"cited": False, "content": "x"}},
            {"tool": "search",
             "args": {"query": "$step1.extractions", "corpus": "knowledge"}},
        ]})

        fake_bundle = AsyncMock(return_value={"summary": "done"})
        with patch("nexus.plans.bundle.dispatch_bundle", fake_bundle):
            with pytest.raises(PlanRunStepRefError) as exc_info:
                await plan_run(_make_match_from_plan(plan_json))

        msg = str(exc_info.value)
        assert "intermediate inside an operator bundle" in msg
        assert "FINAL step of the bundle" in msg or "bundle_operators=False" in msg

    @pytest.mark.asyncio
    async def test_three_op_bundle_with_leading_retrieval(self):
        """search → extract → rank → summarize: 1 search + 1 bundle of 3."""
        from nexus.plans.runner import plan_run

        plan_json = json.dumps({"steps": [
            {"tool": "search", "args": {"query": "x", "corpus": "knowledge"}},
            {"tool": "extract", "args": {"fields": "a", "inputs": "[]"}},
            {"tool": "rank", "args": {"criterion": "y", "items": "[]"}},
            {"tool": "summarize", "args": {"cited": False, "content": "x"}},
        ]})

        fake_bundle = AsyncMock(return_value={"summary": "final"})
        async def stub_search(**kwargs):
            return {"ids": [], "tumblers": [], "distances": [],
                    "collections": []}
        from nexus.mcp import core as mcp_core
        with patch("nexus.plans.bundle.dispatch_bundle", fake_bundle), \
             patch.object(mcp_core, "search", stub_search):
            result = await plan_run(_make_match_from_plan(plan_json))

        assert fake_bundle.call_count == 1
        # Inspect the bundle passed in — should have 3 operator steps.
        bundle_arg = fake_bundle.call_args[0][0]
        assert len(bundle_arg.steps) == 3
        assert [s.tool for s in bundle_arg.steps] == ["extract", "rank", "summarize"]
        # step_outputs: [search, intermediate, intermediate, terminal]
        assert len(result.steps) == 4
        assert result.steps[1]["_bundled_intermediate"] is True
        assert result.steps[2]["_bundled_intermediate"] is True
        assert result.steps[3] == {"summary": "final"}
