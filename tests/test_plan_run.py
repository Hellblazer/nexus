# SPDX-License-Identifier: AGPL-3.0-or-later
"""TDD tests for ``plan_run`` — RDR-078 P1 (nexus-05i.2).

Covers:

  * ``$var`` placeholder resolution (caller bindings + default_bindings,
    caller wins on conflict).
  * ``$stepN.<field>`` reference resolution from the prior step output
    contract (RDR-078 §Phase 1, retrieval steps emit ``{tumblers, ids,
    distances}``; operators emit ``{text, citations}``).
  * ``PlanRunBindingError(missing=[...])`` — required binding unresolved.
  * ``PlanRunStepRefError`` — bad ``$stepN.<field>`` reference.
  * ``PlanRunEmbeddingDomainError`` (SC-10) — step declares
    ``scope.taxonomy_domain`` that doesn't match the dispatched
    collection's embedding model.

The tests inject a fake ``ToolDispatcher`` so we exercise the runner
without real MCP tools running.
"""
from __future__ import annotations

import json

import pytest


# ── Fixtures ────────────────────────────────────────────────────────────────


def _match(plan: dict, *, default_bindings: dict | None = None) -> "Match":  # noqa: F821
    """Build a ``Match`` from an inline plan dict."""
    from nexus.plans.match import Match

    return Match(
        plan_id=1, name="default", description="test",
        confidence=0.9, dimensions={"verb": "test"},
        tags="", plan_json=json.dumps(plan),
        required_bindings=list(plan.get("required_bindings", []) or []),
        optional_bindings=list(plan.get("optional_bindings", []) or []),
        default_bindings=default_bindings or {},
        parent_dims=None,
    )


class _FakeDispatcher:
    """Records every dispatch call and returns scripted outputs.

    Async since RDR-079 P4 — the runner awaits dispatchers.
    """

    def __init__(self, outputs: list[dict] | None = None) -> None:
        self.calls: list[tuple[str, dict]] = []
        self._outputs = list(outputs or [])

    async def __call__(self, tool: str, args: dict) -> dict:
        self.calls.append((tool, args))
        if self._outputs:
            return self._outputs.pop(0)
        return {"text": f"{tool}(stub)"}


# ── Variable resolution ────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_run_resolves_caller_var_in_args() -> None:
    from nexus.plans.runner import plan_run

    plan = {
        "steps": [
            {"tool": "search", "args": {"query": "$intent", "limit": 5}},
        ],
        "required_bindings": ["intent"],
    }
    disp = _FakeDispatcher([{"text": "ok"}])
    await plan_run(_match(plan), {"intent": "how does X work"}, dispatcher=disp)

    assert disp.calls[0] == (
        "search", {"query": "how does X work", "limit": 5},
    )


@pytest.mark.asyncio
async def test_run_caller_binding_overrides_default() -> None:
    """default_bindings + caller_bindings: caller wins on conflict."""
    from nexus.plans.runner import plan_run

    plan = {
        "steps": [{"tool": "search", "args": {"query": "$intent"}}],
        "required_bindings": ["intent"],
    }
    match = _match(plan, default_bindings={"intent": "default"})
    disp = _FakeDispatcher()
    await plan_run(match, {"intent": "caller"}, dispatcher=disp)
    assert disp.calls[0][1] == {"query": "caller"}


@pytest.mark.asyncio
async def test_run_falls_back_to_default_when_caller_omits() -> None:
    from nexus.plans.runner import plan_run

    plan = {
        "steps": [{"tool": "search", "args": {"query": "$intent"}}],
        "required_bindings": ["intent"],
    }
    match = _match(plan, default_bindings={"intent": "from-default"})
    disp = _FakeDispatcher()
    await plan_run(match, {}, dispatcher=disp)
    assert disp.calls[0][1] == {"query": "from-default"}


@pytest.mark.asyncio
async def test_run_rejects_missing_required_binding() -> None:
    from nexus.plans.runner import PlanRunBindingError, plan_run

    plan = {
        "steps": [{"tool": "search", "args": {"query": "$intent"}}],
        "required_bindings": ["intent", "subtree"],
    }
    with pytest.raises(PlanRunBindingError) as exc:
        await plan_run(_match(plan), {"intent": "x"}, dispatcher=_FakeDispatcher())
    assert sorted(exc.value.missing) == ["subtree"]


# ── $stepN.<field> reference ───────────────────────────────────────────────


