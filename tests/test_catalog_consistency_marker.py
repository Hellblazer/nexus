# SPDX-License-Identifier: AGPL-3.0-or-later
"""nexus-wehp: cross-process consistency-marker regression test.

The pre-fix behaviour: every Catalog() construction with a non-empty
documents.jsonl reset _last_consistency_mtime to 0.0 and triggered a
full DELETE+replay rebuild via _ensure_consistent. Two CLI processes
running while nx-mcp held an open SQLite connection produced
'database is locked' errors at write time because the rebuild's
DELETE FROM links contended with MCP's held read transaction.

The fix persists the highest successfully-projected canonical mtime
to ``.last_consistency_mtime`` in the catalog directory; new
processes read it on construction and skip the rebuild when no
canonical-source file has been written since.
"""
from __future__ import annotations

import os
from pathlib import Path

import pytest

from nexus.catalog.catalog import Catalog


@pytest.fixture
def seeded_catalog_dir(tmp_path: Path) -> Path:
    """Catalog dir with a populated documents.jsonl so _ensure_consistent runs."""
    cat = Catalog(tmp_path, tmp_path / "catalog.db")
    owner = cat.register_owner(name="seed-owner", owner_type="repo", repo_hash="seed-hash")
    cat.register(owner=owner, title="seed-doc", content_type="prose", file_path="seed.md")
    return tmp_path


def _make_catalog(catalog_dir: Path) -> Catalog:
    return Catalog(catalog_dir, catalog_dir / "catalog.db")


def test_consistency_marker_written_on_successful_rebuild(seeded_catalog_dir: Path) -> None:
    """A successful _ensure_consistent run persists the marker file.

    The seed fixture already constructed once; constructing again over
    the seeded state writes the marker because _ensure_consistent
    fires when documents.jsonl exists.
    """
    cat = _make_catalog(seeded_catalog_dir)
    marker = seeded_catalog_dir / ".last_consistency_mtime"
    assert marker.exists(), "construction over an existing catalog should write the marker"
    persisted = float(marker.read_text().strip())
    assert persisted > 0
    assert cat._last_consistency_mtime == persisted


def test_consistency_marker_skips_rebuild_when_unchanged(seeded_catalog_dir: Path) -> None:
    """Two constructions back-to-back: the second skips the rebuild.

    Verified by ensuring the second construction's _last_consistency_mtime
    matches the persisted marker (set by the first), not 0.0 (the pre-fix
    every-instance reset value).
    """
    cat1 = _make_catalog(seeded_catalog_dir)
    first_mtime = cat1._last_consistency_mtime
    assert first_mtime > 0

    cat2 = _make_catalog(seeded_catalog_dir)
    assert cat2._last_consistency_mtime == first_mtime, (
        "second construction must read the persisted marker, "
        "not reset to 0.0 and re-rebuild"
    )


def test_consistency_marker_missing_returns_zero(tmp_path: Path) -> None:
    """First-ever construction with no marker file reads 0.0."""
    cat = _make_catalog(tmp_path)
    # No documents.jsonl exists, so _ensure_consistent isn't called;
    # the in-memory marker reflects the read-from-disk value.
    assert cat._last_consistency_mtime == 0.0
    assert not (tmp_path / ".last_consistency_mtime").exists()


def test_consistency_marker_malformed_falls_back_to_zero(tmp_path: Path) -> None:
    """A corrupted marker file does not propagate as an exception."""
    (tmp_path / ".last_consistency_mtime").write_text("not-a-float")
    cat = _make_catalog(tmp_path)
    assert cat._last_consistency_mtime == 0.0


def test_consistency_marker_advances_after_external_write(seeded_catalog_dir: Path) -> None:
    """A canonical-file mtime advance forces a rebuild and updates the marker.

    Simulates the cross-process case: one writer commits while another
    process is starting up. The new construction should detect the
    advance and rebuild rather than serve a stale projection.
    """
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


def test_consistency_marker_path_lives_in_catalog_dir(seeded_catalog_dir: Path) -> None:
    """The marker is co-located with the catalog files (not in cwd, not in $HOME)."""
    cat = _make_catalog(seeded_catalog_dir)
    assert cat._consistency_marker_path == seeded_catalog_dir / ".last_consistency_mtime"
