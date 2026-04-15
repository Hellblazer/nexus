# SPDX-License-Identifier: AGPL-3.0-or-later
"""TDD tests for scope-domain forwarding in the plan runner.

RDR-078 P2 (nexus-05i.3). Builds on the P1 runner: when a step has
``scope.taxonomy_domain`` and/or ``scope.topic`` set, the runner
populates the dispatched tool's ``corpus`` / ``topic`` args
accordingly so the call lands in the right embedding space and (when
asked) only against the named topic.

Covers SC-3 (domain-scoped retrieval) and re-pins SC-10
(cross-embedding boundary not crossed) at this level of the stack.
"""
from __future__ import annotations

import json

import pytest


def _match(plan: dict) -> "Match":  # noqa: F821
    from nexus.plans.match import Match

    return Match(
        plan_id=1, name="default", description="t", confidence=0.9,
        dimensions={}, tags="", plan_json=json.dumps(plan),
        required_bindings=list(plan.get("required_bindings", []) or []),
        optional_bindings=[], default_bindings={}, parent_dims=None,
    )


class _FakeDispatcher:
    def __init__(self, outputs: list[dict] | None = None) -> None:
        self.calls: list[tuple[str, dict]] = []
        self._outputs = list(outputs or [])

    def __call__(self, tool: str, args: dict) -> dict:
        self.calls.append((tool, args))
        if self._outputs:
            return self._outputs.pop(0)
        return {"text": f"{tool}-stub", "ids": []}


# ── SC-3: taxonomy_domain → corpus routing ──────────────────────────────────


@pytest.mark.asyncio
async def test_scope_prose_routes_to_prose_corpora() -> None:
    """``scope.taxonomy_domain=prose`` injects a prose corpus prefix
    set when the step args don't already pin a collection."""
    from nexus.plans.runner import plan_run

    plan = {
        "steps": [
            {
                "tool": "search",
                "args": {"query": "x"},
                "scope": {"taxonomy_domain": "prose"},
            },
        ],
    }
    disp = _FakeDispatcher()
    await plan_run(_match(plan), {}, dispatcher=disp)

    sent = disp.calls[0][1]
    assert "corpus" in sent, "prose scope should populate corpus arg"
    parts = {p.strip() for p in sent["corpus"].split(",") if p.strip()}
    assert parts == {"knowledge", "docs", "rdr", "paper"}


@pytest.mark.asyncio
async def test_scope_code_routes_to_code_corpora() -> None:
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
    disp = _FakeDispatcher()
    await plan_run(_match(plan), {}, dispatcher=disp)

    sent = disp.calls[0][1]
    assert sent["corpus"] == "code"


@pytest.mark.asyncio
async def test_explicit_corpus_arg_wins_over_scope_default() -> None:
    """A caller-pinned ``args.corpus`` is not overridden by scope
    domain; the SC-10 guard already ensures consistency."""
    from nexus.plans.runner import plan_run

    plan = {
        "steps": [
            {
                "tool": "search",
                "args": {"query": "x", "corpus": "knowledge"},
                "scope": {"taxonomy_domain": "prose"},
            },
        ],
    }
    disp = _FakeDispatcher()
    await plan_run(_match(plan), {}, dispatcher=disp)
    assert disp.calls[0][1]["corpus"] == "knowledge"


@pytest.mark.asyncio
async def test_explicit_collection_arg_skips_corpus_injection() -> None:
    """When the args already pin ``collection`` or ``collections``,
    the scope domain doesn't add a redundant ``corpus`` field."""
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
    disp = _FakeDispatcher()
    await plan_run(_match(plan), {}, dispatcher=disp)

    sent = disp.calls[0][1]
    assert sent["collection"] == "code__myrepo"
    assert "corpus" not in sent


@pytest.mark.asyncio
async def test_scope_unknown_domain_raises() -> None:
    """Unknown ``taxonomy_domain`` is rejected at the runtime guard
    (SC-10 from P1) — re-validated here as a regression pin."""
    from nexus.plans.runner import PlanRunEmbeddingDomainError, plan_run

    plan = {
        "steps": [
            {
                "tool": "search",
                "args": {"query": "x"},
                "scope": {"taxonomy_domain": "binary"},
            },
        ],
    }
    with pytest.raises(PlanRunEmbeddingDomainError):
        await plan_run(_match(plan), {}, dispatcher=_FakeDispatcher())


# ── SC-3: topic forwarding ─────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_topic_filter_applied() -> None:
    """``scope.topic`` is forwarded as ``args.topic`` to the dispatched tool."""
    from nexus.plans.runner import plan_run

    plan = {
        "steps": [
            {
                "tool": "search",
                "args": {"query": "x"},
                "scope": {"taxonomy_domain": "prose", "topic": "Projection Quality"},
            },
        ],
    }
    disp = _FakeDispatcher()
    await plan_run(_match(plan), {}, dispatcher=disp)
    assert disp.calls[0][1]["topic"] == "Projection Quality"


@pytest.mark.asyncio
async def test_topic_resolves_var_substitution() -> None:
    """``scope.topic: $concept`` resolves via the binding map."""
    from nexus.plans.runner import plan_run

    plan = {
        "steps": [
            {
                "tool": "search",
                "args": {"query": "x"},
                "scope": {"taxonomy_domain": "prose", "topic": "$concept"},
            },
        ],
        "required_bindings": ["concept"],
    }
    disp = _FakeDispatcher()
    await plan_run(_match(plan), {"concept": "Hub Suppression"}, dispatcher=disp)
    assert disp.calls[0][1]["topic"] == "Hub Suppression"


@pytest.mark.asyncio
async def test_explicit_topic_arg_not_overridden() -> None:
    """Caller-supplied ``args.topic`` wins over ``scope.topic``."""
    from nexus.plans.runner import plan_run

    plan = {
        "steps": [
            {
                "tool": "search",
                "args": {"query": "x", "topic": "Caller Topic"},
                "scope": {"taxonomy_domain": "prose", "topic": "Scope Topic"},
            },
        ],
    }
    disp = _FakeDispatcher()
    await plan_run(_match(plan), {}, dispatcher=disp)
    assert disp.calls[0][1]["topic"] == "Caller Topic"


# ── SC-10: cross-embedding boundary still enforced ─────────────────────────


@pytest.mark.asyncio
async def test_no_cross_embedding_cosine_computed() -> None:
    """The SC-10 guard from P1 fires before any cross-embedding
    dispatch, regardless of whether scope was injecting corpus or not.

    This is the runtime guard — pinned again here so a P2 regression
    that bypasses :func:`_check_embedding_domain` would surface
    immediately.
    """
    from nexus.plans.runner import PlanRunEmbeddingDomainError, plan_run

    plan = {
        "steps": [
            {
                "tool": "search",
                "args": {"query": "x", "collection": "knowledge__delos"},
                "scope": {"taxonomy_domain": "code"},
            },
        ],
    }
    with pytest.raises(PlanRunEmbeddingDomainError):
        await plan_run(_match(plan), {}, dispatcher=_FakeDispatcher())


@pytest.mark.asyncio
async def test_traverse_step_unaffected_by_scope_corpus_injection() -> None:
    """``traverse`` operates on tumblers; scope.taxonomy_domain
    must not inject ``corpus`` into its args."""
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
    sent = disp.calls[0][1]
    assert "corpus" not in sent
