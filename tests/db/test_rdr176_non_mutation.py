# SPDX-License-Identifier: AGPL-3.0-or-later
"""RDR-176 Phase 1 (Gap 2) — downgrade = NON-MUTATION invariant.

Pillar 2 of RDR-176: the 6.x -> service upgrade must leave the legacy 5.x T2
SQLite content-unchanged, so a downgrade is just "reinstall the prior CLI."

In *service mode* (``storage_backend_for("memory") == StorageBackend.SERVICE``)
the local ``.db`` is a migration SOURCE only. It must never be opened
read-write, migrated, or ``_nexus_version``-stamped. The bug surfaced in Hal's
6.0.0 dogfood (2026-06-29): opening the local T2 DB under 6.0.0 ran
``apply_pending`` and stamped ``_nexus_version`` forward, mutating the very DB
the migration treats as immutable and silently breaking the rollback guarantee.

This was the Phase-1 entry test (bead nexus-t9rmg.1), written failing-first
against ``T2Database.bootstrap_schema``. RDR-158 P4 Stage 4 (nexus-i711w)
deleted ``bootstrap_schema`` with the migration chain; the surviving ACT is
``T2Database(source_db, run_migrations=True)`` — the retained-and-ignored
parameter is now the pinned surface: construction must never regrow mutation
machinery against the frozen source, whatever the caller passes.

Content invariant is hashed via a logical ``.dump`` (``iterdump``) over a
read-only connection, which captures schema + row content while EXCLUDING the
``-wal`` / ``-shm`` sidecars (a read-only open may touch sidecars without
changing logical content). Assertions are exact equality, never inequalities
(mem:feedback_exact_assertions_for_fixture_regression).
"""
from __future__ import annotations

import hashlib
import shutil
import sqlite3
from pathlib import Path

import pytest

from nexus.db.t2 import T2Database

from tests._t2_fixture_ops import bootstrap_migration_source

#: The legacy version we stamp the seeded source DB to. Strictly below the
#: current ``expected_t2_schema_version()`` (5.10.7 at authoring time) so the
#: DB is "pending" and current code's bootstrap path would migrate + re-stamp
#: it — exactly the mutation this test forbids in service mode.
_LEGACY_VERSION = "5.10.6"


def _content_digest(path: Path) -> str:
    """SHA-256 of the logical ``.dump`` (schema + data), excluding sidecars."""
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    try:
        dump = "\n".join(conn.iterdump())
    finally:
        conn.close()
    return hashlib.sha256(dump.encode("utf-8")).hexdigest()


def _stored_version(path: Path) -> str | None:
    """Read ``_nexus_version.cli_version`` over a read-only connection.

    A 5.x CLI's ``hello`` handshake compares this exact string against its own
    built-against version (``daemon/t2_client.py`` exact-match guard); a forward
    stamp would raise ``T2SchemaVersionMismatchError`` for the downgraded CLI.
    """
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    try:
        row = conn.execute(
            "SELECT value FROM _nexus_version WHERE key='cli_version'"
        ).fetchone()
    finally:
        conn.close()
    return row[0] if row else None


def _seed_legacy_source_db(build_path: Path, dest_path: Path) -> None:
    """Produce a realistic legacy 5.x-shaped T2 source DB at *dest_path*.

    Builds the real current schema at *build_path* (a throwaway path so the
    in-process ``_upgrade_done`` cache does not later short-circuit the act
    step), checkpoints the WAL into the main file, stamps ``_nexus_version``
    down to :data:`_LEGACY_VERSION`, then copies the self-contained ``.db`` to
    *dest_path* (a fresh path the migration machinery has never seen).
    """
    # nexus-aqbrk: NOT bootstrap_schema. This file's SUBJECT is the
    # service-mode non-mutation guard, so it must RUN in service mode —
    # where bootstrap_schema early-returns by design and the legacy source
    # was therefore never built ('no such table: _nexus_version'). Pinning
    # would disable the branch under test; build the source directly.
    bootstrap_migration_source(build_path)
    conn = sqlite3.connect(str(build_path))
    try:
        conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        conn.execute("PRAGMA journal_mode=DELETE")
        conn.execute(
            "UPDATE _nexus_version SET value=? WHERE key='cli_version'",
            (_LEGACY_VERSION,),
        )
        conn.commit()
    finally:
        conn.close()
    shutil.copyfile(build_path, dest_path)


def test_service_mode_bootstrap_does_not_mutate_legacy_source_db(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Constructing T2Database over the legacy DB must leave it untouched —
    even with ``run_migrations=True`` (retained-and-ignored since RDR-158 P4
    Stage 4 deleted the migration machinery behind it).

    Content digest AND the stored version string must both be exactly equal
    before and after — proving the migration source is immutable and a prior
    CLI opens it with no version mismatch.
    """
    # Seed under the suite-wide sqlite pin so the real schema is built; the
    # source DB represents a legacy 5.x install that predates service mode.
    source_db = tmp_path / "memory.db"
    _seed_legacy_source_db(tmp_path / "seed.db", source_db)

    digest_before = _content_digest(source_db)
    version_before = _stored_version(source_db)
    assert version_before == _LEGACY_VERSION  # seed sanity

    # Now flip to service mode (later setenv wins over the pin): the local DB
    # is a migration SOURCE and must stay immutable.
    monkeypatch.setenv("NX_STORAGE_BACKEND", "service")

    # ACT: the open path that, before the Gap-2 guard (and before Stage 4
    # deleted the machinery outright), migrated + stamped.
    T2Database(source_db, run_migrations=True).close()

    # INVARIANT: source DB content and version are byte-for-content unchanged.
    assert _content_digest(source_db) == digest_before
    assert _stored_version(source_db) == _LEGACY_VERSION
