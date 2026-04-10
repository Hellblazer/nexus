# SPDX-License-Identifier: AGPL-3.0-or-later
import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from nexus.db.t2 import T2Database, _sanitize_fts5

_OLD_FTS_SCHEMA = """PRAGMA journal_mode=WAL;
CREATE TABLE IF NOT EXISTS memory (
  id INTEGER PRIMARY KEY, project TEXT NOT NULL, title TEXT NOT NULL,
  session TEXT, agent TEXT, content TEXT NOT NULL, tags TEXT,
  timestamp TEXT NOT NULL, ttl INTEGER);
CREATE UNIQUE INDEX IF NOT EXISTS idx_memory_project_title ON memory(project, title);
CREATE INDEX IF NOT EXISTS idx_memory_project ON memory(project);
CREATE INDEX IF NOT EXISTS idx_memory_agent ON memory(agent);
CREATE INDEX IF NOT EXISTS idx_memory_timestamp ON memory(timestamp);
CREATE INDEX IF NOT EXISTS idx_memory_ttl_timestamp ON memory(ttl, timestamp);
CREATE VIRTUAL TABLE IF NOT EXISTS memory_fts USING fts5(
  content, tags, content='memory', content_rowid='id');
CREATE TRIGGER IF NOT EXISTS memory_ai AFTER INSERT ON memory BEGIN
  INSERT INTO memory_fts(rowid, content, tags) VALUES (new.id, new.content, new.tags); END;
CREATE TRIGGER IF NOT EXISTS memory_ad AFTER DELETE ON memory BEGIN
  INSERT INTO memory_fts(memory_fts, rowid, content, tags)
  VALUES ('delete', old.id, old.content, old.tags); END;
CREATE TRIGGER IF NOT EXISTS memory_au AFTER UPDATE ON memory BEGIN
  INSERT INTO memory_fts(memory_fts, rowid, content, tags)
  VALUES ('delete', old.id, old.content, old.tags);
  INSERT INTO memory_fts(rowid, content, tags) VALUES (new.id, new.content, new.tags); END;"""


# ── helpers ──────────────────────────────────────────────────────────────────

def _backdate(db: T2Database, title: str, days: float = 0, seconds: float = 0) -> None:
    past = (datetime.now(UTC) - timedelta(days=days, seconds=seconds)).strftime("%Y-%m-%dT%H:%M:%SZ")
    db.conn.execute("UPDATE memory SET timestamp=? WHERE title=?", (past, title))
    db.conn.commit()


def _create_old_schema_db(path: Path) -> None:
    conn = sqlite3.connect(str(path))
    conn.executescript(_OLD_FTS_SCHEMA)
    conn.execute(
        "INSERT INTO memory (project, title, session, agent, content, tags, timestamp, ttl) "
        "VALUES (?, ?, NULL, NULL, ?, ?, ?, ?)",
        ("testproj", "RDR-007-design.md", "generic body content", "rdr", "2026-01-01T00:00:00Z", 30),
    )
    conn.commit()
    conn.close()


# ── context manager ──────────────────────────────────────────────────────────

def test_context_manager_closes_on_exit(tmp_path: Path) -> None:
    with T2Database(tmp_path / "cm.db") as db:
        row_id = db.put(project="test", title="cm-entry", content="hello")
        assert row_id is not None
    with pytest.raises(Exception):
        db.conn.execute("SELECT 1")


def test_context_manager_closes_on_exception(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="intentional"):
        with T2Database(tmp_path / "cm_exc.db") as db:
            db.put(project="test", title="exc-entry", content="before error")
            raise ValueError("intentional")
    with pytest.raises(Exception):
        db.conn.execute("SELECT 1")


def test_context_manager_returns_self(tmp_path: Path) -> None:
    with T2Database(tmp_path / "cm_self.db") as db:
        assert isinstance(db, T2Database)


