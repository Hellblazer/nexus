# SPDX-License-Identifier: AGPL-3.0-or-later
"""Tests for nexus.surfaces.mcp_ui and the render_surface MCP tool (RDR-127)."""
from __future__ import annotations

import json
from unittest.mock import patch

import pytest


# ---- Imports under test ----------------------------------------------------


@pytest.fixture
def render_surface_resource():
    from nexus.surfaces.mcp_ui import render_surface_resource
    return render_surface_resource


@pytest.fixture
def nexus_chash_resolver():
    from nexus.surfaces.mcp_ui import nexus_chash_resolver
    return nexus_chash_resolver


@pytest.fixture
def render_surface_tool():
    from nexus.mcp.surfaces import render_surface
    return render_surface


# ---- Fixture payloads ------------------------------------------------------


@pytest.fixture
def chash_a():
    return "abcdef0123456789abcdef0123456789"


@pytest.fixture
def chash_b():
    return "fedcba9876543210fedcba9876543210"


@pytest.fixture
def sample_payload(chash_a, chash_b):
    return {
        "version": "v0.9",
        "messages": [
            {"version": "v0.9", "createSurface": {"surfaceId": "t", "catalogId": "a2ui.basic.v0_9"}},
            {"version": "v0.9", "updateDataModel": {"surfaceId": "t", "path": "/", "value": {
                "items": [
                    {"title": "alpha", "chash": chash_a},
                    {"title": "beta", "chash": chash_b},
                ],
            }}},
            {"version": "v0.9", "updateComponents": {"surfaceId": "t", "components": [
                {"id": "root", "component": "Column", "children": ["btn"]},
                {"id": "btn-lbl", "component": "Text", "text": "Open"},
                {"id": "btn", "component": "Button", "child": "btn-lbl",
                 "action": {"functionCall": {"call": "openChash",
                                             "args": {"chash": {"path": "/items/0/chash"}}}}},
            ]}},
        ],
    }


# ---- nexus_chash_resolver --------------------------------------------------


def test_resolver_rejects_non_string(nexus_chash_resolver):
    assert nexus_chash_resolver(None) is None
    assert nexus_chash_resolver(42) is None
    assert nexus_chash_resolver({"a": 1}) is None


def test_resolver_rejects_wrong_length(nexus_chash_resolver):
    assert nexus_chash_resolver("abc") is None
    assert nexus_chash_resolver("a" * 31) is None
    assert nexus_chash_resolver("a" * 33) is None


def test_resolver_rejects_non_hex(nexus_chash_resolver):
    # 32 chars but not all hex
    assert nexus_chash_resolver("g" * 32) is None
    assert nexus_chash_resolver("z" * 32) is None
    # Uppercase rejected (chashes are lowercase per RDR-108)
    assert nexus_chash_resolver("A" * 32) is None


def test_resolver_returns_content_on_t3_hit(nexus_chash_resolver, chash_a):
    fake_t3 = type("T3", (), {})()
    fake_t3.get_by_id = lambda col, doc_id: (
        {"content": "the resolved chunk text"} if doc_id == chash_a else None
    )
    with patch("nexus.mcp_infra.get_t3", return_value=fake_t3), \
         patch("nexus.corpus.t3_collection_name", return_value="knowledge"):
        assert nexus_chash_resolver(chash_a) == "the resolved chunk text"


def test_resolver_returns_none_on_t3_miss(nexus_chash_resolver, chash_a):
    fake_t3 = type("T3", (), {})()
    fake_t3.get_by_id = lambda col, doc_id: None
    with patch("nexus.mcp_infra.get_t3", return_value=fake_t3), \
         patch("nexus.corpus.t3_collection_name", return_value="knowledge"):
        assert nexus_chash_resolver(chash_a) is None


def test_resolver_swallows_t3_exception(nexus_chash_resolver, chash_a):
    fake_t3 = type("T3", (), {})()
    def boom(col, doc_id):
        raise RuntimeError("simulated T3 failure")
    fake_t3.get_by_id = boom
    with patch("nexus.mcp_infra.get_t3", return_value=fake_t3), \
         patch("nexus.corpus.t3_collection_name", return_value="knowledge"):
        # Doesn't raise; resolver returns None and logs a warning.
        assert nexus_chash_resolver(chash_a) is None


