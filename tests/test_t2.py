"""Tests for T2Database context manager support and core database operations."""
import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from nexus.db.t2 import T2Database, _sanitize_fts5

# Old FTS5 schema (without title) for migration tests
_OLD_FTS_SCHEMA = """\
PRAGMA journal_mode=WAL;

CREATE TABLE IF NOT EXISTS memory (
    id        INTEGER PRIMARY KEY,
    project   TEXT    NOT NULL,
    title     TEXT    NOT NULL,
    session   TEXT,
    agent     TEXT,
    content   TEXT    NOT NULL,
    tags      TEXT,
    timestamp TEXT    NOT NULL,
    ttl       INTEGER
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_memory_project_title ON memory(project, title);
CREATE INDEX        IF NOT EXISTS idx_memory_project       ON memory(project);
CREATE INDEX        IF NOT EXISTS idx_memory_agent         ON memory(agent);
CREATE INDEX        IF NOT EXISTS idx_memory_timestamp     ON memory(timestamp);
CREATE INDEX        IF NOT EXISTS idx_memory_ttl_timestamp ON memory(ttl, timestamp);

CREATE VIRTUAL TABLE IF NOT EXISTS memory_fts USING fts5(
    content,
    tags,
    content='memory',
    content_rowid='id'
);

CREATE TRIGGER IF NOT EXISTS memory_ai AFTER INSERT ON memory BEGIN
    INSERT INTO memory_fts(rowid, content, tags) VALUES (new.id, new.content, new.tags);
END;

CREATE TRIGGER IF NOT EXISTS memory_ad AFTER DELETE ON memory BEGIN
    INSERT INTO memory_fts(memory_fts, rowid, content, tags)
        VALUES ('delete', old.id, old.content, old.tags);
END;

CREATE TRIGGER IF NOT EXISTS memory_au AFTER UPDATE ON memory BEGIN
    INSERT INTO memory_fts(memory_fts, rowid, content, tags)
        VALUES ('delete', old.id, old.content, old.tags);
    INSERT INTO memory_fts(rowid, content, tags) VALUES (new.id, new.content, new.tags);
END;
"""


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
    db.put(project="nexus_rdr", title="phase1.md", content="authentication design")
    db.put(project="nexus_active", title="notes.md", content="authentication notes")
    db.put(project="arcaneum_rdr", title="arch.md", content="authentication architecture")

    # Search for 'authentication' scoped to *_rdr projects
    results = db.search_glob("authentication", "*_rdr")
    assert len(results) == 2
    projects = {r["project"] for r in results}
    assert projects == {"nexus_rdr", "arcaneum_rdr"}


def test_t2_search_glob_no_match(db: T2Database) -> None:
    """search_glob() returns empty list when no projects match the GLOB pattern."""
    db.put(project="nexus_active", title="notes.md", content="some content")

    results = db.search_glob("content", "*_rdr")
    assert results == []


# ── get_projects_with_prefix ──────────────────────────────────────────────────

def test_get_projects_with_prefix_returns_matching_namespaces(db: T2Database) -> None:
    """get_projects_with_prefix() returns all projects that start with the prefix."""
    db.put(project="nexus", title="ctx.md", content="main context")
    db.put(project="nexus_rdr", title="006.md", content="rdr entry")
    db.put(project="nexus_knowledge", title="notes.md", content="knowledge entry")
    db.put(project="other", title="x.md", content="unrelated")

    results = db.get_projects_with_prefix("nexus")
    projects = {r["project"] for r in results}
    assert projects == {"nexus", "nexus_rdr", "nexus_knowledge"}
    assert "other" not in projects


def test_get_projects_with_prefix_returns_last_updated(db: T2Database) -> None:
    """Each result row includes a last_updated field (the MAX timestamp for that project)."""
    db.put(project="repo_rdr", title="entry.md", content="content")

    results = db.get_projects_with_prefix("repo")
    assert len(results) == 1
    assert "last_updated" in results[0]
    assert results[0]["project"] == "repo_rdr"


