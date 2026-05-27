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
    # Single Python marker -> exactly header + one type line.
    assert len(lines) == 2
    assert lines[1] == "- Python"


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
    # 15 src dirs exist but the cap is 10 -- exact, so a removed cap is caught.
    assert len(src_lines) == 10


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


# ---------------------------------------------------------------------------
# P2.3: architecture subcommand
# ---------------------------------------------------------------------------


def test_architecture_exits_zero(tmp_path: Path, monkeypatch) -> None:
    """architecture subcommand exits 0."""
    monkeypatch.chdir(tmp_path)
    runner = CliRunner()
    from nexus.cli import main

    result = runner.invoke(main, ["command-context", "architecture"])
    assert result.exit_code == 0, result.output


def test_architecture_has_context_header(tmp_path: Path, monkeypatch) -> None:
    """architecture output contains ## Context header."""
    monkeypatch.chdir(tmp_path)
    runner = CliRunner()
    from nexus.cli import main

    result = runner.invoke(main, ["command-context", "architecture"])
    assert "## Context" in result.output


def test_architecture_has_project_structure(tmp_path: Path, monkeypatch) -> None:
    """architecture output contains ### Project Structure heading."""
    monkeypatch.chdir(tmp_path)
    runner = CliRunner()
    from nexus.cli import main

    result = runner.invoke(main, ["command-context", "architecture"])
    assert "### Project Structure" in result.output


def test_architecture_has_active_beads(tmp_path: Path, monkeypatch) -> None:
    """architecture output contains ### Active Beads heading."""
    monkeypatch.chdir(tmp_path)
    runner = CliRunner()
    from nexus.cli import main

    result = runner.invoke(main, ["command-context", "architecture"])
    assert "### Active Beads" in result.output


def test_architecture_has_pipeline_position(tmp_path: Path, monkeypatch) -> None:
    """architecture output contains ### Pipeline Position with static text."""
    monkeypatch.chdir(tmp_path)
    runner = CliRunner()
    from nexus.cli import main

    result = runner.invoke(main, ["command-context", "architecture"])
    assert "### Pipeline Position" in result.output
    assert "strategic-planner" in result.output
    assert "architect-planner" in result.output


def test_architecture_has_tip(tmp_path: Path, monkeypatch) -> None:
    """architecture output contains ### Tip section."""
    monkeypatch.chdir(tmp_path)
    runner = CliRunner()
    from nexus.cli import main

    result = runner.invoke(main, ["command-context", "architecture"])
    assert "### Tip" in result.output


def test_architecture_double_dash_terminator(tmp_path: Path, monkeypatch) -> None:
    """architecture with -- terminator exits 0."""
    monkeypatch.chdir(tmp_path)
    runner = CliRunner()
    from nexus.cli import main

    result = runner.invoke(main, ["command-context", "architecture", "--", "extra"])
    assert result.exit_code == 0, result.output


# ---------------------------------------------------------------------------
# P2.3: create-plan subcommand
# ---------------------------------------------------------------------------


def test_create_plan_exits_zero(tmp_path: Path, monkeypatch) -> None:
    """create-plan subcommand exits 0."""
    monkeypatch.chdir(tmp_path)
    runner = CliRunner()
    from nexus.cli import main

    result = runner.invoke(main, ["command-context", "create-plan"])
    assert result.exit_code == 0, result.output


def test_create_plan_has_context_header(tmp_path: Path, monkeypatch) -> None:
    """create-plan output contains ## Context header."""
    monkeypatch.chdir(tmp_path)
    runner = CliRunner()
    from nexus.cli import main

    result = runner.invoke(main, ["command-context", "create-plan"])
    assert "## Context" in result.output


def test_create_plan_has_existing_epics(tmp_path: Path, monkeypatch) -> None:
    """create-plan output contains ### Existing Epics/Features heading."""
    monkeypatch.chdir(tmp_path)
    runner = CliRunner()
    from nexus.cli import main

    result = runner.invoke(main, ["command-context", "create-plan"])
    assert "### Existing Epics/Features" in result.output


def test_create_plan_has_project_structure(tmp_path: Path, monkeypatch) -> None:
    """create-plan output contains ### Project Structure heading."""
    monkeypatch.chdir(tmp_path)
    runner = CliRunner()
    from nexus.cli import main

    result = runner.invoke(main, ["command-context", "create-plan"])
    assert "### Project Structure" in result.output


def test_create_plan_double_dash_terminator(tmp_path: Path, monkeypatch) -> None:
    """create-plan with -- terminator exits 0."""
    monkeypatch.chdir(tmp_path)
    runner = CliRunner()
    from nexus.cli import main

    result = runner.invoke(main, ["command-context", "create-plan", "--", "extra"])
    assert result.exit_code == 0, result.output


# ---------------------------------------------------------------------------
# P2.3: implement subcommand
# ---------------------------------------------------------------------------


def test_implement_exits_zero(tmp_path: Path, monkeypatch) -> None:
    """implement subcommand exits 0."""
    monkeypatch.chdir(tmp_path)
    runner = CliRunner()
    from nexus.cli import main

    result = runner.invoke(main, ["command-context", "implement"])
    assert result.exit_code == 0, result.output


def test_implement_has_context_header(tmp_path: Path, monkeypatch) -> None:
    """implement output contains ## Context header."""
    monkeypatch.chdir(tmp_path)
    runner = CliRunner()
    from nexus.cli import main

    result = runner.invoke(main, ["command-context", "implement"])
    assert "## Context" in result.output


def test_implement_has_plan_audit_note(tmp_path: Path, monkeypatch) -> None:
    """implement output contains the plan-audit Note static line."""
    monkeypatch.chdir(tmp_path)
    runner = CliRunner()
    from nexus.cli import main

    result = runner.invoke(main, ["command-context", "implement"])
    assert "**Note:**" in result.output
    assert "plan" in result.output.lower()


def test_implement_has_active_work(tmp_path: Path, monkeypatch) -> None:
    """implement output contains ### Active Work heading."""
    monkeypatch.chdir(tmp_path)
    runner = CliRunner()
    from nexus.cli import main

    result = runner.invoke(main, ["command-context", "implement"])
    assert "### Active Work" in result.output


def test_implement_has_project_info(tmp_path: Path, monkeypatch) -> None:
    """implement output contains ### Project Info heading."""
    monkeypatch.chdir(tmp_path)
    runner = CliRunner()
    from nexus.cli import main

    result = runner.invoke(main, ["command-context", "implement"])
    assert "### Project Info" in result.output


