# SPDX-License-Identifier: AGPL-3.0-or-later
"""Unit tests for the SQLite->Postgres telemetry ETL (bead nexus-gmiaf.12).

These tests run entirely in-process without a real Java service or Postgres.
They validate:
  - Field mapping and NULL-preservation for agent/project/target_title in
    tier_writes (Fix 3: _nullable_str not _str_or_empty)
  - copy-not-move: SQLite source file is never written
  - Table-absent tolerance: missing tables are silently skipped
"""
from __future__ import annotations

import sqlite3
import tempfile
from pathlib import Path
from unittest.mock import MagicMock, call

import pytest

from nexus.db.t2.telemetry_etl import (
    _nullable_str,
    _str_or_empty,
    count_source_rows,
    migrate_telemetry_rows,
)


# ── Helper: _nullable_str and _str_or_empty semantics ────────────────────────

class TestNullableStr:
    """Validate the helper used for nullable PG columns."""

    def test_none_returns_none(self):
        assert _nullable_str(None) is None

    def test_empty_string_returns_none(self):
        """Empty string in SQLite should become NULL in PG."""
        assert _nullable_str("") is None

    def test_non_empty_returns_string(self):
        assert _nullable_str("developer") == "developer"
        assert _nullable_str("nexus") == "nexus"
        assert _nullable_str("some-title") == "some-title"

    def test_str_or_empty_contrast(self):
        """_str_or_empty preserves '' for non-nullable columns; _nullable_str does not."""
        assert _str_or_empty(None) == ""
        assert _str_or_empty("") == ""
        assert _nullable_str(None) is None


# ── Helper: build a minimal in-memory SQLite with tier_writes rows ─────────────

def _make_tier_writes_db(rows: list[dict]) -> sqlite3.Connection:
    """Return an in-memory SQLite connection with a tier_writes table."""
    conn = sqlite3.connect(":memory:")
    conn.execute("""
        CREATE TABLE tier_writes (
            id INTEGER PRIMARY KEY,
            session_id TEXT,
            ts TEXT,
            tool TEXT,
            tier TEXT,
            agent TEXT,
            project TEXT,
            target_title TEXT
        )
    """)
    for i, r in enumerate(rows):
        conn.execute(
            "INSERT INTO tier_writes (id, session_id, ts, tool, tier, agent, project, target_title) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                i + 1,
                r.get("session_id", "sess"),
                r.get("ts", "2024-01-01T00:00:00Z"),
                r.get("tool", "memory_put"),
                r.get("tier", "T2"),
                r.get("agent"),          # None preserved as NULL
                r.get("project"),
                r.get("target_title"),
            ),
        )
    conn.commit()
    return conn


# ── Unit tests: NULL-preservation for tier_writes (Fix 3) ────────────────────

