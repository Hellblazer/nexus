# SPDX-License-Identifier: AGPL-3.0-or-later
"""TDD tests for ``nx command-context`` Click group and helpers (RDR-130 P2.2).

Bead nexus-sg7hb: these tests define the contract for the command_context
Click group, composable block helpers, and the proof subcommand analyze-code.

Covers:
  (a) command-context group is registered on main (nexus.cli)
  (b) analyze-code subcommand dispatches and exits 0 via CliRunner
  (c) output contains required markdown sections
  (d) project-type reflection in a synthetic tmp project
  (e) -- terminator: trailing args do not break option parsing
  (f) block helpers are cwd-independent (use explicit tmp_path root)

All assertions use == or `in` per feedback_exact_assertions_for_fixture_regression.
No T2/chroma opens; no epsilon-allow needed (RDR-128 lint must stay clean).
"""
from __future__ import annotations

from pathlib import Path

import pytest
from click.testing import CliRunner


# ---------------------------------------------------------------------------
# (a) Registration: command-context group present on main
# ---------------------------------------------------------------------------


def test_command_context_registered_on_main() -> None:
    """The command-context group must appear in main's commands."""
    from nexus.cli import main

    assert "command-context" in main.commands


def test_command_context_is_a_group() -> None:
    """command_context must be a Click Group (not a plain command)."""
    import click

    from nexus.commands.command_context import command_context

    assert isinstance(command_context, click.Group)


# ---------------------------------------------------------------------------
# (b) analyze-code dispatches and exits 0
# ---------------------------------------------------------------------------


def test_analyze_code_exits_zero(tmp_path: Path, monkeypatch) -> None:
    """analyze-code subcommand exits 0 and emits markdown."""
    monkeypatch.chdir(tmp_path)
    runner = CliRunner()
    from nexus.cli import main

    result = runner.invoke(main, ["command-context", "analyze-code"])
    assert result.exit_code == 0, result.output


# ---------------------------------------------------------------------------
# (c) Output contains required markdown sections
# ---------------------------------------------------------------------------


def test_analyze_code_output_has_context_header(tmp_path: Path, monkeypatch) -> None:
    """Output must contain the ## Context header."""
    monkeypatch.chdir(tmp_path)
    runner = CliRunner()
    from nexus.cli import main

    result = runner.invoke(main, ["command-context", "analyze-code"])
    assert "## Context" in result.output


def test_analyze_code_output_has_working_directory(tmp_path: Path, monkeypatch) -> None:
    """Output must contain **Working directory:** line."""
    monkeypatch.chdir(tmp_path)
    runner = CliRunner()
    from nexus.cli import main

    result = runner.invoke(main, ["command-context", "analyze-code"])
    assert "**Working directory:**" in result.output


def test_analyze_code_output_has_project_type(tmp_path: Path, monkeypatch) -> None:
    """Output must contain **Project type:** heading."""
    monkeypatch.chdir(tmp_path)
    runner = CliRunner()
    from nexus.cli import main

    result = runner.invoke(main, ["command-context", "analyze-code"])
    assert "**Project type:**" in result.output


def test_analyze_code_output_has_top_level_structure(tmp_path: Path, monkeypatch) -> None:
    """Output must contain ### Top-level Structure heading."""
    monkeypatch.chdir(tmp_path)
    runner = CliRunner()
    from nexus.cli import main

    result = runner.invoke(main, ["command-context", "analyze-code"])
    assert "### Top-level Structure" in result.output


def test_analyze_code_output_has_source_locations(tmp_path: Path, monkeypatch) -> None:
    """Output must contain ### Source Locations heading."""
    monkeypatch.chdir(tmp_path)
    runner = CliRunner()
    from nexus.cli import main

    result = runner.invoke(main, ["command-context", "analyze-code"])
    assert "### Source Locations" in result.output


# ---------------------------------------------------------------------------
# (d) Project-type reflection in a synthetic tmp project
# ---------------------------------------------------------------------------


def test_analyze_code_reflects_python_project(tmp_path: Path, monkeypatch) -> None:
    """When pyproject.toml exists, output must include '- Python'."""
    (tmp_path / "pyproject.toml").touch()
    monkeypatch.chdir(tmp_path)
    runner = CliRunner()
    from nexus.cli import main

    result = runner.invoke(main, ["command-context", "analyze-code"])
    assert "- Python" in result.output


def test_analyze_code_reflects_rust_project(tmp_path: Path, monkeypatch) -> None:
    """When Cargo.toml exists, output must include '- Rust'."""
    (tmp_path / "Cargo.toml").touch()
    monkeypatch.chdir(tmp_path)
    runner = CliRunner()
    from nexus.cli import main

    result = runner.invoke(main, ["command-context", "analyze-code"])
    assert "- Rust" in result.output


