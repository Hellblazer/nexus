"""MCP startup warm-up of the search path (nexus-vpa9q).

A cold MCP process paid a first search's version probe (~0.25 s), routing listing
(0.25-0.43 s) and a new connection for the second model group (0.2-0.3 s): network
round trips and TCP/TLS setup, 0.01-0.02 s of each on the engine (conexus ALB,
2026-10-09). The server now does those at start, on a daemon thread, and never the
full stats listing (0.7 s of engine DB time), which a session that never searches
should not pay.
"""

from __future__ import annotations

import inspect
from typing import Any

import pytest

import nexus.mcp_infra as mcp_infra
from nexus.db.http_vector_client import HttpVectorClient

_ROW = {"name": "code__own__bge-base-en-v15-768__v1", "content_type": "code",
        "owner_id": "own", "embedding_model": "bge-base-en-v15-768", "lifecycle_state": "live"}


@pytest.fixture
def engine(monkeypatch):
    """A real client over a fake engine; records every GET path."""
    seen: list[str] = []

    def fake_get(path: str, *, tenant: str = "default") -> Any:
        seen.append(path)
        if path == "/version":
            return {"embedding_mode": "voyage"}
        if "fields=routing" in path:
            return [dict(_ROW)]
        return [{**_ROW, "dim": 768, "count": 10, "stored_count": 10}]

    monkeypatch.setattr("nexus.db.http_vector_client._get", fake_get)
    client = HttpVectorClient()
    monkeypatch.setattr(mcp_infra, "get_t3", lambda: client)
    monkeypatch.setattr("nexus.db.http_vector_client._process_default_tenant", lambda: "not-this-tenant")
    t2_ops: list[str] = []
    link_reads: list[list] = []

    class _Taxonomy:
        def get_topic_link_pairs(self, ids):
            link_reads.append(list(ids))
            return {}

    def fake_t2(fn, *, op="t2_write"):
        t2_ops.append(op)
        return fn(type("DB", (), {"taxonomy": _Taxonomy()})())

    monkeypatch.setattr(mcp_infra, "t2_index_write", fake_t2)
    catalogs: list[list] = []

    class _Catalog:
        def resolve_many(self, ids):
            catalogs.append(list(ids))
            return {}

    monkeypatch.setattr(mcp_infra, "get_catalog", lambda: _Catalog())
    mcp_infra.invalidate_collections_cache()
    yield seen, t2_ops, catalogs
    mcp_infra.invalidate_collections_cache()


def test_warmup_does_the_cheap_steps_and_never_the_full_listing(engine) -> None:
    seen, t2_ops, catalogs = engine
    mcp_infra.warm_search_path()
    assert seen.count("/v1/vectors/stats?fields=routing") == 2, "names plus a second warm connection"
    assert "/v1/vectors/stats" not in seen, "the full listing costs 0.7 s of engine DB time"
    assert "/version" in seen
    assert t2_ops == ["search_warmup"]
    assert catalogs == [[mcp_infra._WARMUP_ABSENT_DOC_ID]], "one real catalog request opens its connection"
    assert mcp_infra.get_collection_names() == [_ROW["name"]]


def test_a_failing_step_does_not_stop_the_rest(engine, monkeypatch) -> None:
    _seen, t2_ops, catalogs = engine

    def boom():
        raise RuntimeError("engine down")

    monkeypatch.setattr(mcp_infra, "get_live_collection_names", boom)
    mcp_infra.warm_search_path()
    assert t2_ops == ["search_warmup"]
    assert len(catalogs) == 1


def test_no_t3_skips_the_vector_steps(monkeypatch) -> None:
    def no_t3():
        raise RuntimeError("no service")

    monkeypatch.setattr(mcp_infra, "get_t3", no_t3)
    monkeypatch.setattr(mcp_infra, "get_live_collection_names", lambda: [])
    t2_ops: list[str] = []
    monkeypatch.setattr(mcp_infra, "t2_index_write", lambda fn, *, op="t2_write": t2_ops.append(op))
    monkeypatch.setattr(mcp_infra, "get_catalog", lambda: None)
    mcp_infra.warm_search_path()
    assert t2_ops == ["search_warmup"]


@pytest.mark.parametrize("value", ["0", "false", "off", "no", "OFF"])
def test_the_env_switch_turns_it_off(monkeypatch, value) -> None:
    monkeypatch.setenv(mcp_infra.SEARCH_WARMUP_ENV, value)
    ran: list[int] = []
    assert mcp_infra.warm_search_path_in_background(target=lambda: ran.append(1)) is None
    assert ran == []


def test_on_by_default_runs_on_a_daemon_thread(monkeypatch) -> None:
    monkeypatch.delenv(mcp_infra.SEARCH_WARMUP_ENV, raising=False)
    ran: list[str] = []
    import threading

    thread = mcp_infra.warm_search_path_in_background(
        target=lambda: ran.append(threading.current_thread().name),
    )
    assert thread is not None and thread.daemon
    thread.join(5)
    assert ran == ["nexus-search-warmup"]


def test_the_mcp_lifespan_starts_the_warmup() -> None:
    from nexus.mcp import core

    assert "_warm_search_path_in_background()" in inspect.getsource(core._t1_lifespan)
