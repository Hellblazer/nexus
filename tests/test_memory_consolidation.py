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


def test_merge_updates_fts_for_kept_entry(db: T2Database) -> None:
    """After merge, the merged content is findable via FTS search."""
    id1 = db.put(project="proj", title="keep.md", content="original boring content")
    id2 = db.put(project="proj", title="gone.md", content="other stuff")
    db.merge_memories(keep_id=id1, delete_ids=[id2], merged_content="unique_merged_phrase_xyz")
    results = db.search("unique_merged_phrase_xyz")
    assert len(results) == 1
    assert results[0]["title"] == "keep.md"


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


# ── MCP tool integration (RDR-061 E6) ────────────────────────────────────────


class _NonClosingT2Ctx:
    """Context manager wrapping a T2Database without closing on exit.

    Used in tests so the same db fixture can be reused across MCP tool calls.
    """
    def __init__(self, db: T2Database) -> None:
        self._db = db

    def __enter__(self) -> T2Database:
        return self._db

    def __exit__(self, *_: object) -> None:
        pass  # intentionally do not close


def test_mcp_memory_consolidate_find_overlaps(db: T2Database, tmp_path, monkeypatch) -> None:
    """memory_consolidate(action='find-overlaps') returns overlapping pairs."""
    from nexus.mcp.core import memory_consolidate

    db.put(project="proj", title="a.md",
           content="search engine architecture design patterns optimization")
    db.put(project="proj", title="b.md",
           content="search engine architecture design patterns optimization benchmarks")
    monkeypatch.setattr("nexus.mcp.core._t2_ctx", lambda: _NonClosingT2Ctx(db))

    result = memory_consolidate(action="find-overlaps", project="proj")
    assert "overlapping pair" in result
    assert "a.md" in result and "b.md" in result


def test_mcp_memory_consolidate_find_overlaps_none(db: T2Database, monkeypatch) -> None:
    """memory_consolidate returns friendly no-overlap message."""
    from nexus.mcp.core import memory_consolidate

    db.put(project="proj", title="a.md", content="completely unique xyzzy42 content")
    monkeypatch.setattr("nexus.mcp.core._t2_ctx", lambda: _NonClosingT2Ctx(db))

    result = memory_consolidate(action="find-overlaps", project="proj")
    assert "No overlapping" in result


def test_mcp_memory_consolidate_flag_stale(db: T2Database, monkeypatch) -> None:
    """memory_consolidate(action='flag-stale') lists stale entries."""
    from nexus.mcp.core import memory_consolidate

    db.put(project="proj", title="old.md", content="stale")
    old_ts = (datetime.now(UTC) - timedelta(days=60)).strftime("%Y-%m-%dT%H:%M:%SZ")
    db.conn.execute(
        "UPDATE memory SET timestamp=?, last_accessed='' WHERE title='old.md'",
        (old_ts,),
    )
    db.conn.commit()
    monkeypatch.setattr("nexus.mcp.core._t2_ctx", lambda: _NonClosingT2Ctx(db))

    result = memory_consolidate(action="flag-stale", project="proj", idle_days=30)
    assert "old.md" in result


def test_mcp_memory_consolidate_merge(db: T2Database, monkeypatch) -> None:
    """memory_consolidate(action='merge') merges entries and deletes the others."""
    from nexus.mcp.core import memory_consolidate

    id_a = db.put(project="proj", title="a.md", content="content a")
    id_b = db.put(project="proj", title="b.md", content="content b")
    monkeypatch.setattr("nexus.mcp.core._t2_ctx", lambda: _NonClosingT2Ctx(db))

    result = memory_consolidate(
        action="merge",
        project="proj",
        keep_id=id_a,
        delete_ids=str(id_b),
        merged_content="merged content from a and b",
    )
    assert "Merged" in result
    assert db.get(id=id_b) is None
    kept = db.get(id=id_a)
    assert kept is not None
    assert "merged content" in kept["content"]


def test_mcp_memory_consolidate_invalid_action(db: T2Database, monkeypatch) -> None:
    """Invalid action returns an error."""
    from nexus.mcp.core import memory_consolidate

    monkeypatch.setattr("nexus.mcp.core._t2_ctx", lambda: _NonClosingT2Ctx(db))
    result = memory_consolidate(action="bogus", project="proj")
    assert "Error" in result and "unknown action" in result


