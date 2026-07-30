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
    """Facade-level consent audit (the production shape: mcp/core.py's
    remediate gate calls ``_db.telemetry.record_consent``).

    The SQLite ``Telemetry`` store's own record_consent tests died with the
    store (nexus-i711w Stage 2 sub-stage A); the exact-row / append-only
    audit-trail semantics against the REAL PG store are engine-side
    territory (wire shape pinned in tests/db/test_http_telemetry_store.py).
    """

    def test_facade_delegate(self, tmp_path: Path) -> None:
        from nexus.db.t2 import T2Database

        db = T2Database(tmp_path / "memory.db")
        try:
            db.telemetry.record_consent(
                scope="remediate:chash-poison", ts="2026-07-12T00:00:00+00:00",
                granted=True,
            )
            # Read back through the public surface (substrate-neutral;
            # RDR-155 P4b P0a') instead of a raw-conn SELECT.
            consents = db.telemetry.list_consents()
        finally:
            db.close()
        assert [(c["scope"], bool(c["granted"])) for c in consents] == [
            ("remediate:chash-poison", True)
        ]
