# SPDX-License-Identifier: AGPL-3.0-or-later
"""Tests for nx enrich CLI command."""
from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from click.testing import CliRunner

from nexus.catalog.catalog import Catalog
from nexus.catalog.tumbler import Tumbler
from nexus.commands.enrich import enrich
from nexus.db.http_vector_client import HttpVectorClient
from nexus.db.t3 import T3Database


@patch("nexus.db.make_t3")
@patch("nexus.bib_enricher.enrich")
def test_enrich_empty_collection(mock_bib: MagicMock, mock_t3_factory: MagicMock) -> None:
    """Empty collection prints message and exits cleanly."""
    mock_col = MagicMock()
    mock_col.get.return_value = {"ids": [], "metadatas": []}
    # make_t3() returns the service-backed HttpVectorClient
    # unconditionally in production since RDR-155 P4a.2.
    # get_or_create_collection() is a direct call on both handles, and
    # _chroma_with_retry (patched where used) or the collection mock
    # itself absorbs the rest, so spec=HttpVectorClient pins the real
    # return type without changing behavior.
    mock_db = MagicMock(spec=HttpVectorClient)
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
    # make_t3() returns the service-backed HttpVectorClient
    # unconditionally in production since RDR-155 P4a.2.
    # get_or_create_collection() is a direct call on both handles, and
    # _chroma_with_retry (patched where used) or the collection mock
    # itself absorbs the rest, so spec=HttpVectorClient pins the real
    # return type without changing behavior.
    mock_db = MagicMock(spec=HttpVectorClient)
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
    # make_t3() returns the service-backed HttpVectorClient
    # unconditionally in production since RDR-155 P4a.2.
    # get_or_create_collection() is a direct call on both handles, and
    # _chroma_with_retry (patched where used) or the collection mock
    # itself absorbs the rest, so spec=HttpVectorClient pins the real
    # return type without changing behavior.
    mock_db = MagicMock(spec=HttpVectorClient)
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
    # make_t3() returns the service-backed HttpVectorClient
    # unconditionally in production since RDR-155 P4a.2.
    # get_or_create_collection() is a direct call on both handles, and
    # _chroma_with_retry (patched where used) or the collection mock
    # itself absorbs the rest, so spec=HttpVectorClient pins the real
    # return type without changing behavior.
    mock_db = MagicMock(spec=HttpVectorClient)
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
    # make_t3() returns the service-backed HttpVectorClient
    # unconditionally in production since RDR-155 P4a.2.
    # get_or_create_collection() is a direct call on both handles, and
    # _chroma_with_retry (patched where used) or the collection mock
    # itself absorbs the rest, so spec=HttpVectorClient pins the real
    # return type without changing behavior.
    mock_db = MagicMock(spec=HttpVectorClient)
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
    # make_t3() returns the service-backed HttpVectorClient
    # unconditionally in production since RDR-155 P4a.2.
    # get_or_create_collection() is a direct call on both handles, and
    # _chroma_with_retry (patched where used) or the collection mock
    # itself absorbs the rest, so spec=HttpVectorClient pins the real
    # return type without changing behavior.
    mock_db = MagicMock(spec=HttpVectorClient)
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
    # make_t3() returns the service-backed HttpVectorClient
    # unconditionally in production since RDR-155 P4a.2.
    # get_or_create_collection() is a direct call on both handles, and
    # _chroma_with_retry (patched where used) or the collection mock
    # itself absorbs the rest, so spec=HttpVectorClient pins the real
    # return type without changing behavior.
    mock_db = MagicMock(spec=HttpVectorClient)
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
    # make_t3() returns the service-backed HttpVectorClient
    # unconditionally in production since RDR-155 P4a.2.
    # get_or_create_collection() is a direct call on both handles, and
    # _chroma_with_retry (patched where used) or the collection mock
    # itself absorbs the rest, so spec=HttpVectorClient pins the real
    # return type without changing behavior.
    mock_db = MagicMock(spec=HttpVectorClient)
    mock_db.get_or_create_collection.return_value = mock_col
    mock_t3_factory.return_value = mock_db

    runner = CliRunner()
    result = runner.invoke(
        enrich,
        ["bib", "knowledge__test", "--delay", "0", "--source", "openalex"],
    )
    assert result.exit_code == 0, result.output
    # nexus-yy1m: caller passes the source title as expected_title so
    # the OpenAlex backend can reject citation-DOI poisoning.
    mock_doi.assert_called_once_with("10.1145/X.Y", expected_title="mfaz")
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
    # make_t3() returns the service-backed HttpVectorClient
    # unconditionally in production since RDR-155 P4a.2.
    # get_or_create_collection() is a direct call on both handles, and
    # _chroma_with_retry (patched where used) or the collection mock
    # itself absorbs the rest, so spec=HttpVectorClient pins the real
    # return type without changing behavior.
    mock_db = MagicMock(spec=HttpVectorClient)
    mock_db.get_or_create_collection.return_value = mock_col
    mock_t3_factory.return_value = mock_db

    runner = CliRunner()
    result = runner.invoke(
        enrich, ["bib", "knowledge__test", "--delay", "0", "--source", "openalex"],
    )
    assert result.exit_code == 0, result.output
    # nexus-yy1m: caller passes the source title as expected_title.
    mock_arxiv.assert_called_once_with("1706.03762", expected_title="Attention")
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
    # make_t3() returns the service-backed HttpVectorClient
    # unconditionally in production since RDR-155 P4a.2.
    # get_or_create_collection() is a direct call on both handles, and
    # _chroma_with_retry (patched where used) or the collection mock
    # itself absorbs the rest, so spec=HttpVectorClient pins the real
    # return type without changing behavior.
    mock_db = MagicMock(spec=HttpVectorClient)
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


