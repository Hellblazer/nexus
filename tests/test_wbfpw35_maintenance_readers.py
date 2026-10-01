# SPDX-License-Identifier: AGPL-3.0-or-later
"""nexus-wbfpw.35 (RDR-192 Phase 2 gate minors M1): maintenance readers that
discover documents or guard deletion must read STORED chunks, not live ones.

Under live(c) a chunk with no live owner is hidden from a plain read. These verbs
exist for exactly those chunks: ``nx catalog backfill --from-t3`` registers
documents for chunks that have no catalog row, and ``nx collection reindex``
refuses to delete a collection whose entries lack a source. Both were left on
live rows by nexus-wbfpw.10's sweep, so an all-unowned collection looked empty
to them (the reindex refusal then did not fire and the collection was deleted).

The fake below hides rows without a live owner (unowned, or owned only by a
TOMBSTONED catalog document) unless ``include_non_live=True`` is passed, the same
contract the engine serves, so a reader that forgets the flag sees nothing.

The same flag makes a tombstoned document's chunks visible again, and the verbs'
"already registered?" lookups exclude tombstones, so each reader must also skip a
path the catalog holds as a deliberately deleted document (fix round 2).
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


LIVE = "live"
UNOWNED = "unowned"
TOMBSTONED = "tombstoned"  # owned, but only by a deleted catalog document


class _LiveAwareCollection:
    """Rows are ``(id, metadata, owner_state)`` with the state one of
    :data:`LIVE`, :data:`UNOWNED`, :data:`TOMBSTONED`; every state but LIVE is
    hidden from a read that does not ask for ``include_non_live``."""

    def __init__(self, rows: list[tuple[str, dict, str]]) -> None:
        self._rows = rows
        self.calls: list[dict] = []

    def get(self, *, include=None, limit=100, offset=0, include_non_live=False, **_kw):
        self.calls.append({"limit": limit, "offset": offset, "include_non_live": include_non_live})
        rows = self._rows if include_non_live else [r for r in self._rows if r[2] == LIVE]
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
            (f"c{i}", {"source_path": str(repo_root / "src" / f"{n}.py")}, UNOWNED)
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
                    "title": "RDR-001"}, UNOWNED),
        ])
        t3 = _t3_over("rdr__orphan-deadbeef", col, count=0, stored=1)

        count = _backfill_rdrs(cat, t3, dry_run=False)

        assert count == 1

    def test_paper_backfill_reads_title_from_an_unowned_first_chunk(
        self, catalog_env,  # noqa: F811
    ) -> None:
        from nexus.commands.catalog import _backfill_papers

        cat = ActiveCatalog()
        col = _LiveAwareCollection([("c0", {"title": "A Recovered Paper", "bib_year": 2021}, UNOWNED)])
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
        # collection_info()["count"] IS the stored count (RDR-192 Step 5
        # amendment); only list_collections() rows carry live count beside it.
        db.collection_info.return_value = {"count": 4, "metadata": {}}
        col = _LiveAwareCollection([(f"id{i}", {}, UNOWNED) for i in range(4)])
        db.get_or_create_collection.return_value = col

        with patch("nexus.commands.collection._t3", return_value=db):
            result = CliRunner().invoke(
                main, ["collection", "reindex", "knowledge__notes", "--force"],
            )

        assert result.exit_code != 0
        assert "refusing to reindex" in result.output.lower()
        assert "all 4 entries" in result.output
        db.delete_collection.assert_not_called()


class TestMaintenanceReadersLeaveDeletedDocumentsDeleted:
    """Reading stored chunks brings a tombstoned document's chunks back into view,
    and the verbs' "already registered?" lookups exclude tombstones, so without a
    guard each verb re-registers (or re-indexes) a document that was deleted on
    purpose. These run against the real catalog (``ActiveCatalog``), so the
    tombstone is an engine row and the trash listing is the engine's."""

    def test_from_t3_does_not_re_register_a_deleted_document(
        self, catalog_env, tmp_path: Path,  # noqa: F811
    ) -> None:
        from nexus.commands.catalog import _backfill_per_file_from_t3

        repo_root = tmp_path / "myrepo"
        repo_root.mkdir()
        cat = ActiveCatalog()
        owner = cat.register_owner(
            "myrepo", "repo", repo_hash="abc12345", repo_root=str(repo_root),
        )
        deleted = cat.register(
            owner=owner, title="a.py", content_type="code",
            physical_collection="code__myrepo-abc12345", file_path="src/a.py",
        )
        cat.delete_document(deleted)
        col = _LiveAwareCollection([
            ("c0", {"source_path": str(repo_root / "src" / "a.py")}, TOMBSTONED),
            ("c1", {"source_path": str(repo_root / "src" / "b.py")}, UNOWNED),
        ])
        t3 = _t3_over("code__myrepo-abc12345", col, count=0, stored=2)

        registered = _backfill_per_file_from_t3(
            cat, t3, "code__myrepo-abc12345", dry_run=False,
        )

        assert registered == 1  # b.py only
        assert cat.find_all_by_file_path("src/a.py") == []
        assert len(cat.find_all_by_file_path("src/b.py")) == 1

    def test_from_t3_dry_run_does_not_count_a_deleted_document(
        self, catalog_env, tmp_path: Path,  # noqa: F811
    ) -> None:
        from nexus.commands.catalog import _backfill_per_file_from_t3

        repo_root = tmp_path / "myrepo"
        repo_root.mkdir()
        cat = ActiveCatalog()
        owner = cat.register_owner(
            "myrepo", "repo", repo_hash="abc12345", repo_root=str(repo_root),
        )
        cat.delete_document(cat.register(
            owner=owner, title="a.py", content_type="code",
            physical_collection="code__myrepo-abc12345", file_path="src/a.py",
        ))
        col = _LiveAwareCollection([
            ("c0", {"source_path": str(repo_root / "src" / "a.py")}, TOMBSTONED),
            ("c1", {"source_path": str(repo_root / "src" / "b.py")}, UNOWNED),
        ])
        t3 = _t3_over("code__myrepo-abc12345", col, count=0, stored=2)

        assert _backfill_per_file_from_t3(
            cat, t3, "code__myrepo-abc12345", dry_run=True,
        ) == 1

    def test_rdr_backfill_does_not_re_register_a_deleted_document(
        self, catalog_env, tmp_path: Path,  # noqa: F811
    ) -> None:
        from nexus.commands.catalog import _backfill_rdrs, _get_or_create_curator

        cat = ActiveCatalog()
        gone = str(tmp_path / "docs" / "rdr" / "RDR-001.md")
        kept = str(tmp_path / "docs" / "rdr" / "RDR-002.md")
        curator = _get_or_create_curator(cat, "orphaned-rdrs", writer=cat)
        cat.delete_document(cat.register(
            owner=curator, title="RDR-001", content_type="rdr",
            physical_collection="rdr__orphan-deadbeef", file_path=gone,
        ))
        col = _LiveAwareCollection([
            ("c0", {"source_path": gone, "title": "RDR-001"}, TOMBSTONED),
            ("c1", {"source_path": kept, "title": "RDR-002"}, UNOWNED),
        ])
        t3 = _t3_over("rdr__orphan-deadbeef", col, count=0, stored=2)

        assert _backfill_rdrs(cat, t3, dry_run=False) == 1  # RDR-002 only
        assert cat.find_all_by_file_path(gone) == []

    def test_paper_backfill_does_not_re_register_a_deleted_paper_collection(
        self, catalog_env,  # noqa: F811
    ) -> None:
        from nexus.commands.catalog import _backfill_papers, _get_or_create_curator

        cat = ActiveCatalog()
        curator = _get_or_create_curator(cat, "papers", writer=cat)
        cat.delete_document(cat.register(
            owner=curator, title="A Deleted Paper", content_type="paper",
            physical_collection="docs__paper-x",
        ))
        col = _LiveAwareCollection([("c0", {"title": "A Deleted Paper"}, TOMBSTONED)])
        t3 = _t3_over("docs__paper-x", col, count=0, stored=1)

        assert _backfill_papers(cat, t3, dry_run=False) == 0
        assert [d for d in cat.all_documents() if d.content_type == "paper"] == []

    def test_reindex_does_not_re_index_a_deleted_documents_source(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """The reindex scan collects every stored chunk's source_path to rebuild
        from, deleting the collection first. A path whose only catalog document is
        a tombstone is a deleted document: it is skipped (and said so), not
        rebuilt into a new live one."""
        monkeypatch.setenv("NX_LOCAL", "0")
        monkeypatch.setenv("CHROMA_API_KEY", "k")
        monkeypatch.setenv("VOYAGE_API_KEY", "k")
        name = "docs__notes-abc12345"
        gone = tmp_path / "gone.md"
        kept = tmp_path / "kept.md"
        gone.write_text("deleted on purpose")
        kept.write_text("still wanted")
        col = _LiveAwareCollection([
            ("c0", {"source_path": str(gone)}, TOMBSTONED),
            ("c1", {"source_path": str(kept)}, LIVE),
        ])
        db = MagicMock(spec=HttpVectorClient)
        db.collection_info.return_value = {"count": 2, "metadata": {}}
        db.get_or_create_collection.return_value = col
        cat = MagicMock()
        cat.docs_for_chashes.return_value = {}
        cat.list_trash.return_value = [{
            "tumbler": "1.1.9", "title": "gone.md", "physical_collection": name,
            "content_type": "prose", "file_path": "gone.md", "deleted_at": "2026-10-01",
        }]
        indexed: list[str] = []

        def _record(path, **_kw):
            indexed.append(str(path))
            raise RuntimeError("stop after recording")  # keeps the post-processing chain out

        with patch("nexus.commands.collection._t3", return_value=db), \
             patch("nexus.catalog.factory.make_catalog_reader", return_value=cat), \
             patch("nexus.db.collection_purge.purge_collection_cascade"), \
             patch("nexus.doc_indexer.index_markdown", side_effect=_record):
            result = CliRunner().invoke(main, ["collection", "reindex", name])

        assert indexed == [str(kept)], (indexed, result.output)
        assert "deleted catalog document" in result.output


def test_owner_by_name_accepts_the_tumbler_the_service_client_returns() -> None:
    """``HttpCatalogClient.curator_owner_tumbler_by_name`` returns a Tumbler;
    ``_owner_by_name`` re-parsed it and raised on every re-run once the curator
    existed, which stopped the RDR and paper backfills mid-pass."""
    from nexus.catalog.tumbler import Tumbler
    from nexus.commands.catalog import _owner_by_name

    as_tumbler = MagicMock()
    as_tumbler.curator_owner_tumbler_by_name.return_value = Tumbler.parse("1.7")
    as_text = MagicMock()
    as_text.curator_owner_tumbler_by_name.return_value = "1.7"
    absent = MagicMock()
    absent.curator_owner_tumbler_by_name.return_value = None

    assert _owner_by_name(as_tumbler, "papers") == Tumbler.parse("1.7")
    assert _owner_by_name(as_text, "papers") == Tumbler.parse("1.7")
    assert _owner_by_name(absent, "papers") is None
