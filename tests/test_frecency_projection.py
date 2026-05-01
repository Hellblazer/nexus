# SPDX-License-Identifier: AGPL-3.0-or-later

"""Tests for the RDR-101 Phase 1 PR D ``frecency`` projection table.

Coverage:
- The migration creates the table with the documented columns and types.
- The migration is idempotent across re-runs.
- Phase 1 ships the schema only (no domain-store API, no projector
  handler) — these tests exercise the schema contract via direct SQL,
  not via a higher-level wrapper.
- Decay queries can derive ``expires_at`` from ``(embedded_at,
  ttl_days)`` without a materialized column.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from nexus.db.migrations import (
    MIGRATIONS,
    migrate_frecency_projection_table,
)


_FRECENCY_COLUMNS: dict[str, tuple[str, str, int]] = {
    # name → (type, default, pk_position)
    "chunk_id":       ("TEXT",    None,  1),
    "embedded_at":    ("TEXT",    "''",  0),
    "ttl_days":       ("INTEGER", "0",   0),
    "frecency_score": ("REAL",    "0",   0),
    "miss_count":     ("INTEGER", "0",   0),
    "last_hit_at":    ("TEXT",    "''",  0),
}


def _columns(conn: sqlite3.Connection, table: str) -> dict[str, dict]:
    cur = conn.execute(f"PRAGMA table_info({table})")
    return {
        row[1]: {"type": row[2], "notnull": row[3], "dflt": row[4], "pk": row[5]}
        for row in cur.fetchall()
    }


# ── Schema ───────────────────────────────────────────────────────────────


class TestFrecencySchema:
    def test_migration_creates_table(self):
        conn = sqlite3.connect(":memory:")
        migrate_frecency_projection_table(conn)
        row = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='frecency'"
        ).fetchone()
        assert row is not None

    def test_columns_match_spec(self):
        conn = sqlite3.connect(":memory:")
        migrate_frecency_projection_table(conn)
        cols = _columns(conn, "frecency")
        assert set(cols.keys()) == set(_FRECENCY_COLUMNS.keys())
        for name, (expected_type, expected_dflt, expected_pk) in _FRECENCY_COLUMNS.items():
            meta = cols[name]
            assert meta["type"] == expected_type, (
                f"{name}: type {meta['type']!r}, expected {expected_type!r}"
            )
            assert meta["pk"] == expected_pk, (
                f"{name}: pk position {meta['pk']}, expected {expected_pk}"
            )
            if expected_dflt is None:
                # PK columns don't carry an explicit default.
                continue
            assert meta["dflt"] == expected_dflt, (
                f"{name}: default {meta['dflt']!r}, expected {expected_dflt!r}"
            )

    def test_chunk_id_is_primary_key(self):
        conn = sqlite3.connect(":memory:")
        migrate_frecency_projection_table(conn)
        # PRAGMA reports pk=1 for chunk_id.
        cols = _columns(conn, "frecency")
        pks = [name for name, meta in cols.items() if meta["pk"] > 0]
        assert pks == ["chunk_id"]

    def test_no_expires_at_column(self):
        # Phase 1 simpler-path direction: expires_at is derived, not
        # materialized. This test is the regression guard if a future
        # change tries to add a column for it without an RDR amendment.
        conn = sqlite3.connect(":memory:")
        migrate_frecency_projection_table(conn)
        cols = _columns(conn, "frecency")
        assert "expires_at" not in cols, (
            "Phase 1 PR D chose to derive expires_at from "
            "(embedded_at, ttl_days). Materializing it as a column "
            "requires an RDR-101 amendment."
        )


# ── Idempotency ──────────────────────────────────────────────────────────


class TestFrecencyMigrationIdempotent:
    def test_re_apply_is_noop(self):
        conn = sqlite3.connect(":memory:")
        migrate_frecency_projection_table(conn)
        # Insert a row to verify re-application doesn't clobber data.
        conn.execute(
            "INSERT INTO frecency "
            "(chunk_id, embedded_at, ttl_days, frecency_score, "
            "miss_count, last_hit_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            ("ck-1", "2026-04-30T00:00:00Z", 30, 0.75, 2, "2026-04-30T11:00:00Z"),
        )
        conn.commit()

        migrate_frecency_projection_table(conn)  # must not raise
        migrate_frecency_projection_table(conn)  # must not raise

        # Data preserved.
        row = conn.execute(
            "SELECT chunk_id, embedded_at, ttl_days, frecency_score, "
            "miss_count, last_hit_at FROM frecency"
        ).fetchone()
        assert row == ("ck-1", "2026-04-30T00:00:00Z", 30, 0.75, 2, "2026-04-30T11:00:00Z")


# ── Migration registration ───────────────────────────────────────────────


class TestFrecencyMigrationRegistered:
    def test_in_migrations_list(self):
        matches = [
            (m.introduced, m.name) for m in MIGRATIONS
            if "frecency" in m.name.lower()
        ]
        assert matches, "frecency migration must be registered in MIGRATIONS"
        # Must ship in the same minor as the rest of Phase 1 (4.21.x);
        # bumped to 4.21.4 because 4.21.3 is the chash_index rename.
        assert matches[0][0] == "4.21.4", (
            f"Frecency migration should be at 4.21.4; got {matches[0][0]}"
        )


# ── Decay query: derive expires_at without a column ──────────────────────


class TestExpiresAtDerivation:
    """Validate that the simpler-path choice (no materialized
    ``expires_at``) supports a usable decay query."""

    def test_derive_expires_at_from_embedded_at_plus_ttl(self):
        conn = sqlite3.connect(":memory:")
        migrate_frecency_projection_table(conn)
        # Three rows: two should be expired as of "now", one fresh.
        rows = [
            ("ck-old",   "2024-01-01T00:00:00Z", 30, 0.1, 0, ""),  # very old
            ("ck-edge",  "2026-03-31T00:00:00Z", 30, 0.5, 0, ""),  # ~30 days ago
            ("ck-fresh", "2026-04-29T00:00:00Z",  7, 0.9, 0, ""),  # 1 day ago, 7-day TTL
        ]
        conn.executemany(
            "INSERT INTO frecency "
            "(chunk_id, embedded_at, ttl_days, frecency_score, "
            "miss_count, last_hit_at) VALUES (?, ?, ?, ?, ?, ?)",
            rows,
        )
        conn.commit()

        # SQL composes the derived expires_at via datetime() arithmetic.
        # The query is the canonical Phase 5 sweep shape.
        cur = conn.execute(
            "SELECT chunk_id FROM frecency "
            "WHERE datetime(embedded_at, '+' || ttl_days || ' days') < ? "
            "ORDER BY chunk_id",
            ("2026-04-30T00:00:00Z",),
        )
        expired = [r[0] for r in cur.fetchall()]
        # ck-old expired long ago; ck-edge expired 2026-04-30; ck-fresh
        # is still within its 7-day window.
        assert expired == ["ck-edge", "ck-old"]