def test_get_projects_with_prefix_ordered_by_most_recent(db: T2Database) -> None:
    """Results are ordered by MAX(timestamp) DESC — most-recently-updated namespace first."""
    db.put(project="repo_knowledge", title="old.md", content="older entry")
    db.put(project="repo_rdr", title="new.md", content="newer entry")
    # Backdate repo_knowledge so ordering is deterministic at 1-second timestamp resolution
    db.conn.execute(
        "UPDATE memory SET timestamp='2020-01-01T00:00:00Z' WHERE project='repo_knowledge'"
    )
    db.conn.commit()

    results = db.get_projects_with_prefix("repo")
    assert results[0]["project"] == "repo_rdr"
    assert results[1]["project"] == "repo_knowledge"


def test_get_projects_with_prefix_empty_when_no_match(db: T2Database) -> None:
    """Returns empty list when no projects match the prefix."""
    db.put(project="nexus_rdr", title="e.md", content="entry")

    results = db.get_projects_with_prefix("arcaneum")
    assert results == []


def test_get_projects_with_prefix_exact_prefix_only(db: T2Database) -> None:
    """Prefix 'abc' does NOT match a project named 'xabc' or 'abcx_foo'."""
    db.put(project="abc", title="e.md", content="entry")
    db.put(project="abc_sub", title="e2.md", content="entry2")
    db.put(project="xabc", title="e3.md", content="entry3")

    results = db.get_projects_with_prefix("abc")
    projects = {r["project"] for r in results}
    assert "xabc" not in projects
    assert "abc" in projects
    assert "abc_sub" in projects


def test_get_projects_with_prefix_underscore_not_wildcard(db: T2Database) -> None:
    """An underscore in the prefix is treated as a literal '_', not a LIKE wildcard."""
    db.put(project="my_repo", title="a.md", content="entry")
    db.put(project="myXrepo", title="b.md", content="other")  # X in position of _

    results = db.get_projects_with_prefix("my_repo")
    projects = {r["project"] for r in results}
    assert "myXrepo" not in projects
    assert "my_repo" in projects


def test_get_projects_with_prefix_percent_not_wildcard(db: T2Database) -> None:
    """A percent sign in the prefix is treated as a literal '%', not a LIKE wildcard."""
    db.put(project="50%_done", title="a.md", content="entry")
    db.put(project="50x_done", title="b.md", content="other")

    results = db.get_projects_with_prefix("50%")
    projects = {r["project"] for r in results}
    assert "50x_done" not in projects
    assert "50%_done" in projects


def test_get_projects_with_prefix_empty_prefix_returns_empty(db: T2Database) -> None:
    """Empty prefix returns empty list (not every project)."""
    db.put(project="alpha", title="a.md", content="entry")
    results = db.get_projects_with_prefix("")
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
    db.put(project="nexus", title="phase1.md", content="authentication design", tags="rdr,phase:1")
    db.put(project="nexus", title="notes.md", content="authentication notes", tags="notes")
    db.put(project="arcaneum", title="arch.md", content="authentication architecture", tags="rdr,arch")

    results = db.search_by_tag("authentication", "rdr")
    assert len(results) == 2
    projects = {r["project"] for r in results}
    assert projects == {"nexus", "arcaneum"}


def test_t2_search_by_tag_boundary_matching(db: T2Database) -> None:
    """search_by_tag() uses boundary matching — 'rdr' does not match 'rdr-archived'."""
    db.put(project="myrepo", title="active.md", content="active doc", tags="rdr,context")
    db.put(project="myrepo", title="archived.md", content="archived doc", tags="rdr-archived,context")

    results = db.search_by_tag("doc", "rdr")
    assert len(results) == 1
    assert results[0]["title"] == "active.md"


def test_t2_search_by_tag_no_match(db: T2Database) -> None:
    """search_by_tag() returns empty list when no entries match the tag."""
    db.put(project="proj", title="notes.md", content="some content", tags="notes")

    results = db.search_by_tag("content", "rdr")
    assert results == []


# ── TTL edge cases ──────────────────────────────────────────────────────────

