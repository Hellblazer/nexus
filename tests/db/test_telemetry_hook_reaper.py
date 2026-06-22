# SPDX-License-Identifier: AGPL-3.0-or-later
"""nexus-7365x — hook_failures age reaper (audit-table TTL parity, RDR-164 P0).

Mirrors the search_telemetry reaper: an age-based trim on the no-cascade
hook_failures audit table, keyed on occurred_at. Exact-count assertions.
"""
from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from nexus.db.t2.telemetry import Telemetry


def _record(tel: Telemetry, doc_id: str, occurred_at: str) -> None:
    tel.record_hook_failure(
        doc_id=doc_id, collection="code__nexus", hook_name="h_" + doc_id,
        error="boom", chain="single", occurred_at=occurred_at,
    )


def test_trim_hook_failures_exact_count(tmp_path: Path) -> None:
    tel = Telemetry(tmp_path / "memory.db")
    try:
        old = (datetime.now(UTC) - timedelta(days=60)).isoformat()
        recent = (datetime.now(UTC) - timedelta(days=1)).isoformat()
        _record(tel, "old-1", old)
        _record(tel, "old-2", old)
        _record(tel, "recent-1", recent)

        deleted = tel.trim_hook_failures(days=30)

        assert deleted == 2  # exactly the two aged rows
        remaining = tel.conn.execute(
            "SELECT COUNT(*) FROM hook_failures"
        ).fetchone()[0]
        assert remaining == 1  # the recent row survives
    finally:
        tel.close()


def test_trim_hook_failures_absent_table_safe(tmp_path: Path) -> None:
    # Fresh DB: hook_failures is created by migration on first record, so a trim
    # before any record finds no table — must no-op, not raise.
    tel = Telemetry(tmp_path / "memory.db")
    try:
        assert tel.trim_hook_failures(days=30) == 0
    finally:
        tel.close()


def test_trim_hook_failures_default_occurred_at_is_iso_and_survives(tmp_path: Path) -> None:
    # nexus-7365x regression: a row recorded WITHOUT occurred_at must be stamped in
    # ISO-8601 (T separator), not the DDL DEFAULT CURRENT_TIMESTAMP space format —
    # otherwise the reaper's TEXT cutoff comparison skews at the cutoff-day boundary.
    tel = Telemetry(tmp_path / "memory.db")
    try:
        tel.record_hook_failure(
            doc_id="d-default", collection="c", hook_name="h", error="e",
            chain="single",  # no occurred_at → store stamps isoformat()
        )
        stored = tel.conn.execute(
            "SELECT occurred_at FROM hook_failures WHERE doc_id='d-default'"
        ).fetchone()[0]
        assert "T" in stored, f"occurred_at must be isoformat (T-separated); got {stored!r}"
        # A just-now default row is recent → a 30d trim must NOT delete it.
        assert tel.trim_hook_failures(days=30) == 0
        assert tel.conn.execute(
            "SELECT COUNT(*) FROM hook_failures"
        ).fetchone()[0] == 1
    finally:
        tel.close()


def test_trim_hook_failures_rejects_bad_days(tmp_path: Path) -> None:
    tel = Telemetry(tmp_path / "memory.db")
    try:
        with pytest.raises(ValueError):
            tel.trim_hook_failures(days=0)
    finally:
        tel.close()


def test_trim_hook_failures_facade_delegate(tmp_path: Path) -> None:
    from nexus.db.t2 import T2Database

    db = T2Database(tmp_path / "memory.db")
    try:
        old = (datetime.now(UTC) - timedelta(days=60)).isoformat()
        db.telemetry.record_hook_failure(
            doc_id="d", collection="c", hook_name="h", error="e",
            chain="single", occurred_at=old,
        )
        assert db.trim_hook_failures(days=30) == 1
    finally:
        db.close()
