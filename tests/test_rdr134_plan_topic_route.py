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
import yaml

from bench.topic_route_nx_answer_ab import arm_setting
from nexus import call_deadline
from nexus import config as _config
from nexus.config import _DEFAULTS  # type: ignore[attr-defined]
from nexus.entity_keys import EntityConfigError
from nexus.mcp import core
from nexus.plans import runner
from nexus.plans import topic_route_scope as trs
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

pytestmark = pytest.mark.usefixtures("named_collection_rows")  # RDR-229 P2.9: prefix-named fixtures get a catalog row

_TOP1 = {"mode": "top1"}
_PROSE = {"taxonomy_domain": "prose"}


@pytest.fixture(autouse=True)
def _tenant_free():
    """The runner's non-evidentiary hygiene and owner narrowing would ask the service; answer 'nothing'."""
    # RDR-229 P2.9: a collection now routes by its row, so a search that finds rows returns a page, and the
    # search records its trace against the T1 session (best-effort; "session may be unavailable in test
    # contexts"). Answer "no session" rather than mint one for a test that holds no tenant.
    with patch("nexus.plans.runner._resolve_non_evidentiary_chashes", return_value=set()), \
         patch("nexus.plans.owner_narrowing.narrow_args_by_owner", lambda args, owners: (args, None)), \
         patch("nexus.mcp.core._get_t1", side_effect=RuntimeError("no T1 session in a tenant-free test")):
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
async def test_a_step_routes_unless_its_scope_names_another_domain_or_a_topic(monkeypatch) -> None:
    # Sam 2026-11-19 (option b after the Step 9 probe): routing no longer needs a prose-scoped plan; only
    # three builtin templates declare one, so most nx_answer plans could never route. The search layer
    # sends the route to prose collections only, so an unscoped step that reaches code is unrouted there.
    _setting(monkeypatch, "top1")
    d = await _run([
        {"tool": "search", "args": {"query": "a"}, "scope": {"taxonomy_domain": "code"}},
        {"tool": "search", "args": {"query": "b"}},
        {"tool": "search", "args": {"query": "c", "corpus": "all"}, "scope": {"topic": "t"}},
        {"tool": "search", "args": {"query": "d"}, "scope": _PROSE},
        {"tool": "search", "args": {"query": "e"}, "scope": {}},
        {"tool": "search", "args": {"query": "f"}, "scope": {"taxonomy_domain": None}},
    ])
    routed = {a["query"]: "topic_route" in a for a in d.of("search")}
    # a null domain is unscoped, as the runner's own domain check reads it
    assert routed == {"a": False, "b": True, "c": False, "d": True, "e": True, "f": True}


@pytest.mark.asyncio
async def test_a_step_that_names_a_topic_is_not_routed(monkeypatch) -> None:
    _setting(monkeypatch, "top1")
    d = await _run([
        {"tool": "search", "args": {"query": "a", "topic": "Some label"}, "scope": _PROSE},
        {"tool": "search", "args": {"query": "b"}, "scope": {"taxonomy_domain": "prose", "topic": "Some label"}},
    ])
    assert all("topic_route" not in a for a in d.of("search"))


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



def _wire_core(monkeypatch, cols: list[str]):
    engine = _engine(monkeypatch, cols)
    monkeypatch.setattr(core, "_get_t3", lambda: engine.client)
    monkeypatch.setattr(core, "_get_catalog", lambda: None)
    monkeypatch.setattr(core, "_search_taxonomy", lambda: None)
    monkeypatch.setattr(core, "_search_telemetry_sink", lambda: None)
    monkeypatch.setattr(core, "_resolve_corpus_target", lambda corpus, t3, **k: list(cols))
    # the ranking boosts read the collection listing; the route double serves only the search routes
    monkeypatch.setattr(engine.client, "list_collections",
                        lambda *a, **k: [{"name": c, "count": 4} for c in cols], raising=False)
    return engine


def test_a_routed_and_an_unrouted_search_never_share_a_cached_page(monkeypatch) -> None:
    # Step 9 runs the two arms of one question seconds apart, inside the page cache's TTL: the route must be part
    # of the key the page is looked up by.
    cols = _cols("docs", _BGE, 2)
    _wire_core(monkeypatch, cols)
    keys: list = []
    monkeypatch.setattr(core, "_page_cache_get", lambda key, need: keys.append(key))
    for route in (_TOP1, None, {"mode": "band", "width": 0.1}, _TOP1):
        with topic_route_scope(route):
            core.search(query="same question", corpus=",".join(cols), threshold=2.0, structured=True)
    assert len(keys) == 4
    assert len({keys[0], keys[1], keys[2]}) == 3, "routed, unrouted and band each have their own page"
    assert keys[0] == keys[3], "the same route reuses its page"


@pytest.mark.asyncio
async def test_the_ab_drivers_arm_switch_reaches_the_runner(monkeypatch) -> None:
    monkeypatch.setattr("nexus.config.load_config", lambda *a, **k: {"search": {}})
    steps = [{"tool": "search", "args": {"query": "q"}, "scope": _PROSE}]
    with arm_setting("top1"):
        on = await _run(steps)
    with arm_setting("off"):
        off = await _run(steps)
    assert on.of("search")[0]["topic_route"] == _TOP1
    assert "topic_route" not in off.of("search")[0]


def test_a_config_that_cannot_load_gives_no_route_and_a_warning(monkeypatch: pytest.MonkeyPatch) -> None:
    """The route is an optional setting read once per plan run (nexus-7fku2.2.1). A config that fails to load, such as
    a malformed ``entity`` section (EntityConfigError), must not end the plan run here: RDR-147's resolution boundary
    is where that error is judged. No route, and one warning naming the error."""

    def broken(*a: Any, **k: Any) -> dict:
        raise EntityConfigError("entity.collections[x]: not a regex")

    monkeypatch.setattr(_config, "load_config", broken)
    with structlog.testing.capture_logs() as logs:
        assert trs.configured_route() is None
    events = [e for e in logs if e.get("event") == trs.CONFIG_UNREADABLE_EVENT]
    assert len(events) == 1 and "not a regex" in events[0]["error"]


@pytest.mark.parametrize("error", [
    yaml.YAMLError("mapping values are not allowed here"),
    OSError("config.yml: permission denied"),
    UnicodeDecodeError("utf-8", b"\xff", 0, 1, "invalid start byte"),
], ids=["yaml", "oserror", "unicode"])
def test_any_unreadable_config_gives_no_route_and_one_capped_warning(
    monkeypatch: pytest.MonkeyPatch, error: Exception,
) -> None:
    """The route is an optional, off-by-default setting, so a config.yml that cannot be read at all (YAML, file or
    encoding error) must not end a plan run that has no search step: no route, one warning carrying the error capped at
    200 characters. The search steps re-read the config and report the error themselves."""

    def broken(*a: Any, **k: Any) -> dict:
        raise error

    monkeypatch.setattr(_config, "load_config", broken)
    with structlog.testing.capture_logs() as logs:
        assert trs.configured_route() is None
    events = [e for e in logs if e.get("event") == trs.CONFIG_UNREADABLE_EVENT]
    assert len(events) == 1 and len(events[0]["error"]) <= 200 and events[0]["setting_state"] == "unreadable"


def test_an_unreadable_config_warning_caps_a_long_error() -> None:
    long = EntityConfigError("x" * 500)
    with structlog.testing.capture_logs() as logs, patch.object(_config, "load_config", side_effect=long):
        assert trs.configured_route() is None
    (event,) = [e for e in logs if e.get("event") == trs.CONFIG_UNREADABLE_EVENT]
    assert event["error"] == "x" * 200