def test_mcp_memory_consolidate_missing_project(db: T2Database, monkeypatch) -> None:
    """find-overlaps requires project."""
    from nexus.mcp.core import memory_consolidate

    monkeypatch.setattr("nexus.mcp.core._t2_ctx", lambda: _NonClosingT2Ctx(db))
    result = memory_consolidate(action="find-overlaps", project="")
    assert "Error" in result


def test_mcp_memory_consolidate_merge_rejects_keep_in_delete(db: T2Database, monkeypatch) -> None:
    """merge must reject keep_id appearing in delete_ids (would silently delete the kept row)."""
    from nexus.mcp.core import memory_consolidate

    id_a = db.put(project="proj", title="a.md", content="content")
    monkeypatch.setattr("nexus.mcp.core._t2_ctx", lambda: _NonClosingT2Ctx(db))

    result = memory_consolidate(
        action="merge",
        project="proj",
        keep_id=id_a,
        delete_ids=str(id_a),
        merged_content="x",
    )
    assert "Error" in result
    assert "must not appear" in result
    # The kept row must still exist
    assert db.get(id=id_a) is not None


def test_merge_memories_raises_when_keep_in_delete(db: T2Database) -> None:
    """T2Database.merge_memories raises ValueError if keep_id is in delete_ids."""
    id_a = db.put(project="proj", title="a.md", content="content")
    with pytest.raises(ValueError, match="must not be in delete_ids"):
        db.merge_memories(keep_id=id_a, delete_ids=[id_a], merged_content="x")
    assert db.get(id=id_a) is not None


def test_mcp_memory_consolidate_merge_empty_delete_ids(db: T2Database, monkeypatch) -> None:
    """Whitespace-only delete_ids is rejected (not silently no-op)."""
    from nexus.mcp.core import memory_consolidate

    id_a = db.put(project="proj", title="a.md", content="content")
    monkeypatch.setattr("nexus.mcp.core._t2_ctx", lambda: _NonClosingT2Ctx(db))

    result = memory_consolidate(
        action="merge",
        project="proj",
        keep_id=id_a,
        delete_ids="   ",
        merged_content="x",
    )
    assert "Error" in result


def test_mcp_memory_consolidate_merge_non_numeric_delete_ids(db: T2Database, monkeypatch) -> None:
    """Non-numeric delete_ids returns a parse error."""
    from nexus.mcp.core import memory_consolidate

    id_a = db.put(project="proj", title="a.md", content="content")
    monkeypatch.setattr("nexus.mcp.core._t2_ctx", lambda: _NonClosingT2Ctx(db))

    result = memory_consolidate(
        action="merge",
        project="proj",
        keep_id=id_a,
        delete_ids="abc,def",
        merged_content="x",
    )
    assert "Error" in result
    assert "integer" in result


def test_mcp_memory_consolidate_keep_id_zero_rejected(db: T2Database, monkeypatch) -> None:
    """keep_id=0 is explicitly rejected (not implicitly via falsy check)."""
    from nexus.mcp.core import memory_consolidate

    monkeypatch.setattr("nexus.mcp.core._t2_ctx", lambda: _NonClosingT2Ctx(db))
    result = memory_consolidate(
        action="merge",
        project="proj",
        keep_id=0,
        delete_ids="1",
        merged_content="x",
    )
    assert "Error" in result
    assert "keep_id>0" in result


def test_find_overlapping_does_not_bump_access_count(db: T2Database) -> None:
    """find_overlapping_memories must NOT contaminate the staleness signal."""
    db.put(project="proj", title="a.md",
           content="search engine architecture design patterns")
    db.put(project="proj", title="b.md",
           content="search engine architecture design implementation")

    # Snapshot access_count before the scan
    before_a = db.conn.execute(
        "SELECT access_count FROM memory WHERE title='a.md'"
    ).fetchone()[0]
    before_b = db.conn.execute(
        "SELECT access_count FROM memory WHERE title='b.md'"
    ).fetchone()[0]

    db.find_overlapping_memories("proj")

    after_a = db.conn.execute(
        "SELECT access_count FROM memory WHERE title='a.md'"
    ).fetchone()[0]
    after_b = db.conn.execute(
        "SELECT access_count FROM memory WHERE title='b.md'"
    ).fetchone()[0]

    assert after_a == before_a
    assert after_b == before_b
