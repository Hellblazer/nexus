# SPDX-License-Identifier: AGPL-3.0-or-later
"""RDR-111 P1.3 (nexus-r0vi): T2 liveness table contract tests.

Tests cover:
  - Schema: table and index created by migration.
  - liveness_upsert: idempotent on same PK; last_seen updates.
  - liveness_sweep: deletes stale rows; returns count; fresh rows survive.
  - liveness_list: returns all current rows in stable (pid, machine) order.
  - Fixed-clock injection via optional ``_now`` kwarg to each method.
"""
from __future__ import annotations

import socket
import sqlite3
import time
from pathlib import Path

import pytest

from nexus.db.t2.memory_store import MemoryStore


# ── Helpers ──────────────────────────────────────────────────────────────────

_MACHINE = socket.gethostname()
_USER = "testuser"


def _store(tmp_path: Path) -> MemoryStore:
    return MemoryStore(tmp_path / "memory.db")


# ── Schema ───────────────────────────────────────────────────────────────────


class TestSchema:
    def test_migration_creates_table(self, tmp_path: Path) -> None:
        import sqlite3 as _sq

        from nexus.db.migrations import migrate_liveness_table

        conn = _sq.connect(str(tmp_path / "memory.db"))
        try:
            migrate_liveness_table(conn)
            tables = {
                r[0]
                for r in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                ).fetchall()
            }
            assert "liveness" in tables
        finally:
            conn.close()

    def test_migration_creates_last_seen_index(self, tmp_path: Path) -> None:
        import sqlite3 as _sq

        from nexus.db.migrations import migrate_liveness_table

        conn = _sq.connect(str(tmp_path / "memory.db"))
        try:
            migrate_liveness_table(conn)
            indexes = {
                r[0]
                for r in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='index' "
                    "AND tbl_name='liveness'"
                ).fetchall()
            }
            assert "idx_liveness_last_seen" in indexes
        finally:
            conn.close()

    def test_migration_idempotent(self, tmp_path: Path) -> None:
        import sqlite3 as _sq

        from nexus.db.migrations import migrate_liveness_table

        conn = _sq.connect(str(tmp_path / "memory.db"))
        try:
            migrate_liveness_table(conn)
            migrate_liveness_table(conn)  # second call must not raise
        finally:
            conn.close()

    def test_primary_key_is_pid_and_machine(self, tmp_path: Path) -> None:
        import sqlite3 as _sq

        from nexus.db.migrations import migrate_liveness_table

        conn = _sq.connect(str(tmp_path / "memory.db"))
        try:
            migrate_liveness_table(conn)
            pk_cols = sorted(
                r[1]
                for r in conn.execute(
                    "PRAGMA table_info(liveness)"
                ).fetchall()
                if r[5] > 0
            )
        finally:
            conn.close()
        assert pk_cols == ["machine", "pid"]

    def test_store_init_creates_liveness_table(self, tmp_path: Path) -> None:
        """MemoryStore.__init__ must create the liveness table."""
        store = _store(tmp_path)
        try:
            tables = {
                r[0]
                for r in store.conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                ).fetchall()
            }
            assert "liveness" in tables
        finally:
            store.close()


# ── Upsert ───────────────────────────────────────────────────────────────────


class TestLivenessUpsert:
    def test_upsert_inserts_row(self, tmp_path: Path) -> None:
        store = _store(tmp_path)
        try:
            store.liveness_upsert(pid=1234, machine=_MACHINE, user_id=_USER)
            rows = store.liveness_list()
        finally:
            store.close()
        assert len(rows) == 1
        assert rows[0]["pid"] == 1234
        assert rows[0]["machine"] == _MACHINE
        assert rows[0]["user_id"] == _USER

    def test_upsert_optional_fields_stored(self, tmp_path: Path) -> None:
        store = _store(tmp_path)
        try:
            store.liveness_upsert(
                pid=1234,
                machine=_MACHINE,
                user_id=_USER,
                session="ses-abc",
                project="myproj",
                focus="rdr-111",
                activity="coding",
            )
            rows = store.liveness_list()
        finally:
            store.close()
        assert rows[0]["session"] == "ses-abc"
        assert rows[0]["project"] == "myproj"
        assert rows[0]["focus"] == "rdr-111"
        assert rows[0]["activity"] == "coding"

    def test_upsert_idempotent_same_pk(self, tmp_path: Path) -> None:
        """Same (pid, machine) must REPLACE, not INSERT a second row."""
        store = _store(tmp_path)
        t0 = 1_000_000.0
        t1 = 1_000_030.0
        try:
            store.liveness_upsert(pid=42, machine=_MACHINE, user_id=_USER, _now=t0)
            store.liveness_upsert(pid=42, machine=_MACHINE, user_id=_USER, _now=t1)
            rows = store.liveness_list()
        finally:
            store.close()
        assert len(rows) == 1

    def test_upsert_updates_last_seen(self, tmp_path: Path) -> None:
        """Repeated upsert must advance last_seen."""
        store = _store(tmp_path)
        t0 = 1_000_000.0
        t1 = 1_000_030.0
        try:
            store.liveness_upsert(pid=42, machine=_MACHINE, user_id=_USER, _now=t0)
            store.liveness_upsert(pid=42, machine=_MACHINE, user_id=_USER, _now=t1)
            rows = store.liveness_list()
        finally:
            store.close()
        assert rows[0]["last_seen"] == pytest.approx(t1)

    def test_upsert_distinct_pids_coexist(self, tmp_path: Path) -> None:
        store = _store(tmp_path)
        try:
            store.liveness_upsert(pid=1, machine=_MACHINE, user_id=_USER)
            store.liveness_upsert(pid=2, machine=_MACHINE, user_id=_USER)
            rows = store.liveness_list()
        finally:
            store.close()
        assert len(rows) == 2

    def test_upsert_distinct_machines_coexist(self, tmp_path: Path) -> None:
        store = _store(tmp_path)
        try:
            store.liveness_upsert(pid=1, machine="host-a", user_id=_USER)
            store.liveness_upsert(pid=1, machine="host-b", user_id=_USER)
            rows = store.liveness_list()
        finally:
            store.close()
        assert len(rows) == 2


