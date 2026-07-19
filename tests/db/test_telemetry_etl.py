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


def _batch_mock_store() -> MagicMock:
    """Mock HttpTelemetryStore for the RDR-176 P3 batched ETL: import_rows_batch
    returns the batch size; per-table rows are inspectable via _batched_row."""
    store = MagicMock()
    store.import_rows_batch.side_effect = lambda table, rows: len(rows)
    return store


def _batched_row(store: MagicMock, table: str, index: int = 0) -> dict:
    """Return the *index*-th row dict that the ETL batched for *table*."""
    for c in store.import_rows_batch.call_args_list:
        t = c.args[0] if c.args else c.kwargs.get("table")
        rows = c.args[1] if len(c.args) > 1 else c.kwargs.get("rows")
        if t == table:
            return rows[index]
    raise AssertionError(f"no batch call for table {table!r}")

import pytest

from nexus.db.t2.telemetry_etl import (
    CONFLICT_KEY_COLUMNS,
    _nullable_str,
    _str_or_empty,
    conflict_key,
    count_source_rows,
    migrate_telemetry_rows,
    read_rows_for_fill,
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
        store = _batch_mock_store()

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
        row = _batched_row(store, "tier_writes")
        assert row["agent"] is None, (
            f"agent must be None (not '') for NULL SQLite value; got {row['agent']!r}")
        assert row["project"] is None, (
            f"project must be None (not '') for NULL SQLite value; got {kwargs['project']!r}")
        assert row["target_title"] is None, (
            f"target_title must be None (not '') for NULL SQLite value; got {kwargs['target_title']!r}")

    def test_populated_agent_passed_verbatim(self):
        """Non-NULL agent in SQLite must be passed verbatim to store."""
        store = _batch_mock_store()

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

        row = _batched_row(store, "tier_writes")
        assert row["agent"] == "developer"
        assert row["project"] == "nexus"
        assert row["target_title"] == "impl-notes.md"

    def test_empty_string_agent_becomes_none(self):
        """Empty-string agent in SQLite (legacy rows) must also become None."""
        store = _batch_mock_store()

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

        row = _batched_row(store, "tier_writes")
        assert row["agent"] is None, (
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
        return _batch_mock_store()

    def test_integer_plan_id_passed_as_int_not_str(self):
        store = self._mock_store()
        with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
            db_path = Path(f.name)
        self._seed(db_path, "42")
        try:
            migrate_telemetry_rows(db_path, store)
        finally:
            db_path.unlink(missing_ok=True)

        row = _batched_row(store, "nx_answer_runs")
        assert row["plan_id"] == 42
        assert isinstance(row["plan_id"], int), (
            f"plan_id must be int, not {type(row['plan_id']).__name__} — "
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

        row = _batched_row(store, "nx_answer_runs")
        assert row["plan_id"] is None


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


# ── Unit tests: verify-fill P3b (nexus-s3dd4.14) — CONFLICT_KEY_COLUMNS /
#    conflict_key / read_rows_for_fill ─────────────────────────────────────────

def _seed_full_telemetry_db(
    db_path: Path,
    *,
    relevance: list[dict] | None = None,
    search: list[dict] | None = None,
    tier: list[dict] | None = None,
    nx: list[dict] | None = None,
    hooks: list[dict] | None = None,
    frecency: list[dict] | None = None,
    plans: list[int] | None = None,
) -> None:
    """Seed all six telemetry tables (+ an optional `plans` FK table for
    nx_answer_runs' soft-dangler check) in one SQLite file."""
    conn = sqlite3.connect(str(db_path))
    conn.execute(
        "CREATE TABLE relevance_log (id INTEGER PRIMARY KEY, query TEXT, "
        "chunk_id TEXT, collection TEXT, action TEXT, session_id TEXT, timestamp TEXT)"
    )
    conn.execute(
        "CREATE TABLE search_telemetry (ts TEXT, query_hash TEXT, collection TEXT, "
        "raw_count INTEGER, kept_count INTEGER, top_distance REAL, threshold REAL)"
    )
    conn.execute(
        "CREATE TABLE tier_writes (id INTEGER PRIMARY KEY, session_id TEXT, ts TEXT, "
        "tool TEXT, tier TEXT, agent TEXT, project TEXT, target_title TEXT)"
    )
    conn.execute(
        "CREATE TABLE nx_answer_runs (id INTEGER PRIMARY KEY, question TEXT, "
        "plan_id INTEGER, matched_confidence REAL, step_count INTEGER, "
        "final_text TEXT, cost_usd REAL, duration_ms INTEGER, created_at TEXT)"
    )
    conn.execute(
        "CREATE TABLE hook_failures (id INTEGER PRIMARY KEY, doc_id TEXT, "
        "collection TEXT, hook_name TEXT, error TEXT, occurred_at TEXT, "
        "batch_doc_ids TEXT, is_batch INTEGER, chain TEXT)"
    )
    conn.execute(
        "CREATE TABLE frecency (chunk_id TEXT, embedded_at TEXT, ttl_days INTEGER, "
        "frecency_score REAL, miss_count INTEGER, last_hit_at TEXT)"
    )
    if plans is not None:
        conn.execute("CREATE TABLE plans (id INTEGER PRIMARY KEY)")
        conn.executemany("INSERT INTO plans (id) VALUES (?)", [(p,) for p in plans])

    for i, r in enumerate(relevance or []):
        conn.execute(
            "INSERT INTO relevance_log (id, query, chunk_id, collection, action, "
            "session_id, timestamp) VALUES (?,?,?,?,?,?,?)",
            (i + 1, r["query"], r["chunk_id"], r.get("collection", ""), r["action"],
             r.get("session_id", ""), r["timestamp"]),
        )
    for r in search or []:
        conn.execute(
            "INSERT INTO search_telemetry (ts, query_hash, collection, raw_count, "
            "kept_count, top_distance, threshold) VALUES (?,?,?,?,?,?,?)",
            (r["ts"], r["query_hash"], r["collection"], r.get("raw_count", 0),
             r.get("kept_count", 0), r.get("top_distance"), r.get("threshold")),
        )
    for i, r in enumerate(tier or []):
        conn.execute(
            "INSERT INTO tier_writes (id, session_id, ts, tool, tier, agent, "
            "project, target_title) VALUES (?,?,?,?,?,?,?,?)",
            (i + 1, r["session_id"], r["ts"], r["tool"], r["tier"], r.get("agent"),
             r.get("project"), r.get("target_title")),
        )
    for i, r in enumerate(nx or []):
        conn.execute(
            "INSERT INTO nx_answer_runs (id, question, plan_id, matched_confidence, "
            "step_count, final_text, cost_usd, duration_ms, created_at) "
            "VALUES (?,?,?,?,?,?,?,?,?)",
            (i + 1, r["question"], r.get("plan_id"), r.get("matched_confidence"),
             r.get("step_count", 0), r.get("final_text", ""), r.get("cost_usd"),
             r.get("duration_ms", 0), r["created_at"]),
        )
    for i, r in enumerate(hooks or []):
        conn.execute(
            "INSERT INTO hook_failures (id, doc_id, collection, hook_name, error, "
            "occurred_at, batch_doc_ids, is_batch, chain) VALUES (?,?,?,?,?,?,?,?,?)",
            (i + 1, r.get("doc_id", ""), r.get("collection", ""), r["hook_name"],
             r.get("error", ""), r["occurred_at"], r.get("batch_doc_ids"),
             int(r.get("is_batch", False)), r.get("chain")),
        )
    for r in frecency or []:
        conn.execute(
            "INSERT INTO frecency (chunk_id, embedded_at, ttl_days, frecency_score, "
            "miss_count, last_hit_at) VALUES (?,?,?,?,?,?)",
            (r["chunk_id"], r.get("embedded_at"), r.get("ttl_days", 30),
             r.get("frecency_score", 0.0), r.get("miss_count", 0), r.get("last_hit_at")),
        )
    conn.commit()
    conn.close()


class TestConflictKeyColumns:
    """Transcription check: CONFLICT_KEY_COLUMNS must match
    HttpTelemetryStore.probe_ids' docstring verbatim (itself transcribed
    from TelemetryRepository.probeIds' javadoc / the UNIQUE indexes in
    telemetry-001-baseline.xml — nexus-s3dd4.14 R1 note 1). A drift here
    would silently corrupt every verify-fill diff for that table."""

    def test_matches_probe_ids_docstring_exactly(self) -> None:
        assert CONFLICT_KEY_COLUMNS == {
            "relevance_log":    ("query", "chunk_id", "action", "session_id", "timestamp"),
            "search_telemetry": ("ts", "query_hash", "collection"),
            "tier_writes":      ("session_id", "ts", "tool", "tier"),
            "nx_answer_runs":   ("question", "created_at"),
            "hook_failures":    ("doc_id", "hook_name", "occurred_at"),
            "frecency":         ("chunk_id",),
        }

    def test_relevance_log_key_excludes_collection(self) -> None:
        # R1 note 1: relevance_log rows carry 'collection' but it is NOT
        # part of the conflict key.
        assert "collection" not in CONFLICT_KEY_COLUMNS["relevance_log"]


class TestConflictKey:
    def test_relevance_log_extracts_key_columns_only(self) -> None:
        row = {
            "query": "q1", "chunk_id": "c1", "collection": "code__x",
            "action": "store_put", "session_id": "s1", "timestamp": "2024-01-01T00:00:00Z",
        }
        assert conflict_key("relevance_log", row) == (
            "q1", "c1", "store_put", "s1", "2024-01-01T00:00:00Z",
        )

    def test_frecency_single_column_key(self) -> None:
        row = {"chunk_id": "chunk-42", "embedded_at": None, "ttl_days": 30,
               "frecency_score": 0.5, "miss_count": 0, "last_hit_at": None}
        assert conflict_key("frecency", row) == ("chunk-42",)


class TestReadRowsForFill:
    """verify-fill P3b (nexus-s3dd4.14): read_rows_for_fill must return the
    SAME transformed shape _run_batched sends through import_rows_batch —
    the conflict-key diff and the eventual fill payload must agree."""

    def test_relevance_log_rows_match_build_shape(self, tmp_path: Path) -> None:
        db_path = tmp_path / "t2.db"
        _seed_full_telemetry_db(db_path, relevance=[
            {"query": "q1", "chunk_id": "c1", "action": "store_put",
             "session_id": "", "timestamp": "2024-01-01T00:00:00Z"},
        ])
        conn = sqlite3.connect(str(db_path))
        try:
            rows = read_rows_for_fill(conn, "relevance_log")
        finally:
            conn.close()

        assert rows == [{
            "query": "q1", "chunk_id": "c1", "collection": "",
            "action": "store_put", "session_id": "", "timestamp": "2024-01-01T00:00:00Z",
        }]
        assert conflict_key("relevance_log", rows[0]) == (
            "q1", "c1", "store_put", "", "2024-01-01T00:00:00Z",
        )

    def test_nx_answer_runs_plan_id_is_int_and_dangler_still_included(
        self, tmp_path: Path,
    ) -> None:
        db_path = tmp_path / "t2.db"
        _seed_full_telemetry_db(
            db_path,
            nx=[
                {"question": "q-live", "plan_id": 1, "created_at": "2024-01-01T00:00:00Z"},
                {"question": "q-dangling", "plan_id": 999, "created_at": "2024-01-02T00:00:00Z"},
            ],
            plans=[1],  # plan_id=999 has no matching plans row -> soft dangler
        )
        conn = sqlite3.connect(str(db_path))
        try:
            rows = read_rows_for_fill(conn, "nx_answer_runs")
        finally:
            conn.close()

        assert len(rows) == 2  # the dangler is NOT dropped, only flagged
        assert rows[0]["plan_id"] == 1
        assert isinstance(rows[0]["plan_id"], int)
        assert rows[1]["plan_id"] == 999
        assert conflict_key("nx_answer_runs", rows[1]) == (
            "q-dangling", "2024-01-02T00:00:00Z",
        )

    def test_hook_failures_unparseable_timestamp_is_skipped_not_crashed(
        self, tmp_path: Path,
    ) -> None:
        db_path = tmp_path / "t2.db"
        _seed_full_telemetry_db(db_path, hooks=[
            {"doc_id": "d1", "hook_name": "h1", "occurred_at": "2024-01-01T00:00:00Z"},
            {"doc_id": "d2", "hook_name": "h2", "occurred_at": "not-a-timestamp"},
        ])
        conn = sqlite3.connect(str(db_path))
        try:
            rows = read_rows_for_fill(conn, "hook_failures")
        finally:
            conn.close()

        # the corrupt row is skipped, not raised -- the good row still lands
        assert len(rows) == 1
        assert rows[0]["doc_id"] == "d1"

    def test_hook_failures_space_form_timestamp_normalized(self, tmp_path: Path) -> None:
        db_path = tmp_path / "t2.db"
        _seed_full_telemetry_db(db_path, hooks=[
            {"doc_id": "d1", "hook_name": "h1", "occurred_at": "2026-04-23 10:47:54"},
        ])
        conn = sqlite3.connect(str(db_path))
        try:
            rows = read_rows_for_fill(conn, "hook_failures")
        finally:
            conn.close()

        assert rows[0]["occurred_at"] == "2026-04-23T10:47:54+00:00"

    def test_frecency_rows_and_key(self, tmp_path: Path) -> None:
        db_path = tmp_path / "t2.db"
        _seed_full_telemetry_db(db_path, frecency=[
            {"chunk_id": "chunk-1", "frecency_score": 1.5, "miss_count": 2},
        ])
        conn = sqlite3.connect(str(db_path))
        try:
            rows = read_rows_for_fill(conn, "frecency")
        finally:
            conn.close()

        assert len(rows) == 1
        assert conflict_key("frecency", rows[0]) == ("chunk-1",)

    def test_empty_table_returns_empty_list(self, tmp_path: Path) -> None:
        db_path = tmp_path / "t2.db"
        _seed_full_telemetry_db(db_path)
        conn = sqlite3.connect(str(db_path))
        try:
            rows = read_rows_for_fill(conn, "search_telemetry")
        finally:
            conn.close()
        assert rows == []


# ── Per-table split (RDR-180 nexus-jxizy.10.7) ─────────────────────────────────
# Each new public entry point migrates ONLY its own table; the other five
# tables' rows must never reach the store, even when the source DB has all
# six tables populated (proven via the store's per-kind batch calls).


def _seeded_all_six_db(tmp_path: Path) -> Path:
    db_path = tmp_path / "t2.db"
    _seed_full_telemetry_db(
        db_path,
        relevance=[{"query": "q1", "chunk_id": "c1", "action": "store_put",
                     "timestamp": "2024-01-01T00:00:00Z"}],
        search=[{"ts": "2024-01-01T00:00:00Z", "query_hash": "h1", "collection": "c"}],
        tier=[{"session_id": "s1", "ts": "2024-01-01T00:00:00Z", "tool": "t", "tier": "T2"}],
        nx=[{"question": "q", "created_at": "2024-01-01T00:00:00Z"}],
        hooks=[{"hook_name": "h", "occurred_at": "2024-01-01T00:00:00Z"}],
        frecency=[{"chunk_id": "chunk-1"}],
    )
    return db_path


class TestPerTableSplit:
    def test_migrate_relevance_log_writes_only_relevance_log(self, tmp_path: Path) -> None:
        from nexus.db.t2.telemetry_etl import migrate_relevance_log

        db_path = _seeded_all_six_db(tmp_path)
        store = _batch_mock_store()
        result = migrate_relevance_log(db_path, store)

        assert result["read"] == 1
        assert result["written"] == 1
        tables = {c.args[0] for c in store.import_rows_batch.call_args_list}
        assert tables == {"relevance_log"}, f"only relevance_log must be sent, got {tables}"

    def test_migrate_search_telemetry_writes_only_search_telemetry(self, tmp_path: Path) -> None:
        from nexus.db.t2.telemetry_etl import migrate_search_telemetry

        db_path = _seeded_all_six_db(tmp_path)
        store = _batch_mock_store()
        result = migrate_search_telemetry(db_path, store)

        assert result["read"] == 1
        assert result["written"] == 1
        tables = {c.args[0] for c in store.import_rows_batch.call_args_list}
        assert tables == {"search_telemetry"}

    def test_migrate_tier_writes_writes_only_tier_writes(self, tmp_path: Path) -> None:
        from nexus.db.t2.telemetry_etl import migrate_tier_writes

        db_path = _seeded_all_six_db(tmp_path)
        store = _batch_mock_store()
        result = migrate_tier_writes(db_path, store)

        assert result["read"] == 1
        assert result["written"] == 1
        tables = {c.args[0] for c in store.import_rows_batch.call_args_list}
        assert tables == {"tier_writes"}

    def test_migrate_nx_answer_runs_writes_only_nx_answer_runs(self, tmp_path: Path) -> None:
        from nexus.db.t2.telemetry_etl import migrate_nx_answer_runs

        db_path = _seeded_all_six_db(tmp_path)
        store = _batch_mock_store()
        result = migrate_nx_answer_runs(db_path, store)

        assert result["read"] == 1
        assert result["written"] == 1
        tables = {c.args[0] for c in store.import_rows_batch.call_args_list}
        assert tables == {"nx_answer_runs"}

    def test_migrate_hook_failures_writes_only_hook_failures(self, tmp_path: Path) -> None:
        from nexus.db.t2.telemetry_etl import migrate_hook_failures

        db_path = _seeded_all_six_db(tmp_path)
        store = _batch_mock_store()
        result = migrate_hook_failures(db_path, store)

        assert result["read"] == 1
        assert result["written"] == 1
        tables = {c.args[0] for c in store.import_rows_batch.call_args_list}
        assert tables == {"hook_failures"}

    def test_migrate_frecency_writes_only_frecency(self, tmp_path: Path) -> None:
        from nexus.db.t2.telemetry_etl import migrate_frecency

        db_path = _seeded_all_six_db(tmp_path)
        store = _batch_mock_store()
        result = migrate_frecency(db_path, store)

        assert result["read"] == 1
        assert result["written"] == 1
        tables = {c.args[0] for c in store.import_rows_batch.call_args_list}
        assert tables == {"frecency"}

    def test_migrate_telemetry_without_chash_excludes_relevance_and_frecency(
        self, tmp_path: Path,
    ) -> None:
        """The guided-path entry point: search_telemetry/tier_writes/
        nx_answer_runs/hook_failures land; the chash-bearing relevance_log
        and frecency tables are NEVER written."""
        from nexus.db.t2.telemetry_etl import migrate_telemetry_without_chash

        db_path = _seeded_all_six_db(tmp_path)
        store = _batch_mock_store()
        result = migrate_telemetry_without_chash(db_path, store)

        assert set(result) == {
            "search_telemetry", "tier_writes", "nx_answer_runs", "hook_failures",
        }
        for table in result.values():
            assert table["written"] == 1
        tables = {c.args[0] for c in store.import_rows_batch.call_args_list}
        assert "relevance_log" not in tables and "frecency" not in tables, (
            f"relevance_log/frecency must NEVER be written by the guided-path "
            f"non-chash entry point, got batch tables {tables}"
        )
        assert tables == {"search_telemetry", "tier_writes", "nx_answer_runs", "hook_failures"}

    def test_migrate_telemetry_rows_composition_matches_monolithic_result(
        self, tmp_path: Path,
    ) -> None:
        """The thin composition must still migrate all six tables (byte-
        identical behavior for existing callers)."""
        db_path = _seeded_all_six_db(tmp_path)
        store = _batch_mock_store()
        result = migrate_telemetry_rows(db_path, store)

        assert set(result) == {
            "relevance_log", "search_telemetry", "tier_writes",
            "nx_answer_runs", "hook_failures", "frecency",
        }
        for table in result.values():
            assert table["read"] == 1
            assert table["written"] == 1
        tables = {c.args[0] for c in store.import_rows_batch.call_args_list}
        assert tables == {
            "relevance_log", "search_telemetry", "tier_writes",
            "nx_answer_runs", "hook_failures", "frecency",
        }
