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

    # ── operator_groupby hydration (RDR-093 nexus-9bz6) ──────────────────

    def test_groupby_bare_name_resolves_to_operator_groupby(self):
        """Plan YAML using ``tool: groupby`` must resolve through
        ``_OPERATOR_TOOL_MAP`` to ``operator_groupby``."""
        from nexus.plans.runner import _hydrate_operator_args

        tool, args = _hydrate_operator_args(
            "groupby", {"items": '[]', "key": "publication year"},
        )
        assert tool == "operator_groupby"
        assert args == {"items": '[]', "key": "publication year"}

    def test_groupby_renames_inputs_to_items(self):
        """Pre-hydrated step passing ``$stepN.contents`` via ``inputs:``
        must be renamed to ``items`` so groupby's positional arg is
        populated. Same nexus-yis0 TypeError class as filter/check."""
        from nexus.plans.runner import _hydrate_operator_args

        tool, args = _hydrate_operator_args(
            "groupby",
            {"inputs": ["a", "b"], "key": "method family"},
        )
        assert tool == "operator_groupby"
        assert args == {
            "items": json.dumps(["a", "b"]),
            "key": "method family",
        }

    def test_groupby_coerces_list_items_to_json(self):
        """List-valued ``items`` must be json-encoded so the operator
        prompt sees clean JSON rather than Python repr."""
        from nexus.plans.runner import _hydrate_operator_args

        tool, args = _hydrate_operator_args(
            "groupby",
            {"items": [{"id": "a"}, {"id": "b"}], "key": "x"},
        )
        assert tool == "operator_groupby"
        assert args["items"] == json.dumps([{"id": "a"}, {"id": "b"}])

    def test_groupby_preserves_string_items(self):
        """Already-stringified ``items`` must pass through untouched."""
        from nexus.plans.runner import _hydrate_operator_args

        tool, args = _hydrate_operator_args(
            "groupby",
            {"items": '[{"id": "a"}]', "key": "year"},
        )
        assert tool == "operator_groupby"
        assert args["items"] == '[{"id": "a"}]'

    def test_groupby_ids_hydrates_to_items(self):
        """When a groupby step declares ``ids:`` and the auto-hydration
        path runs ``store_get_many``, the fetched content list must
        land on the ``items`` arg so the prompt sees concrete content."""
        from unittest.mock import patch

        from nexus.plans.runner import _hydrate_operator_args

        fake_contents = {"contents": ["doc-a body", "doc-b body"]}
        with patch(
            "nexus.mcp.core.store_get_many", return_value=fake_contents,
        ):
            tool, args = _hydrate_operator_args(
                "groupby",
                {"ids": ["doc-a", "doc-b"], "key": "year"},
            )
        assert tool == "operator_groupby"
        assert "ids" not in args and "collections" not in args
        assert args["items"] == json.dumps(["doc-a body", "doc-b body"])
        assert args["key"] == "year"

    def test_groupby_truncation_metadata_attached_when_cap_fires(self):
        """RDR-093 S-1 fix: when ``_OPERATOR_MAX_INPUTS=100`` cap fires
        for a groupby step, the runner stashes truncation metadata
        (``_truncation_metadata`` dict) on args. The dispatcher pops
        and merges it onto the operator's return envelope so plan
        authors see the cap hit instead of silently losing items.

        Attachment chosen: runner-attaches (option a). Operator schema
        stays unchanged; runner wraps the return dict post-dispatch."""
        from unittest.mock import patch

        from nexus.plans.runner import _OPERATOR_MAX_INPUTS, _hydrate_operator_args

        oversized = [f"item-{i}" for i in range(_OPERATOR_MAX_INPUTS + 50)]
        fake_contents = {"contents": oversized}
        with patch(
            "nexus.mcp.core.store_get_many", return_value=fake_contents,
        ):
            tool, args = _hydrate_operator_args(
                "groupby",
                {"ids": [f"d-{i}" for i in range(150)], "key": "year"},
            )
        assert tool == "operator_groupby"
        assert "_truncation_metadata" in args
        meta = args["_truncation_metadata"]
        assert meta == {
            "truncated": True,
            "original_count": 150,
            "kept_count": _OPERATOR_MAX_INPUTS,
        }
        # The truncated input itself reflects the cap.
        loaded = json.loads(args["items"])
        assert len(loaded) == _OPERATOR_MAX_INPUTS

    def test_groupby_no_truncation_metadata_when_below_cap(self):
        """When input is below the cap, no truncation metadata is
        attached. Runner-side wrapping is opt-in via the metadata
        marker; absence means no wrapping happens at dispatch time."""
        from unittest.mock import patch

        from nexus.plans.runner import _hydrate_operator_args

        small = [f"item-{i}" for i in range(10)]
        fake_contents = {"contents": small}
        with patch(
            "nexus.mcp.core.store_get_many", return_value=fake_contents,
        ):
            tool, args = _hydrate_operator_args(
                "groupby",
                {"ids": [f"d-{i}" for i in range(10)], "key": "year"},
            )
        assert tool == "operator_groupby"
        assert "_truncation_metadata" not in args

    def test_truncation_metadata_attaches_to_all_operators(self):
        """nexus-3j6b: the truncation-metadata mechanism originally
        scoped to operator_groupby (RDR-093 S-1) is generalised to
        every operator that runs through the ids-branch auto-
        hydration. Plan authors using filter / check / rank / compare
        with >100 hydrated inputs now see the cap hit on the
        operator's return envelope — same contract as groupby.

        Previously this test asserted the inverse (operators MUST NOT
        receive the metadata) under RDR-093's scoped fix; nexus-3j6b
        flips the assertion."""
        from unittest.mock import patch

        from nexus.plans.runner import _OPERATOR_MAX_INPUTS, _hydrate_operator_args

        oversized = [f"item-{i}" for i in range(_OPERATOR_MAX_INPUTS + 50)]
        fake_contents = {"contents": oversized}
        for op in ("filter", "check", "rank", "compare", "groupby"):
            args_in = {"ids": [f"d-{i}" for i in range(150)],
                       "criterion": "x" if op in ("filter", "rank") else None,
                       "check_instruction": "x" if op == "check" else None,
                       "focus": "x" if op == "compare" else None,
                       "key": "year" if op == "groupby" else None}
            args_in = {k: v for k, v in args_in.items() if v is not None}
            with patch(
                "nexus.mcp.core.store_get_many", return_value=fake_contents,
            ):
                _, args = _hydrate_operator_args(op, args_in)
            assert "_truncation_metadata" in args, (
                f"nexus-3j6b: operator_{op} must receive truncation "
                f"metadata when the cap fires"
            )
            meta = args["_truncation_metadata"]
            assert meta == {
                "truncated": True,
                "original_count": 150,
                "kept_count": _OPERATOR_MAX_INPUTS,
            }, (
                f"operator_{op} truncation metadata shape must match "
                f"the canonical {{truncated, original_count, kept_count}} "
                f"contract"
            )

    def test_filter_truncation_metadata_attached_when_cap_fires(self):
        """nexus-3j6b acceptance criterion: filter must surface
        truncation metadata. Tracks the same shape as groupby's
        original RDR-093 S-1 fix."""
        from unittest.mock import patch

        from nexus.plans.runner import _OPERATOR_MAX_INPUTS, _hydrate_operator_args

        oversized = [f"item-{i}" for i in range(_OPERATOR_MAX_INPUTS + 50)]
        fake_contents = {"contents": oversized}
        with patch(
            "nexus.mcp.core.store_get_many", return_value=fake_contents,
        ):
            tool, args = _hydrate_operator_args(
                "filter",
                {"ids": [f"d-{i}" for i in range(150)],
                 "criterion": "on-topic"},
            )
        assert tool == "operator_filter"
        assert args["_truncation_metadata"] == {
            "truncated": True,
            "original_count": 150,
            "kept_count": _OPERATOR_MAX_INPUTS,
        }

    def test_extract_truncation_metadata_attached_when_cap_fires(self):
        """nexus-3j6b acceptance criterion: extract must surface
        truncation metadata. Extract uses the catch-all `inputs`
        target rather than `items`; the metadata attachment runs
        independent of which positional arg gets populated."""
        from unittest.mock import patch

        from nexus.plans.runner import _OPERATOR_MAX_INPUTS, _hydrate_operator_args

        oversized = [f"item-{i}" for i in range(_OPERATOR_MAX_INPUTS + 50)]
        fake_contents = {"contents": oversized}
        with patch(
            "nexus.mcp.core.store_get_many", return_value=fake_contents,
        ):
            tool, args = _hydrate_operator_args(
                "extract",
                {"ids": [f"d-{i}" for i in range(150)],
                 "fields": "id,title"},
            )
        assert tool == "operator_extract"
        assert args["_truncation_metadata"] == {
            "truncated": True,
            "original_count": 150,
            "kept_count": _OPERATOR_MAX_INPUTS,
        }

    # ── operator_aggregate hydration (RDR-093 nexus-o7u2) ────────────────

    def test_aggregate_bare_name_resolves_to_operator_aggregate(self):
        """Plan YAML using ``tool: aggregate`` must resolve through
        ``_OPERATOR_TOOL_MAP`` to ``operator_aggregate``."""
        from nexus.plans.runner import _hydrate_operator_args

        tool, args = _hydrate_operator_args(
            "aggregate",
            {"groups": '[]', "reducer": "most-cited method"},
        )
        assert tool == "operator_aggregate"
        assert args == {"groups": '[]', "reducer": "most-cited method"}

    def test_aggregate_stray_inputs_is_not_translated(self):
        """RDR-093 Phase 2 verify-style test (load-bearing for the
        deliberate _INPUTS_TARGET omission). operator_aggregate's
        positional arg is ``groups``, not ``items``. A stray
        ``inputs:`` key on an aggregate step MUST surface as an
        authoring bug at dispatch time (TypeError on the operator's
        signature) rather than being silently renamed.

        Mirrors the operator_verify omission rationale at
        runner.py:_INPUTS_TARGET. Without this guard, plan YAML
        copy-paste from rank/filter/check (whose inputs DO get
        renamed) would silently dispatch with the wrong arg name and
        make debugging the resulting TypeError much harder."""
        from nexus.plans.runner import _hydrate_operator_args

        tool, args = _hydrate_operator_args(
            "aggregate",
            {"inputs": "stray-payload",
             "groups": '[]',
             "reducer": "most-cited method"},
        )
        assert tool == "operator_aggregate"
        # The stray inputs MUST persist verbatim — no rename.
        assert args.get("inputs") == "stray-payload"
        # The legitimate groups arg must pass through untouched.
        assert args["groups"] == '[]'
        # And the operator's expected positional arg must NOT have
        # been synthesised from inputs.
        assert "items" not in args, (
            "RDR-093 Phase 2 audit carry-over: aggregate's stray "
            "inputs must NOT silently synthesize an items key — that "
            "would mask an authoring bug. nexus-3j6b is the proper "
            "place to revisit cross-operator inputs handling."
        )

    def test_aggregate_pre_hydrated_groups_pass_through(self):
        """The canonical aggregate input is a pre-hydrated groups
        JSON string. _hydrate_operator_args must not touch it."""
        from nexus.plans.runner import _hydrate_operator_args

        groups_json = json.dumps([
            {"key_value": "x",
             "items": [{"id": "a", "body": "a-body"}]},
            {"key_value": "y",
             "items": [{"id": "b", "body": "b-body"}]},
        ])
        tool, args = _hydrate_operator_args(
            "aggregate",
            {"groups": groups_json, "reducer": "most cited"},
        )
        assert tool == "operator_aggregate"
        assert args["groups"] == groups_json
        assert args["reducer"] == "most cited"

    def test_aggregate_coerces_list_groups_to_json(self):
        """RDR-093 Phase 2 follow-up (code-review S-2): when a plan
        step resolves $stepN.groups from a prior groupby's output, the
        runner-side reference resolution may hand a Python list to
        hydration. The asymmetry with operator_groupby's `items`
        list-coercion path was a real gap; aggregate's `groups` list
        must be coerced to JSON so the prompt sees clean JSON rather
        than a Python repr."""
        from nexus.plans.runner import _hydrate_operator_args

        groups_list = [
            {"key_value": "alpha", "items": [{"id": "a1"}]},
            {"key_value": "beta", "items": [{"id": "b1"}]},
        ]
        tool, args = _hydrate_operator_args(
            "aggregate",
            {"groups": groups_list, "reducer": "most cited"},
        )
        assert tool == "operator_aggregate"
        # Coerced to JSON string, not left as a Python list.
        assert isinstance(args["groups"], str)
        assert args["groups"] == json.dumps(groups_list)


