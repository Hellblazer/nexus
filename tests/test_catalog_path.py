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
  - ``TestResolvePath`` — fully ported. The three strict-xfail pins that
    were PORT-BLOCKED on nexus-5i864 went live when the service client
    gained the local resolution order (see the class docstring).
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


# nexus-i711w terminal deletion: TestCatalogDBMigration (4 tests) retired
# WITH nexus.catalog.catalog_db — the subject was the SQLite schema and its
# ALTER-on-open migration; the engine schema is Liquibase-managed and
# covered by the Java suite. (Its ``local_catalog_backend`` pin decorator
# was removed in the Stage 5 sweep — after the class deletion it was
# accidentally decorating TestResolvePath below.)


# ── resolve_path (nexus-1p4g.2) ──────────────────────────────────────────────


class TestResolvePath:
    """Catalog.resolve_path() resolution tests.

    nexus-5i864 LANDED: ``HttpCatalogClient.resolve_path`` now carries the
    full local resolution order (resolve -> owner lookup -> curator guard ->
    absolute passthrough -> ``owner.repo_root`` recombination -> DEBUG +
    None for legacy empty-repo_root owners), so the three strict-xfail pins
    this class carried through the nexus-i711w port are live tests again,
    running against the service client. History: the local Catalog harness
    died in the i711w terminal deletion; the registry-legacy variant's
    structlog-event half was local-only observability and retired with the
    module (its ``None``-return half is folded into the empty-repo_root
    test); the ``RepoRegistry`` source-text lint guard retired with its
    subject file ``catalog_docs.py``.
    """

    def test_resolve_path_with_repo_root(
        self, active_catalog: Any, tmp_path: Path,
    ) -> None:
        """A stored RELATIVE file_path recombines with owner.repo_root."""
        repo_dir = tmp_path / "myrepo"
        repo_dir.mkdir()
        owner = active_catalog.register_owner(
            "resolve-path-root", "repo", repo_hash="abc12345",
            repo_root=str(repo_dir),
        )
        tumbler = active_catalog.register(
            owner, "test.py", content_type="code", file_path="src/test.py",
        )
        assert active_catalog.resolve_path(tumbler) == repo_dir / "src" / "test.py"

    def test_resolve_path_curator_returns_none(self, active_catalog: Any) -> None:
        """Curator owners have no repo root: resolve_path must be None."""
        owner = active_catalog.register_owner("resolve-path-papers", "curator")
        tumbler = active_catalog.register(
            owner, "paper.pdf", content_type="paper", file_path="paper.pdf",
        )
        assert active_catalog.resolve_path(tumbler) is None

    def test_resolve_path_empty_repo_root_returns_none(
        self, active_catalog: Any,
    ) -> None:
        """RDR-137 Phase 3.6 (OQ-11): legacy owners with empty repo_root
        resolve to None (no repos.json fallback)."""
        owner = active_catalog.register_owner(
            "resolve-path-legacy", "repo", repo_hash="abc12345",
        )
        tumbler = active_catalog.register(
            owner, "test.py", content_type="code", file_path="src/test.py",
        )
        assert active_catalog.resolve_path(tumbler) is None

    def test_resolve_path_unknown_tumbler(self, active_catalog: Any) -> None:
        """PORTED: an unregistered tumbler resolves to ``None`` on both arms
        (local returned None from the owner lookup, the service client's
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
