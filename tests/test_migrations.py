# SPDX-License-Identifier: AGPL-3.0-or-later
"""TDD tests for src/nexus/db/migrations.py — migration registry core.

Red phase: these tests define the contract for the migration registry.
They will fail until migrations.py is implemented (nexus-6cn).
"""
from __future__ import annotations

import json
import sqlite3
import threading
from pathlib import Path
from unittest.mock import MagicMock

import pytest


# ── _parse_version tests ────────────────────────────────────────────────────


class TestParseVersion:
    def test_normal_version(self) -> None:
        from nexus.db.migrations import _parse_version

        assert _parse_version("4.1.2") == (4, 1, 2)

    def test_zero_version(self) -> None:
        from nexus.db.migrations import _parse_version

        assert _parse_version("0.0.0") == (0, 0, 0)

    def test_prerelease_fallback(self) -> None:
        from nexus.db.migrations import _parse_version

        assert _parse_version("1.0.0rc1") == (0, 0, 0)

    def test_empty_string_fallback(self) -> None:
        from nexus.db.migrations import _parse_version

        assert _parse_version("") == (0, 0, 0)

    def test_two_part_version_normalized(self) -> None:
        from nexus.db.migrations import _parse_version

        assert _parse_version("3.7") == (3, 7, 0)

    def test_single_part_version_normalized(self) -> None:
        from nexus.db.migrations import _parse_version

        assert _parse_version("5") == (5, 0, 0)

    def test_ordering(self) -> None:
        from nexus.db.migrations import _parse_version

        assert _parse_version("1.10.0") > _parse_version("1.9.0")
        assert _parse_version("2.0.0") > _parse_version("1.99.99")
        assert _parse_version("4.1.2") == _parse_version("4.1.2")


# ── Migration dataclass tests ──────────────────────────────────────────────


class TestMigrationDataclass:
    def test_fields(self) -> None:
        from nexus.db.migrations import Migration

        fn = MagicMock()
        m = Migration(introduced="4.0.0", name="test migration", fn=fn)
        assert m.introduced == "4.0.0"
        assert m.name == "test migration"
        assert m.fn is fn

    def test_migrations_list_exists(self) -> None:
        from nexus.db.migrations import MIGRATIONS

        assert isinstance(MIGRATIONS, list)
        # Baseline 16 + RDR-092 Phase 0d backfill (4.9.12) +
        # RDR-092 Phase 3.1 match_text column (4.9.13) +
        # legacy operation-shape retirement (4.10.1) +
        # builtin-bindings backfill (4.10.2) +
        # hook_failures batch columns (4.14.1, RDR-095) +
        # hook_failures chain enum (4.14.2, RDR-089) +
        # document_aspects table (4.14.2, RDR-089 P1.1) +
        # aspect_extraction_queue (4.14.2, RDR-089 nexus-qeo8) +
        # aspect_promotion_log table (4.14.2, RDR-089 Phase E) +
        # document_aspects.source_uri column + backfill (4.16.0,
        # RDR-096 P2.1) +
        # drop pre-RDR-096 null-field aspect rows (4.16.0, RDR-096 P2.2) +
        # plans.disabled_at column (4.17.1, nexus-mrzp soft-disable) +
        # retire hook_telemetry table — telemetry hook removed entirely
        # (4.18.1; the migration entry that previously created the table
        # was dropped when the read/write surface was retired)
        # = 28.
        # Prefer the name-based checks in TestBackfillPlanDimensions
        # and TestAddPlanMatchTextColumn for future guards; this
        # count is a cheap sentinel only.
        assert len(MIGRATIONS) == 28

    def test_migrations_ordered_by_version(self) -> None:
        from nexus.db.migrations import MIGRATIONS, _parse_version

        versions = [_parse_version(m.introduced) for m in MIGRATIONS]
        assert versions == sorted(versions)

    def test_all_migration_fns_callable(self) -> None:
        from nexus.db.migrations import MIGRATIONS

        for m in MIGRATIONS:
            assert callable(m.fn), f"Migration {m.name!r} fn is not callable"


# ── Module-level migration function tests ───────────────────────────────────


class TestMigrateMemoryFts:
    """migrate_memory_fts: upgrades FTS5 to include title column."""

    def test_noop_when_title_present(self) -> None:
        """Fresh DB already has title — no-op."""
        from nexus.db.migrations import migrate_memory_fts

        conn = sqlite3.connect(":memory:")
        # Create schema with title already present
        conn.executescript(
            """\
            CREATE TABLE memory (
                id INTEGER PRIMARY KEY, project TEXT NOT NULL, title TEXT NOT NULL,
                content TEXT NOT NULL, tags TEXT, timestamp TEXT NOT NULL
            );
            CREATE VIRTUAL TABLE memory_fts USING fts5(
                title, content, tags, content='memory', content_rowid='id'
            );
            """
        )
        migrate_memory_fts(conn)  # should not raise

    def test_migrates_old_schema(self) -> None:
        """Old DB without title in FTS — should migrate."""
        from nexus.db.migrations import migrate_memory_fts

        conn = sqlite3.connect(":memory:")
        conn.executescript(
            """\
            CREATE TABLE memory (
                id INTEGER PRIMARY KEY, project TEXT NOT NULL, title TEXT NOT NULL,
                content TEXT NOT NULL, tags TEXT, timestamp TEXT NOT NULL
            );
            CREATE VIRTUAL TABLE memory_fts USING fts5(
                content, tags, content='memory', content_rowid='id'
            );
            CREATE TRIGGER memory_ai AFTER INSERT ON memory BEGIN
                INSERT INTO memory_fts(rowid, content, tags) VALUES (new.id, new.content, new.tags);
            END;
            """
        )
        migrate_memory_fts(conn)

        # Verify FTS table now includes title
        row = conn.execute(
            "SELECT sql FROM sqlite_master WHERE type='table' AND name='memory_fts'"
        ).fetchone()
        assert row is not None
        assert "title" in row[0]

    def test_idempotent(self) -> None:
        """Calling twice should not raise."""
        from nexus.db.migrations import migrate_memory_fts

        conn = sqlite3.connect(":memory:")
        conn.executescript(
            """\
            CREATE TABLE memory (
                id INTEGER PRIMARY KEY, project TEXT NOT NULL, title TEXT NOT NULL,
                content TEXT NOT NULL, tags TEXT, timestamp TEXT NOT NULL
            );
            CREATE VIRTUAL TABLE memory_fts USING fts5(
                content, tags, content='memory', content_rowid='id'
            );
            """
        )
        migrate_memory_fts(conn)
        migrate_memory_fts(conn)  # second call — no-op


class TestMigratePlanProject:
    """migrate_plan_project: adds project column + FTS rebuild."""

    def test_noop_when_project_present(self) -> None:
        from nexus.db.migrations import migrate_plan_project

        conn = sqlite3.connect(":memory:")
        conn.executescript(
            """\
            CREATE TABLE plans (
                id INTEGER PRIMARY KEY, project TEXT NOT NULL DEFAULT '',
                query TEXT NOT NULL, plan_json TEXT NOT NULL,
                outcome TEXT DEFAULT 'success', tags TEXT DEFAULT '',
                created_at TEXT NOT NULL
            );
            """
        )
        migrate_plan_project(conn)

    def test_migrates_missing_project(self) -> None:
        from nexus.db.migrations import migrate_plan_project

        conn = sqlite3.connect(":memory:")
        conn.executescript(
            """\
            CREATE TABLE plans (
                id INTEGER PRIMARY KEY,
                query TEXT NOT NULL, plan_json TEXT NOT NULL,
                outcome TEXT DEFAULT 'success', tags TEXT DEFAULT '',
                created_at TEXT NOT NULL
            );
            CREATE VIRTUAL TABLE plans_fts USING fts5(
                query, tags, content=plans, content_rowid='id'
            );
            CREATE TRIGGER plans_ai AFTER INSERT ON plans BEGIN
                INSERT INTO plans_fts(rowid, query, tags) VALUES (new.id, new.query, new.tags);
            END;
            """
        )
        migrate_plan_project(conn)

        # project column exists
        cols = {r[1] for r in conn.execute("PRAGMA table_info(plans)").fetchall()}
        assert "project" in cols

        # FTS rebuilt with project
        row = conn.execute(
            "SELECT sql FROM sqlite_master WHERE type='table' AND name='plans_fts'"
        ).fetchone()
        assert row is not None
        assert "project" in row[0]

    def test_idempotent(self) -> None:
        from nexus.db.migrations import migrate_plan_project

        conn = sqlite3.connect(":memory:")
        conn.executescript(
            """\
            CREATE TABLE plans (
                id INTEGER PRIMARY KEY,
                query TEXT NOT NULL, plan_json TEXT NOT NULL,
                outcome TEXT DEFAULT 'success', tags TEXT DEFAULT '',
                created_at TEXT NOT NULL
            );
            CREATE VIRTUAL TABLE plans_fts USING fts5(
                query, tags, content=plans, content_rowid='id'
            );
            """
        )
        migrate_plan_project(conn)
        migrate_plan_project(conn)


class TestMigrateAccessTracking:
    """migrate_access_tracking: adds access_count and last_accessed columns."""

    def test_noop_when_columns_present(self) -> None:
        from nexus.db.migrations import migrate_access_tracking

        conn = sqlite3.connect(":memory:")
        conn.execute(
            "CREATE TABLE memory (id INTEGER PRIMARY KEY, access_count INTEGER DEFAULT 0 NOT NULL, last_accessed TEXT DEFAULT '')"
        )
        migrate_access_tracking(conn)

    def test_adds_missing_columns(self) -> None:
        from nexus.db.migrations import migrate_access_tracking

        conn = sqlite3.connect(":memory:")
        conn.execute("CREATE TABLE memory (id INTEGER PRIMARY KEY, content TEXT)")
        migrate_access_tracking(conn)
        cols = {r[1] for r in conn.execute("PRAGMA table_info(memory)").fetchall()}
        assert "access_count" in cols
        assert "last_accessed" in cols

    def test_idempotent(self) -> None:
        from nexus.db.migrations import migrate_access_tracking

        conn = sqlite3.connect(":memory:")
        conn.execute("CREATE TABLE memory (id INTEGER PRIMARY KEY, content TEXT)")
        migrate_access_tracking(conn)
        migrate_access_tracking(conn)


