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
    result = runner.invoke(enrich, ["bib", "knowledge__test", "--source", "s2"])
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
    result = runner.invoke(enrich, ["bib", "knowledge__test", "--source", "s2"])
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
    result = runner.invoke(enrich, ["bib", "knowledge__test", "--delay", "0", "--source", "s2"])
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
    result = runner.invoke(enrich, ["bib", "knowledge__test", "--delay", "0", "--source", "s2"])
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
    result = runner.invoke(enrich, ["bib", "knowledge__test", "--delay", "0", "--limit", "2", "--source", "s2"])
    assert result.exit_code == 0
    assert "capped at 2" in result.output
    assert mock_bib.call_count == 2


# ── nexus-57mk: --source flag + auto fallback + OpenAlex backend ────────────


@patch("nexus.db.make_t3")
@patch("nexus.bib_enricher_openalex.enrich")
def test_enrich_source_openalex_routes_to_openalex_backend(
    mock_oa: MagicMock, mock_t3_factory: MagicMock,
) -> None:
    """``--source openalex`` calls the OpenAlex enricher and writes
    ``bib_openalex_id`` (not ``bib_semantic_scholar_id``)."""
    mock_oa.return_value = {
        "year": 2024, "venue": "Nature", "authors": "X, Y, Z",
        "citation_count": 5, "openalex_id": "W12345",
        "doi": "10.1/abc", "references": [],
    }
    mock_col = MagicMock()
    mock_col.get.return_value = {
        "ids": ["c1"],
        "metadatas": [{"title": "Paper Z"}],
    }
    mock_db = MagicMock()
    mock_db.get_or_create_collection.return_value = mock_col
    mock_t3_factory.return_value = mock_db

    runner = CliRunner()
    result = runner.invoke(
        enrich,
        ["bib", "knowledge__test", "--delay", "0", "--source", "openalex"],
    )
    assert result.exit_code == 0, result.output
    assert "Backend: openalex" in result.output
    assert "bib_openalex_id" in result.output
    mock_oa.assert_called_once_with("Paper Z")
    # Confirm the chunk update wrote bib_openalex_id, not the S2 ID.
    update_call = mock_col.update.call_args
    metas = update_call.kwargs["metadatas"]
    assert metas[0]["bib_openalex_id"] == "W12345"
    assert "bib_semantic_scholar_id" not in metas[0]


@patch("nexus.db.make_t3")
@patch("nexus.bib_enricher_openalex.enrich")
@patch("nexus.bib_enricher.enrich")
def test_enrich_source_auto_falls_back_to_openalex_without_s2_key(
    mock_s2: MagicMock,
    mock_oa: MagicMock,
    mock_t3_factory: MagicMock,
    monkeypatch,
) -> None:
    """``--source auto`` (or default) routes to OpenAlex when
    ``S2_API_KEY`` is unset. S2 enricher is not called."""
    monkeypatch.delenv("S2_API_KEY", raising=False)
    mock_oa.return_value = {}

    mock_col = MagicMock()
    mock_col.get.return_value = {
        "ids": ["c1"],
        "metadatas": [{"title": "Paper Auto"}],
    }
    mock_db = MagicMock()
    mock_db.get_or_create_collection.return_value = mock_col
    mock_t3_factory.return_value = mock_db

    runner = CliRunner()
    result = runner.invoke(
        enrich, ["bib", "knowledge__test", "--delay", "0"],
    )
    assert result.exit_code == 0
    assert "Backend: openalex" in result.output
    mock_s2.assert_not_called()
    mock_oa.assert_called_once_with("Paper Auto")


