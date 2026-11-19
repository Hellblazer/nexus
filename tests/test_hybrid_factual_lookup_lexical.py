# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""Plan 10 (``hybrid-factual-lookup``) and ``entity-resolution`` ask their search step for ``lexical=true``.

The flag is there for the cross-encoder rerank, which MCP ``search`` applies only when ``lexical=True``
(``core.search``: ``lexical_rerank = lexical and t3.supports_server_rerank``). Measured 2026-11-19 on a local engine,
one HERB collection, 50 "affected by" lookup questions (T2 ``nexus/plan10-lexical-measure-2026-11-19``, and
``nexus_rdr/herb-full-pool-retrieval-variants-2026-11-19`` for the decline shapes): the share of gold companies in the
top 20 went 0.795 to 0.968, and the lexical leg itself returned 0 rows on every question. Retrieval reach only: answer
quality, cloud mode and the default limit of 40 are unmeasured. nexus-wohh7 retires the proxy with a rerank parameter.

No substrate: YAML parses, a stubbed T3 and a stub runner dispatcher.
"""
from __future__ import annotations

import inspect
import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
import yaml

from nexus import mcp_infra
from nexus.db.http_vector_client import HttpVectorClient
from nexus.mcp import core as mcp_core
from nexus.mcp.core import search
from nexus.plans.match import Match
from nexus.plans.runner import _default_dispatcher, plan_run
from nexus.plans.schema import validate_plan_template
from nexus.types import SearchResult

_BUILTIN = Path(__file__).resolve().parents[1] / "conexus" / "plans" / "builtin"
_PLANS = ["hybrid-factual-lookup.yml", "entity-resolution.yml"]


def _template(name: str) -> dict:
    return yaml.safe_load((_BUILTIN / name).read_text())


def _search_step(name: str) -> dict:
    first = _template(name)["plan_json"]["steps"][0]
    assert first["tool"] == "search", "step 1 must stay the search step ($step1.* bindings depend on it)"
    return first


@pytest.mark.parametrize("name", _PLANS)
def test_search_step_carries_lexical_true(name: str) -> None:
    assert _search_step(name)["args"].get("lexical") is True


@pytest.mark.parametrize("name", _PLANS)
def test_search_step_keeps_its_other_arguments(name: str) -> None:
    args = _search_step(name)["args"]
    assert args["query"] == "$question"
    assert args["limit"] == "$limit"
    assert args["corpus"] == "$corpus"
    assert args["threshold"] == 2.0


@pytest.mark.parametrize("name", _PLANS)
def test_template_still_validates(name: str) -> None:
    validate_plan_template(_template(name))


@pytest.mark.parametrize("name", _PLANS)
def test_the_comment_names_the_bead_that_retires_the_proxy_and_the_one_for_the_failure_mode(name: str) -> None:
    text = (_BUILTIN / name).read_text()
    assert "nexus-wohh7" in text
    assert "nexus-vi9u4" in text


def test_search_tool_accepts_the_lexical_argument() -> None:
    """The runner dispatches the step's args as keywords; ``search`` must name ``lexical``."""
    assert "lexical" in inspect.signature(search).parameters


def test_the_service_backend_serves_the_lexical_leg() -> None:
    """The backend markers ``search_cross_corpus`` reads before it will run a lexical request.

    ``make_t3()`` returns ``HttpVectorClient`` for every production install (RDR-155 P4a.2); ``T3Database`` (no
    markers) exists only behind an injected test client, so a ``lexical=true`` step is not refused for the backend.
    This does NOT mean ``LexicalLegUnavailableError`` cannot happen: ``search_engine`` also raises it on any
    ``VectorServiceError`` from the lexical leg (retried only on ``statement_timeout`` and
    ``fanout_budget_exhausted``), see the failure-mode test below.
    """
    assert HttpVectorClient.supports_hybrid_search is True
    assert HttpVectorClient.supports_server_rerank is True


# ── behaviour: lexical=True is what asks the engine for the rerank ──────────