def test_t2_expire_permanent_entries_preserved(db: T2Database) -> None:
    """Entries with ttl=None are permanent and never expire."""
    db.put(project="proj", title="permanent.md", content="forever", ttl=None)

    # Backdate timestamp by 1000 days
    past = (datetime.now(UTC) - timedelta(days=1000)).strftime("%Y-%m-%dT%H:%M:%SZ")
    db.conn.execute("UPDATE memory SET timestamp=? WHERE title='permanent.md'", (past,))
    db.conn.commit()

    deleted = db.expire()
    assert deleted == 0
    assert db.get(project="proj", title="permanent.md") is not None


def test_t2_expire_ttl_zero_expires_immediately(db: T2Database) -> None:
    """ttl=0 means entry expires as soon as any time passes."""
    db.put(project="proj", title="zero.md", content="instant expire", ttl=0)

    # Backdate by just 1 minute — julianday diff > 0
    past = (datetime.now(UTC) - timedelta(seconds=60)).strftime("%Y-%m-%dT%H:%M:%SZ")
    db.conn.execute("UPDATE memory SET timestamp=? WHERE title='zero.md'", (past,))
    db.conn.commit()

    deleted = db.expire()
    assert deleted == 1


def test_t2_expire_boundary_not_yet_expired(db: T2Database) -> None:
    """Entry with ttl=30 that is only 29 days old should survive."""
    db.put(project="proj", title="recent.md", content="not yet", ttl=30)

    # Backdate by 29 days (< 30)
    past = (datetime.now(UTC) - timedelta(days=29)).strftime("%Y-%m-%dT%H:%M:%SZ")
    db.conn.execute("UPDATE memory SET timestamp=? WHERE title='recent.md'", (past,))
    db.conn.commit()

    deleted = db.expire()
    assert deleted == 0
    assert db.get(project="proj", title="recent.md") is not None


# ── FTS5 special characters ─────────────────────────────────────────────────

def test_t2_unicode_content_roundtrips(db: T2Database) -> None:
    """Non-ASCII content (CJK, emoji) round-trips correctly via get()."""
    db.put(project="proj", title="cn.md", content="训练神经网络 🚀")
    result = db.get(project="proj", title="cn.md")
    assert result is not None
    assert result["content"] == "训练神经网络 🚀"


def test_t2_search_accented_latin(db: T2Database) -> None:
    """FTS5 indexes accented Latin characters (handled by default tokenizer)."""
    db.put(project="proj", title="fr.md", content="résumé cafetière naïve")
    results = db.search("resume")
    assert len(results) == 1
    assert results[0]["title"] == "fr.md"


def test_t2_search_with_quotes_in_content(db: T2Database) -> None:
    """Content containing double quotes is stored and searchable."""
    db.put(project="proj", title="quotes.md", content='He said "hello world" to everyone')
    results = db.search("hello")
    assert len(results) == 1


def test_t2_search_prefix_wildcard(db: T2Database) -> None:
    """FTS5 prefix search with * works."""
    db.put(project="proj", title="auth.md", content="authentication authorization tokens")
    results = db.search("auth*")
    assert len(results) == 1


# ── Tag edge cases ──────────────────────────────────────────────────────────

def test_t2_search_by_tag_single_letter(db: T2Database) -> None:
    """Single-letter tags are matched correctly by boundary matching."""
    db.put(project="proj", title="tagged.md", content="searchable content", tags="a,b,c")
    results = db.search_by_tag("searchable", "b")
    assert len(results) == 1


def test_t2_search_by_tag_no_false_positive(db: T2Database) -> None:
    """Tag 'rdr' does not match 'rdr-archived' or 'xrdr'."""
    db.put(project="proj", title="active.md", content="active doc", tags="rdr")
    db.put(project="proj", title="archived.md", content="archived doc", tags="rdr-archived")
    db.put(project="proj", title="xrdr.md", content="xrdr doc", tags="xrdr")

    results = db.search_by_tag("doc", "rdr")
    assert len(results) == 1
    assert results[0]["title"] == "active.md"


# ── Upsert semantics ───────────────────────────────────────────────────────

def test_t2_put_upsert_updates_content(db: T2Database) -> None:
    """put() with same (project, title) updates the content."""
    db.put(project="proj", title="doc.md", content="version 1")
    db.put(project="proj", title="doc.md", content="version 2")

    entry = db.get(project="proj", title="doc.md")
    assert entry["content"] == "version 2"

    entries = db.list_entries(project="proj")
    assert len(entries) == 1


