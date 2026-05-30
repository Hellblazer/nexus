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
from unittest.mock import AsyncMock, patch

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
    # ~20 curated tools: status + the 19 DT tools/composites.
    assert len(names) == 20
    assert "devonthink_status" in names
    # spot-check the curated set across categories
    for expected in (
        "search_records", "get_record_text", "find_similar_records",
        "extract_record_content", "resolve_doi_metadata", "capture_web_page",
        "download_pdf_from_doi", "import_file", "get_databases", "dt_incorporate",
    ):
        assert expected in names, f"missing curated tool {expected}"
    # out-of-scope file-management verbs are NOT exposed
    for banned in ("move_record", "trash_record", "merge_records", "duplicate_record"):
        assert banned not in names


def test_status_always_present_independent_of_gate() -> None:
    assert "devonthink_status" in _tool_names(srv.build_server(available=False))
    assert "devonthink_status" in _tool_names(srv.build_server(available=True))


# ── Proxies forward to DT via the async core ────────────────────────────────

def test_search_records_proxies() -> None:
    with patch.object(srv, "_dt_proxy", new=AsyncMock(return_value="{}")) as m:
        asyncio.run(srv.search_records("memory systems", limit=5))
    m.assert_awaited_once_with("search_records", {"query": "memory systems", "limit": 5})


def test_search_records_kind_filter_merges_into_query() -> None:
    with patch.object(srv, "_dt_proxy", new=AsyncMock(return_value="{}")) as m:
        asyncio.run(srv.search_records("x", kind="pdf"))
    assert m.await_args.args[1]["query"] == "x kind:pdf"


def test_get_record_text_proxies() -> None:
    with patch.object(srv, "_dt_proxy", new=AsyncMock(return_value="{}")) as m:
        asyncio.run(srv.get_record_text("U1"))
    m.assert_awaited_once_with("get_record_text", {"uuid": "U1"})


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
