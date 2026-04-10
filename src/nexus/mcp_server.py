# SPDX-License-Identifier: AGPL-3.0-or-later
"""Backward-compatible shim — imports from nexus.mcp package.

All tool functions and injection helpers remain importable from this module.
"""
from __future__ import annotations

# Core tools (14 registered + 3 demoted + helper)
from nexus.mcp.core import (  # noqa: F401
    search, query,
    store_put, store_get, store_list, store_delete,
    memory_put, memory_get, memory_delete, memory_search, memory_consolidate,
    scratch, scratch_manage,
    collection_list, collection_info, collection_verify,
    plan_save, plan_search,
    _store_list_docs,
    main,
)

# Catalog tools (10 registered + 3 demoted)
from nexus.mcp.catalog import (  # noqa: F401
    catalog_search, catalog_show, catalog_list,
    catalog_register, catalog_update,
    catalog_link, catalog_links, catalog_unlink,
    catalog_link_audit, catalog_link_bulk, catalog_link_query,
    catalog_resolve, catalog_stats,
)

# Injection helpers — tests import these with underscore aliases
from nexus.mcp_infra import (  # noqa: F401
    catalog_auto_link as _catalog_auto_link,
    clear_search_traces as _clear_search_traces,
    get_catalog as _get_catalog,
    get_collection_names as _get_collection_names,
    get_recent_search_traces as _get_recent_search_traces,
    get_t1 as _get_t1,
    get_t3 as _get_t3,
    inject_catalog as _inject_catalog,
    inject_t1 as _inject_t1,
    inject_t3 as _inject_t3,
    record_search_trace as _record_search_trace,
    require_catalog as _require_catalog,
    reset_singletons as _reset_singletons,
    resolve_tumbler_mcp as _resolve_tumbler_mcp,
    t2_ctx as _t2_ctx,
)