class TestMigrateTopics:
    """migrate_topics: creates topics and topic_assignments tables."""

    def test_noop_when_tables_exist(self) -> None:
        from nexus.db.migrations import migrate_topics

        conn = sqlite3.connect(":memory:")
        conn.execute(
            "CREATE TABLE topics (id INTEGER PRIMARY KEY, label TEXT NOT NULL, collection TEXT NOT NULL, created_at TEXT NOT NULL)"
        )
        migrate_topics(conn)

    def test_creates_tables(self) -> None:
        from nexus.db.migrations import migrate_topics

        conn = sqlite3.connect(":memory:")
        migrate_topics(conn)

        tables = {
            r[0]
            for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        }
        assert "topics" in tables
        assert "taxonomy_meta" in tables
        assert "topic_assignments" in tables
        assert "topic_links" in tables

    def test_idempotent(self) -> None:
        from nexus.db.migrations import migrate_topics

        conn = sqlite3.connect(":memory:")
        migrate_topics(conn)
        migrate_topics(conn)


class TestMigratePlanTtl:
    """migrate_plan_ttl: adds ttl column to plans table."""

    def test_noop_when_ttl_present(self) -> None:
        from nexus.db.migrations import migrate_plan_ttl

        conn = sqlite3.connect(":memory:")
        conn.execute(
            "CREATE TABLE plans (id INTEGER PRIMARY KEY, query TEXT, ttl INTEGER)"
        )
        migrate_plan_ttl(conn)

    def test_adds_ttl(self) -> None:
        from nexus.db.migrations import migrate_plan_ttl

        conn = sqlite3.connect(":memory:")
        conn.execute("CREATE TABLE plans (id INTEGER PRIMARY KEY, query TEXT)")
        migrate_plan_ttl(conn)
        cols = {r[1] for r in conn.execute("PRAGMA table_info(plans)").fetchall()}
        assert "ttl" in cols

    def test_noop_when_plans_table_missing(self) -> None:
        """No plans table → no-op (PRAGMA table_info returns empty)."""
        from nexus.db.migrations import migrate_plan_ttl

        conn = sqlite3.connect(":memory:")
        migrate_plan_ttl(conn)  # should not raise


class TestMigrateAssignedBy:
    """migrate_assigned_by: adds assigned_by column to topic_assignments."""

    def test_noop_when_table_missing(self) -> None:
        """No topic_assignments table → no-op (not crash)."""
        from nexus.db.migrations import migrate_assigned_by

        conn = sqlite3.connect(":memory:")
        migrate_assigned_by(conn)  # should not raise

    def test_noop_when_column_present(self) -> None:
        from nexus.db.migrations import migrate_assigned_by

        conn = sqlite3.connect(":memory:")
        conn.execute(
            "CREATE TABLE topic_assignments (doc_id TEXT, topic_id INTEGER, assigned_by TEXT NOT NULL DEFAULT 'hdbscan')"
        )
        migrate_assigned_by(conn)

    def test_adds_column(self) -> None:
        from nexus.db.migrations import migrate_assigned_by

        conn = sqlite3.connect(":memory:")
        conn.execute(
            "CREATE TABLE topic_assignments (doc_id TEXT, topic_id INTEGER)"
        )
        migrate_assigned_by(conn)
        cols = {
            r[1]
            for r in conn.execute("PRAGMA table_info(topic_assignments)").fetchall()
        }
        assert "assigned_by" in cols

    def test_idempotent(self) -> None:
        from nexus.db.migrations import migrate_assigned_by

        conn = sqlite3.connect(":memory:")
        conn.execute(
            "CREATE TABLE topic_assignments (doc_id TEXT, topic_id INTEGER)"
        )
        migrate_assigned_by(conn)
        migrate_assigned_by(conn)


class TestMigrateReviewColumns:
    """migrate_review_columns: adds review_status and terms to topics."""

    def test_noop_when_table_missing(self) -> None:
        """No topics table → no-op (not crash)."""
        from nexus.db.migrations import migrate_review_columns

        conn = sqlite3.connect(":memory:")
        migrate_review_columns(conn)  # should not raise

    def test_noop_when_columns_present(self) -> None:
        from nexus.db.migrations import migrate_review_columns

        conn = sqlite3.connect(":memory:")
        conn.execute(
            "CREATE TABLE topics (id INTEGER PRIMARY KEY, label TEXT, review_status TEXT DEFAULT 'pending', terms TEXT)"
        )
        migrate_review_columns(conn)

    def test_adds_columns(self) -> None:
        from nexus.db.migrations import migrate_review_columns

        conn = sqlite3.connect(":memory:")
        conn.execute("CREATE TABLE topics (id INTEGER PRIMARY KEY, label TEXT)")
        migrate_review_columns(conn)
        cols = {r[1] for r in conn.execute("PRAGMA table_info(topics)").fetchall()}
        assert "review_status" in cols
        assert "terms" in cols

    def test_idempotent(self) -> None:
        from nexus.db.migrations import migrate_review_columns

        conn = sqlite3.connect(":memory:")
        conn.execute("CREATE TABLE topics (id INTEGER PRIMARY KEY, label TEXT)")
        migrate_review_columns(conn)
        migrate_review_columns(conn)

    def test_partial_column_missing_commits(self) -> None:
        """Only terms missing (review_status present) → still commits."""
        from nexus.db.migrations import migrate_review_columns

        conn = sqlite3.connect(":memory:")
        conn.execute(
            "CREATE TABLE topics (id INTEGER PRIMARY KEY, label TEXT, "
            "review_status TEXT NOT NULL DEFAULT 'pending')"
        )
        migrate_review_columns(conn)

        # Verify terms column was added and committed
        cols = {r[1] for r in conn.execute("PRAGMA table_info(topics)").fetchall()}
        assert "terms" in cols


# ── apply_pending tests ─────────────────────────────────────────────────────


