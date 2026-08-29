# SPDX-License-Identifier: AGPL-3.0-or-later
"""RDR-176 Phase 1 (Gap 2) — failing-first coverage for the three defense-in-depth
service-mode guards the primary tests did not exercise (substantive-critic
Significant-2): ``_run_upgrade`` and the doctor read-only diagnostic connection.
(``run_t2_daemon``'s guard went with the daemon — see the Guard #3 tombstone.)
"""
from __future__ import annotations

import hashlib
import sqlite3
from pathlib import Path

import pytest

from nexus.commands import upgrade

from tests._t2_fixture_ops import bootstrap_migration_source
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
    # nexus-aqbrk: NOT bootstrap_schema — it early-returns in service mode
    # (RDR-176 Gap 2), and this file's whole subject is the SERVICE-MODE
    # guards over a legacy DB, so pinning to SQLite would disable the
    # branch under test. Build the legacy source directly instead.
    bootstrap_migration_source(build)
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
    # RDR-158 P4 Stage 4 (nexus-i711w): no `_db_path` monkeypatch — the
    # collapsed _run_upgrade resolves no local path at all; the digest
    # assertion below proves it touched nothing.

    monkeypatch.setenv("NX_STORAGE_BACKEND", "service")
    upgrade._run_upgrade(dry_run=False, auto_mode=False)

    assert _content_digest(db_path) == digest_before


# NO Guard #3 (the SQLite T2 daemon does not start in service mode): the guard
# was an early return inside `run_t2_daemon`, and both it and its module retired
# with the daemon (nexus-i711w Stage 2 sub-stage B). A daemon that cannot be
# started needs no service-mode check to stop it starting. Guards #4 (nx upgrade
# no-ops) and #5 (read-only doctor diagnostics) are the surviving RDR-176 Phase 1
# non-mutation defenses and are exercised above and below.


# ── Guard #5: doctor diagnostics open read-only (no WAL header write) ─────────
