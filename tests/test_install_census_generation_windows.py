# SPDX-License-Identifier: AGPL-3.0-or-later
"""Census attribution through a Windows ledger junction (RDR-224, nexus-f9bgu.47).

``census_core._match_prefix`` and ``legacy_tree_candidates`` asked ``is_symlink``
and looked in ``bin/``. On Windows the registered legacy pointer is a junction
and a venv's executables are in ``Scripts\\``, so a held legacy tree read as
free: the under-reporting direction. ``platform="win32"`` is injected.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from nexus._install import census_core as cc
from tests._module_seam import setattr_in

WIN = "win32"


def test_a_junction_pointer_is_matched_by_its_real_target(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    pointer = tmp_path / "tools" / "gen-legacy-uv-tool"
    pointer.mkdir(parents=True)  # stands in for the junction
    setattr_in(monkeypatch, "nexus._install.layout_core", "os.path.isjunction", lambda p: Path(p) == pointer, raising=False)
    setattr_in(monkeypatch, "nexus._install.layout_core", "os.readlink", lambda p, **kw: "\\\\?\\C:\\Users\\Sam\\AppData\\Roaming\\uv\\tools\\conexus")
    assert cc._match_prefix(pointer, WIN) == "C:/Users/Sam/AppData/Roaming/uv/tools/conexus/"


def test_a_holder_running_from_the_real_legacy_tree_is_found_through_the_pointer(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    pointer = tmp_path / "tools" / "gen-legacy-uv-tool"
    pointer.mkdir(parents=True)
    setattr_in(monkeypatch, "nexus._install.layout_core", "os.path.isjunction", lambda p: Path(p) == pointer, raising=False)
    setattr_in(monkeypatch, "nexus._install.layout_core", "os.readlink", lambda p, **kw: "\\\\?\\C:\\uv\\tools\\conexus")
    snapshot = "777 C:/uv/tools/conexus/Scripts/python.exe -m nexus.mcp\n888 C:/other/x.exe\n"
    assert cc.generation_holder_pids(pointer, snapshot, platform=WIN) == [777]


def test_without_the_junction_reading_the_pointer_itself_is_matched_and_nobody_holds_it(
    tmp_path: Path,
) -> None:
    """The bug this fixes: a plain directory pointer matches its own path."""
    pointer = tmp_path / "tools" / "gen-legacy-uv-tool"
    pointer.mkdir(parents=True)
    snapshot = "777 C:/uv/tools/conexus/Scripts/python.exe\n"
    assert cc.generation_holder_pids(pointer, snapshot, platform=WIN) == []


def test_legacy_candidates_look_in_scripts_on_windows(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    tools = tmp_path / "tools"
    tools.mkdir()
    venv = tmp_path / "uv" / "conexus"
    (venv / "Scripts").mkdir(parents=True)
    (tools / "gen-legacy-uv-tool").symlink_to(venv)
    monkeypatch.setenv("UV_TOOL_DIR", str(tmp_path / "elsewhere"))
    assert cc.legacy_tree_candidates(tools=tools, platform=WIN) == [venv]
    # A POSIX reading of the same tree finds no bin/ and so no legacy tree.
    assert cc.legacy_tree_candidates(tools=tools, platform="linux") == []


def test_the_unregistered_uv_tree_is_found_under_the_windows_tool_root(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    tools = tmp_path / "tools"
    tools.mkdir()
    root = tmp_path / "uvtools"
    (root / "conexus" / "Scripts").mkdir(parents=True)
    monkeypatch.setenv("UV_TOOL_DIR", str(root))
    assert cc.legacy_tree_candidates(tools=tools, platform=WIN) == [root / "conexus"]
