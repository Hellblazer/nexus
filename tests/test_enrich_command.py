# SPDX-License-Identifier: AGPL-3.0-or-later
"""Tests for nx enrich CLI command."""
from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from tests._catalog_fixture_ops import ActiveCatalog
from click.testing import CliRunner

from nexus.catalog.tumbler import Tumbler
from nexus.commands.enrich import _CollectionDocuments, enrich
from nexus.db.http_vector_client import HttpVectorClient
from nexus.db.t3 import T3Database
from tests._enrich_holders import ch as _ch
from tests._enrich_holders import held as _held


@patch("nexus.db.make_t3")
@patch("nexus.bib_enricher.enrich")
def test_enrich_empty_collection(mock_bib: MagicMock, mock_t3_factory: MagicMock) -> None:
    """Empty collection prints message and exits cleanly."""
    mock_col = MagicMock()
    mock_col.get.return_value = {"ids": [], "metadatas": []}
    # make_t3() returns the service-backed HttpVectorClient
    # unconditionally in production since RDR-155 P4a.2.
    # get_or_create_collection() is a direct call on both handles, and
    # _vector_with_retry (patched where used) or the collection mock
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
    """A document whose catalog row holds the backend's id is skipped (idempotency).
    The chunk rows carry NO id key here, so a verb that read the chunk rows (the
    pre-6pfgb.21 rule) would look the document up and call the backend."""
    mock_col = MagicMock()
    mock_col.get.return_value = {
        "ids": ["c1", "c2"],
        "metadatas": [{"title": "Paper A"}, {"title": "Paper A"}],
    }
    # make_t3() returns the service-backed HttpVectorClient
    # unconditionally in production since RDR-155 P4a.2.
    # get_or_create_collection() is a direct call on both handles, and
    # _vector_with_retry (patched where used) or the collection mock
    # itself absorbs the rest, so spec=HttpVectorClient pins the real
    # return type without changing behavior.
    mock_db = MagicMock(spec=HttpVectorClient)
    mock_db.get_or_create_collection.return_value = mock_col
    mock_t3_factory.return_value = mock_db

    runner = CliRunner()
    with _held({"c1": "Paper A", "c2": "Paper A"}, bib_ids={"Paper A": "abc123"}):
        result = runner.invoke(enrich, ["bib", "knowledge__test", "--source", "s2"])
    assert result.exit_code == 0
    assert "1 documents, 1 already enriched" in result.output
    assert "0 documents to look up" in result.output
    mock_bib.assert_not_called()


@patch("nexus.retry._vector_with_retry")
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
    # _vector_with_retry (patched where used) or the collection mock
    # itself absorbs the rest, so spec=HttpVectorClient pins the real
    # return type without changing behavior.
    mock_db = MagicMock(spec=HttpVectorClient)
    mock_db.get_or_create_collection.return_value = mock_col
    mock_t3_factory.return_value = mock_db

    runner = CliRunner()
    with _held({"c1": "Paper A", "c2": "Paper A"}), \
            patch("nexus.commands.enrich._catalog_enrich_hook", return_value=True) as hook:
        result = runner.invoke(enrich, ["bib", "knowledge__test", "--delay", "0", "--source", "s2"])
    assert result.exit_code == 0
    assert "enriched 2 chunks across 1 documents" in result.output
    # The catalog row written is the document's own, by tumbler: no title or path lookup.
    assert hook.call_args.kwargs["tumblers"] == ["1.1.1"]
    assert hook.call_args.kwargs["title"] == "Paper A"
    updated = mock_retry.call_args_list[-1]
    assert updated.kwargs["ids"] == ["c1", "c2"]
    assert all(m["bib_year"] == 2024 for m in updated.kwargs["metadatas"])


@patch("nexus.db.make_t3")
@patch("nexus.bib_enricher.enrich")
def test_enrich_no_match_increments_skipped(mock_bib: MagicMock, mock_t3_factory: MagicMock) -> None:
    """When bib_enrich returns {}, the document is counted as skipped."""
    mock_bib.return_value = {}

    mock_col = MagicMock()
    mock_col.get.return_value = {
        "ids": ["c1"],
        "metadatas": [{"title": "Unknown Paper"}],
    }
    # make_t3() returns the service-backed HttpVectorClient
    # unconditionally in production since RDR-155 P4a.2.
    # get_or_create_collection() is a direct call on both handles, and
    # _vector_with_retry (patched where used) or the collection mock
    # itself absorbs the rest, so spec=HttpVectorClient pins the real
    # return type without changing behavior.
    mock_db = MagicMock(spec=HttpVectorClient)
    mock_db.get_or_create_collection.return_value = mock_col
    mock_t3_factory.return_value = mock_db

    runner = CliRunner()
    with _held({"c1": "Unknown Paper"}):
        result = runner.invoke(enrich, ["bib", "knowledge__test", "--delay", "0", "--source", "s2"])
    assert result.exit_code == 0
    assert "1 documents had no Semantic Scholar match" in result.output


