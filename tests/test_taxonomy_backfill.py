# SPDX-License-Identifier: AGPL-3.0-or-later
"""``nx taxonomy backfill-source-collection`` — RDR-087 Phase 4.1.

Backfills ``topic_assignments.source_collection`` for legacy rows that
predate the RDR-077 projection path. Strategy: for ``assigned_by`` in
``{'hdbscan', 'centroid'}``, clustering is per-collection so
``topics.collection`` is the source collection; for ``'auto-matched'``
and ``'projection'`` rows, the source is genuinely ambiguous and we
leave the field alone.

**Irreversible DB write** — ``--apply`` required; default is dry-run.
Sandbox verification happens at the end of CI via a run against a
tmp copy of the real T2 DB.
"""
from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest
from click.testing import CliRunner


@pytest.fixture
def runner() -> CliRunner:
    return CliRunner()


def _seed_t2_with_legacy_assignments(path: Path) -> None:
    """Build a T2 DB with:

    - 1 ``projection`` row (NON-null source_collection; distinct from topic)
    - 1 ``hdbscan`` row (NULL source_collection; topic in 'code__X')
    - 1 ``centroid`` row (NULL source_collection; topic in 'docs__Y')
    - 1 ``auto-matched`` row (NULL source_collection; topic in 'knowledge__Z')
    """
    from nexus.db.t2 import T2Database

    db = T2Database(path)
    c = db.taxonomy.conn
    # Insert topics. ``created_at`` is NOT NULL with no default so the
    # seed must supply it; ``INSERT OR IGNORE`` would otherwise silently
    # drop the row on constraint violation.
    c.executemany(
        "INSERT OR IGNORE INTO topics "
        "(id, label, collection, created_at) VALUES (?, ?, ?, ?)",
        [
            (1, "alpha", "code__X", "2026-04-01T00:00:00Z"),
            (2, "beta", "docs__Y", "2026-04-01T00:00:00Z"),
            (3, "gamma", "knowledge__Z", "2026-04-01T00:00:00Z"),
        ],
    )
    c.executemany(
        "INSERT INTO topic_assignments "
        "(doc_id, topic_id, assigned_by, similarity, assigned_at, source_collection) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        [
            # Projection: already populated; MUST NOT be touched.
            ("d1", 1, "projection", 0.85, "2026-04-01T00:00:00Z", "docs__external"),
            # Legacy rows with NULL source_collection:
            ("d2", 1, "hdbscan", None, None, None),
            ("d3", 2, "centroid", None, None, None),
            ("d4", 3, "auto-matched", None, None, None),
        ],
    )
    c.commit()
    db.close()


class TestBackfillDryRun:
    def test_report_counts_without_writing(self, tmp_path: Path) -> None:
        from nexus.taxonomy_backfill import backfill_source_collection
        from nexus.db.t2 import T2Database

        db_path = tmp_path / "memory.db"
        _seed_t2_with_legacy_assignments(db_path)

        db = T2Database(db_path)
        try:
            report = backfill_source_collection(db.taxonomy.conn, apply=False)
        finally:
            db.close()

        assert report.dry_run is True
        assert report.eligible_rows == 2  # hdbscan + centroid
        assert report.updated_rows == 0
        assert report.coverage_before == pytest.approx(0.25)  # 1/4
        # Projected coverage after apply: 3/4 = 0.75
        assert report.coverage_projected == pytest.approx(0.75)
        # Per-category eligible counts:
        assert report.eligible_by_category == {"hdbscan": 1, "centroid": 1}

    def test_dry_run_leaves_data_unchanged(self, tmp_path: Path) -> None:
        from nexus.taxonomy_backfill import backfill_source_collection
        from nexus.db.t2 import T2Database

        db_path = tmp_path / "memory.db"
        _seed_t2_with_legacy_assignments(db_path)

        db = T2Database(db_path)
        try:
            backfill_source_collection(db.taxonomy.conn, apply=False)
            nulls = db.taxonomy.conn.execute(
                "SELECT COUNT(*) FROM topic_assignments "
                "WHERE source_collection IS NULL"
            ).fetchone()[0]
            assert nulls == 3  # nothing written
        finally:
            db.close()


