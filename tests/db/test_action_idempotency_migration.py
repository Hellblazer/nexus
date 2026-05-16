# SPDX-License-Identifier: AGPL-3.0-or-later
"""Tests for the action_idempotency migration + sweep helper (RDR-111 nexus-8wvs).

The migration creates the dedup table in memory.db; the sweep helper
deletes expired rows under the daemon's retention loop.
"""

from __future__ import annotations

import sqlite3
import time
from pathlib import Path

import pytest


def _memory_conn(tmp_path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(str(tmp_path / "memory.db"))
    conn.row_factory = sqlite3.Row
    return conn


class TestSchema:
    def test_migration_creates_table(self, tmp_path: Path) -> None:
        from nexus.db.migrations import migrate_action_idempotency_table

        conn = _memory_conn(tmp_path)
        try:
            migrate_action_idempotency_table(conn)
            tables = {
                r[0]
                for r in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                ).fetchall()
            }
            assert "action_idempotency" in tables
        finally:
            conn.close()

    def test_migration_creates_expires_index(self, tmp_path: Path) -> None:
        from nexus.db.migrations import migrate_action_idempotency_table

        conn = _memory_conn(tmp_path)
        try:
            migrate_action_idempotency_table(conn)
            indexes = {
                r[0]
                for r in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='index' "
                    "AND tbl_name='action_idempotency'"
                ).fetchall()
            }
            assert "idx_action_idempotency_expires" in indexes
        finally:
            conn.close()

    def test_migration_idempotent(self, tmp_path: Path) -> None:
        from nexus.db.migrations import migrate_action_idempotency_table

        conn = _memory_conn(tmp_path)
        try:
            migrate_action_idempotency_table(conn)
            migrate_action_idempotency_table(conn)  # second call must not raise
        finally:
            conn.close()

    def test_primary_key_is_idempotency_key(self, tmp_path: Path) -> None:
        from nexus.db.migrations import migrate_action_idempotency_table

        conn = _memory_conn(tmp_path)
        try:
            migrate_action_idempotency_table(conn)
            pk_cols = sorted(
                r[1]
                for r in conn.execute(
                    "PRAGMA table_info(action_idempotency)"
                ).fetchall()
                if r[5] > 0
            )
        finally:
            conn.close()
        assert pk_cols == ["idempotency_key"]


class TestStoreInit:
    """Fresh ``MemoryStore`` instances must create the table directly.

    Mirrors the existing liveness pattern — the registry migration covers
    upgrades from pre-RDR-111 databases, but store init seeds new
    ``memory.db`` files unconditionally so the dedup gate is functional
    on day one even before the next package-version bump activates the
    registry-based migration.
    """

    def test_store_init_creates_action_idempotency_table(
        self, tmp_path: Path
    ) -> None:
        from nexus.db.t2.memory_store import MemoryStore

        store = MemoryStore(tmp_path / "memory.db")
        try:
            tables = {
                r[0]
                for r in store.conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                ).fetchall()
            }
            assert "action_idempotency" in tables
        finally:
            store.close()


class TestSweep:
    def test_sweep_no_table_returns_zero(self, tmp_path: Path) -> None:
        from nexus.db.migrations import sweep_action_idempotency

        conn = _memory_conn(tmp_path)
        try:
            assert sweep_action_idempotency(conn) == 0
        finally:
            conn.close()

    def test_sweep_deletes_expired_rows(self, tmp_path: Path) -> None:
        from nexus.db.migrations import (
            migrate_action_idempotency_table,
            sweep_action_idempotency,
        )

        conn = _memory_conn(tmp_path)
        try:
            migrate_action_idempotency_table(conn)
            now = time.time()
            rows = [
                ("expired-1", now - 60),
                ("expired-2", now - 1),
                ("live-1", now + 600),
                ("live-2", now + 60),
            ]
            conn.executemany(
                "INSERT INTO action_idempotency (idempotency_key, expires_at) "
                "VALUES (?, ?)",
                rows,
            )
            conn.commit()
            deleted = sweep_action_idempotency(conn)
            assert deleted == 2
            remaining = {
                r[0]
                for r in conn.execute(
                    "SELECT idempotency_key FROM action_idempotency"
                ).fetchall()
            }
            assert remaining == {"live-1", "live-2"}
        finally:
            conn.close()

    def test_sweep_idempotent_on_empty_table(self, tmp_path: Path) -> None:
        from nexus.db.migrations import (
            migrate_action_idempotency_table,
            sweep_action_idempotency,
        )

        conn = _memory_conn(tmp_path)
        try:
            migrate_action_idempotency_table(conn)
            assert sweep_action_idempotency(conn) == 0
            assert sweep_action_idempotency(conn) == 0  # second call also zero
        finally:
            conn.close()