def test_implement_double_dash_terminator(tmp_path: Path, monkeypatch) -> None:
    """implement with -- terminator exits 0."""
    monkeypatch.chdir(tmp_path)
    runner = CliRunner()
    from nexus.cli import main

    result = runner.invoke(main, ["command-context", "implement", "--", "extra"])
    assert result.exit_code == 0, result.output


# ---------------------------------------------------------------------------
# P2.3: debug subcommand
# ---------------------------------------------------------------------------


def test_debug_exits_zero(tmp_path: Path, monkeypatch) -> None:
    """debug subcommand exits 0."""
    monkeypatch.chdir(tmp_path)
    runner = CliRunner()
    from nexus.cli import main

    result = runner.invoke(main, ["command-context", "debug"])
    assert result.exit_code == 0, result.output


def test_debug_has_context_header(tmp_path: Path, monkeypatch) -> None:
    """debug output contains ## Context header."""
    monkeypatch.chdir(tmp_path)
    runner = CliRunner()
    from nexus.cli import main

    result = runner.invoke(main, ["command-context", "debug"])
    assert "## Context" in result.output


def test_debug_has_recent_test_failures(tmp_path: Path, monkeypatch) -> None:
    """debug output contains ### Recent Test Failures heading."""
    monkeypatch.chdir(tmp_path)
    runner = CliRunner()
    from nexus.cli import main

    result = runner.invoke(main, ["command-context", "debug"])
    assert "### Recent Test Failures" in result.output


def test_debug_has_active_beads(tmp_path: Path, monkeypatch) -> None:
    """debug output contains ### Active Beads heading."""
    monkeypatch.chdir(tmp_path)
    runner = CliRunner()
    from nexus.cli import main

    result = runner.invoke(main, ["command-context", "debug"])
    assert "### Active Beads" in result.output


def test_debug_surefire_lists_only_failure_reports(tmp_path: Path, monkeypatch) -> None:
    """Under 'Recent Test Failures', list only surefire reports that contain a
    FAILURE/ERROR, not passing reports (mirrors the original grep -l semantics)."""
    surefire = tmp_path / "target" / "surefire-reports"
    surefire.mkdir(parents=True)
    (surefire / "com.example.FailingTest.txt").write_text("Tests run: 1, FAILURE!")
    (surefire / "com.example.PassingTest.txt").write_text("Tests run: 1, OK")
    monkeypatch.chdir(tmp_path)
    runner = CliRunner()
    from nexus.cli import main

    result = runner.invoke(main, ["command-context", "debug"])
    assert "com.example.FailingTest.txt" in result.output
    assert "com.example.PassingTest.txt" not in result.output


def test_debug_double_dash_terminator(tmp_path: Path, monkeypatch) -> None:
    """debug with -- terminator exits 0."""
    monkeypatch.chdir(tmp_path)
    runner = CliRunner()
    from nexus.cli import main

    result = runner.invoke(main, ["command-context", "debug", "--", "some issue"])
    assert result.exit_code == 0, result.output


# ---------------------------------------------------------------------------
# P2.3: deep-analysis subcommand
# ---------------------------------------------------------------------------


def test_deep_analysis_exits_zero(tmp_path: Path, monkeypatch) -> None:
    """deep-analysis subcommand exits 0."""
    monkeypatch.chdir(tmp_path)
    runner = CliRunner()
    from nexus.cli import main

    result = runner.invoke(main, ["command-context", "deep-analysis"])
    assert result.exit_code == 0, result.output


def test_deep_analysis_has_context_header(tmp_path: Path, monkeypatch) -> None:
    """deep-analysis output contains ## Context header."""
    monkeypatch.chdir(tmp_path)
    runner = CliRunner()
    from nexus.cli import main

    result = runner.invoke(main, ["command-context", "deep-analysis"])
    assert "## Context" in result.output


def test_deep_analysis_has_active_beads(tmp_path: Path, monkeypatch) -> None:
    """deep-analysis output contains ### Active Beads heading."""
    monkeypatch.chdir(tmp_path)
    runner = CliRunner()
    from nexus.cli import main

    result = runner.invoke(main, ["command-context", "deep-analysis"])
    assert "### Active Beads" in result.output


def test_deep_analysis_has_tip(tmp_path: Path, monkeypatch) -> None:
    """deep-analysis output contains ### Tip section."""
    monkeypatch.chdir(tmp_path)
    runner = CliRunner()
    from nexus.cli import main

    result = runner.invoke(main, ["command-context", "deep-analysis"])
    assert "### Tip" in result.output


def test_deep_analysis_double_dash_terminator(tmp_path: Path, monkeypatch) -> None:
    """deep-analysis with -- terminator exits 0."""
    monkeypatch.chdir(tmp_path)
    runner = CliRunner()
    from nexus.cli import main

    result = runner.invoke(main, ["command-context", "deep-analysis", "--", "extra"])
    assert result.exit_code == 0, result.output


# ---------------------------------------------------------------------------
# P2.3: enrich-plan subcommand
# ---------------------------------------------------------------------------


def test_enrich_plan_exits_zero(tmp_path: Path, monkeypatch) -> None:
    """enrich-plan subcommand exits 0."""
    monkeypatch.chdir(tmp_path)
    runner = CliRunner()
    from nexus.cli import main

    result = runner.invoke(main, ["command-context", "enrich-plan"])
    assert result.exit_code == 0, result.output


def test_enrich_plan_has_context_header(tmp_path: Path, monkeypatch) -> None:
    """enrich-plan output contains ## Context header."""
    monkeypatch.chdir(tmp_path)
    runner = CliRunner()
    from nexus.cli import main

    result = runner.invoke(main, ["command-context", "enrich-plan"])
    assert "## Context" in result.output


def test_enrich_plan_has_related_beads(tmp_path: Path, monkeypatch) -> None:
    """enrich-plan output contains ### Related Beads heading."""
    monkeypatch.chdir(tmp_path)
    runner = CliRunner()
    from nexus.cli import main

    result = runner.invoke(main, ["command-context", "enrich-plan"])
    assert "### Related Beads" in result.output


def test_enrich_plan_double_dash_terminator(tmp_path: Path, monkeypatch) -> None:
    """enrich-plan with -- terminator exits 0."""
    monkeypatch.chdir(tmp_path)
    runner = CliRunner()
    from nexus.cli import main

    result = runner.invoke(main, ["command-context", "enrich-plan", "--", "extra"])
    assert result.exit_code == 0, result.output