def test_context_manager_does_not_suppress_exception(tmp_path: Path) -> None:
    with pytest.raises(RuntimeError, match="propagated"):
        with T2Database(tmp_path / "cm_nosup.db") as db:
            raise RuntimeError("propagated")


# ── get() ValueError ─────────────────────────────────────────────────────────

@pytest.mark.parametrize("kwargs", [
    {},
    {"project": "proj"},
    {"title": "some.md"},
])
def test_get_missing_args_raises_valueerror(db: T2Database, kwargs: dict) -> None:
    with pytest.raises(ValueError, match="Provide either id or both project and title"):
        db.get(**kwargs)


# ── WAL mode ─────────────────────────────────────────────────────────────────

def test_wal_mode_enabled(db: T2Database) -> None:
    mode = db.conn.execute("PRAGMA journal_mode").fetchone()[0]
    assert mode.lower() == "wal"


# ── expire ───────────────────────────────────────────────────────────────────

def test_expire_returns_zero_on_fresh_db(db: T2Database) -> None:
    assert db.expire() == 0


@pytest.mark.parametrize("title,ttl,backdate_days,backdate_secs,expect_deleted,expect_gone", [
    ("stale.md",     1,    2,  0, 1, True),
    ("fresh.md",     30,   0,  0, 0, False),
    ("permanent.md", None, 1000, 0, 0, False),
    ("zero.md",      0,    0, 60, 1, True),
    ("recent.md",    30,  29,  0, 0, False),
])
def test_expire_scenarios(
    db: T2Database, title: str, ttl: int | None,
    backdate_days: float, backdate_secs: float,
    expect_deleted: int, expect_gone: bool,
) -> None:
    db.put(project="proj", title=title, content="data", ttl=ttl)
    if backdate_days or backdate_secs:
        _backdate(db, title, days=backdate_days, seconds=backdate_secs)
    assert db.expire() == expect_deleted
    if expect_gone:
        assert db.get(project="proj", title=title) is None
    else:
        assert db.get(project="proj", title=title) is not None


# ── list_entries with agent filter ───────────────────────────────────────────

def test_list_entries_with_agent_filter(db: T2Database) -> None:
    db.put(project="proj", title="a.md", content="aaa", agent="agent-alpha")
    db.put(project="proj", title="b.md", content="bbb", agent="agent-beta")
    db.put(project="proj", title="c.md", content="ccc", agent="agent-alpha")

    alpha = db.list_entries(project="proj", agent="agent-alpha")
    assert {e["title"] for e in alpha} == {"a.md", "c.md"}

    beta = db.list_entries(project="proj", agent="agent-beta")
    assert len(beta) == 1 and beta[0]["title"] == "b.md"


def test_list_entries_agent_filter_only(db: T2Database) -> None:
    db.put(project="proj_a", title="x.md", content="xxx", agent="shared-agent")
    db.put(project="proj_b", title="y.md", content="yyy", agent="shared-agent")
    db.put(project="proj_c", title="z.md", content="zzz", agent="other-agent")

    entries = db.list_entries(agent="shared-agent")
    assert {e["title"] for e in entries} == {"x.md", "y.md"}


# ── search_glob ──────────────────────────────────────────────────────────────

def test_search_glob(db: T2Database) -> None:
    db.put(project="nexus_rdr", title="phase1.md", content="authentication design")
    db.put(project="nexus_active", title="notes.md", content="authentication notes")
    db.put(project="arcaneum_rdr", title="arch.md", content="authentication architecture")

    results = db.search_glob("authentication", "*_rdr")
    assert {r["project"] for r in results} == {"nexus_rdr", "arcaneum_rdr"}


def test_search_glob_no_match(db: T2Database) -> None:
    db.put(project="nexus_active", title="notes.md", content="some content")
    assert db.search_glob("content", "*_rdr") == []


# ── get_projects_with_prefix ─────────────────────────────────────────────────