def test_core_search_with_lexical_asks_the_search_call_for_the_rerank(monkeypatch: pytest.MonkeyPatch) -> None:
    """The proxy coupling the plan comments rely on: ``lexical=True`` -> ``rerank=True`` on a rerank-capable T3."""
    monkeypatch.setattr(mcp_infra, "_start_fanout_counts_refresh", lambda: None)
    t3 = MagicMock()
    t3.supports_server_rerank = True
    t3.list_collections.return_value = [{
        "name": "code__test", "count": 1, "content_type": "code", "owner_id": "test",
        "embedding_model": "test-model", "lifecycle_state": "live",
    }]
    mcp_infra.inject_t3(t3)
    seen: list[dict] = []

    def _cross_corpus(*a, **kw):
        seen.append(kw)
        return [SearchResult(id="r1", content="x", distance=0.1, collection="code__test", metadata={})]

    try:
        with patch("nexus.mcp.core._search_telemetry_sink", return_value=None), \
             patch("nexus.mcp.core._get_catalog", return_value=None), \
             patch("nexus.mcp.core._get_t1", return_value=(MagicMock(session_id=""), None)), \
             patch("nexus.mcp.core._search_taxonomy", return_value=None), \
             patch("nexus.search_engine.search_cross_corpus", _cross_corpus):
            search(query="what is the value", corpus="code__test", lexical=True, structured=True)
            search(query="what is the value", corpus="code__test", lexical=False, structured=True)
    finally:
        mcp_infra.reset_singletons()
    assert [kw["lexical"] for kw in seen] == [True, False]
    assert [kw["rerank"] for kw in seen] == [True, False], "rerank follows lexical, and only lexical"


# ── behaviour: the plan's step 1 reaches the dispatcher with lexical=True ───


def _match(name: str) -> Match:
    t = _template(name)
    return Match(
        plan_id=0, name=name, description="test", confidence=1.0, dimensions={}, tags="",
        plan_json=json.dumps(t["plan_json"]),
        required_bindings=["question"],
        optional_bindings=["corpus", "limit", "depth", "vector_budget_chunks", "graph_budget_chunks"],
        default_bindings=t["default_bindings"], parent_dims=None,
    )


def _dispatcher(calls: list[tuple[str, dict]], search_result):
    async def dispatch(tool: str, args: dict):
        calls.append((tool, args))
        if tool == "search":
            return search_result
        if tool == "traverse":
            return {"ids": [], "tumblers": [], "collections": []}
        if tool == "store_get_many":
            return {"contents": ["evidence"], "missing": []}
        if tool == "rank":
            return {"ranked": ["evidence"]}
        return {"text": "ok"}

    return dispatch


@pytest.mark.asyncio
@pytest.mark.parametrize("name", _PLANS)
async def test_step_one_is_dispatched_with_lexical_true(name: str) -> None:
    calls: list[tuple[str, dict]] = []
    ok = {"ids": ["a"], "tumblers": ["1.1.1"], "distances": [0.1], "collections": ["knowledge__x"]}
    await plan_run(_match(name), {"question": "which companies were affected"}, dispatcher=_dispatcher(calls, ok))
    first_tool, first_args = calls[0]
    assert first_tool == "search"
    assert first_args["lexical"] is True
    assert first_args["threshold"] == 2.0


@pytest.mark.asyncio
async def test_a_search_error_string_becomes_an_empty_step_result_with_the_error_kept() -> None:
    """The failure mode the plan comments state. A lexical-leg or rerank error is an ``Error:`` string from
    ``core.search``; the default dispatcher synthesizes an EMPTY retrieval result (a warning only, the error text
    kept under ``error``), so ``$step1.tumblers`` resolves to ``[]`` and the plan goes on with no seeds and no
    vector rows. Pins the claim; nexus-vi9u4 is the fix (fail, retry or fall back)."""
    err = "Error: the lexical leg failed for ['knowledge__x']: engine 503. Refusing rather than returning vector-only rows"
    seen: list[dict] = []

    def fake_search(**kw):
        seen.append(kw)
        return err

    with patch.object(mcp_core, "search", fake_search):
        out = await _default_dispatcher("search", {"query": "q", "lexical": True, "threshold": 2.0})
    assert seen and seen[0]["lexical"] is True
    assert out["ids"] == [] and out["tumblers"] == [] and out["contents"] == []
    assert out["error"] == err
