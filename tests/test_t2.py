"""Tests for T2Database context manager support and core database operations."""
import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from nexus.db.t2 import T2Database


def test_t2database_context_manager_closes_on_exit(tmp_path: Path) -> None:
    """T2Database used as a context manager closes the connection on __exit__."""
    db_path = tmp_path / "cm_test.db"
    with T2Database(db_path) as db:
        # Connection is usable inside the block
        row_id = db.put(project="test", title="cm-entry", content="hello context manager")
        assert row_id is not None

    # After the block the connection must be closed; any operation raises ProgrammingError
    with pytest.raises(Exception):
        db.conn.execute("SELECT 1")


def test_t2database_context_manager_closes_on_exception(tmp_path: Path) -> None:
    """T2Database context manager closes the connection even when an exception is raised."""
    db_path = tmp_path / "cm_exc_test.db"
    with pytest.raises(ValueError, match="intentional"):
        with T2Database(db_path) as db:
            # Write something to prove the connection was open
            db.put(project="test", title="exc-entry", content="before error")
            raise ValueError("intentional")

    # Connection must be closed despite the exception
    with pytest.raises(Exception):
        db.conn.execute("SELECT 1")


def test_t2database_context_manager_returns_self(tmp_path: Path) -> None:
    """__enter__ returns the T2Database instance itself."""
    db_path = tmp_path / "cm_self_test.db"
    with T2Database(db_path) as db:
        assert isinstance(db, T2Database)


def test_t2database_context_manager_does_not_suppress_exception(tmp_path: Path) -> None:
    """__exit__ must not suppress exceptions (returns None / falsy)."""
    db_path = tmp_path / "cm_nosuppress.db"
    with pytest.raises(RuntimeError, match="propagated"):
        with T2Database(db_path) as db:
            raise RuntimeError("propagated")


# ── get() ValueError ─────────────────────────────────────────────────────────

def test_t2_get_without_id_or_project_title_raises_valueerror(db: T2Database) -> None:
    """get() with neither id nor (project, title) raises ValueError."""
    with pytest.raises(ValueError, match="Provide either id or both project and title"):
        db.get()


def test_t2_get_with_project_only_raises_valueerror(db: T2Database) -> None:
    """get() with project but no title raises ValueError."""
    with pytest.raises(ValueError, match="Provide either id or both project and title"):
        db.get(project="proj")


def test_t2_get_with_title_only_raises_valueerror(db: T2Database) -> None:
    """get() with title but no project raises ValueError."""
    with pytest.raises(ValueError, match="Provide either id or both project and title"):
        db.get(title="some.md")


# ── WAL mode ─────────────────────────────────────────────────────────────────

def test_t2_wal_mode_enabled(db: T2Database) -> None:
    """T2Database opens the SQLite connection in WAL journal mode."""
    mode = db.conn.execute("PRAGMA journal_mode").fetchone()[0]
    assert mode.lower() == "wal"


# ── expire ───────────────────────────────────────────────────────────────────

def test_t2_expire_returns_zero_on_fresh_db(db: T2Database) -> None:
    """expire() on a fresh (empty) database returns 0 deleted rows."""
    deleted = db.expire()
    assert deleted == 0


def test_t2_expire_removes_expired_entries(db: T2Database) -> None:
    """expire() removes entries whose TTL has elapsed based on timestamp age."""
    db.put(project="proj", title="stale.md", content="old data", ttl=1)

    # Backdate the timestamp by 2 days so the 1-day TTL is exceeded
    past = (datetime.now(UTC) - timedelta(days=2)).strftime("%Y-%m-%dT%H:%M:%SZ")
    db.conn.execute("UPDATE memory SET timestamp=? WHERE title='stale.md'", (past,))
    db.conn.commit()

    deleted = db.expire()
    assert deleted == 1
    assert db.get(project="proj", title="stale.md") is None


def test_t2_expire_preserves_unexpired_entries(db: T2Database) -> None:
    """expire() does not remove entries whose TTL has not yet elapsed."""
    db.put(project="proj", title="fresh.md", content="recent data", ttl=30)

    deleted = db.expire()
    assert deleted == 0
    assert db.get(project="proj", title="fresh.md") is not None


# ── list_entries with agent filter ───────────────────────────────────────────

def test_t2_list_entries_with_agent_filter(db: T2Database) -> None:
    """list_entries() filtered by agent returns only entries from that agent."""
    db.put(project="proj", title="a.md", content="aaa", agent="agent-alpha")
    db.put(project="proj", title="b.md", content="bbb", agent="agent-beta")
    db.put(project="proj", title="c.md", content="ccc", agent="agent-alpha")

    alpha_entries = db.list_entries(project="proj", agent="agent-alpha")
    assert len(alpha_entries) == 2
    titles = {e["title"] for e in alpha_entries}
    assert titles == {"a.md", "c.md"}

    beta_entries = db.list_entries(project="proj", agent="agent-beta")
    assert len(beta_entries) == 1
    assert beta_entries[0]["title"] == "b.md"


def test_t2_list_entries_agent_filter_only(db: T2Database) -> None:
    """list_entries() with only agent filter (no project) returns all matching entries."""
    db.put(project="proj_a", title="x.md", content="xxx", agent="shared-agent")
    db.put(project="proj_b", title="y.md", content="yyy", agent="shared-agent")
    db.put(project="proj_c", title="z.md", content="zzz", agent="other-agent")

    entries = db.list_entries(agent="shared-agent")
    assert len(entries) == 2
    titles = {e["title"] for e in entries}
    assert titles == {"x.md", "y.md"}


