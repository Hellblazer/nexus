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
