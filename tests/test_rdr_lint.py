# SPDX-License-Identifier: AGPL-3.0-or-later
"""nexus-u7ek: lint catches the YAML #-flow-sequence hazard."""
from pathlib import Path

from click.testing import CliRunner

from nexus.commands.rdr import rdr


def _runner() -> CliRunner:
    # Click 9+ separates stdout and stderr by default — ``result.stdout``
    # and ``result.stderr`` are distinct streams. Findings go to stderr;
    # the "clean" success line goes to stdout.
    return CliRunner()


def test_lint_flags_unquoted_hash_in_flow_sequence(tmp_path: Path):
    bad = tmp_path / "bad.md"
    bad.write_text(
        "---\nprs: [#381, #382, #383]\nstatus: post-mortem\n---\n\nBody\n",
        encoding="utf-8",
    )
    result = _runner().invoke(rdr, ["lint", str(bad)])
    assert result.exit_code == 1
    assert "unquoted #-ref in YAML flow sequence" in result.stderr


def test_lint_reports_correct_line_number(tmp_path: Path):
    """Review #756: the offender is on file line 2 (line 1 is ``---``);
    the previous implementation reported line 3 because the leading ``\\n``
    after the opening fence consumed an enumerate slot."""
    bad = tmp_path / "bad.md"
    bad.write_text(
        "---\nprs: [#381, #382]\nstatus: x\n---\n\nBody\n",
        encoding="utf-8",
    )
    result = _runner().invoke(rdr, ["lint", str(bad)])
    assert result.exit_code == 1
    assert f"{bad}:2:" in result.stderr, (
        f"expected offender at file line 2; stderr was:\n{result.stderr}"
    )


def test_lint_does_not_flag_yaml_comment_lines(tmp_path: Path):
    """Review #756: ``# note: [#381]`` is a YAML comment, not the hazard.
    The regex matches it, so the lint must skip lines whose first
    non-whitespace char is ``#``."""
    benign = tmp_path / "benign.md"
    benign.write_text(
        "---\n# note: [#381] is just a comment\ntitle: ok\n---\n\nBody\n",
        encoding="utf-8",
    )
    result = _runner().invoke(rdr, ["lint", str(benign)])
    assert result.exit_code == 0, (
        f"comment line falsely flagged; stderr was:\n{result.stderr}"
    )


def test_lint_catches_multiline_flow_sequence(tmp_path: Path):
    """Review #756: multi-line flow sequences (``prs: [\\n  #381,\\n]``)
    parse silently into empty lists in PyYAML — yaml.safe_load is not a
    safety net for them. The regex must span lines so the hazard is
    caught at the source."""
    bad = tmp_path / "multiline.md"
    bad.write_text(
        "---\nprs: [\n  #381,\n  #382,\n]\nstatus: x\n---\n\nBody\n",
        encoding="utf-8",
    )
    result = _runner().invoke(rdr, ["lint", str(bad)])
    assert result.exit_code == 1
    assert "unquoted #-ref in YAML flow sequence" in result.stderr
    # The opener `prs: [` is on file line 2.
    assert f"{bad}:2:" in result.stderr


def test_lint_passes_quoted_refs(tmp_path: Path):
    good = tmp_path / "good.md"
    good.write_text(
        '---\nprs: ["#381", "#382"]\nstatus: accepted\n---\n\nBody\n',
        encoding="utf-8",
    )
    result = _runner().invoke(rdr, ["lint", str(good)])
    assert result.exit_code == 0, result.stderr
    assert "clean" in result.stdout


def test_lint_passes_block_form(tmp_path: Path):
    good = tmp_path / "good.md"
    good.write_text(
        '---\nprs:\n  - "#381"\n  - "#382"\nstatus: accepted\n---\n\nBody\n',
        encoding="utf-8",
    )
    result = _runner().invoke(rdr, ["lint", str(good)])
    assert result.exit_code == 0, result.stderr


def test_lint_passes_file_without_frontmatter(tmp_path: Path):
    plain = tmp_path / "plain.md"
    plain.write_text("# Just a heading\n\nNo frontmatter here.\n", encoding="utf-8")
    result = _runner().invoke(rdr, ["lint", str(plain)])
    assert result.exit_code == 0, result.stderr


def test_lint_recurses_into_directory(tmp_path: Path):
    sub = tmp_path / "post-mortem"
    sub.mkdir()
    (sub / "bad.md").write_text(
        "---\nprs: [#1, #2]\n---\n", encoding="utf-8",
    )
    (sub / "good.md").write_text(
        "---\ntitle: ok\n---\n", encoding="utf-8",
    )
    result = _runner().invoke(rdr, ["lint", str(tmp_path)])
    assert result.exit_code == 1
    assert "bad.md" in result.stderr
    assert "good.md" not in result.stderr
    # Summary distinguishes files-with-findings from total scanned.
    assert "1 of 2 file(s)" in result.stderr


def test_lint_default_root_missing_exits_2(tmp_path: Path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    result = _runner().invoke(rdr, ["lint"])
    assert result.exit_code == 2