class TestBackfillApply:
    def test_apply_fills_hdbscan_and_centroid(self, tmp_path: Path) -> None:
        from nexus.taxonomy_backfill import backfill_source_collection
        from nexus.db.t2 import T2Database

        db_path = tmp_path / "memory.db"
        _seed_t2_with_legacy_assignments(db_path)

        db = T2Database(db_path)
        try:
            report = backfill_source_collection(db.taxonomy.conn, apply=True)
            rows = db.taxonomy.conn.execute(
                "SELECT doc_id, assigned_by, source_collection "
                "FROM topic_assignments ORDER BY doc_id"
            ).fetchall()
        finally:
            db.close()

        by_id = {r[0]: r for r in rows}
        # Projection row untouched — still 'docs__external'.
        assert by_id["d1"][2] == "docs__external"
        # hdbscan (topic 1 → code__X) backfilled to 'code__X'.
        assert by_id["d2"][2] == "code__X"
        # centroid (topic 2 → docs__Y) backfilled to 'docs__Y'.
        assert by_id["d3"][2] == "docs__Y"
        # auto-matched LEFT ALONE — still NULL.
        assert by_id["d4"][2] is None
        # Report.
        assert report.dry_run is False
        assert report.updated_rows == 2
        assert report.coverage_after == pytest.approx(0.75)

    def test_idempotent_second_apply_is_noop(self, tmp_path: Path) -> None:
        from nexus.taxonomy_backfill import backfill_source_collection
        from nexus.db.t2 import T2Database

        db_path = tmp_path / "memory.db"
        _seed_t2_with_legacy_assignments(db_path)

        db = T2Database(db_path)
        try:
            backfill_source_collection(db.taxonomy.conn, apply=True)
            second = backfill_source_collection(db.taxonomy.conn, apply=True)
            assert second.eligible_rows == 0
            assert second.updated_rows == 0
        finally:
            db.close()

    def test_projection_rows_never_modified(self, tmp_path: Path) -> None:
        """Defence in depth — even if the SQL got sloppy, the WHERE
        clause must prevent ``projection``-row writes."""
        from nexus.taxonomy_backfill import backfill_source_collection
        from nexus.db.t2 import T2Database

        db_path = tmp_path / "memory.db"
        _seed_t2_with_legacy_assignments(db_path)

        db = T2Database(db_path)
        try:
            before = db.taxonomy.conn.execute(
                "SELECT source_collection FROM topic_assignments "
                "WHERE doc_id = 'd1'"
            ).fetchone()[0]
            backfill_source_collection(db.taxonomy.conn, apply=True)
            after = db.taxonomy.conn.execute(
                "SELECT source_collection FROM topic_assignments "
                "WHERE doc_id = 'd1'"
            ).fetchone()[0]
            assert before == after == "docs__external"
        finally:
            db.close()


class TestBackfillCli:
    def _stub_db_open(self, monkeypatch, db_path: Path) -> None:
        """Point the CLI at the seeded tmp T2 DB."""
        monkeypatch.setattr(
            "nexus.commands._helpers.default_db_path",
            lambda: db_path,
        )

    def test_default_is_dry_run(
        self, runner: CliRunner, tmp_path: Path, monkeypatch,
    ) -> None:
        from nexus.cli import main

        db_path = tmp_path / "memory.db"
        _seed_t2_with_legacy_assignments(db_path)
        self._stub_db_open(monkeypatch, db_path)

        result = runner.invoke(main, ["taxonomy", "backfill-source-collection"])
        assert result.exit_code == 0, result.output
        assert "dry-run" in result.output.lower()

        # No data was modified.
        conn = sqlite3.connect(str(db_path))
        nulls = conn.execute(
            "SELECT COUNT(*) FROM topic_assignments WHERE source_collection IS NULL"
        ).fetchone()[0]
        conn.close()
        assert nulls == 3

    def test_apply_flag_commits_writes(
        self, runner: CliRunner, tmp_path: Path, monkeypatch,
    ) -> None:
        from nexus.cli import main

        db_path = tmp_path / "memory.db"
        _seed_t2_with_legacy_assignments(db_path)
        self._stub_db_open(monkeypatch, db_path)

        result = runner.invoke(
            main, ["taxonomy", "backfill-source-collection", "--apply"],
        )
        assert result.exit_code == 0, result.output

        conn = sqlite3.connect(str(db_path))
        nulls = conn.execute(
            "SELECT COUNT(*) FROM topic_assignments WHERE source_collection IS NULL"
        ).fetchone()[0]
        conn.close()
        # Only auto-matched left NULL.
        assert nulls == 1
