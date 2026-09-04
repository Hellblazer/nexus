# SPDX-License-Identifier: AGPL-3.0-or-later
"""scripts/sync_sn_serena_tools.py: the sn Serena tool snapshot generator (nexus-jbt5x)."""
from __future__ import annotations

import json
import pathlib
import sys

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[2] / "scripts"))
import sync_sn_serena_tools as sync  # noqa: E402


def _fake_checkout(root: pathlib.Path, *, excluded: list[str]) -> pathlib.Path:
    tools = root / "src" / "serena" / "tools"
    tools.mkdir(parents=True)
    (tools / "tools_base.py").write_text(
        "class Tool(Component):\n    def apply_ex(self):\n        pass\n\n"
        "class ToolMarkerOptional(ToolMarker):\n    pass\n\n"
        "class EditingToolWithDiagnostics(Tool, ToolMarkerCanEdit):\n    ENABLE = False\n"
    )
    (tools / "symbol_tools.py").write_text(
        "class FindSymbolTool(Tool, ToolMarkerSymbolicRead):\n    def apply(self):\n        pass\n\n"
        "class JetBrainsFindSymbolTool(Tool):\n    def apply(self):\n        pass\n\n"
        "class SafeDeleteSymbol(Tool, ToolMarkerSymbolicEdit):\n    def apply(self):\n        pass\n\n"
        "class HelperNotATool:\n    def apply(self):\n        pass\n"
    )
    (tools / "file_tools.py").write_text(
        "class ReadFileTool(Tool):\n    def apply(self):\n        pass\n\n"
        "class SearchForPatternTool(Tool):\n    def apply(self):\n        pass\n\n"
        "class DeleteLinesTool(EditingToolWithDiagnostics, ToolMarkerOptional):\n    def apply(self):\n        pass\n"
    )
    (tools / "config_tools.py").write_text(
        "class ActivateProjectTool(Tool):\n    def apply(self):\n        pass\n\n"
        "class RemoveProjectTool(Tool):\n    def apply(self):\n        pass\n"
    )
    ctx = root / "src" / "serena" / "resources" / "config" / "contexts"
    ctx.mkdir(parents=True)
    (ctx / "claude-code.yml").write_text("excluded_tools:\n" + "".join(f"  - {e}\n" for e in excluded))
    return root


@pytest.mark.parametrize(
    ("cls", "name"),
    [
        ("JetBrainsFindSymbolTool", "jet_brains_find_symbol"),
        ("FindReferencingSymbolsTool", "find_referencing_symbols"),
        ("SerenaInfoTool", "serena_info"),
        ("GetDiagnosticsForFileTool", "get_diagnostics_for_file"),
    ],
)
def test_tool_name_matches_serena_get_name_from_cls(cls: str, name: str) -> None:
    assert sync.tool_name(cls) == name


def test_derive_tools_applies_context_exclusions_and_single_project(tmp_path: pathlib.Path) -> None:
    checkout = _fake_checkout(tmp_path, excluded=["read_file", "search_for_pattern"])
    available, excluded = sync.derive_tools(checkout)
    assert available == ["delete_lines", "find_symbol", "jet_brains_find_symbol", "safe_delete_symbol"], (
        "a suffix-less Tool subclass (SafeDeleteSymbol) and a grandchild through an abstract base "
        "(DeleteLinesTool) are tools; the abstract base, Tool itself, and a non-Tool class with apply are not"
    )
    assert excluded == ["activate_project", "read_file", "remove_project", "search_for_pattern"], (
        "remove_project is in NEVER_APPROVE; the context exposes it, the plugin does not approve it"
    )


def test_derive_tools_refuses_a_tree_that_is_not_serena(tmp_path: pathlib.Path) -> None:
    with pytest.raises(SystemExit):
        sync.derive_tools(tmp_path)


def test_render_and_parse_round_trip(tmp_path: pathlib.Path) -> None:
    text = sync.render("a" * 40, ["find_symbol", "serena_info"], ["read_file"])
    snap = tmp_path / "serena-tools.txt"
    snap.write_text(text)
    assert sync.parse_snapshot(snap) == ("a" * 40, ["find_symbol", "serena_info"], ["read_file"])


def test_serena_pin_refuses_an_unpinned_from_spec(tmp_path: pathlib.Path) -> None:
    mcp = tmp_path / ".mcp.json"
    mcp.write_text(json.dumps({"serena": {"args": ["--from", "git+https://github.com/oraios/serena", "serena"]}}))
    with pytest.raises(SystemExit, match="not pinned"):
        sync.serena_pin(mcp)


def test_serena_pin_reads_url_and_revision(tmp_path: pathlib.Path) -> None:
    mcp = tmp_path / ".mcp.json"
    rev = "0123456789abcdef0123456789abcdef01234567"
    mcp.write_text(json.dumps({"serena": {"args": ["--from", f"git+https://github.com/oraios/serena@{rev}"]}}))
    assert sync.serena_pin(mcp) == ("https://github.com/oraios/serena", rev)


def test_checked_in_snapshot_is_consistent_with_itself() -> None:
    """The real snapshot's header names the real pin, and its body is non-trivial."""
    _, rev = sync.serena_pin()
    snap_rev, available, excluded = sync.parse_snapshot()
    assert snap_rev == rev
    assert "jet_brains_find_symbol" in available
    assert "jet_brains_inline_symbol" in available, "suffix-less class at the pin; the reviewer's regression case"
    assert "safe_delete_symbol" in available
    assert "editing_tool_with_diagnostics" not in available
    assert "search_for_pattern" in excluded
    assert set(sync.NEVER_APPROVE) <= set(excluded)
    assert not (set(available) & set(excluded))