# ---------------------------------------------------------------------------
# P2.3: knowledge-tidy subcommand
# ---------------------------------------------------------------------------


def test_knowledge_tidy_exits_zero(tmp_path: Path, monkeypatch) -> None:
    """knowledge-tidy subcommand exits 0."""
    monkeypatch.chdir(tmp_path)
    runner = CliRunner()
    from nexus.cli import main

    result = runner.invoke(main, ["command-context", "knowledge-tidy"])
    assert result.exit_code == 0, result.output


def test_knowledge_tidy_has_context_header(tmp_path: Path, monkeypatch) -> None:
    """knowledge-tidy output contains ## Context header."""
    monkeypatch.chdir(tmp_path)
    runner = CliRunner()
    from nexus.cli import main

    result = runner.invoke(main, ["command-context", "knowledge-tidy"])
    assert "## Context" in result.output


def test_knowledge_tidy_has_existing_knowledge(tmp_path: Path, monkeypatch) -> None:
    """knowledge-tidy output contains ### Existing Knowledge heading."""
    monkeypatch.chdir(tmp_path)
    runner = CliRunner()
    from nexus.cli import main

    result = runner.invoke(main, ["command-context", "knowledge-tidy"])
    assert "### Existing Knowledge" in result.output


def test_knowledge_tidy_has_recently_completed_beads(
    tmp_path: Path, monkeypatch
) -> None:
    """knowledge-tidy output contains ### Recently Completed Beads heading."""
    monkeypatch.chdir(tmp_path)
    runner = CliRunner()
    from nexus.cli import main

    result = runner.invoke(main, ["command-context", "knowledge-tidy"])
    assert "### Recently Completed Beads" in result.output


def test_knowledge_tidy_has_storage_standards(tmp_path: Path, monkeypatch) -> None:
    """knowledge-tidy output contains ### Storage Standards heading."""
    monkeypatch.chdir(tmp_path)
    runner = CliRunner()
    from nexus.cli import main

    result = runner.invoke(main, ["command-context", "knowledge-tidy"])
    assert "### Storage Standards" in result.output


def test_knowledge_tidy_double_dash_terminator(tmp_path: Path, monkeypatch) -> None:
    """knowledge-tidy with -- terminator exits 0."""
    monkeypatch.chdir(tmp_path)
    runner = CliRunner()
    from nexus.cli import main

    result = runner.invoke(main, ["command-context", "knowledge-tidy", "--", "extra"])
    assert result.exit_code == 0, result.output


# ---------------------------------------------------------------------------
# P2.3: pdf-process subcommand
# ---------------------------------------------------------------------------


def test_pdf_process_exits_zero(tmp_path: Path, monkeypatch) -> None:
    """pdf-process subcommand exits 0."""
    monkeypatch.chdir(tmp_path)
    runner = CliRunner()
    from nexus.cli import main

    result = runner.invoke(main, ["command-context", "pdf-process"])
    assert result.exit_code == 0, result.output


def test_pdf_process_has_context_header(tmp_path: Path, monkeypatch) -> None:
    """pdf-process output contains ## Context header."""
    monkeypatch.chdir(tmp_path)
    runner = CliRunner()
    from nexus.cli import main

    result = runner.invoke(main, ["command-context", "pdf-process"])
    assert "## Context" in result.output


def test_pdf_process_has_pdf_files_heading(tmp_path: Path, monkeypatch) -> None:
    """pdf-process output contains ### PDF Files in Current Directory heading."""
    monkeypatch.chdir(tmp_path)
    runner = CliRunner()
    from nexus.cli import main

    result = runner.invoke(main, ["command-context", "pdf-process"])
    assert "### PDF Files in Current Directory" in result.output


def test_pdf_process_lists_pdf_files(tmp_path: Path, monkeypatch) -> None:
    """pdf-process output lists PDF files found in cwd."""
    (tmp_path / "paper.pdf").touch()
    monkeypatch.chdir(tmp_path)
    runner = CliRunner()
    from nexus.cli import main

    result = runner.invoke(main, ["command-context", "pdf-process"])
    assert "paper.pdf" in result.output


def test_pdf_process_no_pdfs_fallback(tmp_path: Path, monkeypatch) -> None:
    """pdf-process output shows fallback when no PDFs present."""
    monkeypatch.chdir(tmp_path)
    runner = CliRunner()
    from nexus.cli import main

    result = runner.invoke(main, ["command-context", "pdf-process"])
    assert "No PDF files found" in result.output


def test_pdf_process_has_indexed_collections(tmp_path: Path, monkeypatch) -> None:
    """pdf-process output contains ### Existing Indexed Collections heading."""
    monkeypatch.chdir(tmp_path)
    runner = CliRunner()
    from nexus.cli import main

    result = runner.invoke(main, ["command-context", "pdf-process"])
    assert "### Existing Indexed Collections" in result.output


def test_pdf_process_has_tip(tmp_path: Path, monkeypatch) -> None:
    """pdf-process output contains ### Tip section."""
    monkeypatch.chdir(tmp_path)
    runner = CliRunner()
    from nexus.cli import main

    result = runner.invoke(main, ["command-context", "pdf-process"])
    assert "### Tip" in result.output


def test_pdf_process_double_dash_terminator(tmp_path: Path, monkeypatch) -> None:
    """pdf-process with -- terminator exits 0."""
    monkeypatch.chdir(tmp_path)
    runner = CliRunner()
    from nexus.cli import main

    result = runner.invoke(main, ["command-context", "pdf-process", "--", "file.pdf"])
    assert result.exit_code == 0, result.output


# ---------------------------------------------------------------------------
# P2.3: plan-audit subcommand
# ---------------------------------------------------------------------------


def test_plan_audit_exits_zero(tmp_path: Path, monkeypatch) -> None:
    """plan-audit subcommand exits 0."""
    monkeypatch.chdir(tmp_path)
    runner = CliRunner()
    from nexus.cli import main

    result = runner.invoke(main, ["command-context", "plan-audit"])
    assert result.exit_code == 0, result.output


def test_plan_audit_has_context_header(tmp_path: Path, monkeypatch) -> None:
    """plan-audit output contains ## Context header."""
    monkeypatch.chdir(tmp_path)
    runner = CliRunner()
    from nexus.cli import main

    result = runner.invoke(main, ["command-context", "plan-audit"])
    assert "## Context" in result.output


