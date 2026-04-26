# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""RDR-089 Phase E: extras → fixed-column promotion mechanic.

Substrate at ``src/nexus/aspect_promotion.py``; CLI wrapper at
``nx enrich aspects-promote-field``. These tests pin both surfaces.
"""
from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

import pytest
from click.testing import CliRunner

from nexus.aspect_extractor import AspectRecord
from nexus.aspect_promotion import (
    list_promotions, promote_extras_field,
)
from nexus.commands.enrich import enrich
from nexus.db.t2 import T2Database


def _make_record(
    *, source_path: str = "/p1.pdf",
    extras: dict | None = None,
) -> AspectRecord:
    return AspectRecord(
        collection="knowledge__delos",
        source_path=source_path,
        problem_formulation="P",
        proposed_method="M",
        experimental_datasets=[],
        experimental_baselines=[],
        experimental_results="R",
        extras=extras or {},
        confidence=0.9,
        extracted_at=datetime.now(UTC).isoformat(),
        model_version="claude-haiku-4-5-20251001",
        extractor_name="scholarly-paper-v1",
    )


@pytest.fixture()
def db_with_papers(tmp_path: Path):
    """T2 DB pre-populated with three papers carrying ``extras.venue``
    and ``extras.year``."""
    db_path = tmp_path / "promotion.db"
    with T2Database(db_path) as db:
        db.document_aspects.upsert(_make_record(
            source_path="/p1.pdf",
            extras={"venue": "VLDB", "year": 2023},
        ))
        db.document_aspects.upsert(_make_record(
            source_path="/p2.pdf",
            extras={"venue": "OSDI", "year": 2024},
        ))
        db.document_aspects.upsert(_make_record(
            source_path="/p3.pdf",
            extras={"venue": "SIGMOD"},  # year missing
        ))
    yield db_path


# ── Substrate tests ─────────────────────────────────────────────────────────


class TestPromoteSubstrate:
    def test_promote_adds_column_and_backfills(self, db_with_papers: Path) -> None:
        with T2Database(db_with_papers) as db:
            result = promote_extras_field(db, "venue", sql_type="TEXT")

        assert result.field_name == "venue"
        assert result.sql_type == "TEXT"
        assert result.column_added is True
        assert result.rows_backfilled == 3
        assert result.rows_pruned == 0
        assert result.pruned is False

        # Column exists, backfilled values match extras.
        with T2Database(db_with_papers) as db:
            cols = {
                r[1] for r in db.document_aspects.conn.execute(
                    "PRAGMA table_info(document_aspects)"
                ).fetchall()
            }
            assert "venue" in cols
            rows = dict(db.document_aspects.conn.execute(
                "SELECT source_path, venue FROM document_aspects "
                "ORDER BY source_path"
            ).fetchall())
        assert rows == {
            "/p1.pdf": "VLDB",
            "/p2.pdf": "OSDI",
            "/p3.pdf": "SIGMOD",
        }

    def test_promote_with_prune_removes_extras_key(
        self, db_with_papers: Path,
    ) -> None:
        with T2Database(db_with_papers) as db:
            result = promote_extras_field(
                db, "venue", sql_type="TEXT", prune=True,
            )
            assert result.pruned is True
            assert result.rows_pruned == 3

            # extras should no longer carry the venue key.
            for sp in ["/p1.pdf", "/p2.pdf", "/p3.pdf"]:
                rec = db.document_aspects.get("knowledge__delos", sp)
                assert "venue" not in rec.extras
            # Other keys preserved (year on p1, p2).
            rec = db.document_aspects.get("knowledge__delos", "/p1.pdf")
            assert rec.extras.get("year") == 2023

    def test_promote_idempotent_re_run(self, db_with_papers: Path) -> None:
        with T2Database(db_with_papers) as db:
            r1 = promote_extras_field(db, "venue")
            r2 = promote_extras_field(db, "venue")

        # Second call: column already added, no backfill needed
        # (typed column is non-NULL for every row from r1).
        assert r1.column_added is True
        assert r2.column_added is False
        assert r2.rows_backfilled == 0

    def test_promote_skips_rows_with_existing_typed_value(
        self, db_with_papers: Path,
    ) -> None:
        """If a row already has a value in the typed column (e.g. set
        directly by an extractor), promotion does NOT overwrite it
        from extras."""
        with T2Database(db_with_papers) as db:
            promote_extras_field(db, "venue")
            # Manually overwrite p1's venue.
            with db.document_aspects._lock:
                db.document_aspects.conn.execute(
                    "UPDATE document_aspects SET venue = 'OVERWRITTEN' "
                    "WHERE source_path = ?", ("/p1.pdf",),
                )
                db.document_aspects.conn.commit()
            # Re-run; the row's venue should NOT revert to extras.
            r = promote_extras_field(db, "venue")
            assert r.rows_backfilled == 0
            row = db.document_aspects.conn.execute(
                "SELECT venue FROM document_aspects WHERE source_path = ?",
                ("/p1.pdf",),
            ).fetchone()
        assert row[0] == "OVERWRITTEN"

    def test_promote_with_partial_extras_only_updates_present(
        self, db_with_papers: Path,
    ) -> None:
        """``year`` is present on p1 + p2 but missing from p3. Only
        the two rows that have it get backfilled."""
        with T2Database(db_with_papers) as db:
            r = promote_extras_field(db, "year", sql_type="INTEGER")
            assert r.rows_backfilled == 2
            rows = dict(db.document_aspects.conn.execute(
                "SELECT source_path, year FROM document_aspects "
                "ORDER BY source_path"
            ).fetchall())
        # p3 stays NULL because extras.year was missing.
        assert rows == {
            "/p1.pdf": 2023,
            "/p2.pdf": 2024,
            "/p3.pdf": None,
        }

    def test_promote_rejects_reserved_name(
        self, db_with_papers: Path,
    ) -> None:
        with T2Database(db_with_papers) as db:
            for name in [
                "collection", "source_path",
                "problem_formulation", "extras", "confidence",
            ]:
                with pytest.raises(ValueError, match="reserved"):
                    promote_extras_field(db, name)

    def test_promote_rejects_unsafe_identifier(
        self, db_with_papers: Path,
    ) -> None:
        with T2Database(db_with_papers) as db:
            for bad in [
                "1leading_digit",
                "has-hyphen",
                "has space",
                'has"quote',
                "has;semi",
                "DROP TABLE document_aspects",
                "",
            ]:
                with pytest.raises(ValueError):
                    promote_extras_field(db, bad)

    def test_promote_rejects_unknown_sql_type(
        self, db_with_papers: Path,
    ) -> None:
        with T2Database(db_with_papers) as db:
            with pytest.raises(ValueError, match="sql_type"):
                promote_extras_field(db, "venue", sql_type="BLOB")


# ── Audit log ───────────────────────────────────────────────────────────────


class TestAuditLog:
    def test_promotion_logged(self, db_with_papers: Path) -> None:
        with T2Database(db_with_papers) as db:
            promote_extras_field(db, "venue")
            entries = list_promotions(db)
        assert len(entries) == 1
        e = entries[0]
        assert e["field_name"] == "venue"
        assert e["sql_type"] == "TEXT"
        assert e["column_added"] is True
        assert e["rows_backfilled"] == 3
        assert e["pruned"] is False

    def test_multiple_promotions_logged_in_order(
        self, db_with_papers: Path,
    ) -> None:
        with T2Database(db_with_papers) as db:
            promote_extras_field(db, "venue")
            promote_extras_field(db, "year", sql_type="INTEGER")
            promote_extras_field(db, "venue", prune=True)  # second venue with prune
            entries = list_promotions(db)
        assert len(entries) == 3
        names = [e["field_name"] for e in entries]
        assert names == ["venue", "year", "venue"]
        # Last entry is the pruning re-run.
        assert entries[-1]["pruned"] is True


# ── CLI tests ───────────────────────────────────────────────────────────────


class TestCLI:
    def test_cli_promotes_and_reports(
        self, db_with_papers: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        import nexus.commands._helpers as h
        monkeypatch.setattr(h, "default_db_path", lambda: db_with_papers)

        runner = CliRunner()
        result = runner.invoke(
            enrich, ["aspects-promote-field", "venue"],
        )
        assert result.exit_code == 0, result.output
        assert "Added column venue TEXT" in result.output
        assert "Backfilled 3 row(s)" in result.output

    def test_cli_idempotent_re_run(
        self, db_with_papers: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        import nexus.commands._helpers as h
        monkeypatch.setattr(h, "default_db_path", lambda: db_with_papers)

        runner = CliRunner()
        runner.invoke(enrich, ["aspects-promote-field", "venue"])
        result = runner.invoke(
            enrich, ["aspects-promote-field", "venue"],
        )
        assert result.exit_code == 0
        assert "already exists" in result.output

    def test_cli_prune_flag(
        self, db_with_papers: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        import nexus.commands._helpers as h
        monkeypatch.setattr(h, "default_db_path", lambda: db_with_papers)

        runner = CliRunner()
        result = runner.invoke(
            enrich,
            ["aspects-promote-field", "venue", "--prune"],
        )
        assert result.exit_code == 0
        assert "Pruned 3 extras key(s)" in result.output

    def test_cli_history_flag(
        self, db_with_papers: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        import nexus.commands._helpers as h
        monkeypatch.setattr(h, "default_db_path", lambda: db_with_papers)

        runner = CliRunner()
        runner.invoke(enrich, ["aspects-promote-field", "venue"])
        runner.invoke(
            enrich,
            ["aspects-promote-field", "year", "--type", "INTEGER"],
        )
        result = runner.invoke(
            enrich, ["aspects-promote-field", "_unused", "--history"],
        )
        assert result.exit_code == 0
        assert "venue" in result.output
        assert "year" in result.output
        assert "INTEGER" in result.output

    def test_cli_history_empty(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        import nexus.commands._helpers as h
        empty_db = tmp_path / "empty.db"
        T2Database(empty_db).close()
        monkeypatch.setattr(h, "default_db_path", lambda: empty_db)

        runner = CliRunner()
        result = runner.invoke(
            enrich, ["aspects-promote-field", "_x", "--history"],
        )
        assert result.exit_code == 0
        assert "No promotion history" in result.output

    def test_cli_rejects_reserved_name_with_clear_error(
        self, db_with_papers: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        import nexus.commands._helpers as h
        monkeypatch.setattr(h, "default_db_path", lambda: db_with_papers)

        runner = CliRunner()
        result = runner.invoke(
            enrich, ["aspects-promote-field", "extras"],
        )
        assert result.exit_code == 2
        assert "reserved" in result.output.lower() \
            or "reserved" in (result.stderr or "").lower()