def test_t2_put_upsert_updates_fts(db: T2Database) -> None:
    """Upsert updates the FTS5 index so old content is no longer searchable."""
    db.put(project="proj", title="doc.md", content="unique_keyword_alpha")
    db.put(project="proj", title="doc.md", content="unique_keyword_beta")

    assert db.search("unique_keyword_alpha") == []
    assert len(db.search("unique_keyword_beta")) == 1



# ── get_all ─────────────────────────────────────────────────────────────────

def test_t2_get_all_returns_full_content(db: T2Database) -> None:
    db.put(project="proj", title="a.md", content="content a")
    db.put(project="proj", title="b.md", content="content b")

    entries = db.get_all("proj")
    assert len(entries) == 2
    titles = {e["title"] for e in entries}
    assert titles == {"a.md", "b.md"}
    assert all("content" in e["content"] for e in entries)


# ── _sanitize_fts5 ──────────────────────────────────────────────────────────

def test_sanitize_fts5_plain_query_unchanged() -> None:
    """Plain alphanumeric tokens pass through unquoted."""
    assert _sanitize_fts5("hello world") == "hello world"


def test_sanitize_fts5_hyphen_token_quoted() -> None:
    """Tokens containing a hyphen are wrapped in double quotes."""
    assert _sanitize_fts5("verification-probe") == '"verification-probe"'


def test_sanitize_fts5_colon_token_quoted() -> None:
    """Tokens containing a colon are wrapped in double quotes."""
    assert _sanitize_fts5("phase:1") == '"phase:1"'


def test_sanitize_fts5_mixed_tokens() -> None:
    """Plain and special tokens are handled independently."""
    assert _sanitize_fts5("foo bar-baz") == 'foo "bar-baz"'


def test_sanitize_fts5_embedded_double_quote_escaped() -> None:
    """Embedded double quotes within a token are escaped as ''."""
    result = _sanitize_fts5('say"hello')
    assert result == '"say""hello"'


def test_sanitize_fts5_hyphenated_query_no_crash(db: T2Database) -> None:
    """search() with a hyphenated query does not raise OperationalError."""
    db.put(project="proj", title="notes.md", content="verification probe test")
    results = db.search("verification-probe")
    # May or may not match depending on FTS tokenisation, but must not crash.
    assert isinstance(results, list)


def test_sanitize_fts5_hyphenated_query_with_project_no_crash(db: T2Database) -> None:
    """search() with project filter and hyphenated query does not raise."""
    db.put(project="proj", title="notes.md", content="smoke test entry")
    results = db.search("smoke-test", project="proj")
    assert isinstance(results, list)


def test_sanitize_fts5_search_glob_hyphen_no_crash(db: T2Database) -> None:
    """search_glob() with a hyphenated query does not raise OperationalError."""
    db.put(project="nexus_rdr", title="notes.md", content="verification probe")
    results = db.search_glob("verification-probe", "*_rdr")
    assert isinstance(results, list)


def test_sanitize_fts5_search_by_tag_hyphen_no_crash(db: T2Database) -> None:
    """search_by_tag() with a hyphenated query does not raise OperationalError."""
    db.put(project="proj", title="notes.md", content="verification probe", tags="rdr")
    results = db.search_by_tag("verification-probe", "rdr")
    assert isinstance(results, list)


# ── FTS5 title indexing ───────────────────────────────────────────────────────

def test_t2_search_finds_entry_by_title_keyword(db: T2Database) -> None:
    """search() returns entries whose title contains the search term."""
    db.put(project="nexus", title="RDR-025-implementation.md", content="some generic content")
    db.put(project="nexus", title="unrelated.md", content="other content here")

    results = db.search("RDR-025")
    assert len(results) == 1
    assert results[0]["title"] == "RDR-025-implementation.md"