class TestApplyPending:
    """Tests for apply_pending(conn, current_version)."""

    @pytest.fixture(autouse=True)
    def _clear_upgrade_done(self) -> None:
        """Clear the module-level _upgrade_done set between tests."""
        from nexus.db import migrations

        migrations._upgrade_done.clear()

    def test_fresh_db_seeds_zero(self) -> None:
        """Empty DB → seeds '0.0.0', runs all migrations, creates _nexus_version."""
        from nexus.db.migrations import apply_pending

        conn = sqlite3.connect(":memory:")
        apply_pending(conn, "4.1.2")

        # _nexus_version table exists with current version
        row = conn.execute(
            "SELECT value FROM _nexus_version WHERE key='cli_version'"
        ).fetchone()
        assert row is not None
        assert row[0] == "4.1.2"

        # Base tables should exist (created in step 1)
        tables = {
            r[0]
            for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        }
        assert "memory" in tables
        assert "plans" in tables
        assert "topics" in tables

    def test_existing_db_seeds_pre_registry(self, tmp_path: Path) -> None:
        """DB with existing data → seeds PRE_REGISTRY_VERSION, skips old migrations."""
        from nexus.db.migrations import PRE_REGISTRY_VERSION, apply_pending

        db_path = tmp_path / "test.db"
        conn = sqlite3.connect(str(db_path))
        # Simulate a fully-migrated pre-registry install (all columns present)
        conn.executescript(
            """\
            CREATE TABLE memory (
                id INTEGER PRIMARY KEY, project TEXT NOT NULL, title TEXT NOT NULL,
                session TEXT, agent TEXT, content TEXT NOT NULL, tags TEXT,
                timestamp TEXT NOT NULL, ttl INTEGER,
                access_count INTEGER DEFAULT 0 NOT NULL, last_accessed TEXT DEFAULT ''
            );
            CREATE VIRTUAL TABLE memory_fts USING fts5(
                title, content, tags, content='memory', content_rowid='id'
            );
            CREATE TRIGGER memory_ai AFTER INSERT ON memory BEGIN
                INSERT INTO memory_fts(rowid, title, content, tags)
                    VALUES (new.id, new.title, new.content, new.tags);
            END;
            """
        )
        conn.execute(
            "INSERT INTO memory (project, title, content, tags, timestamp) "
            "VALUES ('test', 'note1', 'content', '', '2026-01-01')"
        )
        conn.commit()

        apply_pending(conn, "4.1.2")

        row = conn.execute(
            "SELECT value FROM _nexus_version WHERE key='cli_version'"
        ).fetchone()
        assert row is not None
        assert row[0] == "4.1.2"
        conn.close()

    def test_existing_db_empty_memory_seeds_pre_registry(self, tmp_path: Path) -> None:
        """Existing install with empty memory table → still seeds PRE_REGISTRY_VERSION.

        Regression test: the bootstrap heuristic must detect the pre-existing
        memory table structurally (not by row count), so an existing install
        with zero memory entries is correctly identified.
        """
        from nexus.db.migrations import PRE_REGISTRY_VERSION, apply_pending

        db_path = tmp_path / "test.db"
        conn = sqlite3.connect(str(db_path))
        # Create memory table (no data) to simulate existing install
        conn.execute(
            "CREATE TABLE memory ("
            "id INTEGER PRIMARY KEY, project TEXT NOT NULL, title TEXT NOT NULL, "
            "session TEXT, agent TEXT, content TEXT NOT NULL, tags TEXT, "
            "timestamp TEXT NOT NULL, ttl INTEGER, "
            "access_count INTEGER DEFAULT 0 NOT NULL, last_accessed TEXT DEFAULT '')"
        )
        conn.commit()

        apply_pending(conn, PRE_REGISTRY_VERSION)

        row = conn.execute(
            "SELECT value FROM _nexus_version WHERE key='cli_version'"
        ).fetchone()
        assert row[0] == PRE_REGISTRY_VERSION
        conn.close()

    def test_already_current_version_noop(self, tmp_path: Path) -> None:
        """DB already at current version → no migrations run."""
        from nexus.db.migrations import apply_pending

        db_path = tmp_path / "test.db"
        conn = sqlite3.connect(str(db_path))

        # First run
        apply_pending(conn, "4.1.2")

        # Clear fast path to force DB read
        from nexus.db import migrations

        migrations._upgrade_done.clear()

        # Second run with same version — should be a no-op
        apply_pending(conn, "4.1.2")

        row = conn.execute(
            "SELECT value FROM _nexus_version WHERE key='cli_version'"
        ).fetchone()
        assert row[0] == "4.1.2"
        conn.close()

    def test_upgrade_done_fast_path(self, tmp_path: Path) -> None:
        """Once apply_pending runs, subsequent calls on same path skip entirely."""
        from nexus.db.migrations import _upgrade_done, apply_pending

        db_path = tmp_path / "test.db"
        conn = sqlite3.connect(str(db_path))
        apply_pending(conn, "4.1.2")

        # _upgrade_done should contain the path key
        assert len(_upgrade_done) > 0

        # Second call should hit the fast path (not touch DB at all)
        apply_pending(conn, "4.1.2")  # no-op
        conn.close()

    def test_version_filtering(self) -> None:
        """Only migrations between last_seen and current_version execute."""
        from nexus.db.migrations import apply_pending

        conn = sqlite3.connect(":memory:")

        # First: apply up to version 2.0.0 — should run only migrate_memory_fts (1.10.0)
        apply_pending(conn, "2.0.0")

        row = conn.execute(
            "SELECT value FROM _nexus_version WHERE key='cli_version'"
        ).fetchone()
        assert row[0] == "2.0.0"

        # Clear fast path to test incremental upgrade
        from nexus.db import migrations

        migrations._upgrade_done.clear()

        # Now upgrade to 4.1.2 — should run remaining migrations
        apply_pending(conn, "4.1.2")

        row = conn.execute(
            "SELECT value FROM _nexus_version WHERE key='cli_version'"
        ).fetchone()
        assert row[0] == "4.1.2"

    def test_idempotent(self, tmp_path: Path) -> None:
        """Running apply_pending twice with same version yields identical DB state."""
        from nexus.db.migrations import apply_pending

        db_path = tmp_path / "test.db"
        conn = sqlite3.connect(str(db_path))
        apply_pending(conn, "4.1.2")

        # Snapshot schema
        schema1 = conn.execute(
            "SELECT sql FROM sqlite_master WHERE sql IS NOT NULL ORDER BY name"
        ).fetchall()

        from nexus.db import migrations

        migrations._upgrade_done.clear()

        apply_pending(conn, "4.1.2")

        schema2 = conn.execute(
            "SELECT sql FROM sqlite_master WHERE sql IS NOT NULL ORDER BY name"
        ).fetchall()

        assert schema1 == schema2
        conn.close()

    def test_concurrent_bootstrap(self, tmp_path: Path) -> None:
        """Two threads calling apply_pending simultaneously — no crash, version seeded once."""
        from nexus.db.migrations import apply_pending

        db_path = tmp_path / "test.db"
        errors: list[Exception] = []
        barrier = threading.Barrier(2, timeout=5)

        def worker() -> None:
            try:
                conn = sqlite3.connect(str(db_path))
                conn.execute("PRAGMA busy_timeout=5000")
                barrier.wait()
                apply_pending(conn, "4.1.2")
                conn.close()
            except Exception as e:
                errors.append(e)

        from nexus.db import migrations

        migrations._upgrade_done.clear()

        t1 = threading.Thread(target=worker)
        t2 = threading.Thread(target=worker)
        t1.start()
        t2.start()
        t1.join(timeout=10)
        t2.join(timeout=10)

        assert not errors, f"Concurrent bootstrap failed: {errors}"

        # Verify single correct version row
        conn = sqlite3.connect(str(db_path))
        rows = conn.execute(
            "SELECT value FROM _nexus_version WHERE key='cli_version'"
        ).fetchall()
        assert len(rows) == 1
        assert rows[0][0] == "4.1.2"
        conn.close()

    def test_concurrent_apply_pending_runs_once(self, tmp_path: Path) -> None:
        """_upgrade_lock prevents concurrent apply_pending double-execution."""
        from unittest.mock import patch as _patch

        from nexus.db.migrations import apply_pending

        db_path = tmp_path / "test.db"
        call_count = {"n": 0}
        original_bootstrap = None

        # Lazy capture of the real bootstrap_version
        from nexus.db import migrations

        original_bootstrap = migrations.bootstrap_version

        def counting_bootstrap(conn):
            call_count["n"] += 1
            return original_bootstrap(conn)

        migrations._upgrade_done.clear()
        errors: list[Exception] = []
        barrier = threading.Barrier(2, timeout=5)

        def worker() -> None:
            try:
                conn = sqlite3.connect(str(db_path))
                conn.execute("PRAGMA busy_timeout=5000")
                barrier.wait()
                apply_pending(conn, "4.1.2")
                conn.close()
            except Exception as e:
                errors.append(e)

        with _patch.object(migrations, "bootstrap_version", counting_bootstrap):
            t1 = threading.Thread(target=worker)
            t2 = threading.Thread(target=worker)
            t1.start()
            t2.start()
            t1.join(timeout=10)
            t2.join(timeout=10)

        assert not errors, f"Concurrent apply_pending failed: {errors}"
        # First thread adds path_key under _upgrade_lock; the second sees
        # it already present and returns early before calling bootstrap_version.
        assert call_count["n"] == 1

    def test_prerelease_version_not_stored(self, tmp_path: Path) -> None:
        """Pre-release versions (parsed as (0,0,0)) must not be stored."""
        from nexus.db.migrations import apply_pending

        db_path = tmp_path / "test.db"
        conn = sqlite3.connect(str(db_path))
        # First: seed with a proper version
        apply_pending(conn, "4.1.2")

        from nexus.db import migrations

        migrations._upgrade_done.clear()

        # Now call with a pre-release — should NOT downgrade stored version
        apply_pending(conn, "4.2.0rc1")

        row = conn.execute(
            "SELECT value FROM _nexus_version WHERE key='cli_version'"
        ).fetchone()
        assert row[0] == "4.1.2"  # unchanged
        conn.close()

    def test_version_downgrade_not_stored(self, tmp_path: Path) -> None:
        """Calling apply_pending with a lower version must not lower stored version."""
        from nexus.db.migrations import apply_pending

        db_path = tmp_path / "test.db"
        conn = sqlite3.connect(str(db_path))
        apply_pending(conn, "4.1.2")

        from nexus.db import migrations

        migrations._upgrade_done.clear()

        apply_pending(conn, "3.0.0")

        row = conn.execute(
            "SELECT value FROM _nexus_version WHERE key='cli_version'"
        ).fetchone()
        assert row[0] == "4.1.2"  # unchanged
        conn.close()


# ── Constants tests ─────────────────────────────────────────────────────────


class TestConstants:
    def test_pre_registry_version(self) -> None:
        from nexus.db.migrations import PRE_REGISTRY_VERSION

        assert PRE_REGISTRY_VERSION == "4.1.2"

    def test_upgrade_done_is_set(self) -> None:
        from nexus.db.migrations import _upgrade_done

        assert isinstance(_upgrade_done, set)


# ── T2Database integration tests (Phase 3) ──────────────────────────────────


class TestT2DatabaseIntegration:
    """Test T2Database.__init__() with transient connection and _upgrade_done fast path."""

    @pytest.fixture(autouse=True)
    def _clear_module_state(self) -> None:
        """Clear all module-level migration guard sets."""
        from nexus.db import migrations
        from nexus.db.t2 import catalog_taxonomy, memory_store, plan_library

        migrations._upgrade_done.clear()
        memory_store._migrated_paths.clear()
        plan_library._migrated_paths.clear()
        catalog_taxonomy._migrated_paths.clear()

    def test_t2database_creates_version_table(self, tmp_path: Path) -> None:
        """T2Database construction should create _nexus_version table."""
        from nexus.db.t2 import T2Database

        db_path = tmp_path / "memory.db"
        db = T2Database(db_path)

        # Verify _nexus_version exists by connecting directly
        conn = sqlite3.connect(str(db_path))
        row = conn.execute(
            "SELECT value FROM _nexus_version WHERE key='cli_version'"
        ).fetchone()
        assert row is not None
        conn.close()
        db.close()

    def test_t2database_fast_path_second_construction(self, tmp_path: Path) -> None:
        """Second T2Database on same path skips apply_pending entirely."""
        from unittest.mock import patch

        from nexus.db.t2 import T2Database

        db_path = tmp_path / "memory.db"
        db1 = T2Database(db_path)
        db1.close()

        with patch("nexus.db.migrations.apply_pending") as mock_ap:
            db2 = T2Database(db_path)
            mock_ap.assert_not_called()
            db2.close()

    def test_t2database_base_tables_exist(self, tmp_path: Path) -> None:
        """All four base schemas are created by transient connection."""
        from nexus.db.t2 import T2Database

        db_path = tmp_path / "memory.db"
        db = T2Database(db_path)

        conn = sqlite3.connect(str(db_path))
        tables = {
            r[0]
            for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        }
        assert "memory" in tables
        assert "plans" in tables
        assert "topics" in tables
        assert "relevance_log" in tables
        conn.close()
        db.close()

    def test_standalone_memory_store_works(self, tmp_path: Path) -> None:
        """MemoryStore constructed outside T2Database still works."""
        from nexus.db.t2.memory_store import MemoryStore

        db_path = tmp_path / "memory.db"
        store = MemoryStore(db_path)
        # Basic operation — verify put doesn't raise
        store.put("test", "title1", "content", tags="tag1")
        # Verify row exists via get
        result = store.get("test", "title1")
        assert result is not None
        assert result["content"] == "content"
        store.close()

    def test_concurrent_t2database_construction(self, tmp_path: Path) -> None:
        """Two threads constructing T2Database on same path — no crash."""
        from nexus.db.t2 import T2Database

        db_path = tmp_path / "memory.db"
        errors: list[Exception] = []
        databases: list[T2Database] = []
        barrier = threading.Barrier(2, timeout=5)

        def construct() -> None:
            try:
                barrier.wait()
                db = T2Database(db_path)
                databases.append(db)
            except Exception as e:
                errors.append(e)

        t1 = threading.Thread(target=construct)
        t2 = threading.Thread(target=construct)
        t1.start()
        t2.start()
        t1.join(timeout=10)
        t2.join(timeout=10)

        assert not errors, f"Concurrent T2Database construction failed: {errors}"

        for db in databases:
            db.close()


# ── Issue #190 regression: _create_base_tables on pre-4.4.0 DB ──────────────


