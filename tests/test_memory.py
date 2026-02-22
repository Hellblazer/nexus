"""AC3-AC5: nx memory put/get/search/expire behavior."""
from datetime import UTC, datetime, timedelta

from nexus.db.t2 import T2Database


def test_memory_put_upsert(db: T2Database) -> None:
    """Put twice with same project+title → single row, updated content."""
    db.put(project="proj", title="file.md", content="first")
    db.put(project="proj", title="file.md", content="updated")

    count = db.conn.execute(
        "SELECT COUNT(*) FROM memory WHERE project='proj' AND title='file.md'"
    ).fetchone()[0]
    assert count == 1

    content = db.conn.execute(
        "SELECT content FROM memory WHERE project='proj' AND title='file.md'"
    ).fetchone()[0]
    assert content == "updated"


def test_memory_get_by_project_title(db: T2Database) -> None:
    """Deterministic retrieval by (project, title) returns correct entry."""
    db.put(project="proj_a", title="notes.md", content="hello world")

    result = db.get(project="proj_a", title="notes.md")
    assert result is not None
    assert result["content"] == "hello world"
    assert result["project"] == "proj_a"
    assert result["title"] == "notes.md"


def test_memory_get_by_id(db: T2Database) -> None:
    """Retrieval by numeric ID returns correct entry."""
    row_id = db.put(project="p", title="x.md", content="by id")
    result = db.get(id=row_id)
    assert result is not None
    assert result["content"] == "by id"


def test_memory_get_missing_returns_none(db: T2Database) -> None:
    result = db.get(project="no", title="such.md")
    assert result is None


def test_memory_search_fts5(db: T2Database) -> None:
    """Insert content; search by keyword; ranked results returned."""
    db.put(project="p", title="alpha.md", content="The quick brown fox")
    db.put(project="p", title="beta.md", content="A lazy dog sleeping")
    db.put(project="p", title="gamma.md", content="The quick fox jumps high")

    results = db.search("quick fox")
    titles = {r["title"] for r in results}
    # Both docs containing "quick" AND "fox" must appear
    assert "alpha.md" in titles
    assert "gamma.md" in titles
    assert "beta.md" not in titles


def test_memory_search_scoped_to_project(db: T2Database) -> None:
    """Search scoped to a project excludes other projects."""
    db.put(project="proj_a", title="a.md", content="authentication token")
    db.put(project="proj_b", title="b.md", content="authentication token")

    results = db.search("authentication", project="proj_a")
    assert all(r["project"] == "proj_a" for r in results)
    assert len(results) == 1


def test_memory_expire_ttl(db: T2Database) -> None:
    """Entry with expired TTL is removed by expire()."""
    db.put(project="proj", title="old.md", content="stale", ttl=1)

    # Backdate the timestamp by 2 days to simulate time passing
    past = (datetime.now(UTC) - timedelta(days=2)).strftime("%Y-%m-%dT%H:%M:%SZ")
    db.conn.execute("UPDATE memory SET timestamp=? WHERE title='old.md'", (past,))
    db.conn.commit()

    assert db.conn.execute("SELECT COUNT(*) FROM memory WHERE title='old.md'").fetchone()[0] == 1

    deleted = db.expire()
    assert deleted == 1
    assert db.conn.execute("SELECT COUNT(*) FROM memory WHERE title='old.md'").fetchone()[0] == 0


def test_memory_expire_permanent_not_deleted(db: T2Database) -> None:
    """Entry with ttl=None (permanent) is NOT removed by expire()."""
    db.put(project="proj", title="perm.md", content="keep forever", ttl=None)

    past = (datetime.now(UTC) - timedelta(days=365)).strftime("%Y-%m-%dT%H:%M:%SZ")
    db.conn.execute("UPDATE memory SET timestamp=? WHERE title='perm.md'", (past,))
    db.conn.commit()

    db.expire()
    assert db.conn.execute("SELECT COUNT(*) FROM memory WHERE title='perm.md'").fetchone()[0] == 1


def test_memory_list_by_project(db: T2Database) -> None:
    """list_entries filtered by project returns only matching entries."""
    db.put(project="proj_a", title="x.md", content="x")
    db.put(project="proj_a", title="y.md", content="y")
    db.put(project="proj_b", title="z.md", content="z")

    entries = db.list_entries(project="proj_a")
    assert len(entries) == 2
    titles = {e["title"] for e in entries}
    assert titles == {"x.md", "y.md"}
