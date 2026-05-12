# SPDX-License-Identifier: AGPL-3.0-or-later
"""nexus-urj4: GC verb for document_aspects orphans.

Aspects whose source document was deleted from the catalog accumulate
because catalog and T2 live in separate SQLite files (cross-DB FK
CASCADE is not supported by SQLite). The Phase 5 verification probe
on 2026-05-10 found 50.72% orphan rate in prod.

These tests pin the contract for ``DocumentAspects.delete_orphans``
and the ``nx aspects gc`` CLI verb.
"""
from __future__ import annotations

import sqlite3
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import patch

import pytest
from click.testing import CliRunner

from nexus.aspect_extractor import AspectRecord
from nexus.db.t2 import T2Database


def _seed_catalog(cat_db: Path, source_uris: list[str]) -> None:
    """Build a minimal catalog SQLite cache with one row per source_uri."""
    from nexus.catalog.catalog_db import _SCHEMA_SQL
    conn = sqlite3.connect(str(cat_db))
    try:
        conn.executescript(_SCHEMA_SQL)
        for i, uri in enumerate(source_uris):
            conn.execute(
                "INSERT INTO documents "
                "(tumbler, title, content_type, file_path, "
                " physical_collection, source_uri) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (f"1.1.{i + 1}", f"doc-{i}", "paper",
                 f"/tmp/{i}.pdf", "knowledge__test", uri),
            )
        conn.commit()
    finally:
        conn.close()


def _make_aspect(*, source_uri: str, source_path: str = "") -> AspectRecord:
    return AspectRecord(
        collection="knowledge__test",
        source_path=source_path or source_uri.removeprefix("file://"),
        problem_formulation="P",
        proposed_method="M",
        experimental_datasets=["d1"],
        experimental_baselines=["b1"],
        experimental_results="R",
        extras={},
        confidence=0.9,
        extracted_at=datetime.now(UTC).isoformat(),
        model_version="claude-haiku-4-5-20251001",
        extractor_name="scholarly-paper-v1",
        source_uri=source_uri,
    )


# ── DocumentAspects.delete_orphans ────────────────────────────────────────


class TestDocumentAspectsDeleteOrphans:
    """Pin the contract for the new ``delete_orphans`` method."""

    def test_no_catalog_returns_zero(self, tmp_path: Path) -> None:
        """Catalog-absent is a safe no-op (cannot detect orphans without
        the live document set)."""
        with T2Database(tmp_path / "t2.db") as db:
            db.document_aspects.upsert(_make_aspect(source_uri="file:///a"))
            assert db.document_aspects.delete_orphans(None) == (0, 0)
            missing = tmp_path / "nonexistent.db"
            assert db.document_aspects.delete_orphans(missing) == (0, 0)

    def test_dry_run_counts_but_does_not_delete(self, tmp_path: Path) -> None:
        """Default ``dry_run=True`` reports orphans without writing."""
        cat_db = tmp_path / ".catalog.db"
        _seed_catalog(cat_db, ["file:///live.pdf"])
        with T2Database(tmp_path / "t2.db") as db:
            db.document_aspects.upsert(_make_aspect(source_uri="file:///live.pdf"))
            db.document_aspects.upsert(_make_aspect(source_uri="file:///orphan.pdf"))

            orphans, total = db.document_aspects.delete_orphans(cat_db)
            assert (orphans, total) == (1, 2)

            # Nothing was actually deleted.
            remaining = db.document_aspects.conn.execute(
                "SELECT COUNT(*) FROM document_aspects"
            ).fetchone()[0]
            assert remaining == 2

    def test_apply_deletes_only_orphans(self, tmp_path: Path) -> None:
        """``dry_run=False`` deletes orphans and preserves live rows."""
        cat_db = tmp_path / ".catalog.db"
        _seed_catalog(cat_db, ["file:///live1.pdf", "file:///live2.pdf"])
        with T2Database(tmp_path / "t2.db") as db:
            db.document_aspects.upsert(_make_aspect(source_uri="file:///live1.pdf"))
            db.document_aspects.upsert(_make_aspect(source_uri="file:///live2.pdf"))
            db.document_aspects.upsert(_make_aspect(source_uri="file:///orphan1.pdf"))
            db.document_aspects.upsert(_make_aspect(source_uri="file:///orphan2.pdf"))

            orphans, total = db.document_aspects.delete_orphans(
                cat_db, dry_run=False,
            )
            assert (orphans, total) == (2, 4)

            surviving = sorted(
                r[0] for r in db.document_aspects.conn.execute(
                    "SELECT source_uri FROM document_aspects ORDER BY source_uri"
                ).fetchall()
            )
            assert surviving == ["file:///live1.pdf", "file:///live2.pdf"]

    def test_idempotent_after_apply(self, tmp_path: Path) -> None:
        """A second run on a clean state reports zero orphans."""
        cat_db = tmp_path / ".catalog.db"
        _seed_catalog(cat_db, ["file:///live.pdf"])
        with T2Database(tmp_path / "t2.db") as db:
            db.document_aspects.upsert(_make_aspect(source_uri="file:///live.pdf"))
            db.document_aspects.upsert(_make_aspect(source_uri="file:///orphan.pdf"))

            db.document_aspects.delete_orphans(cat_db, dry_run=False)
            orphans, total = db.document_aspects.delete_orphans(
                cat_db, dry_run=False,
            )
            assert (orphans, total) == (0, 1)

    def test_empty_source_uri_not_classified_as_orphan(self, tmp_path: Path) -> None:
        """Aspects with empty source_uri are legacy / pre-RDR-096-P2.1
        rows; they cannot be GC'd by URI mismatch and must be addressed
        by other paths (rename_collection, direct delete)."""
        cat_db = tmp_path / ".catalog.db"
        _seed_catalog(cat_db, ["file:///live.pdf"])
        with T2Database(tmp_path / "t2.db") as db:
            # Direct SQL: AspectRecord requires source_uri. The legacy
            # state has it as empty/NULL (pre-P2.1).
            db.document_aspects.upsert(_make_aspect(source_uri="file:///live.pdf"))
            db.document_aspects.conn.execute(
                "INSERT INTO document_aspects "
                "(collection, source_path, extracted_at, model_version, "
                " extractor_name, source_uri) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                ("knowledge__test", "/legacy.pdf", "2024-01-01T00:00:00",
                 "v0", "scholarly-paper-v1", ""),
            )
            db.document_aspects.conn.commit()

            orphans, total = db.document_aspects.delete_orphans(
                cat_db, dry_run=False,
            )
            # Total counts only non-empty source_uri rows.
            assert (orphans, total) == (0, 1)

            # The empty-source_uri legacy row survives.
            remaining = db.document_aspects.conn.execute(
                "SELECT COUNT(*) FROM document_aspects"
            ).fetchone()[0]
            assert remaining == 2

    def test_collection_renamed_does_not_orphan_when_source_uri_stable(
        self, tmp_path: Path,
    ) -> None:
        """Source URI is the join key; collection rename does not affect
        orphan classification as long as the URI binds catalog ↔ aspect."""
        cat_db = tmp_path / ".catalog.db"
        _seed_catalog(cat_db, ["file:///stable.pdf"])
        with T2Database(tmp_path / "t2.db") as db:
            r = _make_aspect(source_uri="file:///stable.pdf")
            r = AspectRecord(
                **{**r.__dict__, "collection": "knowledge__newname"},
            )
            db.document_aspects.upsert(r)

            orphans, total = db.document_aspects.delete_orphans(cat_db)
            assert (orphans, total) == (0, 1)


