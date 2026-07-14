# SPDX-License-Identifier: AGPL-3.0-or-later
"""nexus-ykzbj.6 (RDR-182 P1.2) — consent audit primitive.

Covers:
- ``migrate_claude_assisted_remediation_consents`` creates the
  ``claude_assisted_remediation_consents`` table + index, idempotently.
- ``Telemetry.record_consent`` writes exact (scope, ts, granted) rows.
- Both grant AND revoke events are first-class rows (an audit trail, not
  an upsert) — RDR-182's ``claude_assisted_remediation.enabled`` flag is
  revocable, so the audit must retain both directions of the toggle.
- The clock is caller-injected (``ts`` parameter) — no wall-clock read
  inside the store.
"""
from __future__ import annotations

import sqlite3
from pathlib import Path

from nexus.db.t2.telemetry import Telemetry

# ── Migration ────────────────────────────────────────────────────────────────


class TestMigration:
    def test_creates_table_with_expected_columns(self, tmp_path: Path) -> None:
        from nexus.db.migrations import migrate_claude_assisted_remediation_consents

        conn = sqlite3.connect(str(tmp_path / "t.db"))
        try:
            migrate_claude_assisted_remediation_consents(conn)
            cols = {
                row[1]
                for row in conn.execute(
                    "PRAGMA table_info(claude_assisted_remediation_consents)"
                )
            }
        finally:
            conn.close()
        assert {"id", "scope", "ts", "granted"}.issubset(cols)

    def test_creates_scope_index(self, tmp_path: Path) -> None:
        from nexus.db.migrations import migrate_claude_assisted_remediation_consents

        conn = sqlite3.connect(str(tmp_path / "t.db"))
        try:
            migrate_claude_assisted_remediation_consents(conn)
            indexes = {
                row[0]
                for row in conn.execute(
                    "SELECT name FROM sqlite_master "
                    "WHERE type='index' AND tbl_name='claude_assisted_remediation_consents'"
                )
            }
        finally:
            conn.close()
        assert "idx_consents_scope" in indexes

    def test_idempotent(self, tmp_path: Path) -> None:
        """Second call must be a clean no-op (no exception, no double-create)."""
        from nexus.db.migrations import migrate_claude_assisted_remediation_consents

        conn = sqlite3.connect(str(tmp_path / "t.db"))
        try:
            migrate_claude_assisted_remediation_consents(conn)
            migrate_claude_assisted_remediation_consents(conn)  # must not raise
            count = conn.execute(
                "SELECT COUNT(*) FROM sqlite_master "
                "WHERE type='table' AND name='claude_assisted_remediation_consents'"
            ).fetchone()[0]
        finally:
            conn.close()
        assert count == 1


# ── record_consent ───────────────────────────────────────────────────────────


class TestRecordConsent:
    def test_grant_writes_exact_row(self, tmp_path: Path) -> None:
        tel = Telemetry(tmp_path / "memory.db")
        try:
            tel.record_consent(
                scope="remediate:chash-poison", ts="2026-07-12T00:00:00+00:00",
                granted=True,
            )
            row = tel.conn.execute(
                "SELECT scope, ts, granted FROM claude_assisted_remediation_consents"
            ).fetchone()
        finally:
            tel.close()
        assert row == ("remediate:chash-poison", "2026-07-12T00:00:00+00:00", 1)

    def test_revoke_writes_exact_row(self, tmp_path: Path) -> None:
        tel = Telemetry(tmp_path / "memory.db")
        try:
            tel.record_consent(
                scope="remediate:chash-poison", ts="2026-07-12T01:00:00+00:00",
                granted=False,
            )
            row = tel.conn.execute(
                "SELECT scope, ts, granted FROM claude_assisted_remediation_consents"
            ).fetchone()
        finally:
            tel.close()
        assert row == ("remediate:chash-poison", "2026-07-12T01:00:00+00:00", 0)

    def test_grant_then_revoke_are_both_retained(self, tmp_path: Path) -> None:
        """Consent audit is an append-only trail, not an upsert-by-scope: a
        revoke must not overwrite or delete the prior grant row."""
        tel = Telemetry(tmp_path / "memory.db")
        try:
            tel.record_consent(
                scope="remediate:chash-poison", ts="2026-07-12T00:00:00+00:00",
                granted=True,
            )
            tel.record_consent(
                scope="remediate:chash-poison", ts="2026-07-12T02:00:00+00:00",
                granted=False,
            )
            rows = tel.conn.execute(
                "SELECT scope, ts, granted FROM claude_assisted_remediation_consents "
                "ORDER BY ts"
            ).fetchall()
        finally:
            tel.close()
        assert rows == [
            ("remediate:chash-poison", "2026-07-12T00:00:00+00:00", 1),
            ("remediate:chash-poison", "2026-07-12T02:00:00+00:00", 0),
        ]

    def test_distinct_scopes_are_independent_rows(self, tmp_path: Path) -> None:
        tel = Telemetry(tmp_path / "memory.db")
        try:
            tel.record_consent(
                scope="remediate:chash-poison", ts="2026-07-12T00:00:00+00:00",
                granted=True,
            )
            tel.record_consent(
                scope="forensics:catalog-013", ts="2026-07-12T00:05:00+00:00",
                granted=True,
            )
            count = tel.conn.execute(
                "SELECT COUNT(*) FROM claude_assisted_remediation_consents"
            ).fetchone()[0]
        finally:
            tel.close()
        assert count == 2

    def test_table_created_lazily_on_first_record(self, tmp_path: Path) -> None:
        """Mirrors record_tier_write / record_nx_answer_run: no table exists
        until the first record_consent call creates it idempotently."""
        tel = Telemetry(tmp_path / "memory.db")
        try:
            exists_before = tel.conn.execute(
                "SELECT name FROM sqlite_master "
                "WHERE type='table' AND name='claude_assisted_remediation_consents'"
            ).fetchone()
            assert exists_before is None

            tel.record_consent(
                scope="remediate:chash-poison", ts="2026-07-12T00:00:00+00:00",
                granted=True,
            )
            exists_after = tel.conn.execute(
                "SELECT name FROM sqlite_master "
                "WHERE type='table' AND name='claude_assisted_remediation_consents'"
            ).fetchone()
        finally:
            tel.close()
        assert exists_after is not None

    def test_facade_delegate(self, tmp_path: Path) -> None:
        from nexus.db.t2 import T2Database

        db = T2Database(tmp_path / "memory.db")
        try:
            db.telemetry.record_consent(
                scope="remediate:chash-poison", ts="2026-07-12T00:00:00+00:00",
                granted=True,
            )
            row = db.telemetry.conn.execute(
                "SELECT scope, granted FROM claude_assisted_remediation_consents"
            ).fetchone()
        finally:
            db.close()
        assert row == ("remediate:chash-poison", 1)
