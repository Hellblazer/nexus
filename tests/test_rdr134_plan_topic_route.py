# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""RDR-134 Step 7 (nexus-7fku2.2.1): the plan runner carries ``topic_route`` from ``search.topic_route_nx_answer``.

Three layers, as the ``chash_allow`` tests of ``test_rdr232_document_confinement`` are laid out: the runner attaches
the route to a step (a counting dispatcher), the default dispatcher turns it into a context value the tool reads,
and ``search_cross_corpus`` sends it to the engine (the stubbed engine of ``test_search_topic_route_client``). One
test runs the whole chain: a real ``plan_run`` through the default dispatcher and the real ``search`` tool.
"""
from __future__ import annotations

import asyncio
from typing import Any
from unittest.mock import patch

import pytest
import structlog

from nexus import call_deadline
from nexus.config import _DEFAULTS  # type: ignore[attr-defined]
from nexus.mcp import core
from nexus.plans import runner
from nexus.plans.runner import plan_run
from nexus.plans.topic_route_scope import (
    INVALID_EVENT,
    active_topic_route,
    parse_setting,
    setting_from_config,
    topic_route_scope,
)
from tests.test_rdr232_coverage_ledger import RDR_COLL, _match, _structured
from tests.test_search_per_collection_route import (  # type: ignore[import-not-found]
    _BGE,
    _cols,
    _fresh_process_state,  # noqa: F401 — autouse: opts the per-collection route in, resets the memos
)
from tests.test_search_topic_route_client import _engine, _search

_TOP1 = {"mode": "top1"}
_PROSE = {"taxonomy_domain": "prose"}


@pytest.fixture(autouse=True)
def _tenant_free():
    """The runner's non-evidentiary hygiene and owner narrowing would ask the service; answer 'nothing'."""
    with patch("nexus.plans.runner._resolve_non_evidentiary_chashes", return_value=set()), \
         patch("nexus.plans.owner_narrowing.narrow_args_by_owner", lambda args, owners: (args, None)):
        yield


def _setting(monkeypatch, value: Any) -> None:
    """Configure ``search.topic_route_nx_answer`` the way ``.nexus.yml`` would."""
    monkeypatch.setattr("nexus.config.load_config", lambda *a, **k: {"search": {"topic_route_nx_answer": value}})


class _Counting:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict]] = []

    async def __call__(self, tool: str, args: dict) -> dict:
        self.calls.append((tool, dict(args)))
        if tool == "generate":
            return {"text": "the answer"}
        return _structured([], RDR_COLL)

    def of(self, tool: str) -> list[dict]:
        return [a for t, a in self.calls if t == tool]


async def _run(steps: list[dict]) -> _Counting:
    d = _Counting()
    await plan_run(_match({"steps": steps}), {}, dispatcher=d, bundle_operators=False)
    return d


# ── the setting ──────────────────────────────────────────────────────────────


@pytest.mark.parametrize(("value", "want"), [
    ("top1", {"mode": "top1"}),
    ("TOP1", {"mode": "top1"}),
    ("band:0.05", {"mode": "band", "width": 0.05}),
    ("band:1", {"mode": "band", "width": 1.0}),
])
def test_a_routed_setting_parses(value: str, want: dict) -> None:
    assert parse_setting(value) == want


@pytest.mark.parametrize("value", [None, False, "off", "OFF", ""])
def test_off_in_every_spelling_is_off_and_quiet(value: Any) -> None:
    # YAML 1.1 reads an unquoted ``off`` in .nexus.yml as false
    with structlog.testing.capture_logs() as logs:
        assert parse_setting(value) is None
    assert not [e for e in logs if e["event"] == INVALID_EVENT]


@pytest.mark.parametrize("value", ["band:abc", "band:", "band:0", "band:-0.1", "band:nan", "band:inf", "bogus", True, 1])
def test_a_bad_value_warns_and_falls_back_to_off_never_to_routed(value: Any) -> None:
    with structlog.testing.capture_logs() as logs:
        assert parse_setting(value) is None
    (event,) = [e for e in logs if e["event"] == INVALID_EVENT]
    assert event["log_level"] == "warning" and event["fallback"] == "off"


def test_the_default_is_off() -> None:
    assert _DEFAULTS["search"]["topic_route_nx_answer"] == "off"
    assert setting_from_config({}) is None and setting_from_config({"search": {}}) is None


