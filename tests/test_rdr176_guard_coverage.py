# SPDX-License-Identifier: AGPL-3.0-or-later
"""RDR-176 Phase 1 (Gap 2) — failing-first coverage for the three defense-in-depth
service-mode guards the primary tests did not exercise (substantive-critic
Significant-2): ``_run_upgrade``, ``run_t2_daemon``, and the doctor read-only
diagnostic connection.
"""
from __future__ import annotations

import hashlib
import sqlite3
from pathlib import Path

import pytest

from nexus.commands import doctor, upgrade
from nexus.daemon import t2_daemon
from nexus.db.t2 import T2Database


def _content_digest(path: Path) -> str:
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    try:
        return hashlib.sha256("\n".join(conn.iterdump()).encode("utf-8")).hexdigest()
    finally:
        conn.close()


def _seed_legacy_db(tmp_path: Path) -> Path:
    """Build a real T2 schema, stamp it to a legacy version, return the path."""
    build = tmp_path / "seed.db"
    T2Database.bootstrap_schema(build)
    conn = sqlite3.connect(str(build))
    try:
        conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        conn.execute("PRAGMA journal_mode=DELETE")
        conn.execute(
            "UPDATE _nexus_version SET value='5.10.6' WHERE key='cli_version'"
        )
        conn.commit()
    finally:
        conn.close()
    dest = tmp_path / "memory.db"
    dest.write_bytes(build.read_bytes())
    return dest


# ── Guard #4: nx upgrade no-ops in service mode ──────────────────────────────


def test_service_mode_run_upgrade_does_not_mutate_db(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:

    db_path = _seed_legacy_db(tmp_path)
    digest_before = _content_digest(db_path)
    monkeypatch.setattr(upgrade, "_db_path", lambda: db_path)

    monkeypatch.setenv("NX_STORAGE_BACKEND", "service")
    upgrade._run_upgrade(dry_run=False, force=False, auto_mode=False)

    assert _content_digest(db_path) == digest_before


# ── Guard #3: the SQLite T2 daemon does not start in service mode ─────────────


def test_service_mode_run_t2_daemon_does_not_construct_daemon(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:

    def _boom(*_a: object, **_k: object) -> None:
        raise AssertionError("T2Daemon must not be constructed in service mode")

    monkeypatch.setattr(t2_daemon, "T2Daemon", _boom)
    monkeypatch.setenv("NX_STORAGE_BACKEND", "service")

    # Must return cleanly without instantiating (or starting) the daemon.
    t2_daemon.run_t2_daemon(config_dir=tmp_path, db_path=tmp_path / "memory.db")


# ── Guard #5: doctor diagnostics open read-only (no WAL header write) ─────────


def test_t2_diagnostic_connect_service_mode_is_read_only(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:

    db_path = _seed_legacy_db(tmp_path)  # journal_mode=DELETE on disk
    digest_before = _content_digest(db_path)

    monkeypatch.setenv("NX_STORAGE_BACKEND", "service")
    conn = doctor._t2_diagnostic_connect(db_path, sqlite3)
    try:
        # Read-only: a write must be rejected, and the WAL pragma must NOT have
        # been forced (which would rewrite the DB header = a mutation).
        with pytest.raises(sqlite3.OperationalError):
            conn.execute("CREATE TABLE _probe (x INTEGER)")
        mode = conn.execute("PRAGMA journal_mode").fetchone()[0]
        assert mode != "wal"
    finally:
        conn.close()

    assert _content_digest(db_path) == digest_before


def test_t2_diagnostic_connect_sqlite_mode_is_writable_wal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Positive control: in sqlite mode the helper keeps the historical
    writable WAL connection (the guard is mode-gated, not an unconditional ro)."""

    db_path = _seed_legacy_db(tmp_path)
    monkeypatch.setenv("NX_STORAGE_BACKEND", "sqlite")
    conn = doctor._t2_diagnostic_connect(db_path, sqlite3)
    try:
        assert conn.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
    finally:
        conn.close()
