# SPDX-License-Identifier: AGPL-3.0-or-later
"""nexus-0miq7.7: every registered ``@mcp.tool()`` name appears in
docs/mcp-servers.md.

Companion to ``tests/test_cli_reference_coverage.py`` (the same coverage
question for the CLI's Click tree) and ``tests/test_mcp_wire_shapes.py``'s
registration census (structured-output declaration, not doc coverage) —
this file asserts the third leg: the tool CATALOG page names every tool a
client could actually call.

Pure AST walk of the same two source files ``test_mcp_wire_shapes.py``
already censuses (``src/nexus/mcp/{core,catalog}.py``), no substrate. The
registered NAME is not always the Python function name: ``nexus-catalog``
tools drop the redundant ``catalog_`` prefix via an explicit ``name=``
keyword on the decorator (mcp-servers.md's own stated convention, "No
redundant ``catalog_`` prefix on the short names") — ``catalog_show`` is
registered as ``"show"``, and ``_file_path_matches`` is registered as
``"search"``. Reading the function name alone would report all nine
``nexus-catalog`` tools as undocumented; this walk reads the decorator's
``name=`` literal when present and falls back to the function name
otherwise, exactly as FastMCP itself resolves the registered name.

Coverage is judged by a literal backtick-quoted occurrence anywhere in the
document (``` `tool_name` ```) — the doc uses that convention uniformly,
in tables and in prose (``daemon_uninstall`` is named in prose, not a
table row) — never section-scoped the way the CLI reference check is,
because there is no analogous "same leaf name under two different parent
groups" collision here: MCP tool names are globally unique per server.

Non-vacuity: the walk must find a floor of tools (56 at the time this
test was written: 46 in core.py + 10 in catalog.py), and a planted tool
name absent from the doc reds the check.
"""
from __future__ import annotations

import ast
import pathlib

import pytest

pytestmark = pytest.mark.lint

_REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent
_MCP_SOURCE_FILES = [
    _REPO_ROOT / "src" / "nexus" / "mcp" / "core.py",
    _REPO_ROOT / "src" / "nexus" / "mcp" / "catalog.py",
]
_DOC = _REPO_ROOT / "docs" / "mcp-servers.md"

#: Tool names deliberately absent from the doc's tool catalog, with a
#: reason each. Empty today — every registered tool is documented.
ALLOWLIST: dict[str, str] = {}


def _registered_tool_names(path: pathlib.Path) -> list[str]:
    """Every ``@mcp.tool(...)`` registration's resolved wire name in *path*.

    Resolved name is the decorator's ``name=`` string literal when
    present, else the decorated function's own name — matching FastMCP's
    real resolution order.
    """
    tree = ast.parse(path.read_text(), filename=str(path))
    names: list[str] = []
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        for deco in node.decorator_list:
            if not (
                isinstance(deco, ast.Call)
                and isinstance(deco.func, ast.Attribute)
                and deco.func.attr == "tool"
                and isinstance(deco.func.value, ast.Name)
                and deco.func.value.id == "mcp"
            ):
                continue
            resolved = node.name
            for kw in deco.keywords:
                if kw.arg == "name" and isinstance(kw.value, ast.Constant):
                    resolved = kw.value.value
            names.append(resolved)
    return names


def test_every_mcp_tool_is_named_in_the_doc() -> None:
    doc = _DOC.read_text()
    names: list[str] = []
    for path in _MCP_SOURCE_FILES:
        assert path.is_file(), f"expected MCP tool source file at {path}"
        names.extend(_registered_tool_names(path))

    assert len(names) >= 50, (
        f"walked only {len(names)} registered @mcp.tool() names across "
        f"{[p.name for p in _MCP_SOURCE_FILES]} -- census may be broken "
        "(decorator shape changed?) rather than the tool count actually "
        "having dropped"
    )

    missing = sorted(
        n for n in set(names) if n not in ALLOWLIST and f"`{n}`" not in doc
    )
    assert not missing, (
        f"{len(missing)} MCP tool(s) registered but not named (in backticks) "
        f"anywhere in {_DOC.relative_to(_REPO_ROOT)}; document each or add it "
        f"to ALLOWLIST with a reason:\n  " + "\n  ".join(missing)
    )


def test_allowlist_carries_no_dead_rows() -> None:
    names: set[str] = set()
    for path in _MCP_SOURCE_FILES:
        names.update(_registered_tool_names(path))
    dead = [n for n in ALLOWLIST if n not in names]
    assert not dead, f"ALLOWLIST names tool(s) that no longer exist: {dead}"


def test_a_planted_undocumented_tool_name_is_detected(tmp_path) -> None:
    """The detector reds on a name it has never seen documented."""
    fake_src = tmp_path / "fake_mcp_tools.py"
    fake_src.write_text(
        "@mcp.tool(title='Fake', structured_output=False)\n"
        "def absolutely_undocumented_tool_xyz():\n"
        "    pass\n"
    )
    names = _registered_tool_names(fake_src)
    assert names == ["absolutely_undocumented_tool_xyz"]
    assert "`absolutely_undocumented_tool_xyz`" not in _DOC.read_text()
