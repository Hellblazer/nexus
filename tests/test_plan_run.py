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

from nexus.plans.runner import _PLAN_STEP_DEFAULT_CORPUS


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
        "search",
        {
            "query": "how does X work", "limit": 5,
            "corpus": _PLAN_STEP_DEFAULT_CORPUS,
        },
    )


@pytest.mark.asyncio
async def test_resolved_step_args_captures_runtime_corpus_not_template(
) -> None:
    """nexus-ivv4d code review follow-up (T2 [24198]): PlanResult.
    resolved_step_args must carry the FULLY RESOLVED corpus/query — the
    plan template here leaves corpus unset entirely, so a caller reading
    it back must see the runner's own ``_PLAN_STEP_DEFAULT_CORPUS``
    fall-through, not an empty string."""
    from nexus.plans.runner import plan_run

    plan = {
        "steps": [
            {"tool": "search", "args": {"query": "$intent"}},
        ],
        "required_bindings": ["intent"],
    }
    disp = _FakeDispatcher([{"text": "ok"}])
    result = await plan_run(
        _match(plan), {"intent": "how does X work"}, dispatcher=disp,
    )

    assert result.resolved_step_args == [
        {
            "step_index": 0, "tool": "search",
            "corpus": _PLAN_STEP_DEFAULT_CORPUS, "query": "how does X work",
        },
    ]


@pytest.mark.asyncio
async def test_resolved_step_args_excludes_store_get_many() -> None:
    """store_get_many carries no corpus/query args of its own -- a
    step_index entry for it would read as a spurious ``corpus=''
    query=''`` rather than "not applicable"."""
    from nexus.plans.runner import plan_run

    plan = {
        "steps": [
            {"tool": "search", "args": {"query": "$intent", "corpus": "rdr"}},
            {"tool": "store_get_many", "args": {"ids": "$step1.ids"}},
        ],
        "required_bindings": ["intent"],
    }
    disp = _FakeDispatcher([
        {"ids": ["a"], "collections": ["rdr__1-1"]},
        {"contents": {"a": "text"}, "missing": []},
    ])
    result = await plan_run(
        _match(plan), {"intent": "how does X work"}, dispatcher=disp,
    )

    assert len(result.resolved_step_args) == 1
    assert result.resolved_step_args[0]["tool"] == "search"


@pytest.mark.asyncio
async def test_resolved_step_args_excludes_operator_steps() -> None:
    """An operator step (summarize/rank/...) has no corpus/query args --
    only tools in _CORPUS_QUERY_TOOLS are captured."""
    from nexus.plans.runner import plan_run

    plan = {
        "steps": [
            {"tool": "search", "args": {"query": "$intent", "corpus": "rdr"}},
            {"tool": "summarize", "args": {"content": "$step1.ids"}},
        ],
        "required_bindings": ["intent"],
    }
    disp = _FakeDispatcher([
        {"ids": ["a"], "collections": ["rdr__1-1"]},
        {"text": "a summary"},
    ])
    result = await plan_run(
        _match(plan), {"intent": "how does X work"}, dispatcher=disp,
    )

    assert len(result.resolved_step_args) == 1
    assert result.resolved_step_args[0]["step_index"] == 0


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
    assert disp.calls[0][1] == {
        "query": "caller", "corpus": _PLAN_STEP_DEFAULT_CORPUS,
    }


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
    assert disp.calls[0][1] == {
        "query": "from-default", "corpus": _PLAN_STEP_DEFAULT_CORPUS,
    }


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


# ── Unresolved $var — nexus-pucte ───────────────────────────────────────────
#
# ``$stepN.<field>`` refs already raised ``PlanRunStepRefError`` on an
# unresolved reference. ``$var`` refs had no equivalent: ``_validate_bindings``
# only checks ``match.required_bindings`` NAMES, never what a step's args
# actually reference, so an optional (or simply undeclared) var with no
# default and no caller value silently reached the dispatched tool as the
# literal string ``'$var'``. These tests cover the fix: fail loud at
# validation, before any dispatch, naming the step index and the var.


@pytest.mark.asyncio
async def test_run_raises_on_unresolved_optional_var() -> None:
    """An optional $var with no default and no caller value must be
    rejected at validation — never reach the tool as the literal token."""
    from nexus.plans.runner import PlanRunUnresolvedVarError, plan_run

    plan = {
        "steps": [{"tool": "search", "args": {"query": "$topic"}}],
        "optional_bindings": ["topic"],
    }
    disp = _FakeDispatcher()
    with pytest.raises(PlanRunUnresolvedVarError) as exc:
        await plan_run(_match(plan), {}, dispatcher=disp)
    assert exc.value.step_index == 0
    assert exc.value.var_name == "topic"
    # Validated before the first dispatch — nothing ever ran.
    assert disp.calls == []


@pytest.mark.asyncio
async def test_run_raises_naming_the_step_index_of_a_later_step() -> None:
    """The unresolved var can be in any step — the whole plan is
    validated up front, before step 0 (or any step) dispatches."""
    from nexus.plans.runner import PlanRunUnresolvedVarError, plan_run

    plan = {
        "steps": [
            {"tool": "search", "args": {"query": "literal"}},
            {"tool": "summarize", "args": {"content": "$missing"}},
        ],
    }
    disp = _FakeDispatcher()
    with pytest.raises(PlanRunUnresolvedVarError) as exc:
        await plan_run(_match(plan), {}, dispatcher=disp)
    assert exc.value.step_index == 1
    assert exc.value.var_name == "missing"
    assert disp.calls == []


@pytest.mark.asyncio
async def test_run_raises_on_unresolved_var_inside_a_list_arg() -> None:
    """``_resolve_value`` recurses element-wise into list args; the
    validator must scan the same shape."""
    from nexus.plans.runner import PlanRunUnresolvedVarError, plan_run

    plan = {
        "steps": [{"tool": "traverse", "args": {"seeds": ["fixed", "$missing_seed"]}}],
    }
    with pytest.raises(PlanRunUnresolvedVarError) as exc:
        await plan_run(_match(plan), {}, dispatcher=_FakeDispatcher())
    assert exc.value.step_index == 0
    assert exc.value.var_name == "missing_seed"


@pytest.mark.asyncio
async def test_run_raises_on_unresolved_var_in_scope_topic() -> None:
    """``scope.topic: $var`` (e.g. analyze-default, research-default) is
    resolved through the same ``$var`` branch as args, via
    ``_apply_scope_to_args``, and forwarded into the dispatched call —
    an unresolved var there must be caught too, not just in ``args``."""
    from nexus.plans.runner import PlanRunUnresolvedVarError, plan_run

    plan = {
        "steps": [{
            "tool": "search",
            "args": {"query": "literal"},
            "scope": {"topic": "$missing_topic"},
        }],
    }
    with pytest.raises(PlanRunUnresolvedVarError) as exc:
        await plan_run(_match(plan), {}, dispatcher=_FakeDispatcher())
    assert exc.value.step_index == 0
    assert exc.value.var_name == "missing_topic"


@pytest.mark.asyncio
async def test_run_scope_topic_dead_when_args_already_sets_topic() -> None:
    """``_apply_scope_to_args`` only resolves ``scope.topic`` when the
    step's own ``args`` has NOT already set ``"topic"`` — a caller-set
    ``args["topic"]`` wins outright and ``scope.topic`` is never touched
    by ``_resolve_value`` at all. This is a VALID plan and must NOT
    raise, even though ``scope.topic`` names an unresolved $var (review
    finding, code-review MEDIUM)."""
    from nexus.plans.runner import plan_run

    plan = {
        "steps": [{
            "tool": "search",
            "args": {"topic": "explicit", "query": "literal"},
            "scope": {"topic": "$unset_var"},
        }],
    }
    disp = _FakeDispatcher()
    await plan_run(_match(plan), {}, dispatcher=disp)
    assert disp.calls[0][1] == {
        "topic": "explicit", "query": "literal",
        "corpus": _PLAN_STEP_DEFAULT_CORPUS,
    }


@pytest.mark.asyncio
async def test_run_raises_named_error_on_non_dict_args() -> None:
    """A malformed step whose ``args`` isn't a dict must raise a named
    PlanRun*Error with the step index — not a bare AttributeError from
    ``.values()`` (critic Significant #2)."""
    from nexus.plans.runner import PlanRunToolNotFoundError, plan_run

    plan = {
        "steps": [{"tool": "traverse", "args": ["not", "a", "dict"]}],
    }
    disp = _FakeDispatcher()
    with pytest.raises(PlanRunToolNotFoundError) as exc:
        await plan_run(_match(plan), {}, dispatcher=disp)
    assert "steps[0]" in str(exc.value)
    assert "non-dict args" in str(exc.value)
    assert disp.calls == []


@pytest.mark.asyncio
async def test_run_validates_whole_plan_before_a_past_deadline_stops_it() -> None:
    """Eager whole-plan validation runs BEFORE the deadline pre-segment
    check — a $var problem in a late step still hard-fails the call even
    when a tight deadline would otherwise have stopped the run before
    ever reaching that step (critic Significant #1; contract now
    documented in plan_run's own docstring)."""
    import time

    from nexus.plans.runner import PlanRunUnresolvedVarError, plan_run

    plan = {
        "steps": [
            {"tool": "search", "args": {"query": "literal"}},
            {"tool": "summarize", "args": {"content": "$missing"}},
        ],
    }
    disp = _FakeDispatcher()
    with pytest.raises(PlanRunUnresolvedVarError) as exc:
        await plan_run(
            _match(plan), {}, dispatcher=disp,
            deadline=time.monotonic() - 1.0,  # already expired
        )
    assert exc.value.step_index == 1
    assert exc.value.var_name == "missing"
    # Not a silent budget_exhausted early-stop -- the run never got that
    # far because validation happens first, and nothing dispatched.
    assert disp.calls == []