@pytest.mark.asyncio
async def test_run_resolves_step_ref_to_prior_output_field() -> None:
    """``$stepN.field`` reads the field from the Nth step's stashed output."""
    from nexus.plans.runner import plan_run

    plan = {
        "steps": [
            {"tool": "search", "args": {"query": "concept"}},
            {"tool": "summarize", "args": {"corpus": "$step1.text"}},
        ],
    }
    disp = _FakeDispatcher([
        {"text": "first-result", "tumblers": ["1.1"]},
        {"text": "summary"},
    ])
    await plan_run(_match(plan), {}, dispatcher=disp)
    assert disp.calls[1] == ("summarize", {"corpus": "first-result"})


@pytest.mark.asyncio
async def test_run_resolves_step_ref_for_list_field() -> None:
    """Lists pass through verbatim — caller may consume the list as-is."""
    from nexus.plans.runner import plan_run

    plan = {
        "steps": [
            {"tool": "search", "args": {"query": "concept"}},
            {"tool": "extract", "args": {"ids": "$step1.ids"}},
        ],
    }
    disp = _FakeDispatcher([
        {"ids": ["a", "b", "c"], "tumblers": ["1.1", "1.2", "1.3"]},
        {"text": "extracted"},
    ])
    await plan_run(_match(plan), {}, dispatcher=disp)
    assert disp.calls[1] == ("extract", {"ids": ["a", "b", "c"]})


@pytest.mark.asyncio
async def test_default_dispatcher_wraps_str_return_as_text_dict() -> None:
    """Non-retrieval MCP tools return str (human-readable). The runner
    requires dict. The default dispatcher must wrap str → {"text": ...}
    so plan_run works end-to-end with real MCP tools.

    Note: as of RDR-079 P1, retrieval tools (search, query) are auto-
    promoted to structured=True and return dict directly. This test
    covers the OTHER path: non-retrieval tools that still return str
    (e.g., `plan_search` when called without structured flag)."""
    from nexus.plans.runner import _default_dispatcher

    # plan_search — not in _RETRIEVAL_TOOLS, returns str by default.
    result = await _default_dispatcher(
        "plan_search", {"query": "no-such-plan-xyz", "project": "none"},
    )
    assert isinstance(result, dict), (
        "default dispatcher must normalize str return into dict form"
    )
    assert "text" in result, (
        "str-returning MCP tools must be wrapped as {'text': ...}"
    )
    assert isinstance(result["text"], str)


@pytest.mark.asyncio
async def test_default_dispatcher_auto_injects_structured_for_retrieval_tools() -> None:
    """RDR-079 P1: search and query are auto-promoted to structured=True
    by the dispatcher so plan steps receive the runner-contract dict."""
    from nexus.plans.runner import _default_dispatcher

    result = await _default_dispatcher(
        "search",
        {"query": "nothing-indexed-sentinel-xyz", "corpus": "knowledge", "limit": 1},
    )
    assert isinstance(result, dict)
    # Must be the runner-contract shape, not {"text": str}.
    assert "ids" in result
    assert "tumblers" in result
    assert "distances" in result
    assert "collections" in result


@pytest.mark.asyncio
async def test_default_dispatcher_passes_through_dict_return() -> None:
    """The `traverse` MCP tool returns dict directly — must not be
    re-wrapped. Verified by stub-calling a dict-returning function
    via the dispatcher."""
    from nexus.plans.runner import _default_dispatcher

    # Use plan_search via _default_dispatcher is fine (returns str wrapped),
    # but the contract pin is that dict returns pass through verbatim. We
    # test this by inspecting that _default_dispatcher for a real dict-
    # returning tool (traverse) does NOT add an extra 'text' field.
    # Seed an empty traverse — returns {'error': ..., 'tumblers': [], ...}.
    result = await _default_dispatcher(
        "traverse", {"seeds": [], "link_types": [], "depth": 1},
    )
    assert isinstance(result, dict)
    assert "tumblers" in result  # dict passed through unchanged


@pytest.mark.asyncio
async def test_default_dispatcher_raises_tool_not_found_for_unknown_tool() -> None:
    """Unknown tool → PlanRunToolNotFoundError, not PlanRunStepRefError.

    The two are distinct failure modes: step-ref errors mean a
    ``$stepN.field`` pointer is wrong; tool-not-found means the plan
    names a callable that the dispatcher doesn't have. Conflating them
    hurts error-driven branching at the caller."""
    from nexus.plans.runner import (
        PlanRunStepRefError,
        PlanRunToolNotFoundError,
        _default_dispatcher,
    )

    with pytest.raises(PlanRunToolNotFoundError) as exc:
        await _default_dispatcher("definitely_not_a_real_tool_xyz", {})
    # And it's NOT a PlanRunStepRefError (the previous conflated type).
    assert not isinstance(exc.value, PlanRunStepRefError)
    assert "definitely_not_a_real_tool_xyz" in str(exc.value)


