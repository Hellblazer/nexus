# SPDX-License-Identifier: AGPL-3.0-or-later
"""Tests for RDR-060 path rationalization: OwnerRecord.repo_root + DDL + resolve_path + relative paths.

CATALOG SUBSTRATE (nexus-i711w Stage 2). Four of this file's six classes
never needed a catalog at all — ``OwnerRecord`` is a dataclass in
``nexus.catalog.tumbler`` (not retiring), ``make_relative`` is pure path
arithmetic, and the ``_markdown_chunks`` / ``_index_document`` classes drive
``nexus.doc_indexer``. They were all held hostage by two module-level
imports of dying modules, which kill COLLECTION for the whole file.

Those two imports are gone; the dying names are imported inside the bodies
that still need them. What remains pinned to the local catalog:

  - ``TestCatalogDBMigration`` — DIE. Its subject IS ``CatalogDB``'s
    ALTER-on-open DDL, a SQLite-only observable.
  - ``TestResolvePath``, four of seven — PORT-BLOCKED on nexus-5i864 (see
    the class docstring). Two of the seven DO port and now run against
    whichever catalog is live.
  - ``test_resolve_path_no_RepoRegistry_import_in_module`` — DIE by
    construction: it greps ``src/nexus/catalog/catalog_docs.py``, a file on
    the deletion list, so it retires with its subject.

⚠️ FINDING (nexus-i711w, reported not fixed here): ``make_relative`` is
DEFINED in the dying ``nexus/catalog/catalog.py`` but imported by six LIVE
call sites — ``doc_indexer.py`` (x4), ``mcp/catalog.py``,
``commands/catalog.py`` (x2), ``commands/doctor.py``. It is substrate-neutral
pure path arithmetic and must be RELOCATED, not deleted, or indexing and
``doctor --fix-paths`` break. ``TestMakeRelative`` below is its only
coverage.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest

from nexus.catalog.tumbler import OwnerRecord, Tumbler, _filter_fields
from tests._catalog_fixture_ops import ActiveCatalog


@pytest.fixture()
def active_catalog() -> ActiveCatalog:
    """Seed and read through whichever catalog is live (nexus-i711w Stage 2)."""
    return ActiveCatalog()


class TestOwnerRecordRepoRoot:
    """OwnerRecord repo_root field basics.

    Substrate-neutral: ``OwnerRecord`` / ``_filter_fields`` live in
    ``nexus.catalog.tumbler``, which is NOT on the retirement list. Unchanged
    by the i711w port beyond no longer being hostage to the file's
    module-level imports.
    """

    def test_default_repo_root_is_empty_string(self):
        rec = OwnerRecord(owner="1.1", name="r", owner_type="repo", repo_hash="h", description="d")
        assert rec.repo_root == ""

    def test_explicit_repo_root(self):
        rec = OwnerRecord(
            owner="1.1", name="r", owner_type="repo", repo_hash="h",
            description="d", repo_root="/home/user/repo",
        )
        assert rec.repo_root == "/home/user/repo"

    def test_jsonl_roundtrip_with_repo_root(self):
        rec = OwnerRecord(
            owner="1.1", name="r", owner_type="repo", repo_hash="h",
            description="d", repo_root="/tmp/repo",
        )
        serialized = json.dumps(rec.__dict__)
        deserialized = json.loads(serialized)
        rec2 = OwnerRecord(**_filter_fields(OwnerRecord, deserialized))
        assert rec2.repo_root == "/tmp/repo"

    def test_jsonl_backwards_compat_without_repo_root(self):
        """Old JSONL entries without repo_root should deserialize with default ''."""
        old_data = {"owner": "1.1", "name": "r", "owner_type": "repo", "repo_hash": "h", "description": "d"}
        rec = OwnerRecord(**_filter_fields(OwnerRecord, old_data))
        assert rec.repo_root == ""


@pytest.mark.usefixtures("local_catalog_backend")
class TestCatalogDBMigration:
    """DDL migration: existing DBs get repo_root column added.

    nexus-i711w: DIE. The whole class's subject is ``CatalogDB``'s SQLite
    schema and its ALTER-on-open migration — there is no service-mode
    expression of either (the engine's schema is Liquibase-managed and
    covered by the Java suite). ``nexus.catalog.catalog_db`` is imported
    inside each body so the FILE still collects after the deletion; the class
    itself retires with ``nexus/catalog/catalog_db.py``.
    """

    def test_new_db_has_repo_root_column(self, tmp_path):
        from nexus.catalog.catalog_db import CatalogDB

        db = CatalogDB(tmp_path / "catalog.db")
        # Should be able to query repo_root without error
        db.execute("SELECT repo_root FROM owners LIMIT 0")
        db.close()

    def test_migration_adds_repo_root_to_existing_db(self, tmp_path):
        """Simulate an existing DB without repo_root, then open with new CatalogDB."""
        import sqlite3

        from nexus.catalog.catalog_db import CatalogDB

        db_path = tmp_path / "catalog.db"
        # Create old-schema DB manually
        conn = sqlite3.connect(str(db_path))
        conn.execute("""
            CREATE TABLE owners (
                tumbler_prefix TEXT PRIMARY KEY,
                name TEXT NOT NULL UNIQUE,
                owner_type TEXT NOT NULL,
                repo_hash TEXT,
                description TEXT
            )
        """)
        conn.execute("INSERT INTO owners VALUES ('1.1', 'old-repo', 'repo', 'hash1', 'desc')")
        conn.commit()
        conn.close()

        # Open with new CatalogDB — should migrate
        db = CatalogDB(db_path)
        row = db.execute("SELECT repo_root FROM owners WHERE tumbler_prefix = '1.1'").fetchone()
        assert row[0] == ""  # default empty string
        db.close()

    def test_rebuild_stores_repo_root(self, tmp_path):
        from nexus.catalog.catalog_db import CatalogDB

        db = CatalogDB(tmp_path / "catalog.db")
        owner = OwnerRecord(
            owner="1.1", name="test-repo", owner_type="repo",
            repo_hash="abc", description="test", repo_root="/home/user/repo",
        )
        db.rebuild(owners={"1.1": owner}, documents={}, links=[])
        row = db.execute("SELECT repo_root FROM owners WHERE tumbler_prefix = '1.1'").fetchone()
        assert row[0] == "/home/user/repo"
        db.close()

    def test_rebuild_stores_empty_repo_root(self, tmp_path):
        from nexus.catalog.catalog_db import CatalogDB

        db = CatalogDB(tmp_path / "catalog.db")
        owner = OwnerRecord(
            owner="1.1", name="test-repo", owner_type="repo",
            repo_hash="abc", description="test",
        )
        db.rebuild(owners={"1.1": owner}, documents={}, links=[])
        row = db.execute("SELECT repo_root FROM owners WHERE tumbler_prefix = '1.1'").fetchone()
        assert row[0] == ""
        db.close()


# ── resolve_path (nexus-1p4g.2) ──────────────────────────────────────────────


class TestResolvePath:
    """Catalog.resolve_path() resolution tests.

    ⚠️ FOUR OF THESE SEVEN ARE PORT-BLOCKED ON nexus-5i864 AND STAY PINNED TO
    THE LOCAL CATALOG. Verified in source: ``HttpCatalogClient.resolve_path``
    (http_catalog_client.py:1150-1152) is a bare
    ``Path(entry.file_path) if entry and entry.file_path else None``. It drops
    BOTH behaviours the local implementation has
    (``catalog_docs.py``:462+):

      1. the ``owner.repo_root`` recombination that turns a stored RELATIVE
         ``file_path`` into an absolute on-disk path, and
      2. the curator guard that returns ``None`` for owners which have no
         repo root at all.

    On the service arm a relative ``src/test.py`` therefore comes back as the
    relative ``Path("src/test.py")`` and a curator doc comes back as
    ``Path("paper.pdf")`` instead of ``None`` — so converting these
    assertions would mean inverting them. THESE FOUR ARE THE ONLY
    repo-wide coverage of ``resolve_path``; they must not be lost. They
    become the regression tests when 5i864 is fixed.

    The two that DO port (unknown tumbler, absolute file_path) agree across
    substrates and now exercise whichever catalog is live.
    """

    def _make_local_catalog(self, tmp_path: Path) -> Any:
        """A real LOCAL ``Catalog``, for the nexus-5i864-pinned tests only."""
        from nexus.catalog.catalog import Catalog

        cat_dir = tmp_path / "catalog"
        cat_dir.mkdir()
        (cat_dir / "owners.jsonl").touch()
        (cat_dir / "documents.jsonl").touch()
        (cat_dir / "links.jsonl").touch()
        return Catalog(cat_dir, cat_dir / ".catalog.db")

    @pytest.mark.usefixtures("local_catalog_backend")
    def test_resolve_path_with_repo_root(self, tmp_path: Path) -> None:
        """PINNED — nexus-5i864 defect (1): the repo_root recombination is
        absent service-side, so this returns ``Path("src/test.py")`` there.
        """
        cat = self._make_local_catalog(tmp_path)
        repo_dir = tmp_path / "myrepo"
        repo_dir.mkdir()
        owner = cat.register_owner(
            "test-repo", "repo", repo_hash="abc12345", repo_root=str(repo_dir),
        )
        tumbler = cat.register(
            owner, "test.py", content_type="code", file_path="src/test.py",
        )
        result = cat.resolve_path(tumbler)
        assert result == repo_dir / "src" / "test.py"

    @pytest.mark.usefixtures("local_catalog_backend")
    def test_resolve_path_curator_returns_none(self, tmp_path: Path) -> None:
        """PINNED — nexus-5i864 defect (2): the curator guard is absent
        service-side, so this returns ``Path("paper.pdf")`` there.
        """
        cat = self._make_local_catalog(tmp_path)
        owner = cat.register_owner("papers", "curator")
        tumbler = cat.register(
            owner, "paper.pdf", content_type="paper", file_path="paper.pdf",
        )
        assert cat.resolve_path(tumbler) is None

    def test_resolve_path_unknown_tumbler(self, active_catalog: Any) -> None:
        """PORTED: an unregistered tumbler resolves to ``None`` on both arms
        (local returns None from the owner lookup, the service client's
        ``resolve`` returns None and short-circuits).
        """
        assert active_catalog.resolve_path(Tumbler.parse("1.99.99")) is None

    def test_resolve_path_absolute_file_path(self, active_catalog: Any) -> None:
        """Existing absolute file_path returned as-is.

        PORTED: this is the one input shape both implementations agree on —
        an already-absolute ``file_path`` needs no repo_root recombination,
        so ``Path(entry.file_path)`` is the correct answer on the service arm
        too.
        """
        owner = active_catalog.register_owner(
            "resolve-path-abs", "repo", repo_hash="abc12345",
        )
        tumbler = active_catalog.register(
            owner, "test.py", content_type="code",
            file_path="/absolute/path/test.py",
        )
        assert active_catalog.resolve_path(tumbler) == Path(
            "/absolute/path/test.py"
        )

    @pytest.mark.usefixtures("local_catalog_backend")
    def test_resolve_path_empty_repo_root_no_registry(self, tmp_path: Path) -> None:
        """repo_root empty and no registry -> None.

        PINNED — nexus-5i864. The ``None`` here comes from the local
        repo_root/registry logic that the service client does not have; there
        it returns ``Path("src/test.py")``. The ``_default_registry_path``
        patch target is itself in the dying module.
        """
        cat = self._make_local_catalog(tmp_path)
        owner = cat.register_owner("test-repo", "repo", repo_hash="abc12345")
        tumbler = cat.register(
            owner, "test.py", content_type="code", file_path="src/test.py",
        )
        with patch(
            "nexus.catalog.catalog._default_registry_path",
            return_value=tmp_path / "nonexistent" / "repos.json",
        ):
            assert cat.resolve_path(tumbler) is None

    @pytest.mark.usefixtures("local_catalog_backend")
    def test_resolve_path_empty_repo_root_returns_none_no_registry_consult(
        self, tmp_path: Path,
    ) -> None:
        """RDR-137 Phase 3.6 (nexus-tts0d.11, OQ-11): legacy owners with
        empty ``repo_root`` no longer fall back to ``repos.json``. The
        previous RepoRegistry-iteration path is excised; the function
        now returns ``None`` and emits a DEBUG event so the legacy
        owner is observable for re-index.

        PINNED — nexus-5i864, and the STRONGEST of the four: it asserts BOTH
        that ``resolve_path`` returns ``None`` for an empty-repo_root owner
        AND that the ``catalog_resolve_path_legacy_owner_missing_repo_root``
        DEBUG event fires. The service client emits no such event and returns
        a relative Path, so neither assertion has a service-mode form. This
        is the observability half of 5i864.
        """
        import logging
        import structlog
        from structlog.testing import capture_logs

        repo_dir = tmp_path / "myrepo"
        repo_dir.mkdir()
        repo_hash = hashlib.sha256(str(repo_dir).encode()).hexdigest()[:8]

        # Bump structlog so the DEBUG event fires under capture_logs.
        structlog.configure(
            wrapper_class=structlog.make_filtering_bound_logger(logging.DEBUG),
        )
        try:
            cat = self._make_local_catalog(tmp_path)
            owner = cat.register_owner("test-repo", "repo", repo_hash=repo_hash)
            tumbler = cat.register(
                owner, "test.py", content_type="code", file_path="src/test.py",
            )
            with capture_logs() as cap:
                result = cat.resolve_path(tumbler)
            assert result is None
            # DEBUG event names the owner and gives a hint.
            assert any(
                e.get("event") == "catalog_resolve_path_legacy_owner_missing_repo_root"
                and e.get("repo_hash") == repo_hash
                for e in cap
            )
        finally:
            structlog.configure(
                wrapper_class=structlog.make_filtering_bound_logger(
                    logging.WARNING,
                ),
            )

    def test_resolve_path_no_RepoRegistry_import_in_module(self) -> None:
        """RDR-137 Phase 3.6 OQ-11 acceptance gate: ``RepoRegistry`` is
        no longer imported in ``catalog_docs.py`` after this cutover.

        Note: ``_repo_identity`` (a pure helper, not registry-coupled)
        stays for now — it migrates with the other helpers in
        ``nexus-tts0d.21`` (relocation to ``nexus.repo_identity``).
        Phase 5's lint guard fires on ``RepoRegistry`` and
        ``repos.json`` re-introduction, not on the helpers.

        nexus-i711w: DIE BY CONSTRUCTION, not by substrate. This is a
        source-text lint guard over ``src/nexus/catalog/catalog_docs.py`` — a
        file on the deletion list — so it retires with its subject rather
        than porting. Left exactly as-is: it passes today, and it keeps
        guarding the re-introduction until the file goes.
        """
        from pathlib import Path as _Path

        src = _Path(__file__).resolve().parent.parent / (
            "src/nexus/catalog/catalog_docs.py"
        )
        text = src.read_text()
        assert "RepoRegistry" not in text
        assert "_default_registry_path" not in text
        assert "repos.json" not in text


# ── make_relative (nexus-1p4g.3) ─────────────────────────────────────────────


class TestMakeRelative:
    """make_relative() helper for path normalization.

    Substrate-NEUTRAL: pure path arithmetic, no catalog object, no I/O. But
    the function is currently DEFINED in the dying
    ``nexus/catalog/catalog.py``, so the import moved into each body to keep
    this file collecting — and see the module docstring's FINDING: six LIVE
    call sites import it from there, so it needs relocating rather than
    deleting. This class is its only coverage.
    """

    def test_relativizes_path_under_root(self, tmp_path: Path) -> None:
        from nexus.catalog.types import make_relative

        root = tmp_path / "repo"
        assert make_relative(root / "src" / "foo.py", root) == "src/foo.py"

    def test_returns_original_if_not_under_root(self, tmp_path: Path) -> None:
        from nexus.catalog.types import make_relative

        root = tmp_path / "repo"
        other = tmp_path / "other" / "bar.py"
        assert make_relative(other, root) == str(other)

    def test_returns_original_string_for_relative_input(self) -> None:
        from nexus.catalog.types import make_relative

        assert make_relative("src/foo.py", Path("/repo")) == "src/foo.py"

    def test_accepts_string_input(self, tmp_path: Path) -> None:
        from nexus.catalog.types import make_relative

        root = tmp_path / "repo"
        assert make_relative(str(root / "src" / "foo.py"), root) == "src/foo.py"


# ── _markdown_chunks no longer emits source_path (RDR-102 D2) ────────────────


class TestMarkdownChunksRelativePath:
    """RDR-102 D2 retired ``source_path`` from the chunk schema. The
    ``base_path`` parameter still drives the catalog hook's
    ``file_path`` storage (relative vs absolute) so the tumbler's
    catalog row can be looked up in the operator's expected form, but
    the chunk metadata itself no longer carries source_path at all.
    """

    def test_markdown_chunks_does_not_emit_source_path_by_default(
        self, tmp_path: Path,
    ) -> None:
        from nexus.doc_indexer import _markdown_chunks

        md = tmp_path / "doc.md"
        md.write_text("# Hello\n\nSome content here for chunking.")
        result = _markdown_chunks(md, "abc123", "voyage-context-3", "2026-01-01", "corp")
        assert result  # non-empty
        assert "source_path" not in result[0][2]

    def test_markdown_chunks_does_not_emit_source_path_with_base(
        self, tmp_path: Path,
    ) -> None:
        from nexus.doc_indexer import _markdown_chunks

        repo = tmp_path / "myrepo"
        repo.mkdir()
        md = repo / "docs" / "rdr" / "rdr-001.md"
        md.parent.mkdir(parents=True)
        md.write_text("# RDR-001\n\nSome research content for chunking.")
        result = _markdown_chunks(md, "abc123", "voyage-context-3", "2026-01-01", "corp", base_path=repo)
        assert result
        assert "source_path" not in result[0][2]


# ── _index_document source_key (nexus-1p4g.3) ───────────────────────────────


class TestIndexDocumentSourceKey:
    """_index_document uses source_key for staleness check and pruning."""

    def test_staleness_check_uses_content_hash_when_catalog_absent(self, tmp_path: Path, monkeypatch) -> None:
        """RDR-101 Phase 5c: source_path is gone from chunk metadata.
        When no catalog is initialised (no doc_id), the staleness check
        falls back to content_hash — which uniquely identifies an
        unchanged file just as well as the legacy source_path key."""
        from unittest.mock import MagicMock

        from tests.conftest import set_credentials
        from nexus.doc_indexer import _index_document

        set_credentials(monkeypatch)

        md = tmp_path / "doc.md"
        md.write_text("# Test\n\nContent for staleness check.")
        expected_hash = hashlib.sha256(md.read_bytes()).hexdigest()

        mock_col = MagicMock()
        mock_col.get.return_value = {
            "ids": ["existing"],
            "metadatas": [{"content_hash": expected_hash, "embedding_model": "voyage-context-3"}],
        }
        mock_t3 = MagicMock()
        mock_t3.get_or_create_collection.return_value = mock_col

        def dummy_chunk_fn(file_path, content_hash, target_model, now_iso, corpus):
            return [("id1", "text", {})]

        with patch("nexus.doc_indexer.make_t3", return_value=mock_t3):
            result = _index_document(md, "corp", dummy_chunk_fn, t3=mock_t3, source_key="relative/doc.md")

        staleness_call = mock_col.get.call_args
        assert staleness_call.kwargs["where"] == {"content_hash": expected_hash}
        assert result == 0