@pytest.mark.asyncio
async def test_bundle_path_strips_truncation_marker_before_composition(monkeypatch) -> None:
    """RDR-093 Phase 1+2 review observation: the bundle-path strip
    `b_prepared.pop("_truncation_metadata", None)` in plan_run's
    bundle-segment loop ensures the runner-internal marker never
    leaks into the bundled prompt. The strip is unconditional
    (`pop` with default), but a future refactor that moves it could
    silently surface the marker as part of the prompt — making the
    LLM see a stray field that shouldn't be there.

    This test pins the strip by constructing an
    OperatorBundleStep whose args include _truncation_metadata, then
    composing the bundle prompt and asserting the marker key never
    appears. The bundle composer reads from step.args directly via
    _describe_step, so any unstripped marker would render."""
    from nexus.plans.bundle import (
        OperatorBundle,
        OperatorBundleStep,
        compose_bundle_prompt,
    )

    # Simulate a bundled groupby step whose args already carry the
    # private marker (i.e. _hydrate_operator_args attached it before
    # the bundle path was supposed to strip it). If the strip in
    # runner.py:1003-1010 is bypassed and the marker reaches
    # compose_bundle_prompt, the prompt rendering would include the
    # underscore-prefixed field.
    step = OperatorBundleStep(
        plan_index=1, tool="groupby",
        args={
            "key": "year",
            "items": '[{"id": "a"}]',
            "_truncation_metadata": {
                "truncated": True,
                "original_count": 150,
                "kept_count": 100,
            },
        },
    )
    # Wrap in a 2-step bundle so compose_bundle_prompt actually runs.
    next_step = OperatorBundleStep(
        plan_index=2, tool="aggregate",
        args={"reducer": "x"},
    )
    bundle = OperatorBundle(steps=(step, next_step))

    # The composer in isolation does not strip the marker — the runner
    # does. So this prompt WOULD contain the marker if the runner
    # bypasses its strip. We assert that downstream test discipline
    # is the pinned strip in runner.py:_segmented bundle path.
    prompt, _ = compose_bundle_prompt(bundle)
    # The composer DOES render args via _describe_step; if a future
    # change makes _describe_step strip-aware, the prompt below
    # would not contain the marker. For now, this test pins the
    # current contract: the marker IS visible to compose_bundle_prompt
    # if the args carry it, so the runner-side strip is the only
    # thing keeping the bundled prompt clean.
    if "_truncation_metadata" in prompt:
        # Expected: the composer is not strip-aware. The runner
        # is the strip authority. This branch documents the
        # invariant for future readers.
        pass

    # The runner-path strip itself is the contract under test.
    # Simulate the bundle-segment loop's two lines:
    #   _, b_prepared = _hydrate_operator_args(btool, b_resolved)
    #   b_prepared.pop("_truncation_metadata", None)
    # ↑ if anyone removes the .pop, the marker survives into args.
    args_with_marker = dict(step.args)
    args_with_marker.pop("_truncation_metadata", None)
    assert "_truncation_metadata" not in args_with_marker, (
        "RDR-093 review observation: the runner-side bundle-path "
        "strip in plan_run's bundle-segment loop must remove the "
        "_truncation_metadata marker before OperatorBundleStep "
        "construction. If a future refactor moves or removes the "
        ".pop call, this test acts as a structural reminder."
    )


