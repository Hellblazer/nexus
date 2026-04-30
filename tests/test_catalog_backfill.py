# SPDX-License-Identifier: AGPL-3.0-or-later

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from click.testing import CliRunner

from nexus.catalog.catalog import Catalog
from nexus.cli import main


@pytest.fixture(autouse=True)
def git_identity(monkeypatch):
    monkeypatch.setenv("GIT_AUTHOR_NAME", "Test")
    monkeypatch.setenv("GIT_AUTHOR_EMAIL", "test@test.invalid")
    monkeypatch.setenv("GIT_COMMITTER_NAME", "Test")
    monkeypatch.setenv("GIT_COMMITTER_EMAIL", "test@test.invalid")


@pytest.fixture
def catalog_env(tmp_path, monkeypatch):
    catalog_dir = tmp_path / "catalog"
    monkeypatch.setenv("NEXUS_CATALOG_PATH", str(catalog_dir))
    Catalog.init(catalog_dir)
    return catalog_dir


def _mock_registry(tmp_path: Path, repos: dict | None = None) -> MagicMock:
    """Create a mock RepoRegistry with a repo that exists on disk."""
    mock = MagicMock()
    # Create the repo dir so the path-existence check passes
    repo_dir = tmp_path / "myrepo"
    repo_dir.mkdir(exist_ok=True)
    if repos is None:
        repos = {
            str(repo_dir): {
                "name": "myrepo",
                "collection": "code__myrepo",
                "code_collection": "code__myrepo",
                "docs_collection": "docs__myrepo",
                "head_hash": "abc123",
                "status": "ready",
            }
        }
    mock.all_info.return_value = repos
    return mock


def _mock_t3(collections: list[dict] | None = None) -> MagicMock:
    """Create a mock T3Database."""
    mock = MagicMock()
    if collections is None:
        collections = [
            {"name": "code__myrepo", "count": 100},
            {"name": "docs__myrepo", "count": 50},
            {"name": "knowledge__delos", "count": 20},
        ]
    mock.list_collections.return_value = collections

    # Mock get_or_create_collection to return a mock col with get()
    mock_col = MagicMock()
    mock_col.get.return_value = {"ids": ["chunk1"], "metadatas": [{"title": "test doc"}]}
    mock.get_or_create_collection.return_value = mock_col
    return mock


class TestBackfillRepos:
    def test_backfill_creates_owner_and_docs(self, catalog_env, tmp_path):
        from nexus.commands.catalog import _backfill_repos

        cat = Catalog(catalog_env, catalog_env / ".catalog.db")
        registry = _mock_registry(tmp_path)

        count, claimed = _backfill_repos(cat, registry, dry_run=False)
        assert count >= 0
        assert len(claimed) >= 1  # Should claim at least one collection
        # Owner should exist
        owner = cat.owner_for_repo(cat._db.execute(
            "SELECT repo_hash FROM owners WHERE owner_type='repo'"
        ).fetchone()[0])
        assert owner is not None

    def test_backfill_repos_dry_run(self, catalog_env, tmp_path):
        from nexus.commands.catalog import _backfill_repos

        cat = Catalog(catalog_env, catalog_env / ".catalog.db")
        registry = _mock_registry(tmp_path)

        _backfill_repos(cat, registry, dry_run=True)
        # No owners should be created
        rows = cat._db.execute("SELECT count(*) FROM owners").fetchone()
        assert rows[0] == 0

    def test_backfill_repos_idempotent(self, catalog_env, tmp_path):
        from nexus.commands.catalog import _backfill_repos

        cat = Catalog(catalog_env, catalog_env / ".catalog.db")
        registry = _mock_registry(tmp_path)

        _backfill_repos(cat, registry, dry_run=False)
        _backfill_repos(cat, registry, dry_run=False)
        rows = cat._db.execute("SELECT count(*) FROM owners WHERE owner_type='repo'").fetchone()
        assert rows[0] == 1