def test_get_projects_with_prefix_returns_matching(db: T2Database) -> None:
    db.put(project="nexus", title="ctx.md", content="main context")
    db.put(project="nexus_rdr", title="006.md", content="rdr entry")
    db.put(project="nexus_knowledge", title="notes.md", content="knowledge entry")
    db.put(project="other", title="x.md", content="unrelated")

    projects = {r["project"] for r in db.get_projects_with_prefix("nexus")}
    assert projects == {"nexus", "nexus_rdr", "nexus_knowledge"}


def test_get_projects_with_prefix_returns_last_updated(db: T2Database) -> None:
    db.put(project="repo_rdr", title="entry.md", content="content")
    results = db.get_projects_with_prefix("repo")
    assert len(results) == 1 and "last_updated" in results[0]


def test_get_projects_with_prefix_ordered_by_most_recent(db: T2Database) -> None:
    db.put(project="repo_knowledge", title="old.md", content="older entry")
    db.put(project="repo_rdr", title="new.md", content="newer entry")
    db.conn.execute(
        "UPDATE memory SET timestamp='2020-01-01T00:00:00Z' WHERE project='repo_knowledge'"
    )
    db.conn.commit()

    results = db.get_projects_with_prefix("repo")
    assert results[0]["project"] == "repo_rdr"
    assert results[1]["project"] == "repo_knowledge"


@pytest.mark.parametrize("prefix,setup,expected,excluded", [
    ("arcaneum", [("nexus_rdr", "e.md")], set(), set()),
    ("abc", [("abc", "e.md"), ("abc_sub", "e2.md"), ("xabc", "e3.md")],
     {"abc", "abc_sub"}, {"xabc"}),
    ("my_repo", [("my_repo", "a.md"), ("myXrepo", "b.md")],
     {"my_repo"}, {"myXrepo"}),
    ("50%", [("50%_done", "a.md"), ("50x_done", "b.md")],
     {"50%_done"}, {"50x_done"}),
    ("", [("alpha", "a.md")], set(), set()),
])
def test_get_projects_with_prefix_edge_cases(
    db: T2Database, prefix: str,
    setup: list[tuple[str, str]], expected: set[str], excluded: set[str],
) -> None:
    for proj, title in setup:
        db.put(project=proj, title=title, content="entry")
    projects = {r["project"] for r in db.get_projects_with_prefix(prefix)}
    assert projects == expected
    for ex in excluded:
        assert ex not in projects


# ── delete ───────────────────────────────────────────────────────────────────

def test_delete(db: T2Database) -> None:
    db.put(project="proj", title="doomed.md", content="to be deleted")
    assert db.delete(project="proj", title="doomed.md") is True
    assert db.get(project="proj", title="doomed.md") is None


def test_delete_nonexistent_returns_false(db: T2Database) -> None:
    assert db.delete(project="proj", title="never_existed.md") is False


def test_delete_removes_from_fts_index(db: T2Database) -> None:
    db.put(project="proj", title="indexed.md", content="searchable unique keyword xyzzy")
    assert len(db.search("xyzzy")) == 1
    db.delete(project="proj", title="indexed.md")
    assert db.search("xyzzy") == []


# ── search_by_tag ────────────────────────────────────────────────────────────

def test_search_by_tag(db: T2Database) -> None:
    db.put(project="nexus", title="phase1.md", content="authentication design", tags="rdr,phase:1")
    db.put(project="nexus", title="notes.md", content="authentication notes", tags="notes")
    db.put(project="arcaneum", title="arch.md", content="authentication architecture", tags="rdr,arch")

    results = db.search_by_tag("authentication", "rdr")
    assert {r["project"] for r in results} == {"nexus", "arcaneum"}


def test_search_by_tag_boundary_and_no_false_positive(db: T2Database) -> None:
    db.put(project="proj", title="active.md", content="active doc", tags="rdr")
    db.put(project="proj", title="archived.md", content="archived doc", tags="rdr-archived")
    db.put(project="proj", title="xrdr.md", content="xrdr doc", tags="xrdr")

    results = db.search_by_tag("doc", "rdr")
    assert len(results) == 1 and results[0]["title"] == "active.md"