@pytest.mark.asyncio
async def test_run_resolves_list_of_step_refs_flattens() -> None:
    """``[$step1.ids, $step2.ids]`` resolves element-wise and flattens one
    level — callers combining outputs from multiple prior steps can use
    the list literal shape directly. Regression for RDR-078 critique finding
    that analyze-default.yml had no way to combine prose + code corpora."""
    from nexus.plans.runner import plan_run

    plan = {
        "steps": [
            {"tool": "search", "args": {"query": "p"}},
            {"tool": "search", "args": {"query": "c"}},
            {"tool": "rank", "args": {"candidates": ["$step1.ids", "$step2.ids"]}},
        ],
    }
    disp = _FakeDispatcher([
        {"ids": ["a", "b"], "tumblers": []},
        {"ids": ["c", "d"], "tumblers": []},
        {"text": "ranked"},
    ])
    await plan_run(_match(plan), {}, dispatcher=disp)
    assert disp.calls[2] == ("rank", {"candidates": ["a", "b", "c", "d"]})


@pytest.mark.asyncio
async def test_run_step_ref_unknown_field_raises() -> None:
    from nexus.plans.runner import PlanRunStepRefError, plan_run

    plan = {
        "steps": [
            {"tool": "search", "args": {"query": "x"}},
            {"tool": "rank", "args": {"by": "$step1.bogus_field"}},
        ],
    }
    disp = _FakeDispatcher([{"text": "first", "tumblers": []}])
    with pytest.raises(PlanRunStepRefError) as exc:
        await plan_run(_match(plan), {}, dispatcher=disp)
    assert "$step1.bogus_field" in str(exc.value)


@pytest.mark.asyncio
async def test_run_step_ref_to_missing_step_raises() -> None:
    from nexus.plans.runner import PlanRunStepRefError, plan_run

    plan = {
        "steps": [
            {"tool": "search", "args": {"query": "$step5.text"}},
        ],
    }
    with pytest.raises(PlanRunStepRefError):
        await plan_run(_match(plan), {}, dispatcher=_FakeDispatcher())


# ── Cross-embedding guard (SC-10) ──────────────────────────────────────────


@pytest.mark.asyncio
async def test_run_rejects_cross_embedding_dispatch() -> None:
    """``scope.taxonomy_domain=code`` cannot dispatch to a ``docs__``
    collection (whose embedding model is ``voyage-context-3``)."""
    from nexus.plans.runner import PlanRunEmbeddingDomainError, plan_run

    plan = {
        "steps": [
            {
                "tool": "search",
                "args": {"query": "x", "collection": "docs__corpus"},
                "scope": {"taxonomy_domain": "code"},
            },
        ],
    }
    with pytest.raises(PlanRunEmbeddingDomainError) as exc:
        await plan_run(_match(plan), {}, dispatcher=_FakeDispatcher())
    msg = str(exc.value)
    assert "code" in msg
    assert "docs__corpus" in msg


@pytest.mark.asyncio
async def test_run_allows_matching_taxonomy_domain() -> None:
    from nexus.plans.runner import plan_run

    plan = {
        "steps": [
            {
                "tool": "search",
                "args": {"query": "x", "collection": "code__myrepo"},
                "scope": {"taxonomy_domain": "code"},
            },
        ],
    }
    disp = _FakeDispatcher([{"text": "ok", "ids": []}])
    await plan_run(_match(plan), {}, dispatcher=disp)
    assert disp.calls[0][0] == "search"


@pytest.mark.asyncio
async def test_run_allows_step_without_scope() -> None:
    """No ``scope`` declared → no embedding-domain check."""
    from nexus.plans.runner import plan_run

    plan = {
        "steps": [
            {"tool": "search", "args": {"query": "x", "collection": "any__name"}},
        ],
    }
    await plan_run(_match(plan), {}, dispatcher=_FakeDispatcher([{"text": "ok"}]))


@pytest.mark.asyncio
async def test_run_traverse_step_skips_embedding_check() -> None:
    """``traverse`` operates on tumblers — no embeddings involved."""
    from nexus.plans.runner import plan_run

    plan = {
        "steps": [
            {
                "tool": "traverse",
                "args": {"seeds": ["1.1"], "purpose": "find-implementations"},
                "scope": {"taxonomy_domain": "code"},
            },
        ],
    }
    disp = _FakeDispatcher([{"tumblers": ["1.1.1"], "ids": []}])
    await plan_run(_match(plan), {}, dispatcher=disp)


# ── Caller-supplied scope override (nexus-zs1d Phase 1) ────────────────────


