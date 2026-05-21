# SPDX-License-Identifier: AGPL-3.0-or-later
"""RDR-120 §A8 / nexus-yulol: ``nx aspects gc-fixtures`` verb.

The verb hard-deletes test-fixture rows from ``document_aspects`` and
``aspect_extraction_queue`` against a small allowlist of collection
prefixes that the test suite uses and that should never persist into
production. Previously this work ran unconditionally inside the
RDR-108 Phase 1c PK-swap migrations; carved out per RDR-120's
substrate-vs-consumer boundary.
"""
from __future__ import annotations

from pathlib import Path

import pytest
from click.testing import CliRunner

from nexus.commands.aspects import (
    _FIXTURE_COLLECTION_PATTERNS,
    _is_fixture_collection,
    aspects_group,
)


# ── Allowlist sanity ──────────────────────────────────────────────────────────


class TestFixtureMatching:
    @pytest.mark.parametrize("name,expected", [
        ("knowledge__cli-test", True),
        ("knowledge__cli-abc", True),
        ("knowledge__cli", False),  # bare 'cli' without trailing '-' not matched
        ("knowledge__nexus-integration-test", True),
        ("knowledge__nexus-integration-test-extra", False),  # exact-name match only
        ("knowledge__reproducer", True),
        ("knowledge__pagtest", True),
        ("knowledge__pagend", True),
        ("knowledge__production-data", False),
        ("knowledge__delos", False),
    ])
    def test_is_fixture_collection(self, name: str, expected: bool) -> None:
        assert _is_fixture_collection(name) is expected

    def test_pattern_list_unchanged_shape(self) -> None:
        """Five fixture entries (4 exact + 1 LIKE-prefix). This is the
        same set the PK-swap migrations used pre-RDR-120; bumping it
        is intentional (operator preference) and would require a
        deliberate change here."""
        assert len(_FIXTURE_COLLECTION_PATTERNS) == 5
        assert sum(1 for p in _FIXTURE_COLLECTION_PATTERNS if p.endswith("-")) == 1


# ── Verb behaviour against real T2 ────────────────────────────────────────────


def _seed_aspects(db, *, collection: str, count: int = 1) -> None:
    """Insert N rows into both document_aspects and aspect_extraction_queue.

    Inserts target columns that exist in both pre-RDR-108 and post-
    migration shapes; the PK-swap migration is skipped automatically
    in fixture setup because there is no catalog DB, so the tables
    retain the (collection, source_path) PK and lack a populated
    doc_id column. The verb operates only on the ``collection``
    column, so the rows do not need a doc_id to exercise the path.
    """
    da_conn = db.document_aspects.conn
    aq_conn = db.aspect_queue.conn
    for i in range(count):
        da_conn.execute(
            "INSERT INTO document_aspects "
            "(collection, source_path, extracted_at, model_version, "
            "extractor_name) VALUES (?, ?, ?, ?, ?)",
            (collection, f"/doc-{i}.pdf", "2026-05-21T00:00:00",
             "v1", "test-extractor"),
        )
    da_conn.commit()
    for i in range(count):
        aq_conn.execute(
            "INSERT INTO aspect_extraction_queue "
            "(collection, source_path, status, enqueued_at) "
            "VALUES (?, ?, ?, ?)",
            (collection, f"/doc-{i}.pdf", "failed",
             "2026-05-21T00:00:00"),
        )
    aq_conn.commit()


@pytest.fixture
def t2_with_fixtures(tmp_path: Path, monkeypatch):
    """Open a T2Database against a tmp memory.db; seed fixture +
    non-fixture rows in both target tables; yield (db, mem_path).
    Monkeypatch default_db_path so the CLI verb hits this database."""
    from nexus.db.t2 import T2Database

    mem_path = tmp_path / "memory.db"
    monkeypatch.setattr(
        "nexus.commands._helpers.default_db_path",
        lambda: mem_path,
    )

    db = T2Database(mem_path)
    try:
        # Two fixture-pattern rows, one production-data row that must NOT
        # be touched by the verb.
        _seed_aspects(db, collection="knowledge__cli-test", count=1)
        _seed_aspects(db, collection="knowledge__reproducer", count=1)
        _seed_aspects(db, collection="knowledge__production-corpus", count=1)
        yield db, mem_path
    finally:
        db.close()


class TestGcFixturesVerb:
    def test_dry_run_default_reports_without_deleting(self, t2_with_fixtures) -> None:
        db, _ = t2_with_fixtures
        runner = CliRunner()
        result = runner.invoke(aspects_group, ["gc-fixtures"])
        assert result.exit_code == 0, result.output
        assert "would delete" in result.output
        assert "Re-run with --yes" in result.output
        # No row was actually deleted.
        rows = db.document_aspects.conn.execute(
            "SELECT COUNT(*) FROM document_aspects"
        ).fetchone()[0]
        assert rows == 3
        qrows = db.aspect_queue.conn.execute(
            "SELECT COUNT(*) FROM aspect_extraction_queue"
        ).fetchone()[0]
        assert qrows == 3

    def test_yes_flag_deletes_only_fixture_rows(self, t2_with_fixtures) -> None:
        db, _ = t2_with_fixtures
        runner = CliRunner()
        result = runner.invoke(aspects_group, ["gc-fixtures", "--yes"])
        assert result.exit_code == 0, result.output
        assert "deleted" in result.output
        # Fixture rows deleted from both tables; production row preserved.
        rows = db.document_aspects.conn.execute(
            "SELECT collection FROM document_aspects"
        ).fetchall()
        assert [r[0] for r in rows] == ["knowledge__production-corpus"]
        qrows = db.aspect_queue.conn.execute(
            "SELECT collection FROM aspect_extraction_queue"
        ).fetchall()
        assert [r[0] for r in qrows] == ["knowledge__production-corpus"]

    def test_no_fixture_rows_is_clean_noop(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        """Empty fixture state → exit 0 with 'No fixture rows found.'"""
        from nexus.db.t2 import T2Database

        mem_path = tmp_path / "memory.db"
        monkeypatch.setattr(
            "nexus.commands._helpers.default_db_path",
            lambda: mem_path,
        )
        db = T2Database(mem_path)
        try:
            _seed_aspects(db, collection="knowledge__production-corpus", count=2)
        finally:
            db.close()

        runner = CliRunner()
        result = runner.invoke(aspects_group, ["gc-fixtures", "--yes"])
        assert result.exit_code == 0, result.output
        assert "No fixture rows found." in result.output

    def test_missing_db_is_clean_noop(self, tmp_path: Path, monkeypatch) -> None:
        """No memory.db at all → exit 0 with the 'nothing to do' message."""
        mem_path = tmp_path / "does_not_exist.db"
        monkeypatch.setattr(
            "nexus.commands._helpers.default_db_path",
            lambda: mem_path,
        )
        runner = CliRunner()
        result = runner.invoke(aspects_group, ["gc-fixtures", "--yes"])
        assert result.exit_code == 0, result.output
        assert "nothing to do" in result.output

    def test_verb_registered_under_aspects_group(self) -> None:
        assert "gc-fixtures" in aspects_group.commands