def test_search_by_tag_no_match(db: T2Database) -> None:
    db.put(project="proj", title="notes.md", content="some content", tags="notes")
    assert db.search_by_tag("content", "rdr") == []


def test_search_by_tag_single_letter(db: T2Database) -> None:
    db.put(project="proj", title="tagged.md", content="searchable content", tags="a,b,c")
    assert len(db.search_by_tag("searchable", "b")) == 1


# ── FTS5 special characters ─────────────────────────────────────────────────

@pytest.mark.parametrize("title,content,query,expect_found", [
    ("cn.md", "训练神经网络 🚀", None, True),       # unicode roundtrip (checked via get)
    ("fr.md", "résumé cafetière naïve", "resume", True),
    ("quotes.md", 'He said "hello world" to everyone', "hello", True),
    ("auth.md", "authentication authorization tokens", "auth*", True),
])
def test_fts_special_characters(
    db: T2Database, title: str, content: str, query: str | None, expect_found: bool,
) -> None:
    db.put(project="proj", title=title, content=content)
    if query is None:
        entry = db.get(project="proj", title=title)
        assert entry is not None and entry["content"] == content
    else:
        assert (len(db.search(query)) >= 1) == expect_found


# ── Upsert semantics ────────────────────────────────────────────────────────

def test_put_upsert_updates_content(db: T2Database) -> None:
    db.put(project="proj", title="doc.md", content="version 1")
    db.put(project="proj", title="doc.md", content="version 2")

    entry = db.get(project="proj", title="doc.md")
    assert entry["content"] == "version 2"
    assert len(db.list_entries(project="proj")) == 1


def test_put_upsert_updates_fts(db: T2Database) -> None:
    db.put(project="proj", title="doc.md", content="unique_keyword_alpha")
    db.put(project="proj", title="doc.md", content="unique_keyword_beta")
    assert db.search("unique_keyword_alpha") == []
    assert len(db.search("unique_keyword_beta")) == 1


# ── get_all ──────────────────────────────────────────────────────────────────

def test_get_all_returns_full_content(db: T2Database) -> None:
    db.put(project="proj", title="a.md", content="content a")
    db.put(project="proj", title="b.md", content="content b")

    entries = db.get_all("proj")
    assert {e["title"] for e in entries} == {"a.md", "b.md"}
    assert all("content" in e["content"] for e in entries)


# ── _sanitize_fts5 ──────────────────────────────────────────────────────────

@pytest.mark.parametrize("input_q,expected", [
    ("hello world", "hello world"),
    ("verification-probe", '"verification-probe"'),
    ("phase:1", '"phase:1"'),
    ("foo bar-baz", 'foo "bar-baz"'),
    ('say"hello', '"say""hello"'),
])
def test_sanitize_fts5(input_q: str, expected: str) -> None:
    assert _sanitize_fts5(input_q) == expected


@pytest.mark.parametrize("search_fn,query,setup_kwargs", [
    ("search", "verification-probe", {}),
    ("search", "smoke-test", {"project": "proj"}),
    ("search_glob", "verification-probe", {"glob_pattern": "*_rdr"}),
    ("search_by_tag", "verification-probe", {"tag": "rdr"}),
])
def test_sanitize_fts5_hyphen_no_crash(
    db: T2Database, search_fn: str, query: str, setup_kwargs: dict,
) -> None:
    db.put(project="nexus_rdr", title="notes.md", content="verification probe", tags="rdr")
    if search_fn == "search":
        results = db.search(query, project=setup_kwargs.get("project"))
    elif search_fn == "search_glob":
        results = db.search_glob(query, setup_kwargs["glob_pattern"])
    else:
        results = db.search_by_tag(query, setup_kwargs["tag"])
    assert isinstance(results, list)


# ── FTS5 title indexing ──────────────────────────────────────────────────────

