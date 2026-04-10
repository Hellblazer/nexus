"""AC2: T2 SQLite creates schema with WAL mode, FTS5 table, and all 3 triggers."""
from nexus.db.t2 import T2Database


def test_schema_creation(db: T2Database) -> None:
    # Phase 2: the memory table, memory_fts, indexes, and triggers all
    # live on the memory store's dedicated connection.
    cur = db.memory.conn.cursor()

    # WAL mode
    cur.execute("PRAGMA journal_mode")
    assert cur.fetchone()[0] == "wal"

    # Main table
    cur.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='memory'")
    assert cur.fetchone() is not None, "memory table missing"

    # FTS5 virtual table (shows as table in sqlite_master)
    cur.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='memory_fts'")
    assert cur.fetchone() is not None, "memory_fts virtual table missing"

    # Indexes
    for idx in ("idx_memory_project_title", "idx_memory_project", "idx_memory_agent", "idx_memory_timestamp"):
        cur.execute("SELECT name FROM sqlite_master WHERE type='index' AND name=?", (idx,))
        assert cur.fetchone() is not None, f"index {idx} missing"

    # Triggers
    for trigger in ("memory_ai", "memory_ad", "memory_au"):
        cur.execute("SELECT name FROM sqlite_master WHERE type='trigger' AND name=?", (trigger,))
        assert cur.fetchone() is not None, f"trigger {trigger} missing"
