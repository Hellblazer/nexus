# SPDX-License-Identifier: AGPL-3.0-or-later
"""TDD tests for RDR-079 P1 — `structured: bool = False` flag on core MCP tools.

Closes RDR-079 Gap A (tool-output contract). Each affected MCP tool gains an
additive keyword-only flag; when True the tool returns a dict matching the
RDR-078 Phase-1 runner contract (retrieval steps emit
``{tumblers, ids, distances}``; state-mutating tools emit confirmation dicts).
When False (the default), behavior is unchanged — existing string-returning
callers are unaffected (backward-compat regression guard, SC-5).

SC-1 enablement: runner's ``_default_dispatcher`` passes ``structured=True``
when dispatching retrieval steps, so plan steps that reference
``$stepN.tumblers`` / ``$stepN.ids`` resolve correctly.
"""
from __future__ import annotations

import pytest


# ── Backward-compat regression: str behavior unchanged when flag is omitted ──


def test_search_without_structured_flag_returns_str() -> None:
    """Zero-arg tool use of search still returns a human-readable string."""
    from nexus.mcp.core import search

    out = search(query="rdr-079-backcompat-sentinel-xyz", corpus="knowledge", limit=1)
    assert isinstance(out, str)


def test_query_without_structured_flag_returns_str() -> None:
    from nexus.mcp.core import query

    out = query(
        question="rdr-079-backcompat-sentinel-xyz", corpus="knowledge", limit=1,
    )
    assert isinstance(out, str)


def test_memory_get_without_structured_flag_returns_str() -> None:
    from nexus.mcp.core import memory_get

    out = memory_get(project="rdr-079-nonexistent-project", title="")
    assert isinstance(out, str)


def test_memory_search_without_structured_flag_returns_str() -> None:
    from nexus.mcp.core import memory_search

    out = memory_search(query="rdr-079-nonexistent", project="rdr-079-empty", limit=1)
    assert isinstance(out, str)


def test_memory_put_without_structured_flag_returns_str() -> None:
    from nexus.mcp.core import memory_put

    out = memory_put(
        content="backcompat str return", project="rdr-079-p1-test",
        title="backcompat-probe", tags="test",
    )
    assert isinstance(out, str)


def test_store_put_without_structured_flag_returns_str() -> None:
    from nexus.mcp.core import store_put

    out = store_put(
        content="rdr-079 backcompat probe",
        collection="knowledge",
        title="rdr-079-backcompat",
        tags="test,rdr-079",
    )
    assert isinstance(out, str)


# ── Structured returns: dict shapes match runner contract ──────────────────


def test_search_structured_returns_runner_contract_dict() -> None:
    """SC-1 enablement: search(..., structured=True) emits
    ``{tumblers, ids, distances, collections}`` — the shape plan_run expects
    from retrieval steps per RDR-078 §Phase 1."""
    from nexus.mcp.core import search

    out = search(
        query="rdr-079-nonexistent-sentinel-xyz", corpus="knowledge", limit=1,
        structured=True,
    )
    assert isinstance(out, dict), f"expected dict, got {type(out).__name__}"
    # The runner contract requires all four keys.
    assert "ids" in out
    assert "tumblers" in out
    assert "distances" in out
    assert "collections" in out
    # All should be list-valued (may be empty when no hits).
    assert isinstance(out["ids"], list)
    assert isinstance(out["tumblers"], list)
    assert isinstance(out["distances"], list)
    assert isinstance(out["collections"], list)
    # Length consistency: ids/tumblers/distances align 1:1.
    assert len(out["ids"]) == len(out["tumblers"]) == len(out["distances"])


def test_query_structured_returns_runner_contract_dict() -> None:
    from nexus.mcp.core import query

    out = query(
        question="rdr-079-nonexistent-sentinel-xyz", corpus="knowledge", limit=1,
        structured=True,
    )
    assert isinstance(out, dict)
    assert set(out.keys()) >= {"ids", "tumblers", "distances", "collections"}
    assert len(out["ids"]) == len(out["tumblers"]) == len(out["distances"])


def test_store_put_structured_returns_confirmation_dict() -> None:
    from nexus.mcp.core import store_put

    out = store_put(
        content="rdr-079 structured probe",
        collection="knowledge",
        title="rdr-079-structured",
        tags="test,rdr-079",
        structured=True,
    )
    assert isinstance(out, dict)
    assert "collection" in out
    # On success: stored=True and doc_id is populated.
    # On failure: error is populated.
    assert "stored" in out or "error" in out