@pytest.mark.asyncio
async def test_run_caller_scope_fills_unset_corpus_on_search() -> None:
    """Caller's ``_nx_scope`` binding fills in ``corpus`` on a search step
    that doesn't pin one. RDR-compatible with existing plans."""
    from nexus.plans.runner import plan_run

    plan = {"steps": [{"tool": "search", "args": {"query": "x"}}]}
    disp = _FakeDispatcher([{"text": "ok", "ids": []}])
    await plan_run(
        _match(plan),
        {"_nx_scope": "rdr__arcaneum-2ad2825c"},
        dispatcher=disp,
    )
    _tool, args = disp.calls[0]
    assert args["corpus"] == "rdr__arcaneum-2ad2825c"


@pytest.mark.asyncio
async def test_run_caller_scope_fills_unset_corpus_on_query() -> None:
    """``query`` is a retrieval tool too — caller scope fills it in."""
    from nexus.plans.runner import plan_run

    plan = {"steps": [{"tool": "query", "args": {"question": "x"}}]}
    disp = _FakeDispatcher([{"text": "ok"}])
    await plan_run(
        _match(plan),
        {"_nx_scope": "rdr__nexus"},
        dispatcher=disp,
    )
    _tool, args = disp.calls[0]
    assert args["corpus"] == "rdr__nexus"


@pytest.mark.asyncio
async def test_run_caller_scope_does_not_override_plan_pinned_corpus() -> None:
    """When the plan step already pins ``corpus``, caller scope does NOT
    override. Plan authors win."""
    from nexus.plans.runner import plan_run

    plan = {
        "steps": [
            {"tool": "search", "args": {"query": "x", "corpus": "code__delos"}},
        ],
    }
    disp = _FakeDispatcher([{"text": "ok", "ids": []}])
    await plan_run(
        _match(plan),
        {"_nx_scope": "rdr__arcaneum"},
        dispatcher=disp,
    )
    _tool, args = disp.calls[0]
    assert args["corpus"] == "code__delos"


@pytest.mark.asyncio
async def test_run_caller_scope_does_not_override_plan_collection() -> None:
    """When the plan step pins a specific ``collection``, caller scope does
    NOT inject a corpus."""
    from nexus.plans.runner import plan_run

    plan = {
        "steps": [
            {
                "tool": "search",
                "args": {"query": "x", "collection": "rdr__delos"},
            },
        ],
    }
    disp = _FakeDispatcher([{"text": "ok", "ids": []}])
    await plan_run(
        _match(plan),
        {"_nx_scope": "rdr__arcaneum"},
        dispatcher=disp,
    )
    _tool, args = disp.calls[0]
    assert "corpus" not in args
    assert args["collection"] == "rdr__delos"


@pytest.mark.asyncio
async def test_run_caller_scope_does_not_override_plan_taxonomy_domain() -> None:
    """When the plan step declares ``scope.taxonomy_domain``, that populates
    corpus first; caller scope does not clobber it."""
    from nexus.plans.runner import plan_run

    plan = {
        "steps": [
            {
                "tool": "search",
                "args": {"query": "x"},
                "scope": {"taxonomy_domain": "code"},
            },
        ],
    }
    disp = _FakeDispatcher([{"text": "ok", "ids": []}])
    await plan_run(
        _match(plan),
        {"_nx_scope": "rdr__arcaneum"},
        dispatcher=disp,
    )
    _tool, args = disp.calls[0]
    # taxonomy_domain=code → "code" corpus prefix (from _DOMAIN_TO_CORPUS)
    assert args["corpus"] == "code"


@pytest.mark.asyncio
async def test_run_caller_scope_skips_non_retrieval_tools() -> None:
    """Non-retrieval tools (summarize, extract, rank, compare, generate,
    traverse) do not get ``corpus`` injected from caller scope."""
    from nexus.plans.runner import plan_run

    plan = {
        "steps": [
            {"tool": "summarize", "args": {"text": "hello"}},
            {"tool": "extract", "args": {"text": "hello", "schema": {}}},
            {"tool": "traverse", "args": {"seeds": ["1.1"]}},
        ],
    }
    disp = _FakeDispatcher([
        {"text": "s"}, {"text": "e"}, {"tumblers": []},
    ])
    await plan_run(
        _match(plan),
        {"_nx_scope": "rdr__arcaneum"},
        dispatcher=disp,
    )
    for _tool, args in disp.calls:
        assert "corpus" not in args


@pytest.mark.asyncio
async def test_run_no_scope_binding_unchanged() -> None:
    """When caller omits ``_nx_scope``, behavior is unchanged — the step
    runs without any corpus injection."""
    from nexus.plans.runner import plan_run

    plan = {"steps": [{"tool": "search", "args": {"query": "x"}}]}
    disp = _FakeDispatcher([{"text": "ok", "ids": []}])
    await plan_run(_match(plan), {}, dispatcher=disp)
    _tool, args = disp.calls[0]
    assert "corpus" not in args