@pytest.mark.parametrize("title,content,query,project_filter,expect_count", [
    ("RDR-025-implementation.md", "some generic content", "RDR-025", None, 1),
    ("xylophone99.md", "completely different words", "xylophone99", None, 1),
])
def test_search_finds_by_title(
    db: T2Database, title: str, content: str, query: str,
    project_filter: str | None, expect_count: int,
) -> None:
    db.put(project="proj", title=title, content=content)
    results = db.search(query, project=project_filter)
    assert len(results) == expect_count


def test_search_title_with_project_filter(db: T2Database) -> None:
    db.put(project="proj_a", title="auth-design.md", content="generic text")
    db.put(project="proj_b", title="auth-notes.md", content="generic text")
    results = db.search("auth", project="proj_a")
    assert len(results) == 1 and results[0]["project"] == "proj_a"


def test_search_title_update_triggers_reindex(db: T2Database) -> None:
    db.put(project="proj", title="olduniquetitleword.md", content="content")
    assert len(db.search("olduniquetitleword")) == 1

    db.conn.execute(
        "UPDATE memory SET title='newuniquetitleword.md' "
        "WHERE project='proj' AND title='olduniquetitleword.md'"
    )
    db.conn.commit()

    assert db.search("olduniquetitleword") == []
    assert len(db.search("newuniquetitleword")) == 1


# ── FTS5 migration (old schema without title) ───────────────────────────────

def test_fts_migration_enables_title_search(tmp_path: Path) -> None:
    db_path = tmp_path / "old_schema.db"
    _create_old_schema_db(db_path)

    conn = sqlite3.connect(str(db_path))
    fts_schema = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name='memory_fts'"
    ).fetchone()[0]
    assert "title" not in fts_schema
    conn.close()

    with T2Database(db_path) as db:
        fts_new = db.conn.execute(
            "SELECT sql FROM sqlite_master WHERE type='table' AND name='memory_fts'"
        ).fetchone()[0]
        assert "title" in fts_new

        db.put(project="testproj", title="RDR-999-migration.md", content="unrelated body")

        results = db.search("RDR-007")
        assert len(results) == 1 and results[0]["title"] == "RDR-007-design.md"

        results2 = db.search("RDR-999")
        assert len(results2) == 1 and results2[0]["title"] == "RDR-999-migration.md"


def test_fts_migration_is_idempotent(tmp_path: Path) -> None:
    db_path = tmp_path / "new_schema.db"
    with T2Database(db_path) as db:
        db.put(project="proj", title="existing-entry.md", content="content here")

    with T2Database(db_path) as db:
        results = db.search("existing-entry")
        assert len(results) == 1 and results[0]["title"] == "existing-entry.md"


# ── T2 access tracking (RDR-057 P2-2a, nexus-b4x0) ────────────────────────


def test_access_tracking_columns_exist(db: T2Database) -> None:
    """access_count and last_accessed columns are present after init."""
    db.conn.execute("SELECT access_count FROM memory LIMIT 0")
    db.conn.execute("SELECT last_accessed FROM memory LIMIT 0")


def test_access_tracking_migration_idempotent(tmp_path: Path) -> None:
    """Opening DB twice doesn't fail (migration is safe to re-run)."""
    db_path = tmp_path / "migrate.db"
    T2Database(db_path).close()
    T2Database(db_path).close()  # second open = idempotent migration


def test_new_entry_access_count_zero(db: T2Database) -> None:
    """New entries start with access_count=0."""
    db.put(project="proj", title="fresh.md", content="new content")
    row = db.conn.execute(
        "SELECT access_count FROM memory WHERE title='fresh.md'"
    ).fetchone()
    assert row[0] == 0


def test_get_increments_access_count(db: T2Database) -> None:
    """get() increments access_count by 1."""
    db.put(project="proj", title="tracked.md", content="trackable")
    db.get(project="proj", title="tracked.md")
    row = db.conn.execute(
        "SELECT access_count FROM memory WHERE title='tracked.md'"
    ).fetchone()
    assert row[0] == 1


