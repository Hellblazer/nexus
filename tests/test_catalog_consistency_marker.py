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
from unittest.mock import patch

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


def test_rebuild_emits_rich_summary_on_slow_rebuild(
    seeded_catalog_dir: Path,
    capsys: pytest.CaptureFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When a rebuild crosses the elapsed-time gate the summary line
    carries diagnostic detail (trigger file, replay/load counts,
    elapsed). Pre-fix the rebuild printed only ``done (Ns)``; users
    seeing a 3.4s rebuild had no signal of what was actually replayed.

    Patches ``time.monotonic`` inside ``nexus.catalog.catalog`` so the
    fast in-test rebuild fakes ~2 s of elapsed and crosses the
    1-second progress gate. Verifies the line contains the trigger
    file name, a count signal (docs/owners/links/events), and the
    elapsed.
    """
    # Force a rebuild — bump mtime past whatever the marker holds.
    import time
    for name in ("documents.jsonl", "owners.jsonl"):
        f = seeded_catalog_dir / name
        if f.exists():
            now = time.time()
            os.utime(f, (now, now))

    # Patch time.monotonic in the catalog module so elapsed reads ~2s
    # and crosses _PROGRESS_MIN_ELAPSED. Use a list-as-iterator so the
    # heartbeat thread (which also reads monotonic) doesn't blow up.
    real_monotonic = time.monotonic
    base = real_monotonic()
    call_count = {"n": 0}

    def fake_monotonic() -> float:
        call_count["n"] += 1
        # First call: sets started=base. Subsequent calls: return
        # base + 2.0s so elapsed reads as 2.0.
        return base if call_count["n"] == 1 else base + 2.0

    monkeypatch.setattr("nexus.catalog.catalog.time.monotonic", fake_monotonic)

    capsys.readouterr()  # drain anything from the seeded fixture
    _make_catalog(seeded_catalog_dir)

    err = capsys.readouterr().err
    assert "Catalog: rebuild triggered by" in err, (
        f"expected trigger label in stderr, got: {err!r}"
    )
    # Summary line includes a count signal — what makes the message
    # useful versus the prior bare "done (Ns)".
    assert any(token in err for token in (" docs,", " links", " events ")), (
        f"summary line missing the size signal: {err!r}"
    )
    # Elapsed reported.
    assert "2.0s" in err, (
        f"elapsed missing or wrong scale: {err!r}"
    )


def test_rebuild_silent_under_one_second(
    seeded_catalog_dir: Path, capsys: pytest.CaptureFixture,
) -> None:
    """Fast rebuilds (sub-second; the common case post-FTS5-fix) emit
    no summary line. CLI commands that incidentally trigger a rebuild
    don't scribble progress over their own output.

    The seeded fixture rebuild completes in milliseconds, well below
    the :data:`_PROGRESS_MIN_ELAPSED` gate.
    """
    capsys.readouterr()
    _make_catalog(seeded_catalog_dir)

    err = capsys.readouterr().err
    assert "Catalog: rebuild" not in err, (
        f"fast rebuild leaked progress line: {err!r}"
    )


def test_marker_does_not_advance_when_rebuild_raises(
    seeded_catalog_dir: Path,
) -> None:
    """RDR-104 critic Critical #2: marker write must be atomic with
    projection writes. Pre-fix the marker lived in its own ``commit()``
    after the rebuild transaction closed; a refactor that put the marker
    write before the projection commit would silently corrupt the
    projection by skipping events on the next run (rolled-back
    projection + advanced marker).

    Test simulates the dangerous direction: ``Projector.apply_all`` (or
    ``CatalogDB.rebuild``, depending on which path fires) raises
    mid-transaction. Asserts that the marker stored in ``_meta`` did
    NOT advance — same value as before the failed rebuild attempt.

    Patches BOTH the event-sourced and legacy mid-transaction sites so
    the invariant is pinned regardless of which path the seeded fixture
    happens to exercise (depends on NEXUS_EVENT_SOURCED gating). This
    is the load-bearing test for the atomicity fix.
    """
    cat1 = _make_catalog(seeded_catalog_dir)
    initial_marker = cat1._last_consistency_mtime
    assert initial_marker > 0
    cat1._db.close()

    # Force a rebuild — bump documents.jsonl mtime past the marker
    docs_path = seeded_catalog_dir / "documents.jsonl"
    future = initial_marker + 10
    os.utime(docs_path, (future, future))

    from nexus.catalog.catalog_db import CatalogDB
    from nexus.catalog.projector import Projector

    def db_rebuild_boom(self, *a, **kw):
        # Open transaction, write something, raise — simulates a
        # mid-rebuild crash on the legacy path. The transaction's
        # __exit__ rollback fires.
        with self._lock, self._conn:
            self._conn.execute("DELETE FROM documents")
            raise RuntimeError("simulated mid-rebuild crash (legacy)")

    def projector_apply_all_boom(self, *a, **kw):
        # The event-sourced path is wrapped in self._db.transaction()
        # by _ensure_consistent. Raising here triggers that block's
        # rollback before the marker write would have fired.
        raise RuntimeError("simulated mid-rebuild crash (event-sourced)")

    with patch.object(CatalogDB, "rebuild", db_rebuild_boom), \
         patch.object(Projector, "apply_all", projector_apply_all_boom):
        cat2 = _make_catalog(seeded_catalog_dir)

    # _ensure_consistent's outer try/except catches the raise and
    # sets degraded=True regardless of which path fired.
    assert cat2.degraded is True, (
        "rebuild raised — degraded must be set so callers know the "
        "projection is potentially stale"
    )

    # Marker must NOT have advanced. This is the invariant the
    # atomicity fix guarantees.
    row = cat2._db.execute(
        "SELECT value FROM _meta WHERE key = ?",
        ("last_consistency_mtime",),
    ).fetchone()
    stored_marker = float(row[0]) if row else 0.0
    assert stored_marker == initial_marker, (
        f"rebuild raised but marker advanced from {initial_marker} "
        f"to {stored_marker} — projection rollback would now be "
        f"silently masked by the advanced marker; events that the "
        f"rolled-back rebuild should have replayed will be skipped "
        f"on the next run"
    )


# ── RDR-104 Step 2: header-hash + offset marker ──────────────────────────


def test_compute_header_hash_small_file(tmp_path: Path) -> None:
    """A file smaller than the window hashes to the sha256 of its full bytes."""
    import hashlib
    from nexus.catalog.catalog import _compute_header_hash, _HEADER_HASH_BYTES

    p = tmp_path / "small.jsonl"
    payload = b'{"type":"X","v":1,"payload":{},"ts":"t"}\n' * 10
    assert len(payload) < _HEADER_HASH_BYTES
    p.write_bytes(payload)
    expected = hashlib.sha256(payload).hexdigest()
    assert _compute_header_hash(p) == expected


def test_compute_header_hash_large_file_uses_only_first_window(
    tmp_path: Path,
) -> None:
    """A file larger than the window hashes the first ``_HEADER_HASH_BYTES`` only."""
    import hashlib
    from nexus.catalog.catalog import _compute_header_hash, _HEADER_HASH_BYTES

    p = tmp_path / "big.jsonl"
    head = b"H" * _HEADER_HASH_BYTES
    tail = b"T" * 1024
    p.write_bytes(head + tail)
    expected = hashlib.sha256(head).hexdigest()
    assert _compute_header_hash(p) == expected
    # Sanity: tail mutation does NOT change the hash.
    p.write_bytes(head + b"DIFFERENT" * 100)
    assert _compute_header_hash(p) == expected


def test_compute_header_hash_constant_window_size() -> None:
    """RDR-104 Round 1 gate observation #3: the window is 64 KB."""
    from nexus.catalog.catalog import _HEADER_HASH_BYTES
    assert _HEADER_HASH_BYTES == 64 * 1024


def test_offset_marker_round_trips_through_meta(seeded_catalog_dir: Path) -> None:
    """Writing the three offset-marker rows then reading returns the same tuple."""
    cat = _make_catalog(seeded_catalog_dir)
    expected_offset = 12345
    expected_hash = "a" * 64
    expected_window = 64 * 1024

    with cat._db.transaction():
        cat._write_offset_marker(
            offset=expected_offset,
            header_hash=expected_hash,
            window=expected_window,
        )

    got = cat._read_offset_marker()
    assert got == (expected_offset, expected_hash, expected_window)


def test_offset_marker_read_returns_none_when_missing(
    seeded_catalog_dir: Path,
) -> None:
    """Bootstrap: no offset marker rows → reader returns None.

    Step 3's incremental-path branch consumes this signal to fall
    through to full rebuild.
    """
    cat = _make_catalog(seeded_catalog_dir)
    # Seeded fixture rebuilds on construction but the offset-marker rows
    # are written by Step 3, not Step 2 alone — so they must be absent
    # right now even after construction.
    assert cat._read_offset_marker() is None


@pytest.mark.parametrize(
    "missing_key",
    [
        "last_applied_event_offset",
        "last_applied_event_header_hash",
        "last_applied_event_header_window",
    ],
)
def test_offset_marker_read_returns_none_when_any_row_missing(
    seeded_catalog_dir: Path, missing_key: str,
) -> None:
    """Reader treats any missing row as 'no marker' so caller falls back."""
    cat = _make_catalog(seeded_catalog_dir)
    with cat._db.transaction():
        cat._write_offset_marker(
            offset=42, header_hash="b" * 64, window=64 * 1024,
        )
    assert cat._read_offset_marker() == (42, "b" * 64, 64 * 1024)

    with cat._db.transaction():
        cat._db.execute("DELETE FROM _meta WHERE key = ?", (missing_key,))

    assert cat._read_offset_marker() is None, (
        f"reader must return None when {missing_key} is absent so the "
        f"orchestrator falls through to full rebuild rather than acting "
        f"on a partial marker"
    )


def test_offset_marker_read_returns_none_on_unparseable_value(
    seeded_catalog_dir: Path,
) -> None:
    """Non-int offset / window string → reader returns None (defensive)."""
    cat = _make_catalog(seeded_catalog_dir)
    with cat._db.transaction():
        cat._db.execute(
            "INSERT OR REPLACE INTO _meta (key, value) VALUES (?, ?)",
            ("last_applied_event_offset", "not-an-int"),
        )
        cat._db.execute(
            "INSERT OR REPLACE INTO _meta (key, value) VALUES (?, ?)",
            ("last_applied_event_header_hash", "x" * 64),
        )
        cat._db.execute(
            "INSERT OR REPLACE INTO _meta (key, value) VALUES (?, ?)",
            ("last_applied_event_header_window", "65536"),
        )
    assert cat._read_offset_marker() is None


def test_offset_marker_atomic_rollback_on_transaction_raise(
    seeded_catalog_dir: Path,
) -> None:
    """A transaction that writes the offset marker then raises rolls back all rows.

    The 4.24.4 atomicity contract applies to every marker write. The
    Step 2 helper does not open its own transaction — it expects the
    caller to be inside one. This test confirms the rollback shape so
    Step 3's orchestrator can rely on it.
    """
    cat = _make_catalog(seeded_catalog_dir)
    # Seed a known-good marker first.
    with cat._db.transaction():
        cat._write_offset_marker(
            offset=100, header_hash="c" * 64, window=64 * 1024,
        )
    assert cat._read_offset_marker() == (100, "c" * 64, 64 * 1024)

    # Now write an updated marker but raise mid-transaction.
    with pytest.raises(RuntimeError, match="simulated"):
        with cat._db.transaction():
            cat._write_offset_marker(
                offset=999, header_hash="d" * 64, window=128 * 1024,
            )
            raise RuntimeError("simulated mid-tx crash")

    # All three rows must have rolled back to the prior values.
    assert cat._read_offset_marker() == (100, "c" * 64, 64 * 1024), (
        "transaction rollback must revert all three offset-marker rows "
        "atomically; partial-row state would let Step 3's incremental "
        "path act on inconsistent metadata"
    )


def test_offset_marker_atomic_with_consistency_marker(
    seeded_catalog_dir: Path,
) -> None:
    """All FOUR marker rows roll back together when the transaction raises.

    Step 3's incremental path writes the mtime marker AND the offset
    marker inside the same transaction. The atomicity contract means
    a mid-transaction raise reverts all four rows atomically.
    """
    cat = _make_catalog(seeded_catalog_dir)
    initial_mtime = cat._last_consistency_mtime
    with cat._db.transaction():
        cat._write_offset_marker(
            offset=50, header_hash="e" * 64, window=64 * 1024,
        )
    assert cat._read_offset_marker() == (50, "e" * 64, 64 * 1024)

    with pytest.raises(RuntimeError, match="simulated"):
        with cat._db.transaction():
            cat._write_consistency_marker(initial_mtime + 100)
            cat._write_offset_marker(
                offset=200, header_hash="f" * 64, window=64 * 1024,
            )
            raise RuntimeError("simulated mid-tx crash")

    # Mtime row must be at the prior value, offset marker likewise.
    row = cat._db.execute(
        "SELECT value FROM _meta WHERE key = ?",
        ("last_consistency_mtime",),
    ).fetchone()
    assert row is not None
    persisted_mtime = float(row[0])
    assert persisted_mtime == initial_mtime, (
        "mtime must roll back when its co-transaction raises"
    )
    assert cat._read_offset_marker() == (50, "e" * 64, 64 * 1024)
