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
    """Records every dispatch call and returns scripted outputs."""

    def __init__(self, outputs: list[dict] | None = None) -> None:
        self.calls: list[tuple[str, dict]] = []
        self._outputs = list(outputs or [])

    def __call__(self, tool: str, args: dict) -> dict:
        self.calls.append((tool, args))
        if self._outputs:
            return self._outputs.pop(0)
        return {"text": f"{tool}(stub)"}


# ── Variable resolution ────────────────────────────────────────────────────


def test_run_resolves_caller_var_in_args() -> None:
    from nexus.plans.runner import plan_run

    plan = {
        "steps": [
            {"tool": "search", "args": {"query": "$intent", "limit": 5}},
        ],
        "required_bindings": ["intent"],
    }
    disp = _FakeDispatcher([{"text": "ok"}])
    plan_run(_match(plan), {"intent": "how does X work"}, dispatcher=disp)

    assert disp.calls[0] == (
        "search", {"query": "how does X work", "limit": 5},
    )


def test_run_caller_binding_overrides_default() -> None:
    """default_bindings + caller_bindings: caller wins on conflict."""
    from nexus.plans.runner import plan_run

    plan = {
        "steps": [{"tool": "search", "args": {"query": "$intent"}}],
        "required_bindings": ["intent"],
    }
    match = _match(plan, default_bindings={"intent": "default"})
    disp = _FakeDispatcher()
    plan_run(match, {"intent": "caller"}, dispatcher=disp)
    assert disp.calls[0][1] == {"query": "caller"}


def test_run_falls_back_to_default_when_caller_omits() -> None:
    from nexus.plans.runner import plan_run

    plan = {
        "steps": [{"tool": "search", "args": {"query": "$intent"}}],
        "required_bindings": ["intent"],
    }
    match = _match(plan, default_bindings={"intent": "from-default"})
    disp = _FakeDispatcher()
    plan_run(match, {}, dispatcher=disp)
    assert disp.calls[0][1] == {"query": "from-default"}


def test_run_rejects_missing_required_binding() -> None:
    from nexus.plans.runner import PlanRunBindingError, plan_run

    plan = {
        "steps": [{"tool": "search", "args": {"query": "$intent"}}],
        "required_bindings": ["intent", "subtree"],
    }
    with pytest.raises(PlanRunBindingError) as exc:
        plan_run(_match(plan), {"intent": "x"}, dispatcher=_FakeDispatcher())
    assert sorted(exc.value.missing) == ["subtree"]


# ── $stepN.<field> reference ───────────────────────────────────────────────


def test_run_resolves_step_ref_to_prior_output_field() -> None:
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
    plan_run(_match(plan), {}, dispatcher=disp)
    assert disp.calls[1] == ("summarize", {"corpus": "first-result"})


def test_run_resolves_step_ref_for_list_field() -> None:
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
    plan_run(_match(plan), {}, dispatcher=disp)
    assert disp.calls[1] == ("extract", {"ids": ["a", "b", "c"]})


def test_run_step_ref_unknown_field_raises() -> None:
    from nexus.plans.runner import PlanRunStepRefError, plan_run

    plan = {
        "steps": [
            {"tool": "search", "args": {"query": "x"}},
            {"tool": "rank", "args": {"by": "$step1.bogus_field"}},
        ],
    }
    disp = _FakeDispatcher([{"text": "first", "tumblers": []}])
    with pytest.raises(PlanRunStepRefError) as exc:
        plan_run(_match(plan), {}, dispatcher=disp)
    assert "$step1.bogus_field" in str(exc.value)


def test_run_step_ref_to_missing_step_raises() -> None:
    from nexus.plans.runner import PlanRunStepRefError, plan_run

    plan = {
        "steps": [
            {"tool": "search", "args": {"query": "$step5.text"}},
        ],
    }
    with pytest.raises(PlanRunStepRefError):
        plan_run(_match(plan), {}, dispatcher=_FakeDispatcher())


# ── Cross-embedding guard (SC-10) ──────────────────────────────────────────


def test_run_rejects_cross_embedding_dispatch() -> None:
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
        plan_run(_match(plan), {}, dispatcher=_FakeDispatcher())
    msg = str(exc.value)
    assert "code" in msg
    assert "docs__corpus" in msg


def test_run_allows_matching_taxonomy_domain() -> None:
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
    plan_run(_match(plan), {}, dispatcher=disp)
    assert disp.calls[0][0] == "search"


def test_run_allows_step_without_scope() -> None:
    """No ``scope`` declared → no embedding-domain check."""
    from nexus.plans.runner import plan_run

    plan = {
        "steps": [
            {"tool": "search", "args": {"query": "x", "collection": "any__name"}},
        ],
    }
    plan_run(_match(plan), {}, dispatcher=_FakeDispatcher([{"text": "ok"}]))


def test_run_traverse_step_skips_embedding_check() -> None:
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
    plan_run(_match(plan), {}, dispatcher=disp)


# ── Result + step trace ────────────────────────────────────────────────────


def test_run_returns_result_with_step_outputs() -> None:
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
    result = plan_run(_match(plan), {}, dispatcher=disp)
    assert result.steps[0]["text"] == "search-output"
    assert result.steps[1]["text"] == "summary-output"
    assert result.final == result.steps[1]


def test_run_with_empty_steps_returns_empty_result() -> None:
    from nexus.plans.runner import plan_run

    result = plan_run(
        _match({"steps": []}), {}, dispatcher=_FakeDispatcher(),
    )
    assert result.steps == []
    assert result.final is None


def test_run_passes_through_static_args_unchanged() -> None:
    """Args without any ``$`` substitution pass through untouched."""
    from nexus.plans.runner import plan_run

    plan = {"steps": [{"tool": "x", "args": {"limit": 10, "flag": True}}]}
    disp = _FakeDispatcher()
    plan_run(_match(plan), {}, dispatcher=disp)
    assert disp.calls[0][1] == {"limit": 10, "flag": True}
