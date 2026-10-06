# SPDX-License-Identifier: AGPL-3.0-or-later
"""The doctor's shim checks on Windows (RDR-224, nexus-f9bgu.47).

A Windows shim is a byte copy of the generation's own launcher, so the "shim
contents" row compares against that launcher rather than ``render_shim``'s
shell text (which would flag every Windows shim as a fatal mismatch). The
Windows reading is moved with ``health._win``; the files are real, in tmp.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from nexus import health, install_layout


@pytest.fixture
def windows(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(health, "_win", lambda: "win32")


def _tree(tmp_path: Path) -> tuple[Path, Path]:
    current = tmp_path / "tools" / "gen-A"
    (current / "Scripts").mkdir(parents=True)
    (current / "Scripts" / "nx.exe").write_bytes(b"launcher:nx:A")
    (current / "Scripts" / "nx-mcp.exe").write_bytes(b"launcher:nx-mcp:A")
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    return current, bin_dir


def test_shims_that_copy_the_current_launchers_pass(windows, tmp_path: Path) -> None:
    current, bin_dir = _tree(tmp_path)
    (bin_dir / "nx.exe").write_bytes(b"launcher:nx:A")
    (bin_dir / "nx-mcp.exe").write_bytes(b"launcher:nx-mcp:A")
    [row] = health._check_shims_match_template(current, bin_dir, tmp_path / "tools", {"nx", "nx-mcp"})
    assert row.ok and row.label == "Shim contents"
    assert "2 shim(s)" in row.detail


def test_a_shim_copied_from_another_generation_is_a_fatal_row_naming_it(windows, tmp_path: Path) -> None:
    current, bin_dir = _tree(tmp_path)
    (bin_dir / "nx.exe").write_bytes(b"launcher:nx:A")
    (bin_dir / "nx-mcp.exe").write_bytes(b"launcher:nx-mcp:OLDER")
    [row] = health._check_shims_match_template(current, bin_dir, tmp_path / "tools", {"nx", "nx-mcp"})
    assert not row.ok and row.fatal
    assert "nx-mcp" in row.detail and "nx," not in row.detail
    assert "nx self install" in row.fix_suggestions[0]


def test_the_template_text_is_not_applied_to_windows_shims(windows, tmp_path: Path) -> None:
    """The regression this branch exists for: comparing an exe to render_shim's
    shell script says every Windows shim differs."""
    current, bin_dir = _tree(tmp_path)
    (bin_dir / "nx.exe").write_bytes(b"launcher:nx:A")
    rows = health._check_shims_match_template(current, bin_dir, tmp_path / "tools", {"nx"})
    assert all(row.ok for row in rows)


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