@patch("nexus.db.make_t3")
@patch("nexus.bib_enricher.enrich")
def test_enrich_limit_option(mock_bib: MagicMock, mock_t3_factory: MagicMock) -> None:
    """--limit caps the number of documents enriched."""
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
    # _vector_with_retry (patched where used) or the collection mock
    # itself absorbs the rest, so spec=HttpVectorClient pins the real
    # return type without changing behavior.
    mock_db = MagicMock(spec=HttpVectorClient)
    mock_db.get_or_create_collection.return_value = mock_col
    mock_t3_factory.return_value = mock_db

    runner = CliRunner()
    with _held({"c1": "Paper A", "c2": "Paper B", "c3": "Paper C"}):
        result = runner.invoke(
            enrich, ["bib", "knowledge__test", "--delay", "0", "--limit", "2", "--source", "s2"],
        )
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
    # _vector_with_retry (patched where used) or the collection mock
    # itself absorbs the rest, so spec=HttpVectorClient pins the real
    # return type without changing behavior.
    mock_db = MagicMock(spec=HttpVectorClient)
    mock_db.get_or_create_collection.return_value = mock_col
    mock_t3_factory.return_value = mock_db

    runner = CliRunner()
    with _held({"c1": "Paper Z"}), \
            patch("nexus.commands.enrich._catalog_enrich_hook", return_value=True):
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
    # _vector_with_retry (patched where used) or the collection mock
    # itself absorbs the rest, so spec=HttpVectorClient pins the real
    # return type without changing behavior.
    mock_db = MagicMock(spec=HttpVectorClient)
    mock_db.get_or_create_collection.return_value = mock_col
    mock_t3_factory.return_value = mock_db

    runner = CliRunner()
    with _held({"c1": "Paper Auto"}):
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
    # _vector_with_retry (patched where used) or the collection mock
    # itself absorbs the rest, so spec=HttpVectorClient pins the real
    # return type without changing behavior.
    mock_db = MagicMock(spec=HttpVectorClient)
    mock_db.get_or_create_collection.return_value = mock_col
    mock_t3_factory.return_value = mock_db

    runner = CliRunner()
    with _held({"c1": "mfaz"}), \
            patch("nexus.commands.enrich._catalog_enrich_hook", return_value=True):
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
    # _vector_with_retry (patched where used) or the collection mock
    # itself absorbs the rest, so spec=HttpVectorClient pins the real
    # return type without changing behavior.
    mock_db = MagicMock(spec=HttpVectorClient)
    mock_db.get_or_create_collection.return_value = mock_col
    mock_t3_factory.return_value = mock_db

    runner = CliRunner()
    # The filename the arXiv id is read from is the DOCUMENT's (catalog file_path).
    with _held({"c1": "Attention"}, file_paths={"Attention": "papers/1706.03762.pdf"}), \
            patch("nexus.commands.enrich._catalog_enrich_hook", return_value=True):
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
    # _vector_with_retry (patched where used) or the collection mock
    # itself absorbs the rest, so spec=HttpVectorClient pins the real
    # return type without changing behavior.
    mock_db = MagicMock(spec=HttpVectorClient)
    mock_db.get_or_create_collection.return_value = mock_col
    mock_t3_factory.return_value = mock_db

    runner = CliRunner()
    with _held({"c1": "Paper S2"}):
        result = runner.invoke(
            enrich, ["bib", "knowledge__test", "--delay", "0"],
        )
    assert result.exit_code == 0
    assert "Backend: s2" in result.output
    mock_oa.assert_not_called()
    mock_s2.assert_called_once_with("Paper S2")


# ── nexus-9l2lg: --backfill-catalog ─────────────────────────────────────────


def _exact(cat, title: str):
    """Resolve the entry whose title is EXACTLY *title*.

    nexus-aqbrk: these tests used ``cat.find(title)[0]``, but ``find`` is a
    SUBSTRING search with no ordering contract on either substrate — and this
    file seeds both "Enriched Paper" and "Non-Enriched Paper", so the query
    legitimately matches BOTH and position decides which you get.

    On a freshly seeded catalog both arms happened to return the intended row
    first. After the backfill UPDATES the enriched row, the engine arm returns
    them reversed, so ``[0]`` silently became the wrong paper and the test read
    bib_year=0 off "Non-Enriched Paper". Not a service defect — neither
    Catalog.find nor HttpCatalogClient.find promises an order, and no
    production caller indexes into find() (grep-checked) — so the fix is to
    stop depending on one.
    """
    # nexus-fgxmk promoted this idiom into the API; kept here as the
    # one-entry assertion these tests want on top of it.
    hits = cat.find_by_title_exact(title)
    assert len(hits) == 1, (
        f"expected exactly one entry titled {title!r}, got "
        f"{[(str(e.tumbler), e.title) for e in hits]}"
    )
    return cat.resolve(hits[0].tumbler)