@patch("nexus.bib_enricher_openalex.enrich_by_doi")
@patch("nexus.bib_enricher_openalex.enrich")
@patch("nexus.db.make_t3")
def test_enrich_openalex_prefers_doi_over_title_search(
    mock_t3_factory: MagicMock,
    mock_title: MagicMock,
    mock_doi: MagicMock,
) -> None:
    """nexus-sbzr: when chunk text contains a DOI, the enricher must
    look up by DOI directly and skip the fuzzy title search. This
    prevents 'mfaz.pdf' from matching a 1996 Developmental Brain
    Research paper at OpenAlex."""
    mock_doi.return_value = {
        "year": 2024, "venue": "VLDB", "authors": "Author X",
        "citation_count": 7, "openalex_id": "WDOI",
        "doi": "10.1145/X.Y", "references": [],
    }
    mock_col = MagicMock()
    mock_col.get.side_effect = [
        # 1: chunk scan
        {"ids": ["c1"], "metadatas": [{
            "title": "mfaz", "source_path": "/papers/mfaz.pdf",
        }]},
        # 2: first chunk text fetch (for ID extraction)
        {"ids": ["c1"],
         "documents": ["Title. Authors A, B.\nDOI: 10.1145/X.Y\nAbstract..."],
         "metadatas": [{"title": "mfaz", "source_path": "/papers/mfaz.pdf"}]},
        # 3: chunk-merge fetch
        {"ids": ["c1"], "metadatas": [{
            "title": "mfaz", "source_path": "/papers/mfaz.pdf",
        }]},
    ]
    mock_db = MagicMock()
    mock_db.get_or_create_collection.return_value = mock_col
    mock_t3_factory.return_value = mock_db

    runner = CliRunner()
    result = runner.invoke(
        enrich,
        ["bib", "knowledge__test", "--delay", "0", "--source", "openalex"],
    )
    assert result.exit_code == 0, result.output
    mock_doi.assert_called_once_with("10.1145/X.Y")
    mock_title.assert_not_called()
    assert "via DOI/arXiv ID" in result.output


@patch("nexus.bib_enricher_openalex.enrich_by_arxiv_id")
@patch("nexus.bib_enricher_openalex.enrich_by_doi")
@patch("nexus.bib_enricher_openalex.enrich")
@patch("nexus.db.make_t3")
def test_enrich_openalex_falls_back_to_arxiv_when_no_doi(
    mock_t3_factory: MagicMock,
    mock_title: MagicMock,
    mock_doi: MagicMock,
    mock_arxiv: MagicMock,
) -> None:
    """No DOI in text + arXiv-shaped filename -> by-arXiv lookup,
    not title search."""
    mock_doi.return_value = {}
    mock_arxiv.return_value = {
        "year": 2017, "venue": "NeurIPS", "authors": "V et al.",
        "citation_count": 90000, "openalex_id": "WARX",
        "doi": "", "references": [],
    }
    mock_col = MagicMock()
    mock_col.get.side_effect = [
        {"ids": ["c1"], "metadatas": [{
            "title": "Attention", "source_path": "/papers/1706.03762.pdf",
        }]},
        {"ids": ["c1"], "documents": ["Abstract, no DOI here."],
         "metadatas": [{"title": "Attention", "source_path": "/papers/1706.03762.pdf"}]},
        {"ids": ["c1"], "metadatas": [{
            "title": "Attention", "source_path": "/papers/1706.03762.pdf",
        }]},
    ]
    mock_db = MagicMock()
    mock_db.get_or_create_collection.return_value = mock_col
    mock_t3_factory.return_value = mock_db

    runner = CliRunner()
    result = runner.invoke(
        enrich, ["bib", "knowledge__test", "--delay", "0", "--source", "openalex"],
    )
    assert result.exit_code == 0, result.output
    mock_arxiv.assert_called_once_with("1706.03762")
    mock_title.assert_not_called()
    assert "via DOI/arXiv ID" in result.output


@patch("nexus.db.make_t3")
@patch("nexus.bib_enricher.enrich")
@patch("nexus.bib_enricher_openalex.enrich")
def test_enrich_source_auto_uses_s2_when_key_present(
    mock_oa: MagicMock,
    mock_s2: MagicMock,
    mock_t3_factory: MagicMock,
    monkeypatch,
) -> None:
    """``--source auto`` routes to Semantic Scholar when ``S2_API_KEY``
    is set."""
    monkeypatch.setenv("S2_API_KEY", "fake-key")
    mock_s2.return_value = {}

    mock_col = MagicMock()
    mock_col.get.return_value = {
        "ids": ["c1"],
        "metadatas": [{"title": "Paper S2"}],
    }
    mock_db = MagicMock()
    mock_db.get_or_create_collection.return_value = mock_col
    mock_t3_factory.return_value = mock_db

    runner = CliRunner()
    result = runner.invoke(
        enrich, ["bib", "knowledge__test", "--delay", "0"],
    )
    assert result.exit_code == 0
    assert "Backend: s2" in result.output
    mock_oa.assert_not_called()
    mock_s2.assert_called_once_with("Paper S2")