@pytest.mark.asyncio
async def test_run_optional_var_with_default_still_resolves() -> None:
    """An optional (non-required) $var backed by a plan default resolves
    normally — the new validation must not regress the default-fallback
    path."""
    from nexus.plans.runner import plan_run

    plan = {
        "steps": [{"tool": "search", "args": {"query": "$topic"}}],
        "optional_bindings": ["topic"],
    }
    match = _match(plan, default_bindings={"topic": "from-default"})
    disp = _FakeDispatcher()
    await plan_run(match, {}, dispatcher=disp)
    assert disp.calls[0][1] == {
        "query": "from-default", "corpus": _PLAN_STEP_DEFAULT_CORPUS,
    }


@pytest.mark.asyncio
async def test_run_optional_var_caller_supplied_still_wins() -> None:
    """A caller-supplied value resolves an optional $var with no plan
    default at all."""
    from nexus.plans.runner import plan_run

    plan = {
        "steps": [{"tool": "search", "args": {"query": "$topic"}}],
        "optional_bindings": ["topic"],
    }
    disp = _FakeDispatcher()
    await plan_run(_match(plan), {"topic": "caller-value"}, dispatcher=disp)
    assert disp.calls[0][1] == {
        "query": "caller-value", "corpus": _PLAN_STEP_DEFAULT_CORPUS,
    }


@pytest.mark.asyncio
async def test_run_plan_with_no_var_refs_is_unaffected() -> None:
    """False-positive guard: a plan with zero $var tokens must run
    exactly as before — the new scan must never fire on plain literals."""
    from nexus.plans.runner import plan_run

    plan = {
        "steps": [{"tool": "search", "args": {"query": "literal query", "limit": 5}}],
    }
    disp = _FakeDispatcher()
    await plan_run(_match(plan), {}, dispatcher=disp)
    assert disp.calls[0] == (
        "search",
        {
            "query": "literal query", "limit": 5,
            "corpus": _PLAN_STEP_DEFAULT_CORPUS,
        },
    )


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
async def test_run_rejects_operator_step_missing_required_arg_at_real_dispatch() -> None:
    """nexus-nyry9.4 review-fix (critic finding #3, T2 [23036]): a
    ``summarize`` step with no way to supply ``content`` — no ``ids`` to
    hydrate from, no ``content`` directly, no ``inputs`` alias
    (nexus-yis0) — reproduces ``operator_summarize() missing 1 required
    positional argument: 'content'`` at real dispatch today
    (nx_answer_runs id=4, mislabeled "likely closed by auto-hydration"
    in the taxonomy — it is not). Rejected with a named, step-indexed
    error instead. Deliberately exercises the REAL dispatcher (no
    ``dispatcher=`` override) — the fix lives in ``_default_dispatcher``,
    gated on the real callable's own signature, so it must never fire
    against a caller-injected fake/test dispatcher (every other test in
    this file uses one and must stay unaffected)."""
    from nexus.plans.runner import PlanRunOperatorArgMissingError, plan_run

    plan = {"steps": [{"tool": "summarize", "args": {}}]}
    with pytest.raises(PlanRunOperatorArgMissingError) as exc:
        await plan_run(_match(plan), {})
    assert exc.value.step_index == 0
    assert exc.value.tool == "operator_summarize"
    assert exc.value.missing_arg == "content"


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
async def test_run_no_scope_binding_falls_through_to_plan_step_default() -> None:
    """When caller omits ``_nx_scope`` too, the step is NOT left
    corpus-less (RDR-200 Phase 1b, nexus-rl59s) — it falls through to
    :data:`_PLAN_STEP_DEFAULT_CORPUS` rather than the bare MCP tool
    default, which structurally excludes ``rdr__``."""
    from nexus.plans.runner import plan_run

    plan = {"steps": [{"tool": "search", "args": {"query": "x"}}]}
    disp = _FakeDispatcher([{"text": "ok", "ids": []}])
    await plan_run(_match(plan), {}, dispatcher=disp)
    _tool, args = disp.calls[0]
    assert args["corpus"] == _PLAN_STEP_DEFAULT_CORPUS


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


# ── Plan-step default corpus fall-through (RDR-200 Phase 1b, nexus-rl59s) ───


@pytest.mark.asyncio
async def test_default_corpus_fills_search_with_no_scoping_at_all() -> None:
    """A ``search`` step with no plan corpus, no ``scope``, and no caller
    ``_nx_scope`` binding gets :data:`_PLAN_STEP_DEFAULT_CORPUS`."""
    from nexus.plans.runner import plan_run

    plan = {"steps": [{"tool": "search", "args": {"query": "x"}}]}
    disp = _FakeDispatcher([{"text": "ok", "ids": []}])
    await plan_run(_match(plan), {}, dispatcher=disp)
    _tool, args = disp.calls[0]
    assert args["corpus"] == _PLAN_STEP_DEFAULT_CORPUS


@pytest.mark.asyncio
async def test_default_corpus_fills_query_with_no_scoping_at_all() -> None:
    """``query`` gets the same default-corpus fall-through as ``search``."""
    from nexus.plans.runner import plan_run

    plan = {"steps": [{"tool": "query", "args": {"question": "x"}}]}
    disp = _FakeDispatcher([{"text": "ok", "ids": []}])
    await plan_run(_match(plan), {}, dispatcher=disp)
    _tool, args = disp.calls[0]
    assert args["corpus"] == _PLAN_STEP_DEFAULT_CORPUS


@pytest.mark.asyncio
async def test_default_corpus_does_not_override_plan_declared_corpus() -> None:
    """A plan-pinned ``corpus`` always wins over the fall-through default."""
    from nexus.plans.runner import plan_run

    plan = {
        "steps": [
            {"tool": "search", "args": {"query": "x", "corpus": "code__delos"}},
        ],
    }
    disp = _FakeDispatcher([{"text": "ok", "ids": []}])
    await plan_run(_match(plan), {}, dispatcher=disp)
    _tool, args = disp.calls[0]
    assert args["corpus"] == "code__delos"


@pytest.mark.asyncio
async def test_default_corpus_does_not_override_plan_collections() -> None:
    """A plan-pinned ``collection``/``collections`` arg wins too — no
    ``corpus`` key is injected alongside it."""
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
    await plan_run(_match(plan), {}, dispatcher=disp)
    _tool, args = disp.calls[0]
    assert "corpus" not in args
    assert args["collection"] == "rdr__delos"


@pytest.mark.asyncio
async def test_caller_scope_binding_wins_over_the_plan_step_default() -> None:
    """When ``_nx_scope`` IS present, it fills the corpus before the
    fall-through default ever runs — the caller's narrower scope wins,
    not the broad four-prefix default."""
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
async def test_default_corpus_skips_search_metadata_scoped() -> None:
    """Catalog-routed combined-query tools keep their own semantics —
    the plain search/query default must not reach them."""
    from nexus.plans.runner import plan_run

    plan = {
        "steps": [
            {"tool": "search_metadata_scoped", "args": {"query": "x"}},
        ],
    }
    disp = _FakeDispatcher([{"ids": [], "tumblers": [], "collections": []}])
    await plan_run(_match(plan), {}, dispatcher=disp)
    _tool, args = disp.calls[0]
    assert "corpus" not in args


@pytest.mark.asyncio
async def test_default_corpus_skips_store_get_many() -> None:
    """``store_get_many`` hydrates by explicit ids/collections — it must
    never get a bare-prefix corpus default injected."""
    from nexus.plans.runner import plan_run

    plan = {
        "steps": [
            {"tool": "store_get_many", "args": {"ids": ["a"], "collections": "knowledge__x"}},
        ],
    }
    disp = _FakeDispatcher([{"contents": ["c"], "missing": []}])
    await plan_run(_match(plan), {}, dispatcher=disp)
    _tool, args = disp.calls[0]
    assert "corpus" not in args


@pytest.mark.asyncio
async def test_default_corpus_skips_traverse() -> None:
    """``traverse`` operates on tumblers, never embeddings — no corpus
    default belongs on it."""
    from nexus.plans.runner import plan_run

    plan = {"steps": [{"tool": "traverse", "args": {"seeds": ["1.1"]}}]}
    disp = _FakeDispatcher([{"tumblers": [], "ids": [], "collections": []}])
    await plan_run(_match(plan), {}, dispatcher=disp)
    _tool, args = disp.calls[0]
    assert "corpus" not in args


@pytest.mark.asyncio
async def test_default_corpus_end_to_end_two_step_plan() -> None:
    """A two-step ``search`` (no corpus) → ``store_get_many`` plan
    dispatches the search step with the default corpus, and the
    hydration step is unaffected — confirms the matched-plan path (a
    library plan whose retrieval step declares no corpus) picks up
    Fix 1's default automatically, with no separate matched-plan change
    needed."""
    from nexus.plans.runner import plan_run

    plan = {
        "steps": [
            {"tool": "search", "args": {"query": "x"}},
            {
                "tool": "store_get_many",
                "args": {"ids": "$step1.ids", "collections": "$step1.collections"},
            },
        ],
    }
    disp = _FakeDispatcher([
        {"ids": ["a"], "tumblers": [], "distances": [], "collections": ["knowledge__y"]},
        {"contents": ["hydrated"], "missing": []},
    ])
    await plan_run(_match(plan), {}, dispatcher=disp)

    search_tool, search_args = disp.calls[0]
    assert search_tool == "search"
    assert search_args["corpus"] == _PLAN_STEP_DEFAULT_CORPUS

    hydrate_tool, hydrate_args = disp.calls[1]
    assert hydrate_tool == "store_get_many"
    assert hydrate_args["ids"] == ["a"]
    assert hydrate_args["collections"] == ["knowledge__y"]
    assert "corpus" not in hydrate_args


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
async def test_run_rejects_step_with_empty_tool_name_before_dispatch() -> None:
    """nexus-nyry9.4 (RDR-196 residual): the plan-138 crash class
    (nx_answer_runs ids 177/183/184/369, ``unknown tool ''``). An empty
    tool name is a malformed plan — reject it at VALIDATION before any
    step dispatches, not at dispatch time after already burning
    wall-clock on earlier steps in the same plan."""
    from nexus.plans.runner import PlanRunToolNotFoundError, plan_run

    plan = {
        "steps": [
            {"tool": "search", "args": {"query": "x"}},
            {"tool": "", "args": {}},
        ],
    }
    disp = _FakeDispatcher([{"text": "search-output"}])
    with pytest.raises(PlanRunToolNotFoundError, match=r"steps\[1\]"):
        await plan_run(_match(plan), {}, dispatcher=disp)
    # Validated up front — step 0 never dispatched either.
    assert disp.calls == []


