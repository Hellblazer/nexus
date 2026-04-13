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
        "doctor": "doctor",
        "hook": "hook",
        "mineru": "mineru",
        "search_cmd": "search",
        "taxonomy_cmd": "taxonomy",
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


def test_mcp_hook_registered():
    """taxonomy_assign_hook must be in _post_store_hooks after core import."""
    import nexus.mcp.core  # noqa: F401 — triggers registration
    from nexus.mcp_infra import _post_store_hooks

    hook_names = [h.__name__ for h in _post_store_hooks]
    assert "taxonomy_assign_hook" in hook_names


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