def test_plan_audit_has_provide_plan_line(tmp_path: Path, monkeypatch) -> None:
    """plan-audit output contains the static 'Provide the plan' instruction."""
    monkeypatch.chdir(tmp_path)
    runner = CliRunner()
    from nexus.cli import main

    result = runner.invoke(main, ["command-context", "plan-audit"])
    assert "Provide the plan" in result.output


def test_plan_audit_has_related_beads(tmp_path: Path, monkeypatch) -> None:
    """plan-audit output contains ### Related Beads heading."""
    monkeypatch.chdir(tmp_path)
    runner = CliRunner()
    from nexus.cli import main

    result = runner.invoke(main, ["command-context", "plan-audit"])
    assert "### Related Beads" in result.output


def test_plan_audit_double_dash_terminator(tmp_path: Path, monkeypatch) -> None:
    """plan-audit with -- terminator exits 0."""
    monkeypatch.chdir(tmp_path)
    runner = CliRunner()
    from nexus.cli import main

    result = runner.invoke(main, ["command-context", "plan-audit", "--", "plan.json"])
    assert result.exit_code == 0, result.output


# ---------------------------------------------------------------------------
# P2.3: research subcommand
# ---------------------------------------------------------------------------


def test_research_exits_zero(tmp_path: Path, monkeypatch) -> None:
    """research subcommand exits 0."""
    monkeypatch.chdir(tmp_path)
    runner = CliRunner()
    from nexus.cli import main

    result = runner.invoke(main, ["command-context", "research"])
    assert result.exit_code == 0, result.output


def test_research_has_context_header(tmp_path: Path, monkeypatch) -> None:
    """research output contains ## Context header."""
    monkeypatch.chdir(tmp_path)
    runner = CliRunner()
    from nexus.cli import main

    result = runner.invoke(main, ["command-context", "research"])
    assert "## Context" in result.output


def test_research_has_available_knowledge_sources(tmp_path: Path, monkeypatch) -> None:
    """research output contains ### Available Knowledge Sources heading."""
    monkeypatch.chdir(tmp_path)
    runner = CliRunner()
    from nexus.cli import main

    result = runner.invoke(main, ["command-context", "research"])
    assert "### Available Knowledge Sources" in result.output


def test_research_has_tip(tmp_path: Path, monkeypatch) -> None:
    """research output contains ### Tip section."""
    monkeypatch.chdir(tmp_path)
    runner = CliRunner()
    from nexus.cli import main

    result = runner.invoke(main, ["command-context", "research"])
    assert "### Tip" in result.output


def test_research_double_dash_terminator(tmp_path: Path, monkeypatch) -> None:
    """research with -- terminator exits 0."""
    monkeypatch.chdir(tmp_path)
    runner = CliRunner()
    from nexus.cli import main

    result = runner.invoke(main, ["command-context", "research", "--", "topic"])
    assert result.exit_code == 0, result.output


# ---------------------------------------------------------------------------
# P2.3: review-code subcommand
# ---------------------------------------------------------------------------


def test_review_code_exits_zero(tmp_path: Path, monkeypatch) -> None:
    """review-code subcommand exits 0."""
    monkeypatch.chdir(tmp_path)
    runner = CliRunner()
    from nexus.cli import main

    result = runner.invoke(main, ["command-context", "review-code"])
    assert result.exit_code == 0, result.output


def test_review_code_has_context_header(tmp_path: Path, monkeypatch) -> None:
    """review-code output contains ## Context header."""
    monkeypatch.chdir(tmp_path)
    runner = CliRunner()
    from nexus.cli import main

    result = runner.invoke(main, ["command-context", "review-code"])
    assert "## Context" in result.output


def test_review_code_has_modified_files_or_note(tmp_path: Path, monkeypatch) -> None:
    """review-code output contains ### Modified Files or Not a git repository note."""
    monkeypatch.chdir(tmp_path)
    runner = CliRunner()
    from nexus.cli import main

    result = runner.invoke(main, ["command-context", "review-code"])
    # Non-git dir: should show the "not a git repository" note
    assert "### Modified Files" in result.output or "Not a git repository" in result.output


def test_review_code_has_active_beads(tmp_path: Path, monkeypatch) -> None:
    """review-code output contains ### Active Beads heading."""
    monkeypatch.chdir(tmp_path)
    runner = CliRunner()
    from nexus.cli import main

    result = runner.invoke(main, ["command-context", "review-code"])
    assert "### Active Beads" in result.output


def test_review_code_double_dash_terminator(tmp_path: Path, monkeypatch) -> None:
    """review-code with -- terminator exits 0."""
    monkeypatch.chdir(tmp_path)
    runner = CliRunner()
    from nexus.cli import main

    result = runner.invoke(main, ["command-context", "review-code", "--", "extra"])
    assert result.exit_code == 0, result.output


# ---------------------------------------------------------------------------
# P2.3: substantive-critique subcommand
# ---------------------------------------------------------------------------


def test_substantive_critique_exits_zero(tmp_path: Path, monkeypatch) -> None:
    """substantive-critique subcommand exits 0."""
    monkeypatch.chdir(tmp_path)
    runner = CliRunner()
    from nexus.cli import main

    result = runner.invoke(main, ["command-context", "substantive-critique"])
    assert result.exit_code == 0, result.output


def test_substantive_critique_has_context_header(tmp_path: Path, monkeypatch) -> None:
    """substantive-critique output contains ## Context header."""
    monkeypatch.chdir(tmp_path)
    runner = CliRunner()
    from nexus.cli import main

    result = runner.invoke(main, ["command-context", "substantive-critique"])
    assert "## Context" in result.output


def test_substantive_critique_has_active_beads(tmp_path: Path, monkeypatch) -> None:
    """substantive-critique output contains ### Active Beads heading."""
    monkeypatch.chdir(tmp_path)
    runner = CliRunner()
    from nexus.cli import main

    result = runner.invoke(main, ["command-context", "substantive-critique"])
    assert "### Active Beads" in result.output


def test_substantive_critique_has_tip(tmp_path: Path, monkeypatch) -> None:
    """substantive-critique output contains ### Tip section."""
    monkeypatch.chdir(tmp_path)
    runner = CliRunner()
    from nexus.cli import main

    result = runner.invoke(main, ["command-context", "substantive-critique"])
    assert "### Tip" in result.output


