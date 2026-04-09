# SPDX-License-Identifier: AGPL-3.0-or-later
"""Tests for memory consolidation (RDR-061 E6, nexus-lfbh)."""
from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from nexus.db.t2 import T2Database


# ── overlap detection ───────────────────────────────────────────────────────


def test_find_overlapping_two_similar_entries(db: T2Database) -> None:
    """Two entries about the same topic → overlap pair found."""
    db.put(project="proj", title="search-arch.md",
           content="search engine architecture design patterns optimization")
    db.put(project="proj", title="search-design.md",
           content="search engine architecture design patterns implementation")
    pairs = db.find_overlapping_memories("proj")
    assert len(pairs) >= 1
    titles = {pairs[0][0]["title"], pairs[0][1]["title"]}
    assert titles == {"search-arch.md", "search-design.md"}


def test_find_no_overlap_dissimilar_entries(db: T2Database) -> None:
    """Entries on different topics → no overlap."""
    db.put(project="proj", title="auth.md", content="authentication security tokens")
    db.put(project="proj", title="deploy.md", content="kubernetes docker containers")
    pairs = db.find_overlapping_memories("proj")
    assert len(pairs) == 0


def test_find_overlapping_respects_threshold(db: T2Database) -> None:
    """High threshold filters out moderate overlap."""
    db.put(project="proj", title="a.md",
           content="search engine architecture design")
    db.put(project="proj", title="b.md",
           content="search engine optimization performance")
    # With very high threshold, partial overlap shouldn't match
    pairs = db.find_overlapping_memories("proj", min_similarity=0.95)
    assert len(pairs) == 0


# ── merge ───────────────────────────────────────────────────────────────────


def test_merge_memories_deletes_and_updates(db: T2Database) -> None:
    """merge_memories keeps one entry, deletes the rest, updates content."""
    id1 = db.put(project="proj", title="keep.md", content="original")
    id2 = db.put(project="proj", title="delete.md", content="duplicate")
    db.merge_memories(keep_id=id1, delete_ids=[id2], merged_content="merged version")
    kept = db.get(id=id1)
    assert kept is not None and kept["content"] == "merged version"
    assert db.get(id=id2) is None


def test_merge_cleans_fts_index(db: T2Database) -> None:
    """After merge, FTS search for deleted content returns no results."""
    id1 = db.put(project="proj", title="keep.md", content="alpha content")
    id2 = db.put(project="proj", title="gone.md", content="unique_zygomorphic_keyword")
    db.merge_memories(keep_id=id1, delete_ids=[id2], merged_content="alpha merged")
    assert db.search("unique_zygomorphic_keyword") == []


def test_merge_multiple_entries(db: T2Database) -> None:
    """Can merge 3+ entries into one."""
    id1 = db.put(project="proj", title="keep.md", content="base")
    id2 = db.put(project="proj", title="dup1.md", content="dup one")
    id3 = db.put(project="proj", title="dup2.md", content="dup two")
    db.merge_memories(keep_id=id1, delete_ids=[id2, id3], merged_content="all merged")
    assert db.get(id=id1)["content"] == "all merged"
    assert db.get(id=id2) is None
    assert db.get(id=id3) is None


# ── stale flagging ──────────────────────────────────────────────────────────


def test_flag_stale_uses_last_accessed(db: T2Database) -> None:
    """Entries with old last_accessed are flagged as stale."""
    db.put(project="proj", title="old.md", content="old entry")
    # Backdate last_accessed
    old_ts = (datetime.now(UTC) - timedelta(days=45)).isoformat()
    db.conn.execute(
        "UPDATE memory SET last_accessed=? WHERE title='old.md'", (old_ts,)
    )
    db.conn.commit()

    db.put(project="proj", title="fresh.md", content="fresh entry")
    db.get(project="proj", title="fresh.md")  # sets last_accessed to now

    stale = db.flag_stale_memories("proj", idle_days=30)
    stale_titles = {e["title"] for e in stale}
    assert "old.md" in stale_titles
    assert "fresh.md" not in stale_titles


def test_flag_stale_falls_back_to_timestamp(db: T2Database) -> None:
    """Entries with empty last_accessed use timestamp for staleness check."""
    db.put(project="proj", title="never-accessed.md", content="untouched")
    # Backdate the timestamp, leave last_accessed empty
    old_ts = (datetime.now(UTC) - timedelta(days=45)).strftime("%Y-%m-%dT%H:%M:%SZ")
    db.conn.execute(
        "UPDATE memory SET timestamp=?, last_accessed='' WHERE title='never-accessed.md'",
        (old_ts,),
    )
    db.conn.commit()

    stale = db.flag_stale_memories("proj", idle_days=30)
    assert len(stale) >= 1
    assert stale[0]["title"] == "never-accessed.md"


def test_flag_stale_skips_recent_entries(db: T2Database) -> None:
    """Recently created entries are not flagged even with access_count=0."""
    db.put(project="proj", title="new.md", content="just added")
    stale = db.flag_stale_memories("proj", idle_days=14)
    assert len(stale) == 0