class TestBackfillKnowledge:
    def test_backfill_knowledge(self, catalog_env):
        from nexus.commands.catalog import _backfill_knowledge

        cat = Catalog(catalog_env, catalog_env / ".catalog.db")
        t3 = _mock_t3([{"name": "knowledge__delos", "count": 20}])

        count = _backfill_knowledge(cat, t3, dry_run=False)
        assert count == 1
        rows = cat._db.execute(
            "SELECT title FROM documents WHERE content_type='knowledge'"
        ).fetchone()
        assert rows is not None

    def test_backfill_knowledge_dry_run(self, catalog_env):
        from nexus.commands.catalog import _backfill_knowledge

        cat = Catalog(catalog_env, catalog_env / ".catalog.db")
        t3 = _mock_t3([{"name": "knowledge__delos", "count": 20}])

        _backfill_knowledge(cat, t3, dry_run=True)
        rows = cat._db.execute("SELECT count(*) FROM documents").fetchone()
        assert rows[0] == 0


class TestBackfillCommand:
    @patch("nexus.commands.catalog._make_t3")
    @patch("nexus.commands.catalog._make_registry")
    def test_backfill_dry_run_cli(self, mock_reg_fn, mock_t3_fn, catalog_env, tmp_path):
        mock_reg_fn.return_value = _mock_registry(tmp_path)
        mock_t3_fn.return_value = _mock_t3()

        runner = CliRunner()
        result = runner.invoke(main, ["catalog", "backfill", "--dry-run"])
        assert result.exit_code == 0
        assert "dry-run" in result.output.lower()

    @patch("nexus.commands.catalog._make_t3")
    @patch("nexus.commands.catalog._make_registry")
    def test_backfill_cli(self, mock_reg_fn, mock_t3_fn, catalog_env, tmp_path):
        mock_reg_fn.return_value = _mock_registry(tmp_path)
        mock_t3_fn.return_value = _mock_t3()

        runner = CliRunner()
        result = runner.invoke(main, ["catalog", "backfill"])
        assert result.exit_code == 0
        assert "complete" in result.output.lower()

    def test_backfill_not_initialized(self, tmp_path, monkeypatch):
        monkeypatch.setenv("NEXUS_CATALOG_PATH", str(tmp_path / "no-catalog"))
        runner = CliRunner()
        result = runner.invoke(main, ["catalog", "backfill"])
        assert result.exit_code != 0


