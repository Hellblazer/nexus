# SPDX-License-Identifier: AGPL-3.0-or-later
"""RDR-104 Steps 3-4: incremental rebuild path + equivalence suite.

Step 3 (unit-level branch tests, top half): five-way dispatch in
``Catalog._ensure_consistent``:

1. Existing mtime fast path (current_mtime <= last_consistency_mtime) —
   unchanged.
2. Empty-delta fast path: marker valid, ``eof_offset == stored_offset``
   → advance only the ``last_consistency_mtime`` row inside a
   ``transaction()`` block.
3. Bootstrap / invalidated full rebuild: marker missing, header-hash
   drift, or window-size mismatch → DELETE + replay from offset zero +
   write all four marker rows.
4. Incremental path: marker valid, delta non-empty → ``replay_from(
   stored_offset, limit_offset=eof_offset_now)`` + ``apply_all(
   commit=False)`` + write all four marker rows.
5. Incremental corruption signal: bounded iterator yields zero events
   for a non-empty ``[stored_offset, eof_offset_now)`` range → escalate
   to full rebuild WITHOUT advancing the marker.

Step 4 (equivalence suite, bottom half): row-by-row projection equality
between full-rebuild and incremental paths across the scenarios in the
RDR Test Plan, plus crash atomicity, malformed-line warn-and-skip,
v0-verb double-apply idempotency, split-pair conditional idempotency
(_v0_document_aliased), collections round-trip, concurrent-appender
bounded-form, CatalogDB.commit-not-called invariant, and the known-cost
same-size rewrite documentation pin.
"""
from __future__ import annotations

import os
import time
from pathlib import Path
from unittest.mock import patch

import pytest

from nexus.catalog.catalog import (
    _HEADER_HASH_BYTES,
    Catalog,
    _compute_header_hash,
)
from nexus.catalog.catalog_db import CatalogDB
from nexus.catalog.projector import Projector


def _make_catalog(catalog_dir: Path, db_name: str = "catalog.db") -> Catalog:
    return Catalog(catalog_dir, catalog_dir / db_name)


def _seed_es_catalog(catalog_dir: Path) -> None:
    cat = _make_catalog(catalog_dir)
    owner = cat.register_owner(
        name="seed-owner", owner_type="repo", repo_hash="seed-hash",
    )
    cat.register(
        owner=owner, title="seed-doc", content_type="prose", file_path="seed.md",
    )
    cat.register_collection(
        "code__1-1__voyage-code-3__v1",
        content_type="code",
        owner_id="1-1",
        embedding_model="voyage-code-3",
        model_version="v1",
    )
    cat._db.close()


@pytest.fixture
def seeded_es_catalog(tmp_path: Path) -> Path:
    """Catalog dir with one owner + one document + one collection.

    Triggers the event-sourced rebuild path on construction (events.jsonl
    is non-empty and covers the legacy state). Closes the fixture's DB
    before yielding so the test re-opens cleanly.
    """
    _seed_es_catalog(tmp_path)
    return tmp_path


