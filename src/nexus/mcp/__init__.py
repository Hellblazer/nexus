# SPDX-License-Identifier: AGPL-3.0-or-later
"""MCP tool package — re-exports all tools for backward compatibility."""
from nexus.mcp.core import (  # noqa: F401
    # Registered tools
    search,
    query,
    store_put,
    store_get,
    store_list,
    memory_put,
    memory_get,
    memory_delete,
    memory_search,
    memory_consolidate,
    scratch,
    scratch_manage,
    collection_list,
    plan_save,
    plan_search,
    operator_extract,
    operator_rank,
    operator_compare,
    operator_summarize,
    operator_generate,
    nx_answer,
    nx_tidy,
    nx_enrich_beads,
    nx_plan_audit,
    # Demoted (plain functions)
    store_delete,
    collection_info,
    collection_verify,
)
from nexus.mcp.catalog import (  # noqa: F401
    # Registered tools
    catalog_search,
    catalog_show,
    catalog_list,
    catalog_register,
    catalog_update,
    catalog_link,
    catalog_links,
    catalog_link_query,
    catalog_resolve,
    catalog_stats,
    # Demoted (plain functions)
    catalog_unlink,
    catalog_link_audit,
    catalog_link_bulk,
)