def test_t2_search_title_with_project_filter(db: T2Database) -> None:
    """search() with project filter finds by title within that project."""
    db.put(project="proj_a", title="auth-design.md", content="generic text")
    db.put(project="proj_b", title="auth-notes.md", content="generic text")

    results = db.search("auth", project="proj_a")
    assert len(results) == 1
    assert results[0]["project"] == "proj_a"


def test_t2_search_title_only_match_no_content_collision(db: T2Database) -> None:
    """A term appearing only in title (not content) is still found by search()."""
    unique_title_word = "xylophone99"
    db.put(project="proj", title=f"{unique_title_word}.md", content="completely different words")

    results = db.search(unique_title_word)
    assert len(results) == 1
    assert unique_title_word in results[0]["title"]


def test_t2_search_title_update_triggers_reindex(db: T2Database) -> None:
    """After UPDATE on memory table, FTS5 title index reflects the new title."""
    # Insert with a title containing a unique word
    db.put(project="proj", title="olduniquetitleword.md", content="content")
    assert len(db.search("olduniquetitleword")) == 1

    # Directly update the title — the au trigger should re-index title in FTS
    db.conn.execute(
        "UPDATE memory SET title='newuniquetitleword.md' WHERE project='proj' AND title='olduniquetitleword.md'"
    )
    db.conn.commit()

    # Old title word should no longer be findable
    assert db.search("olduniquetitleword") == []
    # New title word should be findable
    assert len(db.search("newuniquetitleword")) == 1


# ── FTS5 migration (old schema without title) ─────────────────────────────────

def _create_old_schema_db(path: Path) -> None:
    """Create a SQLite DB using the old FTS5 schema (no title column) and insert rows."""
    conn = sqlite3.connect(str(path))
    conn.executescript(_OLD_FTS_SCHEMA)
    conn.execute(
        "INSERT INTO memory (project, title, session, agent, content, tags, timestamp, ttl) "
        "VALUES (?, ?, NULL, NULL, ?, ?, ?, ?)",
        ("testproj", "RDR-007-design.md", "generic body content", "rdr", "2026-01-01T00:00:00Z", 30),
    )
    conn.commit()
    conn.close()


def test_t2_fts_migration_enables_title_search(tmp_path: Path) -> None:
    """Opening an old-schema DB with T2Database migrates FTS5 to include title column.

    After migration, searching for a term that appears only in the title must
    return the entry, whereas the old schema would have returned nothing.
    """
    db_path = tmp_path / "old_schema.db"
    _create_old_schema_db(db_path)

    # Verify old schema: direct query shows row exists but FTS5 lacks title
    conn = sqlite3.connect(str(db_path))
    fts_schema = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name='memory_fts'"
    ).fetchone()[0]
    assert "title" not in fts_schema, "Pre-condition: old schema should lack title"
    conn.close()

    # Now open with new T2Database code — migration should occur
    with T2Database(db_path) as db:
        # Verify migration updated the FTS schema
        fts_schema_new = db.conn.execute(
            "SELECT sql FROM sqlite_master WHERE type='table' AND name='memory_fts'"
        ).fetchone()[0]
        assert "title" in fts_schema_new, "Post-migration: FTS5 schema should include title"

        # Insert new entry so title search is tested on fresh data too
        db.put(project="testproj", title="RDR-999-migration.md", content="unrelated body")

        # The pre-existing row's title should be searchable after rebuild
        results = db.search("RDR-007")
        assert len(results) == 1, f"Expected to find RDR-007 by title; got {results}"
        assert results[0]["title"] == "RDR-007-design.md"

        # New entry title also searchable
        results2 = db.search("RDR-999")
        assert len(results2) == 1
        assert results2[0]["title"] == "RDR-999-migration.md"


def test_t2_fts_migration_is_idempotent(tmp_path: Path) -> None:
    """Opening a DB that already has title in FTS5 schema does NOT re-migrate."""
    db_path = tmp_path / "new_schema.db"

    # Create DB with new schema
    with T2Database(db_path) as db:
        db.put(project="proj", title="existing-entry.md", content="content here")

    # Open again — should not error or drop existing data
    with T2Database(db_path) as db:
        results = db.search("existing-entry")
        assert len(results) == 1
        assert results[0]["title"] == "existing-entry.md"
