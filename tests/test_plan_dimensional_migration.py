# SPDX-License-Identifier: AGPL-3.0-or-later
"""TDD tests for RDR-078 Phase 4c — plan dimensional identity migration.

Covers:
- new columns added to ``plans`` (verb, scope, dimensions, default_bindings,
  parent_dims, name, use_count, last_used, match_count, match_conf_sum,
  success_count, failure_count)
- new indexes (idx_plans_verb, idx_plans_scope, idx_plans_verb_scope, and
  the partial UNIQUE index idx_plans_project_dimensions)
- idempotent re-apply
- RDR-042 backward compatibility (legacy rows load with NULL dimensional
  fields)
- canonical JSON dedup helper (``canonical_dimensions_json``)

SC-12 (migration idempotency) + SC-18 (canonical JSON dedup).
"""
from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest


# ── Fixtures ────────────────────────────────────────────────────────────────


@pytest.fixture()
def fresh_db(tmp_path: Path) -> sqlite3.Connection:
    """Fresh plans table built from the current base schema (pre-migration).

    Uses the historical DDL — not the post-migration schema — so we can
    exercise the migration's ALTER TABLE path end-to-end.
    """
    conn = sqlite3.connect(str(tmp_path / "plans.db"))
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS plans (
            id         INTEGER PRIMARY KEY,
            project    TEXT NOT NULL DEFAULT '',
            query      TEXT NOT NULL,
            plan_json  TEXT NOT NULL,
            outcome    TEXT DEFAULT 'success',
            tags       TEXT DEFAULT '',
            created_at TEXT NOT NULL,
            ttl        INTEGER
        );
        """
    )
    conn.commit()
    return conn


def _columns(conn: sqlite3.Connection, table: str) -> set[str]:
    return {row[1] for row in conn.execute(f"PRAGMA table_info({table})").fetchall()}


def _indexes(conn: sqlite3.Connection, table: str) -> set[str]:
    return {
        row[1]
        for row in conn.execute(f"PRAGMA index_list({table})").fetchall()
    }


# ── Column existence ────────────────────────────────────────────────────────


def test_migration_adds_dimensional_columns(fresh_db: sqlite3.Connection) -> None:
    from nexus.db.migrations import _add_plan_dimensional_identity

    _add_plan_dimensional_identity(fresh_db)

    cols = _columns(fresh_db, "plans")
    for col in (
        "verb",
        "scope",
        "dimensions",
        "default_bindings",
        "parent_dims",
        "name",
        "use_count",
        "last_used",
        "match_count",
        "match_conf_sum",
        "success_count",
        "failure_count",
    ):
        assert col in cols, f"missing column {col!r}"


def test_migration_adds_expected_indexes(fresh_db: sqlite3.Connection) -> None:
    from nexus.db.migrations import _add_plan_dimensional_identity

    _add_plan_dimensional_identity(fresh_db)

    indexes = _indexes(fresh_db, "plans")
    for idx in (
        "idx_plans_verb",
        "idx_plans_scope",
        "idx_plans_verb_scope",
        "idx_plans_project_dimensions",
    ):
        assert idx in indexes, f"missing index {idx!r}"


# ── Idempotency (SC-12) ─────────────────────────────────────────────────────


def test_migration_idempotent(fresh_db: sqlite3.Connection) -> None:
    """Applying twice must not raise or duplicate schema artefacts."""
    from nexus.db.migrations import _add_plan_dimensional_identity

    _add_plan_dimensional_identity(fresh_db)
    cols_before = _columns(fresh_db, "plans")
    idx_before = _indexes(fresh_db, "plans")

    _add_plan_dimensional_identity(fresh_db)  # must not raise
    cols_after = _columns(fresh_db, "plans")
    idx_after = _indexes(fresh_db, "plans")

    assert cols_after == cols_before
    assert idx_after == idx_before


def test_migration_noop_when_already_migrated(
    fresh_db: sqlite3.Connection,
) -> None:
    """Fresh DB whose schema already has the columns — migration is a no-op."""
    from nexus.db.migrations import _add_plan_dimensional_identity

    # Simulate a DB that already has `verb` column (partial pre-existing).
    fresh_db.execute("ALTER TABLE plans ADD COLUMN verb TEXT")
    fresh_db.commit()

    _add_plan_dimensional_identity(fresh_db)  # must not re-add verb

    cols = _columns(fresh_db, "plans")
    assert "verb" in cols
    assert "scope" in cols  # still adds missing sibling columns


# ── RDR-042 backward compat ─────────────────────────────────────────────────


def test_legacy_rows_load(fresh_db: sqlite3.Connection) -> None:
    """RDR-042 callers that don't pass dimensional fields still work.

    Pre-migration row inserted with only legacy columns reads back with
    NULL for every new dimensional field and zero for every metrics counter.
    """
    from nexus.db.migrations import _add_plan_dimensional_identity

    fresh_db.execute(
        """
        INSERT INTO plans (project, query, plan_json, outcome, tags, created_at)
        VALUES (?, ?, ?, ?, ?, ?)
        """,
        ("proj", "legacy query", "{}", "success", "", "2026-01-01T00:00:00Z"),
    )
    fresh_db.commit()

    _add_plan_dimensional_identity(fresh_db)

    row = fresh_db.execute(
        "SELECT verb, scope, dimensions, default_bindings, parent_dims, name, "
        "use_count, last_used, match_count, match_conf_sum, success_count, "
        "failure_count FROM plans WHERE query = 'legacy query'"
    ).fetchone()
    assert row is not None
    (
        verb,
        scope,
        dimensions,
        default_bindings,
        parent_dims,
        name,
        use_count,
        last_used,
        match_count,
        match_conf_sum,
        success_count,
        failure_count,
    ) = row
    assert verb is None
    assert scope is None
    assert dimensions is None
    assert default_bindings is None
    assert parent_dims is None
    assert name is None
    assert use_count == 0
    assert last_used is None
    assert match_count == 0
    assert match_conf_sum == 0.0
    assert success_count == 0
    assert failure_count == 0


# ── Unique dimensions constraint ────────────────────────────────────────────


def test_unique_dimensions_constraint(fresh_db: sqlite3.Connection) -> None:
    """``UNIQUE (project, dimensions)`` forbids duplicate rows.

    Partial index — applies only when ``dimensions IS NOT NULL``.
    """
    from nexus.db.migrations import _add_plan_dimensional_identity

    _add_plan_dimensional_identity(fresh_db)

    fresh_db.execute(
        """
        INSERT INTO plans (project, query, plan_json, created_at, dimensions)
        VALUES (?, ?, ?, ?, ?)
        """,
        ("proj", "q1", "{}", "2026-04-15T00:00:00Z", '{"scope":"g","verb":"r"}'),
    )
    fresh_db.commit()

    with pytest.raises(sqlite3.IntegrityError):
        fresh_db.execute(
            """
            INSERT INTO plans (project, query, plan_json, created_at, dimensions)
            VALUES (?, ?, ?, ?, ?)
            """,
            (
                "proj",
                "q2",
                "{}",
                "2026-04-15T00:00:01Z",
                '{"scope":"g","verb":"r"}',
            ),
        )


def test_unique_dimensions_allows_null(fresh_db: sqlite3.Connection) -> None:
    """Partial index skips NULL — many RDR-042 legacy rows may coexist."""
    from nexus.db.migrations import _add_plan_dimensional_identity

    _add_plan_dimensional_identity(fresh_db)

    for i in range(3):
        fresh_db.execute(
            """
            INSERT INTO plans (project, query, plan_json, created_at)
            VALUES (?, ?, ?, ?)
            """,
            ("proj", f"legacy {i}", "{}", f"2026-04-{15 + i}T00:00:00Z"),
        )
    fresh_db.commit()  # must not raise

    count = fresh_db.execute(
        "SELECT COUNT(*) FROM plans WHERE dimensions IS NULL"
    ).fetchone()[0]
    assert count == 3


def test_unique_dimensions_permits_different_projects(
    fresh_db: sqlite3.Connection,
) -> None:
    """Same dimensions across different projects must be allowed."""
    from nexus.db.migrations import _add_plan_dimensional_identity

    _add_plan_dimensional_identity(fresh_db)

    dims = '{"scope":"g","verb":"r"}'
    fresh_db.execute(
        "INSERT INTO plans (project, query, plan_json, created_at, dimensions) "
        "VALUES (?, ?, ?, ?, ?)",
        ("projA", "q", "{}", "2026-04-15T00:00:00Z", dims),
    )
    fresh_db.execute(
        "INSERT INTO plans (project, query, plan_json, created_at, dimensions) "
        "VALUES (?, ?, ?, ?, ?)",
        ("projB", "q", "{}", "2026-04-15T00:00:01Z", dims),
    )
    fresh_db.commit()  # must not raise


# ── Canonical JSON helper (SC-18) ───────────────────────────────────────────


class TestCanonicalDimensionsJson:
    def test_stable_across_insertion_order(self) -> None:
        from nexus.plans.schema import canonical_dimensions_json

        a = canonical_dimensions_json({"verb": "r", "scope": "g"})
        b = canonical_dimensions_json({"scope": "g", "verb": "r"})
        assert a == b

    def test_lowercases_keys_and_values(self) -> None:
        from nexus.plans.schema import canonical_dimensions_json

        out = canonical_dimensions_json({"Verb": "Retrieve", "Scope": "Global"})
        assert out == '{"scope":"global","verb":"retrieve"}'

    def test_no_whitespace(self) -> None:
        from nexus.plans.schema import canonical_dimensions_json

        out = canonical_dimensions_json({"verb": "r", "scope": "g"})
        assert " " not in out

    def test_empty_dict(self) -> None:
        from nexus.plans.schema import canonical_dimensions_json

        assert canonical_dimensions_json({}) == "{}"

    def test_non_string_values_preserved(self) -> None:
        """Non-string values (int/bool) stay typed — only string values lowercase."""
        from nexus.plans.schema import canonical_dimensions_json

        out = canonical_dimensions_json({"depth": 3, "strict": True})
        assert out == '{"depth":3,"strict":true}'


# ── Metrics defaults ────────────────────────────────────────────────────────


def test_metrics_default_zero(fresh_db: sqlite3.Connection) -> None:
    """Fresh rows inserted after migration get zero counters by default."""
    from nexus.db.migrations import _add_plan_dimensional_identity

    _add_plan_dimensional_identity(fresh_db)

    fresh_db.execute(
        "INSERT INTO plans (project, query, plan_json, created_at) "
        "VALUES (?, ?, ?, ?)",
        ("proj", "fresh", "{}", "2026-04-15T00:00:00Z"),
    )
    fresh_db.commit()

    row = fresh_db.execute(
        "SELECT use_count, match_count, match_conf_sum, success_count, failure_count "
        "FROM plans WHERE query = 'fresh'"
    ).fetchone()
    assert row == (0, 0, 0.0, 0, 0)