@pytest.mark.asyncio
async def test_run_caller_scope_binding_not_forwarded_to_tool() -> None:
    """``_nx_scope`` is an internal binding; it must not leak into the
    dispatched tool args."""
    from nexus.plans.runner import plan_run

    plan = {"steps": [{"tool": "search", "args": {"query": "x"}}]}
    disp = _FakeDispatcher([{"text": "ok", "ids": []}])
    await plan_run(
        _match(plan),
        {"_nx_scope": "rdr__arcaneum"},
        dispatcher=disp,
    )
    _tool, args = disp.calls[0]
    assert "_nx_scope" not in args


# ── Result + step trace ────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_run_returns_result_with_step_outputs() -> None:
    from nexus.plans.runner import plan_run

    plan = {
        "steps": [
            {"tool": "search", "args": {"query": "x"}},
            {"tool": "summarize", "args": {"corpus": "$step1.text"}},
        ],
    }
    disp = _FakeDispatcher([
        {"text": "search-output", "ids": ["a"]},
        {"text": "summary-output"},
    ])
    result = await plan_run(_match(plan), {}, dispatcher=disp)
    assert result.steps[0]["text"] == "search-output"
    assert result.steps[1]["text"] == "summary-output"
    assert result.final == result.steps[1]


@pytest.mark.asyncio
async def test_run_with_empty_steps_returns_empty_result() -> None:
    from nexus.plans.runner import plan_run

    result = await plan_run(
        _match({"steps": []}), {}, dispatcher=_FakeDispatcher(),
    )
    assert result.steps == []
    assert result.final is None


@pytest.mark.asyncio
async def test_run_passes_through_static_args_unchanged() -> None:
    """Args without any ``$`` substitution pass through untouched."""
    from nexus.plans.runner import plan_run

    plan = {"steps": [{"tool": "x", "args": {"limit": 10, "flag": True}}]}
    disp = _FakeDispatcher()
    await plan_run(_match(plan), {}, dispatcher=disp)
    assert disp.calls[0][1] == {"limit": 10, "flag": True}


# ── operator_filter DAG-registry integration (RDR-088 nexus-ac40.2) ──────────


@pytest.mark.asyncio
async def test_run_filter_step_after_search_narrows_results() -> None:
    """Search then filter: the filter step receives the search output via
    a step reference and dispatches with the resolved items payload. This
    pins the bead's 'plan with filter step after search narrows results'
    acceptance contract — the runner wires filter as an operator, and
    downstream can read the rationale back off the step output."""
    from nexus.plans.runner import plan_run

    plan = {
        "steps": [
            {"tool": "search", "args": {"query": "concept", "limit": 10}},
            {
                "tool": "filter",
                "args": {
                    "items": "$step1.ids",
                    "criterion": "published-after-2023",
                },
            },
        ],
    }
    disp = _FakeDispatcher([
        {"ids": ["a", "b", "c"], "tumblers": ["1.1", "1.2", "1.3"]},
        {
            "items": [{"id": "a"}, {"id": "c"}],
            "rationale": [
                {"id": "a", "reason": "published 2024"},
                {"id": "b", "reason": "rejected: published 2022"},
                {"id": "c", "reason": "published 2025"},
            ],
        },
    ])
    result = await plan_run(_match(plan), {}, dispatcher=disp)

    # Custom dispatchers receive the bare tool name (verb). Operator name
    # translation to ``operator_filter`` is the default-dispatcher's job;
    # we verify resolution via _OPERATOR_TOOL_MAP separately in
    # TestHydrateInputsTranslation.
    tool, args = disp.calls[1]
    assert tool == "filter"
    assert args["criterion"] == "published-after-2023"
    # $step1.ids resolved to the search output's id list.
    assert args["items"] == ["a", "b", "c"]

    filter_output = result.steps[1]
    # The core narrowing contract: output length <= input length (subset).
    # A valid all-pass filter returns every input; the assertion must not
    # over-reach the bead's stated contract.
    assert len(filter_output["items"]) <= len(result.steps[0]["ids"])
    # This specific fake-dispatcher scripts 2 of 3 kept — pin that so the
    # test is sensitive to regressions in the narrowing pipe.
    assert len(filter_output["items"]) == 2
    assert len(filter_output["rationale"]) == 3
    output_ids = {it["id"] for it in filter_output["items"]}
    rationale_ids = {r["id"] for r in filter_output["rationale"]}
    assert output_ids.issubset(rationale_ids), (
        "kept items must appear in rationale"
    )


