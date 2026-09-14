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
    """Core tools registered with @mcp.tool() (RDR-088 added filter, check,
    verify; RDR-126 added daemon_uninstall; RDR-156 P4 added the two
    combined-query primitives; RDR-156 Decision 5 added the fourth,
    search_aspect_scoped)."""
    from nexus.mcp.core import mcp

    tool_names = {t.name for t in mcp._tool_manager.list_tools()}
    expected = {
        "search", "query", "store_put", "store_get", "store_list", "store_get_many",
        "memory_put", "memory_get", "memory_delete", "memory_search", "memory_consolidate",
        "scratch", "scratch_manage", "collection_list",
        "plan_save", "plan_search", "plan_delete",
        "traverse",
        "operator_extract", "operator_rank", "operator_compare",
        "operator_summarize", "operator_generate",
        "operator_filter", "operator_check", "operator_verify",
        "operator_groupby", "operator_aggregate",
        "nx_answer", "nx_tidy", "nx_enrich_beads", "nx_plan_audit",
        # RDR-200 P1c (nexus-4e75w.5): the continuation handoff's
        # completion-report half — a SECOND append pairing on
        # continuation_id, never a mutation of the handoff row.
        "nx_answer_report",
        "daemon_uninstall",
        # RDR-156 P4 combined-query primitives (nexus-joesk, nexus-houg9)
        "search_metadata_scoped", "search_topic_scoped", "search_graph_hop",
        # RDR-156 Decision 5 (bead nexus-ubnwk): the fourth combined-query shape.
        "search_aspect_scoped",
        # RDR-182 P3 consent-gated remediation surface (forensics/remediate)
        # DELETED at nexus-lgdel: the chash-rekey upgrade rung it steered
        # operators toward no longer exists.
        # RDR-205 Phase 2 Step 2 (bead nexus-em75s.10): the eight Linda
        # tuple-space tools over HttpTupleStore (nexus-em75s.9). RDR-206
        # Phase 2 (bead nexus-h61dl.9) added the ninth, tuple_renew.
        "tuple_out", "tuple_rd", "tuple_in", "tuple_ack", "tuple_nack",
        "tuple_renew", "tuple_registry", "tuple_list", "tuple_stats",
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


# ── Wire-level registration correctness (nexus-cnzei.1) ──────────────────────
#
# nexus-cnzei.1: commit 4b756c8c7 inserted the private helper
# ``_file_path_matches`` between the ``@mcp.tool(name="search", ...)``
# decorator and ``def catalog_search`` in src/nexus/mcp/catalog.py. Python
# decorator binding is purely positional (the decorator applies to the very
# next ``def``), so the catalog "search" tool silently became
# ``_file_path_matches`` — a two-argument boolean matcher with an
# ``entry_path``/``wanted`` signature — while ``catalog_search`` itself
# (query/content_type/author/corpus/owner/file_path/limit/offset) was never
# registered at all. ``test_catalog_registered_tools`` above only pins the
# *name* "search" is present in the registry; a name collision like this one
# passes that check while every real caller (skills, hooks, ~45 call sites
# passing ``query=``) fails wire-level input validation. These two tests read
# the LIVE FastMCP registry the way an actual MCP client does — resolved
# name, backing function, and JSON schema — so a decorator/def mismatch of
# this shape cannot recur silently.


def test_catalog_search_tool_schema_matches_catalog_search_not_a_private_helper():
    """The nexus-catalog "search" tool's wire schema is catalog_search's,
    not _file_path_matches's.

    Reads the live registry (mirrors what an MCP client sees via
    tools/list): the registered Tool's ``.fn`` must be the real
    ``catalog_search`` function, and its ``inputSchema`` (``.parameters``)
    must expose ``query`` — never ``entry_path``/``wanted``, the
    _file_path_matches signature this bug wired up instead.
    """
    from nexus.mcp.catalog import catalog_search, mcp

    tools = {t.name: t for t in mcp._tool_manager.list_tools()}
    assert "search" in tools, "catalog 'search' tool not registered at all"
    search_tool = tools["search"]

    assert search_tool.fn is catalog_search, (
        f"catalog 'search' tool is backed by {search_tool.fn!r} "
        f"({search_tool.fn.__module__}.{search_tool.fn.__qualname__}), "
        f"not catalog_search — a decorator/def misplacement wired the "
        f"wrong function to the 'search' name (nexus-cnzei.1)"
    )

    props = set(search_tool.parameters.get("properties", {}))
    expected_props = {
        "query", "content_type", "author", "corpus",
        "owner", "file_path", "limit", "offset",
    }
    assert expected_props <= props, (
        f"catalog 'search' tool's inputSchema properties {sorted(props)} "
        f"are missing {sorted(expected_props - props)} — every real caller "
        f"(skills, hooks) passes query=/content_type=/author=/corpus= and "
        f"would fail wire-level input validation against this schema"
    )
    assert "entry_path" not in props and "wanted" not in props, (
        f"catalog 'search' tool's inputSchema still carries "
        f"_file_path_matches's entry_path/wanted parameters: {sorted(props)}"
    )


def test_no_registered_mcp_tool_is_backed_by_a_private_function():
    """Class-level guard: no @mcp.tool() on either server resolves to a
    function whose ``__name__`` starts with "_".

    A private helper landing between a decorator and its intended target
    (as _file_path_matches did) is exactly the failure mode this catches,
    mechanically, for every current and future tool on both servers — not
    just the one instance found by hand. Non-vacuity: the tool counts are
    asserted well above the current registry (47 core / 10 catalog) so a
    collection regression (e.g. an import error silently emptying the
    registry) fails loud rather than passing on an empty set.
    """
    from nexus.mcp.catalog import mcp as catalog_mcp
    from nexus.mcp.core import mcp as core_mcp

    for label, server_mcp, floor in (
        ("core", core_mcp, 40),
        ("catalog", catalog_mcp, 5),
    ):
        tools = server_mcp._tool_manager.list_tools()
        assert len(tools) > floor, (
            f"{label} server registered only {len(tools)} tools (floor "
            f"{floor}) — registry census may be broken rather than the "
            f"tool count actually having dropped"
        )
        private_backed = [
            (t.name, t.fn.__name__) for t in tools if t.fn.__name__.startswith("_")
        ]
        assert not private_backed, (
            f"{label} server: tool(s) registered under a private backing "
            f"function (name -> __name__): {private_backed} — a decorator "
            f"almost certainly landed on the wrong def (nexus-cnzei.1 class "
            f"of bug)"
        )


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
        "search", "query", "store_put", "store_get", "store_list", "store_get_many",
        "memory_put", "memory_get", "memory_delete", "memory_search", "memory_consolidate",
        "scratch", "scratch_manage", "collection_list",
        "plan_save", "plan_search",
        "traverse",
        "operator_extract", "operator_rank", "operator_compare",
        "operator_summarize", "operator_generate",
        "operator_filter", "operator_check", "operator_verify",
        "operator_groupby", "operator_aggregate",
        "nx_answer", "nx_tidy", "nx_enrich_beads", "nx_plan_audit",
        # RDR-205 Phase 2 Step 2 (bead nexus-em75s.10); RDR-206 Phase 2
        # (bead nexus-h61dl.9) added tuple_renew.
        "tuple_out", "tuple_rd", "tuple_in", "tuple_ack", "tuple_nack",
        "tuple_renew", "tuple_registry", "tuple_list", "tuple_stats",
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


# ── Destructive tool safety meta-test (R3-5) ─────────────────────────────────


# Tools that perform bulk / multi-row destructive operations MUST expose a
# dry_run parameter and confirm_destructive gate. Single-row delete tools
# (memory_delete, store_delete) are exempt because they operate on a single
# explicit ID supplied by the caller — the caller already committed to the
# target. Bulk / merge operations need a preview path because the CALLER
# may not realize how many rows will be affected.
_DESTRUCTIVE_BULK_TOOLS = {
    # (module, function_name)
    ("nexus.mcp.core", "memory_consolidate"),           # merge action
    ("nexus.mcp.catalog", "catalog_link_bulk"),         # demoted but still a function
}


def test_destructive_bulk_tools_have_safety_gates():
    """Every bulk/merge destructive tool must expose dry_run + confirm_destructive.

    Regression guard for R3-5: the memory_consolidate(merge) tool was
    originally missing these parameters (caught in round 3 review).
    Any future bulk destructive tool must follow the same pattern:
    - dry_run: bool defaulting to False (opt-in preview)
    - confirm_destructive: bool defaulting to False (opt-in proceed)

    Defaults matter: a tool with dry_run=True by default would silently
    skip all writes; a tool with confirm_destructive=True by default would
    defeat the safety gate entirely.
    """
    import importlib
    import inspect

    for module_name, fn_name in _DESTRUCTIVE_BULK_TOOLS:
        module = importlib.import_module(module_name)
        fn = getattr(module, fn_name)
        sig = inspect.signature(fn)
        params = sig.parameters

        assert "dry_run" in params, (
            f"{module_name}.{fn_name} is a destructive bulk tool but has no "
            f"dry_run parameter — add one (see memory_consolidate or "
            f"catalog_link_bulk for the pattern)"
        )
        assert params["dry_run"].default is False, (
            f"{module_name}.{fn_name}.dry_run default is "
            f"{params['dry_run'].default!r} — must be False (opt-in preview)"
        )

        assert "confirm_destructive" in params, (
            f"{module_name}.{fn_name} is a destructive bulk tool but has no "
            f"confirm_destructive parameter — required to prevent "
            f"accidental multi-row deletes"
        )
        assert params["confirm_destructive"].default is False, (
            f"{module_name}.{fn_name}.confirm_destructive default is "
            f"{params['confirm_destructive'].default!r} — must be False"
        )