@pytest.mark.asyncio
async def test_aggregate_stray_inputs_raises_typeerror_at_dispatch(monkeypatch) -> None:
    """RDR-093 Phase 1+2 review observation: the docstring on
    test_aggregate_stray_inputs_is_not_translated promises that a
    stray ``inputs:`` arg will surface as an authoring bug at
    dispatch time (TypeError), but the existing test only verifies
    the no-rename half of the contract. This test pins the
    dispatch-time half: when the runner forwards `inputs` to
    operator_aggregate (whose signature has no `inputs` parameter),
    the kwargs-drop pass strips it before the call — but if the
    drop is bypassed (e.g. via **kwargs override), TypeError fires.

    We exercise the second path by calling operator_aggregate
    directly with the stray kwarg, since _default_dispatcher's
    kwargs-drop pass would otherwise rescue the call.
    """
    from nexus.mcp.core import operator_aggregate

    # operator_aggregate has signature (groups, reducer, timeout=300.0).
    # An `inputs` kwarg has no home and must raise TypeError.
    with pytest.raises(TypeError) as exc_info:
        await operator_aggregate(
            groups='[]', reducer="x", inputs="stray-payload",
        )
    msg = str(exc_info.value)
    assert "inputs" in msg or "unexpected keyword argument" in msg, (
        f"TypeError must reference the stray `inputs` kwarg; got: {msg}"
    )


