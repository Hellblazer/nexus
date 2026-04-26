# SPDX-License-Identifier: AGPL-3.0-or-later
"""Tests for nx enrich CLI command."""
from __future__ import annotations

from unittest.mock import MagicMock, patch

from click.testing import CliRunner

from nexus.commands.enrich import enrich


@patch("nexus.db.make_t3")
@patch("nexus.bib_enricher.enrich")
def test_enrich_empty_collection(mock_bib: MagicMock, mock_t3_factory: MagicMock) -> None:
    """Empty collection prints message and exits cleanly."""
    mock_col = MagicMock()
    mock_col.get.return_value = {"ids": [], "metadatas": []}
    mock_db = MagicMock()
    mock_db.get_or_create_collection.return_value = mock_col
    mock_t3_factory.return_value = mock_db

    runner = CliRunner()
    result = runner.invoke(enrich, ["bib", "knowledge__test"])
    assert result.exit_code == 0
    assert "is empty" in result.output
    mock_bib.assert_not_called()


@patch("nexus.db.make_t3")
@patch("nexus.bib_enricher.enrich")
def test_enrich_skips_already_enriched(mock_bib: MagicMock, mock_t3_factory: MagicMock) -> None:
    """Chunks with bib_semantic_scholar_id are skipped (idempotency)."""
    mock_col = MagicMock()
    mock_col.get.return_value = {
        "ids": ["c1", "c2"],
        "metadatas": [
            {"title": "Paper A", "bib_semantic_scholar_id": "abc123"},
            {"title": "Paper A", "bib_semantic_scholar_id": "abc123"},
        ],
    }
    mock_db = MagicMock()
    mock_db.get_or_create_collection.return_value = mock_col
    mock_t3_factory.return_value = mock_db

    runner = CliRunner()
    result = runner.invoke(enrich, ["bib", "knowledge__test"])
    assert result.exit_code == 0
    assert "2 already enriched" in result.output
    assert "0 titles to look up" in result.output
    mock_bib.assert_not_called()


@patch("nexus.retry._chroma_with_retry")
@patch("nexus.db.make_t3")
@patch("nexus.bib_enricher.enrich")
def test_enrich_updates_metadata(
    mock_bib: MagicMock, mock_t3_factory: MagicMock, mock_retry: MagicMock
) -> None:
    """Successful enrichment calls col.update with merged metadata."""
    bib_result = {
        "year": 2024,
        "venue": "SIGMOD",
        "authors": "Alice, Bob",
        "citation_count": 42,
        "semantic_scholar_id": "xyz789",
    }
    mock_bib.return_value = bib_result

    mock_col = MagicMock()
    # Initial paginated fetch
    mock_retry.side_effect = [
        # First call: col.get for all chunks
        {"ids": ["c1", "c2"], "metadatas": [
            {"title": "Paper A", "chunk_index": 0},
            {"title": "Paper A", "chunk_index": 1},
        ]},
        # Second call: col.get for specific chunk IDs (re-fetch before update)
        {"ids": ["c1", "c2"], "metadatas": [
            {"title": "Paper A", "chunk_index": 0},
            {"title": "Paper A", "chunk_index": 1},
        ]},
        # Third call: col.update
        None,
    ]
    mock_db = MagicMock()
    mock_db.get_or_create_collection.return_value = mock_col
    mock_t3_factory.return_value = mock_db

    runner = CliRunner()
    result = runner.invoke(enrich, ["bib", "knowledge__test", "--delay", "0"])
    assert result.exit_code == 0
    assert "enriched 2 chunks across 1 titles" in result.output


@patch("nexus.db.make_t3")
@patch("nexus.bib_enricher.enrich")
def test_enrich_no_match_increments_skipped(mock_bib: MagicMock, mock_t3_factory: MagicMock) -> None:
    """When bib_enrich returns {}, the title is counted as skipped."""
    mock_bib.return_value = {}

    mock_col = MagicMock()
    mock_col.get.return_value = {
        "ids": ["c1"],
        "metadatas": [{"title": "Unknown Paper"}],
    }
    mock_db = MagicMock()
    mock_db.get_or_create_collection.return_value = mock_col
    mock_t3_factory.return_value = mock_db

    runner = CliRunner()
    result = runner.invoke(enrich, ["bib", "knowledge__test", "--delay", "0"])
    assert result.exit_code == 0
    assert "1 titles had no Semantic Scholar match" in result.output


@patch("nexus.db.make_t3")
@patch("nexus.bib_enricher.enrich")
def test_enrich_limit_option(mock_bib: MagicMock, mock_t3_factory: MagicMock) -> None:
    """--limit caps the number of titles enriched."""
    mock_bib.return_value = {}

    mock_col = MagicMock()
    mock_col.get.return_value = {
        "ids": ["c1", "c2", "c3"],
        "metadatas": [
            {"title": "Paper A"},
            {"title": "Paper B"},
            {"title": "Paper C"},
        ],
    }
    mock_db = MagicMock()
    mock_db.get_or_create_collection.return_value = mock_col
    mock_t3_factory.return_value = mock_db

    runner = CliRunner()
    result = runner.invoke(enrich, ["bib", "knowledge__test", "--delay", "0", "--limit", "2"])
    assert result.exit_code == 0
    assert "capped at 2" in result.output
    assert mock_bib.call_count == 2