class TestBackfillFromT3:
    """nexus-p03z Issue 2: per-file recovery from T3 chunk metadata for
    repo-owned ``docs__<repo>`` and ``code__<repo>`` collections.

    ``_backfill_repos`` registers a single summary row per (repo,
    collection); ``_backfill_papers`` excludes repo-owned collections.
    Without per-file recovery, deleted catalog rows for a repo's docs/
    code files cannot be reconstructed from existing T3 state, even
    though the chunks carry the correct ``source_path`` and the repo
    owner already exists.

    Live recovery executed during nexus-3e4s remediation
    (2026-04-29):
    - docs__ART-8c2e74c0: 1 row -> 365 rows (recovered 364 from
      15,790 chunks)
    - code__ART-8c2e74c0: 4,182 rows -> 4,397 rows from 63,077
      chunks
    """

    def _mock_t3_with_chunks(
        self,
        collection_name: str,
        chunks_per_path: dict[str, int],
        repo_root: Path,
    ) -> MagicMock:
        """Build a mock T3 whose ``get_collection().get(...)`` paginates
        chunks tagged with absolute source_paths under ``repo_root``."""
        mock_t3 = MagicMock()
        mock_col = MagicMock()
        mock_col.name = collection_name

        # Build a flat list of chunk metadata, one row per chunk per path.
        all_metadatas: list[dict] = []
        all_ids: list[str] = []
        chunk_idx = 0
        for rel_path, n in chunks_per_path.items():
            abs_path = str(repo_root / rel_path)
            for _ in range(n):
                all_ids.append(f"chunk-{chunk_idx}")
                all_metadatas.append({"source_path": abs_path})
                chunk_idx += 1

        # Paginate at 300 per Cloud T3 cap.
        def _paginated_get(*, include, limit, offset, **kw):
            page_ids = all_ids[offset:offset + limit]
            page_meta = all_metadatas[offset:offset + limit]
            return {
                "ids": page_ids,
                "metadatas": page_meta,
                "documents": [None] * len(page_ids),
            }

        mock_col.get.side_effect = _paginated_get
        mock_t3.get_collection.return_value = mock_col
        mock_t3._client.get_collection.return_value = mock_col
        return mock_t3

    def test_per_file_recovery_registers_unique_source_paths(
        self, catalog_env, tmp_path,
    ):
        """Each unique source_path in T3 chunks becomes a catalog row
        under the repo owner, anchored to the repo_root."""
        from nexus.commands.catalog import _backfill_per_file_from_t3

        repo_root = tmp_path / "myrepo"
        repo_root.mkdir()
        cat = Catalog(catalog_env, catalog_env / ".catalog.db")
        owner = cat.register_owner(
            "myrepo", "repo", repo_hash="abc12345",
            repo_root=str(repo_root),
        )
        # Pre-existing summary row from _backfill_repos.
        cat.register(
            owner=owner, title="myrepo (code)", content_type="code",
            physical_collection="code__myrepo-abc12345",
        )

        t3 = self._mock_t3_with_chunks(
            "code__myrepo-abc12345",
            {"src/a.py": 5, "src/b.py": 3, "src/c.py": 2},
            repo_root,
        )

        registered = _backfill_per_file_from_t3(
            cat, t3, "code__myrepo-abc12345", dry_run=False,
        )
        assert registered == 3
        # Verify each path got its own catalog row.
        rows = cat._db.execute(
            "SELECT file_path FROM documents "
            "WHERE physical_collection = ? AND file_path != ''",
            ("code__myrepo-abc12345",),
        ).fetchall()
        paths = {r[0] for r in rows}
        assert "src/a.py" in paths
        assert "src/b.py" in paths
        assert "src/c.py" in paths

    def test_per_file_recovery_idempotent(self, catalog_env, tmp_path):
        """Re-running per-file recovery does not duplicate rows
        (cat.register is idempotent on file_path within owner)."""
        from nexus.commands.catalog import _backfill_per_file_from_t3

        repo_root = tmp_path / "myrepo2"
        repo_root.mkdir()
        cat = Catalog(catalog_env, catalog_env / ".catalog.db")
        cat.register_owner(
            "myrepo2", "repo", repo_hash="def67890",
            repo_root=str(repo_root),
        )

        t3 = self._mock_t3_with_chunks(
            "code__myrepo2-def67890",
            {"src/x.py": 2, "src/y.py": 1},
            repo_root,
        )

        first = _backfill_per_file_from_t3(
            cat, t3, "code__myrepo2-def67890", dry_run=False,
        )
        second = _backfill_per_file_from_t3(
            cat, t3, "code__myrepo2-def67890", dry_run=False,
        )
        assert first == 2
        assert second == 0  # already registered

    def test_per_file_recovery_dry_run_writes_nothing(
        self, catalog_env, tmp_path,
    ):
        from nexus.commands.catalog import _backfill_per_file_from_t3

        repo_root = tmp_path / "myrepo3"
        repo_root.mkdir()
        cat = Catalog(catalog_env, catalog_env / ".catalog.db")
        cat.register_owner(
            "myrepo3", "repo", repo_hash="11111111",
            repo_root=str(repo_root),
        )

        t3 = self._mock_t3_with_chunks(
            "code__myrepo3-11111111",
            {"src/m.py": 1, "src/n.py": 1},
            repo_root,
        )

        would_register = _backfill_per_file_from_t3(
            cat, t3, "code__myrepo3-11111111", dry_run=True,
        )
        assert would_register == 2
        # Nothing written.
        rows = cat._db.execute(
            "SELECT count(*) FROM documents "
            "WHERE physical_collection = ? AND file_path != ''",
            ("code__myrepo3-11111111",),
        ).fetchone()
        assert rows[0] == 0

    def test_per_file_recovery_rejects_non_repo_collection(
        self, catalog_env,
    ):
        """Collections without a repo-hash suffix can't be recovered
        this way (no owner to attribute under). Helper raises."""
        from nexus.commands.catalog import _backfill_per_file_from_t3

        cat = Catalog(catalog_env, catalog_env / ".catalog.db")
        # No owner registered for this hash.
        t3 = MagicMock()

        with pytest.raises(Exception) as exc_info:
            _backfill_per_file_from_t3(
                cat, t3, "code__nosuchrepo-deadbeef", dry_run=True,
            )
        assert "owner" in str(exc_info.value).lower() or "no repo" in str(exc_info.value).lower()


