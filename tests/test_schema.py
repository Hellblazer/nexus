"""AC2 tombstone (nexus-i711w Stage 2 sub-stage A3).

``test_schema_creation`` DELETED: its subject was "T2 SQLite creates schema
with WAL mode, FTS5 table, and all 3 triggers", asserted through the SQLite
MemoryStore's raw handle (``db.memory.conn``). That store died with the
sub-stage A3 deletions and ``T2Database.memory`` is now unconditionally
``HttpMemoryStore``, which exposes no raw connection by design
(``RawHandleGuardMixin``). The schema DDL's last src home,
``nexus/db/migrations.py``, was deleted in RDR-158 P4 Stage 4 (nexus-i711w);
a frozen snapshot survives only as the test fixture
``tests/_t2_fixture_ops._FROZEN_LEGACY_T2_SCHEMA`` (legacy migration-source
builder). The engine-side schema is Liquibase-owned with no Python twin to
assert here.
"""
