# SPDX-License-Identifier: AGPL-3.0-or-later
"""Verify all CLI command groups are registered in cli.py.

Catches the class of bug where a command file exists in commands/
but is not wired into the main CLI group.
"""
from pathlib import Path

import pytest

COMMANDS_DIR = Path(__file__).parent.parent / "src" / "nexus" / "commands"

# Files that are NOT click command groups (helpers, hidden internals)
_NOT_COMMANDS = {"_helpers.py", "_provision.py", "__init__.py", "__pycache__"}


def _command_modules() -> list[str]:
    """Return the module names of all command files in commands/."""
    return sorted(
        f.stem for f in COMMANDS_DIR.glob("*.py")
        if f.name not in _NOT_COMMANDS and not f.name.startswith("_")
    )


def test_all_command_modules_registered():
    """Every command module in commands/ must be registered in cli.py."""
    from nexus.cli import main

    registered_names = {cmd for cmd in main.commands}
    modules = _command_modules()

    # Map module names to expected CLI command names
    # (some modules use different names via add_command(..., name="X"))
    _MODULE_TO_CLI = {
        "config_cmd": "config",
        "context_cmd": "context",
        "doctor": "doctor",
        "hook": "hook",
        "mineru": "mineru",
        "search_cmd": "search",
        "taxonomy_cmd": "taxonomy",
        "tier_status": "tier-status",
    }

    missing = []
    for mod in modules:
        expected_name = _MODULE_TO_CLI.get(mod, mod)
        if expected_name not in registered_names:
            missing.append(f"{mod} (expected CLI name: {expected_name})")

    assert not missing, (
        f"Command modules not registered in cli.py: {missing}. "
        f"Registered: {sorted(registered_names)}"
    )


def test_taxonomy_subcommands_exist():
    """All 12 taxonomy subcommands must be registered."""
    from nexus.commands.taxonomy_cmd import taxonomy

    expected = {
        "status", "discover", "list", "show", "review", "label",
        "assign", "rename", "merge", "split", "links", "rebuild",
    }
    actual = set(taxonomy.commands)
    missing = expected - actual
    assert not missing, f"Missing taxonomy subcommands: {missing}"


def test_mcp_hooks_registered():
    """Post-store hooks land in their declared chains after install_default_hooks.

    Post-RDR-118-successor refactor: the three hook chains live on
    per-invocation ``HookRegistry`` instances rather than module-level
    globals. ``install_default_hooks(registry)`` wires the load-bearing
    default consumers onto every registry the entry points construct;
    this test pins their order on a fresh registry built the same way.

    RDR-095 + symmetric-fire follow-up: taxonomy + chash dual-write are
    batch-only registrations (the batch hook handles single-document MCP
    events via 1-element batches). Single-doc chain is empty by default;
    future single-doc-only consumers (RDR-089 aspect extraction) will
    add themselves here.
    """
    from nexus.hook_registry import HookRegistry, install_default_hooks

    registry = HookRegistry()
    install_default_hooks(registry)

    batch_names = [h.__name__ for h in registry._batch]
    # RDR-108 D2 (nexus-572g): manifest_write_batch_hook joins the chain
    # so chunk batches landing in T3 update the catalog manifest at the
    # same boundary that chash_dual_write and taxonomy_assign run on.
    assert batch_names == [
        "chash_dual_write_batch_hook",
        "taxonomy_assign_batch_hook",
        "manifest_write_batch_hook",
    ], f"unexpected batch chain order: {batch_names}"

    single_names = [h.__name__ for h in registry._single]
    # Single-doc chain may be empty or carry only future hooks; assert
    # the legacy taxonomy_assign_hook is gone.
    assert "taxonomy_assign_hook" not in single_names


def test_nexus_mcp_tools_registered():
    """All expected MCP tools must be registered on the nexus server."""
    from nexus.mcp.core import mcp

    tools = set(mcp._tool_manager._tools)
    expected = {
        "search", "query", "store_put", "store_get", "store_list",
        "memory_put", "memory_get", "memory_search", "memory_delete",
        "memory_consolidate", "scratch", "scratch_manage",
        "collection_list", "plan_save", "plan_search",
    }
    missing = expected - tools
    assert not missing, f"Missing nexus MCP tools: {missing}"


def test_catalog_mcp_tools_registered():
    """All expected MCP tools must be registered on the catalog server."""
    from nexus.mcp.catalog import mcp

    tools = set(mcp._tool_manager._tools)
    expected = {
        "search", "show", "list", "register", "update",
        "link", "links", "link_query", "resolve", "stats",
    }
    missing = expected - tools
    assert not missing, f"Missing catalog MCP tools: {missing}"


def test_cli_import_enables_line_buffered_stdio():
    """Issue #370: importing nexus.cli must reconfigure stdout/stderr
    to line-buffered so long-running commands flush progress lines
    in non-interactive contexts (background, piped, subprocess).

    Python's default in non-TTY contexts is FULL buffering, which
    swallows 10+ minutes of progress on large repo indexes.
    """
    import sys
    # Re-import is a no-op if already imported, but the side effect
    # (reconfigure) ran once at module load time.
    import nexus.cli  # noqa: F401
    assert sys.stdout.line_buffering, (
        "nexus.cli import must enable line-buffering on stdout"
    )
    assert sys.stderr.line_buffering, (
        "nexus.cli import must enable line-buffering on stderr"
    )