# ── nexus-9l2lg: --backfill-catalog ─────────────────────────────────────────


def _make_catalog(tmp_path: Path) -> tuple[Path, Catalog]:
    catalog_dir = tmp_path / "catalog"
    cat = Catalog.init(catalog_dir)
    return catalog_dir, cat


@pytest.fixture(autouse=True)
def _backfill_catalog_env(monkeypatch):
    monkeypatch.setenv("GIT_AUTHOR_NAME", "Test")
    monkeypatch.setenv("GIT_AUTHOR_EMAIL", "test@test.invalid")
    monkeypatch.setenv("GIT_COMMITTER_NAME", "Test")
    monkeypatch.setenv("GIT_COMMITTER_EMAIL", "test@test.invalid")


class TestBackfillCatalog:
    """nexus-9l2lg Task 4: ``nx enrich bib COLLECTION --backfill-catalog``
    re-derives the catalog bib_* write from already-enriched T3 chunk
    metadata, with zero external API calls. Uses a real T3 (EphemeralClient)
    + real local Catalog rather than mocks (repo convention: integration
    over mocks for boundary-spanning behavior).

    No env pin needed: nexus-6ha8a extended the event-sourced projector
    to persist bib_* too, so these assertions hold under the ambient
    default (ON, RDR-101 Phase 3 PR ζ). See test_catalog_bib_columns.py
    for the dedicated legacy-path (=0) parity suite.
    """

    def _seed_collection_and_catalog(
        self, tmp_path: Path, monkeypatch, local_t3: T3Database,
        collection: str = "knowledge__nexus-1-1__voyage-context-3__v1",
    ) -> tuple[Catalog, str, Tumbler]:
        catalog_dir, cat = _make_catalog(tmp_path)
        monkeypatch.setenv("NEXUS_CATALOG_PATH", str(catalog_dir))
        monkeypatch.setattr("nexus.db.make_t3", lambda: local_t3)

        col = local_t3.get_or_create_collection(collection)
        col.add(
            ids=["c1", "c2"],
            documents=["chunk one text", "chunk two text"],
            metadatas=[
                {
                    "title": "Enriched Paper", "source_path": "/papers/e.pdf",
                    "bib_year": 2019, "bib_venue": "OSDI",
                    "bib_authors": "Dana", "bib_citation_count": 314,
                    "bib_semantic_scholar_id": "ss42",
                },
                {
                    "title": "Enriched Paper", "source_path": "/papers/e.pdf",
                    "bib_year": 2019, "bib_venue": "OSDI",
                    "bib_authors": "Dana", "bib_citation_count": 314,
                    "bib_semantic_scholar_id": "ss42",
                },
            ],
        )

        owner = cat.register_owner("papers", "curator")
        cat.register(
            owner, "Enriched Paper", content_type="paper",
            physical_collection=collection, file_path="papers/e.pdf",
        )
        return cat, collection, owner

    def test_backfill_catalog_populates_bib_columns_from_enriched_chunks(
        self, tmp_path: Path, monkeypatch, local_t3: T3Database,
    ) -> None:
        cat, collection, _owner = self._seed_collection_and_catalog(
            tmp_path, monkeypatch, local_t3,
        )

        runner = CliRunner()
        result = runner.invoke(enrich, ["bib", collection, "--backfill-catalog"])
        assert result.exit_code == 0, result.output
        assert "Backfilled 1 titles" in result.output

        results = cat.find("Enriched Paper")
        assert len(results) == 1
        found = cat.resolve(results[0].tumbler)
        assert found.bib_year == 2019
        assert found.bib_venue == "OSDI"
        assert found.bib_authors == "Dana"
        assert found.bib_citation_count == 314
        assert found.bib_semantic_scholar_id == "ss42"

    def test_backfill_catalog_idempotent_second_run(
        self, tmp_path: Path, monkeypatch, local_t3: T3Database,
    ) -> None:
        cat, collection, _owner = self._seed_collection_and_catalog(
            tmp_path, monkeypatch, local_t3,
        )

        runner = CliRunner()
        first = runner.invoke(enrich, ["bib", collection, "--backfill-catalog"])
        assert first.exit_code == 0, first.output
        before = cat.resolve(cat.find("Enriched Paper")[0].tumbler)

        second = runner.invoke(enrich, ["bib", collection, "--backfill-catalog"])
        assert second.exit_code == 0, second.output
        assert "Backfilled 1 titles" in second.output

        after = cat.resolve(cat.find("Enriched Paper")[0].tumbler)
        for key in (
            "bib_year", "bib_venue", "bib_authors", "bib_citation_count",
            "bib_semantic_scholar_id",
        ):
            assert getattr(before, key) == getattr(after, key), key

    def test_backfill_catalog_skips_non_enriched_chunks(
        self, tmp_path: Path, monkeypatch, local_t3: T3Database,
    ) -> None:
        cat, collection, owner = self._seed_collection_and_catalog(
            tmp_path, monkeypatch, local_t3,
        )
        col = local_t3.get_or_create_collection(collection)
        col.add(
            ids=["c3"],
            documents=["chunk three text, not enriched"],
            metadatas=[{
                "title": "Non-Enriched Paper", "source_path": "/papers/n.pdf",
            }],
        )
        cat.register(
            owner, "Non-Enriched Paper", content_type="paper",
            physical_collection=collection, file_path="papers/n.pdf",
        )

        runner = CliRunner()
        result = runner.invoke(enrich, ["bib", collection, "--backfill-catalog"])
        assert result.exit_code == 0, result.output
        assert "Backfilled 1 titles" in result.output

        enriched = cat.resolve(cat.find("Enriched Paper")[0].tumbler)
        assert enriched.bib_year == 2019

        non_enriched = cat.resolve(cat.find("Non-Enriched Paper")[0].tumbler)
        assert non_enriched.bib_year == 0
        assert non_enriched.bib_semantic_scholar_id == ""

    def test_backfill_catalog_counts_skipped_no_row_separately(
        self, tmp_path: Path, monkeypatch, local_t3: T3Database,
    ) -> None:
        """nexus-6ha8a follow-up (critic finding 3): _backfill_catalog_
        from_chunks previously incremented titles_backfilled
        unconditionally, even when _catalog_enrich_hook silently
        no-oped (no matching catalog row). A title with enriched
        chunks but NO catalog row must count as skipped, not
        backfilled."""
        # Distinct collection name: chromadb's EphemeralClient has known
        # process-shared-state behavior across tests reusing the same
        # collection name (see project_chromadb_ephemeral_shared_state
        # memory) — sibling TestBackfillCatalog tests add their own
        # chunks (e.g. "c3") to the default name, which would silently
        # inflate this test's exact chunks_scanned assertion if reused.
        cat, collection, owner = self._seed_collection_and_catalog(
            tmp_path, monkeypatch, local_t3,
            collection="knowledge__nexus-1-1__bge-base-en-v15-768__v2",
        )
        # A second enriched title with chunks but NO matching catalog
        # row anywhere (no register() call for it) — the hook's
        # source_path / title / FTS lookups must all miss.
        col = local_t3.get_or_create_collection(collection)
        col.add(
            ids=["c9"],
            documents=["orphan enriched chunk text"],
            metadatas=[{
                "title": "Orphan Enriched Paper", "source_path": "/papers/orphan.pdf",
                "bib_year": 2021, "bib_venue": "SOSP",
                "bib_authors": "Eve", "bib_citation_count": 7,
                "bib_semantic_scholar_id": "ss-orphan",
            }],
        )

        runner = CliRunner()
        result = runner.invoke(enrich, ["bib", collection, "--backfill-catalog"])
        assert result.exit_code == 0, result.output
        # 2 enriched titles total ("Enriched Paper" has a matching row;
        # "Orphan Enriched Paper" does not) -> N-1 backfilled, 1 skipped.
        assert "Backfilled 1 titles" in result.output
        assert "1 titles skipped (no matching catalog row)" in result.output

        from nexus.commands.enrich import _backfill_catalog_from_chunks
        # Direct call (fresh collection state unchanged by the CLI run
        # above, since the CLI already applied the update) confirms the
        # exact return-tuple contract idempotently: "Enriched Paper" is
        # still matched (re-applies the same values -> still counted as
        # backfilled), "Orphan Enriched Paper" is still unmatched.
        titles_backfilled, chunks_scanned, titles_skipped_no_row = (
            _backfill_catalog_from_chunks(collection)
        )
        assert titles_backfilled == 1
        assert titles_skipped_no_row == 1
        assert chunks_scanned == 3  # c1, c2 (Enriched Paper) + c9 (orphan)

    def test_backfill_catalog_makes_zero_external_calls(
        self, tmp_path: Path, monkeypatch, local_t3: T3Database,
    ) -> None:
        # Non-voyage collection name: this is a local-mode test (the
        # embedder segment is incidental), and the RDR-109 mode lint
        # flags any test whose source mentions voyage-(context|code)-3
        # without the cloud_mode fixture.
        self._seed_collection_and_catalog(
            tmp_path, monkeypatch, local_t3,
            collection="knowledge__nexus-1-1__bge-base-en-v15-768__v1",
        )

        def _boom(*args, **kwargs):
            raise AssertionError("external bib_enricher call must not happen")

        monkeypatch.setattr("nexus.bib_enricher.enrich", _boom, raising=False)
        monkeypatch.setattr(
            "nexus.bib_enricher_openalex.enrich", _boom, raising=False,
        )

        runner = CliRunner()
        result = runner.invoke(
            enrich, ["bib", "knowledge__nexus-1-1__bge-base-en-v15-768__v1", "--backfill-catalog"],
        )
        assert result.exit_code == 0, result.output