class TestBootstrapOnPreRdr078Plans:
    """``bootstrap_version`` must not crash when the existing ``plans``
    table lacks the RDR-078 columns (``verb``, ``scope``, ``dimensions``,
    …). Seeded the crash Steve reported in issue #190 on conexus 4.5.3.

    Root cause: ``_PLANS_SCHEMA_SQL`` previously created four indexes
    referencing RDR-078 columns inline. On a pre-4.4.0 DB the plans
    table existed without those columns; the ``CREATE TABLE IF NOT
    EXISTS`` was a no-op, but the ``CREATE INDEX IF NOT EXISTS
    idx_plans_verb ON plans(verb)`` statement crashed before the 4.4.0
    ``_add_plan_dimensional_identity`` migration could add the columns.
    """

    def _seed_pre_rdr078_plans(self, conn: sqlite3.Connection) -> None:
        """Seed a ``plans`` table shaped like pre-4.4.0 installs."""
        conn.executescript(
            """\
            CREATE TABLE plans (
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

    def test_bootstrap_does_not_crash_on_pre_rdr078_plans(self) -> None:
        """Regression: bootstrap_version + _create_base_tables must no-op
        cleanly against an old plans shape."""
        from nexus.db.migrations import bootstrap_version

        conn = sqlite3.connect(":memory:")
        self._seed_pre_rdr078_plans(conn)

        # Must not raise ``sqlite3.OperationalError: no such column: verb``.
        last_seen = bootstrap_version(conn)
        assert isinstance(last_seen, str) and last_seen

    def test_apply_pending_adds_columns_then_indexes(self) -> None:
        """After ``apply_pending`` walks past 4.4.0, the plans table has
        both the RDR-078 columns and the four indexes.
        """
        from nexus.db.migrations import apply_pending

        conn = sqlite3.connect(":memory:")
        self._seed_pre_rdr078_plans(conn)

        apply_pending(conn, "4.6.2")

        cols = {r[1] for r in conn.execute("PRAGMA table_info(plans)").fetchall()}
        assert {"verb", "scope", "dimensions"} <= cols

        # PRAGMA index_list rows: (seq, name, unique, origin, partial).
        index_info = {
            r[1]: {"unique": bool(r[2]), "partial": bool(r[4])}
            for r in conn.execute("PRAGMA index_list(plans)").fetchall()
        }
        assert "idx_plans_verb" in index_info
        assert "idx_plans_scope" in index_info
        assert "idx_plans_verb_scope" in index_info
        assert "idx_plans_project_dimensions" in index_info
        # Guard against silent degradation: this one MUST be unique AND
        # partial (``WHERE dimensions IS NOT NULL``) per the migration.
        assert index_info["idx_plans_project_dimensions"]["unique"]
        assert index_info["idx_plans_project_dimensions"]["partial"]


# ── RDR-087 Phase 2.1: search_telemetry migration ───────────────────────────


class TestMigrateSearchTelemetry:
    """``migrate_search_telemetry`` creates the ``search_telemetry`` table
    + two indices (collection, ts) on a fresh DB and is a no-op on
    re-apply. Used by RDR-087 Phase 2 to persist per-call threshold
    filter telemetry.
    """

    def test_creates_table_on_fresh_db(self) -> None:
        from nexus.db.migrations import migrate_search_telemetry

        conn = sqlite3.connect(":memory:")
        migrate_search_telemetry(conn)

        tables = {
            r[0]
            for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        }
        assert "search_telemetry" in tables

    def test_creates_indices(self) -> None:
        from nexus.db.migrations import migrate_search_telemetry

        conn = sqlite3.connect(":memory:")
        migrate_search_telemetry(conn)

        indices = {
            r[0]
            for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='index'"
            ).fetchall()
        }
        assert "idx_search_tel_collection" in indices
        assert "idx_search_tel_ts" in indices

    def test_schema_columns_match_spec(self) -> None:
        """RDR-087 spec (lines 299-308): columns + types must match."""
        from nexus.db.migrations import migrate_search_telemetry

        conn = sqlite3.connect(":memory:")
        migrate_search_telemetry(conn)

        cols = {
            r[1]: r[2]
            for r in conn.execute(
                "PRAGMA table_info(search_telemetry)"
            ).fetchall()
        }
        assert cols.keys() == {
            "ts", "query_hash", "collection",
            "raw_count", "dropped_count", "top_distance", "threshold",
        }
        assert cols["ts"] == "TEXT"
        assert cols["query_hash"] == "TEXT"
        assert cols["collection"] == "TEXT"
        assert cols["raw_count"] == "INTEGER"
        assert cols["dropped_count"] == "INTEGER"
        assert cols["top_distance"] == "REAL"
        assert cols["threshold"] == "REAL"

    def test_primary_key_is_composite(self) -> None:
        """PRIMARY KEY (ts, query_hash, collection) per RDR-087 line 307."""
        from nexus.db.migrations import migrate_search_telemetry

        conn = sqlite3.connect(":memory:")
        migrate_search_telemetry(conn)

        # PRAGMA table_info pk column: 1/2/3 for composite PK members.
        pk_cols = sorted(
            (r[5], r[1])  # (pk_position, column_name)
            for r in conn.execute(
                "PRAGMA table_info(search_telemetry)"
            ).fetchall()
            if r[5] > 0
        )
        assert [name for _, name in pk_cols] == ["ts", "query_hash", "collection"]

    def test_idempotent_on_reapply(self) -> None:
        from nexus.db.migrations import migrate_search_telemetry

        conn = sqlite3.connect(":memory:")
        migrate_search_telemetry(conn)
        migrate_search_telemetry(conn)  # must not raise

    def test_in_migrations_list(self) -> None:
        from nexus.db.migrations import MIGRATIONS

        matches = [
            (m.introduced, m.name) for m in MIGRATIONS
            if "search_telemetry" in m.name
        ]
        assert matches, (
            "search_telemetry migration must be registered in MIGRATIONS"
        )
        assert matches[0][0] >= "4.6.0", (
            f"search_telemetry migration must be introduced in 4.6.0+; "
            f"got {matches[0][0]}"
        )

    def test_accepts_insert_or_ignore(self) -> None:
        """Insert via the Phase 2.2 contract shape — duplicate PK must not raise."""
        from nexus.db.migrations import migrate_search_telemetry

        conn = sqlite3.connect(":memory:")
        migrate_search_telemetry(conn)

        row = (
            "2026-04-17T18:00:00Z", "abc123", "knowledge__art",
            3, 3, 0.80, 0.65,
        )
        conn.execute(
            "INSERT OR IGNORE INTO search_telemetry "
            "(ts, query_hash, collection, raw_count, dropped_count, top_distance, threshold) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            row,
        )
        # Same composite PK — second insert must be a no-op, not an error.
        conn.execute(
            "INSERT OR IGNORE INTO search_telemetry "
            "(ts, query_hash, collection, raw_count, dropped_count, top_distance, threshold) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            row,
        )
        count = conn.execute(
            "SELECT COUNT(*) FROM search_telemetry"
        ).fetchone()[0]
        assert count == 1


# ── RDR-087 errata: rename dropped_count → kept_count (4.6.1) ───────────────


class TestMigrateRenameDroppedToKept:
    """``migrate_rename_dropped_to_kept`` upgrades a 4.6.0-era DB whose
    ``search_telemetry`` table has ``dropped_count`` to the spec-aligned
    ``kept_count``. Stored values are flipped via
    ``kept_count = raw_count - dropped_count``.
    """

    def _seed_4_6_0(self, conn: sqlite3.Connection) -> None:
        from nexus.db.migrations import migrate_search_telemetry

        migrate_search_telemetry(conn)

    def test_renames_column_and_flips_values(self) -> None:
        from nexus.db.migrations import migrate_rename_dropped_to_kept

        conn = sqlite3.connect(":memory:")
        self._seed_4_6_0(conn)
        conn.execute(
            "INSERT INTO search_telemetry "
            "(ts, query_hash, collection, raw_count, dropped_count, top_distance, threshold) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            ("2026-04-17T18:00:00Z", "h" * 64, "code__a", 5, 2, 0.30, 0.45),
        )
        conn.commit()

        migrate_rename_dropped_to_kept(conn)

        cols = {
            r[1]
            for r in conn.execute("PRAGMA table_info(search_telemetry)").fetchall()
        }
        assert "dropped_count" not in cols
        assert "kept_count" in cols

        row = conn.execute(
            "SELECT raw_count, kept_count FROM search_telemetry"
        ).fetchone()
        assert row == (5, 3)  # kept = raw − dropped = 5 − 2

    def test_idempotent_on_reapply(self) -> None:
        """Second call is a no-op — column already renamed."""
        from nexus.db.migrations import migrate_rename_dropped_to_kept

        conn = sqlite3.connect(":memory:")
        self._seed_4_6_0(conn)
        migrate_rename_dropped_to_kept(conn)
        migrate_rename_dropped_to_kept(conn)  # must not raise

        row = conn.execute(
            "SELECT kept_count FROM search_telemetry"
        ).fetchone()
        assert row is None  # empty table, column still exists

    def test_noop_when_table_absent(self) -> None:
        """No ``search_telemetry`` table — migration must be a silent no-op."""
        from nexus.db.migrations import migrate_rename_dropped_to_kept

        conn = sqlite3.connect(":memory:")
        migrate_rename_dropped_to_kept(conn)  # must not raise

    def test_in_migrations_list(self) -> None:
        from nexus.db.migrations import MIGRATIONS

        matches = [
            (m.introduced, m.name)
            for m in MIGRATIONS
            if "kept_count" in m.name
        ]
        assert matches, "rename-to-kept_count migration must be registered"
        assert matches[0][0] == "4.6.1"


# ── RDR-086 Phase 1.1: chash_index migration ───────────────────────────────


class TestMigrateChashIndex:
    """``migrate_chash_index`` creates the T2 ``chash_index`` table that speeds
    up global ``resolve_chash(chash)`` lookups (RDR-086 Phase 2). Compound PK
    ``(chash, physical_collection)`` allows the same chunk text (same SHA-256)
    to be legitimately indexed into multiple collections without FK violation.
    """

    def test_chash_index_migration_fresh_apply(self) -> None:
        """Fresh DB: migration creates the table."""
        from nexus.db.migrations import migrate_chash_index

        conn = sqlite3.connect(":memory:")
        migrate_chash_index(conn)

        tables = {
            r[0]
            for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        }
        assert "chash_index" in tables

    def test_chash_index_schema_columns(self) -> None:
        """Columns match the bead spec: chash, physical_collection, doc_id, created_at — all TEXT."""
        from nexus.db.migrations import migrate_chash_index

        conn = sqlite3.connect(":memory:")
        migrate_chash_index(conn)

        cols = {
            r[1]: r[2]
            for r in conn.execute(
                "PRAGMA table_info(chash_index)"
            ).fetchall()
        }
        assert cols.keys() == {"chash", "physical_collection", "doc_id", "created_at"}
        assert cols["chash"] == "TEXT"
        assert cols["physical_collection"] == "TEXT"
        assert cols["doc_id"] == "TEXT"
        assert cols["created_at"] == "TEXT"

    def test_chash_index_compound_pk_allows_duplicate_hash_different_collection(self) -> None:
        """Same chash in two different collections: both INSERTs must succeed.

        Repro of the FK-violation the single-column PK would cause. RF-10
        Issue 1: knowledge__delos + knowledge__delos_docling both ingest a
        paper; every chunk's SHA-256 is identical.
        """
        from nexus.db.migrations import migrate_chash_index

        conn = sqlite3.connect(":memory:")
        migrate_chash_index(conn)

        conn.execute(
            "INSERT INTO chash_index VALUES (?, ?, ?, ?)",
            ("abc123", "knowledge__delos",          "doc-1", "2026-04-18T00:00:00Z"),
        )
        conn.execute(
            "INSERT INTO chash_index VALUES (?, ?, ?, ?)",
            ("abc123", "knowledge__delos_docling", "doc-2", "2026-04-18T00:00:01Z"),
        )
        conn.commit()

        rows = conn.execute(
            "SELECT physical_collection, doc_id FROM chash_index WHERE chash = ? ORDER BY physical_collection",
            ("abc123",),
        ).fetchall()
        assert rows == [
            ("knowledge__delos",          "doc-1"),
            ("knowledge__delos_docling", "doc-2"),
        ]

    def test_chash_index_compound_pk_rejects_same_chash_same_collection(self) -> None:
        """Same (chash, collection) pair: second INSERT must violate the PK.

        Guards against catalog writing the same chash to the same collection twice.
        The Phase 1.2 dual-write sites use INSERT OR REPLACE, not bare INSERT,
        so this is a schema contract assertion not an operational scenario.
        """
        from nexus.db.migrations import migrate_chash_index

        conn = sqlite3.connect(":memory:")
        migrate_chash_index(conn)

        conn.execute(
            "INSERT INTO chash_index VALUES (?, ?, ?, ?)",
            ("abc123", "knowledge__delos", "doc-1", "2026-04-18T00:00:00Z"),
        )
        conn.commit()

        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "INSERT INTO chash_index VALUES (?, ?, ?, ?)",
                ("abc123", "knowledge__delos", "doc-other", "2026-04-18T00:00:01Z"),
            )
            conn.commit()

    def test_chash_index_secondary_index_exists(self) -> None:
        """Secondary index on physical_collection — used by Phase 1.4 delete cascade.

        Without it, the `DELETE FROM chash_index WHERE physical_collection = ?`
        in `nx collection delete` is a table scan.
        """
        from nexus.db.migrations import migrate_chash_index

        conn = sqlite3.connect(":memory:")
        migrate_chash_index(conn)

        indices = {
            r[0]
            for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='index'"
            ).fetchall()
        }
        assert "idx_chash_index_collection" in indices

    def test_chash_index_primary_key_is_compound(self) -> None:
        """PRIMARY KEY (chash, physical_collection) per bead design — not single-column chash."""
        from nexus.db.migrations import migrate_chash_index

        conn = sqlite3.connect(":memory:")
        migrate_chash_index(conn)

        pk_cols = sorted(
            (r[5], r[1])  # (pk_position, column_name)
            for r in conn.execute(
                "PRAGMA table_info(chash_index)"
            ).fetchall()
            if r[5] > 0
        )
        assert [name for _, name in pk_cols] == ["chash", "physical_collection"]

    def test_chash_index_migration_idempotent(self) -> None:
        """Re-applying on a populated DB must not raise or clobber data."""
        from nexus.db.migrations import migrate_chash_index

        conn = sqlite3.connect(":memory:")
        migrate_chash_index(conn)
        conn.execute(
            "INSERT INTO chash_index VALUES (?, ?, ?, ?)",
            ("abc123", "knowledge__delos", "doc-1", "2026-04-18T00:00:00Z"),
        )
        conn.commit()

        migrate_chash_index(conn)  # must not raise
        migrate_chash_index(conn)  # must not raise

        # Data preserved.
        row = conn.execute(
            "SELECT chash, physical_collection, doc_id FROM chash_index"
        ).fetchone()
        assert row == ("abc123", "knowledge__delos", "doc-1")

    def test_chash_index_in_migrations_list(self) -> None:
        from nexus.db.migrations import MIGRATIONS

        matches = [
            (m.introduced, m.name) for m in MIGRATIONS
            if "chash_index" in m.name
        ]
        assert matches, "chash_index migration must be registered in MIGRATIONS"
        # RDR-086 lands in v4.7.0 (next minor release after v4.6.5).
        assert matches[0][0] >= "4.7.0", (
            f"chash_index migration must be introduced in >= 4.7.0, got {matches[0][0]!r}"
        )


class TestMigrateHookFailures:
    """``migrate_hook_failures`` creates the T2 ``hook_failures`` table so
    GH #251 can surface post-store hook failures in ``nx taxonomy status``.
    """

    def test_hook_failures_migration_fresh_apply(self) -> None:
        from nexus.db.migrations import migrate_hook_failures

        conn = sqlite3.connect(":memory:")
        migrate_hook_failures(conn)

        tables = {
            r[0]
            for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        }
        assert "hook_failures" in tables

    def test_hook_failures_schema_columns(self) -> None:
        from nexus.db.migrations import migrate_hook_failures

        conn = sqlite3.connect(":memory:")
        migrate_hook_failures(conn)

        cols = {
            r[1]: r[2]
            for r in conn.execute(
                "PRAGMA table_info(hook_failures)"
            ).fetchall()
        }
        assert cols.keys() == {
            "id", "doc_id", "collection", "hook_name", "error", "occurred_at",
        }
        assert cols["hook_name"] == "TEXT"
        assert cols["occurred_at"] == "TEXT"

    def test_hook_failures_default_occurred_at(self) -> None:
        """INSERT without occurred_at auto-fills via DEFAULT CURRENT_TIMESTAMP."""
        from nexus.db.migrations import migrate_hook_failures

        conn = sqlite3.connect(":memory:")
        migrate_hook_failures(conn)
        conn.execute(
            "INSERT INTO hook_failures (hook_name, error) VALUES (?, ?)",
            ("taxonomy_assign_batch_hook", "simulated"),
        )
        conn.commit()

        row = conn.execute(
            "SELECT hook_name, error, occurred_at FROM hook_failures"
        ).fetchone()
        assert row[0] == "taxonomy_assign_batch_hook"
        assert row[1] == "simulated"
        assert row[2]  # non-empty timestamp

    def test_hook_failures_indexes_exist(self) -> None:
        from nexus.db.migrations import migrate_hook_failures

        conn = sqlite3.connect(":memory:")
        migrate_hook_failures(conn)

        indices = {
            r[0]
            for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='index'"
            ).fetchall()
        }
        assert "idx_hook_failures_occurred_at" in indices
        assert "idx_hook_failures_collection" in indices

    def test_hook_failures_migration_idempotent(self) -> None:
        from nexus.db.migrations import migrate_hook_failures

        conn = sqlite3.connect(":memory:")
        migrate_hook_failures(conn)
        conn.execute(
            "INSERT INTO hook_failures (hook_name, error) VALUES (?, ?)",
            ("x", "y"),
        )
        conn.commit()

        migrate_hook_failures(conn)  # must not raise
        row = conn.execute(
            "SELECT hook_name, error FROM hook_failures"
        ).fetchone()
        assert row == ("x", "y")

    def test_hook_failures_in_migrations_list(self) -> None:
        from nexus.db.migrations import MIGRATIONS

        matches = [
            (m.introduced, m.name) for m in MIGRATIONS
            if "hook_failures" in m.name
        ]
        assert matches, "hook_failures migration must be registered in MIGRATIONS"
        assert matches[0][0] >= "4.9.10", (
            f"hook_failures migration must be introduced in >= 4.9.10, got {matches[0][0]!r}"
        )


class TestMigrateHookFailuresBatchColumns:
    """``migrate_hook_failures_batch_columns`` adds RDR-095 batch shape columns
    to ``hook_failures`` without disturbing existing scalar rows."""

    def _fresh_db_with_hook_failures(self) -> sqlite3.Connection:
        from nexus.db.migrations import migrate_hook_failures
        conn = sqlite3.connect(":memory:")
        migrate_hook_failures(conn)
        return conn

    def test_adds_both_columns(self) -> None:
        from nexus.db.migrations import migrate_hook_failures_batch_columns

        conn = self._fresh_db_with_hook_failures()
        migrate_hook_failures_batch_columns(conn)

        cols = {
            r[1]: (r[2], r[3], r[4])  # name -> (type, notnull, default)
            for r in conn.execute("PRAGMA table_info(hook_failures)").fetchall()
        }
        assert "batch_doc_ids" in cols
        assert cols["batch_doc_ids"][0] == "TEXT"
        assert cols["batch_doc_ids"][1] == 0  # nullable

        assert "is_batch" in cols
        assert cols["is_batch"][0] == "INTEGER"
        assert cols["is_batch"][1] == 1  # NOT NULL
        assert cols["is_batch"][2] == "0"  # default 0

    def test_preserves_scalar_rows(self) -> None:
        """Existing scalar-doc_id rows survive migration unchanged."""
        from nexus.db.migrations import migrate_hook_failures_batch_columns

        conn = self._fresh_db_with_hook_failures()
        conn.execute(
            "INSERT INTO hook_failures (doc_id, collection, hook_name, error) "
            "VALUES (?, ?, ?, ?)",
            ("doc-pre", "knowledge__delos", "taxonomy_assign_batch_hook", "boom"),
        )
        conn.commit()

        migrate_hook_failures_batch_columns(conn)

        row = conn.execute(
            "SELECT doc_id, collection, hook_name, error, batch_doc_ids, is_batch "
            "FROM hook_failures"
        ).fetchone()
        assert row[0] == "doc-pre"
        assert row[1] == "knowledge__delos"
        assert row[2] == "taxonomy_assign_batch_hook"
        assert row[3] == "boom"
        assert row[4] is None  # batch_doc_ids unset for legacy rows
        assert row[5] == 0  # is_batch default

    def test_idempotent(self) -> None:
        from nexus.db.migrations import migrate_hook_failures_batch_columns

        conn = self._fresh_db_with_hook_failures()
        migrate_hook_failures_batch_columns(conn)
        # Second call must not raise.
        migrate_hook_failures_batch_columns(conn)

        cols = {
            r[1]
            for r in conn.execute("PRAGMA table_info(hook_failures)").fetchall()
        }
        assert "batch_doc_ids" in cols
        assert "is_batch" in cols

    def test_noop_when_table_missing(self) -> None:
        """Runs cleanly even if hook_failures has not been created yet."""
        from nexus.db.migrations import migrate_hook_failures_batch_columns

        conn = sqlite3.connect(":memory:")
        # No hook_failures table at all.
        migrate_hook_failures_batch_columns(conn)  # must not raise

        tables = {
            r[0]
            for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        }
        assert "hook_failures" not in tables

    def test_batch_insert_after_migration(self) -> None:
        """After migration, a batch-shape failure row writes cleanly."""
        from nexus.db.migrations import migrate_hook_failures_batch_columns

        conn = self._fresh_db_with_hook_failures()
        migrate_hook_failures_batch_columns(conn)

        conn.execute(
            "INSERT INTO hook_failures "
            "(doc_id, collection, hook_name, error, batch_doc_ids, is_batch) "
            "VALUES (?, ?, ?, ?, ?, 1)",
            ("doc-1", "code__nexus", "chash_dual_write_batch_hook", "boom",
             '["doc-1","doc-2","doc-3"]'),
        )
        conn.commit()

        row = conn.execute(
            "SELECT doc_id, batch_doc_ids, is_batch FROM hook_failures"
        ).fetchone()
        assert row[0] == "doc-1"
        assert row[1] == '["doc-1","doc-2","doc-3"]'
        assert row[2] == 1

    def test_in_migrations_list(self) -> None:
        from nexus.db.migrations import MIGRATIONS

        matches = [
            (m.introduced, m.name) for m in MIGRATIONS
            if "hook_failures.batch_doc_ids" in m.name
        ]
        assert matches, (
            "hook_failures batch columns migration must be registered in MIGRATIONS"
        )
        assert matches[0][0] >= "4.14.1", (
            "hook_failures batch columns must be introduced in >= 4.14.1, "
            f"got {matches[0][0]!r}"
        )


# ── _backfill_plan_dimensions (RDR-092 Phase 0d.1) ──────────────────────────


def _make_plans_schema(conn: sqlite3.Connection) -> None:
    """Minimal plans schema with the RDR-078 dimensional columns."""
    from nexus.db.migrations import _add_plan_dimensional_identity
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS plans (
            id INTEGER PRIMARY KEY,
            project TEXT NOT NULL DEFAULT '',
            query TEXT NOT NULL,
            plan_json TEXT NOT NULL,
            outcome TEXT DEFAULT 'success',
            tags TEXT DEFAULT '',
            created_at TEXT NOT NULL
        );
    """)
    _add_plan_dimensional_identity(conn)
    conn.commit()


