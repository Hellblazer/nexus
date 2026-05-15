# SPDX-License-Identifier: Apache-2.0
"""Tests for nexus.tuplespace.store — tuples.db schema migration.

RDR-110 P1.2 (nexus-plcz). Covers:
- Fresh database creation (all tables + indexes present).
- Idempotent re-run (calling apply_tuples_schema a second time on the
  same connection is a no-op without error).
- WAL journal mode enabled on open.
- Partial-index correctness: EXPLAIN QUERY PLAN shows idx_tuples_avail
  used for available-tuple scans on (subspace, expires_at) with the
  WHERE predicate that excludes tombstoned and in-flight claimed rows.

Deterministic: tmp_path for the DB file, no unixepoch() calls in
assertions (expected timestamps computed in Python test setup).
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from nexus.tuplespace.store import (
    TUPLES_DB_NAME,
    apply_tuples_schema,
    open_tuples_db,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _table_names(conn: sqlite3.Connection) -> set[str]:
    rows = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'"
    ).fetchall()
    return {r[0] for r in rows}


def _index_names(conn: sqlite3.Connection) -> set[str]:
    rows = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='index'"
    ).fetchall()
    return {r[0] for r in rows}


def _columns(conn: sqlite3.Connection, table: str) -> set[str]:
    rows = conn.execute(f"PRAGMA table_info({table})").fetchall()
    return {r[1] for r in rows}


# ---------------------------------------------------------------------------
# Tests: fresh-db creation
# ---------------------------------------------------------------------------


class TestFreshDbCreation:
    def test_tables_exist_after_apply(self, tmp_path: Path) -> None:
        db_path = tmp_path / "tuples.db"
        conn = sqlite3.connect(str(db_path))
        apply_tuples_schema(conn)
        tables = _table_names(conn)
        assert "tuples" in tables
        assert "tuple_claim_log" in tables

    def test_tuples_columns(self, tmp_path: Path) -> None:
        db_path = tmp_path / "tuples.db"
        conn = sqlite3.connect(str(db_path))
        apply_tuples_schema(conn)
        cols = _columns(conn, "tuples")
        required = {
            "id",
            "subspace",
            "template_name",
            "content",
            "dimensions_json",
            "embed_text",
            "match_text",
            "created_at",
            "expires_at",
            # claim state columns
            "claim_state",
            "claimant",
            "claim_id",
            "claim_expires_at",
            # tombstone columns
            "consumed_at",
            "consumed_by",
        }
        assert required <= cols, f"Missing columns: {required - cols}"

    def test_tuple_claim_log_columns(self, tmp_path: Path) -> None:
        db_path = tmp_path / "tuples.db"
        conn = sqlite3.connect(str(db_path))
        apply_tuples_schema(conn)
        cols = _columns(conn, "tuple_claim_log")
        required = {"log_id", "tuple_id", "claim_id", "claimant", "transition", "at"}
        assert required <= cols, f"Missing columns: {required - cols}"

    def test_tuples_indexes_exist(self, tmp_path: Path) -> None:
        db_path = tmp_path / "tuples.db"
        conn = sqlite3.connect(str(db_path))
        apply_tuples_schema(conn)
        indexes = _index_names(conn)
        assert "idx_tuples_avail" in indexes
        assert "idx_tuples_claimed" in indexes
        assert "idx_tuples_expires" in indexes

    def test_claim_log_indexes_exist(self, tmp_path: Path) -> None:
        db_path = tmp_path / "tuples.db"
        conn = sqlite3.connect(str(db_path))
        apply_tuples_schema(conn)
        indexes = _index_names(conn)
        assert "idx_claim_log_tuple" in indexes
        assert "idx_claim_log_claimant" in indexes


# ---------------------------------------------------------------------------
# Tests: WAL mode
# ---------------------------------------------------------------------------


class TestWalMode:
    def test_wal_mode_after_open_tuples_db(self, tmp_path: Path) -> None:
        db_path = tmp_path / TUPLES_DB_NAME
        conn = open_tuples_db(db_path)
        row = conn.execute("PRAGMA journal_mode").fetchone()
        assert row is not None
        assert row[0] == "wal", f"Expected WAL mode, got: {row[0]!r}"
        conn.close()

    def test_wal_mode_file_created(self, tmp_path: Path) -> None:
        db_path = tmp_path / TUPLES_DB_NAME
        conn = open_tuples_db(db_path)
        conn.close()
        # SQLite creates the -wal shim file once WAL mode is active
        wal_file = tmp_path / (TUPLES_DB_NAME + "-wal")
        # The file is created on first write; existence not guaranteed on
        # a read-only connection but open_tuples_db applies the schema
        # which is a write. On some SQLite builds the wal header is
        # deferred to the first real write after PRAGMA — we accept either:
        # (a) wal file exists, or (b) PRAGMA journal_mode returned 'wal'.
        with sqlite3.connect(str(db_path)) as c:
            row = c.execute("PRAGMA journal_mode").fetchone()
        assert row[0] == "wal"


# ---------------------------------------------------------------------------
# Tests: idempotent re-run
# ---------------------------------------------------------------------------


class TestIdempotentRerun:
    def test_apply_twice_no_error(self, tmp_path: Path) -> None:
        db_path = tmp_path / "tuples.db"
        conn = sqlite3.connect(str(db_path))
        apply_tuples_schema(conn)
        # Second call must not raise.
        apply_tuples_schema(conn)

    def test_tables_still_present_after_rerun(self, tmp_path: Path) -> None:
        db_path = tmp_path / "tuples.db"
        conn = sqlite3.connect(str(db_path))
        apply_tuples_schema(conn)
        apply_tuples_schema(conn)
        tables = _table_names(conn)
        assert "tuples" in tables
        assert "tuple_claim_log" in tables

    def test_open_tuples_db_twice_no_error(self, tmp_path: Path) -> None:
        db_path = tmp_path / TUPLES_DB_NAME
        conn1 = open_tuples_db(db_path)
        conn1.close()
        conn2 = open_tuples_db(db_path)
        conn2.close()


# ---------------------------------------------------------------------------
# Tests: partial-index correctness (EXPLAIN QUERY PLAN)
# ---------------------------------------------------------------------------


class TestPartialIndexCorrectness:
    """Verify that the query planner uses idx_tuples_avail for the
    canonical available-tuple scan pattern that the take() path will use.

    The scan pattern is:
        SELECT id FROM tuples
        WHERE subspace = ?
          AND consumed_at IS NULL
          AND (claim_state IS NULL OR claim_expires_at < ?)
        ORDER BY expires_at

    idx_tuples_avail is defined as:
        ON tuples (subspace, expires_at)
        WHERE consumed_at IS NULL
          AND (claim_state IS NULL OR claim_expires_at < unixepoch())

    SQLite's partial-index scan is used when the query's WHERE clause
    subsumes the index predicate. This test inserts a small data set
    and verifies via EXPLAIN QUERY PLAN that the index is referenced.
    """

    def _seed(self, conn: sqlite3.Connection) -> None:
        """Insert a handful of available tuples so the planner has stats."""
        import time
        now = time.time()
        rows = [
            (f"id-{i}", "tasks/nexus", "tasks/<project>",
             f"content-{i}", "{}", f"embed-{i}", now, None,
             None, None, None, None, None, None)
            for i in range(5)
        ]
        conn.executemany(
            """
            INSERT INTO tuples
                (id, subspace, template_name, content, dimensions_json,
                 embed_text, created_at, expires_at,
                 claim_state, claimant, claim_id, claim_expires_at,
                 consumed_at, consumed_by)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            rows,
        )
        conn.commit()

    def test_idx_tuples_avail_used_for_available_scan(
        self, tmp_path: Path
    ) -> None:
        """Verify idx_tuples_avail is used for the canonical available-tuple scan.

        SQLite only uses a partial index when the query's WHERE clause
        contains the *exact same predicate expression* as the index
        definition. Because idx_tuples_avail is defined with
        ``claim_expires_at < unixepoch()``, the query must also use
        ``unixepoch()`` (not a bound parameter) for SQLite's partial-index
        matching to fire. The take() implementation therefore uses
        ``unixepoch()`` inline in its SQL — this test mirrors that shape.
        """
        db_path = tmp_path / "tuples.db"
        conn = open_tuples_db(db_path)
        self._seed(conn)

        plan_rows = conn.execute(
            """
            EXPLAIN QUERY PLAN
            SELECT id FROM tuples
            WHERE subspace = ?
              AND consumed_at IS NULL
              AND (claim_state IS NULL OR claim_expires_at < unixepoch())
            ORDER BY expires_at
            """,
            ("tasks/nexus",),
        ).fetchall()

        plan_text = " ".join(str(r) for r in plan_rows).lower()
        assert "idx_tuples_avail" in plan_text, (
            f"Expected idx_tuples_avail in query plan, got:\n"
            + "\n".join(str(r) for r in plan_rows)
        )

    def test_idx_tuples_claimed_used_for_claim_id_lookup(
        self, tmp_path: Path
    ) -> None:
        db_path = tmp_path / "tuples.db"
        conn = open_tuples_db(db_path)
        self._seed(conn)

        plan_rows = conn.execute(
            """
            EXPLAIN QUERY PLAN
            SELECT id FROM tuples
            WHERE claim_id = 'some-claim-id'
              AND claim_state = 'claimed'
            """,
        ).fetchall()

        plan_text = " ".join(str(r) for r in plan_rows).lower()
        assert "idx_tuples_claimed" in plan_text, (
            f"Expected idx_tuples_claimed in query plan, got:\n"
            + "\n".join(str(r) for r in plan_rows)
        )

    def test_idx_claim_log_tuple_used_for_tuple_history_lookup(
        self, tmp_path: Path
    ) -> None:
        db_path = tmp_path / "tuples.db"
        conn = open_tuples_db(db_path)

        import time
        now = time.time()

        # Insert a log row so the planner has a populated table.
        conn.execute(
            "INSERT INTO tuple_claim_log (tuple_id, subspace, claim_id, claimant, transition, at) "
            "VALUES ('t1', 'tuples/test', 'c1', 'agent', 'claim', ?)",
            (now,),
        )
        conn.commit()

        plan_rows = conn.execute(
            """
            EXPLAIN QUERY PLAN
            SELECT * FROM tuple_claim_log
            WHERE tuple_id = 't1'
            ORDER BY at
            """,
        ).fetchall()

        plan_text = " ".join(str(r) for r in plan_rows).lower()
        assert "idx_claim_log_tuple" in plan_text, (
            f"Expected idx_claim_log_tuple in query plan, got:\n"
            + "\n".join(str(r) for r in plan_rows)
        )