@pytest.mark.asyncio
async def test_run_filter_rationale_accessible_via_step_ref() -> None:
    """Downstream plan steps must be able to read filter's rationale via
    ``$stepN.rationale`` — the per-item reasons are first-class output for
    plans that need to surface filter decisions (audits, UI)."""
    from nexus.plans.runner import plan_run

    plan = {
        "steps": [
            {"tool": "search", "args": {"query": "x"}},
            {
                "tool": "filter",
                "args": {"items": "$step1.ids", "criterion": "fresh"},
            },
            {
                "tool": "summarize",
                "args": {"content": "$step2.rationale"},
            },
        ],
    }
    disp = _FakeDispatcher([
        {"ids": ["a", "b"], "tumblers": []},
        {
            "items": [{"id": "a"}],
            "rationale": [
                {"id": "a", "reason": "kept-sentinel"},
                {"id": "b", "reason": "rejected-sentinel"},
            ],
        },
        {"summary": "done"},
    ])
    await plan_run(_match(plan), {}, dispatcher=disp)

    summarize_tool, summarize_args = disp.calls[2]
    # Custom dispatcher sees the bare verb; translation is default-dispatcher-only.
    assert summarize_tool == "summarize"
    passed = summarize_args.get("content")
    assert passed is not None, "summarize must receive rationale content"
    flattened = json.dumps(passed) if not isinstance(passed, str) else passed
    assert "kept-sentinel" in flattened
    assert "rejected-sentinel" in flattened


# ── MCP prefix stripping + legacy key aliases ────────────────────────────────


class TestMCPPrefixStripping:
    """Plans generated by the planner worker use fully-qualified MCP tool
    names (mcp__plugin_nx_nexus__search). The runner must strip the prefix
    before resolution."""

    @pytest.mark.asyncio
    async def test_mcp_prefix_stripped(self):
        from nexus.plans.runner import plan_run

        plan = {"steps": [
            {"tool": "mcp__plugin_nx_nexus__search", "args": {"query": "test"}},
        ]}
        disp = _FakeDispatcher()
        await plan_run(_match(plan), {}, dispatcher=disp)
        # Dispatcher should receive "search", not the full prefix.
        assert disp.calls[0][0] == "search"

    @pytest.mark.asyncio
    async def test_bare_tool_name_unchanged(self):
        from nexus.plans.runner import plan_run

        plan = {"steps": [{"tool": "search", "args": {"query": "test"}}]}
        disp = _FakeDispatcher()
        await plan_run(_match(plan), {}, dispatcher=disp)
        assert disp.calls[0][0] == "search"

    @pytest.mark.asyncio
    async def test_non_nexus_mcp_prefix_stripped(self):
        """Planner may use tools from other MCP servers (serena, context7).
        The prefix should be stripped to the bare tool name."""
        from nexus.plans.runner import plan_run

        plan = {"steps": [
            {"tool": "mcp__plugin_sn_serena__jet_brains_find_symbol", "args": {"query": "test"}},
        ]}
        disp = _FakeDispatcher()
        await plan_run(_match(plan), {}, dispatcher=disp)
        assert disp.calls[0][0] == "jet_brains_find_symbol"


class TestLegacyToolKeyAliases:
    """Old plans use 'op' or 'operation' instead of 'tool'. The runner
    should accept all three."""

    @pytest.mark.asyncio
    async def test_op_key_alias(self):
        from nexus.plans.runner import plan_run

        plan = {"steps": [{"op": "search", "args": {"query": "test"}}]}
        disp = _FakeDispatcher()
        await plan_run(_match(plan), {}, dispatcher=disp)
        assert disp.calls[0][0] == "search"

    @pytest.mark.asyncio
    async def test_operation_key_alias(self):
        from nexus.plans.runner import plan_run

        plan = {"steps": [{"operation": "query", "args": {"question": "test"}}]}
        disp = _FakeDispatcher()
        await plan_run(_match(plan), {}, dispatcher=disp)
        assert disp.calls[0][0] == "query"


# ── _hydrate_operator_args: inputs-arg translation (nexus-yis0) ───────────────


