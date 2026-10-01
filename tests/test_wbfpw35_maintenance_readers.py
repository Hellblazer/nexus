# SPDX-License-Identifier: AGPL-3.0-or-later
"""nexus-wbfpw.35 (RDR-192 Phase 2 gate minors M1): maintenance readers that
discover documents or guard deletion must read STORED chunks, not live ones.

Under live(c) a chunk with no live owner is hidden from a plain read. These verbs
exist for exactly those chunks: ``nx catalog backfill --from-t3`` registers
documents for chunks that have no catalog row, and ``nx collection reindex``
refuses to delete a collection whose entries lack a source. Both were left on
live rows by nexus-wbfpw.10's sweep, so an all-unowned collection looked empty
to them (the reindex refusal then did not fire and the collection was deleted).

The fake below hides unowned rows unless ``include_non_live=True`` is passed, the
same contract the engine serves, so a reader that forgets the flag sees nothing.
"""
from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from click.testing import CliRunner

from nexus.cli import main
from nexus.db.http_vector_client import HttpVectorClient
from tests.test_catalog_backfill import catalog_env, git_identity  # noqa: F401 — fixtures
from tests._catalog_fixture_ops import ActiveCatalog


class _LiveAwareCollection:
    """Rows are ``(id, metadata, owned)``; an unowned row is hidden from a read
    that does not ask for ``include_non_live``."""

    def __init__(self, rows: list[tuple[str, dict, bool]]) -> None:
        self._rows = rows
        self.calls: list[dict] = []

    def get(self, *, include=None, limit=100, offset=0, include_non_live=False, **_kw):
        self.calls.append({"limit": limit, "offset": offset, "include_non_live": include_non_live})
        rows = self._rows if include_non_live else [r for r in self._rows if r[2]]
        page = rows[offset:offset + limit]
        return {
            "ids": [r[0] for r in page],
            "metadatas": [r[1] for r in page],
            "documents": [None] * len(page),
        }


def _t3_over(collection_name: str, col: _LiveAwareCollection, *, count: int, stored: int) -> MagicMock:
    t3 = MagicMock(spec=HttpVectorClient)
    t3.list_collections.return_value = [
        {"name": collection_name, "count": count, "stored_count": stored},
    ]
    t3.get_collection.return_value = col
    t3.get_or_create_collection.return_value = col
    return t3


class TestCatalogBackfillReadsStoredChunks:
    def test_from_t3_registers_documents_for_unowned_chunks(
        self, catalog_env, tmp_path: Path,  # noqa: F811
    ) -> None:
        from nexus.commands.catalog import _backfill_per_file_from_t3

        repo_root = tmp_path / "myrepo"
        repo_root.mkdir()
        cat = ActiveCatalog()
        owner = cat.register_owner(
            "myrepo", "repo", repo_hash="abc12345", repo_root=str(repo_root),
        )
        cat.register(
            owner=owner, title="myrepo (code)", content_type="code",
            physical_collection="code__myrepo-abc12345",
        )
        col = _LiveAwareCollection([
            (f"c{i}", {"source_path": str(repo_root / "src" / f"{n}.py")}, False)
            for i, n in enumerate(["a", "a", "b"])
        ])
        t3 = _t3_over("code__myrepo-abc12345", col, count=0, stored=3)

        registered = _backfill_per_file_from_t3(
            cat, t3, "code__myrepo-abc12345", dry_run=False,
        )

        assert registered == 2  # a.py, b.py
        assert any(c["include_non_live"] for c in col.calls)

    def test_rdr_backfill_walks_a_collection_whose_chunks_are_all_unowned(
        self, catalog_env, tmp_path: Path,  # noqa: F811
    ) -> None:
        """The collection list reports ``count`` live (0 here) and ``stored_count``
        physical; a ``count > 0`` filter dropped the collection before any chunk
        was read."""
        from nexus.commands.catalog import _backfill_rdrs

        cat = ActiveCatalog()
        col = _LiveAwareCollection([
            ("c0", {"source_path": str(tmp_path / "docs" / "rdr" / "RDR-001.md"),
                    "title": "RDR-001"}, False),
        ])
        t3 = _t3_over("rdr__orphan-deadbeef", col, count=0, stored=1)

        count = _backfill_rdrs(cat, t3, dry_run=False)

        assert count == 1

    def test_paper_backfill_reads_title_from_an_unowned_first_chunk(
        self, catalog_env,  # noqa: F811
    ) -> None:
        from nexus.commands.catalog import _backfill_papers

        cat = ActiveCatalog()
        col = _LiveAwareCollection([("c0", {"title": "A Recovered Paper", "bib_year": 2021}, False)])
        t3 = _t3_over("docs__paper-x", col, count=0, stored=1)

        count = _backfill_papers(cat, t3, dry_run=False)

        assert count == 1
        titles = [d.title for d in cat.all_documents() if d.content_type == "paper"]
        assert titles == ["A Recovered Paper"]


class TestReindexSafetyScanSeesUnownedChunks:
    @pytest.fixture(autouse=True)
    def _creds(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("NX_LOCAL", "0")
        monkeypatch.setenv("CHROMA_API_KEY", "k")
        monkeypatch.setenv("VOYAGE_API_KEY", "k")

    def test_an_all_unowned_collection_still_trips_the_would_destroy_everything_refusal(self) -> None:
        """GH #367 class. Every stored chunk lacks a source and is hidden; the
        scan used to see zero rows, find nothing sourceless, and delete."""
        db = MagicMock(spec=HttpVectorClient)
        db.collection_info.return_value = {"count": 0, "stored_count": 4, "metadata": {}}
        col = _LiveAwareCollection([(f"id{i}", {}, False) for i in range(4)])
        db.get_or_create_collection.return_value = col

        with patch("nexus.commands.collection._t3", return_value=db):
            result = CliRunner().invoke(
                main, ["collection", "reindex", "knowledge__notes", "--force"],
            )

        assert result.exit_code != 0
        assert "refusing to reindex" in result.output.lower()
        assert "all 4 entries" in result.output
        db.delete_collection.assert_not_called()