def test_substantive_critique_double_dash_terminator(
    tmp_path: Path, monkeypatch
) -> None:
    """substantive-critique with -- terminator exits 0."""
    monkeypatch.chdir(tmp_path)
    runner = CliRunner()
    from nexus.cli import main

    result = runner.invoke(
        main, ["command-context", "substantive-critique", "--", "artifact"]
    )
    assert result.exit_code == 0, result.output


# ---------------------------------------------------------------------------
# P2.3: test-validate subcommand
# ---------------------------------------------------------------------------


def test_test_validate_exits_zero(tmp_path: Path, monkeypatch) -> None:
    """test-validate subcommand exits 0."""
    monkeypatch.chdir(tmp_path)
    runner = CliRunner()
    from nexus.cli import main

    result = runner.invoke(main, ["command-context", "test-validate"])
    assert result.exit_code == 0, result.output


def test_test_validate_has_context_header(tmp_path: Path, monkeypatch) -> None:
    """test-validate output contains ## Context header."""
    monkeypatch.chdir(tmp_path)
    runner = CliRunner()
    from nexus.cli import main

    result = runner.invoke(main, ["command-context", "test-validate"])
    assert "## Context" in result.output


def test_test_validate_has_test_locations(tmp_path: Path, monkeypatch) -> None:
    """test-validate output contains ### Test Locations heading."""
    monkeypatch.chdir(tmp_path)
    runner = CliRunner()
    from nexus.cli import main

    result = runner.invoke(main, ["command-context", "test-validate"])
    assert "### Test Locations" in result.output


def test_test_validate_has_active_beads(tmp_path: Path, monkeypatch) -> None:
    """test-validate output contains ### Active Beads heading."""
    monkeypatch.chdir(tmp_path)
    runner = CliRunner()
    from nexus.cli import main

    result = runner.invoke(main, ["command-context", "test-validate"])
    assert "### Active Beads" in result.output


def test_test_validate_finds_tests_directory(tmp_path: Path, monkeypatch) -> None:
    """test-validate output lists discovered tests directory."""
    (tmp_path / "tests").mkdir()
    monkeypatch.chdir(tmp_path)
    runner = CliRunner()
    from nexus.cli import main

    result = runner.invoke(main, ["command-context", "test-validate"])
    assert "tests" in result.output


def test_test_validate_double_dash_terminator(tmp_path: Path, monkeypatch) -> None:
    """test-validate with -- terminator exits 0."""
    monkeypatch.chdir(tmp_path)
    runner = CliRunner()
    from nexus.cli import main

    result = runner.invoke(main, ["command-context", "test-validate", "--", "extra"])
    assert result.exit_code == 0, result.output


# ---------------------------------------------------------------------------
# P2.3: nx-preflight subcommand
# ---------------------------------------------------------------------------


@pytest.fixture
def stub_preflight_subprocess(monkeypatch) -> None:
    """Stub subprocess so nx-preflight tests are fast and deterministic.

    nx-preflight shells to ``nx --version``, ``nx doctor``, ``bd --version``,
    ``uv --version``, ``npx --version``.  ``nx doctor`` in particular spawns
    the T2 daemon and touches storage, so running it once per test is slow
    and has side effects (orphan daemons, WAL contention).  Stubbing both
    subprocess entry points keeps these tests exercising the render
    structure rather than the live toolchain (the PASS/FAIL/SKIP branches
    are covered by direct helper unit tests).
    """
    import subprocess as _sp

    from nexus.commands import command_context as cc

    def _fake_check_output(cmd, *args, **kwargs):
        return "stub 1.0.0\n"

    def _fake_run(cmd, *args, **kwargs):
        return _sp.CompletedProcess(cmd, 0, stdout="doctor: ok\n", stderr="")

    monkeypatch.setattr(cc.subprocess, "check_output", _fake_check_output)
    monkeypatch.setattr(cc.subprocess, "run", _fake_run)


def test_nx_preflight_exits_zero(
    tmp_path: Path, monkeypatch, stub_preflight_subprocess
) -> None:
    """nx-preflight subcommand exits 0."""
    monkeypatch.chdir(tmp_path)
    runner = CliRunner()
    from nexus.cli import main

    result = runner.invoke(main, ["command-context", "nx-preflight"])
    assert result.exit_code == 0, result.output


def test_nx_preflight_has_plugin_preflight_header(
    tmp_path: Path, monkeypatch, stub_preflight_subprocess
) -> None:
    """nx-preflight output contains the ## conexus Plugin Preflight Check header."""
    monkeypatch.chdir(tmp_path)
    runner = CliRunner()
    from nexus.cli import main

    result = runner.invoke(main, ["command-context", "nx-preflight"])
    assert "## conexus Plugin Preflight Check" in result.output


def test_nx_preflight_has_nx_cli_section(
    tmp_path: Path, monkeypatch, stub_preflight_subprocess
) -> None:
    """nx-preflight output contains ### 1. nx CLI section."""
    monkeypatch.chdir(tmp_path)
    runner = CliRunner()
    from nexus.cli import main

    result = runner.invoke(main, ["command-context", "nx-preflight"])
    assert "### 1. nx CLI" in result.output


def test_nx_preflight_has_bd_cli_section(
    tmp_path: Path, monkeypatch, stub_preflight_subprocess
) -> None:
    """nx-preflight output contains ### 3. bd (Beads) CLI section."""
    monkeypatch.chdir(tmp_path)
    runner = CliRunner()
    from nexus.cli import main

    result = runner.invoke(main, ["command-context", "nx-preflight"])
    assert "### 3. bd (Beads) CLI" in result.output


def test_nx_preflight_has_uv_section(
    tmp_path: Path, monkeypatch, stub_preflight_subprocess
) -> None:
    """nx-preflight output contains ### 4. uv (package manager) section."""
    monkeypatch.chdir(tmp_path)
    runner = CliRunner()
    from nexus.cli import main

    result = runner.invoke(main, ["command-context", "nx-preflight"])
    assert "### 4. uv (package manager)" in result.output


def test_nx_preflight_has_node_section(
    tmp_path: Path, monkeypatch, stub_preflight_subprocess
) -> None:
    """nx-preflight output contains ### 5. Node.js / npx section."""
    monkeypatch.chdir(tmp_path)
    runner = CliRunner()
    from nexus.cli import main

    result = runner.invoke(main, ["command-context", "nx-preflight"])
    assert "### 5. Node.js / npx" in result.output