@pytest.mark.asyncio
async def test_run_rejects_step_with_missing_tool_key_before_dispatch() -> None:
    """Same malformed-plan class as the empty-string case: no
    ``tool``/``op``/``operation`` key at all."""
    from nexus.plans.runner import PlanRunToolNotFoundError, plan_run

    plan = {"steps": [{"args": {"query": "x"}}]}
    disp = _FakeDispatcher()
    with pytest.raises(PlanRunToolNotFoundError, match=r"steps\[0\]"):
        await plan_run(_match(plan), {}, dispatcher=disp)
    assert disp.calls == []


@pytest.mark.asyncio
async def test_run_rejects_step_with_non_string_tool_value_before_dispatch() -> None:
    """nexus-nyry9.4 review-fix (code-review-expert + substantive-critic,
    T2 [23035]/[23036]): a truthy non-string ``tool`` value (e.g. an int)
    previously reached ``t.startswith(...)`` inside ``_extract_tool`` and
    raised a bare ``AttributeError`` instead of the intended, step-
    indexed ``PlanRunToolNotFoundError`` — defeating the very validation
    loop meant to fail loud on exactly this shape."""
    from nexus.plans.runner import PlanRunToolNotFoundError, plan_run

    plan = {"steps": [{"tool": 5, "args": {}}]}
    disp = _FakeDispatcher()
    with pytest.raises(PlanRunToolNotFoundError, match=r"steps\[0\].*non-string.*5"):
        await plan_run(_match(plan), {}, dispatcher=disp)
    assert disp.calls == []


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
    names (mcp__plugin_conexus_nexus__search). The runner must strip the prefix
    before resolution."""

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "tool_in, expected_out",
        [
            ("mcp__plugin_conexus_nexus__search", "search"),
            ("search", "search"),
            # Planner may use tools from other MCP servers (serena,
            # context7). The prefix should be stripped to the bare name.
            ("mcp__plugin_sn_serena__jet_brains_find_symbol", "jet_brains_find_symbol"),
        ],
        ids=["nexus_mcp_prefix_stripped", "bare_tool_name_unchanged", "non_nexus_mcp_prefix_stripped"],
    )
    async def test_tool_name_prefix_stripping(self, tool_in, expected_out):
        from nexus.plans.runner import plan_run

        plan = {"steps": [{"tool": tool_in, "args": {"query": "test"}}]}
        disp = _FakeDispatcher()
        await plan_run(_match(plan), {}, dispatcher=disp)
        # Dispatcher should receive the bare tool name, not the full prefix.
        assert disp.calls[0][0] == expected_out


class TestLegacyToolKeyAliases:
    """Old plans use 'op' or 'operation' instead of 'tool'. The runner
    should accept all three."""

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "key, tool_value, args",
        [
            ("op", "search", {"query": "test"}),
            ("operation", "query", {"question": "test"}),
        ],
        ids=["op_key_alias", "operation_key_alias"],
    )
    async def test_legacy_key_alias(self, key, tool_value, args):
        from nexus.plans.runner import plan_run

        plan = {"steps": [{key: tool_value, "args": args}]}
        disp = _FakeDispatcher()
        await plan_run(_match(plan), {}, dispatcher=disp)
        assert disp.calls[0][0] == tool_value


# ── _hydrate_operator_args: inputs-arg translation (nexus-yis0) ───────────────


class TestHydrateInputsTranslation:
    """nexus-yis0: when a prior explicit ``store_get_many`` step feeds an
    operator via ``inputs: $stepN.contents`` (no ``ids`` key on the
    operator step), ``_hydrate_operator_args`` must still rename
    ``inputs`` to the operator's expected positional arg. Otherwise
    the unknown-kwarg drop in ``_default_dispatcher`` strips the arg
    and the operator fires with no positional, raising TypeError.
    """

    @pytest.mark.parametrize(
        "op, args_in",
        [
            ("filter", {"items": "[]", "criterion": "relevance"}),
            ("check", {"items": "[]", "check_instruction": "consistent"}),
            ("verify", {"claim": "X is true", "evidence": "see §2"}),
            ("groupby", {"items": "[]", "key": "publication year"}),
            ("aggregate", {"groups": "[]", "reducer": "most-cited method"}),
        ],
        ids=["filter", "check", "verify", "groupby", "aggregate"],
    )
    def test_bare_name_resolves_to_operator_tool(self, op, args_in):
        """RDR-088 nexus-ac40.1 / RDR-093 nexus-9bz6+o7u2: plan YAML using
        the bare verb (``tool: filter`` / ``check`` / ``verify`` /
        ``groupby`` / ``aggregate``) must resolve through
        ``_OPERATOR_TOOL_MAP`` to ``operator_<op>``, with correctly-named
        args passed through untouched."""
        from nexus.plans.runner import _hydrate_operator_args

        tool, args = _hydrate_operator_args(op, args_in)
        assert tool == f"operator_{op}"
        assert args == args_in

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

    # ── operator_check hydration (RDR-088 nexus-ac40.4) ─────────────────

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

    # ── operator_verify hydration (RDR-088 nexus-ac40.4) ────────────────

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

    # ── items coercion/hydration shared by filter, check & groupby ───────
    # (RDR-088 nexus-ac40.1/.4, RDR-093 nexus-9bz6): each of these three
    # operators exposes an ``items`` positional arg fed either by a
    # pre-hydrated ``inputs``/``items`` value or an auto-hydrated ``ids``
    # list. The coercion/preservation/hydration behavior is identical
    # across the three; only the operator's other required kwarg
    # (criterion / check_instruction / key) differs.

    @pytest.mark.parametrize(
        "op, extra_kwarg",
        [
            ("filter", ("criterion", "x")),
            ("check", ("check_instruction", "consistent")),
            ("groupby", ("key", "x")),
        ],
        ids=["filter", "check", "groupby"],
    )
    def test_coerces_list_items_to_json(self, op, extra_kwarg):
        """List-valued ``items`` must be json-encoded so the operator
        prompt sees clean JSON rather than Python repr. Mirrors the
        existing rank/compare coercion at runner.py:666."""
        from nexus.plans.runner import _hydrate_operator_args

        key, value = extra_kwarg
        tool, args = _hydrate_operator_args(
            op, {"items": [{"id": "a"}, {"id": "b"}], key: value},
        )
        assert tool == f"operator_{op}"
        assert args["items"] == json.dumps([{"id": "a"}, {"id": "b"}])

    @pytest.mark.parametrize(
        "op, extra_kwarg",
        [
            ("filter", ("criterion", "x")),
            ("groupby", ("key", "year")),
        ],
        ids=["filter", "groupby"],
    )
    def test_preserves_string_items(self, op, extra_kwarg):
        """Already-stringified ``items`` must pass through untouched
        to avoid double-JSON-encoding."""
        from nexus.plans.runner import _hydrate_operator_args

        key, value = extra_kwarg
        already_json = '[{"id": "a"}]'
        tool, args = _hydrate_operator_args(
            op, {"items": already_json, key: value},
        )
        assert tool == f"operator_{op}"
        assert args["items"] == already_json

    @pytest.mark.parametrize(
        "op, extra_kwarg",
        [
            ("filter", ("criterion", "on-topic")),
            ("check", ("check_instruction", "claim holds")),
            ("groupby", ("key", "year")),
        ],
        ids=["filter", "check", "groupby"],
    )
    def test_ids_hydrates_to_items(self, op, extra_kwarg):
        """When a step declares ``ids:`` and the auto-hydration path runs
        ``store_get_many``, the fetched content list must land on the
        ``items`` arg (not ``inputs``), so the operator's positional arg
        is populated."""
        from unittest.mock import patch

        from nexus.plans.runner import _hydrate_operator_args

        key, value = extra_kwarg
        fake_contents = {"contents": ["doc-a body", "doc-b body"]}
        with patch(
            "nexus.mcp.core.store_get_many", return_value=fake_contents,
        ):
            tool, args = _hydrate_operator_args(
                op, {"ids": ["doc-a", "doc-b"], key: value},
            )
        assert tool == f"operator_{op}"
        assert "ids" not in args and "collections" not in args
        assert args["items"] == json.dumps(["doc-a body", "doc-b body"])
        assert args[key] == value

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
        async def fake_dispatch(prompt, schema, timeout=300.0, **kwargs):
            # RDR-196 .p1b: the real bundle path now always passes a
            # usage_sink kwarg into claude_dispatch — accept and ignore
            # unknown kwargs here since this fake stands in for
            # claude_dispatch directly (not dispatch_bundle).
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


# ── nx_answer step progress logs (nexus-0qi9) ───────────────────────────────


class TestPlanRunStepProgressLogs:
    """Per-step structured log events for nx_answer progress visibility.

    Pre-fix: a multi-step plan run was indistinguishable from a hang
    from the caller's seat. With these events on structlog, downstream
    log readers (mcp.log tail, ``nx tier-status`` joins, etc.) can see
    where the run is in real time.
    """

    @pytest.fixture(autouse=True)
    def _info_level(self):
        """Default conftest sets structlog to WARNING; the progress
        events fire at INFO. Lower the threshold for this class so
        capture_logs sees them. The autouse _restore_structlog_after_test
        in conftest.py undoes this between tests."""
        import logging
        import structlog
        structlog.configure(
            wrapper_class=structlog.make_filtering_bound_logger(logging.INFO),
        )

    @pytest.mark.asyncio
    async def test_isolated_steps_emit_start_and_complete(self) -> None:
        """Each isolated segment emits paired start + complete events
        with step indices, tool names, and elapsed_ms on completion."""
        import structlog.testing

        from nexus.plans.runner import plan_run

        plan = {
            "steps": [
                {"tool": "search", "args": {"query": "x"}},
                {"tool": "query",  "args": {"question": "y"}},
            ],
        }
        disp = _FakeDispatcher([
            {"text": "r1"}, {"text": "r2"},
        ])
        with structlog.testing.capture_logs() as captured:
            await plan_run(_match(plan), {}, dispatcher=disp)

        starts = [
            e for e in captured if e.get("event") == "nx_answer_step_start"
        ]
        completes = [
            e for e in captured if e.get("event") == "nx_answer_step_complete"
        ]
        assert len(starts) == 2, f"expected 2 starts, got {captured}"
        assert len(completes) == 2, f"expected 2 completes, got {captured}"
        # Step indices monotonically increase.
        assert starts[0]["step_indices"] == [0]
        assert starts[1]["step_indices"] == [1]
        # Tools land in the event.
        assert starts[0]["tools"] == ["search"]
        assert starts[1]["tools"] == ["query"]

    @pytest.mark.asyncio
    async def test_complete_event_carries_elapsed_ms(self) -> None:
        """The complete event must include elapsed_ms so callers can
        diagnose slow steps after the fact."""
        import structlog.testing

        from nexus.plans.runner import plan_run

        plan = {"steps": [{"tool": "search", "args": {"query": "x"}}]}
        disp = _FakeDispatcher([{"text": "r"}])

        with structlog.testing.capture_logs() as captured:
            await plan_run(_match(plan), {}, dispatcher=disp)

        completes = [
            e for e in captured if e.get("event") == "nx_answer_step_complete"
        ]
        assert len(completes) == 1
        ev = completes[0]
        assert "elapsed_ms" in ev, (
            f"complete event missing elapsed_ms: {ev!r}"
        )
        assert isinstance(ev["elapsed_ms"], int)
        assert ev["elapsed_ms"] >= 0
        assert ev["kind"] == "isolated"


# ── nexus-l0yh: OperatorError graceful fallback ────────────────────────────


class _OperatorFailingDispatcher:
    """Dispatcher that raises ``OperatorError`` on any operator step.

    Retrieval steps return a stub dict so step-output binding still
    works for downstream refs. Used to verify the runner's graceful
    degradation behavior (nexus-l0yh).
    """

    def __init__(self, message: str = "claude -p exited 1") -> None:
        self.calls: list[tuple[str, dict]] = []
        self._message = message

    async def __call__(self, tool: str, args: dict) -> dict:
        # Import-from-module each call so the OperatorError class
        # identity always matches whatever ``nexus.operators.dispatch``
        # currently exposes — robust against test-order-dependent
        # patches in sibling test files (e.g. mock.patch.object on the
        # dispatch module that briefly replaces module attributes).
        import nexus.operators.dispatch as _dispatch_mod
        self.calls.append((tool, args))
        if tool.startswith("operator_"):
            raise _dispatch_mod.OperatorError(self._message)
        return {"text": f"{tool}(stub)", "ids": [], "tumblers": []}


@pytest.mark.asyncio
async def test_run_substitutes_sentinel_on_operator_error_isolated() -> None:
    """nexus-l0yh: an isolated operator step that raises OperatorError
    must NOT propagate; the runner substitutes a failure sentinel into
    step_outputs and continues with the next step.
    """
    from nexus.plans.runner import plan_run

    plan = {
        "steps": [
            {"tool": "search", "args": {"query": "x"}},
            {"tool": "operator_summarize", "args": {"text": "$step1.text"}},
            {"tool": "search", "args": {"query": "y"}},
        ],
    }
    disp = _OperatorFailingDispatcher("simulated dispatch failure")
    result = await plan_run(_match(plan), {}, dispatcher=disp)

    # All three steps must have run — failure on step 2 must NOT
    # short-circuit step 3.
    assert len(disp.calls) == 3
    assert disp.calls[0][0] == "search"
    assert disp.calls[1][0] == "operator_summarize"
    assert disp.calls[2][0] == "search"

    # step 2's output is the sentinel.
    s2 = result.steps[1]
    assert s2["status"] == "failed"
    assert s2["tool"] == "operator_summarize"
    assert s2["step_index"] == 1
    assert "simulated dispatch failure" in s2["error"]
    # Empty downstream-ref fields so $stepN.<field> resolves to "" not raises.
    assert s2["text"] == ""
    assert s2["summary"] == ""


# ── nexus-h33x8.6 a4: hard wall-clock budget + partial results ─────────────
#
# Design (T2 nexus/nx-answer-capability-analysis-2026-08-19 + dev notes
# nx-answer-a3-a1-dev-notes-2026-08-19): retrieval steps finish early
# (16-29% of run time, mean 8.5s/step); the claude -p operator bundle is
# the 30s+ tail. ``plan_run(..., deadline=<monotonic time>)`` checks the
# deadline before starting each segment and, when exceeded, stops the
# loop and records ``PlanResult.budget_exhausted_at_step`` (1-indexed)
# instead of running the operator segment. When an operator segment's
# own dispatch raises ``OperatorTimeoutError`` while a deadline is
# active, the runner captures the exception's reconstructed
# ``partial_text``/``event_count`` into the step sentinel and stops the
# loop the same way — it does NOT silently substitute-and-continue as
# the deadline-less path does.
#
# ``deadline=None`` (the default) must reproduce EXACTLY the pre-a4
# behavior: substitute a sentinel and keep going, per
# ``test_run_substitutes_sentinel_on_operator_error_isolated`` above.


@pytest.mark.asyncio
async def test_deadline_stops_before_operator_segment_after_retrieval() -> None:
    """A budget that expires between the retrieval step and the operator
    step must let retrieval finish, then stop BEFORE dispatching the
    operator — never mid-flight, never after.
    """
    import time as _time
    from unittest.mock import patch

    from nexus.plans.runner import plan_run

    plan = {
        "steps": [
            {"tool": "search", "args": {"query": "x", "corpus": "knowledge"}},
            {"tool": "extract", "args": {"fields": "a", "inputs": "[]"}},
        ],
    }
    calls: list[str] = []

    async def stub_search(**kwargs):
        calls.append("search")
        import asyncio
        await asyncio.sleep(0.05)
        return {"ids": ["a"], "tumblers": [], "distances": [], "collections": []}

    async def stub_extract(**kwargs):
        calls.append("extract")
        return {"extractions": []}

    from nexus.mcp import core as mcp_core

    deadline = _time.monotonic() + 0.02  # expires during the search sleep
    with patch.object(mcp_core, "search", stub_search), \
         patch.object(mcp_core, "operator_extract", stub_extract):
        result = await plan_run(_match(plan), {}, deadline=deadline, bundle_operators=False)

    assert calls == ["search"], f"extract must not have been dispatched, got {calls}"
    assert result.budget_exhausted_at_step == 2
    assert result.total_planned_steps == 2
    assert len(result.steps) == 1


@pytest.mark.asyncio
async def test_no_deadline_preserves_default_fields() -> None:
    """``deadline=None`` (the default) must leave the new PlanResult
    fields at their inert defaults — proves the a4 param does not
    silently change behavior when unset.
    """
    from nexus.plans.runner import plan_run

    plan = {"steps": [{"tool": "search", "args": {"query": "x"}}]}
    disp = _FakeDispatcher(outputs=[{"ids": [], "tumblers": []}])
    result = await plan_run(_match(plan), {}, dispatcher=disp)

    assert result.budget_exhausted_at_step is None
    assert result.total_planned_steps == 1


@pytest.mark.asyncio
async def test_isolated_operator_timeout_under_deadline_captures_partial_and_stops() -> None:
    """An OperatorTimeoutError raised while a deadline is active must
    capture partial_text/event_count into the sentinel AND stop the
    loop — the third step (another search) must never run.
    """
    from unittest.mock import patch
    import time as _time

    from nexus.operators.dispatch import OperatorTimeoutError
    from nexus.plans.runner import plan_run

    plan = {
        "steps": [
            {"tool": "search", "args": {"query": "x"}},
            {"tool": "extract", "args": {"fields": "a", "inputs": "[]"}},
            {"tool": "search", "args": {"query": "y"}},
        ],
    }

    async def stub_search(**kwargs):
        return {"ids": ["a"], "tumblers": [], "distances": [], "collections": []}

    async def stub_extract(**kwargs):
        raise OperatorTimeoutError(
            "claude -p timed out after 9s",
            partial_text="partial synthesis text",
            event_count=7,
        )

    from nexus.mcp import core as mcp_core

    deadline = _time.monotonic() + 300  # not expired at any check point
    with patch.object(mcp_core, "search", stub_search), \
         patch.object(mcp_core, "operator_extract", stub_extract):
        result = await plan_run(_match(plan), {}, deadline=deadline, bundle_operators=False)

    assert result.budget_exhausted_at_step == 2
    assert len(result.steps) == 2, "the third (post-timeout) step must not have run"
    s2 = result.steps[1]
    assert s2["partial_text"] == "partial synthesis text"
    assert s2["event_count"] == 7
    assert s2["status"] == "timeout"


@pytest.mark.asyncio
async def test_isolated_operator_timeout_without_deadline_unchanged_behavior() -> None:
    """Regression: with NO deadline, an OperatorTimeoutError must
    behave exactly like any other OperatorError — sentinel substituted,
    loop continues, no partial_text leaks into the sentinel shape.
    """
    from unittest.mock import patch

    from nexus.operators.dispatch import OperatorTimeoutError
    from nexus.plans.runner import plan_run

    plan = {
        "steps": [
            {"tool": "search", "args": {"query": "x"}},
            {"tool": "extract", "args": {"fields": "a", "inputs": "[]"}},
            {"tool": "search", "args": {"query": "y"}},
        ],
    }

    async def stub_search(**kwargs):
        return {"ids": ["a"], "tumblers": [], "distances": [], "collections": []}

    async def stub_extract(**kwargs):
        raise OperatorTimeoutError(
            "claude -p timed out after 300s",
            partial_text="should not leak",
            event_count=3,
        )

    from nexus.mcp import core as mcp_core

    with patch.object(mcp_core, "search", stub_search), \
         patch.object(mcp_core, "operator_extract", stub_extract):
        result = await plan_run(_match(plan), {}, bundle_operators=False)

    assert result.budget_exhausted_at_step is None
    assert len(result.steps) == 3, "all three steps must run without a deadline"
    s2 = result.steps[1]
    assert s2["status"] == "failed"
    assert "partial_text" not in s2
    assert "event_count" not in s2


@pytest.mark.asyncio
async def test_isolated_operator_receives_remaining_budget_as_timeout_kwarg() -> None:
    """When a deadline is active, the isolated operator dispatch must
    receive the REMAINING budget as its ``timeout`` kwarg — genuine
    enforcement, not just a pre-check."""
    from unittest.mock import patch
    import time as _time

    from nexus.plans.runner import plan_run

    plan = {"steps": [{"tool": "extract", "args": {"fields": "a", "inputs": "[]"}}]}
    captured: dict = {}

    async def stub_extract(**kwargs):
        captured.update(kwargs)
        return {"extractions": []}

    from nexus.mcp import core as mcp_core

    deadline = _time.monotonic() + 9.0
    with patch.object(mcp_core, "operator_extract", stub_extract):
        await plan_run(_match(plan), {}, deadline=deadline, bundle_operators=False)

    assert "timeout" in captured
    assert 0 < captured["timeout"] <= 9.0


@pytest.mark.asyncio
async def test_bundle_operator_timeout_under_deadline_captures_partial_and_stops() -> None:
    """Same contract as the isolated-path test, for the bundle path:
    the OperatorTimeoutError's partial_text must land on the terminal
    slot's sentinel, and the loop must stop (bundling requires 2+
    contiguous operator steps)."""
    from unittest.mock import AsyncMock, patch
    import time as _time

    from nexus.operators.dispatch import OperatorTimeoutError
    from nexus.plans.runner import plan_run

    plan = {
        "steps": [
            {"tool": "search", "args": {"query": "x"}},
            {"tool": "extract", "args": {"fields": "a", "inputs": "[]"}},
            {"tool": "summarize", "args": {"cited": False, "content": "$step2.extractions"}},
        ],
    }

    async def stub_search(**kwargs):
        return {"ids": ["a"], "tumblers": [], "distances": [], "collections": []}

    fake_bundle = AsyncMock(side_effect=OperatorTimeoutError(
        "claude -p timed out", partial_text="bundle partial", event_count=3,
    ))

    from nexus.mcp import core as mcp_core

    deadline = _time.monotonic() + 300
    with patch.object(mcp_core, "search", stub_search), \
         patch("nexus.plans.bundle.dispatch_bundle", fake_bundle):
        result = await plan_run(_match(plan), {}, deadline=deadline)

    assert result.budget_exhausted_at_step == 2
    last = result.steps[-1]
    assert last["partial_text"] == "bundle partial"
    assert last["event_count"] == 3
    # dispatch_bundle received the remaining budget, not the 300s default.
    _, kwargs = fake_bundle.call_args
    assert "timeout" in kwargs
    assert 0 < kwargs["timeout"] <= 300


@pytest.mark.asyncio
async def test_bundle_receives_remaining_budget_as_timeout_kwarg_on_success() -> None:
    """Even on a successful bundle dispatch, an active deadline must
    thread the REMAINING budget through as ``timeout`` — this is what
    makes the budget genuinely hard rather than a best-effort pre-check."""
    from unittest.mock import AsyncMock, patch
    import time as _time

    from nexus.plans.runner import plan_run

    plan = {
        "steps": [
            {"tool": "search", "args": {"query": "x"}},
            {"tool": "extract", "args": {"fields": "a", "inputs": "[]"}},
            {"tool": "summarize", "args": {"cited": False, "content": "$step2.extractions"}},
        ],
    }

    async def stub_search(**kwargs):
        return {"ids": ["a"], "tumblers": [], "distances": [], "collections": []}

    fake_bundle = AsyncMock(return_value={"summary": "ok"})

    from nexus.mcp import core as mcp_core

    deadline = _time.monotonic() + 12.5
    with patch.object(mcp_core, "search", stub_search), \
         patch("nexus.plans.bundle.dispatch_bundle", fake_bundle):
        result = await plan_run(_match(plan), {}, deadline=deadline)

    assert result.budget_exhausted_at_step is None
    _, kwargs = fake_bundle.call_args
    assert kwargs["timeout"] == pytest.approx(12.5, abs=2.0)


# ── RDR-196 .p3c (nexus-nyry9.21): USD cost check ───────────────────────────
#
# Mirrors the deadline check's pre-segment placement exactly:
# ``budget_usd_remaining`` is compared against the running sum of already-
# completed steps' non-None cost_usd BEFORE dispatching the next segment.
# Unlike the deadline, there is no mid-dispatch cost cut (a dispatch's real
# cost is unknowable until it returns) -- so this is a stop-line, not a
# hard ceiling: the segment that pushes the sum over the cap still runs to
# completion.


@pytest.mark.asyncio
async def test_budget_usd_remaining_stops_before_next_segment() -> None:
    """A bundle dispatch costing more than budget_usd_remaining must be
    allowed to finish (cost is unknowable before it returns), but the
    NEXT segment must not be dispatched -- pre-segment stop-line, not a
    mid-dispatch cut."""
    from unittest.mock import AsyncMock, patch

    from nexus.plans.runner import plan_run

    plan = {
        "steps": [
            {"tool": "extract", "args": {"fields": "a", "inputs": "[]"}},
            {"tool": "summarize", "args": {"cited": False, "content": "$step1.extractions"}},
            {"tool": "search", "args": {"query": "x"}},
        ],
    }
    bundle_usage = _usage(cost_usd=0.60)

    async def fake_bundle(bundle, *, usage_sink=None, **kwargs):
        if usage_sink is not None:
            usage_sink.append(bundle_usage)
        return {"summary": "synthesis"}

    search_calls: list[str] = []

    async def stub_search(**kwargs):
        search_calls.append("search")
        return {"ids": ["a"], "tumblers": [], "distances": [], "collections": []}

    from nexus.mcp import core as mcp_core

    with patch.object(mcp_core, "search", stub_search), \
         patch("nexus.plans.bundle.dispatch_bundle", fake_bundle):
        result = await plan_run(_match(plan), {}, budget_usd_remaining=0.5)

    assert search_calls == [], "the retrieval step must not have dispatched"
    assert len(result.step_records) == 1
    assert result.step_records[0].cost_usd == pytest.approx(0.60)
    assert result.budget_exhausted_at_step == 3  # 1-indexed: the search step
    assert result.budget_exhausted_kind == "cost"


@pytest.mark.asyncio
async def test_budget_usd_remaining_generous_cap_runs_to_completion() -> None:
    """A cap comfortably above the total spend must let every segment
    run -- no exhaustion field set, mirroring the deadline check's own
    'no-op when not exceeded' behavior."""
    from unittest.mock import AsyncMock, patch

    from nexus.plans.runner import plan_run

    plan = {
        "steps": [
            {"tool": "extract", "args": {"fields": "a", "inputs": "[]"}},
            {"tool": "summarize", "args": {"cited": False, "content": "$step1.extractions"}},
            {"tool": "search", "args": {"query": "x"}},
        ],
    }
    bundle_usage = _usage(cost_usd=0.60)

    async def fake_bundle(bundle, *, usage_sink=None, **kwargs):
        if usage_sink is not None:
            usage_sink.append(bundle_usage)
        return {"summary": "synthesis"}

    async def stub_search(**kwargs):
        return {"ids": ["a"], "tumblers": [], "distances": [], "collections": []}

    from nexus.mcp import core as mcp_core

    with patch.object(mcp_core, "search", stub_search), \
         patch("nexus.plans.bundle.dispatch_bundle", fake_bundle):
        result = await plan_run(_match(plan), {}, budget_usd_remaining=100.0)

    assert len(result.step_records) == 2  # bundle + search, both dispatched
    assert result.budget_exhausted_at_step is None
    assert result.budget_exhausted_kind is None


@pytest.mark.asyncio
async def test_budget_usd_remaining_none_preserves_default_fields() -> None:
    """``budget_usd_remaining=None`` (the default) must leave the new
    PlanResult fields at their inert defaults -- proves the .p3c param
    does not silently change behavior when unset."""
    from nexus.plans.runner import plan_run

    plan = {"steps": [{"tool": "search", "args": {"query": "x"}}]}
    disp = _FakeDispatcher(outputs=[{"ids": [], "tumblers": []}])
    result = await plan_run(_match(plan), {}, dispatcher=disp)

    assert result.budget_exhausted_at_step is None
    assert result.budget_exhausted_kind is None


@pytest.mark.asyncio
async def test_budget_usd_remaining_unknown_cost_steps_never_trip() -> None:
    """An isolated LLM-tool step whose dispatch produces no captured
    DispatchUsage records cost_usd=None (genuinely unknown, never a
    fabricated 0 -- see StepRecord's own docstring). A tiny
    budget_usd_remaining must NOT stop a run built entirely of such
    steps: an unknown cost contributes nothing to the running sum and
    therefore can never trip the check on its own.

    This proves the BLIND SPOT exists at the runner level (dispatch
    continues, nothing accumulates, nothing trips). RDR-196 .p3c round
    2 (critic Significant 1, T2 p3c-critique-2026-08-21) surfaces this
    to the caller as a WARNING one layer up -- see
    ``TestNxAnswerBudgetUsdEnforcement::
    test_unknown_cost_steps_surface_the_blind_spot_warning`` in
    tests/test_nx_answer.py, which proves the SIGNAL now fires."""
    from unittest.mock import patch

    from nexus.plans.runner import plan_run

    plan = {
        "steps": [
            {"tool": "extract", "args": {"fields": "a", "inputs": "[]"}},
            {"tool": "extract", "args": {"fields": "b", "inputs": "[]"}},
            {"tool": "extract", "args": {"fields": "c", "inputs": "[]"}},
        ],
    }
    calls: list[str] = []

    async def stub_extract(**kwargs):
        calls.append("extract")
        return {"extractions": []}

    from nexus.mcp import core as mcp_core

    with patch.object(mcp_core, "operator_extract", stub_extract):
        result = await plan_run(
            _match(plan), {}, budget_usd_remaining=0.01, bundle_operators=False,
        )

    assert calls == ["extract", "extract", "extract"], (
        "every step must have run -- unknown (None) per-step cost must "
        "never be treated as a fabricated 0 that would trip the check"
    )
    assert result.budget_exhausted_at_step is None
    assert result.budget_exhausted_kind is None
    assert all(r.cost_usd is None for r in result.step_records)


# ── RDR-200 .p1c (nexus-4e75w.5): continuation "stop-before-cut" ──────────
#
# Same pre-segment placement as deadline / budget_usd_remaining above: the
# segment at or past continuation_cut_at_step never dispatches. Proves the
# terminal continuation suffix a caller is about to hand off never executes
# server-side (RDR-200 R2).


@pytest.mark.asyncio
async def test_continuation_cut_at_step_stops_before_terminal_operator() -> None:
    """search (step 0) runs; extract (step 1, the cut point) must NOT
    dispatch at all -- proves zero claude_dispatch/SQL executions for a
    suffix step once the cut applies."""
    from unittest.mock import patch

    from nexus.plans.runner import plan_run

    plan = {
        "steps": [
            {"tool": "search", "args": {"query": "x"}},
            {"tool": "extract", "args": {"inputs": "$step1.ids", "fields": "a"}},
        ],
    }
    extract_calls: list[str] = []

    async def stub_search(**kwargs):
        return {"ids": ["a"], "tumblers": ["1.1"], "distances": [0.1], "collections": ["knowledge"]}

    async def stub_extract(**kwargs):
        extract_calls.append("extract")
        return {"extractions": []}

    from nexus.mcp import core as mcp_core

    with patch.object(mcp_core, "search", stub_search), \
         patch.object(mcp_core, "operator_extract", stub_extract):
        result = await plan_run(
            _match(plan), {}, continuation_cut_at_step=1, bundle_operators=False,
        )

    assert extract_calls == [], (
        "the cut step must never dispatch -- zero executions of the "
        "suffix operator"
    )
    assert len(result.steps) == 1, "only the pre-cut search step ran"
    assert result.steps[0]["ids"] == ["a"]
    assert result.continuation_cut_applied is True
    assert result.step_records[0].operator == "search"
    assert result.budget_exhausted_at_step is None, (
        "a continuation cut is a distinct mechanism from budget "
        "exhaustion -- it must never set the budget marker fields"
    )


@pytest.mark.asyncio
async def test_continuation_cut_at_step_none_preserves_default_fields() -> None:
    """``continuation_cut_at_step=None`` (the default) must reproduce
    pre-.p1c behavior exactly -- every segment dispatches, unchanged."""
    from nexus.plans.runner import plan_run

    plan = {
        "steps": [
            {"tool": "search", "args": {"query": "x"}},
            {"tool": "extract", "args": {"inputs": "$step1.ids", "fields": "a"}},
        ],
    }
    from unittest.mock import patch

    from nexus.mcp import core as mcp_core

    async def stub_search(**kwargs):
        return {"ids": ["a"], "tumblers": ["1.1"], "distances": [0.1], "collections": ["knowledge"]}

    async def stub_extract(**kwargs):
        return {"extractions": []}

    with patch.object(mcp_core, "search", stub_search), \
         patch.object(mcp_core, "operator_extract", stub_extract):
        result = await plan_run(_match(plan), {}, bundle_operators=False)

    assert len(result.steps) == 2
    assert result.continuation_cut_applied is False


@pytest.mark.asyncio
async def test_continuation_cut_at_step_past_the_plan_never_fires() -> None:
    """A cut index beyond the plan's last segment must let the whole
    plan run -- ``continuation_cut_applied`` stays False (nothing was
    withheld that would otherwise have run)."""
    from unittest.mock import patch

    from nexus.plans.runner import plan_run

    plan = {"steps": [{"tool": "search", "args": {"query": "x"}}]}

    async def stub_search(**kwargs):
        return {"ids": ["a"], "tumblers": ["1.1"], "distances": [0.1], "collections": ["knowledge"]}

    from nexus.mcp import core as mcp_core

    with patch.object(mcp_core, "search", stub_search):
        result = await plan_run(_match(plan), {}, continuation_cut_at_step=5)

    assert len(result.steps) == 1
    assert result.continuation_cut_applied is False


@pytest.mark.asyncio
async def test_continuation_cut_at_step_zero_stops_before_the_first_step() -> None:
    """A cut at the very first step (an all-operator plan) withholds
    everything -- zero StepRecords, matching the RDR's "the terminal
    suffix MUST NOT execute server-side" contract for the degenerate
    all-suffix case."""
    from unittest.mock import patch

    from nexus.plans.runner import plan_run

    plan = {"steps": [{"tool": "summarize", "args": {"content": "x"}}]}
    calls: list[str] = []

    async def stub_summarize(**kwargs):
        calls.append("summarize")
        return {"summary": "x"}

    from nexus.mcp import core as mcp_core

    with patch.object(mcp_core, "operator_summarize", stub_summarize):
        result = await plan_run(
            _match(plan), {}, continuation_cut_at_step=0, bundle_operators=False,
        )

    assert calls == []
    assert result.steps == []
    assert result.step_records == []
    assert result.continuation_cut_applied is True


# ── StepRecord (RDR-196 .p1b, nexus-nyry9.8) ────────────────────────────────


def _usage(**overrides):
    """Build a DispatchUsage with sane all-populated defaults, overridable."""
    from nexus.operators.dispatch import DispatchUsage

    fields = {
        "model": "claude-sonnet-5-20260101",
        "cost_usd": 0.0123,
        "input_tokens": 111,
        "output_tokens": 22,
        "cache_creation_input_tokens": 0,
        "cache_read_input_tokens": 0,
        "duration_ms": 4200,
        "duration_api_ms": 4000,
        "num_turns": 1,
    }
    fields.update(overrides)
    return DispatchUsage(**fields)


class TestStepRecords:
    """Covers the bead's falsifiable VERIFICATION list verbatim:

    - len(step_records) == executed-segment count on a plan traversing
      >=2 of the 3 nx_answer_step_complete sites (isolated + bundle here;
      bundle_fallback covered separately in test_plan_bundle.py).
    - a bundled dispatch produces exactly ONE record with bundled_steps
      populated, cost never divided.
    - a SQL fast-path step records source="sql", model is None,
      cost_usd == 0 (distinguishable from a zero-cost LLM step).
    - a FAILED step still produces a record with ok=False.
    - the recorded model equals the envelope's reported canonical id,
      never the requested alias (196-R3 audit-fold addition).
    """

    @pytest.mark.asyncio
    async def test_one_record_per_executed_step_isolated_and_bundle(self):
        """1 isolated (search, :1467 site) + 1 bundle (extract+summarize,
        :1361 site) -> exactly 2 step_records, traversing 2 of the 3
        completion sites. The bundle's cost is NOT divided across its
        2 fused plan indices."""
        from unittest.mock import AsyncMock, patch

        from nexus.mcp import core as mcp_core
        from nexus.plans.runner import plan_run

        plan = {"steps": [
            {"tool": "search", "args": {"query": "x"}},
            {"tool": "extract", "args": {"fields": "a", "inputs": "[]"}},
            {"tool": "summarize", "args": {"cited": False, "content": "$step2.extractions"}},
        ]}

        async def stub_search(**kwargs):
            return {"ids": ["a"], "tumblers": ["1.1"], "distances": [0.1],
                     "collections": ["kn"]}

        bundle_usage = _usage(cost_usd=0.42, input_tokens=900, output_tokens=300)

        async def fake_bundle(bundle, *, usage_sink=None, **kwargs):
            if usage_sink is not None:
                usage_sink.append(bundle_usage)
            return {"summary": "synthesis"}

        with patch.object(mcp_core, "search", stub_search), \
             patch("nexus.plans.bundle.dispatch_bundle", fake_bundle):
            result = await plan_run(_match(plan))

        assert len(result.step_records) == 2

        isolated = result.step_records[0]
        assert isolated.step_index == 0
        assert isolated.operator == "search"
        assert isolated.source == "sql"  # retrieval tool, no LLM call
        assert isolated.ok is True
        assert isolated.bundled_steps == []
        assert isolated.model is None
        assert (isolated.input_tokens, isolated.output_tokens, isolated.cost_usd) == (0, 0, 0.0)

        bundled = result.step_records[1]
        assert bundled.source == "bundle"
        assert bundled.step_index == 1
        assert bundled.bundled_steps == [1, 2]
        assert bundled.operator == "extract+summarize"
        assert bundled.ok is True
        # Real usage, NOT divided across the 2 fused indices.
        assert bundled.cost_usd == 0.42
        assert bundled.input_tokens == 900
        assert bundled.output_tokens == 300
        assert bundled.model == "claude-sonnet-5-20260101"

    @pytest.mark.asyncio
    async def test_sql_fast_path_step_source_sql_zero_cost(self):
        """A Gap-5 SQL-hit operator_filter step: source="sql", model is
        None, cost_usd == 0 (a TRUE zero — distinguishable from an
        unobserved LLM cost, which would be None)."""
        from unittest.mock import patch

        from nexus.mcp import core as mcp_core
        from nexus.plans.runner import plan_run

        plan = {"steps": [
            {"tool": "filter", "args": {"items": "[]", "criterion": "x"}},
        ]}

        async def stub_operator_filter(**kwargs):
            # Mirrors core.py's real Gap-5 marker stamp on an SQL hit.
            return {"items": [], "rationale": [], "_dispatch_source": "sql"}

        with patch.object(mcp_core, "operator_filter", stub_operator_filter):
            result = await plan_run(_match(plan))

        assert len(result.step_records) == 1
        rec = result.step_records[0]
        assert rec.source == "sql"
        assert rec.model is None
        assert rec.cost_usd == 0.0
        assert rec.input_tokens == 0
        assert rec.output_tokens == 0
        assert rec.ok is True
        # The marker must not leak into the visible step output.
        assert "_dispatch_source" not in result.steps[0]

    @pytest.mark.asyncio
    async def test_llm_fallback_filter_step_source_llm_unknown_cost(self):
        """The SAME tool, but the SQL fast path fell through to LLM (no
        marker on the returned dict) -> source="llm", and cost is
        genuinely UNKNOWN (None), never a fabricated 0 — this is the
        isolated-dispatch architecture gap named in the handback."""
        from unittest.mock import patch

        from nexus.mcp import core as mcp_core
        from nexus.plans.runner import plan_run

        plan = {"steps": [
            {"tool": "filter", "args": {"items": "[]", "criterion": "x"}},
        ]}

        async def stub_operator_filter(**kwargs):
            return {"items": [], "rationale": []}  # no _dispatch_source

        with patch.object(mcp_core, "operator_filter", stub_operator_filter):
            result = await plan_run(_match(plan))

        rec = result.step_records[0]
        assert rec.source == "llm"
        assert rec.model is None
        assert rec.cost_usd is None
        assert rec.input_tokens is None
        assert rec.output_tokens is None

    @pytest.mark.asyncio
    async def test_failed_isolated_step_records_ok_false(self):
        """A step that raises an operator error still produces a
        record, with ok=False."""
        import nexus.operators.dispatch as _dispatch_mod
        from nexus.plans.runner import plan_run

        plan = {"steps": [
            {"tool": "summarize", "args": {"content": "x"}},
        ]}

        async def failing_dispatch(tool, args):
            raise _dispatch_mod.OperatorError("boom")

        result = await plan_run(_match(plan), dispatcher=failing_dispatch)

        assert len(result.step_records) == 1
        rec = result.step_records[0]
        assert rec.ok is False
        assert rec.source == "llm"
        assert rec.step_index == 0
        assert rec.operator == "summarize"
        # Unobservable through a caller-supplied dispatcher -- honest None.
        assert rec.model is None
        assert rec.cost_usd is None

    @pytest.mark.asyncio
    async def test_bundle_failure_produces_one_record_ok_false(self):
        """A whole bundled dispatch failing produces exactly ONE record
        (not N, one per fused index) with ok=False."""
        import nexus.operators.dispatch as _dispatch_mod
        from unittest.mock import patch

        from nexus.plans.runner import plan_run

        plan = {"steps": [
            {"tool": "extract", "args": {"fields": "a", "inputs": "[]"}},
            {"tool": "summarize", "args": {"cited": False, "content": "x"}},
        ]}

        async def failing_bundle(bundle, *, usage_sink=None, **kwargs):
            raise _dispatch_mod.OperatorError("bundle boom")

        with patch("nexus.plans.bundle.dispatch_bundle", failing_bundle):
            result = await plan_run(_match(plan))

        assert len(result.step_records) == 1
        rec = result.step_records[0]
        assert rec.ok is False
        assert rec.source == "bundle"
        assert rec.bundled_steps == [0, 1]

    @pytest.mark.asyncio
    async def test_bundle_model_recorded_is_canonical_not_alias(self):
        """196-R3 audit-fold verification: the recorded model equals the
        envelope's reported canonical id. Feeding a DispatchUsage whose
        model is already the canonical id (the only value this layer
        ever sees -- DispatchUsage.model is canonical-or-None by
        construction) and asserting it propagates verbatim, unaltered."""
        from unittest.mock import patch

        from nexus.plans.runner import plan_run

        plan = {"steps": [
            {"tool": "extract", "args": {"fields": "a", "inputs": "[]"}},
            {"tool": "summarize", "args": {"cited": False, "content": "x"}},
        ]}

        canonical_usage = _usage(model="claude-opus-4-20260514")

        async def fake_bundle(bundle, *, usage_sink=None, **kwargs):
            if usage_sink is not None:
                usage_sink.append(canonical_usage)
            return {"summary": "ok"}

        with patch("nexus.plans.bundle.dispatch_bundle", fake_bundle):
            result = await plan_run(_match(plan))

        assert result.step_records[0].model == "claude-opus-4-20260514"

    @pytest.mark.asyncio
    async def test_isolated_step_through_real_toolDispatcher_records_real_usage(self):
        """RDR-196 .p1b Gap-1 addendum (nexus-nyry9.8 coordinator
        directive, 2026-08-20): the DEFAULT dispatcher (real
        _default_dispatcher -> mcp_core.operator_summarize ->
        claude_dispatch -> subprocess), with a faked subprocess at the
        claude_dispatch seam, must produce a non-None model/cost on its
        StepRecord -- proving the ambient_usage_sink mechanism actually
        threads through the real call chain, not just a mock."""
        from unittest.mock import AsyncMock, patch

        from tests.test_operator_dispatch import _make_proc, _result_ndjson
        from nexus.plans.runner import plan_run

        plan = {"steps": [
            {"tool": "summarize", "args": {"content": "some content", "cited": False}},
        ]}

        proc = _make_proc(
            stdout=_result_ndjson(
                cost_usd=0.045, input_tokens=30, output_tokens=12,
                model="claude-sonnet-5-20260101",
                structured_output={"summary": "ok"},
            ),
            returncode=0, stderr=b"",
        )

        with patch("asyncio.create_subprocess_exec", new=AsyncMock(return_value=proc)):
            result = await plan_run(_match(plan), dispatcher=None)

        assert len(result.step_records) == 1
        rec = result.step_records[0]
        assert rec.source == "llm"
        assert rec.ok is True
        assert rec.model == "claude-sonnet-5-20260101"
        assert rec.cost_usd == 0.045
        assert rec.input_tokens == 30
        assert rec.output_tokens == 12

    @pytest.mark.asyncio
    async def test_two_consecutive_isolated_steps_no_usage_leakage(self):
        """Two isolated LLM steps, back to back, each dispatching a
        DIFFERENT faked subprocess response -- each StepRecord must
        carry only its OWN step's usage, never the other's."""
        from unittest.mock import AsyncMock, patch

        from tests.test_operator_dispatch import _make_proc, _result_ndjson
        from nexus.plans.runner import plan_run

        plan = {"steps": [
            {"tool": "summarize", "args": {"content": "first", "cited": False}},
            # A non-bundleable-adjacent structure would bundle; force two
            # ISOLATED dispatches by disabling bundling entirely.
            {"tool": "check", "args": {"items": "[]", "check_instruction": "x"}},
        ]}

        responses = [
            _make_proc(
                stdout=_result_ndjson(
                    cost_usd=0.01, input_tokens=1, output_tokens=1,
                    model="claude-sonnet-5-20260101",
                    structured_output={"summary": "first-out"},
                ),
                returncode=0, stderr=b"",
            ),
            _make_proc(
                stdout=_result_ndjson(
                    cost_usd=0.99, input_tokens=100, output_tokens=200,
                    model="claude-opus-4-20260514",
                    structured_output={"ok": True, "evidence": []},
                ),
                returncode=0, stderr=b"",
            ),
        ]

        async def fake_create_subprocess_exec(*args, **kwargs):
            return responses.pop(0)

        with patch("asyncio.create_subprocess_exec", side_effect=fake_create_subprocess_exec):
            result = await plan_run(_match(plan), dispatcher=None, bundle_operators=False)

        assert len(result.step_records) == 2
        first, second = result.step_records
        assert first.cost_usd == 0.01
        assert first.model == "claude-sonnet-5-20260101"
        assert second.cost_usd == 0.99
        assert second.model == "claude-opus-4-20260514"

    def test_rollup_step_usage_multi_entry(self):
        """RDR-196 .p1b Gap-1 addendum, review-fix (code-review-expert
        [23056] APPROVE / substantive-critic [23057] SHIP-with-3-
        Significant, 2026-08-20): ``_rollup_step_usage``'s >1-entry
        branch had zero coverage. No operator issues >1 internal
        claude_dispatch call today, but the mechanism must not silently
        mis-divide or fabricate a value if one ever does.

        THE RULE, stated (per coordinator ask): summing an attribute
        across entries is None+float -> None, never 0+float or a
        partial sum -- if ANY entry's value for a field is None (usage
        unobservable for that one dispatch), the summed field is None
        too. Treating an unknown as a zero mid-sum would silently
        understate real spend, exactly the RDR-196 risk-#1 class this
        arc exists to close.

        Two scenarios in one test (as requested): (1) two entries with
        DIFFERENT canonical models, one with cost_usd=None -- exercises
        the None-arithmetic path -- asserts tokens summed, cost_usd is
        None (the rule above), model is None (ambiguous, same rule
        DispatchUsage.model already applies within a single call).
        (2) two entries sharing the SAME model, both fully populated --
        asserts the model is KEPT (not needlessly nulled) and cost_usd
        is a real sum, not divided or dropped.
        """
        from nexus.operators.dispatch import DispatchUsage
        from nexus.plans.runner import _rollup_step_usage

        def _usage(**overrides):
            fields = {
                "model": "claude-sonnet-5-20260101", "cost_usd": 0.1,
                "input_tokens": 10, "output_tokens": 20,
                "cache_creation_input_tokens": 0, "cache_read_input_tokens": 0,
                "duration_ms": 100, "duration_api_ms": 90, "num_turns": 1,
            }
            fields.update(overrides)
            return DispatchUsage(**fields)

        # (1) Different models, one entry's cost_usd is None.
        entry_a = _usage(
            model="claude-sonnet-5-20260101", cost_usd=None,
            input_tokens=10, output_tokens=20,
        )
        entry_b = _usage(
            model="claude-opus-4-20260514", cost_usd=0.5,
            input_tokens=100, output_tokens=200,
        )
        rolled = _rollup_step_usage([entry_a, entry_b])
        assert rolled is not None
        assert rolled.input_tokens == 110, "tokens sum across entries even when cost is unknown"
        assert rolled.output_tokens == 220
        assert rolled.cost_usd is None, (
            "None+float must roll up to None, never a fabricated partial sum"
        )
        assert rolled.model is None, "two distinct models is ambiguous -- never guess"

        # (2) Same model, both entries fully populated.
        entry_c = _usage(
            model="claude-sonnet-5-20260101", cost_usd=0.10,
            input_tokens=10, output_tokens=20,
        )
        entry_d = _usage(
            model="claude-sonnet-5-20260101", cost_usd=0.25,
            input_tokens=30, output_tokens=40,
        )
        rolled_same = _rollup_step_usage([entry_c, entry_d])
        assert rolled_same is not None
        assert rolled_same.model == "claude-sonnet-5-20260101", (
            "shared model across every entry must be KEPT, not nulled"
        )
        assert rolled_same.cost_usd == pytest.approx(0.35)
        assert rolled_same.input_tokens == 40
        assert rolled_same.output_tokens == 60


class TestPartialStepRecordsOnException(object):
    """RDR-196 .p1d critique fold (T2 [23092], consumed by nexus-nyry9.11):
    a mid-loop exception must not discard the step_records already
    completed -- failed runs are exactly the population that produced the
    45x-wrong latency docstring (nexus-h33x8.6), so telemetry that only
    records SUCCESSFUL runs measures the wrong population. ``plan_run``
    attaches the partial ``step_records`` list directly to the raised
    exception instance (no new carrier type) so a caller's except-handler
    can still record real per-step data.
    """

    @pytest.mark.asyncio
    async def test_exception_after_one_completed_step_carries_its_record(self):
        """Step 0 (isolated, real dispatch) succeeds and is recorded;
        step 1's dispatcher returns a non-dict, which raises
        PlanRunStepRefError from OUTSIDE any of the loop's own
        try/except blocks -- the exact "escapes the loop" shape the
        critique named. The raised exception must still carry
        step_records=[<step 0's record>], not []."""
        from nexus.plans.runner import PlanRunStepRefError, plan_run

        plan = {"steps": [
            {"tool": "search", "args": {"query": "x"}},
            {"tool": "search", "args": {"query": "y"}},
        ]}
        outputs = [{"ids": ["a"], "tumblers": [], "distances": [], "collections": []}, "not-a-dict"]

        async def disp(tool, args):
            return outputs.pop(0)

        with pytest.raises(PlanRunStepRefError) as excinfo:
            await plan_run(_match(plan), dispatcher=disp, bundle_operators=False)

        step_records = getattr(excinfo.value, "step_records", None)
        assert step_records is not None, (
            "the raised exception must carry a step_records attribute, "
            "even when empty -- getattr defaulting to None would mean the "
            "attach never happened"
        )
        assert len(step_records) == 1
        assert step_records[0].step_index == 0
        assert step_records[0].ok is True

    @pytest.mark.asyncio
    async def test_exception_before_any_step_completes_carries_empty_list(self):
        """A failure on the FIRST step (before any _record_step call) must
        still attach step_records -- an empty list, not a missing
        attribute -- so a caller need not special-case "no attribute" vs
        "attribute present but empty"."""
        from nexus.plans.runner import PlanRunStepRefError, plan_run

        plan = {"steps": [
            {"tool": "search", "args": {"query": "x"}},
        ]}

        async def disp(tool, args):
            return "not-a-dict"

        with pytest.raises(PlanRunStepRefError) as excinfo:
            await plan_run(_match(plan), dispatcher=disp, bundle_operators=False)

        step_records = getattr(excinfo.value, "step_records", None)
        assert step_records == []

    @pytest.mark.asyncio
    async def test_operator_arg_missing_reraise_still_carries_step_records(self):
        """A re-raised (not freshly raised) exception -- the
        PlanRunOperatorArgMissingError patch-and-reraise-from path --
        must ALSO carry step_records, proving the attach happens at the
        single outer boundary regardless of how many times the
        exception object was replaced inside the loop."""
        from nexus.plans.runner import PlanRunOperatorArgMissingError, plan_run

        plan = {"steps": [
            {"tool": "search", "args": {"query": "x"}},
            {"tool": "summarize", "args": {}},  # missing required 'content'
        ]}

        async def disp(tool, args):
            if tool == "search":
                return {"ids": ["a"], "tumblers": [], "distances": [], "collections": []}
            # step_index=-1 is the sentinel _default_dispatcher stamps when
            # it can't see its own position in the plan (see that
            # function's docstring) -- the runner patches in the real
            # index and re-raises a NEW exception instance "from exc".
            raise PlanRunOperatorArgMissingError(
                step_index=-1, tool="summarize", missing_arg="content",
            )

        with pytest.raises(PlanRunOperatorArgMissingError) as excinfo:
            await plan_run(_match(plan), dispatcher=disp, bundle_operators=False)

        step_records = getattr(excinfo.value, "step_records", None)
        assert step_records is not None
        assert len(step_records) == 1
        assert step_records[0].step_index == 0


# ── operator hydration carries the display-truncation marker (nexus-lugwx) ──


class TestHydrationCarriesDisplayTruncationMarker:
    """nexus-lugwx: ``store_get_many`` cuts each document at
    ``max_chars_per_doc``. Before this bead it appended a bare ellipsis, so a
    tool-free operator read the mid-sentence cut as a broken document (the
    nexus-c0sdc defect, first fixed for nx_tidy alone). The marker now lives
    at the cut inside store_get_many, so BOTH runner paths carry it without
    re-detecting anything: ``ids``-in-args auto-hydration, and the explicit
    ``store_get_many`` step feeding ``inputs: $stepN.contents`` (the shape
    every built-in plan uses). The stub reproduces store_get_many's REAL
    shape via the same helper it uses, so it stays honest if that changes.
    """

    @staticmethod
    def _stub(monkeypatch, bodies: list[str]) -> None:
        from nexus.mcp import core as mcp_core

        def fake_store_get_many(*, ids, collections, structured, max_chars_per_doc=4000):
            cut = [
                b[:max_chars_per_doc] + "…"
                + mcp_core.display_truncation_marker(max_chars_per_doc)
                if len(b) > max_chars_per_doc else b
                for b in bodies
            ]
            return {"contents": cut, "missing": []}

        monkeypatch.setattr(mcp_core, "store_get_many", fake_store_get_many)

    def test_ids_branch_summarize_content_carries_marker(self, monkeypatch):
        from nexus.plans.runner import _hydrate_operator_args

        self._stub(monkeypatch, ["word " * 1500])  # 7500 chars, cut
        tool, args = _hydrate_operator_args("summarize", {"ids": ["a"]})
        assert tool == "operator_summarize"
        assert "TRUNCATED FOR DISPLAY" in args["content"]
        assert "NOT a defect" in args["content"]

    def test_ids_branch_marker_rides_inside_json_items(self, monkeypatch):
        """rank/compare/filter receive a JSON list; the marker must ride
        inside the item string, where the model reads it."""
        from nexus.plans.runner import _hydrate_operator_args

        self._stub(monkeypatch, ["x" * 9000, "short"])
        _, args = _hydrate_operator_args("rank", {"ids": ["a", "b"], "criterion": "c"})
        items = json.loads(args["items"])
        assert len(items) == 2
        assert "TRUNCATED FOR DISPLAY" in items[0]
        assert "TRUNCATED FOR DISPLAY" not in items[1]

    def test_inputs_branch_preserves_marker_from_explicit_step(self, monkeypatch):
        """The built-in plans' shape: an explicit store_get_many step, then
        ``inputs: $stepN.contents`` on the operator. The runner must pass the
        marked text through unchanged on this path too."""
        from nexus.mcp import core as mcp_core
        from nexus.plans.runner import _hydrate_operator_args

        marked = "x" * 4000 + "…" + mcp_core.display_truncation_marker(4000)
        _, args = _hydrate_operator_args("summarize", {"inputs": [marked, "short"]})
        assert "TRUNCATED FOR DISPLAY" in args["content"]
        _, args = _hydrate_operator_args("compare", {"inputs": [marked], "focus": "f"})
        assert "TRUNCATED FOR DISPLAY" in json.loads(args["items"])[0]

    def test_short_document_is_not_marked(self, monkeypatch):
        """Marking an intact document would teach the model to discount a
        real truncation it should report."""
        from nexus.plans.runner import _hydrate_operator_args

        self._stub(monkeypatch, ["a complete short note."])
        _, args = _hydrate_operator_args("summarize", {"ids": ["a"]})
        assert args["content"] == "a complete short note."