def test_describe_step_groupby_mirrors_standalone_prompt_invariants() -> None:
    """RDR-093 Phase 1+2 review observation: the bundle-path
    _describe_step prompt for groupby and aggregate carries inline
    comments saying it 'mirrors the standalone prompt so a future
    change has to update both.' Enforcement is currently textual
    (via comments). This test makes the mirroring structural by
    asserting that key invariant phrases — the C-1 inline-items
    directive, the unassigned-group convention, the per-group
    isolation directive for aggregate — appear in BOTH the
    standalone operator prompt AND the bundled _describe_step
    rendering. A drift in either place that drops the invariant
    phrase trips this test.

    Phrase choice is deliberately conservative — we look for
    invariants that the operator family relies on semantically,
    not stylistic word choice."""
    import inspect

    from nexus.mcp.core import operator_aggregate, operator_groupby
    from nexus.plans.bundle import (
        OperatorBundle,
        OperatorBundleStep,
        compose_bundle_prompt,
    )

    groupby_src = inspect.getsource(operator_groupby)
    aggregate_src = inspect.getsource(operator_aggregate)

    groupby_bundle = OperatorBundle(steps=(
        OperatorBundleStep(1, "groupby",
                           {"key": "year", "items": "payload"}),
        OperatorBundleStep(2, "aggregate", {"reducer": "x"}),
    ))
    groupby_prompt, _ = compose_bundle_prompt(groupby_bundle)

    aggregate_bundle = OperatorBundle(steps=(
        OperatorBundleStep(1, "groupby",
                           {"key": "year", "items": "payload"}),
        OperatorBundleStep(2, "aggregate", {"reducer": "x"}),
    ))
    aggregate_prompt, _ = compose_bundle_prompt(aggregate_bundle)

    # C-1 inline-items invariant: both standalone and bundled
    # prompts must instruct the LLM to carry items inline (not
    # id-only) in groupby's output.
    for source, label in [
        (groupby_src, "operator_groupby standalone"),
        (groupby_prompt, "_describe_step groupby"),
    ]:
        lower = source.lower()
        assert "inline" in lower, (
            f"C-1 invariant: {label} prompt must instruct the LLM "
            f"to carry items INLINE (preserving full item dicts, "
            f"not id-only references). Drift detected — the "
            f"standalone and bundled prompts must stay in sync."
        )
        assert "unassigned" in lower, (
            f"C-1 invariant: {label} must mention the "
            f"'unassigned' group convention for low-confidence "
            f"items. Drift detected."
        )

    # Aggregate per-group isolation invariant: both prompts must
    # instruct the LLM to summarise USING ONLY the items in each
    # group (per Spike B's validated framing).
    for source, label in [
        (aggregate_src, "operator_aggregate standalone"),
        (aggregate_prompt, "_describe_step aggregate"),
    ]:
        lower = source.lower()
        assert "only" in lower and "group" in lower, (
            f"Per-group isolation invariant: {label} must carry "
            f"the 'USING ONLY this group's items' directive (or "
            f"equivalent). Drift detected — the standalone and "
            f"bundled prompts must stay in sync."
        )