def _insert_plan(
    conn: sqlite3.Connection,
    *,
    query: str,
    tags: str = "",
    dimensions: str | None = None,
    verb: str | None = None,
    name: str | None = None,
    scope: str | None = None,
) -> int:
    cursor = conn.execute(
        "INSERT INTO plans "
        "(query, plan_json, outcome, tags, created_at, "
        "verb, scope, dimensions, name) "
        "VALUES (?, '{}', 'success', ?, datetime('now'), ?, ?, ?, ?)",
        (query, tags, verb, scope, dimensions, name),
    )
    conn.commit()
    return cursor.lastrowid


class TestBackfillPlanDimensions:
    """RDR-092 Phase 0d.1: backfill verb/name/dimensions for NULL rows.

    Contract:
      * Rows with ``dimensions IS NULL`` get verb/name/dimensions filled
        by a 29-stem verb-from-stem heuristic + wh-fallback on the
        ``query`` column text.
      * High-confidence matches tag ``,backfill``; zero-score rows
        (wh-fallback) tag ``,backfill-low-conf`` so ``nx plan repair``
        can prioritise them for manual review.
      * Rows with ``dimensions IS NOT NULL`` are untouched (authored
        rows, including already-backfilled rows on re-run).
    """

    def test_backfill_plan_dimensions_infers_verb(self) -> None:
        """Stem matches select the expected verb for each rule family."""
        from nexus.db.migrations import _backfill_plan_dimensions

        conn = sqlite3.connect(":memory:")
        _make_plans_schema(conn)

        fixtures: list[tuple[str, str, str]] = [
            # (query, expected verb, row-id label)
            ("find documents about indexing", "research", "research-find"),
            ("analyze chunk throughput", "analyze", "analyze-direct"),
            ("compare two retrieval backends", "analyze", "analyze-compare"),
            ("review the auth refactor", "review", "review-direct"),
            ("debug the chroma timeout", "debug", "debug-direct"),
            ("document the cache protocol", "document", "document-direct"),
        ]
        ids = {}
        for query, _verb, label in fixtures:
            ids[label] = _insert_plan(conn, query=query)

        _backfill_plan_dimensions(conn)

        for query, expected_verb, label in fixtures:
            row = conn.execute(
                "SELECT verb, name, dimensions, tags FROM plans WHERE id = ?",
                (ids[label],),
            ).fetchone()
            assert row[0] == expected_verb, (
                f"{query!r}: expected verb={expected_verb!r}, got {row[0]!r}"
            )
            assert row[1], f"{query!r}: name must be populated"
            assert row[2], f"{query!r}: dimensions JSON must be populated"
            assert "backfill" in (row[3] or "")

    def test_backfill_plan_dimensions_idempotent(self) -> None:
        """Re-running the migration is a no-op on already-backfilled rows."""
        from nexus.db.migrations import _backfill_plan_dimensions

        conn = sqlite3.connect(":memory:")
        _make_plans_schema(conn)
        rid = _insert_plan(conn, query="analyze the ranker output")

        _backfill_plan_dimensions(conn)
        first = conn.execute(
            "SELECT verb, name, dimensions, tags FROM plans WHERE id = ?",
            (rid,),
        ).fetchone()
        assert first[0] == "analyze"
        assert "backfill" in (first[3] or "")

        # Second run: state must be stable and tags must not duplicate.
        _backfill_plan_dimensions(conn)
        second = conn.execute(
            "SELECT verb, name, dimensions, tags FROM plans WHERE id = ?",
            (rid,),
        ).fetchone()
        assert second == first
        # No double-tagging.
        assert (second[3] or "").count("backfill") == 1

    def test_backfill_preserves_authored_verbs(self) -> None:
        """Rows with dimensions IS NOT NULL are untouched."""
        from nexus.db.migrations import _backfill_plan_dimensions

        conn = sqlite3.connect(":memory:")
        _make_plans_schema(conn)
        # Simulate an authored row.
        rid = _insert_plan(
            conn,
            query="analyze lineage",
            tags="builtin-template,rdr-078,research",
            verb="research",
            scope="global",
            name="default",
            dimensions='{"scope":"global","verb":"research"}',
        )

        _backfill_plan_dimensions(conn)

        row = conn.execute(
            "SELECT verb, name, dimensions, tags FROM plans WHERE id = ?",
            (rid,),
        ).fetchone()
        # Not rewritten by the heuristic (query says 'analyze' but
        # authored verb was 'research'); tags not appended.
        assert row[0] == "research"
        assert row[1] == "default"
        assert row[2] == '{"scope":"global","verb":"research"}'
        assert "backfill" not in row[3]

    def test_backfill_low_conf_flagging(self) -> None:
        """Rows that hit only the wh-fallback carry backfill-low-conf."""
        from nexus.db.migrations import _backfill_plan_dimensions

        conn = sqlite3.connect(":memory:")
        _make_plans_schema(conn)
        # A query with no stem match — only the wh-word triggers.
        rid = _insert_plan(conn, query="what about the graph")

        _backfill_plan_dimensions(conn)

        row = conn.execute(
            "SELECT verb, tags FROM plans WHERE id = ?", (rid,),
        ).fetchone()
        assert row[0], "verb must be populated even on low-conf"
        assert "backfill-low-conf" in (row[1] or "")

    def test_backfill_registered_in_migrations_list(self) -> None:
        """The migration appears in MIGRATIONS at a >= 4.9.12 version."""
        from nexus.db.migrations import MIGRATIONS

        matches = [
            (m.introduced, m.name) for m in MIGRATIONS
            if "backfill" in m.name.lower() and "plan" in m.name.lower()
        ]
        assert matches, (
            "backfill plan-dimensions migration must be in MIGRATIONS"
        )
        # Must ship AFTER the dimensional-identity migration (4.4.0).
        assert matches[0][0] >= "4.9.12", (
            f"must be introduced in >= 4.9.12, got {matches[0][0]!r}"
        )

    def test_backfill_collision_resolves_via_row_id_suffix(self) -> None:
        """RDR-092 code-review C-1: two NULL-dimension rows whose
        queries collapse to the same kebab name must not crash the
        migration on the UNIQUE(project, dimensions) partial index.
        The second row lands with its strategy suffixed by row id
        so both rows land with distinct, deterministic identities.
        """
        from nexus.db.migrations import _backfill_plan_dimensions

        conn = sqlite3.connect(":memory:")
        _make_plans_schema(conn)
        # Two queries that reduce to the same content tokens after
        # stop-word stripping.
        rid1 = _insert_plan(conn, query="find the documents for author")
        rid2 = _insert_plan(conn, query="find documents by author")

        _backfill_plan_dimensions(conn)  # must not raise

        rows = conn.execute(
            "SELECT id, name, dimensions FROM plans ORDER BY id ASC"
        ).fetchall()
        assert len(rows) == 2
        first_name, second_name = rows[0][1], rows[1][1]
        first_dims, second_dims = rows[0][2], rows[1][2]
        # Both rows got dimensions populated (no skipped rows).
        assert first_dims, "first row must carry dimensions"
        assert second_dims, "collision must not leave dimensions NULL"
        # The colliding row carries the row_id suffix.
        assert first_name != second_name
        assert str(rid2) in second_name, (
            f"collision row's name must include row_id={rid2}, got {second_name!r}"
        )
        # Dimensions JSON differs (unique partial-index satisfied).
        assert first_dims != second_dims

    def test_backfill_sentinel_name_does_not_collide(self) -> None:
        """Queries with no alphanumeric tokens fall to the sentinel
        ``backfilled-plan`` name; multiple such rows must each get a
        deterministic unique identity via the row-id suffix path
        (test-validator note: earlier fixture used stop-word-only
        queries whose raw-token fallback still differed; replace with
        queries whose ``tokens`` list is actually empty).
        """
        from nexus.db.migrations import _backfill_plan_dimensions

        conn = sqlite3.connect(":memory:")
        _make_plans_schema(conn)
        # Queries with no regex-matched tokens trigger the
        # 'backfilled-plan' sentinel in _derive_plan_name_from_query.
        rid1 = _insert_plan(conn, query="!!!")
        rid2 = _insert_plan(conn, query="...")
        rid3 = _insert_plan(conn, query="???")

        _backfill_plan_dimensions(conn)

        rows = conn.execute(
            "SELECT id, name, dimensions FROM plans ORDER BY id ASC"
        ).fetchall()
        names = [row[1] for row in rows]
        dims = [row[2] for row in rows]
        # All three rows land with unique names and unique dimensions.
        assert len(set(names)) == 3, f"names should all differ, got {names!r}"
        assert len(set(dims)) == 3, "dimensions JSON must be unique per row"
        # First row keeps the plain sentinel; 2nd and 3rd carry row_id
        # suffixes (the in-memory ``claimed`` set catches them because
        # the 1st row's UPDATE has not been persisted at 2nd row's
        # pre-check time).
        assert names[0] == "backfilled-plan"
        assert str(rid2) in names[1]
        assert str(rid3) in names[2]

    def test_backfill_within_loop_claimed_set_fires(self) -> None:
        """Three queries that derive to the same kebab name exercise
        the in-memory ``claimed`` fallback: the 2nd and 3rd rows both
        collide, and only the 1st has been persisted when the 2nd's
        SELECT pre-check runs, so the ``key in claimed`` branch is
        the one that catches the 3rd.
        """
        from nexus.db.migrations import _backfill_plan_dimensions

        conn = sqlite3.connect(":memory:")
        _make_plans_schema(conn)
        # All three strip down to 'find-documents-author' after stop-
        # word filter on the first 5 content tokens.
        _insert_plan(conn, query="find the documents for author")
        _insert_plan(conn, query="find documents by author")
        _insert_plan(conn, query="find documents about author")

        _backfill_plan_dimensions(conn)

        rows = conn.execute(
            "SELECT dimensions FROM plans ORDER BY id ASC"
        ).fetchall()
        dims = [r[0] for r in rows]
        assert len(set(dims)) == 3, (
            f"all three rows must land with distinct dimensions, got {dims!r}"
        )

    def test_backfill_collision_idempotent_on_rerun(self) -> None:
        """A second run against a DB that already contains a
        collision-resolved row must leave the suffixed identity
        unchanged: the NULL-dimension filter skips it entirely.
        """
        from nexus.db.migrations import _backfill_plan_dimensions

        conn = sqlite3.connect(":memory:")
        _make_plans_schema(conn)
        _insert_plan(conn, query="find the documents for author")
        rid2 = _insert_plan(conn, query="find documents by author")

        _backfill_plan_dimensions(conn)
        first = conn.execute(
            "SELECT name, dimensions, tags FROM plans WHERE id = ?",
            (rid2,),
        ).fetchone()

        _backfill_plan_dimensions(conn)
        second = conn.execute(
            "SELECT name, dimensions, tags FROM plans WHERE id = ?",
            (rid2,),
        ).fetchone()

        assert first == second, (
            "collision-resolved row must be stable across reruns"
        )
        # Collision row carries the row_id suffix on both runs.
        assert str(rid2) in first[0]