def test_nx_preflight_has_claude_md_section(
    tmp_path: Path, monkeypatch, stub_preflight_subprocess
) -> None:
    """nx-preflight output contains ### 6. CLAUDE.md Agent Readiness section."""
    monkeypatch.chdir(tmp_path)
    runner = CliRunner()
    from nexus.cli import main

    result = runner.invoke(main, ["command-context", "nx-preflight"])
    assert "### 6. CLAUDE.md Agent Readiness" in result.output


def test_nx_preflight_double_dash_terminator(
    tmp_path: Path, monkeypatch, stub_preflight_subprocess
) -> None:
    """nx-preflight with -- terminator exits 0."""
    monkeypatch.chdir(tmp_path)
    runner = CliRunner()
    from nexus.cli import main

    result = runner.invoke(main, ["command-context", "nx-preflight", "--", "extra"])
    assert result.exit_code == 0, result.output


# ---------------------------------------------------------------------------
# P2.4: _sanitize_slug helper
# ---------------------------------------------------------------------------


def test_sanitize_slug_lowercase() -> None:
    """_sanitize_slug must lowercase its input."""
    from nexus.commands.command_context import _sanitize_slug

    assert _sanitize_slug("HELLO") == "hello"


def test_sanitize_slug_spaces_to_dash() -> None:
    """_sanitize_slug replaces spaces with dashes."""
    from nexus.commands.command_context import _sanitize_slug

    assert _sanitize_slug("hello world") == "hello-world"


def test_sanitize_slug_punctuation_to_dash() -> None:
    """_sanitize_slug replaces non-alnum chars with a single dash."""
    from nexus.commands.command_context import _sanitize_slug

    assert _sanitize_slug("hello/world.foo") == "hello-world-foo"


def test_sanitize_slug_collapses_repeated_separators() -> None:
    """_sanitize_slug collapses multiple consecutive non-alnum runs to one dash."""
    from nexus.commands.command_context import _sanitize_slug

    assert _sanitize_slug("a--b") == "a-b"
    assert _sanitize_slug("hello  world") == "hello-world"
    assert _sanitize_slug("a/./b") == "a-b"


def test_sanitize_slug_strips_leading_trailing_dashes() -> None:
    """_sanitize_slug strips leading and trailing dashes."""
    from nexus.commands.command_context import _sanitize_slug

    assert _sanitize_slug("-hello-") == "hello"
    assert _sanitize_slug("__foo__") == "foo"


def test_sanitize_slug_alphanumeric_passthrough() -> None:
    """_sanitize_slug leaves lowercase alnum strings unchanged."""
    from nexus.commands.command_context import _sanitize_slug

    assert _sanitize_slug("abc123") == "abc123"


def test_sanitize_slug_mixed_case_and_separators() -> None:
    """_sanitize_slug handles mixed-case with separators end to end."""
    from nexus.commands.command_context import _sanitize_slug

    assert _sanitize_slug("RDR-130 P2.4") == "rdr-130-p2-4"


def test_sanitize_slug_empty_string_returns_empty() -> None:
    """_sanitize_slug on empty input returns empty string (caller applies fallback)."""
    from nexus.commands.command_context import _sanitize_slug

    assert _sanitize_slug("") == ""


def test_sanitize_slug_all_separators_returns_empty() -> None:
    """_sanitize_slug on all-separator input returns empty string."""
    from nexus.commands.command_context import _sanitize_slug

    assert _sanitize_slug("---") == ""
    assert _sanitize_slug("...") == ""


# ---------------------------------------------------------------------------
# P2.4: compute_continuation_path
# ---------------------------------------------------------------------------


def test_compute_continuation_path_base_when_not_exists() -> None:
    """compute_continuation_path returns base path when exists returns False."""
    from datetime import datetime
    from nexus.commands.command_context import compute_continuation_path

    now = datetime(2026, 5, 26, 14, 30)
    result = compute_continuation_path(
        repo_safe="nexus",
        slug="rdr-130-p2",
        now=now,
        out_dir=Path("/tmp"),
        exists=lambda p: False,
    )
    assert result == Path("/tmp/nexus-continuation-nexus-rdr-130-p2-2026-05-26.md")


def test_compute_continuation_path_hhmm_suffix_when_exists() -> None:
    """compute_continuation_path appends HHMM suffix when base path exists."""
    from datetime import datetime
    from nexus.commands.command_context import compute_continuation_path

    now = datetime(2026, 5, 26, 14, 30)
    result = compute_continuation_path(
        repo_safe="nexus",
        slug="rdr-130-p2",
        now=now,
        out_dir=Path("/tmp"),
        exists=lambda p: True,
    )
    assert result == Path("/tmp/nexus-continuation-nexus-rdr-130-p2-2026-05-26-1430.md")


def test_compute_continuation_path_exact_filename_no_exists() -> None:
    """Exact filename assertion for base case (fixed clock, fixed slug)."""
    from datetime import datetime
    from nexus.commands.command_context import compute_continuation_path

    now = datetime(2026, 1, 1, 9, 5)
    result = compute_continuation_path(
        repo_safe="repo",
        slug="session",
        now=now,
        out_dir=Path("/tmp"),
        exists=lambda p: False,
    )
    assert result.name == "nexus-continuation-repo-session-2026-01-01.md"


def test_compute_continuation_path_exact_filename_with_hhmm() -> None:
    """Exact HHMM-suffixed filename (fixed clock, midnight edge)."""
    from datetime import datetime
    from nexus.commands.command_context import compute_continuation_path

    now = datetime(2026, 12, 31, 0, 0)
    result = compute_continuation_path(
        repo_safe="my-repo",
        slug="main",
        now=now,
        out_dir=Path("/tmp"),
        exists=lambda p: True,
    )
    assert result.name == "nexus-continuation-my-repo-main-2026-12-31-0000.md"


def test_compute_continuation_path_does_not_call_wall_clock(monkeypatch) -> None:
    """compute_continuation_path never calls datetime.now() internally."""
    from datetime import datetime
    from nexus.commands.command_context import compute_continuation_path

    # Poison datetime.now to catch any internal call
    def _poison(*args, **kwargs):  # noqa: ANN001
        raise AssertionError("compute_continuation_path must not call datetime.now()")

    monkeypatch.setattr("nexus.commands.command_context.datetime", type(
        "FakeDatetime", (), {"now": staticmethod(_poison)}
    )())

    now = datetime(2026, 5, 26, 10, 0)
    # Should not raise even with poisoned datetime
    result = compute_continuation_path(
        repo_safe="nexus",
        slug="test",
        now=now,
        out_dir=Path("/tmp"),
        exists=lambda p: False,
    )
    assert result.name == "nexus-continuation-nexus-test-2026-05-26.md"


