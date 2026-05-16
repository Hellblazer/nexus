# SPDX-License-Identifier: AGPL-3.0-or-later
"""Pre-migration backup helpers (RDR-112, nexus-uvv1).

The T2 daemon snapshots ``memory.db`` before ``apply_pending`` runs so
a partial-failure mid-list leaves a recoverable backup beside the live
file. Tests cover the backup primitive, the orchestrator helper, the
opt-out env var, and the retention pruner.
"""

from __future__ import annotations

import sqlite3
import time
from pathlib import Path

import pytest

from nexus.db import migrations as migrations_mod


def _seed_db(path: Path) -> None:
    conn = sqlite3.connect(str(path))
    try:
        conn.execute("CREATE TABLE t (id INTEGER PRIMARY KEY, name TEXT)")
        conn.executemany(
            "INSERT INTO t (name) VALUES (?)",
            [("alice",), ("bob",), ("carol",)],
        )
        conn.commit()
    finally:
        conn.close()


def _count_rows(path: Path) -> int:
    conn = sqlite3.connect(str(path))
    try:
        return conn.execute("SELECT COUNT(*) FROM t").fetchone()[0]
    finally:
        conn.close()


class TestBackupSqliteDb:
    """The low-level backup primitive uses SQLite's online backup API."""

    def test_copies_full_database(self, tmp_path: Path) -> None:
        src = tmp_path / "src.db"
        dst = tmp_path / "src.db.bak"
        _seed_db(src)

        migrations_mod._backup_sqlite_db(src, dst)
        assert dst.exists()
        assert _count_rows(dst) == 3

    def test_backup_is_self_consistent_with_wal(self, tmp_path: Path) -> None:
        """WAL mode source still produces a consistent snapshot."""
        src = tmp_path / "src.db"
        _seed_db(src)
        conn = sqlite3.connect(str(src))
        try:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("INSERT INTO t (name) VALUES ('dave')")
            conn.commit()
        finally:
            conn.close()

        dst = tmp_path / "wal-src.db.bak"
        migrations_mod._backup_sqlite_db(src, dst)
        assert _count_rows(dst) == 4