# ── _add_plan_match_text_column (RDR-092 Phase 3.1) ─────────────────────────


class TestAddPlanMatchTextColumn:
    """RDR-092 Phase 3.1: add ``match_text`` column to plans + rebuild
    ``plans_fts`` so the T2 FTS lane indexes the same hybrid shape the
    T1 cosine cache uses.

    Idempotent. Must run after ``_add_plan_dimensional_identity`` so
    verb/name/scope are present to feed the backfill synthesiser.
    """

    def _base_schema(self, conn: sqlite3.Connection) -> None:
        """Plans table + dimensional columns, no match_text yet."""
        from nexus.db.migrations import _add_plan_dimensional_identity
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS plans (
                id INTEGER PRIMARY KEY,
                project TEXT NOT NULL DEFAULT '',
                query TEXT NOT NULL,
                plan_json TEXT NOT NULL,
                outcome TEXT DEFAULT 'success',
                tags TEXT DEFAULT '',
                created_at TEXT NOT NULL
            );
            CREATE VIRTUAL TABLE IF NOT EXISTS plans_fts USING fts5(
                query, tags, project, content=plans, content_rowid='id'
            );
        """)
        _add_plan_dimensional_identity(conn)
        conn.commit()

    def test_adds_column_idempotent(self) -> None:
        from nexus.db.migrations import _add_plan_match_text_column

        conn = sqlite3.connect(":memory:")
        self._base_schema(conn)

        _add_plan_match_text_column(conn)
        _add_plan_match_text_column(conn)  # no raise

        cols = {r[1] for r in conn.execute("PRAGMA table_info(plans)").fetchall()}
        assert "match_text" in cols

    def test_fts_table_rebuilt_with_match_text(self) -> None:
        from nexus.db.migrations import _add_plan_match_text_column

        conn = sqlite3.connect(":memory:")
        self._base_schema(conn)
        _add_plan_match_text_column(conn)

        row = conn.execute(
            "SELECT sql FROM sqlite_master WHERE type='table' AND name='plans_fts'"
        ).fetchone()
        assert row is not None
        assert "match_text" in row[0], (
            f"plans_fts must index match_text, got: {row[0]!r}"
        )

    def test_backfill_populates_dimensional_rows(self) -> None:
        """Rows with verb/name/scope get a hybrid match_text; legacy
        NULL-dimension rows keep the raw query text.
        """
        from nexus.db.migrations import _add_plan_match_text_column

        conn = sqlite3.connect(":memory:")
        self._base_schema(conn)

        conn.execute(
            "INSERT INTO plans "
            "(query, plan_json, outcome, tags, created_at, "
            "verb, scope, name) "
            "VALUES (?, '{}', 'success', '', datetime('now'), ?, ?, ?)",
            ("Find documents attributed to a specific author.",
             "research", "global", "find-by-author"),
        )
        conn.execute(
            "INSERT INTO plans "
            "(query, plan_json, outcome, tags, created_at) "
            "VALUES (?, '{}', 'success', '', datetime('now'))",
            ("legacy plan text",),
        )
        conn.commit()

        _add_plan_match_text_column(conn)

        dimensional = conn.execute(
            "SELECT match_text FROM plans WHERE name = 'find-by-author'"
        ).fetchone()
        assert dimensional is not None
        assert "research find-by-author scope global" in dimensional[0]

        legacy = conn.execute(
            "SELECT match_text FROM plans WHERE query = 'legacy plan text'"
        ).fetchone()
        assert legacy is not None
        assert legacy[0] == "legacy plan text"

    def test_backfill_idempotent(self) -> None:
        """Re-running the migration does not duplicate or corrupt
        already-synthesised match_text values.
        """
        from nexus.db.migrations import _add_plan_match_text_column

        conn = sqlite3.connect(":memory:")
        self._base_schema(conn)
        conn.execute(
            "INSERT INTO plans "
            "(query, plan_json, outcome, tags, created_at, "
            "verb, scope, name) "
            "VALUES (?, '{}', 'success', '', datetime('now'), ?, ?, ?)",
            ("Analyze lineage across prose and code.",
             "analyze", "global", "default"),
        )
        conn.commit()

        _add_plan_match_text_column(conn)
        first = conn.execute("SELECT match_text FROM plans").fetchone()[0]

        _add_plan_match_text_column(conn)
        second = conn.execute("SELECT match_text FROM plans").fetchone()[0]

        assert first == second
        assert "analyze default scope global" in first

    def test_registered_in_migrations_list(self) -> None:
        from nexus.db.migrations import MIGRATIONS

        matches = [
            (m.introduced, m.name) for m in MIGRATIONS
            if "match_text" in m.name.lower()
        ]
        assert matches, (
            "plan-match-text migration must be in MIGRATIONS"
        )
        assert matches[0][0] >= "4.9.13", (
            f"must be introduced in >= 4.9.13, got {matches[0][0]!r}"
        )

    def test_interrupted_upgrade_recovers(self) -> None:
        """RDR-092 code-review S-1: if a process dies between the
        ALTER TABLE (column add) and the FTS rebuild executescript,
        the column exists but ``plans_fts`` is gone. The migration's
        guard must detect this mid-state on retry and re-run the
        backfill + FTS rebuild rather than short-circuiting on the
        ``match_text in cols`` check.
        """
        from nexus.db.migrations import _add_plan_match_text_column

        conn = sqlite3.connect(":memory:")
        self._base_schema(conn)

        # Seed one dimensional row so the backfill has something to do.
        conn.execute(
            "INSERT INTO plans "
            "(query, plan_json, outcome, tags, created_at, "
            "verb, scope, name) "
            "VALUES (?, '{}', 'success', '', datetime('now'), ?, ?, ?)",
            ("Trace the citation chain surrounding a document.",
             "research", "global", "citation-traversal"),
        )
        conn.commit()

        # Simulate the interrupted state: column added (ALTER
        # succeeded) but plans_fts dropped and not yet recreated.
        conn.execute(
            "ALTER TABLE plans ADD COLUMN match_text TEXT "
            "NOT NULL DEFAULT ''"
        )
        conn.executescript("""
            DROP TRIGGER IF EXISTS plans_ai;
            DROP TRIGGER IF EXISTS plans_ad;
            DROP TRIGGER IF EXISTS plans_au;
            DROP TABLE  IF EXISTS plans_fts;
        """)
        conn.commit()
        # Confirm the setup: column present, FTS gone, match_text empty.
        cols = {r[1] for r in conn.execute(
            "PRAGMA table_info(plans)"
        ).fetchall()}
        assert "match_text" in cols
        fts_row = conn.execute(
            "SELECT name FROM sqlite_master "
            "WHERE type='table' AND name='plans_fts'"
        ).fetchone()
        assert fts_row is None
        row = conn.execute(
            "SELECT match_text FROM plans"
        ).fetchone()
        assert row[0] == "", "setup invariant: match_text must be ''"

        # Re-run the migration. The has_fts guard should detect the
        # mid-state and fall through to rebuild FTS + backfill.
        _add_plan_match_text_column(conn)

        fts_row = conn.execute(
            "SELECT name FROM sqlite_master "
            "WHERE type='table' AND name='plans_fts'"
        ).fetchone()
        assert fts_row is not None, (
            "plans_fts must be recreated after interrupted-upgrade retry"
        )
        row = conn.execute(
            "SELECT match_text FROM plans"
        ).fetchone()
        assert "research citation-traversal scope global" in row[0], (
            f"backfill must synthesize match_text on retry, got {row[0]!r}"
        )


# ── _retire_legacy_operation_shape_plans (nexus-4m9b) ────────────────────────


class TestRetireLegacyOperationShapePlans:
    """nexus-4m9b: RDR-092 Phase 0a retired the ``_PLAN_TEMPLATES`` seed
    array but did not migrate the rows it had previously seeded. Those
    rows, plus pre-RDR-078 user ad-hoc plans, carry step entries shaped
    like ``{"operation": "X", "params": {...}}`` that ``plan_run``
    cannot dispatch. The migration deletes them so modern replacements
    win during plan-match routing.
    """

    def _schema(self, conn: sqlite3.Connection) -> None:
        conn.executescript("""
            CREATE TABLE plans (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                query TEXT NOT NULL,
                plan_json TEXT NOT NULL,
                outcome TEXT DEFAULT 'success',
                tags TEXT DEFAULT '',
                created_at TEXT NOT NULL DEFAULT (datetime('now'))
            );
        """)
        conn.commit()

    def test_deletes_legacy_operation_shape(self) -> None:
        from nexus.db.migrations import _retire_legacy_operation_shape_plans

        conn = sqlite3.connect(":memory:")
        self._schema(conn)
        legacy = json.dumps({
            "steps": [
                {"step": 1, "operation": "catalog_search", "params": {"author": "x"}},
            ],
        })
        modern = json.dumps({
            "steps": [{"tool": "search", "args": {"query": "x"}}],
        })
        conn.execute(
            "INSERT INTO plans (query, plan_json) VALUES (?, ?)",
            ("legacy", legacy),
        )
        conn.execute(
            "INSERT INTO plans (query, plan_json) VALUES (?, ?)",
            ("modern", modern),
        )
        conn.commit()

        _retire_legacy_operation_shape_plans(conn)

        remaining = [r[0] for r in conn.execute(
            "SELECT query FROM plans ORDER BY id"
        ).fetchall()]
        assert remaining == ["modern"]

    def test_idempotent(self) -> None:
        from nexus.db.migrations import _retire_legacy_operation_shape_plans

        conn = sqlite3.connect(":memory:")
        self._schema(conn)
        conn.execute(
            "INSERT INTO plans (query, plan_json) VALUES (?, ?)",
            ("legacy",
             '{"steps": [{"operation": "search", "params": {}}]}'),
        )
        conn.commit()

        _retire_legacy_operation_shape_plans(conn)
        _retire_legacy_operation_shape_plans(conn)  # no raise

        count = conn.execute("SELECT COUNT(*) FROM plans").fetchone()[0]
        assert count == 0

    def test_preserves_rows_that_mention_operation_in_args(self) -> None:
        """A modern plan whose args payload happens to contain the word
        ``operation`` (e.g. a ``purpose: reference-operation`` string)
        must not be retired. The post-parse ``has_tool`` check decides.
        """
        from nexus.db.migrations import _retire_legacy_operation_shape_plans

        conn = sqlite3.connect(":memory:")
        self._schema(conn)
        plan_json = json.dumps({
            "steps": [{
                "tool": "traverse",
                "args": {"purpose": "reference-operation"},
            }],
        })
        conn.execute(
            "INSERT INTO plans (query, plan_json) VALUES (?, ?)",
            ("false-positive guard", plan_json),
        )
        conn.commit()

        _retire_legacy_operation_shape_plans(conn)

        count = conn.execute("SELECT COUNT(*) FROM plans").fetchone()[0]
        assert count == 1

    def test_registered_in_migrations_list(self) -> None:
        from nexus.db.migrations import MIGRATIONS

        matches = [
            (m.introduced, m.name) for m in MIGRATIONS
            if "legacy" in m.name.lower() and "operation" in m.name.lower()
        ]
        assert matches, (
            "retire-legacy-operation-shape migration must be in MIGRATIONS"
        )
        assert matches[0][0] >= "4.10.1", (
            f"must be introduced at >= 4.10.1, got {matches[0][0]!r}"
        )


# ── _backfill_builtin_bindings (nexus-uyc6) ──────────────────────────────────


class TestBackfillBuiltinBindings:
    """nexus-uyc6: the seed_loader fix merges binding declarations into
    plan_json at save_plan time, but existing rows short-circuit via
    ``get_plan_by_dimensions`` on re-seed. This migration patches the
    declarations into pre-existing builtin rows by matching
    ``(verb, scope, strategy)`` against the shipping YAMLs.
    """

    def _schema(self, conn: sqlite3.Connection) -> None:
        conn.executescript("""
            CREATE TABLE plans (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                query TEXT NOT NULL,
                plan_json TEXT NOT NULL,
                outcome TEXT DEFAULT 'success',
                tags TEXT DEFAULT '',
                created_at TEXT NOT NULL DEFAULT (datetime('now')),
                project TEXT NOT NULL DEFAULT '',
                verb TEXT,
                scope TEXT,
                dimensions TEXT,
                name TEXT
            );
        """)
        conn.commit()

    def _write_yaml(self, path, name, verb, scope, strategy,
                    required, optional):
        import yaml as _yaml
        doc = {
            "name": name,
            "description": f"{verb} / {strategy} test template",
            "dimensions": {"verb": verb, "scope": scope, "strategy": strategy},
            "plan_json": {"steps": [{"tool": "search", "args": {}}]},
        }
        if required:
            doc["required_bindings"] = required
        if optional:
            doc["optional_bindings"] = optional
        path.write_text(_yaml.safe_dump(doc))

    def test_backfills_matching_row_by_dimensions(self, tmp_path, monkeypatch):
        from nexus.db.migrations import _backfill_builtin_bindings

        yaml_dir = tmp_path / "nx" / "plans" / "builtin"
        yaml_dir.mkdir(parents=True)
        self._write_yaml(
            yaml_dir / "analyze.yml",
            name="default", verb="analyze", scope="global",
            strategy="default", required=["area", "criterion"],
            optional=["limit"],
        )

        # Point the migration's repo-root fallback at our tmp layout.
        # Migration walks __file__.parents[3]/nx/plans/builtin; a monkeypatch
        # of the module-level ``Path`` lookup in the migration is brittle,
        # so instead pivot on ``importlib.resources`` by setting up a
        # resource stub. Simpler: mock ``importlib.resources.files`` to
        # return an object whose ``joinpath`` chain resolves to tmp_path.
        monkeypatch.chdir(tmp_path)

        class _FakeResource:
            def __init__(self, root):
                self._root = root
            def __truediv__(self, segment):
                return _FakeResource(self._root / segment)
            def is_dir(self):
                return self._root.is_dir()
            def iterdir(self):
                return self._root.iterdir()

        def fake_files(_pkg):
            # Mirror the migration's expected layout: <pkg> / _resources
            # / plans / builtin. We ship the YAMLs at tmp_path/nx/plans/
            # builtin, so route _resources/plans/builtin to tmp_path/nx/
            # plans/builtin via the FakeResource chain.
            root = tmp_path / "nx"
            # Eat the leading "_resources" segment by returning a resource
            # rooted at tmp_path/nx so the subsequent / "plans" / "builtin"
            # lands correctly. Requires a small shim: intercept the first
            # / and redirect.
            class _RedirectingFakeResource(_FakeResource):
                def __truediv__(self, segment):
                    if segment == "_resources":
                        return _FakeResource(root)
                    return _FakeResource(self._root / segment)
            return _RedirectingFakeResource(root)

        class _FakeAsFile:
            def __init__(self, resource):
                self._resource = resource
            def __enter__(self):
                return self._resource._root
            def __exit__(self, *a):
                return False

        monkeypatch.setattr(
            "importlib.resources.files", fake_files,
        )
        monkeypatch.setattr(
            "importlib.resources.as_file",
            lambda resource: _FakeAsFile(resource),
        )

        conn = sqlite3.connect(":memory:")
        self._schema(conn)
        conn.execute(
            "INSERT INTO plans "
            "(query, plan_json, tags, verb, scope, dimensions, name) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                "Analysis and synthesis",
                '{"steps": [{"tool": "search", "args": {}}]}',
                "builtin-template,rdr-078,analyze",
                "analyze", "global",
                '{"scope":"global","strategy":"default","verb":"analyze"}',
                "default",
            ),
        )
        conn.commit()

        _backfill_builtin_bindings(conn)

        row = conn.execute(
            "SELECT plan_json FROM plans WHERE name='default'"
        ).fetchone()
        parsed = json.loads(row[0])
        assert parsed["required_bindings"] == ["area", "criterion"]
        assert parsed["optional_bindings"] == ["limit"]

    def test_idempotent_when_row_already_has_bindings(self, tmp_path, monkeypatch):
        from nexus.db.migrations import _backfill_builtin_bindings

        conn = sqlite3.connect(":memory:")
        self._schema(conn)
        already = json.dumps({
            "steps": [{"tool": "search", "args": {}}],
            "required_bindings": ["area"],
        })
        conn.execute(
            "INSERT INTO plans "
            "(query, plan_json, tags, verb, scope, dimensions, name) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            ("x", already, "builtin-template", "analyze", "global",
             '{"verb":"analyze","scope":"global","strategy":"default"}',
             "default"),
        )
        conn.commit()

        _backfill_builtin_bindings(conn)

        row = conn.execute(
            "SELECT plan_json FROM plans WHERE name='default'"
        ).fetchone()
        assert row[0] == already

    def test_skips_non_builtin_rows(self, tmp_path, monkeypatch):
        """User ad-hoc rows (no ``builtin`` in tags) must not be touched
        even if a dimension match exists in the shipping YAMLs.
        """
        from nexus.db.migrations import _backfill_builtin_bindings

        conn = sqlite3.connect(":memory:")
        self._schema(conn)
        conn.execute(
            "INSERT INTO plans "
            "(query, plan_json, tags, verb, scope, dimensions, name) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            ("ad-hoc", '{"steps": []}', "ad-hoc,grown",
             "analyze", "global",
             '{"verb":"analyze","scope":"global","strategy":"default"}',
             "default"),
        )
        conn.commit()

        # Even with missing YAMLs the migration should no-op cleanly
        # on non-builtin rows. Call it without fixture YAMLs.
        _backfill_builtin_bindings(conn)

        row = conn.execute(
            "SELECT plan_json FROM plans WHERE name='default'"
        ).fetchone()
        assert row[0] == '{"steps": []}'

    def test_registered_in_migrations_list(self):
        from nexus.db.migrations import MIGRATIONS

        matches = [
            (m.introduced, m.name) for m in MIGRATIONS
            if "backfill" in m.name.lower() and "binding" in m.name.lower()
        ]
        assert matches, (
            "backfill-builtin-bindings migration must be in MIGRATIONS"
        )
        assert matches[0][0] >= "4.10.2", (
            f"must be introduced at >= 4.10.2, got {matches[0][0]!r}"
        )