class TestTierWriteNullPreservation:
    """
    Fix 3: _migrate_tier_writes used _str_or_empty for agent/project/target_title
    instead of _nullable_str. A NULL SQLite value was sent as "" to PG, corrupting
    GROUP BY agent / WHERE agent IS NOT NULL aggregations.

    These tests verify that NULL SQLite values become None in the store call
    (the store serialises None as JSON null → PG NULL via the Java service).
    """

    def test_null_agent_passed_as_none_to_store(self):
        """NULL agent in SQLite must be passed as None to store.import_tier_write."""
        store = MagicMock()
        store.import_tier_write.return_value = None
        store.import_relevance_row.return_value = None
        store.import_search_row.return_value = None
        store.import_nx_answer_run.return_value = None
        store.import_hook_failure.return_value = None
        store.import_frecency_row.return_value = None

        with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
            db_path = Path(f.name)

        conn = sqlite3.connect(str(db_path))
        conn.execute("""
            CREATE TABLE tier_writes (
                id INTEGER PRIMARY KEY, session_id TEXT, ts TEXT,
                tool TEXT, tier TEXT, agent TEXT, project TEXT, target_title TEXT
            )
        """)
        # Row with NULL agent, project, target_title
        conn.execute(
            "INSERT INTO tier_writes VALUES (1, 'sess1', '2024-01-15T10:30:00Z',"
            " 'memory_put', 'T2', NULL, NULL, NULL)"
        )
        conn.commit()
        conn.close()

        try:
            migrate_telemetry_rows(db_path, store)
        finally:
            db_path.unlink(missing_ok=True)

        # Verify the call that reached the store
        assert store.import_tier_write.call_count == 1
        _, kwargs = store.import_tier_write.call_args
        assert kwargs["agent"] is None, (
            f"agent must be None (not '') for NULL SQLite value; got {kwargs['agent']!r}")
        assert kwargs["project"] is None, (
            f"project must be None (not '') for NULL SQLite value; got {kwargs['project']!r}")
        assert kwargs["target_title"] is None, (
            f"target_title must be None (not '') for NULL SQLite value; got {kwargs['target_title']!r}")

    def test_populated_agent_passed_verbatim(self):
        """Non-NULL agent in SQLite must be passed verbatim to store."""
        store = MagicMock()
        store.import_tier_write.return_value = None
        store.import_relevance_row.return_value = None
        store.import_search_row.return_value = None
        store.import_nx_answer_run.return_value = None
        store.import_hook_failure.return_value = None
        store.import_frecency_row.return_value = None

        with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
            db_path = Path(f.name)

        conn = sqlite3.connect(str(db_path))
        conn.execute("""
            CREATE TABLE tier_writes (
                id INTEGER PRIMARY KEY, session_id TEXT, ts TEXT,
                tool TEXT, tier TEXT, agent TEXT, project TEXT, target_title TEXT
            )
        """)
        conn.execute(
            "INSERT INTO tier_writes VALUES (1, 'sess2', '2024-02-01T00:00:00Z',"
            " 'store_put', 'T3', 'developer', 'nexus', 'impl-notes.md')"
        )
        conn.commit()
        conn.close()

        try:
            migrate_telemetry_rows(db_path, store)
        finally:
            db_path.unlink(missing_ok=True)

        _, kwargs = store.import_tier_write.call_args
        assert kwargs["agent"] == "developer"
        assert kwargs["project"] == "nexus"
        assert kwargs["target_title"] == "impl-notes.md"

    def test_empty_string_agent_becomes_none(self):
        """Empty-string agent in SQLite (legacy rows) must also become None."""
        store = MagicMock()
        store.import_tier_write.return_value = None
        store.import_relevance_row.return_value = None
        store.import_search_row.return_value = None
        store.import_nx_answer_run.return_value = None
        store.import_hook_failure.return_value = None
        store.import_frecency_row.return_value = None

        with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
            db_path = Path(f.name)

        conn = sqlite3.connect(str(db_path))
        conn.execute("""
            CREATE TABLE tier_writes (
                id INTEGER PRIMARY KEY, session_id TEXT, ts TEXT,
                tool TEXT, tier TEXT, agent TEXT, project TEXT, target_title TEXT
            )
        """)
        conn.execute(
            "INSERT INTO tier_writes VALUES (1, 'sess3', '2024-03-01T00:00:00Z',"
            " 'catalog_link', 'T3', '', '', '')"
        )
        conn.commit()
        conn.close()

        try:
            migrate_telemetry_rows(db_path, store)
        finally:
            db_path.unlink(missing_ok=True)

        _, kwargs = store.import_tier_write.call_args
        assert kwargs["agent"] is None, (
            "empty-string agent must also map to None (not '') via _nullable_str")


class TestNxAnswerRunPlanIdType:
    """REGRESSION (nexus-5gaj7): nx_answer_runs.plan_id is an INTEGER column (BIGINT
    on the service). The ETL stringified it via _nullable_str, so the service's
    ((Number) plan_id) cast threw ClassCastException and 180/182 rows failed import.
    The ETL must pass plan_id as an int (or None), never a string.
    """

    @staticmethod
    def _seed(db_path: Path, plan_id_sql: str) -> None:
        conn = sqlite3.connect(str(db_path))
        conn.execute("""
            CREATE TABLE nx_answer_runs (
                id INTEGER PRIMARY KEY AUTOINCREMENT, question TEXT NOT NULL,
                plan_id INTEGER, matched_confidence REAL, step_count INTEGER NOT NULL DEFAULT 0,
                final_text TEXT NOT NULL DEFAULT '', cost_usd REAL NOT NULL DEFAULT 0.0,
                duration_ms INTEGER NOT NULL DEFAULT 0, created_at TEXT NOT NULL
            )
        """)
        conn.execute(
            f"INSERT INTO nx_answer_runs (question, plan_id, created_at) "
            f"VALUES ('q', {plan_id_sql}, '2024-01-15T10:30:00Z')"
        )
        conn.commit()
        conn.close()

    @staticmethod
    def _mock_store() -> MagicMock:
        store = MagicMock()
        for m in ("import_tier_write", "import_relevance_row", "import_search_row",
                  "import_nx_answer_run", "import_hook_failure", "import_frecency_row"):
            getattr(store, m).return_value = None
        return store

    def test_integer_plan_id_passed_as_int_not_str(self):
        store = self._mock_store()
        with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
            db_path = Path(f.name)
        self._seed(db_path, "42")
        try:
            migrate_telemetry_rows(db_path, store)
        finally:
            db_path.unlink(missing_ok=True)

        assert store.import_nx_answer_run.call_count == 1
        _, kwargs = store.import_nx_answer_run.call_args
        assert kwargs["plan_id"] == 42
        assert isinstance(kwargs["plan_id"], int), (
            f"plan_id must be int, not {type(kwargs['plan_id']).__name__} — "
            "a str trips the service ((Number) plan_id) cast (nexus-5gaj7)"
        )

    def test_null_plan_id_passed_as_none(self):
        store = self._mock_store()
        with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
            db_path = Path(f.name)
        self._seed(db_path, "NULL")
        try:
            migrate_telemetry_rows(db_path, store)
        finally:
            db_path.unlink(missing_ok=True)

        _, kwargs = store.import_nx_answer_run.call_args
        assert kwargs["plan_id"] is None