def test_the_setting_is_read_once_per_run_not_once_per_step(monkeypatch) -> None:
    reads: list[int] = []
    monkeypatch.setattr("nexus.config.load_config", lambda *a, **k: reads.append(1) or {"search": {}})
    asyncio.run(_run([{"tool": "search", "args": {"query": "q"}, "scope": _PROSE}] * 3))
    assert len(reads) == 1


# ── the runner attaches the route to a step ─────────────────────────────────


@pytest.mark.asyncio
async def test_a_prose_search_and_query_step_carry_the_route(monkeypatch) -> None:
    _setting(monkeypatch, "band:0.05")
    d = await _run([
        {"tool": "search", "args": {"query": "q"}, "scope": _PROSE},
        {"tool": "query", "args": {"question": "q"}, "scope": _PROSE},
    ])
    want = {"mode": "band", "width": 0.05}
    assert [a["topic_route"] for a in d.of("search") + d.of("query")] == [want, want]


@pytest.mark.asyncio
async def test_only_a_prose_scope_is_routed(monkeypatch) -> None:
    _setting(monkeypatch, "top1")
    d = await _run([
        {"tool": "search", "args": {"query": "a"}, "scope": {"taxonomy_domain": "code"}},
        {"tool": "search", "args": {"query": "b"}},
        {"tool": "search", "args": {"query": "c", "corpus": "all"}, "scope": {"topic": "t"}},
        {"tool": "search", "args": {"query": "d"}, "scope": _PROSE},
    ])
    routed = {a["query"]: "topic_route" in a for a in d.of("search")}
    assert routed == {"a": False, "b": False, "c": False, "d": True}


@pytest.mark.asyncio
@pytest.mark.parametrize("param", ["author", "content_type", "follow_links", "subtree"])
async def test_a_query_step_on_the_catalog_route_is_not_routed(monkeypatch, param: str) -> None:
    _setting(monkeypatch, "top1")
    d = await _run([{"tool": "query", "args": {"question": "q", param: "x"}, "scope": _PROSE}])
    (sent,) = d.of("query")
    assert "topic_route" not in sent


@pytest.mark.asyncio
@pytest.mark.parametrize("tool", ["search_metadata_scoped", "search_topic_scoped", "search_graph_hop", "search_aspect_scoped"])
async def test_the_combined_query_routes_are_never_routed(monkeypatch, tool: str) -> None:
    _setting(monkeypatch, "top1")
    d = await _run([{"tool": tool, "args": {"query": "q", "topic": "t", "seeds": ["1.1"]}, "scope": _PROSE}])
    (sent,) = d.of(tool)
    assert "topic_route" not in sent


@pytest.mark.asyncio
@pytest.mark.parametrize("value", [None, "off", False, "bogus"])
async def test_setting_off_sends_nothing_even_when_the_plan_wrote_the_key(monkeypatch, value: Any) -> None:
    if value is None:
        monkeypatch.setattr("nexus.config.load_config", lambda *a, **k: {"search": {}})
    else:
        _setting(monkeypatch, value)
    d = await _run([
        {"tool": "search", "args": {"query": "q"}, "scope": _PROSE},
        {"tool": "search", "args": {"query": "q", "topic_route": _TOP1}, "scope": _PROSE},
    ])
    assert all("topic_route" not in a for a in d.of("search")), "the setting is the only source of the route"


# ── the default dispatcher turns the arg into a context value ───────────────


@pytest.mark.asyncio
@pytest.mark.parametrize("tool", ["search", "query"])
async def test_the_default_dispatcher_hands_the_route_to_the_tool_as_context_not_as_a_parameter(tool: str) -> None:
    seen: dict[str, Any] = {}

    def fake(*, query: str = "", question: str = "", structured: bool = False) -> dict:
        seen["route"] = active_topic_route()
        return {"ids": [], "tumblers": [], "distances": [], "collections": []}

    with structlog.testing.capture_logs() as logs, patch.object(core, tool, fake):
        await runner._default_dispatcher(tool, {"query": "q", "question": "q", "topic_route": _TOP1})
    assert seen["route"] == _TOP1
    assert not [e for e in logs if e["event"] == "plan_dispatcher_kwargs_dropped"], "popped before the drop pass"
    assert active_topic_route() is None, "the context value does not outlive the call"
    with patch.object(core, tool, fake):
        await runner._default_dispatcher(tool, {"query": "q", "question": "q"})
    assert seen["route"] is None