# ── search_glob ──────────────────────────────────────────────────────────────

def test_t2_search_glob(db: T2Database) -> None:
    """search_glob() returns results matching both FTS query and project GLOB pattern."""
    db.put(project="nexus_pm", title="phase1.md", content="authentication design")
    db.put(project="nexus_active", title="notes.md", content="authentication notes")
    db.put(project="arcaneum_pm", title="arch.md", content="authentication architecture")

    # Search for 'authentication' scoped to *_pm projects
    results = db.search_glob("authentication", "*_pm")
    assert len(results) == 2
    projects = {r["project"] for r in results}
    assert projects == {"nexus_pm", "arcaneum_pm"}


def test_t2_search_glob_no_match(db: T2Database) -> None:
    """search_glob() returns empty list when no projects match the GLOB pattern."""
    db.put(project="nexus_active", title="notes.md", content="some content")

    results = db.search_glob("content", "*_pm")
    assert results == []


# ── delete ───────────────────────────────────────────────────────────────────

def test_t2_delete(db: T2Database) -> None:
    """delete() removes the entry and returns True."""
    db.put(project="proj", title="doomed.md", content="to be deleted")

    assert db.get(project="proj", title="doomed.md") is not None

    deleted = db.delete(project="proj", title="doomed.md")
    assert deleted is True
    assert db.get(project="proj", title="doomed.md") is None


def test_t2_delete_nonexistent_returns_false(db: T2Database) -> None:
    """delete() returns False when the entry does not exist."""
    deleted = db.delete(project="proj", title="never_existed.md")
    assert deleted is False


def test_t2_delete_removes_from_fts_index(db: T2Database) -> None:
    """delete() also removes the entry from the FTS5 index."""
    db.put(project="proj", title="indexed.md", content="searchable unique keyword xyzzy")

    # Verify FTS finds it before deletion
    results = db.search("xyzzy")
    assert len(results) == 1

    db.delete(project="proj", title="indexed.md")

    # FTS should no longer find it
    results = db.search("xyzzy")
    assert len(results) == 0


# ── search_by_tag ────────────────────────────────────────────────────────────

def test_t2_search_by_tag(db: T2Database) -> None:
    """search_by_tag() returns only entries whose tags contain the specified tag."""
    db.put(project="nexus", title="phase1.md", content="authentication design", tags="pm,phase:1")
    db.put(project="nexus", title="notes.md", content="authentication notes", tags="notes")
    db.put(project="arcaneum", title="arch.md", content="authentication architecture", tags="pm,arch")

    results = db.search_by_tag("authentication", "pm")
    assert len(results) == 2
    projects = {r["project"] for r in results}
    assert projects == {"nexus", "arcaneum"}


def test_t2_search_by_tag_boundary_matching(db: T2Database) -> None:
    """search_by_tag() uses boundary matching — 'pm' does not match 'pm-archived'."""
    db.put(project="myrepo", title="active.md", content="active doc", tags="pm,context")
    db.put(project="myrepo", title="archived.md", content="archived doc", tags="pm-archived,context")

    results = db.search_by_tag("doc", "pm")
    assert len(results) == 1
    assert results[0]["title"] == "active.md"


def test_t2_search_by_tag_no_match(db: T2Database) -> None:
    """search_by_tag() returns empty list when no entries match the tag."""
    db.put(project="proj", title="notes.md", content="some content", tags="notes")

    results = db.search_by_tag("content", "pm")
    assert results == []


# ── migrate_pm_namespaces ────────────────────────────────────────────────────

def test_t2_migrate_pm_namespaces(db: T2Database) -> None:
    """migrate_pm_namespaces() renames *_pm projects to bare names for pm-tagged entries."""
    db.put(project="nexus_pm", title="phase1.md", content="pm content", tags="pm,phase:1")
    db.put(project="nexus_pm", title="arch.md", content="pm arch", tags="pm,arch")
    db.put(project="nexus_active", title="notes.md", content="active notes", tags="notes")

    count = db.migrate_pm_namespaces()
    assert count == 2

    # Verify migration
    assert db.get(project="nexus", title="phase1.md") is not None
    assert db.get(project="nexus", title="arch.md") is not None
    # Non-pm entries should be untouched
    assert db.get(project="nexus_active", title="notes.md") is not None


def test_t2_migrate_pm_namespaces_skips_non_pm_tags(db: T2Database) -> None:
    """migrate_pm_namespaces() only migrates entries with 'pm' tag."""
    db.put(project="nexus_pm", title="notes.md", content="not pm tagged", tags="notes")
    db.put(project="nexus_pm", title="pm.md", content="pm tagged", tags="pm")

    count = db.migrate_pm_namespaces()
    assert count == 1

    # Non-pm-tagged entry stays in old namespace
    assert db.get(project="nexus_pm", title="notes.md") is not None
    # PM-tagged entry was migrated
    assert db.get(project="nexus", title="pm.md") is not None


def test_t2_migrate_pm_namespaces_idempotent(db: T2Database) -> None:
    """migrate_pm_namespaces() is idempotent — running twice doesn't double-migrate."""
    db.put(project="nexus_pm", title="phase1.md", content="pm content", tags="pm,phase:1")

    count1 = db.migrate_pm_namespaces()
    assert count1 == 1

    count2 = db.migrate_pm_namespaces()
    assert count2 == 0

    assert db.get(project="nexus", title="phase1.md") is not None
