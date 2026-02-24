# SPDX-License-Identifier: AGPL-3.0-or-later
"""nx index rdr — RDR document discovery and indexing command tests."""
from pathlib import Path
from unittest.mock import patch

import pytest
from click.testing import CliRunner

from nexus.cli import main


@pytest.fixture
def runner() -> CliRunner:
    return CliRunner()


@pytest.fixture
def repo_with_rdrs(tmp_path: Path) -> Path:
    """Create a fake repo with docs/rdr/ containing RDR files and exclusions."""
    rdr_dir = tmp_path / "docs" / "rdr"
    rdr_dir.mkdir(parents=True)

    # Real RDR files
    (rdr_dir / "001-use-sqlite.md").write_text("# RDR-001: Use SQLite\n\nDecision.\n")
    (rdr_dir / "002-adopt-click.md").write_text("# RDR-002: Adopt Click\n\nReasoning.\n")

    # Excluded files
    (rdr_dir / "README.md").write_text("# RDR Index\n")
    (rdr_dir / "TEMPLATE.md").write_text("# Template\n")

    # Subdirectory that should be excluded
    pm_dir = rdr_dir / "post-mortem"
    pm_dir.mkdir()
    (pm_dir / "pm-001.md").write_text("# Post-mortem\n")

    return tmp_path


def test_index_rdr_discovers_markdown_files(
    runner: CliRunner, repo_with_rdrs: Path
) -> None:
    """nx index rdr discovers .md files in docs/rdr/, excluding README.md and TEMPLATE.md."""
    with patch("nexus.doc_indexer.batch_index_markdowns", return_value={}) as mock_batch:
        result = runner.invoke(main, ["index", "rdr", str(repo_with_rdrs)])

    assert result.exit_code == 0, result.output
    mock_batch.assert_called_once()
    paths_arg = mock_batch.call_args[0][0]
    filenames = sorted(p.name for p in paths_arg)
    assert filenames == ["001-use-sqlite.md", "002-adopt-click.md"]


def test_index_rdr_uses_correct_corpus(
    runner: CliRunner, repo_with_rdrs: Path
) -> None:
    """Corpus is rdr__{dirname} so the full collection becomes docs__rdr__{repo}."""
    with patch("nexus.doc_indexer.batch_index_markdowns", return_value={}) as mock_batch:
        result = runner.invoke(main, ["index", "rdr", str(repo_with_rdrs)])

    assert result.exit_code == 0, result.output
    corpus_arg = mock_batch.call_args[0][1]
    assert corpus_arg == f"rdr__{repo_with_rdrs.name}"


def test_index_rdr_no_rdr_dir(runner: CliRunner, tmp_path: Path) -> None:
    """When docs/rdr/ doesn't exist, exit cleanly with informative message."""
    result = runner.invoke(main, ["index", "rdr", str(tmp_path)])

    assert result.exit_code == 0, result.output
    assert "No docs/rdr/ directory found" in result.output


def test_index_rdr_empty_rdr_dir(runner: CliRunner, tmp_path: Path) -> None:
    """When docs/rdr/ exists but has no .md files, report 0."""
    rdr_dir = tmp_path / "docs" / "rdr"
    rdr_dir.mkdir(parents=True)
    # Put a non-md file to prove it's not just empty
    (rdr_dir / "notes.txt").write_text("not markdown")

    with patch("nexus.doc_indexer.batch_index_markdowns") as mock_batch:
        result = runner.invoke(main, ["index", "rdr", str(tmp_path)])

    assert result.exit_code == 0, result.output
    mock_batch.assert_not_called()
    assert "0" in result.output


def test_index_rdr_excludes_postmortem_dir(
    runner: CliRunner, repo_with_rdrs: Path
) -> None:
    """Files in docs/rdr/post-mortem/ subdirectory are not included."""
    with patch("nexus.doc_indexer.batch_index_markdowns", return_value={}) as mock_batch:
        result = runner.invoke(main, ["index", "rdr", str(repo_with_rdrs)])

    assert result.exit_code == 0, result.output
    paths_arg = mock_batch.call_args[0][0]
    for p in paths_arg:
        assert "post-mortem" not in str(p), f"Unexpected subdirectory file: {p}"
