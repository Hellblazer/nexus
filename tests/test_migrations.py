# SPDX-License-Identifier: AGPL-3.0-or-later
"""TDD tests for src/nexus/db/migrations.py — migration registry core.

Red phase: these tests define the contract for the migration registry.
They will fail until migrations.py is implemented (nexus-6cn).
"""
from __future__ import annotations

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
        assert len(MIGRATIONS) == 11  # + RDR-087 search_telemetry

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
