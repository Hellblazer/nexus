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

from pathlib import Path

# ── Migration tests DELETED (RDR-158 P4 Stage 4, nexus-i711w): the
# migrate_claude_assisted_remediation_consents chain died with
# nexus/db/migrations.py; the consents table is engine-owned (Liquibase,
# nexus-ng2sy). ──────────────────────────────────────────────────────────────


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
