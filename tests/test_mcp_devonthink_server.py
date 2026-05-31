# SPDX-License-Identifier: AGPL-3.0-or-later
"""RDR-139 Layer A' — nx-mcp-devonthink agent-surface MCP server.

The optionality is the internal available() gate, not .mcp.json: with DT absent
the server still builds and advertises only the devonthink_status stub (zero DT
tools); with DT present it advertises the full curated surface. This is the
alwaysLoad-independence contract the RDR mandates a test for.
"""
from __future__ import annotations

import asyncio
import json
from unittest.mock import AsyncMock, MagicMock, patch

from nexus.mcp import devonthink as srv


def _tool_names(mcp) -> set[str]:
    return {t.name for t in mcp._tool_manager.list_tools()}


# ── The gate: zero DT tools when DT absent, full surface when present ────────

def test_zero_dt_tools_when_unavailable() -> None:
    mcp = srv.build_server(available=False)
    names = _tool_names(mcp)
    assert names == {"devonthink_status"}  # the stub only, no spawn error


def test_full_curated_surface_when_available() -> None:
    mcp = srv.build_server(available=True)
    names = _tool_names(mcp)
    # Curated surface = status + 16 in-scope DT tools/composites.
    assert len(names) == 17
    assert "devonthink_status" in names
    # spot-check the in-scope set across categories
    for expected in (
        "get_record_text", "find_similar_records", "classify_record",
        "extract_record_content", "extract_record_highlights",
        "resolve_doi_metadata", "search_crossref", "capture_web_page",
        "download_pdf_from_doi", "import_file", "get_databases",
        "get_record_links", "dt_incorporate",
    ):
        assert expected in names, f"missing curated tool {expected}"
    # RDR "Explicitly out of scope": DT selectors/CRUD stay on osascript and are
    # NOT exposed on the agent surface.
    for banned in (
        "search_records", "lookup_records", "get_record_properties",
        "get_record_children", "get_selected_records", "open_record",
        "move_record", "trash_record", "merge_records", "duplicate_record",
    ):
        assert banned not in names, f"out-of-scope tool {banned} must not be exposed"


def test_status_always_present_independent_of_gate() -> None:
    assert "devonthink_status" in _tool_names(srv.build_server(available=False))
    assert "devonthink_status" in _tool_names(srv.build_server(available=True))


# ── Proxies forward to DT via the async core ────────────────────────────────

def test_get_record_text_proxies() -> None:
    with patch.object(srv, "_dt_proxy", new=AsyncMock(return_value='{"text":"x"}')) as m:
        out = asyncio.run(srv.get_record_text("U1"))
    m.assert_awaited_once_with("get_record_text", {"uuid": "U1"})
    assert out == '{"text":"x"}'  # the proxy's return flows back to the caller


def test_resolve_doi_proxies() -> None:
    with patch.object(srv, "_dt_proxy", new=AsyncMock(return_value="{}")) as m:
        asyncio.run(srv.resolve_doi_metadata("10.1/x"))
    m.assert_awaited_once_with("resolve_doi_metadata", {"doi": "10.1/x"})


def test_find_similar_proxies_record_mode() -> None:
    with patch.object(srv, "_dt_proxy", new=AsyncMock(return_value="{}")) as m:
        asyncio.run(srv.find_similar_records("U1", limit=10))
    m.assert_awaited_once_with(
        "find_similar_records", {"mode": "record", "uuid": "U1", "limit": 10})


# ── _dt_proxy fail-soft ─────────────────────────────────────────────────────

def test_dt_proxy_returns_error_json_on_transport_failure() -> None:
    def _boom(_endpoint):
        raise ConnectionError("refused")
    with patch.object(srv, "open_session", _boom):
        out = asyncio.run(srv._dt_proxy("search_records", {"query": "x"}))
    payload = json.loads(out)
    assert "error" in payload and payload["tool"] == "search_records"


# ── dt_incorporate composite ────────────────────────────────────────────────