@pytest.mark.asyncio
async def test_the_route_reaches_a_tool_run_on_the_retrieval_executor() -> None:
    seen: list = []
    token = call_deadline.set_deadline(10**12)
    try:
        with patch.object(core, "search", lambda *, query="", structured=False: seen.append(active_topic_route()) or {
                "ids": [], "tumblers": [], "distances": [], "collections": []}):
            await runner._default_dispatcher("search", {"query": "q", "topic_route": _TOP1})
    finally:
        call_deadline.reset(token)
    assert seen == [_TOP1], "the deadline path copies the context into the worker thread"


# ── search_cross_corpus sends what the context carries ─────────────────────


def test_search_cross_corpus_reads_the_context_route(monkeypatch) -> None:
    cols = _cols("docs", _BGE, 2)
    engine = _engine(monkeypatch, cols)
    with topic_route_scope(_TOP1):
        _search(engine, cols)
    (call,) = engine.route_calls()
    assert call["topic_route"] == {"mode": "top1", "collections": cols}


def test_search_cross_corpus_without_a_context_route_sends_no_key(monkeypatch) -> None:
    cols = _cols("docs", _BGE, 2)
    engine = _engine(monkeypatch, cols)
    _search(engine, cols)
    (call,) = engine.route_calls()
    assert "topic_route" not in call


def test_a_corpus_all_group_that_mixes_code_and_docs_names_only_the_prose(monkeypatch) -> None:
    # the local-bge shape (fix-check item 3): code and docs share one model group, so one request
    code, docs = _cols("code", _BGE, 2), _cols("docs", _BGE, 2)
    engine = _engine(monkeypatch, code + docs)
    with topic_route_scope(_TOP1):
        _search(engine, code + docs)
    (call,) = engine.route_calls()
    assert call["topic_route"]["collections"] == docs
    assert not any(c.startswith("code__") for c in call["topic_route"]["collections"])


def test_a_code_only_request_is_not_routed(monkeypatch) -> None:
    code = _cols("code", _BGE, 2)
    engine = _engine(monkeypatch, code)
    with topic_route_scope(_TOP1):
        _search(engine, code)
    (call,) = engine.route_calls()
    assert "topic_route" not in call


def test_an_explicit_argument_wins_over_the_context(monkeypatch) -> None:
    cols = _cols("docs", _BGE, 1)
    engine = _engine(monkeypatch, cols)
    with topic_route_scope(_TOP1):
        _search(engine, cols, topic_route={"mode": "band", "width": 0.1})
    (call,) = engine.route_calls()
    assert call["topic_route"]["mode"] == "band"


# ── the whole chain: plan_run, the default dispatcher, the real search tool, the engine request ──


@pytest.mark.asyncio
async def test_a_plan_run_sends_topic_route_in_the_engine_request(monkeypatch) -> None:
    cols = _cols("docs", _BGE, 2)
    engine = _engine(monkeypatch, cols)
    monkeypatch.setattr(core, "_get_t3", lambda: engine.client)
    monkeypatch.setattr(core, "_get_catalog", lambda: None)
    monkeypatch.setattr(core, "_search_taxonomy", lambda: None)
    monkeypatch.setattr(core, "_search_telemetry_sink", lambda: None)
    monkeypatch.setattr(core, "_page_cache_get", lambda *a, **k: None)
    # corpus resolution lists the engine's collections; not what this test is about
    monkeypatch.setattr(core, "_resolve_corpus_target", lambda corpus, t3, **k: list(cols))
    _setting(monkeypatch, "top1")
    steps = [{"tool": "search", "args": {"query": "q", "corpus": ",".join(cols), "threshold": 2.0}, "scope": _PROSE}]
    await plan_run(_match({"steps": steps}), {}, bundle_operators=False)
    routed = [c for c in engine.route_calls() if "topic_route" in c]
    assert routed and routed[0]["topic_route"] == {"mode": "top1", "collections": cols}

    _setting(monkeypatch, "off")
    before = len(engine.route_calls())
    await plan_run(_match({"steps": steps}), {}, bundle_operators=False)
    assert engine.route_calls()[before:] and all("topic_route" not in c for c in engine.route_calls()[before:])