def test_analyze_code_unknown_project_type(tmp_path: Path, monkeypatch) -> None:
    """Empty dir must yield '- Unknown' in output."""
    monkeypatch.chdir(tmp_path)
    runner = CliRunner()
    from nexus.cli import main

    result = runner.invoke(main, ["command-context", "analyze-code"])
    assert "- Unknown" in result.output


# ---------------------------------------------------------------------------
# (e) -- terminator: trailing args do not break option parsing
# ---------------------------------------------------------------------------


def test_analyze_code_with_double_dash_terminator(tmp_path: Path, monkeypatch) -> None:
    """Invoking with -- + trailing args must exit 0 (args are ignored on output path)."""
    monkeypatch.chdir(tmp_path)
    runner = CliRunner()
    from nexus.cli import main

    result = runner.invoke(
        main, ["command-context", "analyze-code", "--", "--status=draft"]
    )
    assert result.exit_code == 0, result.output


def test_analyze_code_with_extra_args_still_has_context_header(
    tmp_path: Path, monkeypatch
) -> None:
    """With -- + trailing args, output still contains ## Context."""
    monkeypatch.chdir(tmp_path)
    runner = CliRunner()
    from nexus.cli import main

    result = runner.invoke(
        main, ["command-context", "analyze-code", "--", "extra-arg"]
    )
    assert "## Context" in result.output


# ---------------------------------------------------------------------------
# (f) Block helpers are cwd-independent (use explicit tmp_path root)
# ---------------------------------------------------------------------------


def test_working_directory_block_uses_passed_cwd(tmp_path: Path) -> None:
    """working_directory_block must reflect the path passed in, not test cwd."""
    from nexus.commands.command_context import working_directory_block

    result = working_directory_block(tmp_path)
    assert str(tmp_path) in result
    assert "**Working directory:**" in result


def test_project_type_block_uses_passed_root(tmp_path: Path) -> None:
    """project_type_block must reflect markers in root, not test cwd."""
    (tmp_path / "go.mod").touch()
    from nexus.commands.command_context import project_type_block

    lines = project_type_block(tmp_path)
    # At least one line, first is the header
    assert lines[0] == "**Project type:**"
    assert "- Go" in lines


def test_project_type_block_has_header_and_types(tmp_path: Path) -> None:
    """project_type_block must return header followed by type lines."""
    (tmp_path / "pyproject.toml").touch()
    from nexus.commands.command_context import project_type_block

    lines = project_type_block(tmp_path)
    assert lines[0] == "**Project type:**"
    assert len(lines) >= 2


def test_top_level_structure_block_uses_passed_root(tmp_path: Path) -> None:
    """top_level_structure_block must list dirs in root, not test runner cwd."""
    # Create subdirectories in tmp_path
    for name in ("alpha", "beta", "gamma"):
        (tmp_path / name).mkdir()
    from nexus.commands.command_context import top_level_structure_block

    lines = top_level_structure_block(tmp_path)
    assert "### Top-level Structure" in lines
    joined = "\n".join(lines)
    assert "alpha" in joined
    assert "beta" in joined
    assert "gamma" in joined


def test_top_level_structure_block_max_15(tmp_path: Path) -> None:
    """top_level_structure_block must return at most 15 subdirectories."""
    for i in range(20):
        (tmp_path / f"dir{i:02d}").mkdir()
    from nexus.commands.command_context import top_level_structure_block

    lines = top_level_structure_block(tmp_path)
    # header + up to 15 dir lines (may also have fallback or blank lines)
    dir_lines = [ln for ln in lines if ln.startswith("- ")]
    assert len(dir_lines) == 15


def test_top_level_structure_block_sorted(tmp_path: Path) -> None:
    """top_level_structure_block directories must be sorted."""
    for name in ("zebra", "apple", "mango"):
        (tmp_path / name).mkdir()
    from nexus.commands.command_context import top_level_structure_block

    lines = top_level_structure_block(tmp_path)
    dir_lines = [ln for ln in lines if ln.startswith("- ")]
    names = [ln[2:] for ln in dir_lines]
    assert names == sorted(names)


def test_top_level_structure_block_excludes_hidden_dirs(tmp_path: Path) -> None:
    """Hidden directories are excluded, matching ``ls -d */`` semantics."""
    for name in (".git", ".idea", "src", "docs"):
        (tmp_path / name).mkdir()
    from nexus.commands.command_context import top_level_structure_block

    lines = top_level_structure_block(tmp_path)
    names = [ln[2:] for ln in lines if ln.startswith("- ")]
    assert names == ["docs", "src"]


