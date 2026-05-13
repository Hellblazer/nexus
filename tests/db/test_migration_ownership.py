"""Tests for the Phase-0 migration-ownership refactor (RDR-112 P0.4 / nexus-uqqy).

Contract:

- ``T2Database.__init__`` no longer drives migrations as a constructor
  side effect. The transient connection + ``apply_pending`` dance has
  moved to an explicit module-level helper.
- ``nexus.db.migrations.run_if_needed(path)`` is the new public entry
  point and is idempotent within a process (memoised via the existing
  ``_upgrade_done`` cache).
- A stale on-disk DB whose ``_nexus_version`` is behind the running
  package version is upgraded by an explicit ``run_if_needed(path)``
  call but NOT by ``T2Database(path)`` alone.

Phase 1 (nexus-w0et) moves migration ownership into the daemon-startup
runner; this bead only severs the constructor-driven trigger.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from nexus.db.migrations import _upgrade_done, _upgrade_lock, run_if_needed
from nexus.db.t2 import T2Database

pytestmark = pytest.mark.no_auto_migrate


def _read_stored_version(path: Path) -> str | None:
    conn = sqlite3.connect(str(path))
    try:
        row = conn.execute(
            "SELECT value FROM _nexus_version WHERE key='cli_version'"
        ).fetchone()
        return row[0] if row else None
    finally:
        conn.close()


def _clear_upgrade_cache(path: Path) -> None:
    """Drop the process-level upgrade-done memo so tests can re-run."""
    with _upgrade_lock:
        _upgrade_done.discard(str(path.resolve()))


def test_t2database_init_does_not_mutate_schema_as_side_effect(tmp_path):
    """Constructing T2Database must not run migrations.

    Domain stores still create their own tables in ``_init_schema``,
    but the ``_nexus_version`` row (owned by ``apply_pending``) must
    NOT be populated by construction alone.
    """
    db_path = tmp_path / "t2.db"
    _clear_upgrade_cache(db_path)

    with T2Database(db_path) as _db:
        pass

    # `_nexus_version` must be absent — only `apply_pending` creates it.
    conn = sqlite3.connect(str(db_path))
    try:
        row = conn.execute(
            "SELECT name FROM sqlite_master "
            "WHERE type='table' AND name='_nexus_version'"
        ).fetchone()
    finally:
        conn.close()
    assert row is None, (
        "_nexus_version table created as a T2Database construction side effect; "
        "apply_pending must be the sole writer (RDR-112 P0.4)"
    )


def test_run_if_needed_is_idempotent(tmp_path):
    """A second call on the same path must be a no-op."""
    db_path = tmp_path / "t2.db"
    _clear_upgrade_cache(db_path)

    run_if_needed(db_path)
    version_after_first = _read_stored_version(db_path)
    assert version_after_first is not None

    run_if_needed(db_path)
    version_after_second = _read_stored_version(db_path)
    assert version_after_first == version_after_second


def test_run_if_needed_upgrades_a_stale_db(tmp_path):
    """An explicit call must upgrade a pre-existing on-disk DB."""
    db_path = tmp_path / "t2.db"
    _clear_upgrade_cache(db_path)

    # First call: bootstrap + migrate.
    run_if_needed(db_path)
    initial_version = _read_stored_version(db_path)
    assert initial_version is not None

    # Simulate a stale install: pin the stored version to 0.0.0 and
    # clear the process cache so the next call re-runs.
    conn = sqlite3.connect(str(db_path))
    try:
        conn.execute(
            "UPDATE _nexus_version SET value='0.0.0' WHERE key='cli_version'"
        )
        conn.commit()
    finally:
        conn.close()
    _clear_upgrade_cache(db_path)

    # Second explicit call: must upgrade.
    run_if_needed(db_path)
    upgraded = _read_stored_version(db_path)
    assert upgraded == initial_version


def test_t2database_init_works_without_prior_migration(tmp_path):
    """Domain stores must function on a fresh path even with no migration.

    Each store ``_init_schema`` is self-sufficient; the constructor
    must not require ``apply_pending`` to have run first.
    """
    db_path = tmp_path / "t2.db"
    _clear_upgrade_cache(db_path)

    with T2Database(db_path) as db:
        db.memory.put("test-project", "test-title", "test body")
        rows = list(db.memory.get("test-project", "test-title"))
    assert rows, "memory store unusable without prior migration"


def test_run_if_needed_creates_parent_directory(tmp_path):
    """The helper accepts a path whose parent does not yet exist."""
    db_path = tmp_path / "nested" / "deeper" / "t2.db"
    _clear_upgrade_cache(db_path)

    run_if_needed(db_path)
    assert db_path.exists()