# ── Sweep ────────────────────────────────────────────────────────────────────


class TestLivenessSweep:
    def test_sweep_removes_stale_rows(self, tmp_path: Path) -> None:
        """Rows with last_seen more than max_age_seconds ago are deleted."""
        store = _store(tmp_path)
        now = 1_000_100.0
        stale = now - 120  # 120 s ago, older than 60 s threshold
        try:
            store.liveness_upsert(pid=1, machine=_MACHINE, user_id=_USER, _now=stale)
            deleted = store.liveness_sweep(max_age_seconds=60, _now=now)
            rows = store.liveness_list()
        finally:
            store.close()
        assert deleted == 1
        assert rows == []

    def test_sweep_preserves_fresh_rows(self, tmp_path: Path) -> None:
        """Rows within max_age_seconds are not deleted."""
        store = _store(tmp_path)
        now = 1_000_100.0
        fresh = now - 10  # 10 s ago, well within 60 s threshold
        try:
            store.liveness_upsert(pid=1, machine=_MACHINE, user_id=_USER, _now=fresh)
            deleted = store.liveness_sweep(max_age_seconds=60, _now=now)
            rows = store.liveness_list()
        finally:
            store.close()
        assert deleted == 0
        assert len(rows) == 1

    def test_sweep_returns_count_of_deleted(self, tmp_path: Path) -> None:
        store = _store(tmp_path)
        now = 1_000_200.0
        stale = now - 90
        try:
            store.liveness_upsert(pid=1, machine="h1", user_id=_USER, _now=stale)
            store.liveness_upsert(pid=2, machine="h2", user_id=_USER, _now=stale)
            store.liveness_upsert(pid=3, machine="h3", user_id=_USER, _now=now - 5)
            deleted = store.liveness_sweep(max_age_seconds=60, _now=now)
        finally:
            store.close()
        assert deleted == 2

    def test_sweep_on_empty_table_returns_zero(self, tmp_path: Path) -> None:
        store = _store(tmp_path)
        try:
            deleted = store.liveness_sweep(max_age_seconds=60)
        finally:
            store.close()
        assert deleted == 0

    def test_sweep_default_max_age_is_sixty_seconds(self, tmp_path: Path) -> None:
        """Default max_age_seconds=60 — row at exactly 61 s is stale."""
        store = _store(tmp_path)
        now = 1_000_200.0
        try:
            store.liveness_upsert(pid=1, machine=_MACHINE, user_id=_USER, _now=now - 61)
            deleted = store.liveness_sweep(_now=now)
        finally:
            store.close()
        assert deleted == 1


# ── List ─────────────────────────────────────────────────────────────────────


class TestLivenessList:
    def test_list_empty_returns_empty(self, tmp_path: Path) -> None:
        store = _store(tmp_path)
        try:
            rows = store.liveness_list()
        finally:
            store.close()
        assert rows == []

    def test_list_returns_expected_keys(self, tmp_path: Path) -> None:
        store = _store(tmp_path)
        try:
            store.liveness_upsert(pid=1, machine=_MACHINE, user_id=_USER)
            rows = store.liveness_list()
        finally:
            store.close()
        assert len(rows) == 1
        r = rows[0]
        expected_keys = {
            "pid", "machine", "user_id", "session", "project",
            "focus", "activity", "last_seen",
        }
        assert set(r.keys()) == expected_keys

    def test_list_stable_order_pid_then_machine(self, tmp_path: Path) -> None:
        """Rows ordered by pid ASC, machine ASC."""
        store = _store(tmp_path)
        try:
            store.liveness_upsert(pid=3, machine="aaa", user_id=_USER)
            store.liveness_upsert(pid=1, machine="zzz", user_id=_USER)
            store.liveness_upsert(pid=1, machine="aaa", user_id=_USER)
            rows = store.liveness_list()
        finally:
            store.close()
        pids = [r["pid"] for r in rows]
        machines = [r["machine"] for r in rows]
        assert pids == [1, 1, 3]
        assert machines == ["aaa", "zzz", "aaa"]

    def test_list_none_fields_for_optional_columns(self, tmp_path: Path) -> None:
        store = _store(tmp_path)
        try:
            store.liveness_upsert(pid=1, machine=_MACHINE, user_id=_USER)
            rows = store.liveness_list()
        finally:
            store.close()
        r = rows[0]
        assert r["session"] is None
        assert r["project"] is None
        assert r["focus"] is None
        assert r["activity"] is None