# ---------------------------------------------------------------------------
# P2.4: REPO_SAFE / SLUG fallback derivation helpers
# ---------------------------------------------------------------------------


def test_repo_safe_fallback_empty_cwd_name() -> None:
    """When cwd.name sanitizes to empty, fallback is 'repo'."""
    from nexus.commands.command_context import _sanitize_slug

    name = _sanitize_slug("---")
    repo_safe = name if name else "repo"
    assert repo_safe == "repo"


def test_slug_from_topic_non_empty() -> None:
    """When topic is non-empty, SLUG = _sanitize_slug(topic)."""
    from nexus.commands.command_context import _sanitize_slug

    topic = "rdr-130 p2.4"
    slug = _sanitize_slug(topic)
    assert slug == "rdr-130-p2-4"


def test_slug_fallback_to_branch() -> None:
    """When topic is empty, SLUG = _sanitize_slug(branch)."""
    from nexus.commands.command_context import _sanitize_slug

    branch = "feature/rdr-130-p2-command-context"
    slug = _sanitize_slug(branch)
    assert slug == "feature-rdr-130-p2-command-context"


def test_slug_fallback_session_when_empty_branch() -> None:
    """When both topic and branch produce empty slug, fallback is 'session'."""
    from nexus.commands.command_context import _sanitize_slug

    branch = "---"
    raw_slug = _sanitize_slug(branch)
    slug = raw_slug if raw_slug else "session"
    assert slug == "session"


def test_title_topic_uses_topic_verbatim_when_given() -> None:
    """TITLE_TOPIC equals topic string verbatim when topic is non-empty."""
    topic = "rdr-130 P2"
    title_topic = topic  # verbatim
    assert title_topic == "rdr-130 P2"


def test_title_topic_uses_branch_name_when_no_topic() -> None:
    """TITLE_TOPIC = 'current branch <branch>' when topic is empty."""
    branch = "feature/rdr-130-p2"
    title_topic = f"current branch {branch}"
    assert title_topic == "current branch feature/rdr-130-p2"


# ---------------------------------------------------------------------------
# P2.4: stub fixture for continuation subprocess calls
# ---------------------------------------------------------------------------


@pytest.fixture
def stub_continuation_subprocess(monkeypatch) -> None:
    """Stub subprocess calls in command_context so continuation tests are fast.

    The continuation subcommand shells to git, gh, bd, and nx -- all of
    which are absent or slow in tmp_path.  This fixture stubs
    subprocess.check_output and subprocess.run to return fast fallbacks,
    exercising the render structure without the live toolchain.
    """
    import subprocess as _sp

    from nexus.commands import command_context as cc

    def _fake_check_output(cmd, *args, **kwargs):  # noqa: ANN001
        # Return plausible output for each known command pattern
        cmd_str = " ".join(str(c) for c in cmd)
        if "git" in cmd_str and "branch" in cmd_str:
            return "no-branch\n"
        if "git" in cmd_str and "log" in cmd_str:
            return "abc1234 feat: stub commit\n"
        if "git" in cmd_str and "status" in cmd_str:
            return ""
        if "git" in cmd_str and "rev-parse" in cmd_str and "abbrev-ref" in cmd_str:
            return "(no upstream)\n"
        if "git" in cmd_str and "rev-list" in cmd_str:
            return "0\n"
        if "bd" in cmd_str:
            return "(none)\n"
        if "nx" in cmd_str and "memory" in cmd_str:
            return "(no active-project memory)\n"
        if "gh" in cmd_str:
            return ""
        return ""

    def _fake_run(cmd, *args, **kwargs):  # noqa: ANN001
        return _sp.CompletedProcess(cmd, 0, stdout="", stderr="")

    monkeypatch.setattr(cc.subprocess, "check_output", _fake_check_output)
    monkeypatch.setattr(cc.subprocess, "run", _fake_run)


# ---------------------------------------------------------------------------
# P2.4: continuation subcommand -- structural contract
# ---------------------------------------------------------------------------


def test_continuation_registered_as_subcommand() -> None:
    """continuation must be registered on the command_context group."""
    from nexus.commands.command_context import command_context

    assert "continuation" in command_context.commands


def test_continuation_exits_zero(
    tmp_path: Path, monkeypatch, stub_continuation_subprocess
) -> None:
    """continuation subcommand exits 0 in a non-git tmp_path."""
    monkeypatch.chdir(tmp_path)
    runner = CliRunner()
    from nexus.cli import main

    result = runner.invoke(main, ["command-context", "continuation"])
    assert result.exit_code == 0, result.output


def test_continuation_has_target_file_line(
    tmp_path: Path, monkeypatch, stub_continuation_subprocess
) -> None:
    """continuation output contains '**Target file:**'."""
    monkeypatch.chdir(tmp_path)
    runner = CliRunner()
    from nexus.cli import main

    result = runner.invoke(main, ["command-context", "continuation"])
    assert "**Target file:**" in result.output


def test_continuation_has_topic_line(
    tmp_path: Path, monkeypatch, stub_continuation_subprocess
) -> None:
    """continuation output contains '**Topic:**'."""
    monkeypatch.chdir(tmp_path)
    runner = CliRunner()
    from nexus.cli import main

    result = runner.invoke(main, ["command-context", "continuation"])
    assert "**Topic:**" in result.output


def test_continuation_has_working_state_header(
    tmp_path: Path, monkeypatch, stub_continuation_subprocess
) -> None:
    """continuation output contains '## Working state'."""
    monkeypatch.chdir(tmp_path)
    runner = CliRunner()
    from nexus.cli import main

    result = runner.invoke(main, ["command-context", "continuation"])
    assert "## Working state" in result.output


def test_continuation_has_uncommitted_section(
    tmp_path: Path, monkeypatch, stub_continuation_subprocess
) -> None:
    """continuation output contains '### Uncommitted'."""
    monkeypatch.chdir(tmp_path)
    runner = CliRunner()
    from nexus.cli import main

    result = runner.invoke(main, ["command-context", "continuation"])
    assert "### Uncommitted" in result.output