def test_resolver_returns_none_on_non_string_content(nexus_chash_resolver, chash_a):
    fake_t3 = type("T3", (), {})()
    fake_t3.get_by_id = lambda col, doc_id: {"content": {"oops": "structured"}}
    with patch("nexus.mcp_infra.get_t3", return_value=fake_t3), \
         patch("nexus.corpus.t3_collection_name", return_value="knowledge"):
        assert nexus_chash_resolver(chash_a) is None


# ---- render_surface_resource ----------------------------------------------


def test_render_surface_returns_mcp_resource_shape(render_surface_resource, sample_payload):
    fake_t3 = type("T3", (), {})()
    fake_t3.get_by_id = lambda col, doc_id: None  # no chashes resolve
    with patch("nexus.mcp_infra.get_t3", return_value=fake_t3), \
         patch("nexus.corpus.t3_collection_name", return_value="knowledge"):
        result = render_surface_resource(sample_payload)
    assert result["type"] == "resource"
    res = result["resource"]
    assert res["mimeType"] == "text/html"
    assert res["uri"].startswith("ui://nexus/surface/")
    assert "<!DOCTYPE html>" in res["text"]
    # Payload was embedded
    assert '"surfaceId": "t"' in res["text"]


def test_render_surface_substitutes_resolved_chashes(render_surface_resource, sample_payload, chash_a, chash_b):
    db = {chash_a: "resolved alpha text", chash_b: "resolved beta text"}
    fake_t3 = type("T3", (), {})()
    fake_t3.get_by_id = lambda col, doc_id: ({"content": db[doc_id]} if doc_id in db else None)
    with patch("nexus.mcp_infra.get_t3", return_value=fake_t3), \
         patch("nexus.corpus.t3_collection_name", return_value="knowledge"):
        result = render_surface_resource(sample_payload)
    text = result["resource"]["text"]
    # Both resolved chunks present
    assert "resolved alpha text" in text
    assert "resolved beta text" in text
    # Chash IDs gone from the data model
    assert f'"{chash_a}"' not in text
    assert f'"{chash_b}"' not in text
    # openChash rewritten to copyToClipboard
    assert '"copyToClipboard"' in text
    assert '"openChash"' not in text


def test_render_surface_unique_uri_per_call(render_surface_resource, sample_payload):
    fake_t3 = type("T3", (), {})()
    fake_t3.get_by_id = lambda col, doc_id: None
    with patch("nexus.mcp_infra.get_t3", return_value=fake_t3), \
         patch("nexus.corpus.t3_collection_name", return_value="knowledge"):
        r1 = render_surface_resource(sample_payload)
        r2 = render_surface_resource(sample_payload)
    assert r1["resource"]["uri"] != r2["resource"]["uri"]


def test_render_surface_custom_collection(render_surface_resource, sample_payload, chash_a):
    captured_collection = {"value": None}
    fake_t3 = type("T3", (), {})()
    fake_t3.get_by_id = lambda col, doc_id: None

    def fake_col_name(arg, *, t3=None):
        captured_collection["value"] = arg
        return arg

    with patch("nexus.mcp_infra.get_t3", return_value=fake_t3), \
         patch("nexus.corpus.t3_collection_name", side_effect=fake_col_name):
        render_surface_resource(sample_payload, collection="docs")
    assert captured_collection["value"] == "docs"


# ---- render_surface MCP tool wrapper --------------------------------------


def test_tool_accepts_json_string_payload(render_surface_tool, sample_payload):
    fake_t3 = type("T3", (), {})()
    fake_t3.get_by_id = lambda col, doc_id: None
    with patch("nexus.mcp_infra.get_t3", return_value=fake_t3), \
         patch("nexus.corpus.t3_collection_name", return_value="knowledge"):
        result = render_surface_tool(json.dumps(sample_payload))
    assert result["type"] == "resource"
    assert '"surfaceId": "t"' in result["resource"]["text"]


def test_tool_accepts_dict_payload(render_surface_tool, sample_payload):
    fake_t3 = type("T3", (), {})()
    fake_t3.get_by_id = lambda col, doc_id: None
    with patch("nexus.mcp_infra.get_t3", return_value=fake_t3), \
         patch("nexus.corpus.t3_collection_name", return_value="knowledge"):
        result = render_surface_tool(sample_payload)
    assert result["type"] == "resource"


def test_tool_rejects_invalid_json_string(render_surface_tool):
    with pytest.raises(ValueError, match="not valid JSON"):
        render_surface_tool("{not valid json")


def test_tool_rejects_non_dict_non_string(render_surface_tool):
    with pytest.raises(ValueError, match="must be a dict"):
        render_surface_tool(42)
