# SPDX-License-Identifier: AGPL-3.0-or-later
"""nexus-ume6q: the "47 core / 10 catalog" registered-tool counts, wherever
they are written down, must agree with the live registry.

``tests/test_mcp_package.py::test_no_registered_mcp_tool_is_backed_by_a_
private_function`` already asserts a FLOOR (core > 40, catalog > 5) --
enough to catch a collection regression, not enough to catch a doc that
says "46" or "37" instead of the real count (nexus/04wmv-critic-pass-
2026-09-13, Q1). ``tests/test_mcp_servers_doc_coverage.py`` separately
proves every registered NAME appears somewhere in the doc -- also not an
exact count. This file is the exact-count leg: derive the live counts
from the registries themselves, and assert every doc/docstring that
states a numeric tool count agrees with that live number.

Non-vacuity: a doc/docstring change that drifts from the live registry
reds this test naming the exact file and the two numbers that disagree,
rather than a floor silently continuing to pass.
"""
from __future__ import annotations

import pathlib
import re

import pytest

pytestmark = pytest.mark.lint

_REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent


def _live_tool_counts() -> tuple[int, int]:
    """(core count, catalog count) from the actual registered FastMCP
    tool managers -- not a re-parse of source, the same live object the
    MCP client itself would see."""
    from nexus.mcp.catalog import mcp as catalog_mcp
    from nexus.mcp.core import mcp as core_mcp

    return (
        len(core_mcp._tool_manager.list_tools()),
        len(catalog_mcp._tool_manager.list_tools()),
    )


def test_mcp_servers_doc_states_the_live_tool_counts():
    core_count, catalog_count = _live_tool_counts()
    doc = _REPO_ROOT / "docs" / "mcp-servers.md"
    text = doc.read_text()

    table_row = f"| `nexus` | `nx-mcp` | {core_count} |"
    assert table_row in text, (
        f"docs/mcp-servers.md's server table does not contain {table_row!r} "
        f"-- live core registry has {core_count} tools"
    )
    heading = f"## `nexus` — retrieval + storage ({core_count} tools)"
    assert heading in text, (
        f"docs/mcp-servers.md is missing the heading {heading!r} -- live "
        f"core registry has {core_count} tools"
    )
    catalog_heading = f"## `nexus-catalog` — document catalog ({catalog_count} tools)"
    assert catalog_heading in text, (
        f"docs/mcp-servers.md is missing the heading {catalog_heading!r} "
        f"-- live catalog registry has {catalog_count} tools"
    )
    catalog_table_row = f"| `nexus-catalog` | `nx-mcp-catalog` | {catalog_count} |"
    assert catalog_table_row in text, (
        f"docs/mcp-servers.md's server table does not contain "
        f"{catalog_table_row!r} -- live catalog registry has "
        f"{catalog_count} tools"
    )


_DOCSTRING_COUNT_RE = re.compile(r"^(\d+) registered tools? \+ (\d+) demoted", re.MULTILINE)


@pytest.mark.parametrize(
    "module_path",
    [
        _REPO_ROOT / "src" / "nexus" / "mcp" / "core.py",
        _REPO_ROOT / "src" / "nexus" / "mcp" / "catalog.py",
    ],
    ids=["core.py", "catalog.py"],
)
def test_mcp_module_docstring_states_its_own_live_tool_count(module_path):
    core_count, catalog_count = _live_tool_counts()
    want = core_count if module_path.name == "core.py" else catalog_count

    text = module_path.read_text()
    match = _DOCSTRING_COUNT_RE.search(text)
    assert match is not None, (
        f"{module_path.relative_to(_REPO_ROOT)}'s module docstring does not "
        f"contain the expected 'N registered tools + M demoted' line at all"
    )
    stated = int(match.group(1))
    assert stated == want, (
        f"{module_path.relative_to(_REPO_ROOT)}'s module docstring states "
        f"{stated} registered tools, but the live registry has {want}"
    )
