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
        self, catalog_env, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,  # noqa: F811
    ) -> None:
        """The reindex scan collects every stored chunk's source_path to rebuild
        from, deleting the collection first. A path whose only catalog document is
        a tombstone is a deleted document: it is skipped (and said so), not
        rebuilt into a new live one."""
        name = "docs__notes-abc12345"
        gone = tmp_path / "gone.md"
        kept = tmp_path / "kept.md"
        gone.write_text("deleted on purpose")
        kept.write_text("still wanted")
        cat = ActiveCatalog()
        owner = cat.register_owner("notes", "repo", repo_hash="abc12345", repo_root=str(tmp_path))
        cat.delete_document(cat.register(
            owner=owner, title="gone.md", content_type="prose",
            physical_collection=name, file_path="gone.md",
        ))
        col = _LiveAwareCollection([
            ("c0", {"source_path": str(gone)}, TOMBSTONED),
            ("c1", {"source_path": str(kept)}, LIVE),
        ])

        result, indexed, purged = _run_reindex(monkeypatch, name, col, cat)

        assert indexed == [str(kept)], (indexed, result.output)
        assert purged == [name]
        assert "deleted catalog document" in result.output


def _run_reindex(monkeypatch: pytest.MonkeyPatch, name: str, col, cat, *, force: bool = False):
    """Drive ``nx collection reindex NAME`` over *col* with *cat* as the catalog
    reader (or, when *cat* is a plain function, as the reader factory). Returns
    ``(result, source paths handed to the indexer, collections purged)``; the
    indexer is stubbed to record and stop."""
    monkeypatch.setenv("NX_LOCAL", "0")
    monkeypatch.setenv("CHROMA_API_KEY", "k")
    monkeypatch.setenv("VOYAGE_API_KEY", "k")
    db = MagicMock(spec=HttpVectorClient)
    db.collection_info.return_value = {"count": len(col._rows), "metadata": {}}
    db.get_or_create_collection.return_value = col
    indexed: list[str] = []
    purged: list[str] = []

    def _record(path, **_kw):
        indexed.append(str(path))
        raise RuntimeError("stop after recording")  # keeps the post-processing chain out

    if isinstance(cat, ActiveCatalog):
        # Resolve the real reader BEFORE patching the factory: the facade reads
        # through that factory, so left as is it would call itself forever.
        from tests._catalog_fixture_ops import active_reader
        cat = active_reader()
    argv = ["collection", "reindex", name] + (["--force"] if force else [])
    factory = (lambda: cat) if hasattr(cat, "list_trash") else cat
    with patch("nexus.commands.collection._t3", return_value=db), \
         patch("nexus.catalog.factory.make_catalog_reader", side_effect=factory), \
         patch("nexus.db.collection_purge.purge_collection_cascade",
               side_effect=lambda _db, n: purged.append(n)), \
         patch("nexus.doc_indexer.index_markdown", side_effect=_record):
        result = CliRunner().invoke(main, argv)
    return result, indexed, purged