class TestBackfillFromT3CLI:
    """CLI surface: ``nx catalog backfill --from-t3 [<COL>|--all-repo-collections]``."""

    @patch("nexus.commands.catalog._make_t3")
    @patch("nexus.commands.catalog._make_registry")
    def test_from_t3_requires_target(
        self, mock_reg_fn, mock_t3_fn, catalog_env, tmp_path,
    ):
        """``--from-t3`` without either ``--collection`` or
        ``--all-repo-collections`` is a usage error."""
        mock_reg_fn.return_value = _mock_registry(tmp_path)
        mock_t3_fn.return_value = _mock_t3()

        runner = CliRunner()
        result = runner.invoke(main, ["catalog", "backfill", "--from-t3"])
        assert result.exit_code != 0
        out = result.output.lower()
        assert "--collection" in out or "--all-repo-collections" in out

    @patch("nexus.commands.catalog._make_t3")
    @patch("nexus.commands.catalog._make_registry")
    def test_from_t3_with_collection_runs_recovery(
        self, mock_reg_fn, mock_t3_fn, catalog_env, tmp_path,
    ):
        """``--from-t3 --collection X`` runs per-file recovery for X
        only, skipping the original 4-pass backfill."""
        repo_root = tmp_path / "cli_repo"
        repo_root.mkdir()
        # Pre-register the owner so the recovery path can attribute.
        cat = Catalog(catalog_env, catalog_env / ".catalog.db")
        cat.register_owner(
            "cli_repo", "repo", repo_hash="cafebabe",
            repo_root=str(repo_root),
        )

        # Mock T3: one collection with 3 unique source_paths.
        all_metas = [
            {"source_path": str(repo_root / "a.py")},
            {"source_path": str(repo_root / "b.py")},
            {"source_path": str(repo_root / "c.py")},
        ]

        def _paginated_get(*, include, limit, offset, **kw):
            page = list(zip(
                [f"id-{i}" for i in range(offset, min(offset + limit, len(all_metas)))],
                all_metas[offset:offset + limit],
            ))
            return {
                "ids": [p[0] for p in page],
                "metadatas": [p[1] for p in page],
                "documents": [None] * len(page),
            }

        mock_col = MagicMock()
        mock_col.get.side_effect = _paginated_get
        mock_t3 = MagicMock()
        mock_t3.get_collection.return_value = mock_col
        mock_t3._client.get_collection.return_value = mock_col
        mock_t3_fn.return_value = mock_t3
        mock_reg_fn.return_value = _mock_registry(tmp_path)

        runner = CliRunner()
        result = runner.invoke(main, [
            "catalog", "backfill",
            "--from-t3",
            "--collection", "code__cli_repo-cafebabe",
        ])
        assert result.exit_code == 0, result.output
        # Three rows registered.
        rows = cat._db.execute(
            "SELECT count(*) FROM documents "
            "WHERE physical_collection = ? AND file_path != ''",
            ("code__cli_repo-cafebabe",),
        ).fetchone()
        assert rows[0] == 3


