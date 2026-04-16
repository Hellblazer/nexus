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
def fake_catalog(tmp_path: Path):
    """A real Catalog seeded with a small graph for end-to-end traverse tests."""
    from nexus.catalog.catalog import Catalog

    cat_dir = tmp_path / "catalog"
    cat_dir.mkdir(parents=True, exist_ok=True)
    cat = Catalog(catalog_dir=cat_dir, db_path=tmp_path / "catalog.db")
    owner = cat.register_owner("p", "test")
    rdr = cat.register(owner, "RDR", physical_collection="rdr__test")
    impl_a = cat.register(owner, "ImplA", physical_collection="code__test")
    impl_b = cat.register(owner, "ImplB", physical_collection="code__test")
    cat.link(rdr, impl_a, "implements", created_by="t")
    cat.link(rdr, impl_b, "implements-heuristic", created_by="t")
    return cat, rdr, impl_a, impl_b


def test_traverse_mcp_tool_resolves_purpose_and_calls_graph_many(
    fake_catalog, monkeypatch,
) -> None:
    """The traverse MCP tool resolves ``purpose`` to link_types,
    calls ``graph_many``, and returns the canonical step output
    ``{tumblers, ids, collections}``."""
    from nexus.mcp import core as mcp_core
    from nexus.mcp_infra import inject_catalog

    cat, rdr, impl_a, impl_b = fake_catalog
    inject_catalog(cat)
    try:
        result = mcp_core.traverse(
            seeds=[str(rdr)],
            purpose="find-implementations",
            depth=1,
            direction="out",
        )
    finally:
        inject_catalog(None)

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
    from nexus.mcp_infra import inject_catalog

    cat, rdr, impl_a, impl_b = fake_catalog
    inject_catalog(cat)
    try:
        result = mcp_core.traverse(
            seeds=[str(rdr)],
            link_types=["implements"],
            depth=1,
            direction="out",
        )
    finally:
        inject_catalog(None)

    assert str(impl_a) in result["tumblers"]
    # 'implements-heuristic' was excluded → impl_b should not appear.
    assert str(impl_b) not in result["tumblers"]


def test_traverse_mcp_tool_rejects_link_types_and_purpose_together(
    fake_catalog,
) -> None:
    """SC-16 enforced at the MCP-tool boundary."""
    from nexus.mcp import core as mcp_core
    from nexus.mcp_infra import inject_catalog

    cat, rdr, *_ = fake_catalog
    inject_catalog(cat)
    try:
        result = mcp_core.traverse(
            seeds=[str(rdr)],
            link_types=["implements"],
            purpose="find-implementations",
        )
    finally:
        inject_catalog(None)
    # MCP tools surface errors as strings rather than raising.
    assert isinstance(result, dict)
    assert result.get("error"), f"expected error, got {result}"


# ── chunk IDs from T3 (nexus-0m3) ──────────────────────────────────────────


@pytest.fixture()
def fake_catalog_with_paths(tmp_path: Path):
    """Catalog seeded with file_path so T3 ID lookup can be tested."""
    from nexus.catalog.catalog import Catalog

    cat_dir = tmp_path / "catalog"
    cat_dir.mkdir(parents=True, exist_ok=True)
    cat = Catalog(catalog_dir=cat_dir, db_path=tmp_path / "catalog.db")
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


def test_traverse_ids_populated_from_t3(fake_catalog_with_paths) -> None:
    """traverse populates ``ids`` by querying T3 with each node's file_path."""
    from unittest.mock import MagicMock
    from nexus.mcp import core as mcp_core
    from nexus.mcp_infra import inject_catalog, inject_t3

    cat, rdr, impl = fake_catalog_with_paths

    mock_t3 = MagicMock()
    # Map (collection, source_path) → chunk IDs
    def _ids_for_source(collection, source_path):
        return {
            ("rdr__test", "docs/rdr/rdr-001.md"): ["chunk-r1", "chunk-r2"],
            ("code__test", "src/foo.py"): ["chunk-c1"],
        }.get((collection, source_path), [])

    mock_t3.ids_for_source.side_effect = _ids_for_source

    inject_catalog(cat)
    inject_t3(mock_t3)
    try:
        result = mcp_core.traverse(
            seeds=[str(rdr)],
            link_types=["implements"],
            depth=1,
            direction="out",
        )
    finally:
        inject_catalog(None)
        inject_t3(None)

    # The seed RDR is not in the result nodes (only traversed nodes), but
    # the impl node's chunks should appear.
    assert "chunk-c1" in result["ids"]


def test_traverse_ids_gracefully_degrade_when_t3_unavailable(
    fake_catalog_with_paths,
) -> None:
    """ids=[] when T3 raises — no exception propagated to caller."""
    from unittest.mock import MagicMock
    from nexus.mcp import core as mcp_core
    from nexus.mcp_infra import inject_catalog, inject_t3

    cat, rdr, _ = fake_catalog_with_paths

    mock_t3 = MagicMock()
    mock_t3.ids_for_source.side_effect = RuntimeError("T3 unavailable")

    inject_catalog(cat)
    inject_t3(mock_t3)
    try:
        result = mcp_core.traverse(
            seeds=[str(rdr)],
            link_types=["implements"],
            depth=1,
        )
    finally:
        inject_catalog(None)
        inject_t3(None)

    assert result["ids"] == []


def test_traverse_ids_dedup_across_nodes(fake_catalog_with_paths) -> None:
    """Duplicate chunk IDs across nodes are deduplicated in output."""
    from unittest.mock import MagicMock
    from nexus.mcp import core as mcp_core
    from nexus.mcp_infra import inject_catalog, inject_t3

    cat, rdr, impl = fake_catalog_with_paths

    mock_t3 = MagicMock()
    # Both nodes share the same chunk ID (shouldn't happen in practice, but
    # the dedup guard should handle it).
    mock_t3.ids_for_source.return_value = ["shared-chunk"]

    inject_catalog(cat)
    inject_t3(mock_t3)
    try:
        result = mcp_core.traverse(
            seeds=[str(rdr)],
            link_types=["implements"],
            depth=1,
            direction="out",
        )
    finally:
        inject_catalog(None)
        inject_t3(None)

    assert result["ids"].count("shared-chunk") == 1