@pytest.fixture(autouse=True)
def _backfill_catalog_env(monkeypatch):
    monkeypatch.setenv("GIT_AUTHOR_NAME", "Test")
    monkeypatch.setenv("GIT_AUTHOR_EMAIL", "test@test.invalid")
    monkeypatch.setenv("GIT_COMMITTER_NAME", "Test")
    monkeypatch.setenv("GIT_COMMITTER_EMAIL", "test@test.invalid")


@pytest.mark.t2_tenant
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
        # nexus-aqbrk: seed through the ACTIVE catalog. `enrich bib
        # --backfill-catalog` resolves via make_catalog_reader()
        # (enrich.py:41/:73), so a local-only seed left the service catalog
        # empty and the verb reported "Backfilled 0 titles ... 1 titles skipped
        # (no matching catalog row)" — the skip count naming the missing row.
        # (The local Catalog.init that used to run here died with the local
        # catalog in the terminal nexus-i711w deletion; the factories are
        # service-only.)
        monkeypatch.setattr("nexus.db.make_t3", lambda: local_t3)
        cat = ActiveCatalog()

        col = local_t3.get_or_create_collection(collection)
        col.add(
            ids=[_ch("c1"), _ch("c2")],
            documents=["chunk one text", "chunk two text"],
            metadatas=[
                {
                    "title": "Enriched Paper", "source_path": "/papers/e.pdf",
                    "content_hash": "hash-Enriched Paper",
                    "bib_year": 2019, "bib_venue": "OSDI",
                    "bib_authors": "Dana", "bib_citation_count": 314,
                    "bib_semantic_scholar_id": "ss42",
                },
                {
                    "title": "Enriched Paper", "source_path": "/papers/e.pdf",
                    "content_hash": "hash-Enriched Paper",
                    "bib_year": 2019, "bib_venue": "OSDI",
                    "bib_authors": "Dana", "bib_citation_count": 314,
                    "bib_semantic_scholar_id": "ss42",
                },
            ],
        )

        owner = cat.register_owner("papers", "curator")
        self._register_holding(
            cat, owner, "Enriched Paper", collection, "papers/e.pdf", ["c1", "c2"],
        )
        return cat, collection, owner

    @staticmethod
    def _register_holding(
        cat, owner, title: str, collection: str, file_path: str, chunk_names: list[str],
    ) -> Tumbler:
        """Register *title* and give it a manifest over the chunks named *chunk_names*
        (nexus-6pfgb.21: a chunk row is evidence for a document only through the
        manifest, never through the row's title). The document's file hash is
        ``hash-<title>``: the ``content_hash`` the chunk rows of its own indexer carry."""
        from tests._catalog_fixture_ops import seed_manifest_chunks

        tumbler = cat.register(
            owner, title, content_type="paper",
            physical_collection=collection, file_path=file_path,
            meta={"content_hash": f"hash-{title}"},
        )
        chashes = [_ch(n) for n in chunk_names]
        seed_manifest_chunks(collection, chashes)
        cat.write_manifest(
            str(tumbler),
            [{"chash": c, "position": i} for i, c in enumerate(chashes)],
            collection=collection,
        )
        return tumbler

    def test_backfill_catalog_populates_bib_columns_from_enriched_chunks(
        self, tmp_path: Path, monkeypatch, local_t3: T3Database,
    ) -> None:
        cat, collection, _owner = self._seed_collection_and_catalog(
            tmp_path, monkeypatch, local_t3,
        )

        runner = CliRunner()
        result = runner.invoke(enrich, ["bib", collection, "--backfill-catalog"])
        assert result.exit_code == 0, result.output
        assert "Backfilled 1 documents" in result.output

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
        before = _exact(cat, "Enriched Paper")

        second = runner.invoke(enrich, ["bib", collection, "--backfill-catalog"])
        assert second.exit_code == 0, second.output
        # Fill-only: the row now holds the id, so the second run writes nothing.
        assert "Backfilled 0 documents" in second.output
        assert "1 documents already hold the id" in second.output

        after = _exact(cat, "Enriched Paper")
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
            ids=[_ch("c3")],
            documents=["chunk three text, not enriched"],
            metadatas=[{
                "title": "Non-Enriched Paper", "source_path": "/papers/n.pdf",
            }],
        )
        self._register_holding(
            cat, owner, "Non-Enriched Paper", collection, "papers/n.pdf", ["c3"],
        )

        runner = CliRunner()
        result = runner.invoke(enrich, ["bib", collection, "--backfill-catalog"])
        assert result.exit_code == 0, result.output
        assert "Backfilled 1 documents" in result.output

        enriched = _exact(cat, "Enriched Paper")
        assert enriched.bib_year == 2019

        non_enriched = _exact(cat, "Non-Enriched Paper")
        assert non_enriched.bib_year == 0
        assert non_enriched.bib_semantic_scholar_id == ""

    def test_backfill_catalog_counts_unheld_chunks_separately(
        self, tmp_path: Path, monkeypatch, local_t3: T3Database,
    ) -> None:
        """nexus-6ha8a follow-up (critic finding 3), as nexus-6pfgb.21 restates it:
        the summary must say what actually changed. An enriched chunk that no
        live document holds is evidence for nobody (its row's title is the last
        writer's), so it is skipped and counted, not matched to a catalog row by
        title and not counted as backfilled."""
        # Distinct collection name: the in-memory T3 shares state across tests
        # reusing a collection name, and sibling tests add chunks (e.g. "c3") to
        # the default name, which would inflate this test's exact
        # chunks_scanned assertion if reused.
        cat, collection, owner = self._seed_collection_and_catalog(
            tmp_path, monkeypatch, local_t3,
            collection="knowledge__nexus-1-1__bge-base-en-v15-768__v2",
        )
        # A second enriched chunk that NO manifest names, though a catalog row
        # carries its title: a title match must not claim it.
        cat.register(
            owner, "Orphan Enriched Paper", content_type="paper",
            physical_collection=collection, file_path="papers/orphan.pdf",
        )
        col = local_t3.get_or_create_collection(collection)
        col.add(
            ids=[_ch("c9")],
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
        assert "Backfilled 1 documents" in result.output
        assert "1 enriched chunks skipped (not held by exactly one live document)" in result.output
        assert _exact(cat, "Orphan Enriched Paper").bib_semantic_scholar_id == ""

        from nexus.commands.enrich import _backfill_catalog_from_chunks
        # Direct call (the CLI run above already applied the update) confirms the
        # return-tuple contract: "Enriched Paper" already holds the id (fill-only,
        # not counted again), the orphan chunk is still skipped.
        documents_backfilled, chunks_scanned, chunks_skipped = (
            _backfill_catalog_from_chunks(collection)
        )
        assert documents_backfilled == 0
        assert chunks_skipped == 1
        assert chunks_scanned == 3  # c1, c2 (Enriched Paper) + c9 (held by nobody)

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


@patch("nexus.db.make_t3")
@patch("nexus.bib_enricher.enrich")
def test_enrich_bare_subject_resolves_to_conformant_collection(
    mock_bib: MagicMock, mock_t3_factory: MagicMock,
) -> None:
    """nexus-g276c: ``nx enrich bib knowledge__vector-search`` (the bare form
    every other verb accepts) reached the engine unresolved and died with
    HTTP 400 "not four-segment conformant"."""
    mock_col = MagicMock()
    mock_col.get.return_value = {"ids": [], "metadatas": []}
    mock_db = MagicMock(spec=HttpVectorClient)
    mock_db.get_or_create_collection.return_value = mock_col
    mock_db.list_collections.return_value = []
    mock_db.collection_exists.return_value = True
    mock_t3_factory.return_value = mock_db

    result = CliRunner().invoke(enrich, ["bib", "knowledge__vector-search", "--source", "s2"])
    assert result.exit_code == 0, result.output
    # The model token is install-dependent (bge under the test config, the
    # docs/knowledge cloud embedder on a cloud box); the shape is what the
    # engine checks.
    resolved = mock_db.get_or_create_collection.call_args.args[0]
    assert resolved.startswith("knowledge__vector-search__"), resolved
    assert resolved.endswith("__v1") and resolved.count("__") == 3, resolved
    assert resolved in result.output


@patch("nexus.retry._vector_with_retry")
@patch("nexus.db.make_t3")
@patch("nexus.bib_enricher.enrich")
def test_enrich_looks_up_by_catalog_title_when_chunk_title_is_a_fragment(
    mock_bib: MagicMock, mock_t3_factory: MagicMock, mock_retry: MagicMock,
) -> None:
    """nexus-g276c: chunks carry the extractor's first line as ``title``
    ("...Retrieval-Augmented Generation:"); the catalog row carries the
    registered title. OpenAlex found the paper only under the full title.
    nexus-6pfgb.21: the unit is the catalog document, so the lookup title is
    its catalog title by construction, whatever the chunk rows say."""
    fragment = "Self-Aware Vector Embeddings for Retrieval-Augmented Generation:"
    full = (
        "Self-Aware Vector Embeddings for Retrieval-Augmented Generation: "
        "A Neuroscience-Inspired Framework for Temporal, Confidence-Weighted, "
        "and Relational Knowledge"
    )
    mock_bib.return_value = {
        "year": 2026, "venue": "arXiv", "authors": "Naizhong Xu",
        "citation_count": 0, "semantic_scholar_id": "s2id",
    }
    chunk_meta = [
        {"title": fragment, "content_hash": "h1"},
        {"title": fragment, "content_hash": "h1"},
    ]
    mock_retry.side_effect = [
        {"ids": ["c1", "c2"], "metadatas": chunk_meta},   # paginated scan
        {"ids": ["c1", "c2"], "metadatas": chunk_meta},   # re-fetch before update
        None,                                             # col.update
    ]
    mock_col = MagicMock()
    mock_db = MagicMock(spec=HttpVectorClient)
    mock_db.get_or_create_collection.return_value = mock_col
    mock_t3_factory.return_value = mock_db

    with _held({"c1": full, "c2": full}), \
            patch("nexus.commands.enrich._catalog_enrich_hook") as hook:
        result = CliRunner().invoke(
            enrich, ["bib", "knowledge__test", "--delay", "0", "--source", "s2"],
        )
    assert result.exit_code == 0, result.output
    mock_bib.assert_called_once_with(full)
    assert hook.call_args.kwargs["title"] == full
    assert "enriched 2 chunks across 1 documents" in result.output


def test_enrich_writes_chunk_rows_only_where_one_document_holds_them() -> None:
    """nexus-6pfgb.21: ``c1`` is held by two documents, ``c2`` and ``c3`` by one each. Each
    document is looked up under its own catalog title and its catalog row is written by tumbler;
    the chunk row both documents hold is not rewritten with either one's bib."""
    chunk = {"c1": {"title": "B"}, "c2": {"title": "A"}, "c3": {"title": "B"}}
    owners = {"c1": ["1.1.1", "1.1.2"], "c2": ["1.1.1"], "c3": ["1.1.2"]}
    entries = {
        "1.1.1": SimpleNamespace(
            title="A", file_path="", bib_semantic_scholar_id="", bib_openalex_id="",
        ),
        "1.1.2": SimpleNamespace(
            title="B", file_path="", bib_semantic_scholar_id="", bib_openalex_id="",
        ),
    }
    found = _CollectionDocuments(
        to_look_up={"1.1.1": (entries["1.1.1"], ["c1", "c2"]), "1.1.2": (entries["1.1.2"], ["c1", "c3"])},
        total=2, enriched=0, untitled=0,
    )
    col = MagicMock()
    col.get.side_effect = lambda ids=None, include=None, limit=None, offset=0: (
        {"ids": list(chunk), "metadatas": list(chunk.values())} if ids is None
        else {"ids": list(ids), "metadatas": [dict(chunk[i]) for i in ids]}
    )
    db = MagicMock(spec=HttpVectorClient)
    db.get_or_create_collection.return_value = col
    answers = {
        "A": {"year": 2019, "venue": "V", "authors": "Dana", "citation_count": 1,
              "semantic_scholar_id": "ssA"},
        "B": {"year": 2024, "venue": "W", "authors": "Eve", "citation_count": 2,
              "semantic_scholar_id": "ssB"},
    }
    with patch("nexus.db.make_t3", return_value=db), \
            patch("nexus.bib_enricher.enrich", side_effect=lambda t: dict(answers[t])) as backend, \
            patch(
                "nexus.commands.enrich._catalog_documents_for_chunks",
                return_value=(owners, entries),
            ) as held, \
            patch("nexus.commands.enrich._catalog_collection_documents", return_value=found), \
            patch("nexus.commands.enrich._catalog_enrich_hook", return_value=True) as hook, \
            patch("nexus.catalog.factory.make_catalog_reader", return_value=None):
        result = CliRunner().invoke(
            enrich, ["bib", "knowledge__test", "--delay", "0", "--source", "s2"],
        )

    assert result.exit_code == 0, result.output
    assert [c.args for c in backend.call_args_list] == [("A",), ("B",)]
    written = {
        cid: m["bib_semantic_scholar_id"]
        for call in col.update.call_args_list
        for cid, m in zip(call.kwargs["ids"], call.kwargs["metadatas"])
    }
    assert written == {"c2": "ssA", "c3": "ssB"}, "the chunk both documents hold stays as it was"
    assert [(c.kwargs["title"], c.kwargs["tumblers"]) for c in hook.call_args_list] == [
        ("A", ["1.1.1"]), ("B", ["1.1.2"]),
    ]
    # One batched ownership lookup for the documents still to look up, never one per chunk.
    assert held.call_count == 1


# ── nexus-6pfgb.21 round 2: --backfill-catalog attribution and fill-only; enrich selection cost ──
#
# Storage-free: T3 rows and the catalog's answers are stand-ins, so these pin the rules of
# ``_backfill_catalog_from_chunks`` and ``run_bib_enrichment`` themselves. The engine path is
# tests/integration/test_6pfgb21_bib_enrich_by_manifest.py.

_BIB_ROW = {
    "bib_year": 2019, "bib_venue": "OSDI", "bib_authors": "Dana",
    "bib_citation_count": 314, "bib_semantic_scholar_id": "ssA",
}


def _doc(title: str, *, content_hash: str = "", bib_id: str = "", tumbler: str = "") -> SimpleNamespace:
    return SimpleNamespace(
        tumbler=tumbler, title=title, file_path="", bib_semantic_scholar_id=bib_id,
        bib_openalex_id="", index_content_hash=content_hash, meta={},
    )


def _backfill(chunks: dict[str, dict], owners: dict[str, list[str]], entries: dict):
    """Run ``--backfill-catalog`` over T3 rows *chunks* (id -> metadata) whose holders are
    *owners* and whose catalog documents are *entries*; returns ``(result, hook)``."""
    col = MagicMock()
    col.get.side_effect = lambda ids=None, include=None, limit=None, offset=0: (
        {"ids": list(chunks), "metadatas": list(chunks.values())} if not offset
        else {"ids": [], "metadatas": []}
    )
    db = MagicMock(spec=HttpVectorClient)
    db.get_or_create_collection.return_value = col
    with patch("nexus.db.make_t3", return_value=db), \
            patch(
                "nexus.commands.enrich._catalog_documents_for_chunks",
                return_value=(owners, entries),
            ), \
            patch("nexus.commands.enrich._catalog_enrich_hook", return_value=True) as hook:
        result = CliRunner().invoke(enrich, ["bib", "knowledge__test", "--backfill-catalog"])
    assert result.exit_code == 0, result.output
    return result, hook


def test_backfill_catalog_ignores_a_chunk_a_former_holder_wrote() -> None:
    """B enriched c1 and B's index write is the last (the row's content_hash is B's), then B
    dropped c1. A is now c1's only live holder, but the row names B as its last indexer: A's
    catalog row stays untouched. Red on b321b13dc, which trusted c1 because one live document
    holds it. This covers only the case where the former holder's stamp is still on the row;
    the case where a later index write restamped it is the known-limit pin below."""
    chunks = {
        "c1": {"content_hash": "hash-B", **{**_BIB_ROW, "bib_semantic_scholar_id": "ssB"}},
        "c2": {"content_hash": "hash-A"},
    }
    entries = {"1.1.1": _doc("A", content_hash="hash-A")}

    result, hook = _backfill(chunks, {"c1": ["1.1.1"], "c2": ["1.1.1"]}, entries)

    hook.assert_not_called()
    assert "Backfilled 0 documents" in result.output
    assert "1 enriched chunks skipped (the row does not name the document that holds it as its last indexer)" in result.output


def test_known_limit_backfill_catalog_copies_a_bib_a_former_co_holder_wrote_when_a_later_index_restamped_the_row() -> None:
    """KNOWN-LIMIT PIN, not a desired behaviour. content_hash names a chunk row's last INDEXER,
    not whoever wrote its bib_*. A enriched c1 (bib ssA); B then re-indexed c1, which restamps
    content_hash to B's and KEEPS bib_* (metadata_schema.REWRITE_OWNED_KEYS leaves bib out and
    the engine merge never retracts a key); A then dropped c1. B is c1's only live holder and
    the row names B, so --backfill-catalog copies A's id onto B's empty catalog row, and the
    two cases cannot be told apart from the row. The verb is documented as a recovery tool
    (prefer `nx enrich bib`). If this test starts failing because the verb now refuses this
    row, the mechanism fix landed: update the docs that disclose the limit and delete the pin."""
    chunks = {"c1": {"content_hash": "hash-B", "title": "B", **_BIB_ROW}}  # bib_semantic_scholar_id ssA

    result, hook = _backfill(chunks, {"c1": ["1.1.2"]}, {"1.1.2": _doc("B", content_hash="hash-B")})

    assert hook.call_count == 1
    assert hook.call_args.kwargs["tumblers"] == ["1.1.2"]
    assert hook.call_args.kwargs["bib_meta"]["semantic_scholar_id"] == "ssA"
    assert "Backfilled 1 documents" in result.output


def test_backfill_catalog_takes_a_chunk_that_names_its_holder() -> None:
    """The control for the test above: the same row with the holder's own content_hash fills A."""
    chunks = {"c1": {"content_hash": "hash-A", **_BIB_ROW}}

    result, hook = _backfill(chunks, {"c1": ["1.1.1"]}, {"1.1.1": _doc("A", content_hash="hash-A")})

    assert hook.call_count == 1
    assert hook.call_args.kwargs["tumblers"] == ["1.1.1"]
    assert hook.call_args.kwargs["bib_meta"]["semantic_scholar_id"] == "ssA"
    assert "Backfilled 1 documents" in result.output


def test_backfill_catalog_is_fill_only() -> None:
    """A catalog row that already holds the id is left as it is, whatever a chunk says."""
    chunks = {"c1": {"content_hash": "hash-A", **_BIB_ROW}}
    entries = {"1.1.1": _doc("A", content_hash="hash-A", bib_id="ssA-catalog")}

    result, hook = _backfill(chunks, {"c1": ["1.1.1"]}, entries)

    hook.assert_not_called()
    assert "Backfilled 0 documents" in result.output
    assert "1 documents already hold the id (left as they were)" in result.output


def test_backfill_catalog_skips_and_reports_a_document_whose_chunks_disagree() -> None:
    """Two attributed chunks of A carry different ids: neither is guessed."""
    chunks = {
        "c1": {"content_hash": "hash-A", **_BIB_ROW},
        "c2": {"content_hash": "hash-A", **{**_BIB_ROW, "bib_semantic_scholar_id": "ssOther"}},
    }
    entries = {"1.1.1": _doc("A", content_hash="hash-A")}

    result, hook = _backfill(chunks, {"c1": ["1.1.1"], "c2": ["1.1.1"]}, entries)

    hook.assert_not_called()
    assert "1 documents skipped (their chunks disagree on the id: 1.1.1)" in result.output


def test_backfill_catalog_collects_every_attributed_chunk_of_a_document() -> None:
    """Chunks that agree fill the document once; a stray chunk of another writer in the same
    document does not make it conflict (it is not attributed, so it is not read)."""
    chunks = {
        "c1": {"content_hash": "hash-A", **_BIB_ROW},
        "c2": {"content_hash": "hash-A", **_BIB_ROW},
        "c3": {"content_hash": "hash-B", **{**_BIB_ROW, "bib_semantic_scholar_id": "ssB"}},
    }
    entries = {"1.1.1": _doc("A", content_hash="hash-A")}

    result, hook = _backfill(
        chunks, {"c1": ["1.1.1"], "c2": ["1.1.1"], "c3": ["1.1.1"]}, entries,
    )

    assert hook.call_count == 1
    assert "Backfilled 1 documents" in result.output
    assert "1 enriched chunks skipped (the row does not name" in result.output


def test_backfill_catalog_reads_a_notes_catalog_doc_id() -> None:
    """A note's row carries ``catalog_doc_id`` (the indexers' rows do not): it attributes the
    row when it names the holder, and refuses it when it names another document."""
    named = {"c1": {"catalog_doc_id": "1.1.1", **_BIB_ROW}}
    other = {"c1": {"catalog_doc_id": "1.1.2", **_BIB_ROW}}

    _result, hook = _backfill(named, {"c1": ["1.1.1"]}, {"1.1.1": _doc("N")})
    assert hook.call_count == 1

    _result, hook = _backfill(other, {"c1": ["1.1.1"]}, {"1.1.1": _doc("N")})
    hook.assert_not_called()


def test_backfill_catalog_counts_every_live_owner_even_one_the_catalog_did_not_resolve() -> None:
    """c1 is held by 1.1.1 and by 1.1.2, but only 1.1.1 resolved to an entry. A chunk with two
    live owners is shared whether or not both resolve, so it is not evidence for 1.1.1."""
    chunks = {"c1": {"content_hash": "hash-A", **_BIB_ROW}}

    result, hook = _backfill(
        chunks, {"c1": ["1.1.1", "1.1.2"]}, {"1.1.1": _doc("A", content_hash="hash-A")},
    )

    hook.assert_not_called()
    assert "1 enriched chunks skipped (not held by exactly one live document)" in result.output


def _enrich_with(chunks: dict[str, dict], found, owners: dict[str, list[str]]):
    """Run ``nx enrich bib`` (s2) with the catalog's answers fixed; returns
    ``(result, backend_mock, owners_lookup_mock)``."""
    col = MagicMock()
    col.get.side_effect = lambda ids=None, include=None, limit=None, offset=0: (
        {"ids": list(chunks), "metadatas": list(chunks.values())} if ids is None
        else {"ids": list(ids), "metadatas": [dict(chunks[i]) for i in ids]}
    )
    db = MagicMock(spec=HttpVectorClient)
    db.get_or_create_collection.return_value = col
    with patch("nexus.db.make_t3", return_value=db), \
            patch("nexus.bib_enricher.enrich", return_value={}) as backend, \
            patch("nexus.commands.enrich._catalog_collection_documents", return_value=found), \
            patch(
                "nexus.commands.enrich._catalog_documents_for_chunks",
                return_value=(owners, {}),
            ) as lookup, \
            patch("nexus.catalog.factory.make_catalog_reader", return_value=None):
        result = CliRunner().invoke(
            enrich, ["bib", "knowledge__test", "--delay", "0", "--source", "s2"],
        )
    assert result.exit_code == 0, result.output
    return result, backend, lookup


def test_enrich_reverse_lookup_is_skipped_when_every_document_is_enriched() -> None:
    """Chunk owners are looked up only for documents that still need a lookup: a collection
    whose documents all hold the id costs the listing and nothing more."""
    found = _CollectionDocuments(to_look_up={}, total=3, enriched=3, untitled=0)

    result, backend, lookup = _enrich_with({"c1": {"title": "x"}}, found, {})

    lookup.assert_not_called()
    backend.assert_not_called()
    assert "3 documents, 3 already enriched, 0 documents to look up" in result.output


def test_enrich_leaves_a_document_with_the_id_on_its_own_chunks_to_backfill_catalog() -> None:
    """Enriched before the catalog carried bib_* columns: the id is only on chunk rows. A chunk
    that A alone holds, whose row names A and carries the id, is the evidence --backfill-catalog
    uses, so A is not looked up again (an API call each, and the answer may replace good
    values). A document with no such chunk is looked up as before."""
    a, b = _doc("A", content_hash="hash-A", tumbler="1.1.1"), _doc("B", content_hash="hash-B", tumbler="1.1.2")
    found = _CollectionDocuments(
        to_look_up={"1.1.1": (a, ["c1"]), "1.1.2": (b, ["c2"])},
        total=3, enriched=0, untitled=1,
    )
    chunks = {
        "c1": {"content_hash": "hash-A", "bib_semantic_scholar_id": "ssA"},
        "c2": {"content_hash": "hash-B"},
    }

    result, backend, _lookup = _enrich_with(chunks, found, {"c1": ["1.1.1"], "c2": ["1.1.2"]})

    assert [c.args for c in backend.call_args_list] == [("B",)]
    assert "1 without a catalog title (not looked up)" in result.output
    assert "1 with the id on their chunks only (run --backfill-catalog" in result.output
    assert "1 documents to look up" in result.output


def test_enrich_looks_up_a_document_whose_attributed_chunks_disagree_on_the_id() -> None:
    """--backfill-catalog skips and names a document whose attributed chunks carry different ids,
    so nx enrich bib must not leave that document to it: it is looked up here, and the progress
    line says so. (Before: any one attributed chunk with the id made the document chunk-only, so
    neither verb ever enriched it.) B is the control: its chunks agree, so it stays chunk-only."""
    a, b = _doc("A", content_hash="hash-A", tumbler="1.1.1"), _doc("B", content_hash="hash-B", tumbler="1.1.2")
    found = _CollectionDocuments(
        to_look_up={"1.1.1": (a, ["c1", "c2"]), "1.1.2": (b, ["c3", "c4"])},
        total=2, enriched=0, untitled=0,
    )
    chunks = {
        "c1": {"content_hash": "hash-A", "bib_semantic_scholar_id": "ssA"},
        "c2": {"content_hash": "hash-A", "bib_semantic_scholar_id": "ssOther"},
        "c3": {"content_hash": "hash-B", "bib_semantic_scholar_id": "ssB"},
        "c4": {"content_hash": "hash-B", "bib_semantic_scholar_id": "ssB"},
    }
    owners = {"c1": ["1.1.1"], "c2": ["1.1.1"], "c3": ["1.1.2"], "c4": ["1.1.2"]}

    result, backend, _lookup = _enrich_with(chunks, found, owners)

    assert [c.args for c in backend.call_args_list] == [("A",)]
    assert "1 with the id on their chunks only" in result.output
    assert "1 whose chunks disagree on the id (looked up here; --backfill-catalog skips them)" in result.output


def test_enrich_looks_up_a_document_whose_attributed_chunks_carry_both_backends_ids() -> None:
    """--backfill-catalog treats an S2 id and an OpenAlex id on one document's chunks as a
    disagreement and skips it; so the lookup here must not call it chunk-only either."""
    a = _doc("A", content_hash="hash-A", tumbler="1.1.1")
    found = _CollectionDocuments(to_look_up={"1.1.1": (a, ["c1", "c2"])}, total=1, enriched=0, untitled=0)
    chunks = {
        "c1": {"content_hash": "hash-A", "bib_semantic_scholar_id": "ssA"},
        "c2": {"content_hash": "hash-A", "bib_openalex_id": "W1"},
    }

    result, backend, _lookup = _enrich_with(chunks, found, {"c1": ["1.1.1"], "c2": ["1.1.1"]})

    assert [c.args for c in backend.call_args_list] == [("A",)]
    assert "1 whose chunks disagree on the id" in result.output


def test_enrich_progress_line_counts_a_titled_document_with_no_served_chunk() -> None:
    """A titled, unenriched document whose manifest names no chunk this collection serves is in
    no other figure; the progress line counts it."""
    found = _CollectionDocuments(to_look_up={}, total=2, enriched=0, untitled=0, no_chunks=2)

    result, _backend, _lookup = _enrich_with({"c1": {"title": "x"}}, found, {})

    assert "2 with no chunk in the collection (not looked up)" in result.output


def test_collection_documents_counts_a_titled_pending_document_with_no_served_chunk() -> None:
    """_catalog_collection_documents: 4 live documents, one enriched, one untitled, one titled
    with a served chunk, one titled whose manifest names only a chunk the collection does not
    serve. The last is counted in no_chunks, so the four counts add up to the total."""
    from nexus.commands.enrich import _catalog_collection_documents

    def _entry(t: str, title: str, bib_id: str = "") -> SimpleNamespace:
        return SimpleNamespace(tumbler=t, title=title, bib_semantic_scholar_id=bib_id)

    reader = MagicMock()
    reader.list_by_collection.return_value = [
        _entry("1.1.1", "Done", "ss1"), _entry("1.1.2", ""), _entry("1.1.3", "Has chunk"),
        _entry("1.1.4", "No served chunk"),
    ]
    reader.get_manifests.return_value = {
        "1.1.3": [SimpleNamespace(chash="served", collection=None)],
        "1.1.4": [SimpleNamespace(chash="elsewhere", collection=None)],
    }
    with patch("nexus.commands.enrich._open_catalog_reader", return_value=reader):
        found = _catalog_collection_documents("knowledge__test", "bib_semantic_scholar_id", {"served"})

    assert list(found.to_look_up) == ["1.1.3"]
    assert (found.total, found.enriched, found.untitled, found.no_chunks) == (4, 1, 1, 1)