# ── nx aspects gc CLI ──────────────────────────────────────────────────────


class TestAspectsGcCLI:
    """Pin the operator-facing CLI behavior."""

    @pytest.fixture()
    def runner(self) -> CliRunner:
        return CliRunner()

    def _setup_env(
        self, tmp_path: Path, monkeypatch,
        *, live: list[str], aspects: list[str],
    ) -> tuple[Path, Path]:
        """Build catalog + T2 with seeded rows; patch helpers."""
        catalog_dir = tmp_path / "catalog"
        catalog_dir.mkdir()
        cat_db = catalog_dir / ".catalog.db"
        _seed_catalog(cat_db, live)

        mem_db = tmp_path / "memory.db"
        with T2Database(mem_db) as db:
            for uri in aspects:
                db.document_aspects.upsert(_make_aspect(source_uri=uri))

        # The CLI does local-imports inside the command function, so
        # source-module patches stick (vs module-attribute patches on
        # nexus.commands.aspects, which don't because the import binds
        # at call time).
        import nexus.commands._helpers as h
        monkeypatch.setattr("nexus.config.default_db_path", lambda: mem_db)
        monkeypatch.setenv("NEXUS_CATALOG_PATH", str(catalog_dir))
        return cat_db, mem_db

    def test_dry_run_default_reports_without_deleting(
        self, tmp_path: Path, monkeypatch, runner,
    ) -> None:
        cat_db, mem_db = self._setup_env(
            tmp_path, monkeypatch,
            live=["file:///a.pdf"],
            aspects=["file:///a.pdf", "file:///orphan.pdf"],
        )

        from nexus.cli import main
        result = runner.invoke(main, ["aspects", "gc"])

        assert result.exit_code == 0, result.output
        assert "would delete 1 orphan" in result.output
        assert "examined 2 row" in result.output
        assert "Re-run with --apply" in result.output

        # Verify no actual delete.
        with T2Database(mem_db) as db:
            n = db.document_aspects.conn.execute(
                "SELECT COUNT(*) FROM document_aspects"
            ).fetchone()[0]
            assert n == 2

    def test_apply_actually_deletes(
        self, tmp_path: Path, monkeypatch, runner,
    ) -> None:
        cat_db, mem_db = self._setup_env(
            tmp_path, monkeypatch,
            live=["file:///a.pdf"],
            aspects=["file:///a.pdf", "file:///orphan.pdf"],
        )

        from nexus.cli import main
        result = runner.invoke(main, ["aspects", "gc", "--apply"])

        assert result.exit_code == 0, result.output
        assert "deleted 1 orphan" in result.output
        assert "Re-run with --apply" not in result.output

        with T2Database(mem_db) as db:
            surviving = sorted(
                r[0] for r in db.document_aspects.conn.execute(
                    "SELECT source_uri FROM document_aspects"
                ).fetchall()
            )
            assert surviving == ["file:///a.pdf"]

    def test_no_catalog_exits_nonzero(
        self, tmp_path: Path, monkeypatch, runner,
    ) -> None:
        catalog_dir = tmp_path / "catalog_absent"
        # Don't create the catalog dir or DB.
        mem_db = tmp_path / "memory.db"
        T2Database(mem_db).close()

        import nexus.commands._helpers as h
        monkeypatch.setattr("nexus.config.default_db_path", lambda: mem_db)
        monkeypatch.setenv("NEXUS_CATALOG_PATH", str(catalog_dir))

        from nexus.cli import main
        result = runner.invoke(main, ["aspects", "gc"])

        assert result.exit_code != 0
        assert "No catalog" in result.output
