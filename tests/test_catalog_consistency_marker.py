# SPDX-License-Identifier: AGPL-3.0-or-later
"""nexus-wehp: cross-process consistency-marker regression test.

The pre-fix behaviour: every Catalog() construction with a non-empty
documents.jsonl reset _last_consistency_mtime to 0.0 and triggered a
full DELETE+replay rebuild via _ensure_consistent. Two CLI processes
running while nx-mcp held an open SQLite connection produced
'database is locked' errors at write time because the rebuild's
DELETE FROM links contended with MCP's held read transaction.

The fix persists the highest successfully-projected canonical mtime
inside the catalog SQLite itself (the ``_meta`` table). Processes
sharing a SQLite cache see the marker and skip the rebuild when no
canonical-source file has been written since. A fresh SQLite cache
naturally has no marker (returns 0.0) and triggers a rebuild,
preserving the pre-fix invariant that the cache always reflects
the canonical state.
"""
from __future__ import annotations

import os
from pathlib import Path

import pytest

from nexus.catalog.catalog import Catalog


@pytest.fixture
def seeded_catalog_dir(tmp_path: Path) -> Path:
    """Catalog dir with a populated documents.jsonl so _ensure_consistent runs.

    Closes the fixture's SQLite connection before yielding so the test
    can open a fresh Catalog without same-process lock contention.
    Production semantics (MCP and CLI in different processes) don't
    have this issue; tests need an explicit close.
    """
    cat = Catalog(tmp_path, tmp_path / "catalog.db")
    owner = cat.register_owner(name="seed-owner", owner_type="repo", repo_hash="seed-hash")
    cat.register(owner=owner, title="seed-doc", content_type="prose", file_path="seed.md")
    cat._db.close()
    return tmp_path


def _make_catalog(catalog_dir: Path, db_name: str = "catalog.db") -> Catalog:
    return Catalog(catalog_dir, catalog_dir / db_name)


def test_marker_written_on_successful_rebuild(seeded_catalog_dir: Path) -> None:
    """A successful _ensure_consistent run persists the marker into _meta."""
    cat = _make_catalog(seeded_catalog_dir)
    row = cat._db.execute(
        "SELECT value FROM _meta WHERE key = ?",
        ("last_consistency_mtime",),
    ).fetchone()
    assert row is not None, "construction over an existing catalog should write the marker"
    persisted = float(row[0])
    assert persisted > 0
    assert cat._last_consistency_mtime == persisted


def test_marker_skips_rebuild_when_unchanged(seeded_catalog_dir: Path) -> None:
    """Two constructions sharing the SAME SQLite skip the second rebuild.

    Verified by ensuring the second construction's _last_consistency_mtime
    matches the persisted marker (set by the first), not 0.0 (the pre-fix
    every-instance reset value).
    """
    cat1 = _make_catalog(seeded_catalog_dir)
    first_mtime = cat1._last_consistency_mtime
    assert first_mtime > 0

    cat2 = _make_catalog(seeded_catalog_dir)
    assert cat2._last_consistency_mtime == first_mtime, (
        "second construction must read the persisted in-DB marker, "
        "not reset to 0.0 and re-rebuild"
    )


def test_fresh_sqlite_cache_against_existing_catalog_forces_rebuild(
    seeded_catalog_dir: Path,
) -> None:
    """A fresh SQLite cache file MUST rebuild against the canonical state.

    This is the critical invariant the in-DB marker preserves: a sidecar
    marker file would incorrectly suppress the rebuild on a fresh cache,
    leaving the new SQLite empty even though documents.jsonl has rows.
    Putting the marker inside the SQLite itself means a fresh DB has no
    marker, returns 0.0, and the rebuild fires.
    """
    cat_fresh = _make_catalog(seeded_catalog_dir, db_name="catalog-fresh.db")
    doc_count = cat_fresh._db.execute(
        "SELECT count(*) FROM documents"
    ).fetchone()[0]
    assert doc_count > 0, (
        "fresh SQLite cache against existing catalog dir must rebuild "
        "from canonical state"
    )


def test_marker_advances_after_external_write(seeded_catalog_dir: Path) -> None:
    """A canonical-file mtime advance forces a rebuild and updates the marker."""
    cat1 = _make_catalog(seeded_catalog_dir)
    initial_mtime = cat1._last_consistency_mtime
    assert initial_mtime > 0

    docs_path = seeded_catalog_dir / "documents.jsonl"
    future = initial_mtime + 10
    os.utime(docs_path, (future, future))

    cat2 = _make_catalog(seeded_catalog_dir)
    assert cat2._last_consistency_mtime >= future, (
        "second construction should detect the advanced documents.jsonl "
        "mtime, rebuild, and update the marker"
    )


def test_marker_table_created_idempotently(tmp_path: Path) -> None:
    """Constructing a Catalog against an empty dir is safe; no error on _meta."""
    cat = _make_catalog(tmp_path)
    # Should not raise; _meta table exists, marker query returns None → 0.0.
    assert cat._last_consistency_mtime == 0.0


def test_marker_lives_inside_sqlite_not_on_disk(seeded_catalog_dir: Path) -> None:
    """No sidecar file polluting the catalog directory."""
    _make_catalog(seeded_catalog_dir)
    sidecar = seeded_catalog_dir / ".last_consistency_mtime"
    assert not sidecar.exists(), (
        "marker should live inside the SQLite _meta table, not as a "
        "sidecar file on disk"
    )