def test_get_increments_access_count_three_times(db: T2Database) -> None:
    """Three get() calls → access_count=3."""
    db.put(project="proj", title="multi.md", content="accessed many times")
    db.get(project="proj", title="multi.md")
    db.get(project="proj", title="multi.md")
    db.get(project="proj", title="multi.md")
    row = db.conn.execute(
        "SELECT access_count FROM memory WHERE title='multi.md'"
    ).fetchone()
    assert row[0] == 3


def test_search_increments_access_count(db: T2Database) -> None:
    """search() increments access_count for returned entries."""
    db.put(project="proj", title="searchable.md", content="unique xyzzy keyword")
    db.search("xyzzy")
    row = db.conn.execute(
        "SELECT access_count FROM memory WHERE title='searchable.md'"
    ).fetchone()
    assert row[0] == 1


def test_get_sets_last_accessed(db: T2Database) -> None:
    """get() updates last_accessed to a non-empty ISO timestamp."""
    db.put(project="proj", title="ts.md", content="timestamp check")
    db.get(project="proj", title="ts.md")
    row = db.conn.execute(
        "SELECT last_accessed FROM memory WHERE title='ts.md'"
    ).fetchone()
    assert row[0] != ""
    from datetime import datetime
    datetime.fromisoformat(row[0])  # validates format


# ── heat-weighted expiry ────────────────────────────────────────────────────


def test_expire_unaccessed_entry_base_behavior(db: T2Database) -> None:
    """access_count=0, base_ttl=1, backdated 2 days → expires (unchanged behavior)."""
    db.put(project="proj", title="cold.md", content="never accessed", ttl=1)
    _backdate(db, "cold.md", days=2)
    assert db.expire() == 1


def test_expire_hot_entry_survives_past_base_ttl(db: T2Database) -> None:
    """access_count=9 → effective_ttl ≈ 3.3 days. Entry at 1.5 days survives."""
    db.put(project="proj", title="hot.md", content="frequently accessed", ttl=1)
    db.conn.execute("UPDATE memory SET access_count=9 WHERE title='hot.md'")
    db.conn.commit()
    _backdate(db, "hot.md", days=1.5)
    assert db.expire() == 0  # survives due to heat


def test_expire_hot_entry_eventually_expires(db: T2Database) -> None:
    """access_count=9, effective_ttl ≈ 3.3 days. Entry at 4 days expires."""
    db.put(project="proj", title="hot-old.md", content="hot but stale", ttl=1)
    db.conn.execute("UPDATE memory SET access_count=9 WHERE title='hot-old.md'")
    db.conn.commit()
    _backdate(db, "hot-old.md", days=4)
    assert db.expire() == 1


def test_expire_permanent_entries_preserved(db: T2Database) -> None:
    """Entries with ttl=None are permanent — never expire regardless of age."""
    db.put(project="proj", title="perm.md", content="permanent", ttl=None)
    _backdate(db, "perm.md", days=1000)
    assert db.expire() == 0


def test_expire_relevance_log_purges_old_entries(db: T2Database) -> None:
    """expire_relevance_log() deletes entries older than the retention window."""
    # Insert rows with timestamps spanning fresh and stale
    db.log_relevance("q1", "c1", "stored", session_id="s1")
    db.log_relevance("q2", "c2", "stored", session_id="s1")
    # Backdate one row to 100 days ago
    db.conn.execute(
        "UPDATE relevance_log SET timestamp = datetime('now', '-100 days') WHERE chunk_id = ?",
        ("c1",),
    )
    db.conn.commit()

    purged = db.expire_relevance_log(days=90)
    assert purged == 1
    remaining = db.get_relevance_log()
    assert len(remaining) == 1
    assert remaining[0]["chunk_id"] == "c2"