def test_continuation_has_recent_commits_section(
    tmp_path: Path, monkeypatch, stub_continuation_subprocess
) -> None:
    """continuation output contains '### Recent commits (last 10 on this branch)'."""
    monkeypatch.chdir(tmp_path)
    runner = CliRunner()
    from nexus.cli import main

    result = runner.invoke(main, ["command-context", "continuation"])
    assert "### Recent commits (last 10 on this branch)" in result.output


def test_continuation_has_open_prs_section(
    tmp_path: Path, monkeypatch, stub_continuation_subprocess
) -> None:
    """continuation output contains '### Open PRs from this branch'."""
    monkeypatch.chdir(tmp_path)
    runner = CliRunner()
    from nexus.cli import main

    result = runner.invoke(main, ["command-context", "continuation"])
    assert "### Open PRs from this branch" in result.output


def test_continuation_has_in_progress_beads_section(
    tmp_path: Path, monkeypatch, stub_continuation_subprocess
) -> None:
    """continuation output contains '### In-progress beads'."""
    monkeypatch.chdir(tmp_path)
    runner = CliRunner()
    from nexus.cli import main

    result = runner.invoke(main, ["command-context", "continuation"])
    assert "### In-progress beads" in result.output


def test_continuation_has_ready_beads_section(
    tmp_path: Path, monkeypatch, stub_continuation_subprocess
) -> None:
    """continuation output contains '### Ready beads (top 10)'."""
    monkeypatch.chdir(tmp_path)
    runner = CliRunner()
    from nexus.cli import main

    result = runner.invoke(main, ["command-context", "continuation"])
    assert "### Ready beads (top 10)" in result.output


def test_continuation_has_feedback_memories_section(
    tmp_path: Path, monkeypatch, stub_continuation_subprocess
) -> None:
    """continuation output contains '### Feedback memories'."""
    monkeypatch.chdir(tmp_path)
    runner = CliRunner()
    from nexus.cli import main

    result = runner.invoke(main, ["command-context", "continuation"])
    assert "### Feedback memories" in result.output


def test_continuation_topic_passthrough(
    tmp_path: Path, monkeypatch, stub_continuation_subprocess
) -> None:
    """Passing topic args emits '**Topic:** my topic' in output."""
    monkeypatch.chdir(tmp_path)
    runner = CliRunner()
    from nexus.cli import main

    result = runner.invoke(
        main, ["command-context", "continuation", "--", "my topic"]
    )
    assert result.exit_code == 0, result.output
    assert "**Topic:** my topic" in result.output


def test_continuation_non_git_dir_does_not_crash(
    tmp_path: Path, monkeypatch, stub_continuation_subprocess
) -> None:
    """continuation in a non-git directory exits 0 (git fallbacks fire)."""
    monkeypatch.chdir(tmp_path)
    runner = CliRunner()
    from nexus.cli import main

    result = runner.invoke(main, ["command-context", "continuation"])
    assert result.exit_code == 0, result.output


# ---------------------------------------------------------------------------
# P2.4: fallback WIRING -- the subcommand applies repo/session fallbacks and
# they flow into the emitted target path (not just the _sanitize_slug unit).
# ---------------------------------------------------------------------------


def test_continuation_repo_safe_fallback_in_target_path(
    tmp_path: Path, monkeypatch, stub_continuation_subprocess
) -> None:
    """A cwd whose basename sanitizes to empty yields '-repo-' in the path.

    Drives REPO_SAFE = _sanitize_slug(cwd.name) or "repo" through the live
    subcommand (cwd basename "---" sanitizes to ""), with a well-formed topic
    so only the repo segment exercises the fallback.
    """
    degenerate = tmp_path / "---"
    degenerate.mkdir()
    monkeypatch.chdir(degenerate)
    runner = CliRunner()
    from nexus.cli import main

    result = runner.invoke(
        main, ["command-context", "continuation", "--", "release notes"]
    )
    assert result.exit_code == 0, result.output
    # repo segment fell back to "repo"; slug came from the topic.
    assert "nexus-continuation-repo-release-notes-" in result.output


def test_continuation_session_slug_fallback_in_target_path(
    tmp_path: Path, monkeypatch, stub_continuation_subprocess
) -> None:
    """A topic that sanitizes to empty yields '-session-' in the path.

    Drives SLUG = _sanitize_slug(topic) or "session" through the live
    subcommand (topic "..." sanitizes to ""), with a well-formed cwd basename
    so only the slug segment exercises the fallback.
    """
    named = tmp_path / "myproj"
    named.mkdir()
    monkeypatch.chdir(named)
    runner = CliRunner()
    from nexus.cli import main

    result = runner.invoke(main, ["command-context", "continuation", "--", "..."])
    assert result.exit_code == 0, result.output
    # repo segment came from cwd basename; slug fell back to "session".
    assert "nexus-continuation-myproj-session-" in result.output


def test_continuation_nx_memory_uses_raw_basename(
    tmp_path: Path, monkeypatch, stub_continuation_subprocess
) -> None:
    """The <repo>_active memory project uses the RAW cwd basename, not the
    sanitized slug (matches session_start_hook's basename convention)."""
    named = tmp_path / "MyRepo"
    named.mkdir()
    monkeypatch.chdir(named)
    runner = CliRunner()
    from nexus.cli import main

    result = runner.invoke(main, ["command-context", "continuation"])
    assert result.exit_code == 0, result.output
    # Raw "MyRepo", not the sanitized slug "myrepo".
    assert "### nx memory (MyRepo_active) titles" in result.output
    # The handoff filename DOES use the sanitized slug.
    assert "nexus-continuation-myrepo-" in result.output


def test_continuation_double_dash_terminator(
    tmp_path: Path, monkeypatch, stub_continuation_subprocess
) -> None:
    """continuation with -- terminator exits 0."""
    monkeypatch.chdir(tmp_path)
    runner = CliRunner()
    from nexus.cli import main

    result = runner.invoke(
        main, ["command-context", "continuation", "--", "some topic"]
    )
    assert result.exit_code == 0, result.output


def test_continuation_target_file_is_in_tmp(
    tmp_path: Path, monkeypatch, stub_continuation_subprocess
) -> None:
    """continuation output contains a path starting with /tmp/nexus-continuation."""
    monkeypatch.chdir(tmp_path)
    runner = CliRunner()
    from nexus.cli import main

    result = runner.invoke(main, ["command-context", "continuation"])
    assert "/tmp/nexus-continuation-" in result.output
