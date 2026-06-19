# SPDX-License-Identifier: AGPL-3.0-or-later
"""Unit tests for plan_etl (bead nexus-gmiaf.11, RDR-152 P2.1).

Tests:
  - _transform_row: full field mapping with all normalization rules
  - migrate_plan_rows: happy path, skip-on-failure, idempotent re-run
  - count_source_rows: read-only row count
"""
from __future__ import annotations

import sqlite3
import tempfile
from pathlib import Path
from typing import Any

import pytest

from nexus.db.t2.plan_etl import _transform_row, migrate_plan_rows, count_source_rows

# ── Helpers ───────────────────────────────────────────────────────────────────


def _make_plans_db(path: Path, rows: list[dict[str, Any]]) -> None:
    """Create a minimal SQLite plans table and insert rows."""
    conn = sqlite3.connect(str(path))
    conn.execute("""
        CREATE TABLE IF NOT EXISTS plans (
            id              INTEGER PRIMARY KEY,
            project         TEXT NOT NULL DEFAULT '',
            query           TEXT NOT NULL,
            plan_json       TEXT NOT NULL,
            outcome         TEXT DEFAULT 'success',
            tags            TEXT DEFAULT '',
            created_at      TEXT NOT NULL,
            ttl             INTEGER,
            name            TEXT,
            verb            TEXT,
            scope           TEXT,
            dimensions      TEXT,
            default_bindings TEXT,
            parent_dims     TEXT,
            use_count       INTEGER NOT NULL DEFAULT 0,
            last_used       TEXT,
            match_count     INTEGER NOT NULL DEFAULT 0,
            match_conf_sum  REAL NOT NULL DEFAULT 0.0,
            success_count   INTEGER NOT NULL DEFAULT 0,
            failure_count   INTEGER NOT NULL DEFAULT 0,
            scope_tags      TEXT NOT NULL DEFAULT '',
            match_text      TEXT NOT NULL DEFAULT '',
            disabled_at     TEXT
        )
    """)
    for r in rows:
        conn.execute(
            """
            INSERT INTO plans (
                project, query, plan_json, outcome, tags, created_at, ttl,
                name, verb, scope, dimensions, default_bindings, parent_dims,
                use_count, last_used, match_count, match_conf_sum,
                success_count, failure_count, scope_tags, match_text, disabled_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                r.get("project", ""),
                r["query"],
                r["plan_json"],
                r.get("outcome", "success"),
                r.get("tags", ""),
                r.get("created_at", "2026-01-01T00:00:00Z"),
                r.get("ttl"),
                r.get("name"),
                r.get("verb"),
                r.get("scope"),
                r.get("dimensions"),
                r.get("default_bindings"),
                r.get("parent_dims"),
                r.get("use_count", 0),
                r.get("last_used"),
                r.get("match_count", 0),
                r.get("match_conf_sum", 0.0),
                r.get("success_count", 0),
                r.get("failure_count", 0),
                r.get("scope_tags", ""),
                r.get("match_text", ""),
                r.get("disabled_at"),
            ),
        )
    conn.commit()
    conn.close()


class _DuckPlanStore:
    """Minimal duck-typed store capturing import_plan calls."""

    def __init__(self) -> None:
        self._calls: list[dict[str, Any]] = []
        self._by_key: dict[tuple[str, str], int] = {}  # (project, query) -> id
        self._next_id = 1

    def import_plan(self, **kwargs: Any) -> int:
        key = (kwargs.get("project", ""), kwargs["query"])
        if key in self._by_key:
            return self._by_key[key]
        pid = self._next_id
        self._next_id += 1
        self._calls.append(kwargs)
        self._by_key[key] = pid
        return pid


class _FailingPlanStore:
    """Store that raises on every call (simulates service unavailable)."""

    def import_plan(self, **kwargs: Any) -> int:
        raise RuntimeError("service unavailable")


# ── _transform_row tests ──────────────────────────────────────────────────────

class TestTransformRow:
    def test_basic_fields_preserved(self):
        row = {
            "project": "proj",
            "query": "How to research RDRs",
            "plan_json": '{"steps":[]}',
            "outcome": "success",
            "tags": "research,rdr",
            "created_at": "2026-05-01T10:00:00Z",
            "ttl": 30,
            "name": "research-rdr",
            "verb": "research",
            "scope": "global",
            "dimensions": '{"verb":"research"}',
            "default_bindings": None,
            "parent_dims": None,
            "use_count": 7,
            "last_used": "2026-05-10T08:00:00Z",
            "match_count": 15,
            "match_conf_sum": 8.5,
            "success_count": 7,
            "failure_count": 0,
            "scope_tags": "knowledge__nexus",
            "match_text": "How to research RDRs. research research-rdr scope global",
            "disabled_at": None,
        }
        t = _transform_row(row)
        assert t["project"] == "proj"
        assert t["query"] == "How to research RDRs"
        assert t["plan_json"] == '{"steps":[]}'
        assert t["outcome"] == "success"
        assert t["tags"] == "research,rdr"
        assert t["created_at"] == "2026-05-01T10:00:00Z"
        assert t["use_count"] == 7
        assert t["match_count"] == 15
        assert abs(t["match_conf_sum"] - 8.5) < 1e-9
        assert t["success_count"] == 7
        assert t["failure_count"] == 0
        assert t["scope_tags"] == "knowledge__nexus"
        assert t["last_used"] == "2026-05-10T08:00:00Z"

    def test_id_not_in_output(self):
        row = {"query": "Q", "plan_json": "{}", "created_at": "2026-01-01T00:00:00Z"}
        t = _transform_row(row)
        assert "id" not in t

    def test_none_project_normalised_to_empty(self):
        row = {"query": "Q", "plan_json": "{}", "created_at": "2026-01-01T00:00:00Z",
               "project": None}
        t = _transform_row(row)
        assert t["project"] == ""

    def test_none_tags_normalised_to_empty(self):
        row = {"query": "Q", "plan_json": "{}", "created_at": "2026-01-01T00:00:00Z",
               "tags": None}
        t = _transform_row(row)
        assert t["tags"] == ""

    def test_none_scope_tags_normalised_to_empty(self):
        row = {"query": "Q", "plan_json": "{}", "created_at": "2026-01-01T00:00:00Z",
               "scope_tags": None}
        t = _transform_row(row)
        assert t["scope_tags"] == ""

    def test_empty_last_used_normalised_to_none(self):
        row = {"query": "Q", "plan_json": "{}", "created_at": "2026-01-01T00:00:00Z",
               "last_used": ""}
        t = _transform_row(row)
        assert t["last_used"] is None

    def test_empty_disabled_at_normalised_to_none(self):
        row = {"query": "Q", "plan_json": "{}", "created_at": "2026-01-01T00:00:00Z",
               "disabled_at": ""}
        t = _transform_row(row)
        assert t["disabled_at"] is None

    def test_non_empty_disabled_at_preserved(self):
        row = {"query": "Q", "plan_json": "{}", "created_at": "2026-01-01T00:00:00Z",
               "disabled_at": "2026-06-01T12:00:00Z"}
        t = _transform_row(row)
        assert t["disabled_at"] == "2026-06-01T12:00:00Z"

    def test_none_outcome_defaults_success(self):
        row = {"query": "Q", "plan_json": "{}", "created_at": "2026-01-01T00:00:00Z",
               "outcome": None}
        t = _transform_row(row)
        assert t["outcome"] == "success"

    def test_none_counters_default_to_zero(self):
        row = {"query": "Q", "plan_json": "{}", "created_at": "2026-01-01T00:00:00Z",
               "use_count": None, "match_count": None, "success_count": None,
               "failure_count": None, "match_conf_sum": None}
        t = _transform_row(row)
        assert t["use_count"] == 0
        assert t["match_count"] == 0
        assert t["success_count"] == 0
        assert t["failure_count"] == 0
        assert t["match_conf_sum"] == 0.0

    def test_missing_created_at_falls_back_to_epoch(self):
        row = {"query": "Q", "plan_json": "{}"}
        t = _transform_row(row)
        assert t["created_at"] == "1970-01-01T00:00:00Z"


# ── count_source_rows tests ───────────────────────────────────────────────────

class TestCountSourceRows:
    def test_counts_correctly(self):
        with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
            path = Path(f.name)
        _make_plans_db(path, [
            {"query": "q1", "plan_json": "{}", "created_at": "2026-01-01T00:00:00Z"},
            {"query": "q2", "plan_json": "{}", "created_at": "2026-01-01T00:00:00Z"},
        ])
        assert count_source_rows(path) == 2
        path.unlink(missing_ok=True)

    def test_empty_db_returns_zero(self):
        with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
            path = Path(f.name)
        _make_plans_db(path, [])
        assert count_source_rows(path) == 0
        path.unlink(missing_ok=True)

    def test_missing_db_raises_runtime_error(self):
        path = Path("/tmp/nonexistent_plans_db_xyz.db")
        with pytest.raises(RuntimeError, match="Cannot open SQLite source"):
            count_source_rows(path)


# ── migrate_plan_rows tests ───────────────────────────────────────────────────

class TestMigratePlanRows:
    def test_migrates_all_rows(self):
        with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
            path = Path(f.name)
        _make_plans_db(path, [
            {
                "project": "proj", "query": "Plan 1", "plan_json": '{"a":1}',
                "created_at": "2026-05-01T00:00:00Z", "use_count": 3,
                "match_count": 10, "match_conf_sum": 5.0,
            },
            {
                "project": "proj", "query": "Plan 2", "plan_json": '{"b":2}',
                "created_at": "2026-05-02T00:00:00Z", "success_count": 1,
            },
        ])
        store = _DuckPlanStore()
        result = migrate_plan_rows(path, store)
        assert result == {"read": 2, "written": 2}
        assert len(store._calls) == 2
        path.unlink(missing_ok=True)

    def test_fidelity_counters_preserved(self):
        with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
            path = Path(f.name)
        _make_plans_db(path, [{
            "project": "fid-proj", "query": "Fidelity plan",
            "plan_json": '{"etl":true}', "created_at": "2025-06-01T10:00:00Z",
            "use_count": 42, "last_used": "2025-06-05T12:00:00Z",
            "match_count": 99, "match_conf_sum": 12.5,
            "success_count": 40, "failure_count": 2,
        }])
        store = _DuckPlanStore()
        result = migrate_plan_rows(path, store)
        assert result == {"read": 1, "written": 1}
        call = store._calls[0]
        assert call["use_count"] == 42
        assert call["match_count"] == 99
        assert abs(call["match_conf_sum"] - 12.5) < 1e-9
        assert call["success_count"] == 40
        assert call["failure_count"] == 2
        assert call["last_used"] == "2025-06-05T12:00:00Z"
        assert call["created_at"] == "2025-06-01T10:00:00Z"
        path.unlink(missing_ok=True)

    def test_idempotent_reruns(self):
        with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
            path = Path(f.name)
        _make_plans_db(path, [
            {"query": "Idem 1", "plan_json": "{}", "created_at": "2026-01-01T00:00:00Z"},
        ])
        store = _DuckPlanStore()
        r1 = migrate_plan_rows(path, store)
        r2 = migrate_plan_rows(path, store)
        assert r1 == {"read": 1, "written": 1}
        assert r2 == {"read": 1, "written": 1}
        # DuckStore deduplicates by (project, query), so total calls is 1 (first run)
        assert len(store._calls) == 1
        path.unlink(missing_ok=True)

    def test_source_never_modified(self):
        with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
            path = Path(f.name)
        _make_plans_db(path, [
            {"query": "Unchanged", "plan_json": "{}", "created_at": "2026-01-01T00:00:00Z"},
        ])
        import os
        mtime_before = os.stat(str(path)).st_mtime_ns
        store = _DuckPlanStore()
        migrate_plan_rows(path, store)
        mtime_after = os.stat(str(path)).st_mtime_ns
        assert mtime_after == mtime_before, "source SQLite file must not be modified"
        path.unlink(missing_ok=True)

    def test_skip_failing_rows_continues(self):
        with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
            path = Path(f.name)
        _make_plans_db(path, [
            {"query": "Plan A", "plan_json": "{}", "created_at": "2026-01-01T00:00:00Z"},
            {"query": "Plan B", "plan_json": "{}", "created_at": "2026-01-01T00:00:00Z"},
        ])
        result = migrate_plan_rows(path, _FailingPlanStore())
        assert result["read"] == 2
        assert result["written"] == 0
        path.unlink(missing_ok=True)

    def test_empty_db_returns_zero(self):
        with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
            path = Path(f.name)
        _make_plans_db(path, [])
        store = _DuckPlanStore()
        result = migrate_plan_rows(path, store)
        assert result == {"read": 0, "written": 0}
        path.unlink(missing_ok=True)

    def test_disabled_at_propagated(self):
        with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
            path = Path(f.name)
        _make_plans_db(path, [{
            "query": "Disabled plan", "plan_json": "{}",
            "created_at": "2026-01-01T00:00:00Z",
            "disabled_at": "2026-06-01T12:00:00Z",
        }])
        store = _DuckPlanStore()
        migrate_plan_rows(path, store)
        call = store._calls[0]
        assert call["disabled_at"] == "2026-06-01T12:00:00Z"
        path.unlink(missing_ok=True)