def test_incorporate_runs_layer_b_and_f() -> None:
    fake = {"tumbler": "1.2.3", "links": {"similar": 1, "link": 0},
            "writeback": {"tags": True, "annotation": True, "metadata": False, "skipped": False}}
    with patch.object(srv, "_incorporate_sync", return_value=fake):
        out = asyncio.run(srv.dt_incorporate("U1"))
    assert json.loads(out) == fake


def test_incorporate_unindexed_record_returns_error() -> None:
    fake = {"error": "record is not indexed in nexus; run "
            "`nx dt index --uuid <uuid>` (or capture) first", "uuid": "U1"}
    with patch.object(srv, "_incorporate_sync", return_value=fake):
        out = asyncio.run(srv.dt_incorporate("U1"))
    assert "not indexed" in json.loads(out)["error"]


def test_incorporate_sync_unindexed_returns_error(tmp_path, monkeypatch) -> None:
    """SIG-3: exercise _incorporate_sync's real body (catalog routing), not a
    mock of the whole function. An unindexed UUID -> structured error, no
    Layer B/F calls, and the catalog connection is closed."""
    cat = MagicMock()
    cat.by_source_uri.return_value = None
    cat._db = MagicMock()
    CatalogMock = MagicMock(return_value=cat)
    CatalogMock.is_initialized = staticmethod(lambda p: True)
    monkeypatch.setattr("nexus.catalog.catalog.Catalog", CatalogMock)
    monkeypatch.setattr("nexus.config.catalog_path", lambda: tmp_path)
    called = {"links": False, "wb": False}
    monkeypatch.setattr("nexus.catalog.dt_link_generator.generate_dt_links",
                        lambda *a, **k: called.__setitem__("links", True))
    monkeypatch.setattr("nexus.dt_writeback.writeback_record",
                        lambda *a, **k: called.__setitem__("wb", True))
    out = srv._incorporate_sync("NO-SUCH-UUID")
    assert "not indexed" in out["error"]
    assert called == {"links": False, "wb": False}  # never reached Layer B/F
    cat._db.close.assert_called_once()  # connection released even on the error path


def test_incorporate_sync_uninitialized_catalog(tmp_path, monkeypatch) -> None:
    CatalogMock = MagicMock()
    CatalogMock.is_initialized = staticmethod(lambda p: False)
    monkeypatch.setattr("nexus.catalog.catalog.Catalog", CatalogMock)
    monkeypatch.setattr("nexus.config.catalog_path", lambda: tmp_path)
    out = srv._incorporate_sync("U1")
    assert "not initialized" in out["error"]


def test_status_restart_hint_when_started_without_dt(monkeypatch) -> None:
    """SIG-2: a server started before DT launched advertises only the stub;
    devonthink_status surfaces a restart hint once DT becomes reachable."""
    srv.build_server(available=False)  # sets _STARTED_WITH_DT = False

    async def _fake_call(session, tool, args):
        return {"running": True} if tool == "is_running" else {"result": [1, 2]}

    class _FakeSession:
        async def __aenter__(self): return self
        async def __aexit__(self, *a): return False

    import contextlib
    @contextlib.asynccontextmanager
    async def _fake_open(endpoint):
        yield object()
    monkeypatch.setattr(srv, "open_session", _fake_open)
    monkeypatch.setattr(srv, "call_tool", _fake_call)
    out = json.loads(asyncio.run(srv.devonthink_status()))
    assert out["available"] is True
    assert out.get("restart_required") is True


# ── main() probes the gate and builds accordingly ───────────────────────────

def test_main_builds_with_zero_tools_when_dt_absent() -> None:
    built = {}

    def _capture(*, available):
        built["available"] = available
        mcp = srv.FastMCP("probe")
        mcp.add_tool(srv.devonthink_status)
        mcp.run = lambda **kw: None  # don't actually serve
        return mcp

    with patch("nexus.mcp_client.devonthink.available", return_value=False), \
         patch("nexus.mcp._first_run.ensure_installed_and_running", lambda: None), \
         patch.object(srv, "build_server", _capture):
        srv.main()
    assert built["available"] is False
