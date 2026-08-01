# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""RDR-089 Phase E after the runtime verb's retirement (nexus-70x7y).

``promote_extras_field()`` ran a runtime ``ALTER TABLE`` + backfill against
SQLite. That has no valid implementation under the ALL-DDL-through-Liquibase
directive, so the verb is deleted rather than ported to the service backend:
promoting a field is now one changeset that does the ADD COLUMN, the backfill,
and the ``aspect_promotion_log`` insert together.

These tests pin what SURVIVES — the audit-log read path on both substrates,
and the fail-loud recipe an operator gets for the retired verb. Each assertion
below is the successor of a test that pinned the old mechanic; the retired
behaviour is asserted ABSENT rather than left as a hole.
"""
from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest

from tests._t2_fixture_ops import bootstrap_migration_source
from click.testing import CliRunner

from nexus.aspect_extractor import AspectRecord
from nexus.aspect_promotion import PROMOTION_RETIRED, list_promotions
from nexus.commands.enrich import enrich
from nexus.db.t2 import T2Database

def _make_record(*, source_path: str = "/p1.pdf", extras: dict | None = None) -> AspectRecord:
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
def db_with_papers(tmp_path: Path) -> Path:
    """T2 DB with three papers carrying ``extras.venue`` / ``extras.year``."""
    db_path = tmp_path / "promotion.db"
    # nexus-aqbrk: build the local SQLite schema explicitly — T2Database(path)
    # never creates it (the bootstrap machinery died with db/migrations.py in
    # RDR-158 P4 Stage 4; the fixture executescripts a frozen DDL snapshot).
    # Kept so the CLI tests that open this path see a well-formed
    # migration-source file.
    # (The legacy-history seed itself no longer touches this file — since
    # sub-stage A3 it goes through the promotion ETL import; see
    # _seed_legacy_history.)
    bootstrap_migration_source(db_path)
    with T2Database(db_path) as db:
        db.document_aspects.upsert(_make_record(
            source_path="/p1.pdf", extras={"venue": "VLDB", "year": 2023}))
        db.document_aspects.upsert(_make_record(
            source_path="/p2.pdf", extras={"venue": "OSDI", "year": 2024}))
        db.document_aspects.upsert(_make_record(
            source_path="/p3.pdf", extras={"venue": "SIGMOD"}))
    return db_path


def _seed_legacy_history(db_path: Path, rows: list[dict]) -> None:
    """Plant rows in the log as a pre-retirement install's history.

    Ported (nexus-i711w Stage 2 sub-stage A3): the raw sqlite3 INSERT died
    with the SQLite ``aspect_promotion_log`` read path. Post-migration, the
    history of an install that promoted while the mechanic was live reaches
    the engine via the promotion ETL import — so the seed IS that path:
    ``HttpDocumentAspectsStore.import_promotion_batch`` (the RDR-176 P3
    bulk import, ``POST /v1/aspects/promotion/import``).
    """
    with T2Database(db_path) as db:
        imported = db.document_aspects.import_promotion_batch(rows)
    assert imported == len(rows), (
        f"promotion ETL seed: expected {len(rows)} rows imported, got {imported}"
    )


def _history_row(
    field_name: str,
    sql_type: str,
    column_added: bool,
    rows_backfilled: int,
    rows_pruned: int,
    pruned: bool,
    promoted_at: str,
) -> dict:
    """One pre-retirement promotion event in the ETL import's dict shape.

    ``column_added`` / ``pruned`` must be real booleans — the engine's
    importer applies ``Boolean.TRUE.equals`` (the SQLite tuples' 1/0 ints
    would silently import as False).
    """
    return {
        "field_name": field_name,
        "sql_type": sql_type,
        "column_added": column_added,
        "rows_backfilled": rows_backfilled,
        "rows_pruned": rows_pruned,
        "pruned": pruned,
        "promoted_at": promoted_at,
    }


# ── The retired verb ────────────────────────────────────────────────────────


class TestPromotionVerbRetired:
    """Successor to TestPromoteSubstrate: the mechanic is asserted GONE."""

    def test_promote_extras_field_no_longer_exists(self) -> None:
        """The runtime promotion entry point is deleted, not deprecated.

        Successor assertion for every test that exercised ALTER TABLE +
        backfill: a re-added runtime promoter fails here rather than
        silently reintroducing client-side DDL.
        """
        import nexus.aspect_promotion as ap

        assert not hasattr(ap, "promote_extras_field")
        assert not hasattr(ap, "PromotionResult")

    def test_module_runs_no_ddl(self) -> None:
        """The module carries no CREATE/ALTER TABLE text at all.

        The old module held both (the lazy audit-table bootstrap and the
        promotion ALTER); the NO-SQLITE census froze them as debt. This
        pins the debt at zero so a future edit cannot quietly restore it.
        """
        src = Path("src/nexus/aspect_promotion.py").read_text(encoding="utf-8")
        assert "CREATE TABLE" not in src.upper()
        # The changeset recipe in the docstring is PG DDL for Liquibase, not
        # SQLite executed here — assert no statement is ever handed to sqlite3.
        assert "conn.execute" not in src
        assert "executescript" not in src

    def test_retired_message_names_the_replacement_path(self) -> None:
        """The recipe must tell the operator what to do instead — all three
        changeset phases plus where the template lives."""
        msg = PROMOTION_RETIRED.format(field="venue")
        assert "venue" in msg
        assert "Liquibase" in msg
        assert "ADD COLUMN" in msg
        assert "aspect_promotion_log" in msg
        assert "aspect_promotion.py" in msg


# ── Audit-log read path (SQLite substrate) ──────────────────────────────────


class TestHistoryReadPath:
    def test_empty_when_never_promoted(self, db_with_papers: Path) -> None:
        """A migrated T2 that never promoted anything has an empty log."""
        with T2Database(db_with_papers) as db:
            assert list_promotions(db) == []

    # test_absent_table_reads_empty_and_is_not_created stood here
    # (nexus-i711w Stage 2 sub-stage A3). It pinned the SQLite store's OWN
    # refusal to lazily bootstrap ``aspect_promotion_log`` — asserted via
    # sqlite_master on a bare ``DocumentAspects(path)``. The store is
    # deleted, so the subject (a client-side SQLite table that could be
    # bootstrapped at read time) is gone; the no-client-DDL half survives
    # as TestPromotionVerbRetired::test_module_runs_no_ddl, and the PG
    # table is Liquibase-owned from birth (aspects-001-baseline.xml), so
    # "absent table" is not a reachable state on the only substrate left.

    # Ported (nexus-i711w Stage 2 sub-stage A3, superseding the nexus-aqbrk
    # pin): the pin's rationale — "no service-mode way to plant what the old
    # install left behind" — stopped being true when the promotion ETL
    # import landed (RDR-176 P3, /v1/aspects/promotion/import): that IS the
    # channel a pre-retirement install's history arrives by after migration.
    # _seed_legacy_history now seeds through it, so the local_t2_backend pin
    # (whose SQLite read path is deleted) is gone.
    def test_reads_pre_retirement_rows_oldest_first(
        self, db_with_papers: Path,
    ) -> None:
        """An install that promoted while the mechanic was live keeps its
        history. Successor to TestAuditLog::test_multiple_promotions_logged."""
        _seed_legacy_history(db_with_papers, [
            _history_row("venue", "TEXT", True, 3, 0, False, "2026-01-01T00:00:00+00:00"),
            _history_row("year", "INTEGER", True, 2, 0, False, "2026-02-01T00:00:00+00:00"),
            _history_row("venue", "TEXT", False, 0, 3, True, "2026-03-01T00:00:00+00:00"),
        ])
        with T2Database(db_with_papers) as db:
            entries = list_promotions(db)

        assert [e["field_name"] for e in entries] == ["venue", "year", "venue"]
        assert entries[0]["sql_type"] == "TEXT"
        assert entries[0]["column_added"] is True
        assert entries[0]["rows_backfilled"] == 3
        assert entries[0]["pruned"] is False
        assert entries[1]["sql_type"] == "INTEGER"
        assert entries[-1]["pruned"] is True
        assert entries[-1]["rows_pruned"] == 3


# ── CLI ─────────────────────────────────────────────────────────────────────


class TestCLI:
    def test_promote_invocation_prints_recipe_and_exits_two(
        self, db_with_papers: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Successor to test_cli_promotes_and_reports: the old happy path is
        now the fail-loud path, and it teaches the replacement."""
        monkeypatch.setattr("nexus.config.default_db_path", lambda: db_with_papers)

        result = CliRunner().invoke(enrich, ["aspects-promote-field", "venue"])

        assert result.exit_code == 2, result.output
        combined = (result.output or "") + (result.stderr or "")
        assert "venue" in combined
        assert "Liquibase" in combined
        assert "Traceback" not in combined

    def test_bare_invocation_exits_two(
        self, db_with_papers: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setattr("nexus.config.default_db_path", lambda: db_with_papers)

        result = CliRunner().invoke(enrich, ["aspects-promote-field"])

        assert result.exit_code == 2
        assert "Traceback" not in ((result.output or "") + (result.stderr or ""))

    def test_retired_options_are_gone(
        self, db_with_papers: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """--type and --prune only fed the deleted DDL path. They must fail
        loudly rather than linger as flags that silently do nothing."""
        monkeypatch.setattr("nexus.config.default_db_path", lambda: db_with_papers)

        runner = CliRunner()
        for argv in (
            ["aspects-promote-field", "venue", "--type", "TEXT"],
            ["aspects-promote-field", "venue", "--prune"],
        ):
            result = runner.invoke(enrich, argv)
            assert result.exit_code != 0, argv
            combined = (result.output or "") + (result.stderr or "")
            assert "no such option" in combined.lower(), combined

    def test_history_flag_renders_entries(
        self, db_with_papers: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        _seed_legacy_history(db_with_papers, [
            _history_row("venue", "TEXT", True, 3, 0, False, "2026-01-01T00:00:00+00:00"),
            _history_row("year", "INTEGER", True, 2, 0, False, "2026-02-01T00:00:00+00:00"),
        ])
        monkeypatch.setattr("nexus.config.default_db_path", lambda: db_with_papers)

        result = CliRunner().invoke(
            enrich, ["aspects-promote-field", "--history"],
        )

        assert result.exit_code == 0, result.output
        assert "venue" in result.output
        assert "year" in result.output
        assert "INTEGER" in result.output

    def test_history_accepts_the_legacy_dummy_argument(
        self, db_with_papers: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """FIELD_NAME became optional; the old ``--history <dummy>`` form that
        operators (and scripts) already type must keep working."""
        _seed_legacy_history(db_with_papers, [
            _history_row("venue", "TEXT", True, 3, 0, False, "2026-01-01T00:00:00+00:00"),
        ])
        monkeypatch.setattr("nexus.config.default_db_path", lambda: db_with_papers)

        result = CliRunner().invoke(
            enrich, ["aspects-promote-field", "_x", "--history"],
        )

        assert result.exit_code == 0, result.output
        assert "venue" in result.output

    def test_history_empty(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        empty_db = tmp_path / "empty.db"
        T2Database(empty_db).close()
        monkeypatch.setattr("nexus.config.default_db_path", lambda: empty_db)

        result = CliRunner().invoke(
            enrich, ["aspects-promote-field", "--history"],
        )

        assert result.exit_code == 0
        assert "No promotion history" in result.output
