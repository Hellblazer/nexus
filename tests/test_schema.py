"""AC2 tombstone (nexus-i711w Stage 2 sub-stage A3).

``test_schema_creation`` DELETED: its subject was "T2 SQLite creates schema
with WAL mode, FTS5 table, and all 3 triggers", asserted through the SQLite
MemoryStore's raw handle (``db.memory.conn``). That store died with the
sub-stage A3 deletions and ``T2Database.memory`` is now unconditionally
``HttpMemoryStore``, which exposes no raw connection by design
(``RawHandleGuardMixin``). The schema DDL itself survives in
``nexus/db/migrations.py`` (a migration SOURCE until Stage 4) and its
bootstrap is pinned by tests/test_migrations.py; the engine-side schema is
Liquibase-owned with no Python twin to assert here.
"""