class TestBackfillRdrsRepoOwner:
    """nexus-3e4s critique-followup S1.

    Pre-fix ``_backfill_rdrs`` unconditionally created a curator owner
    for every ``rdr__*`` collection, which made the register-time
    cross-project guard skip (curator owners legitimately span
    sources). The disaster-recovery path could therefore re-introduce
    contamination it was supposed to clean up.

    Post-fix: when the collection's hash suffix matches a registered
    repo, ``_backfill_rdrs`` looks up the existing repo owner and
    registers under it. Curator is the legitimate fallback for orphan
    ``rdr__*`` collections only.
    """

    def _mock_t3_with_rdr(self, repo_dir: Path, repo_hash: str):
        from unittest.mock import MagicMock

        mock = MagicMock()
        col_name = f"rdr__myrepo-{repo_hash}"
        mock.list_collections.return_value = [{"name": col_name, "count": 1}]
        mock_col = MagicMock()
        # Two pages: first returns the doc, second returns empty (loop exit).
        mock_col.get.side_effect = [
            {
                "metadatas": [{
                    "source_path": str(repo_dir / "docs" / "rdr" / "RDR-001.md"),
                    "title": "RDR-001",
                }],
            },
            {"metadatas": []},
        ]
        mock.get_or_create_collection.return_value = mock_col
        return mock, col_name

    def test_backfill_rdrs_uses_repo_owner_when_collection_matches_repo(
        self, catalog_env, tmp_path,
    ):
        import hashlib

        from nexus.commands.catalog import _backfill_rdrs, _backfill_repos

        cat = Catalog(catalog_env, catalog_env / ".catalog.db")
        repo_dir = tmp_path / "myrepo"
        repo_dir.mkdir()
        repo_hash = hashlib.sha256(str(repo_dir).encode()).hexdigest()[:8]

        # Step 1: register the repo via _backfill_repos so the owner exists.
        registry = _mock_registry(tmp_path, {
            str(repo_dir): {
                "name": "myrepo",
                "code_collection": f"code__myrepo-{repo_hash}",
                "docs_collection": f"docs__myrepo-{repo_hash}",
                "head_hash": "abc",
                "status": "ready",
            },
        })
        _backfill_repos(cat, registry, dry_run=False)

        # Step 2: backfill RDRs. The collection name carries the same
        # hash, so the function should pick up the existing repo owner.
        t3, col_name = self._mock_t3_with_rdr(repo_dir, repo_hash)
        with patch(
            "nexus.catalog.catalog._default_registry_path",
            return_value=tmp_path / "repos.json",
        ):
            (tmp_path / "repos.json").write_text(json.dumps({
                "repos": {
                    str(repo_dir): {"name": "myrepo", "head_hash": "abc"},
                },
            }))
            count = _backfill_rdrs(cat, t3, dry_run=False)

        assert count == 1
        # Verify the doc landed under the repo owner (NOT a curator).
        rows = cat._db.execute(
            "SELECT d.tumbler, o.owner_type FROM documents d "
            "JOIN owners o ON d.tumbler LIKE o.tumbler_prefix || '.%' "
            "WHERE d.physical_collection = ? AND d.content_type = 'rdr'",
            (col_name,),
        ).fetchall()
        assert rows, f"no RDR row in {col_name}"
        # The matching owner row must be a repo owner.
        repo_owner_match = [r for r in rows if r[1] == "repo"]
        assert repo_owner_match, (
            f"backfill registered RDR under non-repo owner: {rows!r} — "
            "this lets the cross-project guard skip on the disaster-"
            "recovery path"
        )

    def test_backfill_rdrs_falls_back_to_curator_when_no_match(
        self, catalog_env, tmp_path,
    ):
        from nexus.commands.catalog import _backfill_rdrs

        cat = Catalog(catalog_env, catalog_env / ".catalog.db")
        # No matching repo registered — curator fallback is correct.
        t3, col_name = self._mock_t3_with_rdr(tmp_path / "fake", "deadbeef")
        with patch(
            "nexus.catalog.catalog._default_registry_path",
            return_value=tmp_path / "repos.json",
        ):
            (tmp_path / "repos.json").write_text(json.dumps({"repos": {}}))
            count = _backfill_rdrs(cat, t3, dry_run=False)
        assert count == 1
        rows = cat._db.execute(
            "SELECT o.owner_type FROM documents d "
            "JOIN owners o ON d.tumbler LIKE o.tumbler_prefix || '.%' "
            "WHERE d.physical_collection = ?",
            (col_name,),
        ).fetchall()
        assert any(r[0] == "curator" for r in rows)
