# SPDX-License-Identifier: AGPL-3.0-or-later
"""RDR-140 P1.1 (nexus-266iu) — TDD: migration current_version fast-path.

Pins the intended cold-start fast path for ``T2Database.bootstrap_schema``:
when the on-disk DB is already at ``current_version`` AND already in WAL
journal mode, a cold start (fresh process: ``_upgrade_done`` empty) must
short-circuit BEFORE acquiring the cross-process migration flock or running
``_apply_pending_with_lock_retry`` — i.e. it takes NO writer lock. A genuine
pending migration (stale stored version, or non-WAL journal) must STILL run
the full flock + apply path so the fast path never swallows a real upgrade.

These tests are written to FAIL against current code (which always enters the
flock + writer-locking apply path on a cold start). P1.2 (nexus-2p52a)
implements the lock-free pre-check that turns them green. Do NOT change
production code in this bead.

A3 (verified): the pre-check must use the lock-free reads ``SELECT value FROM
_nexus_version WHERE key='cli_version'`` + ``PRAGMA journal_mode`` (read),
mirroring ``T2Database.stored_schema_version`` — NOT ``bootstrap_version``,
which is a writer.
"""
from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from importlib.metadata import version as _pkg_version
from pathlib import Path

import pytest


def _current_version() -> str:
    return _pkg_version("conexus")


def _stored_version(db_path: Path) -> str:
    conn = sqlite3.connect(str(db_path))
    try:
        row = conn.execute(
            "SELECT value FROM _nexus_version WHERE key='cli_version'"
        ).fetchone()
        return row[0] if row else "0.0.0"
    finally:
        conn.close()


def _journal_mode(db_path: Path) -> str:
    conn = sqlite3.connect(str(db_path))
    try:
        return conn.execute("PRAGMA journal_mode").fetchone()[0].lower()
    finally:
        conn.close()


def _stamp_version(db_path: Path, value: str) -> None:
    conn = sqlite3.connect(str(db_path))
    try:
        conn.execute(
            "INSERT OR REPLACE INTO _nexus_version (key, value) "
            "VALUES ('cli_version', ?)",
            (value,),
        )
        conn.commit()
    finally:
        conn.close()


def _make_migrated_wal_db(db_path: Path) -> None:
    """Build a realistic 'already fully migrated, WAL-on' DB.

    A bare tmp-path bootstrap leaves the stored version at ``0.0.0`` because
    two RDR-108 PK migrations defer when no catalog exists, so the run never
    stamps ``current_version``. We run the real bootstrap (genuine schema +
    WAL) and then stamp the version row to ``current_version`` to represent
    the steady-state, fully-migrated DB a cold start would actually meet in
    production.
    """
    from nexus.db.t2 import T2Database

    T2Database.bootstrap_schema(db_path)
    _stamp_version(db_path, _current_version())


def _clear_upgrade_done() -> None:
    """Simulate a cold start in a fresh process: the in-process
    ``_upgrade_done`` memo is empty, so only the on-disk state can
    short-circuit the migration."""
    from nexus.db.migrations import _upgrade_done

    _upgrade_done.clear()


@pytest.fixture
def installed_sentinels(monkeypatch):
    """Wrap ``t2_migration_flock`` and ``_apply_pending_with_lock_retry`` with
    call counters that still delegate to the real implementations.

    ``t2_migration_flock`` entry count and ``_apply_pending_with_lock_retry``
    call count are both proxies for "took the writer-locking migration path".
    On the fast path both must be 0; on a real pending migration both must
    be exactly 1.
    """
    import nexus.db.migrations as migrations
    import nexus.db.t2 as t2

    counts = {"flock": 0, "apply": 0}

    real_flock = migrations.t2_migration_flock
    real_apply = t2._apply_pending_with_lock_retry

    @contextmanager
    def counting_flock(parent):
        counts["flock"] += 1
        with real_flock(parent):
            yield

    def counting_apply(conn, current_version):
        counts["apply"] += 1
        return real_apply(conn, current_version)

    monkeypatch.setattr(migrations, "t2_migration_flock", counting_flock)
    monkeypatch.setattr(t2, "_apply_pending_with_lock_retry", counting_apply)
    return counts


