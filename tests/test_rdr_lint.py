# SPDX-License-Identifier: AGPL-3.0-or-later
"""nexus-u7ek: lint catches the YAML #-flow-sequence hazard."""
from pathlib import Path

from click.testing import CliRunner

from nexus.commands.rdr import lint, rdr


def test_lint_flags_unquoted_hash_in_flow_sequence(tmp_path: Path):
    bad = tmp_path / "bad.md"
    bad.write_text(
        "---\nprs: [#381, #382, #383]\nstatus: post-mortem\n---\n\nBody\n",
        encoding="utf-8",
    )
    runner = CliRunner()
    result = runner.invoke(rdr, ["lint", str(bad)])
    assert result.exit_code == 1
    assert "unquoted #-ref in YAML flow sequence" in result.output


def test_lint_passes_quoted_refs(tmp_path: Path):
    good = tmp_path / "good.md"
    good.write_text(
        '---\nprs: ["#381", "#382"]\nstatus: accepted\n---\n\nBody\n',
        encoding="utf-8",
    )
    runner = CliRunner()
    result = runner.invoke(rdr, ["lint", str(good)])
    assert result.exit_code == 0, result.output
    assert "clean" in result.output


def test_lint_passes_block_form(tmp_path: Path):
    good = tmp_path / "good.md"
    good.write_text(
        '---\nprs:\n  - "#381"\n  - "#382"\nstatus: accepted\n---\n\nBody\n',
        encoding="utf-8",
    )
    runner = CliRunner()
    result = runner.invoke(rdr, ["lint", str(good)])
    assert result.exit_code == 0, result.output


def test_lint_passes_file_without_frontmatter(tmp_path: Path):
    plain = tmp_path / "plain.md"
    plain.write_text("# Just a heading\n\nNo frontmatter here.\n", encoding="utf-8")
    runner = CliRunner()
    result = runner.invoke(rdr, ["lint", str(plain)])
    assert result.exit_code == 0, result.output


def test_lint_recurses_into_directory(tmp_path: Path):
    sub = tmp_path / "post-mortem"
    sub.mkdir()
    (sub / "bad.md").write_text(
        "---\nprs: [#1, #2]\n---\n", encoding="utf-8",
    )
    (sub / "good.md").write_text(
        "---\ntitle: ok\n---\n", encoding="utf-8",
    )
    runner = CliRunner()
    result = runner.invoke(rdr, ["lint", str(tmp_path)])
    assert result.exit_code == 1
    assert "bad.md" in result.output
    assert "good.md" not in result.output


def test_lint_default_root_missing_exits_2(tmp_path: Path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    runner = CliRunner()
    result = runner.invoke(rdr, ["lint"])
    assert result.exit_code == 2
