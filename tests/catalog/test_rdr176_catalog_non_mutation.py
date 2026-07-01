# SPDX-License-Identifier: AGPL-3.0-or-later
"""RDR-176 Phase 1 (Gap 2) — ``.catalog.db`` is immutable in service mode.

The downgrade=non-mutation guarantee covers ``.catalog.db`` as well as
``memory.db``. ``CatalogStore.__init__`` runs schema DDL + ``ALTER TABLE``
migrations and ``Catalog.__init__`` appends to ``events.jsonl`` /
``_ensure_consistent``-rebuilds the SQLite cache on every non-``read_only``
construction. Every ``nx catalog`` CLI command and the daemon's
``_build_hosted_catalog`` construct ``Catalog(dir, .catalog.db)`` — so in
service mode the single seam (force ``read_only`` in ``Catalog.__init__``) must
leave the file byte-for-content unchanged. Covers code-review H-1 and the
substantive-critic's ``_build_hosted_catalog`` finding (it routes through the
same guarded constructor).
"""
from __future__ import annotations

import hashlib
import sqlite3
from pathlib import Path

import pytest

from nexus.catalog.catalog import Catalog


def _content_digest(path: Path) -> str:
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    try:
        return hashlib.sha256("\n".join(conn.iterdump()).encode("utf-8")).hexdigest()
    finally:
        conn.close()


def test_service_mode_catalog_construction_is_read_only_and_non_mutating(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cat_dir = tmp_path / "catalog"
    cat_dir.mkdir()
    db_path = cat_dir / ".catalog.db"

    # Seed under the suite-wide sqlite pin: a real, fully-migrated catalog DB.
    seed = Catalog(cat_dir, db_path)
    seed.close()
    digest_before = _content_digest(db_path)

    # Flip to service mode: the local catalog is now a frozen migration source.
    monkeypatch.setenv("NX_STORAGE_BACKEND", "service")

    cat = Catalog(cat_dir, db_path)
    try:
        # The guard forces read-only regardless of the caller's flag.
        assert cat._read_only is True
    finally:
        cat.close()

    # No schema DDL, ALTER, events.jsonl backfill, or rebuild touched the file.
    assert _content_digest(db_path) == digest_before


def test_service_mode_guard_is_existence_gated_for_fresh_db(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The non-mutation guard protects an EXISTING legacy ``.catalog.db`` only.

    A never-existed catalog has no prior 5.x content to preserve, and a
    ``mode=ro`` open cannot create the file. So a first-ever construction in
    service mode is NOT forced read-only — it proceeds normally rather than
    raising ``OperationalError: unable to open database file``.
    """
    cat_dir = tmp_path / "catalog"
    cat_dir.mkdir()
    db_path = cat_dir / ".catalog.db"
    assert not db_path.exists()

    monkeypatch.setenv("NX_STORAGE_BACKEND", "service")

    cat = Catalog(cat_dir, db_path)  # must not raise
    try:
        assert cat._read_only is False
    finally:
        cat.close()