def test_memory_put_structured_returns_confirmation_dict() -> None:
    from nexus.mcp.core import memory_put

    out = memory_put(
        content="rdr-079 structured put",
        project="rdr-079-p1-struct",
        title="struct-probe",
        tags="test",
        structured=True,
    )
    assert isinstance(out, dict)
    assert out.get("project") == "rdr-079-p1-struct"
    assert out.get("title") == "struct-probe"
    assert out.get("stored") is True


def test_memory_get_structured_single_returns_entry_dict() -> None:
    from nexus.mcp.core import memory_get, memory_put

    # Seed an entry so there's something to fetch.
    memory_put(
        content="structured-get payload body",
        project="rdr-079-p1-struct-get",
        title="fetch-me",
        tags="rdr-079",
    )
    out = memory_get(
        project="rdr-079-p1-struct-get", title="fetch-me", structured=True,
    )
    assert isinstance(out, dict)
    assert out.get("project") == "rdr-079-p1-struct-get"
    assert out.get("title") == "fetch-me"
    assert "content" in out


def test_memory_get_structured_list_mode_returns_entries_list() -> None:
    """When title='', memory_get lists titles. Structured form wraps them."""
    from nexus.mcp.core import memory_get, memory_put

    memory_put(
        content="listed-a",
        project="rdr-079-p1-list-mode",
        title="entry-a",
    )
    memory_put(
        content="listed-b",
        project="rdr-079-p1-list-mode",
        title="entry-b",
    )
    out = memory_get(
        project="rdr-079-p1-list-mode", title="", structured=True,
    )
    assert isinstance(out, dict)
    assert out.get("project") == "rdr-079-p1-list-mode"
    assert "entries" in out
    assert isinstance(out["entries"], list)
    titles = {e.get("title") for e in out["entries"]}
    assert {"entry-a", "entry-b"} <= titles


def test_memory_search_structured_returns_entries_and_has_more() -> None:
    from nexus.mcp.core import memory_put, memory_search

    memory_put(
        content="rdr-079 unique searchable sentinel zyzzyva",
        project="rdr-079-p1-search",
        title="search-probe-1",
        tags="test",
    )
    out = memory_search(
        query="zyzzyva", project="rdr-079-p1-search", limit=5, structured=True,
    )
    assert isinstance(out, dict)
    assert "entries" in out
    assert "has_more" in out
    assert isinstance(out["entries"], list)
    assert isinstance(out["has_more"], bool)


# ── Runner integration: _default_dispatcher passes structured=True ────────


def test_default_dispatcher_passes_structured_true_to_retrieval_tools(
    monkeypatch,
) -> None:
    """SC-1 critical path: when the runner dispatches a retrieval step (search
    or query), the dispatcher MUST pass ``structured=True`` so the plan step
    receives a dict matching the runner contract."""
    from nexus.plans import runner

    captured: list[tuple[str, dict]] = []

    def fake_search(**kwargs):
        captured.append(("search", kwargs))
        return {"ids": [], "tumblers": [], "distances": [], "collections": []}

    def fake_query(**kwargs):
        captured.append(("query", kwargs))
        return {"ids": [], "tumblers": [], "distances": [], "collections": []}

    # Monkeypatch the lazy import target
    from nexus.mcp import core as mcp_core
    monkeypatch.setattr(mcp_core, "search", fake_search)
    monkeypatch.setattr(mcp_core, "query", fake_query)

    # Dispatch search
    runner._default_dispatcher("search", {"query": "x", "corpus": "knowledge"})
    assert captured, "dispatcher did not call search"
    name, kwargs = captured[-1]
    assert name == "search"
    assert kwargs.get("structured") is True, (
        "runner must pass structured=True when dispatching retrieval tools"
    )

    # Dispatch query
    runner._default_dispatcher("query", {"question": "x"})
    name, kwargs = captured[-1]
    assert name == "query"
    assert kwargs.get("structured") is True


def test_default_dispatcher_does_not_add_structured_to_non_retrieval_tools(
    monkeypatch,
) -> None:
    """Non-retrieval tools (e.g. memory_put) should NOT silently gain
    ``structured=True`` from the dispatcher. The flag is retrieval-scoped.
    Tools can opt in if their caller chooses, but the dispatcher is
    deterministic about which tools it auto-structures."""
    from nexus.plans import runner

    captured: list[tuple[str, dict]] = []

    def fake_memory_put(**kwargs):
        captured.append(("memory_put", kwargs))
        return "Stored"

    from nexus.mcp import core as mcp_core
    monkeypatch.setattr(mcp_core, "memory_put", fake_memory_put)

    runner._default_dispatcher(
        "memory_put",
        {"content": "x", "project": "p", "title": "t"},
    )
    assert captured
    _, kwargs = captured[-1]
    # structured should NOT be set by dispatcher for non-retrieval tools.
    assert "structured" not in kwargs, (
        "dispatcher must only inject structured=True for retrieval tools"
    )