class TestHydrateInputsTranslation:
    """nexus-yis0: when a prior explicit ``store_get_many`` step feeds an
    operator via ``inputs: $stepN.contents`` (no ``ids`` key on the
    operator step), ``_hydrate_operator_args`` must still rename
    ``inputs`` to the operator's expected positional arg. Otherwise
    the unknown-kwarg drop in ``_default_dispatcher`` strips the arg
    and the operator fires with no positional, raising TypeError.
    """

    def test_summarize_renames_inputs_to_content(self):
        from nexus.plans.runner import _hydrate_operator_args

        tool, args = _hydrate_operator_args(
            "summarize", {"inputs": "hydrated text"},
        )
        assert tool == "operator_summarize"
        assert args == {"content": "hydrated text"}

    def test_summarize_inputs_list_joined_to_content_string(self):
        from nexus.plans.runner import _hydrate_operator_args

        tool, args = _hydrate_operator_args(
            "summarize", {"inputs": ["a", "b", "c"]},
        )
        assert tool == "operator_summarize"
        assert args == {"content": "a\n\nb\n\nc"}

    def test_generate_renames_inputs_to_context(self):
        from nexus.plans.runner import _hydrate_operator_args

        tool, args = _hydrate_operator_args(
            "generate", {"template": "report", "inputs": ["x", "y"]},
        )
        assert tool == "operator_generate"
        assert args == {"template": "report", "context": "x\n\ny"}

    def test_rank_renames_inputs_to_items_json_encoded(self):
        from nexus.plans.runner import _hydrate_operator_args

        tool, args = _hydrate_operator_args(
            "rank", {"inputs": ["first", "second"], "criterion": "relevance"},
        )
        assert tool == "operator_rank"
        assert args == {
            "items": json.dumps(["first", "second"]),
            "criterion": "relevance",
        }

    def test_compare_renames_inputs_to_items(self):
        from nexus.plans.runner import _hydrate_operator_args

        tool, args = _hydrate_operator_args(
            "compare", {"inputs": ["a", "b"], "focus": "diffs"},
        )
        assert tool == "operator_compare"
        assert args == {
            "items": json.dumps(["a", "b"]),
            "focus": "diffs",
        }

    def test_rename_skipped_when_target_already_set(self):
        """If the plan author correctly passes ``content`` already,
        ``inputs`` is left untouched (no silent overwrite).
        """
        from nexus.plans.runner import _hydrate_operator_args

        tool, args = _hydrate_operator_args(
            "summarize", {"content": "keep me", "inputs": "ignored"},
        )
        assert tool == "operator_summarize"
        assert args["content"] == "keep me"
        assert args["inputs"] == "ignored"

    def test_extract_keeps_inputs_unchanged(self):
        """``operator_extract`` natively takes ``inputs`` so no rename
        should happen.
        """
        from nexus.plans.runner import _hydrate_operator_args

        tool, args = _hydrate_operator_args(
            "extract", {"inputs": "item list", "fields": "a,b"},
        )
        assert tool == "operator_extract"
        assert args == {"inputs": "item list", "fields": "a,b"}

    def test_filter_bare_name_resolves_to_operator_filter(self):
        """RDR-088 nexus-ac40.1: plan YAML using ``tool: filter`` must
        resolve through ``_OPERATOR_TOOL_MAP`` to ``operator_filter``."""
        from nexus.plans.runner import _hydrate_operator_args

        tool, args = _hydrate_operator_args(
            "filter", {"items": '[]', "criterion": "relevance"},
        )
        assert tool == "operator_filter"
        assert args == {"items": '[]', "criterion": "relevance"}

    def test_filter_renames_inputs_to_items(self):
        """RDR-088 nexus-ac40.1 audit carry-over: pre-hydrated step passing
        ``$stepN.contents`` via ``inputs:`` must be renamed to
        ``items`` so the filter operator's positional arg is populated.
        Without this translation, a plan step hits the nexus-yis0 TypeError
        class."""
        from nexus.plans.runner import _hydrate_operator_args

        tool, args = _hydrate_operator_args(
            "filter", {"inputs": ["a", "b"], "criterion": "keep"},
        )
        assert tool == "operator_filter"
        assert args == {
            "items": json.dumps(["a", "b"]),
            "criterion": "keep",
        }

    def test_filter_coerces_list_items_to_json(self):
        """List-valued ``items`` must be json-encoded so the operator
        prompt sees clean JSON rather than Python repr. Mirrors the
        existing rank/compare coercion at runner.py:666."""
        from nexus.plans.runner import _hydrate_operator_args

        tool, args = _hydrate_operator_args(
            "filter",
            {"items": [{"id": "a"}, {"id": "b"}], "criterion": "x"},
        )
        assert tool == "operator_filter"
        assert args["items"] == json.dumps([{"id": "a"}, {"id": "b"}])

    def test_filter_preserves_string_items(self):
        """Already-stringified ``items`` must pass through untouched
        to avoid double-JSON-encoding."""
        from nexus.plans.runner import _hydrate_operator_args

        tool, args = _hydrate_operator_args(
            "filter",
            {"items": '["already", "json"]', "criterion": "x"},
        )
        assert tool == "operator_filter"
        assert args["items"] == '["already", "json"]'

    def test_filter_ids_hydrates_to_items(self):
        """When a filter step declares ``ids:`` and the auto-hydration
        path runs ``store_get_many``, the fetched content list must
        land on the ``items`` arg (not ``inputs``), so the filter's
        positional arg is populated."""
        from unittest.mock import patch

        from nexus.plans.runner import _hydrate_operator_args

        fake_contents = {"contents": ["doc-a body", "doc-b body"]}
        with patch(
            "nexus.mcp.core.store_get_many", return_value=fake_contents,
        ):
            tool, args = _hydrate_operator_args(
                "filter",
                {"ids": ["doc-a", "doc-b"], "criterion": "on-topic"},
            )
        assert tool == "operator_filter"
        assert "ids" not in args and "collections" not in args
        assert args["items"] == json.dumps(["doc-a body", "doc-b body"])
        assert args["criterion"] == "on-topic"

    # ── operator_check hydration (RDR-088 nexus-ac40.4) ─────────────────

    def test_check_bare_name_resolves_to_operator_check(self):
        from nexus.plans.runner import _hydrate_operator_args

        tool, args = _hydrate_operator_args(
            "check",
            {"items": '[]', "check_instruction": "consistent"},
        )
        assert tool == "operator_check"
        assert args == {"items": '[]', "check_instruction": "consistent"}

    def test_check_renames_inputs_to_items(self):
        """Pre-hydrated step passing ``$stepN.contents`` via ``inputs:`` must
        be renamed to ``items`` so the check operator's positional arg is
        populated. Same class as the nexus-yis0 TypeError for rank/compare."""
        from nexus.plans.runner import _hydrate_operator_args

        tool, args = _hydrate_operator_args(
            "check",
            {"inputs": ["doc-a", "doc-b"], "check_instruction": "agree"},
        )
        assert tool == "operator_check"
        assert args == {
            "items": json.dumps(["doc-a", "doc-b"]),
            "check_instruction": "agree",
        }

    def test_check_coerces_list_items_to_json(self):
        from nexus.plans.runner import _hydrate_operator_args

        tool, args = _hydrate_operator_args(
            "check",
            {"items": [{"id": "p1"}, {"id": "p2"}],
             "check_instruction": "consistent"},
        )
        assert tool == "operator_check"
        assert args["items"] == json.dumps([{"id": "p1"}, {"id": "p2"}])

    def test_check_ids_hydrates_to_items(self):
        """check + ids: auto-hydration pulls document bodies and lands
        them on ``items`` so the operator prompt sees concrete content."""
        from unittest.mock import patch

        from nexus.plans.runner import _hydrate_operator_args

        fake_contents = {"contents": ["body-a", "body-b"]}
        with patch(
            "nexus.mcp.core.store_get_many", return_value=fake_contents,
        ):
            tool, args = _hydrate_operator_args(
                "check",
                {"ids": ["doc-a", "doc-b"],
                 "check_instruction": "claim holds"},
            )
        assert tool == "operator_check"
        assert args["items"] == json.dumps(["body-a", "body-b"])
        assert args["check_instruction"] == "claim holds"
        assert "ids" not in args and "collections" not in args

    # ── operator_verify hydration (RDR-088 nexus-ac40.4) ────────────────

    def test_verify_bare_name_resolves_to_operator_verify(self):
        from nexus.plans.runner import _hydrate_operator_args

        tool, args = _hydrate_operator_args(
            "verify",
            {"claim": "X is true", "evidence": "see §2"},
        )
        assert tool == "operator_verify"
        assert args == {"claim": "X is true", "evidence": "see §2"}

    def test_verify_passes_scalar_args_untouched(self):
        """operator_verify takes two scalars (claim + evidence); there is
        no list-to-scalar translation to perform. The audit carry-over is
        explicit: skip _INPUTS_TARGET for verify."""
        from nexus.plans.runner import _hydrate_operator_args

        tool, args = _hydrate_operator_args(
            "verify",
            {"claim": "nuclear reactor runs on fusion",
             "evidence": "raw-evidence-text"},
        )
        assert tool == "operator_verify"
        assert args["claim"] == "nuclear reactor runs on fusion"
        assert args["evidence"] == "raw-evidence-text"

    def test_verify_inputs_arg_is_not_translated(self):
        """A stray ``inputs`` arg on a verify step must NOT be silently
        renamed — verify's contract is two scalar args. Translating would
        mask an authoring bug. The step should either raise or drop the
        unknown arg downstream; _hydrate_operator_args must leave
        ``inputs`` in place so the downstream TypeError is attributable."""
        from nexus.plans.runner import _hydrate_operator_args

        tool, args = _hydrate_operator_args(
            "verify",
            {"inputs": "stray", "claim": "c", "evidence": "e"},
        )
        assert tool == "operator_verify"
        assert args.get("inputs") == "stray"
        assert args["claim"] == "c"
        assert args["evidence"] == "e"