# ── Unit tests: copy-not-move ──────────────────────────────────────────────────

class TestCopyNotMove:
    """The SQLite source must never be written by the ETL."""

    def test_source_db_unchanged_after_migrate(self):
        with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
            db_path = Path(f.name)

        conn = sqlite3.connect(str(db_path))
        conn.execute(
            "CREATE TABLE relevance_log "
            "(id INTEGER PRIMARY KEY, query TEXT, chunk_id TEXT, collection TEXT, "
            "action TEXT, session_id TEXT, timestamp TEXT)"
        )
        conn.execute(
            "INSERT INTO relevance_log VALUES "
            "(1, 'q', 'c', '', 'store_put', '', '2024-01-01T00:00:00Z')"
        )
        conn.commit()
        conn.close()

        mtime_before = db_path.stat().st_mtime

        store = MagicMock()
        store.import_relevance_row.return_value = None
        store.import_tier_write.return_value = None
        store.import_search_row.return_value = None
        store.import_nx_answer_run.return_value = None
        store.import_hook_failure.return_value = None
        store.import_frecency_row.return_value = None

        try:
            migrate_telemetry_rows(db_path, store)
        finally:
            pass

        mtime_after = db_path.stat().st_mtime
        db_path.unlink(missing_ok=True)

        assert mtime_before == mtime_after, (
            "SQLite source file must not be modified by the ETL (copy-not-move)")


# ── Unit tests: missing tables are silently skipped ───────────────────────────

class TestMissingTableTolerance:
    """ETL must tolerate a SQLite DB that has only some of the 6 tables."""

    def test_empty_db_runs_without_error(self):
        with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
            db_path = Path(f.name)

        conn = sqlite3.connect(str(db_path))
        conn.close()

        store = MagicMock()
        store.import_relevance_row.return_value = None
        store.import_tier_write.return_value = None
        store.import_search_row.return_value = None
        store.import_nx_answer_run.return_value = None
        store.import_hook_failure.return_value = None
        store.import_frecency_row.return_value = None

        try:
            result = migrate_telemetry_rows(db_path, store)
        finally:
            db_path.unlink(missing_ok=True)

        # All tables report 0 rows read/written
        for table in ("relevance_log", "search_telemetry", "tier_writes",
                      "nx_answer_runs", "hook_failures", "frecency"):
            assert result[table]["read"] == 0
            assert result[table]["written"] == 0

    def test_count_source_rows_on_partial_db(self):
        with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
            db_path = Path(f.name)

        conn = sqlite3.connect(str(db_path))
        conn.execute(
            "CREATE TABLE relevance_log "
            "(id INTEGER PRIMARY KEY, query TEXT, chunk_id TEXT, collection TEXT, "
            "action TEXT, session_id TEXT, timestamp TEXT)"
        )
        conn.execute(
            "INSERT INTO relevance_log VALUES "
            "(1, 'q', 'c', '', 'store_put', '', '2024-01-01T00:00:00Z')"
        )
        conn.commit()
        conn.close()

        try:
            counts = count_source_rows(db_path)
        finally:
            db_path.unlink(missing_ok=True)

        assert counts["relevance_log"] == 1
        # Tables absent in the DB report 0
        for table in ("search_telemetry", "tier_writes",
                      "nx_answer_runs", "hook_failures", "frecency"):
            assert counts[table] == 0