class TestMigrationFastPath:
    def test_already_migrated_wal_db_takes_no_writer_lock_on_cold_start(
        self, tmp_path: Path, installed_sentinels: dict,
    ) -> None:
        from nexus.db.t2 import T2Database

        db = tmp_path / "memory.db"

        # Steady state: schema built, version at current, journal in WAL.
        _make_migrated_wal_db(db)
        assert _stored_version(db) == _current_version()
        assert _journal_mode(db) == "wal"

        # Reset to simulate a brand-new process: nothing memoised in-process,
        # but the on-disk DB is already fully migrated and in WAL.
        _clear_upgrade_done()
        installed_sentinels["flock"] = 0
        installed_sentinels["apply"] = 0

        # Second cold start MUST short-circuit before any writer-locking work.
        T2Database.bootstrap_schema(db)

        assert installed_sentinels["flock"] == 0
        assert installed_sentinels["apply"] == 0

    def test_nonexistent_db_returns_false_without_creating_file(
        self, tmp_path: Path,
    ) -> None:
        """The probe is read-only by contract: on a non-existent path it must
        return False AND not materialise a 0-byte DB file (a plain
        ``sqlite3.connect`` would)."""
        from nexus.db.t2 import _cold_start_is_current_and_wal

        db = tmp_path / "never.db"
        assert _cold_start_is_current_and_wal(db) is False
        assert not db.exists()

    def test_existing_db_without_version_table_returns_false(
        self, tmp_path: Path,
    ) -> None:
        """An existing file with no ``_nexus_version`` table (skeleton DB /
        bootstrap-in-progress) must fall through, not fast-path."""
        from nexus.db.t2 import _cold_start_is_current_and_wal

        db = tmp_path / "skeleton.db"
        conn = sqlite3.connect(str(db))  # materialise an empty, schema-less DB
        conn.close()
        assert db.exists()
        assert _cold_start_is_current_and_wal(db) is False

    def test_zero_zero_zero_current_version_returns_false(
        self, tmp_path: Path, monkeypatch,
    ) -> None:
        """A ``0.0.0`` current version (broken/editable install) must never
        match a real stored version — the probe returns False so the full
        path runs."""
        from nexus.db.t2 import _cold_start_is_current_and_wal

        db = tmp_path / "memory.db"
        _make_migrated_wal_db(db)
        assert _stored_version(db) == _current_version()
        assert _journal_mode(db) == "wal"

        monkeypatch.setattr("importlib.metadata.version", lambda _name: "0.0.0")
        assert _cold_start_is_current_and_wal(db) is False

    def test_stale_stored_version_still_runs_full_migration_path(
        self, tmp_path: Path, installed_sentinels: dict,
    ) -> None:
        from nexus.db.t2 import T2Database

        db = tmp_path / "memory.db"
        _make_migrated_wal_db(db)

        # Force a genuine pending migration by rewinding the stored version.
        _stamp_version(db, "0.0.0")
        assert _stored_version(db) == "0.0.0"

        _clear_upgrade_done()
        installed_sentinels["flock"] = 0
        installed_sentinels["apply"] = 0

        T2Database.bootstrap_schema(db)

        # Full writer-locking migration path taken exactly once.
        assert installed_sentinels["flock"] == 1
        assert installed_sentinels["apply"] == 1

    def test_non_wal_journal_still_runs_full_migration_path(
        self, tmp_path: Path, installed_sentinels: dict,
    ) -> None:
        from nexus.db.t2 import T2Database

        db = tmp_path / "memory.db"
        _make_migrated_wal_db(db)

        # Knock the journal out of WAL: the fast path must NOT short-circuit
        # because re-enabling WAL is itself a writer operation that has to run.
        conn = sqlite3.connect(str(db))
        try:
            conn.execute("PRAGMA journal_mode=DELETE")
        finally:
            conn.close()
        assert _journal_mode(db) == "delete"

        _clear_upgrade_done()
        installed_sentinels["flock"] = 0
        installed_sentinels["apply"] = 0

        T2Database.bootstrap_schema(db)

        assert installed_sentinels["flock"] == 1
        assert installed_sentinels["apply"] == 1
        assert _journal_mode(db) == "wal"