def test_expire_relevance_log_no_op_when_empty(db: T2Database) -> None:
    """expire_relevance_log() on empty table returns 0."""
    assert db.expire_relevance_log(days=90) == 0


def test_expire_relevance_log_partial_purge(db: T2Database) -> None:
    """Partial purge: some rows stale, some fresh."""
    # Insert 4 rows
    for i in range(4):
        db.log_relevance(f"q{i}", f"c{i}", "stored", session_id="s1")
    # Backdate rows 0 and 1 to 100 days ago
    db.conn.execute(
        "UPDATE relevance_log SET timestamp = datetime('now', '-100 days') "
        "WHERE chunk_id IN ('c0', 'c1')"
    )
    db.conn.commit()

    purged = db.expire_relevance_log(days=90)
    assert purged == 2
    remaining = db.get_relevance_log()
    assert len(remaining) == 2
    assert {r["chunk_id"] for r in remaining} == {"c2", "c3"}


def test_expire_relevance_log_days_zero_purges_all(db: T2Database) -> None:
    """days=0 cutoff is "now", so every pre-existing row is purged."""
    db.log_relevance("q1", "c1", "stored")
    db.log_relevance("q2", "c2", "stored")
    purged = db.expire_relevance_log(days=0)
    assert purged == 2
    assert db.get_relevance_log() == []


def test_expire_relevance_log_days_negative_purges_all(db: T2Database) -> None:
    """Negative days means cutoff is in the future — all rows are stale."""
    db.log_relevance("q1", "c1", "stored")
    purged = db.expire_relevance_log(days=-1)
    assert purged == 1


def test_expire_also_purges_relevance_log(db: T2Database) -> None:
    """expire() calls expire_relevance_log() to purge telemetry."""
    # Stale relevance_log row
    db.log_relevance("q", "c", "stored", session_id="s1")
    db.conn.execute(
        "UPDATE relevance_log SET timestamp = datetime('now', '-100 days')"
    )
    db.conn.commit()

    db.expire()  # default relevance_log_days=90

    assert db.get_relevance_log() == []


def test_migration_guard_sequential_construction(tmp_path: Path, monkeypatch) -> None:
    """Two T2Database instances on the same path do not re-run migrations sequentially."""
    from nexus.db import t2 as t2_module

    # Clear any prior migration state for this path. The guard keys on the
    # resolved path, so discard the resolved form (important on macOS where
    # /var and /private/var resolve differently).
    path = tmp_path / "sequential.db"
    with t2_module._migrated_lock:
        t2_module._migrated_paths.discard(str(path.resolve()))

    call_count = {"n": 0}
    original = T2Database._migrate_plans_if_needed

    def counting(self):
        call_count["n"] += 1
        return original(self)

    monkeypatch.setattr(T2Database, "_migrate_plans_if_needed", counting)

    db1 = T2Database(path)
    assert call_count["n"] == 1
    # Second instance on the same path: migration must NOT run again
    db2 = T2Database(path)
    assert call_count["n"] == 1, (
        "Migration ran a second time — the _migrated_paths guard failed"
    )


def test_migration_guard_path_normalization(tmp_path: Path, monkeypatch) -> None:
    """Two paths resolving to the same file share a guard key via symlink.

    Python's ``Path`` collapses ``.`` segments at construction, so a naive
    ``base / "." / "file.db"`` produces the same string as ``base / "file.db"``
    and doesn't exercise normalization. This test uses a real symlink so the
    two string paths genuinely differ — only ``resolve()`` can reconcile them.
    """
    import os

    from nexus.db import t2 as t2_module

    base = tmp_path / "real"
    base.mkdir()
    canonical = base / "norm.db"

    # Create a symlinked alias pointing at the same parent directory
    link_parent = tmp_path / "via_symlink"
    os.symlink(base, link_parent)
    via_symlink = link_parent / "norm.db"

    # Sanity: the two path strings genuinely differ
    assert str(canonical) != str(via_symlink)
    # But resolve() converges them
    assert canonical.resolve() == via_symlink.resolve()

    with t2_module._migrated_lock:
        t2_module._migrated_paths.discard(str(canonical.resolve()))

    call_count = {"n": 0}
    original = T2Database._migrate_plans_if_needed

    def counting(self):
        call_count["n"] += 1
        return original(self)

    monkeypatch.setattr(T2Database, "_migrate_plans_if_needed", counting)

    # First construction via canonical path
    T2Database(canonical)
    assert call_count["n"] == 1

    # Second construction via symlinked path that resolves to the same file.
    # Without path.resolve() in __init__, the guard would see a different key
    # and re-run migrations.
    T2Database(via_symlink)
    assert call_count["n"] == 1, (
        "Migration ran again for a symlinked path resolving to the same "
        "file — path normalization in _init_schema failed"
    )