@pytest.fixture
def seeded_es_catalog_small_window(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> Path:
    """seeded_es_catalog with ``_HEADER_HASH_BYTES`` patched to 32 bytes first.

    A small test fixture's events.jsonl is well under the default 64 KB
    window, so the entire file is hashed. Appending events therefore
    changes the hashed prefix and the orchestrator falls through to
    full rebuild on every append — hiding the incremental code path
    behind the full-rebuild path. Shrinking the window to 32 bytes
    makes the first event line's start the stable prefix; appends
    past 32 bytes do not move the hashed prefix and the incremental
    path actually fires.

    The patch is applied BEFORE seeding so the marker rows persist
    with window=32; subsequent constructions inside the test see the
    same patched constant.
    """
    monkeypatch.setattr("nexus.catalog.catalog._HEADER_HASH_BYTES", 32)
    _seed_es_catalog(tmp_path)
    return tmp_path


def _read_meta(db_path: Path, key: str) -> str | None:
    db = CatalogDB(db_path)
    try:
        row = db.execute(
            "SELECT value FROM _meta WHERE key = ?", (key,),
        ).fetchone()
        return row[0] if row else None
    finally:
        db.close()


# ── Branch 3 (bootstrap / first construction) ────────────────────────────


def test_first_construction_writes_offset_marker(seeded_es_catalog: Path) -> None:
    """Bootstrap: no offset marker → full rebuild writes all four rows.

    The seeded fixture's catalog already ran ``_ensure_consistent`` once
    and closed; its construction path is the bootstrap case. After re-
    opening with a fresh SQLite cache, the offset marker rows must be
    present so subsequent constructions can take the empty-delta or
    incremental paths.
    """
    cat = _make_catalog(seeded_es_catalog, db_name="catalog-fresh.db")
    stored = cat._read_offset_marker()
    assert stored is not None, (
        "first construction with non-empty events.jsonl must write the "
        "three offset-marker rows so subsequent runs can take the "
        "empty-delta or incremental fast paths"
    )
    offset, header_hash, window = stored
    eof = (seeded_es_catalog / "events.jsonl").stat().st_size
    assert offset == eof
    assert window == _HEADER_HASH_BYTES
    assert header_hash == _compute_header_hash(seeded_es_catalog / "events.jsonl")


# ── Branch 2 (empty-delta fast path) ──────────────────────────────────────


def test_empty_delta_fast_path_advances_only_mtime(
    seeded_es_catalog: Path,
) -> None:
    """events.jsonl unchanged but mtime-tick on a sibling file → mtime advance.

    The orchestrator must NOT re-replay events in this case. The marker
    offset trio stays at the prior value; only ``last_consistency_mtime``
    advances. Round 2 Significant #4: the transaction wrapper is still
    mandatory (single-row write inside a ``transaction()`` block).
    """
    cat1 = _make_catalog(seeded_es_catalog)
    initial = cat1._read_offset_marker()
    assert initial is not None
    initial_mtime = cat1._last_consistency_mtime
    cat1._db.close()

    # Tick documents.jsonl (a legacy sibling), not events.jsonl.
    docs = seeded_es_catalog / "documents.jsonl"
    future = initial_mtime + 100
    os.utime(docs, (future, future))

    cat2 = _make_catalog(seeded_es_catalog)
    after = cat2._read_offset_marker()

    assert after == initial, (
        "empty-delta fast path must NOT change the offset/hash/window "
        "rows — only mtime advances"
    )
    assert cat2._last_consistency_mtime >= future


def test_empty_delta_fast_path_does_not_read_events_file(
    seeded_es_catalog: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No header_hash computation, no events read on the empty-delta path.

    Asserts via patching: ``_compute_header_hash`` MUST NOT be called
    when ``stored_offset == eof_offset_now``. The empty-delta branch is
    pure metadata: a single _meta row write inside transaction().
    """
    cat1 = _make_catalog(seeded_es_catalog)
    initial_mtime = cat1._last_consistency_mtime
    cat1._db.close()

    docs = seeded_es_catalog / "documents.jsonl"
    future = initial_mtime + 100
    os.utime(docs, (future, future))

    call_count = {"n": 0}

    def patched(path: Path) -> str:
        call_count["n"] += 1
        return "x" * 64

    monkeypatch.setattr(
        "nexus.catalog.catalog._compute_header_hash", patched,
    )

    _make_catalog(seeded_es_catalog)
    assert call_count["n"] == 0, (
        "empty-delta path must not compute header_hash; computing it "
        "would defeat the fast-path latency target (sub-100ms)"
    )


# ── Branch 4 (incremental delta) ─────────────────────────────────────────


def test_incremental_path_applies_only_delta_events(
    seeded_es_catalog_small_window: Path,
) -> None:
    """Append a new doc → next construction replays only the delta.

    Setup: open catalog, append a doc (writes events.jsonl + documents.jsonl).
    Re-open: events.jsonl tail has new bytes; the orchestrator should
    take the incremental path because the first 32 bytes of events.jsonl
    are stable across the append (small_window fixture shrinks the
    hash window so the prefix doesn't drift).

    Observable: post-construction marker offset == new EOF; the new
    document is in the projection; the hash + window rows are
    unchanged because the stable-prefix bytes are unchanged.
    """
    catalog_dir = seeded_es_catalog_small_window
    cat1 = _make_catalog(catalog_dir)
    pre = cat1._read_offset_marker()
    assert pre is not None
    pre_offset, pre_hash, pre_window = pre
    owner = cat1.register_owner(
        name="seed-owner",  # idempotent
        owner_type="repo",
        repo_hash="seed-hash",
    )
    cat1.register(
        owner=owner, title="new-doc", content_type="prose",
        file_path="new.md",
    )
    cat1._db.close()

    cat2 = _make_catalog(catalog_dir)
    post = cat2._read_offset_marker()
    assert post is not None
    post_offset, post_hash, post_window = post
    eof = (catalog_dir / "events.jsonl").stat().st_size
    assert post_offset == eof
    assert post_offset > pre_offset
    # Header hash and window unchanged: events.jsonl prefix didn't move.
    assert post_hash == pre_hash
    assert post_window == pre_window
    # The new doc is in the projection.
    docs = cat2._db.execute(
        "SELECT title FROM documents ORDER BY title",
    ).fetchall()
    titles = [r[0] for r in docs]
    assert "new-doc" in titles
    assert "seed-doc" in titles


def test_incremental_path_passes_commit_false_to_apply_all(
    seeded_es_catalog_small_window: Path,
) -> None:
    """Round 3 Significant #3: apply_all called with commit=False.

    The Projector default is commit=True which would call
    ``self._db.commit()`` mid-transaction, finalizing the projection
    writes BEFORE the marker writes — defeating the rollback fence and
    re-introducing the 4.24.4-fixed ordering hazard.

    Asserts via patching ``Projector.apply_all`` and inspecting the
    keyword passed on the construction that exercises the incremental
    path (the second construction with new events appended).
    """
    catalog_dir = seeded_es_catalog_small_window
    cat1 = _make_catalog(catalog_dir)
    owner = cat1.register_owner(
        name="seed-owner", owner_type="repo", repo_hash="seed-hash",
    )
    cat1.register(
        owner=owner, title="delta-doc", content_type="prose",
        file_path="delta.md",
    )
    cat1._db.close()

    captured_commit_kwargs: list[bool] = []
    real_apply_all = Projector.apply_all

    def spy_apply_all(self, events, *, commit: bool = True) -> int:
        captured_commit_kwargs.append(commit)
        return real_apply_all(self, events, commit=commit)

    with patch.object(Projector, "apply_all", spy_apply_all):
        _make_catalog(catalog_dir)

    assert captured_commit_kwargs, (
        "second construction must invoke apply_all on the incremental path"
    )
    assert all(kw is False for kw in captured_commit_kwargs), (
        f"every apply_all call inside _ensure_consistent must pass "
        f"commit=False; got {captured_commit_kwargs}. A nested commit "
        f"defeats the rollback fence."
    )


# ── Branch 3 (invalidated paths) ─────────────────────────────────────────


def test_header_hash_drift_triggers_full_rebuild(
    seeded_es_catalog: Path,
) -> None:
    """Stored hash differs from current → full rebuild.

    Simulates an operator script that truncated and replaced the event
    log such that the file was rewritten before the next construction.
    To exercise the hash-drift path explicitly (rather than empty-delta
    or window-mismatch), the test plants a deliberately wrong stored
    hash with offset=0 (so the empty-delta fast path doesn't fire).

    Orchestrator falls through to full rebuild and writes a fresh
    marker matching the current file's content.
    """
    cat1 = _make_catalog(seeded_es_catalog)
    cat1._db.close()

    events = seeded_es_catalog / "events.jsonl"
    db = CatalogDB(seeded_es_catalog / "catalog.db")
    try:
        with db.transaction():
            db.execute(
                "INSERT OR REPLACE INTO _meta (key, value) VALUES (?, ?)",
                ("last_applied_event_offset", "0"),
            )
            db.execute(
                "INSERT OR REPLACE INTO _meta (key, value) VALUES (?, ?)",
                ("last_applied_event_header_hash", "0" * 64),
            )
            db.execute(
                "INSERT OR REPLACE INTO _meta (key, value) VALUES (?, ?)",
                ("last_consistency_mtime", "0"),
            )
    finally:
        db.close()
    future = time.time() + 100
    os.utime(events, (future, future))

    cat2 = _make_catalog(seeded_es_catalog)
    new = cat2._read_offset_marker()
    assert new is not None
    new_offset, new_hash, new_window = new
    assert new_hash == _compute_header_hash(events), (
        "full rebuild after header-hash drift must persist the NEW "
        "hash so the next run's empty-delta check works against the "
        "post-rewrite content"
    )
    assert new_hash != "0" * 64, "stored corrupt hash must be replaced"
    assert new_offset == events.stat().st_size


def test_window_size_mismatch_triggers_full_rebuild(
    seeded_es_catalog: Path,
) -> None:
    """Stored window != current ``_HEADER_HASH_BYTES`` → full rebuild.

    Round 1 gate observation #3: a future bump of ``_HEADER_HASH_BYTES``
    invalidates prior markers via the window-size mismatch rather than
    silently comparing hashes computed over different windows.

    Plants a non-empty delta (offset=0) so the empty-delta fast path
    doesn't short-circuit before the window check fires.
    """
    cat1 = _make_catalog(seeded_es_catalog)
    cat1._db.close()

    db = CatalogDB(seeded_es_catalog / "catalog.db")
    try:
        with db.transaction():
            db.execute(
                "INSERT OR REPLACE INTO _meta (key, value) VALUES (?, ?)",
                ("last_applied_event_offset", "0"),
            )
            db.execute(
                "INSERT OR REPLACE INTO _meta (key, value) VALUES (?, ?)",
                ("last_applied_event_header_window", str(_HEADER_HASH_BYTES + 1)),
            )
            db.execute(
                "INSERT OR REPLACE INTO _meta (key, value) VALUES (?, ?)",
                ("last_consistency_mtime", "0"),
            )
    finally:
        db.close()
    events = seeded_es_catalog / "events.jsonl"
    future = time.time() + 100
    os.utime(events, (future, future))

    cat2 = _make_catalog(seeded_es_catalog)
    after = cat2._read_offset_marker()
    assert after is not None
    _, _, after_window = after
    assert after_window == _HEADER_HASH_BYTES, (
        "full rebuild on window-size mismatch must persist the current "
        "constant value so future fast-path checks use the matching window"
    )


# ── Branch 5 (incremental corruption signal) ─────────────────────────────


def test_zero_events_from_non_empty_delta_escalates_to_full_rebuild(
    seeded_es_catalog: Path,
) -> None:
    """Bounded iterator yields zero events from non-empty range → full rebuild.

    Round 3 Significant #2: the only observable corruption signal is
    "zero events from a non-empty [stored_offset, eof_offset_now) range".
    This can occur when the marker offset lands mid-line and the first-
    line JSON parse fails (warn-and-skip leaves the iterator empty for
    the range), or in the vanishingly improbable hash-collision case
    where the prefix matches but the tail is unrelated.

    Setup: a well-formed catalog with events.jsonl. Manually corrupt
    the offset marker so it points mid-line. Bump mtime. Reconstruct
    catalog → orchestrator detects zero events from non-empty range
    and escalates to full rebuild (writes a fresh, valid marker).
    """
    cat1 = _make_catalog(seeded_es_catalog)
    valid = cat1._read_offset_marker()
    assert valid is not None
    valid_offset, valid_hash, valid_window = valid
    cat1._db.close()

    events = seeded_es_catalog / "events.jsonl"
    eof = events.stat().st_size

    # Plant a mid-line offset (somewhere between the start of the last
    # line and EOF). Pick a position guaranteed to be mid-line: take
    # second-to-last line start + 5.
    raw = events.read_bytes()
    line_starts = [0]
    for i, b in enumerate(raw):
        if b == 0x0A and i + 1 < len(raw):
            line_starts.append(i + 1)
    assert len(line_starts) >= 2, "fixture must have at least 2 lines"
    mid_line_offset = line_starts[-1] + 5
    assert mid_line_offset < eof

    db = CatalogDB(seeded_es_catalog / "catalog.db")
    try:
        with db.transaction():
            db.execute(
                "INSERT OR REPLACE INTO _meta (key, value) VALUES (?, ?)",
                ("last_applied_event_offset", str(mid_line_offset)),
            )
            db.execute(
                "INSERT OR REPLACE INTO _meta (key, value) VALUES (?, ?)",
                ("last_consistency_mtime", "0"),
            )
    finally:
        db.close()
    # Force rebuild path entry.
    future = time.time() + 100
    os.utime(events, (future, future))

    cat2 = _make_catalog(seeded_es_catalog)
    after = cat2._read_offset_marker()
    assert after is not None
    after_offset, after_hash, after_window = after
    # Full-rebuild path persists the current EOF + matching window/hash.
    assert after_offset == events.stat().st_size, (
        "after the corruption signal, the full-rebuild path persists "
        "the current EOF as the new marker — not the stale mid-line offset"
    )
    assert after_window == _HEADER_HASH_BYTES
    # Projection state must be consistent (full rebuild ran).
    assert cat2.degraded is False


# ── Step 4: equivalence suite ────────────────────────────────────────────

# Helpers — projection equality + scenario fixtures.


# Columns whose values are wall-clock-derived and therefore differ
# between two independent runs that register the same logical events.
# Two paths with the same logical event log will produce different
# timestamps; the projection equality test masks them out.
_VOLATILE_COLUMNS: dict[str, frozenset[str]] = {
    "owners": frozenset(),
    "documents": frozenset({"indexed_at", "indexed_at_doc"}),
    "links": frozenset({"created_at"}),
    "collections": frozenset({"created_at", "superseded_at"}),
}


def _projection_snapshot(cat: Catalog) -> dict[str, list[tuple]]:
    """Return a dict of {table -> sorted rows} with volatile columns masked.

    Row ordering is sorted on a per-table basis so two catalogs with
    the same content but different physical insertion order compare
    equal. Wall-clock-derived columns (timestamps) are zeroed out so
    independent runs with the same logical event log compare equal at
    the projection layer.
    """
    tables = ("owners", "documents", "links", "collections")
    snap: dict[str, list[tuple]] = {}
    for t in tables:
        cur = cat._db.execute(f"SELECT * FROM {t}")
        col_names = [d[0] for d in cur.description]
        volatile = _VOLATILE_COLUMNS[t]
        masked: list[tuple] = []
        for row in cur.fetchall():
            entries: list[object] = []
            for name, value in zip(col_names, row):
                entries.append(None if name in volatile else value)
            masked.append(tuple(entries))
        snap[t] = sorted(masked)
    return snap


def _assert_projection_equal(
    cat_a: Catalog, cat_b: Catalog, *, where: str = "",
) -> None:
    snap_a = _projection_snapshot(cat_a)
    snap_b = _projection_snapshot(cat_b)
    where_label = f" ({where})" if where else ""
    for table in snap_a:
        assert snap_a[table] == snap_b[table], (
            f"projection table {table!r} differs between paths"
            f"{where_label}\n"
            f"  A ({len(snap_a[table])} rows): {snap_a[table][:3]}...\n"
            f"  B ({len(snap_b[table])} rows): {snap_b[table][:3]}..."
        )


def _seed_with_diverse_events(
    catalog_dir: Path, count: int = 10,
) -> Catalog:
    """Seed a catalog with a mix of v0 verbs.

    Returns the still-open Catalog so the caller can inspect state
    before closing. Mixes register_owner / register / register_collection
    / update so events.jsonl carries OwnerRegistered, DocumentRegistered,
    DocumentRenamed (via update title), CollectionCreated.
    """
    cat = _make_catalog(catalog_dir)
    owner = cat.register_owner(
        name="seed-owner", owner_type="repo", repo_hash="seed-hash",
    )
    cat.register_collection(
        "code__1-1__voyage-code-3__v1",
        content_type="code",
        owner_id="1-1",
        embedding_model="voyage-code-3",
        model_version="v1",
    )
    for i in range(count):
        cat.register(
            owner=owner,
            title=f"doc-{i:03d}",
            content_type="prose",
            file_path=f"doc-{i:03d}.md",
        )
    return cat


# Scenario: equivalence between full-rebuild and incremental.


def test_full_rebuild_and_incremental_produce_equal_projection(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Two paths over the same event log produce identical projections.

    Sets up two catalog directories. Catalog A: register N events,
    construction triggers a full rebuild on close-and-reopen.
    Catalog B: register the same N events but reopen after EACH event
    so the incremental path fires for every-after-the-first event.
    Asserts B's final projection equals A's.

    Round 1 Critical Assumption: every v0 verb is idempotent under
    accidental double-apply. Without that property the equivalence
    here would not hold.

    Uses the small-window patch so events.jsonl < 64 KB still triggers
    the incremental path on each append (otherwise hash drift forces
    full rebuild on every append).
    """
    monkeypatch.setattr("nexus.catalog.catalog._HEADER_HASH_BYTES", 32)

    a_dir = tmp_path / "a"
    b_dir = tmp_path / "b"
    a_dir.mkdir()
    b_dir.mkdir()

    # Catalog A: register events, then close+reopen so a single rebuild
    # absorbs the full log.
    cat_a = _seed_with_diverse_events(a_dir, count=8)
    cat_a._db.close()
    cat_a = _make_catalog(a_dir)

    # Catalog B: register events under a fresh dir, but reopen after
    # every register so the incremental path fires on each subsequent
    # construction.
    cat_b = _make_catalog(b_dir)
    owner_b = cat_b.register_owner(
        name="seed-owner", owner_type="repo", repo_hash="seed-hash",
    )
    cat_b.register_collection(
        "code__1-1__voyage-code-3__v1",
        content_type="code",
        owner_id="1-1",
        embedding_model="voyage-code-3",
        model_version="v1",
    )
    cat_b._db.close()
    for i in range(8):
        cat_b = _make_catalog(b_dir)
        cat_b.register(
            owner=owner_b,
            title=f"doc-{i:03d}",
            content_type="prose",
            file_path=f"doc-{i:03d}.md",
        )
        cat_b._db.close()
    cat_b = _make_catalog(b_dir)

    _assert_projection_equal(
        cat_a, cat_b,
        where="full rebuild vs. one-event-at-a-time incremental",
    )


# Scenario: appended events apply incrementally, projection matches a
# full rebuild over the combined log.


def test_appended_events_match_full_rebuild_on_combined_log(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Append K events then take incremental path → equals full rebuild on combined log."""
    monkeypatch.setattr("nexus.catalog.catalog._HEADER_HASH_BYTES", 32)

    incr_dir = tmp_path / "incr"
    full_dir = tmp_path / "full"
    incr_dir.mkdir()
    full_dir.mkdir()

    # Phase 1: seed both with the same initial 5 events.
    for d in (incr_dir, full_dir):
        cat = _seed_with_diverse_events(d, count=5)
        cat._db.close()

    # Phase 2: append 5 more events to each.
    for d in (incr_dir, full_dir):
        cat = _make_catalog(d)
        owner = cat.register_owner(
            name="seed-owner", owner_type="repo", repo_hash="seed-hash",
        )
        for i in range(5, 10):
            cat.register(
                owner=owner,
                title=f"doc-{i:03d}",
                content_type="prose",
                file_path=f"doc-{i:03d}.md",
            )
        cat._db.close()

    # Phase 3: incremental dir reopens; full_dir wipes its SQLite cache
    # and forces a fresh full rebuild from offset 0.
    (full_dir / "catalog.db").unlink()  # forces bootstrap on next open
    (full_dir / "catalog.db-shm").unlink(missing_ok=True)
    (full_dir / "catalog.db-wal").unlink(missing_ok=True)

    cat_incr = _make_catalog(incr_dir)
    cat_full = _make_catalog(full_dir)

    _assert_projection_equal(
        cat_incr, cat_full,
        where="incremental over appended events vs. fresh full rebuild",
    )


# Scenario: crash mid-incremental rolls back the marker.


def test_crash_mid_incremental_rolls_back_offset_marker(
    seeded_es_catalog_small_window: Path,
) -> None:
    """Patched apply_all raises on incremental → marker NOT advanced.

    Round 3 / 4.24.4 atomicity contract on the incremental path. The
    transaction context rolls back the projector writes AND all four
    marker rows together; the next run reads the un-advanced marker
    and re-attempts the same delta.
    """
    catalog_dir = seeded_es_catalog_small_window
    cat1 = _make_catalog(catalog_dir)
    pre_marker = cat1._read_offset_marker()
    assert pre_marker is not None
    pre_offset, pre_hash, pre_window = pre_marker
    pre_mtime = cat1._last_consistency_mtime

    # Append an event so the next construction has a non-empty delta.
    owner = cat1.register_owner(
        name="seed-owner", owner_type="repo", repo_hash="seed-hash",
    )
    cat1.register(
        owner=owner, title="will-not-land", content_type="prose",
        file_path="boom.md",
    )
    cat1._db.close()

    def projector_apply_all_boom(self, *a, **kw):
        raise RuntimeError("simulated mid-incremental crash")

    with patch.object(Projector, "apply_all", projector_apply_all_boom):
        cat2 = _make_catalog(catalog_dir)

    assert cat2.degraded is True
    after_marker = cat2._read_offset_marker()
    assert after_marker is not None
    after_offset, after_hash, after_window = after_marker
    assert after_offset == pre_offset, (
        "incremental rebuild raised mid-transaction — offset marker "
        "MUST roll back to the pre-attempt value (4.24.4 atomicity)"
    )
    assert after_hash == pre_hash
    assert after_window == pre_window


# Scenario: malformed-line append → warn-and-skip preserves invariant.


def test_malformed_line_appended_skipped_in_incremental_path(
    seeded_es_catalog_small_window: Path,
) -> None:
    """Append a corrupt JSON line then a valid event → incremental skips bad line.

    The bad line is processed by replay_from's warn-and-skip; the
    valid event applies; the marker advances past both. Projection
    state matches what would result if the bad line did not exist.
    """
    catalog_dir = seeded_es_catalog_small_window
    cat1 = _make_catalog(catalog_dir)
    pre_doc_count = cat1._db.execute(
        "SELECT COUNT(*) FROM documents",
    ).fetchone()[0]
    cat1._db.close()

    # Append a malformed line directly, then a valid event via the API.
    events_path = catalog_dir / "events.jsonl"
    with events_path.open("a") as f:
        f.write("{not json garbage line}\n")
    cat_writer = _make_catalog(catalog_dir)
    owner = cat_writer.register_owner(
        name="seed-owner", owner_type="repo", repo_hash="seed-hash",
    )
    cat_writer.register(
        owner=owner, title="post-bad-line", content_type="prose",
        file_path="post.md",
    )
    cat_writer._db.close()

    # Re-open: incremental fires (cat_writer's last construction
    # already absorbed the bad line on its incremental path; we run
    # one more construction to confirm the marker is at EOF).
    cat2 = _make_catalog(catalog_dir)
    eof = events_path.stat().st_size
    marker = cat2._read_offset_marker()
    assert marker is not None
    assert marker[0] == eof, (
        "marker must advance past both the bad line and the valid event"
    )
    titles = [
        r[0] for r in cat2._db.execute(
            "SELECT title FROM documents",
        ).fetchall()
    ]
    assert "post-bad-line" in titles
    assert cat2.degraded is False


# Scenario: idempotency of v0 verbs under double-apply.


def test_double_apply_idempotency_owner_registered(
    seeded_es_catalog_small_window: Path,
) -> None:
    """Re-registering the same owner is idempotent at the projection level.

    Critical Assumption 1: every v0 projector verb is idempotent under
    accidental double-apply. This pins the property for OwnerRegistered
    via the public API. Other verbs are exercised through the
    full-rebuild-vs-incremental equivalence test above (which double-
    applies every event in the seeded log when re-running the
    full-rebuild path).
    """
    catalog_dir = seeded_es_catalog_small_window
    cat = _make_catalog(catalog_dir)
    pre_owner_count = cat._db.execute(
        "SELECT COUNT(*) FROM owners",
    ).fetchone()[0]
    # Register the same owner three more times.
    for _ in range(3):
        cat.register_owner(
            name="seed-owner", owner_type="repo", repo_hash="seed-hash",
        )
    post_owner_count = cat._db.execute(
        "SELECT COUNT(*) FROM owners",
    ).fetchone()[0]
    assert post_owner_count == pre_owner_count, (
        "OwnerRegistered must be idempotent at the projection layer "
        "even when re-emitted through the public API"
    )


# Scenario: collections round-trip via the incremental path.


def test_collections_round_trip_via_incremental_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Register A, register B, supersede A→B → incremental projection has
    A.superseded_by == B and B.superseded_by == ''.

    Forces incremental path on each step via small-window patch.
    Complements the full-rebuild collections test in
    tests/test_catalog_collections_rebuild.py.
    """
    monkeypatch.setattr("nexus.catalog.catalog._HEADER_HASH_BYTES", 32)

    cat = _make_catalog(tmp_path)
    cat.register_owner(
        name="seed-owner", owner_type="repo", repo_hash="seed-hash",
    )
    old_name = "docs__nexus-571b8edd"
    new_name = "docs__1-1__voyage-context-3__v1"
    cat.register_collection(old_name)
    cat._db.close()

    cat = _make_catalog(tmp_path)
    cat.register_collection(
        new_name,
        content_type="docs",
        owner_id="1-1",
        embedding_model="voyage-context-3",
        model_version="v1",
    )
    cat._db.close()

    cat = _make_catalog(tmp_path)
    cat.supersede_collection(old_name, new_name, reason="rename")
    cat._db.close()

    cat = _make_catalog(tmp_path)
    old = cat.get_collection(old_name)
    new = cat.get_collection(new_name)
    assert old is not None
    assert new is not None
    assert old["superseded_by"] == new_name
    assert old["superseded_at"]
    assert new["superseded_by"] == ""


# Scenario: concurrent-appender bounded form (Round 2 Critical #1).


def test_concurrent_appender_bounded_form_caps_marker_at_snapshot(
    seeded_es_catalog_small_window: Path,
) -> None:
    """Orchestrator-level simulation of concurrent-appender race.

    The orchestrator captures ``eof_offset_now`` from a stat() snapshot
    and passes it as ``limit_offset`` to ``replay_from``. If a writer
    extends the file between the stat and the iterator's read window,
    the bounded form must NOT consume the appended tail — otherwise
    the marker the orchestrator persists drifts below the true tail
    and incremental never settles (RDR-104 Round 2 Critical #1).

    Stage the race deterministically: patch ``EventLog.replay_from``
    so its first call lands additional bytes in events.jsonl BEFORE
    delegating to the real implementation. The orchestrator's
    ``limit_offset`` argument was captured BEFORE the patch's append
    fires (`replay_from` is called once per rebuild), so the
    delegated-to real iterator sees the bounded limit.
    """
    from nexus.catalog.event_log import EventLog

    catalog_dir = seeded_es_catalog_small_window
    cat0 = _make_catalog(catalog_dir)
    owner = cat0.register_owner(
        name="seed-owner", owner_type="repo", repo_hash="seed-hash",
    )
    cat0.register(
        owner=owner, title="pre-race-doc", content_type="prose",
        file_path="pre-race.md",
    )
    cat0._db.close()

    events_path = catalog_dir / "events.jsonl"

    captured_limit_offset: list[int | None] = []
    real_replay_from = EventLog.replay_from
    appender_done = {"flag": False}

    def staged_replay_from(self, offset, *, limit_offset=None):
        captured_limit_offset.append(limit_offset)
        if not appender_done["flag"]:
            appender_done["flag"] = True
            # The orchestrator already captured eof_offset_now and
            # passed it here as limit_offset. NOW we land the race
            # append: the file grows past limit_offset, but the real
            # replay_from below caps the iterator at limit_offset
            # regardless of where live EOF moves.
            #
            # Use DocumentEnriched (a no-op for v0) so the race
            # events don't perturb the bootstrap guardrail's
            # DocumentRegistered/Deleted balance check.
            with events_path.open("a") as f:
                for i in range(2):
                    f.write(
                        '{"type":"DocumentEnriched","v":0,'
                        f'"payload":{{"doc_id":"race-{i}",'
                        '"schema_version":"bib-s2-v1","payload":{}},'
                        '"ts":"t"}\n'
                    )
        return real_replay_from(self, offset, limit_offset=limit_offset)

    # Force a non-empty delta by clearing the marker's offset so the
    # orchestrator takes the incremental branch (replay_from is called).
    db = CatalogDB(catalog_dir / "catalog.db")
    try:
        with db.transaction():
            db.execute(  # epsilon-allow: reset offset marker so the next construction takes a non-empty delta and exercises the concurrent-appender race
                "INSERT OR REPLACE INTO _meta (key, value) VALUES (?, ?)",
                ("last_applied_event_offset", "0"),
            )
            db.execute(  # epsilon-allow: companion reset so empty-delta short-circuit doesn't fire
                "INSERT OR REPLACE INTO _meta (key, value) VALUES (?, ?)",
                ("last_consistency_mtime", "0"),
            )
    finally:
        db.close()

    pre_race_eof = events_path.stat().st_size

    with patch.object(EventLog, "replay_from", staged_replay_from):
        cat = _make_catalog(catalog_dir)

    assert captured_limit_offset, (
        "orchestrator must invoke replay_from for the race simulation"
    )
    snapshot_limit = captured_limit_offset[0]
    assert snapshot_limit == pre_race_eof, (
        f"orchestrator should have passed limit_offset={pre_race_eof} "
        f"(the captured snapshot), got {snapshot_limit}"
    )
    live_eof = events_path.stat().st_size
    assert live_eof > pre_race_eof, (
        "patched replay_from must have landed the race append"
    )

    after_marker = cat._read_offset_marker()
    assert after_marker is not None
    after_offset, _, _ = after_marker
    assert after_offset == pre_race_eof, (
        f"marker must equal the captured pre-race snapshot "
        f"({pre_race_eof}); got {after_offset}. Live EOF is "
        f"{live_eof}. The bounded replay_from must NOT consume the "
        f"appended-during-orchestrator tail; otherwise the marker "
        f"drifts below the true tail and incremental never settles."
    )

    # Next construction sees the L appended-during-race events as a
    # non-empty delta and applies them — proving incremental settles.
    cat_next = _make_catalog(catalog_dir)
    next_marker = cat_next._read_offset_marker()
    assert next_marker is not None
    assert next_marker[0] == live_eof, (
        "next construction's marker must advance to live EOF after "
        "absorbing the race-tail events"
    )


# Scenario: CatalogDB.commit not called explicitly inside the
# incremental rebuild's transaction body (Round 3 Significant #3).


def test_catalog_db_commit_not_called_explicitly_in_incremental_path(
    seeded_es_catalog_small_window: Path,
) -> None:
    """Patches CatalogDB.commit and asserts zero explicit calls during
    the incremental rebuild's body. The connection-level commit at
    ``with self._conn:`` __exit__ is the implicit one; the spy here
    catches an explicit ``self._db.commit()`` that would defeat the
    rollback fence. Companion test to the apply_all(commit=False)
    contract at the unit-level (above).
    """
    catalog_dir = seeded_es_catalog_small_window
    cat1 = _make_catalog(catalog_dir)
    owner = cat1.register_owner(
        name="seed-owner", owner_type="repo", repo_hash="seed-hash",
    )
    cat1.register(
        owner=owner, title="commit-spy-doc", content_type="prose",
        file_path="spy.md",
    )
    cat1._db.close()

    explicit_commit_calls = {"n": 0}
    real_commit = CatalogDB.commit

    def spy_commit(self):
        explicit_commit_calls["n"] += 1
        return real_commit(self)

    with patch.object(CatalogDB, "commit", spy_commit):
        _make_catalog(catalog_dir)

    assert explicit_commit_calls["n"] == 0, (
        f"CatalogDB.commit() must NOT be called explicitly inside the "
        f"incremental rebuild's transaction body; the connection-level "
        f"commit at the with-statement __exit__ is the only commit. "
        f"Got {explicit_commit_calls['n']} explicit calls — defeats "
        f"the rollback fence."
    )


# Scenario: pathological same-size rewrite is documented known cost.


def test_pathological_same_size_rewrite_is_known_cost_not_corruption(
    seeded_es_catalog: Path,
) -> None:
    """events.jsonl rewritten with the same length AND the same first
    64 KB → empty-delta fast path fires (eof unchanged, hash window
    coincides); projection is unchanged; this is the documented
    pathological case bounded by Finding 1 idempotency.

    The RDR Test Plan explicitly notes: "pathological adversarial case
    bounded by Finding 1 idempotency; projection still converges to
    correct state via redundant replay; documented as known-cost-not-
    known-corruption." This test pins the documented behavior so a
    future change that promotes the same-size-rewrite case to a
    correctness invariant has to also add detection logic.
    """
    cat1 = _make_catalog(seeded_es_catalog)
    cat1._db.close()

    events_path = seeded_es_catalog / "events.jsonl"
    original = events_path.read_bytes()
    # Same first 64KB by construction (the seeded fixture is < 64KB).
    # Rewrite to identical bytes; mtime ticks but the file content is
    # byte-for-byte the same. This is the documented "no-op" rewrite —
    # the orchestrator takes the empty-delta path. We DO NOT assert
    # the orchestrator detects the rewrite (it explicitly does not for
    # this case, per the RDR).
    events_path.write_bytes(original)
    future = time.time() + 100
    os.utime(events_path, (future, future))

    cat2 = _make_catalog(seeded_es_catalog)
    # Marker advances to current mtime (empty-delta path); the
    # projection is correct because the file content is identical.
    assert cat2.degraded is False
    docs = cat2._db.execute("SELECT COUNT(*) FROM documents").fetchone()[0]
    assert docs >= 1, "seeded doc must remain in projection after rewrite"


# Scenario: split-pair conditional idempotency for _v0_document_aliased.


def test_split_pair_document_aliased_after_marker_matches_full_rebuild(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """DocumentRegistered before marker, DocumentAliased after marker.

    Round 1 Significant: ``_v0_document_aliased`` requires the prior
    DocumentRegistered to have populated the row; replaying the
    DocumentAliased alone (without the DocumentRegistered) would be a
    no-op against an empty row. Test setup:

    1. Register a doc (DocumentRegistered event lands).
    2. Close + reopen → marker advances past DocumentRegistered.
    3. update() the doc with an alias_of (DocumentAliased event lands
       in the new-events window).
    4. Close + reopen → incremental replays only DocumentAliased.

    Asserts the resulting projection equals a fresh full-rebuild over
    the same combined log.
    """
    monkeypatch.setattr("nexus.catalog.catalog._HEADER_HASH_BYTES", 32)

    incr_dir = tmp_path / "incr"
    full_dir = tmp_path / "full"
    incr_dir.mkdir()
    full_dir.mkdir()

    for d in (incr_dir, full_dir):
        cat = _make_catalog(d)
        owner = cat.register_owner(
            name="seed-owner", owner_type="repo", repo_hash="seed-hash",
        )
        target_t = cat.register(
            owner=owner, title="primary-doc", content_type="prose",
            file_path="primary.md",
        )
        alias_t = cat.register(
            owner=owner, title="alias-doc", content_type="prose",
            file_path="alias.md",
        )
        cat._db.close()
        # Reopen so the incremental path's marker advances past the
        # DocumentRegistered events for both.
        cat = _make_catalog(d)
        # Now alias the second document to the first via update.
        # Pass the target tumbler as a string — the Tumbler dataclass
        # is not natively bindable to SQLite parameters and would
        # crash the projector replay path on the un-stringified
        # alias_of payload field.
        cat.update(tumbler=alias_t, alias_of=str(target_t))
        cat._db.close()

    # Wipe full_dir's SQLite so it rebuilds from offset 0.
    (full_dir / "catalog.db").unlink()
    (full_dir / "catalog.db-shm").unlink(missing_ok=True)
    (full_dir / "catalog.db-wal").unlink(missing_ok=True)

    cat_incr = _make_catalog(incr_dir)
    cat_full = _make_catalog(full_dir)

    _assert_projection_equal(
        cat_incr, cat_full,
        where="split-pair: DocumentRegistered before marker, "
              "DocumentAliased after",
    )


# Scenario: performance — incremental on a 100-event delta against a
# 1K-event base completes in well under 1 second.


def test_incremental_path_meets_performance_budget(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """100-event delta against 1K-event base completes in <1s.

    Looser budget than the RDR's <100ms target on the 100-event /
    100K-event scenario because building a 100K-event fixture is too
    slow for the unit suite. The relative shape (incremental cost
    proportional to delta size, not total log size) is what the
    test pins; absolute regression detection happens at production
    smoke (ART catalog manual check, documented in PR body).
    """
    monkeypatch.setattr("nexus.catalog.catalog._HEADER_HASH_BYTES", 32)

    cat = _make_catalog(tmp_path)
    owner = cat.register_owner(
        name="seed-owner", owner_type="repo", repo_hash="seed-hash",
    )
    for i in range(1000):
        cat.register(
            owner=owner, title=f"base-{i:04d}", content_type="prose",
            file_path=f"base-{i:04d}.md",
        )
    cat._db.close()

    # Re-open so the marker is in steady state.
    cat = _make_catalog(tmp_path)
    for i in range(100):
        cat.register(
            owner=owner, title=f"delta-{i:03d}", content_type="prose",
            file_path=f"delta-{i:03d}.md",
        )
    cat._db.close()

    # Time the incremental path on the 100-event delta.
    start = time.monotonic()
    cat_final = _make_catalog(tmp_path)
    elapsed = time.monotonic() - start

    assert elapsed < 1.0, (
        f"incremental path on 100-event delta against 1K-event base "
        f"took {elapsed:.3f}s — should be sub-second. Production "
        f"target is <100ms on 100-event / 100K-event; the unit-suite "
        f"budget is looser to keep fixture build cost reasonable. "
        f"Investigate if this regresses."
    )
    # Sanity: the deltas applied.
    delta_count = cat_final._db.execute(
        "SELECT COUNT(*) FROM documents WHERE title LIKE 'delta-%'",
    ).fetchone()[0]
    assert delta_count == 100