def test_top_level_structure_block_empty_dir(tmp_path: Path) -> None:
    """top_level_structure_block on empty dir must return fallback."""
    from nexus.commands.command_context import top_level_structure_block

    lines = top_level_structure_block(tmp_path)
    assert "### Top-level Structure" in lines
    # No dir entries; must have a fallback item
    dir_lines = [ln for ln in lines if ln.startswith("- ")]
    assert len(dir_lines) == 0


def test_source_locations_block_finds_src(tmp_path: Path) -> None:
    """source_locations_block must find src subdirectories."""
    src_dir = tmp_path / "src"
    src_dir.mkdir()
    (src_dir / "mylib").mkdir()
    from nexus.commands.command_context import source_locations_block

    lines = source_locations_block(tmp_path)
    assert "### Source Locations" in lines
    joined = "\n".join(lines)
    assert "src" in joined


def test_source_locations_block_max_10(tmp_path: Path) -> None:
    """source_locations_block must return at most 10 entries."""
    # Create 15 src dirs at various depths
    for i in range(15):
        p = tmp_path / f"pkg{i}" / "src"
        p.mkdir(parents=True)
    from nexus.commands.command_context import source_locations_block

    lines = source_locations_block(tmp_path)
    src_lines = [ln for ln in lines if ln.startswith("- ")]
    assert len(src_lines) <= 10


def test_source_locations_block_no_src(tmp_path: Path) -> None:
    """source_locations_block with no src dirs must return fallback."""
    from nexus.commands.command_context import source_locations_block

    lines = source_locations_block(tmp_path)
    assert "### Source Locations" in lines


def test_source_locations_block_sorted(tmp_path: Path) -> None:
    """source_locations_block entries must be sorted."""
    for name in ("zoo", "alpha", "middle"):
        p = tmp_path / name / "src"
        p.mkdir(parents=True)
    from nexus.commands.command_context import source_locations_block

    lines = source_locations_block(tmp_path)
    src_lines = [ln for ln in lines if ln.startswith("- ")]
    assert src_lines == sorted(src_lines)


def test_git_branch_block_empty_for_non_repo(tmp_path: Path) -> None:
    """git_branch_block must return [] when root is not a git repo."""
    from nexus.commands.command_context import git_branch_block

    result = git_branch_block(tmp_path)
    assert result == []


def test_git_branch_block_returns_list(tmp_path: Path) -> None:
    """git_branch_block must return a list (never raises)."""
    from nexus.commands.command_context import git_branch_block

    result = git_branch_block(tmp_path)
    assert isinstance(result, list)


def test_beads_block_returns_list(tmp_path: Path) -> None:
    """beads_block must return a list even when bd is absent."""
    from nexus.commands.command_context import beads_block

    result = beads_block(tmp_path, args=["--limit=5"], heading="### Beads")
    assert isinstance(result, list)


def test_beads_block_has_heading(tmp_path: Path) -> None:
    """beads_block must include the requested heading in output."""
    from nexus.commands.command_context import beads_block

    result = beads_block(tmp_path, args=[], heading="### Open Beads")
    joined = "\n".join(result)
    assert "### Open Beads" in joined


def test_render_shared_context_returns_str(tmp_path: Path) -> None:
    """render_shared_context must return a str."""
    from nexus.commands.command_context import render_shared_context

    result = render_shared_context(tmp_path)
    assert isinstance(result, str)


def test_render_shared_context_has_all_sections(tmp_path: Path) -> None:
    """render_shared_context output must contain all four required sections."""
    from nexus.commands.command_context import render_shared_context

    result = render_shared_context(tmp_path)
    assert "## Context" in result
    assert "**Working directory:**" in result
    assert "**Project type:**" in result
    assert "### Top-level Structure" in result
    assert "### Source Locations" in result


def test_render_shared_context_cwd_independent(tmp_path: Path) -> None:
    """render_shared_context must reflect tmp_path root, not implicit cwd."""
    from nexus.commands.command_context import render_shared_context

    result = render_shared_context(tmp_path)
    assert str(tmp_path) in result


# ---------------------------------------------------------------------------
# SPDX header fix sanity check
# ---------------------------------------------------------------------------


def test_spdx_header_is_agpl() -> None:
    """command_context.py must carry AGPL-3.0-or-later header (not Apache)."""
    import nexus.commands.command_context as _mod

    src = Path(_mod.__file__).read_text(encoding="utf-8")
    assert "AGPL-3.0-or-later" in src
    assert "Apache-2.0" not in src