class TestBackupDbPreMigration:
    """The orchestrator helper applies the env gate + naming + retention."""

    def test_creates_backup_with_versioned_name(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        monkeypatch.delenv("NX_MIGRATION_BACKUP", raising=False)
        src = tmp_path / "memory.db"
        _seed_db(src)

        result = migrations_mod._backup_db_pre_migration(
            src, from_version="4.32.12"
        )
        assert result is not None
        assert result.exists()
        assert result.name.startswith("memory.db.bak-4.32.12-")
        assert _count_rows(result) == 3

    def test_skipped_when_env_opted_out(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        monkeypatch.setenv("NX_MIGRATION_BACKUP", "0")
        src = tmp_path / "memory.db"
        _seed_db(src)

        result = migrations_mod._backup_db_pre_migration(
            src, from_version="4.32.12"
        )
        assert result is None
        siblings = list(tmp_path.glob("memory.db.bak-*"))
        assert siblings == []

    @pytest.mark.parametrize("falsy", ["0", "false", "False"])
    def test_recognises_falsy_opt_outs(
        self, tmp_path: Path, monkeypatch, falsy: str
    ) -> None:
        monkeypatch.setenv("NX_MIGRATION_BACKUP", falsy)
        src = tmp_path / "memory.db"
        _seed_db(src)
        assert (
            migrations_mod._backup_db_pre_migration(src, from_version="x")
            is None
        )

    def test_missing_db_file_skips_backup(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        """Fresh installs (no DB on disk yet) skip the backup cleanly."""
        monkeypatch.delenv("NX_MIGRATION_BACKUP", raising=False)
        src = tmp_path / "absent.db"
        assert (
            migrations_mod._backup_db_pre_migration(src, from_version="0.0.0")
            is None
        )


class TestPruneOldBackups:
    """Retention keeps the newest N and removes the rest."""

    def test_prunes_to_keep_limit(self, tmp_path: Path) -> None:
        src = tmp_path / "memory.db"
        src.write_text("")  # path must exist for the .parent walk

        # Create five backup files with increasing mtime.
        for i in range(5):
            p = tmp_path / f"memory.db.bak-old-{i}"
            p.write_text("snapshot")
            mtime = time.time() - (10 - i)  # earliest first
            import os as _os
            _os.utime(p, (mtime, mtime))

        removed = migrations_mod._prune_old_backups(src, keep=2)
        assert removed == 3
        survivors = sorted(p.name for p in tmp_path.glob("memory.db.bak-*"))
        # Two newest (indices 3 and 4) survive.
        assert survivors == ["memory.db.bak-old-3", "memory.db.bak-old-4"]

    def test_no_op_when_under_limit(self, tmp_path: Path) -> None:
        src = tmp_path / "memory.db"
        src.write_text("")
        (tmp_path / "memory.db.bak-only-one").write_text("snapshot")

        removed = migrations_mod._prune_old_backups(src, keep=3)
        assert removed == 0
        assert len(list(tmp_path.glob("memory.db.bak-*"))) == 1


class TestRunDaemonMigrationsBackup:
    """End-to-end: daemon startup snapshots memory.db before migrating."""

    def test_backup_taken_when_from_version_differs(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        monkeypatch.delenv("NX_MIGRATION_BACKUP", raising=False)
        memory_db = tmp_path / "memory.db"
        tuples_db = tmp_path / "tuples.db"

        # Seed memory.db with the version-tracking row at an older version
        # so apply_pending sees a non-zero from_version. We don't run actual
        # migrations because all current MIGRATIONS list entries are >=
        # 4.33.0 — the package version (4.32.12) keeps them filtered out.
        conn = sqlite3.connect(str(memory_db))
        try:
            conn.execute(
                "CREATE TABLE _nexus_version (key TEXT PRIMARY KEY, value TEXT)"
            )
            conn.execute(
                "INSERT INTO _nexus_version (key, value) VALUES "
                "('cli_version', '0.0.0')"
            )
            conn.commit()
        finally:
            conn.close()

        migrations_mod.run_daemon_migrations(memory_db, tuples_db)

        backups = sorted(tmp_path.glob("memory.db.bak-0.0.0-*"))
        assert backups, "expected a backup file beside memory.db"
        # Backup contains the original schema state.
        bak_conn = sqlite3.connect(str(backups[0]))
        try:
            value = bak_conn.execute(
                "SELECT value FROM _nexus_version WHERE key='cli_version'"
            ).fetchone()
        finally:
            bak_conn.close()
        assert value[0] == "0.0.0"

    def test_no_backup_when_from_version_matches_current(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        monkeypatch.delenv("NX_MIGRATION_BACKUP", raising=False)
        memory_db = tmp_path / "memory.db"
        tuples_db = tmp_path / "tuples.db"

        # Set stored version equal to the current package version so
        # apply_pending sees nothing to do.
        current = migrations_mod._pkg_version_str()
        conn = sqlite3.connect(str(memory_db))
        try:
            conn.execute(
                "CREATE TABLE _nexus_version (key TEXT PRIMARY KEY, value TEXT)"
            )
            conn.execute(
                "INSERT INTO _nexus_version (key, value) VALUES "
                "('cli_version', ?)",
                (current,),
            )
            conn.commit()
        finally:
            conn.close()

        migrations_mod.run_daemon_migrations(memory_db, tuples_db)

        assert list(tmp_path.glob("memory.db.bak-*")) == []

    def test_opt_out_skips_backup_even_with_version_drift(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        monkeypatch.setenv("NX_MIGRATION_BACKUP", "0")
        memory_db = tmp_path / "memory.db"
        tuples_db = tmp_path / "tuples.db"
        conn = sqlite3.connect(str(memory_db))
        try:
            conn.execute(
                "CREATE TABLE _nexus_version (key TEXT PRIMARY KEY, value TEXT)"
            )
            conn.execute(
                "INSERT INTO _nexus_version (key, value) VALUES "
                "('cli_version', '0.0.0')"
            )
            conn.commit()
        finally:
            conn.close()

        migrations_mod.run_daemon_migrations(memory_db, tuples_db)
        assert list(tmp_path.glob("memory.db.bak-*")) == []