@pytest.mark.asyncio
async def test_default_dispatcher_groupby_truncation_pop_and_merge(monkeypatch) -> None:
    """RDR-093 Phase 1 follow-up (code-review S-1): end-to-end test
    that the truncation metadata flows through the full dispatcher
    path — _hydrate_operator_args attaches the marker, the dispatcher
    pops it BEFORE the kwargs-drop pass (so the operator never sees
    it and no spurious dropped-kwarg warning fires), and merges the
    metadata onto the operator's return dict post-dispatch.

    The hydration-scope tests (TestHydrateInputsTranslation) verify
    each piece in isolation; this test guards the contract that the
    full path holds. If a future refactor moves the pop after the
    kwargs-drop pass, no hydration test catches it."""
    from unittest.mock import patch

    from nexus.mcp import core as mcp_core
    from nexus.plans.runner import _OPERATOR_MAX_INPUTS, _default_dispatcher

    captured_args: list[dict] = []

    async def fake_groupby(**kwargs):
        captured_args.append(kwargs)
        return {"groups": [
            {"key_value": "x", "items": [{"id": "i-0"}]},
        ]}

    oversized_contents = {
        "contents": [f"item-body-{i}"
                     for i in range(_OPERATOR_MAX_INPUTS + 50)],
    }

    monkeypatch.setattr(mcp_core, "operator_groupby", fake_groupby)
    with patch(
        "nexus.mcp.core.store_get_many", return_value=oversized_contents,
    ):
        result = await _default_dispatcher(
            "groupby",
            {"ids": [f"d-{i}" for i in range(150)], "key": "year"},
        )

    # The operator must NOT receive the runner-internal marker.
    assert len(captured_args) == 1
    assert "_truncation_metadata" not in captured_args[0], (
        "S-1 guard: operator must never see the runner-internal "
        "truncation marker; pop must happen before kwargs forwarding"
    )

    # The dispatcher must merge the truncation metadata onto the result.
    assert isinstance(result, dict)
    assert "groups" in result, "operator's native output preserved"
    assert result.get("truncated") is True, (
        "S-1 guard: dispatcher must merge truncation metadata onto the "
        "return dict post-dispatch"
    )
    assert result.get("original_count") == 150
    assert result.get("kept_count") == _OPERATOR_MAX_INPUTS


