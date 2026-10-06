# SPDX-License-Identifier: AGPL-3.0-or-later
"""The doctor's Windows shim row (RDR-224, nexus-f9bgu.47).

Windows nexus writes no launcher into uv's bin dir, so the shim IS a user-PATH
entry: ``<tools>\\current\\bin`` must be on the persisted user PATH ahead of any
other directory that provides ``nx.exe``, and must hold ``nx.exe``. The Windows
reading is moved with ``health._win``; a file stands in for HKCU through the
store seam.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from nexus import health, install_layout
from nexus._install import generation_core as gc


@pytest.fixture
def windows(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(health, "_win", lambda: "win32")


def _bed(tmp_path: Path, *, with_nx: bool = True) -> tuple[Path, Path]:
    tools = tmp_path / "tools"
    gen = tools / "gen-A"
    (gen / "bin").mkdir(parents=True)
    if with_nx:
        (gen / "bin" / "nx.exe").write_bytes(b"nx")
    (tools / "current").symlink_to(gen)
    return tools, tools / "current" / "bin"


def _store(tmp_path: Path, value: str) -> gc.FileUserPath:
    store = gc.FileUserPath(tmp_path / "userpath.txt")
    store.write(value, 2)
    return store


def test_a_user_path_with_current_bin_first_passes(windows, tmp_path: Path) -> None:
    tools, entry = _bed(tmp_path)
    [row] = health._check_windows_user_path(tools, store=_store(tmp_path, f"{entry};C:\\Windows"), environ={})
    assert row.ok and row.label == "User PATH"


def test_a_path_without_the_entry_is_fatal_and_names_the_fix(windows, tmp_path: Path) -> None:
    tools, _entry = _bed(tmp_path)
    [row] = health._check_windows_user_path(tools, store=_store(tmp_path, "C:\\Windows"), environ={})
    assert not row.ok and row.fatal
    assert "is not on the user PATH" in row.detail
    assert "nx self install" in row.fix_suggestions[0]


def test_a_directory_ahead_that_provides_nx_exe_is_fatal_and_named(windows, tmp_path: Path) -> None:
    tools, entry = _bed(tmp_path)
    shadow = tmp_path / "uvbin"
    shadow.mkdir()
    (shadow / "nx.exe").write_bytes(b"uv's")
    [row] = health._check_windows_user_path(tools, store=_store(tmp_path, f"{shadow};{entry}"), environ={})
    assert not row.ok and row.fatal and str(shadow) in row.detail


def test_a_missing_launcher_is_fatal(windows, tmp_path: Path) -> None:
    tools, entry = _bed(tmp_path, with_nx=False)
    [row] = health._check_windows_user_path(tools, store=_store(tmp_path, str(entry)), environ={})
    assert not row.ok and row.fatal and "nx.exe does not exist" in row.detail


def test_an_unreadable_store_warns_rather_than_crashing_doctor(windows, tmp_path: Path) -> None:
    class Broken(gc.UserPathStore):
        kind = "broken"

        def read(self):
            raise RuntimeError("registry offline")

    tools, _entry = _bed(tmp_path)
    [row] = health._check_windows_user_path(tools, store=Broken(), environ={})
    assert not row.ok and row.warn and "registry offline" in row.detail


def test_the_windows_row_replaces_the_template_row_in_the_layout_check(
    windows, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The layout check picks the Windows row on Windows and the template row
    elsewhere; the template text is never compared against a launcher."""
    called: list[str] = []
    monkeypatch.setattr(health, "_check_windows_user_path", lambda tools, **kw: called.append("win") or [])
    monkeypatch.setattr(health, "_check_shims_match_template", lambda *a, **kw: called.append("posix") or [])
    tools, _entry = _bed(tmp_path)
    monkeypatch.setenv("NX_TOOLS_DIR", str(tools))
    monkeypatch.setenv("NX_BIN_DIR", str(tmp_path / "bin"))
    monkeypatch.setattr(install_layout, "owned_shim_names", lambda *a, **kw: frozenset())
    monkeypatch.setattr(install_layout, "current_generation", lambda **kw: tools / "current")
    monkeypatch.setattr(health, "_check_base_interpreters", lambda *a, **kw: [])
    monkeypatch.setattr(health, "_check_orphan_uv_install", lambda: [])
    monkeypatch.setattr(health, "_check_generation_holders", lambda *a, **kw: [])
    install_layout_receipt = tools / "current" / "nexus-install.json"
    install_layout_receipt.write_text("{}")
    health._check_generation_layout()
    assert called == ["win"]
    monkeypatch.setattr(health, "_win", lambda: None)
    called.clear()
    health._check_generation_layout()
    assert called == ["posix"]


def test_posix_still_compares_against_the_rendered_template(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(health, "_win", lambda: None)
    tools = tmp_path / "tools"
    current = tools / "gen-A"
    current.mkdir(parents=True)
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    (bin_dir / "nx").write_text(install_layout.render_shim("nx", tools=tools))
    [good] = health._check_shims_match_template(current, bin_dir, tools, {"nx"})
    assert good.ok
    (bin_dir / "nx").write_text("#!/bin/sh\nexec something-else\n")
    [bad] = health._check_shims_match_template(current, bin_dir, tools, {"nx"})
    assert not bad.ok and bad.fatal