def test_migration_guard_concurrent_threads(tmp_path: Path, monkeypatch) -> None:
    """10 threads constructing T2Database on the same path run migrations exactly once.

    This is the regression test for F2 (round 2) — the race where two
    concurrent constructors could both enter the migration functions.
    """
    import threading

    from nexus.db import t2 as t2_module

    path = tmp_path / "concurrent.db"
    with t2_module._migrated_lock:
        t2_module._migrated_paths.discard(str(path.resolve()))

    call_count = {"n": 0}
    count_lock = threading.Lock()
    original = T2Database._migrate_plans_if_needed

    def counting(self):
        with count_lock:
            call_count["n"] += 1
        return original(self)

    monkeypatch.setattr(T2Database, "_migrate_plans_if_needed", counting)

    barrier = threading.Barrier(10, timeout=10)
    errors: list[Exception] = []
    dbs: list[T2Database] = []
    dbs_lock = threading.Lock()

    def worker():
        try:
            barrier.wait()
            db = T2Database(path)
            with dbs_lock:
                dbs.append(db)
        except Exception as exc:
            errors.append(exc)

    threads = [threading.Thread(target=worker) for _ in range(10)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert not errors, f"Concurrent construction raised: {errors}"
    assert call_count["n"] == 1, (
        f"Migration ran {call_count['n']} times across 10 concurrent "
        f"constructions — expected exactly 1 (the guard lock failed)"
    )


def test_get_returns_post_increment_access_count(db: T2Database) -> None:
    """get() return value reflects the incremented access_count, not stale."""
    db.put(project="proj", title="fresh.md", content="data")
    entry = db.get(project="proj", title="fresh.md")
    assert entry["access_count"] == 1
    entry2 = db.get(project="proj", title="fresh.md")
    assert entry2["access_count"] == 2


def test_get_by_id_increments_access_count(db: T2Database) -> None:
    """get(id=...) also increments access_count."""
    row_id = db.put(project="proj", title="byid.md", content="data")
    db.get(id=row_id)
    row = db.conn.execute(
        "SELECT access_count FROM memory WHERE id=?", (row_id,)
    ).fetchone()
    assert row[0] == 1


def test_upsert_preserves_access_count(db: T2Database) -> None:
    """Re-putting with same key preserves accumulated access_count."""
    db.put(project="proj", title="upsert.md", content="v1")
    db.get(project="proj", title="upsert.md")
    db.get(project="proj", title="upsert.md")
    # access_count is now 2
    db.put(project="proj", title="upsert.md", content="v2")
    row = db.conn.execute(
        "SELECT access_count FROM memory WHERE title='upsert.md'"
    ).fetchone()
    assert row[0] == 2  # preserved through upsert


def test_search_glob_does_not_increment_access_count(db: T2Database) -> None:
    """search_glob is an admin operation — does not track access."""
    db.put(project="nexus_rdr", title="scan.md", content="scan content")
    db.search_glob("scan", "*_rdr")
    row = db.conn.execute(
        "SELECT access_count FROM memory WHERE title='scan.md'"
    ).fetchone()
    assert row[0] == 0
