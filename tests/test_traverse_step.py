# SPDX-License-Identifier: AGPL-3.0-or-later
"""TDD tests for the ``traverse`` step in the plan runner — RDR-078 P3.

The runner dispatches ``tool: traverse`` through the standard
ToolDispatcher path. The ``traverse`` MCP tool itself
(``nexus.mcp.core.traverse``) resolves seeds, picks the link types
(from explicit ``link_types`` OR via ``purpose``), and calls
``Catalog.graph_many`` (or ``graph`` for single-seed convenience),
returning the standard step-output contract:
``{"tumblers": [...], "ids": [...], "collections": [...]}``.

Covers SC-5 (traverse → search composition) and SC-16 (mutual
exclusion of ``link_types`` and ``purpose``).
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from tests._catalog_fixture_ops import ActiveCatalog


def _match(plan: dict) -> "Match":  # noqa: F821
    from nexus.plans.match import Match

    return Match(
        plan_id=1, name="default", description="t", confidence=0.9,
        dimensions={}, tags="", plan_json=json.dumps(plan),
        required_bindings=list(plan.get("required_bindings", []) or []),
        optional_bindings=[], default_bindings={}, parent_dims=None,
    )


# ── seeds resolution from $stepN.tumblers ──────────────────────────────────


@pytest.mark.asyncio
async def test_traverse_seeds_resolve_from_step_ref() -> None:
    """``seeds: $step1.tumblers`` resolves from the prior retrieval step."""
    from nexus.plans.runner import plan_run

    plan = {
        "steps": [
            {"tool": "search", "args": {"query": "x"}},
            {
                "tool": "traverse",
                "args": {
                    "seeds": "$step1.tumblers",
                    "purpose": "find-implementations",
                    "depth": 1,
                },
            },
        ],
    }

    captured: list[tuple[str, dict]] = []

    def dispatcher(tool: str, args: dict) -> dict:
        captured.append((tool, args))
        if tool == "search":
            return {
                "text": "x", "tumblers": ["1.1", "1.2"], "ids": ["a", "b"],
            }
        if tool == "traverse":
            assert args["seeds"] == ["1.1", "1.2"]
            return {"tumblers": ["1.1", "1.2", "1.1.1"], "ids": [], "collections": []}
        raise AssertionError(f"unexpected tool {tool}")

    await plan_run(_match(plan), {}, dispatcher=dispatcher)
    assert captured[1][0] == "traverse"


@pytest.mark.asyncio
async def test_traverse_step_output_shape_drives_subtree_filter() -> None:
    """SC-5: traverse output exposes ``collections`` so a downstream
    ``search(subtree=...)`` step can chain off it."""
    from nexus.plans.runner import plan_run

    plan = {
        "steps": [
            {
                "tool": "traverse",
                "args": {
                    "seeds": ["1.1"],
                    "purpose": "find-implementations",
                },
            },
            {
                "tool": "search",
                "args": {
                    "query": "downstream",
                    "subtree": "$step1.collections",
                },
            },
        ],
    }

    def dispatcher(tool: str, args: dict) -> dict:
        if tool == "traverse":
            return {
                "tumblers": ["1.1.1"],
                "ids": [],
                "collections": ["docs__one", "docs__two"],
            }
        if tool == "search":
            assert args["subtree"] == ["docs__one", "docs__two"]
            return {"text": "ok", "ids": []}
        raise AssertionError(f"unexpected tool {tool}")

    await plan_run(_match(plan), {}, dispatcher=dispatcher)


# ── SC-16: link_types / purpose mutual exclusion at runner level ───────────


@pytest.mark.asyncio
async def test_traverse_step_accepts_link_types_only() -> None:
    """``link_types`` alone → dispatcher gets the literal list."""
    from nexus.plans.runner import plan_run

    plan = {
        "steps": [
            {
                "tool": "traverse",
                "args": {
                    "seeds": ["1.1"],
                    "link_types": ["implements"],
                    "depth": 1,
                },
            },
        ],
    }

    captured: list[dict] = []

    def dispatcher(tool: str, args: dict) -> dict:
        captured.append(args)
        return {"tumblers": [], "ids": [], "collections": []}

    await plan_run(_match(plan), {}, dispatcher=dispatcher)
    assert captured[0]["link_types"] == ["implements"]


@pytest.mark.asyncio
async def test_traverse_step_accepts_purpose_only() -> None:
    from nexus.plans.runner import plan_run

    plan = {
        "steps": [
            {
                "tool": "traverse",
                "args": {
                    "seeds": ["1.1"],
                    "purpose": "decision-evolution",
                    "depth": 1,
                },
            },
        ],
    }

    captured: list[dict] = []

    def dispatcher(tool: str, args: dict) -> dict:
        captured.append(args)
        return {"tumblers": [], "ids": [], "collections": []}

    await plan_run(_match(plan), {}, dispatcher=dispatcher)
    assert captured[0]["purpose"] == "decision-evolution"


# ── traverse MCP tool: Catalog.graph_many composition ──────────────────────


@pytest.fixture()
def fake_catalog(tmp_path: Path, monkeypatch):
    """A real Catalog seeded with a small graph for end-to-end traverse tests.

    Sets ``NEXUS_CATALOG_PATH`` so production code paths that call
    ``get_catalog()`` resolve to this on-disk catalog.
    """
    # nexus-aqbrk: seed through the ACTIVE catalog. mcp_core.traverse resolves
    # its catalog through the factory. nexus-i711w terminal deletion: the
    # local Catalog.init leg died; ActiveCatalog needs no local init.
    cat = ActiveCatalog()
    owner = cat.register_owner("p", "test")
    rdr = cat.register(owner, "RDR", physical_collection="rdr__test")
    impl_a = cat.register(owner, "ImplA", physical_collection="code__test")
    impl_b = cat.register(owner, "ImplB", physical_collection="code__test")
    cat.link(rdr, impl_a, "implements", created_by="t")
    cat.link(rdr, impl_b, "implements-heuristic", created_by="t")
    return cat, rdr, impl_a, impl_b


def test_traverse_mcp_tool_resolves_purpose_and_calls_graph_many(
    fake_catalog,
) -> None:
    """The traverse MCP tool resolves ``purpose`` to link_types,
    calls ``graph_many``, and returns the canonical step output
    ``{tumblers, ids, collections}``."""
    from nexus.mcp import core as mcp_core

    cat, rdr, impl_a, impl_b = fake_catalog
    result = mcp_core.traverse(
        seeds=[str(rdr)],
        purpose="find-implementations",
        depth=1,
        direction="out",
    )

    assert isinstance(result, dict)
    assert "tumblers" in result
    assert str(impl_a) in result["tumblers"]
    assert str(impl_b) in result["tumblers"]
    # collections list is the union of physical_collection values.
    assert "code__test" in result["collections"]


def test_traverse_mcp_tool_accepts_explicit_link_types(
    fake_catalog,
) -> None:
    from nexus.mcp import core as mcp_core

    cat, rdr, impl_a, impl_b = fake_catalog
    result = mcp_core.traverse(
        seeds=[str(rdr)],
        link_types=["implements"],
        depth=1,
        direction="out",
    )

    assert str(impl_a) in result["tumblers"]
    # 'implements-heuristic' was excluded → impl_b should not appear.
    assert str(impl_b) not in result["tumblers"]


def test_traverse_mcp_tool_rejects_link_types_and_purpose_together(
    fake_catalog,
) -> None:
    """SC-16 enforced at the MCP-tool boundary."""
    from nexus.mcp import core as mcp_core

    cat, rdr, *_ = fake_catalog
    result = mcp_core.traverse(
        seeds=[str(rdr)],
        link_types=["implements"],
        purpose="find-implementations",
    )
    # MCP tools surface errors as strings rather than raising.
    assert isinstance(result, dict)
    assert result.get("error"), f"expected error, got {result}"


# ── chunk IDs from T3 (nexus-0m3) ──────────────────────────────────────────


@pytest.fixture()
def fake_catalog_with_paths(tmp_path: Path, monkeypatch):
    """Catalog seeded with file_path so T3 ID lookup can be tested.

    Sets ``NEXUS_CATALOG_PATH`` so production code paths that call
    ``get_catalog()`` resolve to this on-disk catalog.
    """
    # nexus-aqbrk: seed through the ACTIVE catalog — see fake_catalog above.
    cat = ActiveCatalog()
    owner = cat.register_owner("p", "test")
    rdr = cat.register(
        owner, "RDR",
        physical_collection="rdr__test",
        file_path="docs/rdr/rdr-001.md",
    )
    impl = cat.register(
        owner, "ImplA",
        physical_collection="code__test",
        file_path="src/foo.py",
    )
    cat.link(rdr, impl, "implements", created_by="t")
    return cat, rdr, impl


# nexus-bm8dd: the three T3-based ``ids`` tests that lived here
# (test_traverse_ids_populated_from_t3, ..._gracefully_degrade_when_t3_unavailable,
# ..._dedup_across_nodes) drove a MagicMock T3 whose ids_for_source returned
# whatever the test told it to. Against the real client that method matched
# nothing — RDR-102 D2 removed source_path from the chunk schema — so traverse
# returned ids=[] in production the whole time these passed.
#
# traverse now reads chunk ids from the catalog manifest. The successors live in
# tests/test_traverse.py (manifest-sourced ids, cross-document dedup, and
# graceful degradation when the manifest lookup fails) and assert against the
# CATALOG, which is where the ids actually come from.