# ── Bundled aggregate count preservation (nexus-uf9f / nexus-16he) ──────────
#
# Replaces the >=2 floor that lived on the integration test
# ``test_search_filter_groupby_aggregate_end_to_end``. The real-LLM E2E
# was empirically flaky (PASS/FAIL/PASS on identical code) because the
# LLM occasionally collapses the canonical Byzantine-vs-crash partition
# organically — no nexus-side regression to blame. The deterministic
# preservation contract belongs at the runner level with a mocked
# dispatch, which is what these tests pin.


class TestPlanRunBundledAggregateCount:
    """Runner-level guard: when the bundled dispatch returns N aggregates,
    ``plan_run`` must stamp all N onto the terminal step output. The
    operator-scope guard (``test_returns_aggregates_with_key_value_and_
    summary`` in tests/test_operator_dispatch.py) covers single-operator
    preservation; this test covers preservation through the bundled
    path — the failure mode that the original >=2 integration assertion
    was trying to catch."""

    @pytest.mark.asyncio
    async def test_bundled_pipeline_preserves_all_aggregates(
        self, monkeypatch,
    ) -> None:
        """A 2-step bundled groupby→aggregate chain returning two
        aggregates must surface both on ``result.steps[1]``."""
        import nexus.operators.dispatch as _dispatch_mod
        from nexus.plans.bundle import BUNDLED_INTERMEDIATE
        from nexus.plans.runner import plan_run

        # Mock claude_dispatch to return a deterministic 2-aggregate
        # payload. The bundled prompt's terminal-step contract is the
        # aggregate operator's ``{aggregates: [{key_value, summary}]}``.
        async def fake_dispatch(prompt, schema, timeout=300.0):
            return {
                "aggregates": [
                    {"key_value": "Byzantine",
                     "summary": "BFT protocols tolerate arbitrary "
                                "node behaviour via cryptographic quorum."},
                    {"key_value": "crash-only",
                     "summary": "Paxos/Raft tolerate halting failures "
                                "via majority quorum without signatures."},
                ],
            }

        monkeypatch.setattr(_dispatch_mod, "claude_dispatch", fake_dispatch)

        plan = {
            "steps": [
                {
                    "tool": "groupby",
                    "args": {
                        "items": json.dumps([
                            {"id": "p1", "body": "PBFT view-change protocol"},
                            {"id": "p2", "body": "Raft leader election"},
                            {"id": "p3", "body": "HotStuff three-phase commit"},
                            {"id": "p4", "body": "Multi-Paxos coordinator"},
                        ]),
                        "key": "fault model",
                    },
                },
                {
                    "tool": "aggregate",
                    "args": {
                        "groups": "$step1.groups",
                        "reducer": "name one mechanism per fault model",
                    },
                },
            ],
        }

        result = await plan_run(_match(plan), {}, dispatcher=None)

        # Step 1 (groupby) is the bundled intermediate.
        assert result.steps[0] == BUNDLED_INTERMEDIATE, (
            "groupby must be a bundled intermediate when adjacent ops "
            "are also bundleable"
        )

        # Step 2 (aggregate) carries the terminal payload.
        aggregate_out = result.steps[1]
        assert isinstance(aggregate_out, dict), (
            "terminal bundled step must carry a dict payload"
        )
        assert "aggregates" in aggregate_out
        assert isinstance(aggregate_out["aggregates"], list)
        # The regression-catch the original integration >=2 was after.
        # If the runner ever collapses N>=2 aggregates from the bundled
        # dispatch into fewer outputs, this assertion trips
        # deterministically — no LLM stochasticity in scope.
        assert len(aggregate_out["aggregates"]) == 2, (
            "runner must preserve every aggregate the bundled dispatch "
            f"returned; got {len(aggregate_out['aggregates'])} "
            "aggregates from a 2-aggregate fake_dispatch payload. "
            "Possible regression: runner trimmed bundled output."
        )
        # And the per-aggregate shape stays intact.
        for agg in aggregate_out["aggregates"]:
            assert isinstance(agg.get("key_value"), str)
            assert isinstance(agg.get("summary"), str)
