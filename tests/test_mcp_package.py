# SPDX-License-Identifier: AGPL-3.0-or-later
"""Tests for the nexus.mcp package split (RDR-062).

Verifies that core.py and catalog.py are importable, tools are registered
with the correct FastMCP instances, and demoted functions remain callable
but unregistered.
"""
from __future__ import annotations


def test_core_module_importable():
    """core.py exists and has a FastMCP instance."""
    from nexus.mcp.core import mcp
    assert mcp.name == "nexus"


def test_catalog_module_importable():
    """catalog.py exists and has a FastMCP instance."""
    from nexus.mcp.catalog import mcp
    assert mcp.name == "nexus-catalog"


def test_core_registered_tools():
    """15 core tools are registered with @mcp.tool()."""
    from nexus.mcp.core import mcp

    tool_names = {t.name for t in mcp._tool_manager.list_tools()}
    expected = {
        "search", "query", "store_put", "store_get", "store_list",
        "memory_put", "memory_get", "memory_delete", "memory_search", "memory_consolidate",
        "scratch", "scratch_manage", "collection_list",
        "plan_save", "plan_search",
    }
    assert expected == tool_names, f"Missing: {expected - tool_names}, Extra: {tool_names - expected}"


def test_catalog_registered_tools():
    """10 catalog tools are registered with short names (no catalog_ prefix)."""
    from nexus.mcp.catalog import mcp

    tool_names = {t.name for t in mcp._tool_manager.list_tools()}
    expected = {
        "search", "show", "list", "register",
        "update", "link", "links", "link_query",
        "resolve", "stats",
    }
    assert expected == tool_names, f"Missing: {expected - tool_names}, Extra: {tool_names - expected}"


def test_demoted_core_functions_callable():
    """Demoted core functions are importable and callable (not registered)."""
    from nexus.mcp.core import store_delete, collection_info, collection_verify
    assert callable(store_delete)
    assert callable(collection_info)
    assert callable(collection_verify)


def test_demoted_catalog_functions_callable():
    """Demoted catalog functions are importable and callable (not registered)."""
    from nexus.mcp.catalog import catalog_unlink, catalog_link_audit, catalog_link_bulk
    assert callable(catalog_unlink)
    assert callable(catalog_link_audit)
    assert callable(catalog_link_bulk)


def test_init_reexports_all():
    """__init__.py re-exports every tool and demoted function."""
    import nexus.mcp as pkg

    # Core tools
    for name in [
        "search", "query", "store_put", "store_get", "store_list",
        "memory_put", "memory_get", "memory_delete", "memory_search", "memory_consolidate",
        "scratch", "scratch_manage", "collection_list",
        "plan_save", "plan_search",
        # demoted
        "store_delete", "collection_info", "collection_verify",
    ]:
        assert hasattr(pkg, name), f"Missing re-export: {name}"

    # Catalog tools
    for name in [
        "catalog_search", "catalog_show", "catalog_list", "catalog_register",
        "catalog_update", "catalog_link", "catalog_links", "catalog_link_query",
        "catalog_resolve", "catalog_stats",
        # demoted
        "catalog_unlink", "catalog_link_audit", "catalog_link_bulk",
    ]:
        assert hasattr(pkg, name), f"Missing re-export: {name}"


def test_core_has_main():
    """core.py exposes a main() entry point."""
    from nexus.mcp.core import main
    assert callable(main)


def test_catalog_has_main():
    """catalog.py exposes a main() entry point."""
    from nexus.mcp.catalog import main
    assert callable(main)


def test_helper_moved_with_store_list():
    """_store_list_docs helper is co-located with store_list in core.py."""
    from nexus.mcp.core import _store_list_docs
    assert callable(_store_list_docs)
