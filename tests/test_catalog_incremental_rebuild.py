# SPDX-License-Identifier: AGPL-3.0-or-later
"""RDR-104 Step 3: incremental rebuild branch in ``Catalog._ensure_consistent``.

Five branches:

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

Step 4 will add the equivalence-suite tests; this file covers the
unit-level branch behaviour and the ``apply_all(commit=False)``
contract.
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