class TestReindexTombstoneGuardIsExactAndFailsClosed:
    """Fix round 3. The guard drops a path and the collection is then purged and
    rebuilt from what is left, so a wrong drop is data loss and a missing guard is
    a revival. It must match the catalog's own normalised path exactly, never drop
    a path the catalog holds LIVE, and refuse rather than run unguarded."""

    NAME = "docs__notes-abc12345"

    def _seed(self, tmp_path: Path):
        cat = ActiveCatalog()
        owner = cat.register_owner("notes", "repo", repo_hash="abc12345", repo_root=str(tmp_path))
        return cat, owner

    def test_a_re_indexed_file_with_a_tombstone_and_a_live_document_is_kept(
        self, catalog_env, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,  # noqa: F811
    ) -> None:
        # Delete a document, index the same file again: the catalog holds the
        # tombstone AND a live document at one (owner, path), and the stored
        # chunks are one row owned by both. The tombstone must not stand in for
        # the live document, or the verb purges its chunks and rebuilds none.
        cat, owner = self._seed(tmp_path)
        again = tmp_path / "again.md"
        again.write_text("re-indexed after a delete")
        cat.delete_document(cat.register(
            owner=owner, title="again.md", content_type="prose",
            physical_collection=self.NAME, file_path="again.md",
        ))
        cat.register(
            owner=owner, title="again.md", content_type="prose",
            physical_collection=self.NAME, file_path="again.md",
        )
        assert cat.by_file_path(owner, "again.md") is not None
        assert len(cat.list_trash()) == 1
        col = _LiveAwareCollection([("c0", {"source_path": str(again)}, LIVE)])

        result, indexed, _purged = _run_reindex(monkeypatch, self.NAME, col, cat)

        assert indexed == [str(again)], (indexed, result.output)
        assert "deleted catalog document" not in result.output

    def test_a_deleted_root_file_does_not_claim_a_same_named_file_deeper_in(
        self, catalog_env, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,  # noqa: F811
    ) -> None:
        # Tombstone "README.md" (the repo root's) against a live pkg/sub/README.md:
        # a suffix match dropped the live file and purged its chunks.
        cat, owner = self._seed(tmp_path)
        deeper = tmp_path / "pkg" / "sub" / "README.md"
        deeper.parent.mkdir(parents=True)
        deeper.write_text("a different readme")
        cat.delete_document(cat.register(
            owner=owner, title="README.md", content_type="prose",
            physical_collection=self.NAME, file_path="README.md",
        ))
        cat.register(
            owner=owner, title="README.md", content_type="prose",
            physical_collection=self.NAME, file_path="pkg/sub/README.md",
        )
        col = _LiveAwareCollection([("c0", {"source_path": str(deeper)}, LIVE)])

        result, indexed, _purged = _run_reindex(monkeypatch, self.NAME, col, cat)

        assert indexed == [str(deeper)], (indexed, result.output)

    def test_it_refuses_when_every_source_is_a_deleted_document(
        self, catalog_env, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,  # noqa: F811
    ) -> None:
        # Nothing left to rebuild: purging would leave an empty collection.
        cat, owner = self._seed(tmp_path)
        gone = tmp_path / "gone.md"
        gone.write_text("x")
        cat.delete_document(cat.register(
            owner=owner, title="gone.md", content_type="prose",
            physical_collection=self.NAME, file_path="gone.md",
        ))
        col = _LiveAwareCollection([("c0", {"source_path": str(gone)}, TOMBSTONED)])

        result, indexed, purged = _run_reindex(monkeypatch, self.NAME, col, cat)

        assert result.exit_code != 0
        assert "every source" in result.output and "deleted catalog document" in result.output
        assert purged == [] and indexed == []

    def test_it_refuses_when_the_catalog_cannot_be_read(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        def _boom():
            raise RuntimeError("catalog unreachable")

        src = tmp_path / "a.md"
        src.write_text("x")
        col = _LiveAwareCollection([("c0", {"source_path": str(src)}, LIVE)])

        result, indexed, purged = _run_reindex(monkeypatch, self.NAME, col, _boom)

        assert result.exit_code != 0
        assert "catalog could not be read" in result.output
        assert purged == [] and indexed == []

    def test_it_refuses_on_an_engine_whose_trash_entries_carry_no_file_path(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        src = tmp_path / "a.md"
        src.write_text("x")
        col = _LiveAwareCollection([("c0", {"source_path": str(src)}, LIVE)])
        cat = MagicMock()
        cat.docs_for_chashes.return_value = {}
        cat.list_trash.return_value = [{
            "tumbler": "1.1.9", "title": "old.md", "physical_collection": self.NAME,
            "content_type": "prose", "deleted_at": "2026-10-01",
        }]

        result, indexed, purged = _run_reindex(monkeypatch, self.NAME, col, cat)

        assert result.exit_code != 0
        assert "engine-service-v0.1.142" in result.output
        assert purged == [] and indexed == []

    def test_a_null_file_path_is_accepted_and_the_verb_runs(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        # A new engine sends null for a tombstoned paper or note. That is not an
        # old engine, and must not refuse the verb.
        src = tmp_path / "a.md"
        src.write_text("x")
        col = _LiveAwareCollection([("c0", {"source_path": str(src)}, LIVE)])
        cat = MagicMock()
        cat.docs_for_chashes.return_value = {}
        cat.get_owner_by_prefix.return_value = {"repo_root": ""}
        cat.by_file_path.return_value = None
        cat.list_trash.return_value = [{
            "tumbler": "1.1.9", "title": "a paper", "physical_collection": self.NAME,
            "content_type": "paper", "file_path": None, "deleted_at": "2026-10-01",
        }]

        result, indexed, _purged = _run_reindex(monkeypatch, self.NAME, col, cat)

        assert indexed == [str(src)], (indexed, result.output)


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


class _AsAnOldEngine:
    """The real catalog, except its trash listing is an engine that predates the
    ``file_path`` field: entries come back without the key."""

    def __init__(self, cat: ActiveCatalog) -> None:
        self._cat = cat
        self.trash_reads = 0

    def list_trash(self, **kw):
        self.trash_reads += 1
        return [{k: v for k, v in d.items() if k != "file_path"} for d in self._cat.list_trash(**kw)]

    def __getattr__(self, name: str):
        return getattr(self._cat, name)


class _CountingTrash:
    def __init__(self, cat: ActiveCatalog) -> None:
        self._cat = cat
        self.trash_reads = 0

    def list_trash(self, **kw):
        self.trash_reads += 1
        return self._cat.list_trash(**kw)

    def __getattr__(self, name: str):
        return getattr(self._cat, name)


class TestBackfillTombstoneGuardIsExactLiveAwareAndFailsClosed:
    """Fix round 3, the backfill call sites of the same guard."""

    def _repo(self, tmp_path: Path):
        repo_root = tmp_path / "myrepo"
        repo_root.mkdir()
        cat = ActiveCatalog()
        owner = cat.register_owner(
            "myrepo", "repo", repo_hash="abc12345", repo_root=str(repo_root),
        )
        return cat, owner, repo_root

    def test_from_t3_registers_a_file_whose_name_matches_a_deleted_root_file(
        self, catalog_env, tmp_path: Path,  # noqa: F811
    ) -> None:
        # Tombstone README.md; the stored chunk is pkg/sub/README.md. A suffix
        # match read the second as the first and refused to register it.
        from nexus.commands.catalog import _backfill_per_file_from_t3

        cat, owner, repo_root = self._repo(tmp_path)
        cat.delete_document(cat.register(
            owner=owner, title="README.md", content_type="code",
            physical_collection="code__myrepo-abc12345", file_path="README.md",
        ))
        col = _LiveAwareCollection([
            ("c0", {"source_path": str(repo_root / "pkg" / "sub" / "README.md")}, UNOWNED),
        ])
        t3 = _t3_over("code__myrepo-abc12345", col, count=0, stored=1)

        registered = _backfill_per_file_from_t3(
            cat, t3, "code__myrepo-abc12345", dry_run=False,
        )

        assert registered == 1
        assert len(cat.find_all_by_file_path("pkg/sub/README.md")) == 1

    def test_from_t3_does_not_call_a_re_indexed_file_a_deleted_document(
        self, catalog_env, tmp_path: Path, capsys: pytest.CaptureFixture[str],  # noqa: F811
    ) -> None:
        # Tombstone and live document at one (owner, path): not "deleted", so no
        # "held as deleted documents" line, and (dry run) it is counted.
        from nexus.commands.catalog import _backfill_per_file_from_t3

        cat, owner, repo_root = self._repo(tmp_path)
        for _ in range(2):
            t = cat.register(
                owner=owner, title="a.py", content_type="code",
                physical_collection="code__myrepo-abc12345", file_path="src/a.py",
            )
            if _ == 0:
                cat.delete_document(t)
        col = _LiveAwareCollection([
            ("c0", {"source_path": str(repo_root / "src" / "a.py")}, LIVE),
        ])
        t3 = _t3_over("code__myrepo-abc12345", col, count=1, stored=1)

        assert _backfill_per_file_from_t3(
            cat, t3, "code__myrepo-abc12345", dry_run=True,
        ) == 1
        assert "deleted documents" not in capsys.readouterr().out

    def test_from_t3_refuses_on_an_engine_without_file_path(
        self, catalog_env, tmp_path: Path,  # noqa: F811
    ) -> None:
        from nexus.catalog.tombstones import TombstoneGuardUnavailable
        from nexus.commands.catalog import _backfill_per_file_from_t3

        cat, owner, repo_root = self._repo(tmp_path)
        cat.delete_document(cat.register(
            owner=owner, title="a.py", content_type="code",
            physical_collection="code__myrepo-abc12345", file_path="src/a.py",
        ))
        col = _LiveAwareCollection([
            ("c0", {"source_path": str(repo_root / "src" / "a.py")}, TOMBSTONED),
        ])
        t3 = _t3_over("code__myrepo-abc12345", col, count=0, stored=1)

        with pytest.raises(TombstoneGuardUnavailable):
            _backfill_per_file_from_t3(
                _AsAnOldEngine(cat), t3, "code__myrepo-abc12345", dry_run=False,
            )
        assert cat.find_all_by_file_path("src/a.py") == []

    def test_the_all_sweep_does_not_swallow_the_refusal(
        self, catalog_env, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,  # noqa: F811
    ) -> None:
        # `--all-repo-collections` skips a collection that raises ClickException
        # (a non-repo-owned one). The refusal is a ClickException too and must
        # still end the verb, not read as one more skipped collection.
        from nexus.catalog.tombstones import TombstoneGuardUnavailable
        from nexus.commands import catalog as catalog_cmds

        def _refuse(*_a, **_k):
            raise TombstoneGuardUnavailable("engine too old")

        t3 = MagicMock()
        t3.list_collections.return_value = [{"name": "docs__myrepo-abc12345", "count": 1}]
        monkeypatch.setattr(catalog_cmds, "_get_catalog", lambda: MagicMock())
        monkeypatch.setattr(catalog_cmds, "_get_catalog_writer", lambda: MagicMock())
        monkeypatch.setattr(catalog_cmds, "_make_t3", lambda: t3)
        monkeypatch.setattr(catalog_cmds, "_backfill_per_file_from_t3", _refuse)

        result = CliRunner().invoke(
            main, ["catalog", "backfill", "--from-t3", "--all-repo-collections"],
        )

        assert result.exit_code != 0
        assert "engine too old" in result.output
        assert "skipped" not in result.output

    def test_rdr_backfill_does_not_call_a_re_indexed_document_deleted(
        self, catalog_env, tmp_path: Path, capsys: pytest.CaptureFixture[str],  # noqa: F811
    ) -> None:
        # Tombstone and live document at one (owner, path): a dry run still counts
        # it (nothing was skipped as deleted).
        from nexus.commands.catalog import _backfill_rdrs, _get_or_create_curator

        cat = ActiveCatalog()
        path = str(tmp_path / "docs" / "rdr" / "RDR-001.md")
        curator = _get_or_create_curator(cat, "orphaned-rdrs", writer=cat)
        for first in (True, False):
            t = cat.register(
                owner=curator, title="RDR-001", content_type="rdr",
                physical_collection="rdr__orphan-deadbeef", file_path=path,
            )
            if first:
                cat.delete_document(t)
        col = _LiveAwareCollection([("c0", {"source_path": path, "title": "RDR-001"}, LIVE)])
        t3 = _t3_over("rdr__orphan-deadbeef", col, count=1, stored=1)

        assert _backfill_rdrs(cat, t3, dry_run=True) == 1
        assert "deleted documents" not in capsys.readouterr().out

    def test_rdr_backfill_refuses_on_an_engine_without_file_path(
        self, catalog_env, tmp_path: Path,  # noqa: F811
    ) -> None:
        from nexus.catalog.tombstones import TombstoneGuardUnavailable
        from nexus.commands.catalog import _backfill_rdrs, _get_or_create_curator

        cat = ActiveCatalog()
        gone = str(tmp_path / "docs" / "rdr" / "RDR-001.md")
        curator = _get_or_create_curator(cat, "orphaned-rdrs", writer=cat)
        cat.delete_document(cat.register(
            owner=curator, title="RDR-001", content_type="rdr",
            physical_collection="rdr__orphan-deadbeef", file_path=gone,
        ))
        col = _LiveAwareCollection([("c0", {"source_path": gone, "title": "RDR-001"}, TOMBSTONED)])
        t3 = _t3_over("rdr__orphan-deadbeef", col, count=0, stored=1)

        # Not caught as one "unreadable collection": the pass refuses outright.
        with pytest.raises(TombstoneGuardUnavailable):
            _backfill_rdrs(_AsAnOldEngine(cat), t3, dry_run=False)
        assert cat.find_all_by_file_path(gone) == []

    def test_paper_backfill_refuses_on_an_engine_without_file_path(
        self, catalog_env,  # noqa: F811
    ) -> None:
        from nexus.catalog.tombstones import TombstoneGuardUnavailable
        from nexus.commands.catalog import _backfill_papers, _get_or_create_curator

        cat = ActiveCatalog()
        curator = _get_or_create_curator(cat, "papers", writer=cat)
        cat.delete_document(cat.register(
            owner=curator, title="A Deleted Paper", content_type="paper",
            physical_collection="docs__paper-x",
        ))
        col = _LiveAwareCollection([("c0", {"title": "A Deleted Paper"}, TOMBSTONED)])
        t3 = _t3_over("docs__paper-x", col, count=0, stored=1)

        with pytest.raises(TombstoneGuardUnavailable):
            _backfill_papers(_AsAnOldEngine(cat), t3, dry_run=False)
        assert [d for d in cat.all_documents() if d.content_type == "paper"] == []

    def test_rdr_and_paper_backfills_read_the_trash_once_for_all_collections(
        self, catalog_env, tmp_path: Path,  # noqa: F811
    ) -> None:
        from nexus.commands.catalog import _backfill_papers, _backfill_rdrs

        cat = _CountingTrash(ActiveCatalog())
        rdr_cols = ["rdr__a-deadbeef", "rdr__b-deadbeef", "rdr__c-deadbeef"]
        cols = {
            n: _LiveAwareCollection([
                ("c0", {"source_path": str(tmp_path / f"{n}.md"), "title": n}, UNOWNED),
            ])
            for n in rdr_cols
        }
        t3 = MagicMock(spec=HttpVectorClient)
        t3.list_collections.return_value = [
            {"name": n, "count": 0, "stored_count": 1} for n in rdr_cols
        ]
        t3.get_or_create_collection.side_effect = lambda n: cols[n]

        _backfill_rdrs(cat, t3, dry_run=False)
        assert cat.trash_reads == 1

        paper_cols = ["docs__paper-a", "docs__paper-b", "docs__paper-c"]
        pcols = {
            n: _LiveAwareCollection([("c0", {"title": n}, UNOWNED)]) for n in paper_cols
        }
        t3.list_collections.return_value = [
            {"name": n, "count": 0, "stored_count": 1} for n in paper_cols
        ]
        t3.get_or_create_collection.side_effect = lambda n: pcols[n]
        cat.trash_reads = 0

        _backfill_papers(cat, t3, dry_run=False)
        assert cat.trash_reads == 1


class TestOrphanBackfillLeavesDeletedDocumentsDeleted:
    """``nx catalog orphan-backfill`` registers a Document per title group of the
    stored chunks, which include a deleted-but-not-purged document's. (The other
    two verbs that read stored chunks for a catalog, ``manifest_backfill`` and
    ``manifest_heal``, only write manifest rows for documents that already exist
    and are live, so they cannot bring a deleted document back.)"""

    COLLECTION = "knowledge__art-papers"

    def _groups(self, titles):
        from nexus.catalog.orphan_backfill import ChunkRef, TitleGroup

        return [
            TitleGroup(title=t, chunks=[ChunkRef(cid=f"{t}-c", chash=f"{t}-h", chunk_index=0)])
            for t in titles
        ]

    def _cat(self, trash, live_titles=()):
        cat = MagicMock()
        cat.list_trash.return_value = trash
        cat.list_by_collection.return_value = [MagicMock(title=t) for t in live_titles]
        return cat

    def _tomb(self, title, collection=None, **over):
        row = {
            "tumbler": "1.9.4", "title": title, "file_path": None,
            "physical_collection": collection or self.COLLECTION,
            "content_type": "knowledge", "deleted_at": "2026-10-01",
        }
        row.update(over)
        return row

    def test_a_group_of_a_deleted_document_is_dropped(self) -> None:
        from nexus.catalog.orphan_backfill import drop_deleted_title_groups

        kept, dropped = drop_deleted_title_groups(
            self._cat([self._tomb("Gone Paper")]), self.COLLECTION,
            self._groups(["Gone Paper", "Wanted Paper"]),
        )

        assert [g.title for g in kept] == ["Wanted Paper"]
        assert dropped == ["Gone Paper"]

    def test_a_title_a_live_document_also_holds_is_kept(self) -> None:
        from nexus.catalog.orphan_backfill import drop_deleted_title_groups

        kept, dropped = drop_deleted_title_groups(
            self._cat([self._tomb("Same Title")], live_titles=["Same Title"]),
            self.COLLECTION, self._groups(["Same Title"]),
        )

        assert [g.title for g in kept] == ["Same Title"] and dropped == []

    def test_a_tombstone_in_another_collection_does_not_drop_the_group(self) -> None:
        from nexus.catalog.orphan_backfill import drop_deleted_title_groups

        kept, dropped = drop_deleted_title_groups(
            self._cat([self._tomb("Gone Paper", collection="knowledge__other")]),
            self.COLLECTION, self._groups(["Gone Paper"]),
        )

        assert len(kept) == 1 and dropped == []

    def test_synthetic_registers_nothing_for_a_deleted_documents_group(self) -> None:
        from nexus.catalog import orphan_backfill as ob

        cat = self._cat([self._tomb("Gone Paper")])
        t3 = MagicMock()
        registered: list[list[str]] = []
        with patch("nexus.commands.catalog._get_catalog", return_value=cat), \
             patch("nexus.db.make_t3", return_value=t3), \
             patch.object(ob, "gather_titled_chunks",
                          return_value=self._groups(["Gone Paper", "Wanted Paper"])), \
             patch.object(ob, "register_synthetic",
                          side_effect=lambda _c, _o, _col, groups: (
                              registered.append([g.title for g in groups]) or (len(groups), 0))):
            result = CliRunner().invoke(
                main, ["catalog", "orphan-backfill", "synthetic", self.COLLECTION, "--no-dry-run"],
            )

        assert result.exit_code == 0, result.output
        assert registered == [["Wanted Paper"]]
        assert "Skipping 1 title group(s) of deleted catalog documents" in result.output
