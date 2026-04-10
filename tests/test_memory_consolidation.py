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


def test_merge_memories_raises_when_keep_id_not_found(db: T2Database) -> None:
    """R4-1: merge aborts if keep_id doesn't exist (prevents data loss on expire race)."""
    id_b = db.put(project="proj", title="b.md", content="b content")
    # Try to merge into a non-existent keep_id (simulates expire() deletion)
    with pytest.raises(KeyError, match="not found"):
        db.merge_memories(keep_id=99999, delete_ids=[id_b], merged_content="merged")
    # Critically: id_b must still exist — the DELETE should NOT have run
    assert db.get(id=id_b) is not None, (
        "R4-1 regression: merge_memories deleted delete_ids even though "
        "keep_id was missing — silent data loss"
    )


def test_merge_memories_concurrent_expire_race(tmp_path) -> None:
    """R5-1: two threads — one deletes keep_id, the other merges.

    Validates that the atomicity guarantee holds. Without the rowcount
    check, the racing thread could delete keep_id between our UPDATE
    (which would silently affect 0 rows) and DELETE, destroying the
    contents of delete_ids. With the rowcount check + single transaction,
    either the merge completes atomically OR it raises KeyError and
    leaves delete_ids intact.

    Uses two T2Database instances on the same file to exercise SQLite's
    write-lock serialization (not just Python's threading.Lock).
    """
    import threading

    from nexus.db.t2 import T2Database

    db_path = tmp_path / "race.db"

    # Seed the DB
    seed = T2Database(db_path)
    id_a = seed.put(project="proj", title="a.md", content="to be merged")
    id_b = seed.put(project="proj", title="b.md", content="also to be merged")
    seed.close()

    # Open two separate connections (simulating separate threads/processes)
    db_merge = T2Database(db_path)
    db_delete = T2Database(db_path)

    # Synchronize: thread 1 deletes keep_id, thread 2 attempts merge.
    # Possible legitimate orderings:
    #   - merge first: UPDATE a, DELETE b, commit → then delete runs DELETE a.
    #     Final: a gone, b gone (both writes succeeded in sequence)
    #   - delete first: DELETE a, commit → merge UPDATE a sees rowcount=0
    #     → merge raises KeyError, b survives. Final: a gone, b present.
    # Data-loss bug would be: merge UPDATE sees rowcount=0 BUT still runs
    # DELETE b anyway. The only way to distinguish legitimate "both gone"
    # from data loss is to track whether merge raised KeyError.
    errors: list[Exception] = []
    merge_raised_keyerror = {"value": False}
    barrier = threading.Barrier(2, timeout=5)

    def do_delete():
        try:
            barrier.wait()
            db_delete.delete(project="proj", title="a.md")
        except Exception as exc:
            errors.append(exc)

    def do_merge():
        try:
            barrier.wait()
            db_merge.merge_memories(
                keep_id=id_a,
                delete_ids=[id_b],
                merged_content="merged content",
            )
        except KeyError:
            merge_raised_keyerror["value"] = True
        except Exception as exc:
            errors.append(exc)

    t1 = threading.Thread(target=do_delete)
    t2 = threading.Thread(target=do_merge)
    t1.start()
    t2.start()
    t1.join(timeout=5)
    t2.join(timeout=5)

    assert not errors, f"Unexpected errors in race test: {errors}"

    # Post-race check: if merge raised KeyError (delete won the race),
    # b.md MUST still exist — the DELETE must NOT have run. This is the
    # R5-1 data-loss invariant.
    db_check = T2Database(db_path)
    a_exists = db_check.get(id=id_a) is not None
    b_exists = db_check.get(id=id_b) is not None
    db_check.close()
    db_merge.close()
    db_delete.close()

    if merge_raised_keyerror["value"]:
        assert b_exists, (
            "R5-1 data loss: merge_memories raised KeyError (keep_id was "
            "missing) but delete_ids were destroyed anyway. The DELETE ran "
            "despite the rowcount check — atomicity guarantee failed."
        )
        assert not a_exists  # delete won — a should be gone


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


def test_mcp_memory_consolidate_merge_dry_run(db: T2Database, monkeypatch) -> None:
    """dry_run=True returns a preview without modifying T2."""
    from nexus.mcp.core import memory_consolidate

    id_a = db.put(project="proj", title="a.md", content="content a")
    id_b = db.put(project="proj", title="b.md", content="content b")
    monkeypatch.setattr("nexus.mcp.core._t2_ctx", lambda: _NonClosingT2Ctx(db))

    result = memory_consolidate(
        action="merge",
        project="proj",
        keep_id=id_a,
        delete_ids=str(id_b),
        merged_content="merged preview",
        dry_run=True,
    )
    assert "[DRY RUN]" in result
    assert "a.md" in result
    # No modification occurred
    assert db.get(id=id_a)["content"] == "content a"
    assert db.get(id=id_b) is not None


def test_mcp_memory_consolidate_merge_multi_requires_confirm(
    db: T2Database, monkeypatch
) -> None:
    """Merging more than one entry requires confirm_destructive=True."""
    from nexus.mcp.core import memory_consolidate

    id_a = db.put(project="proj", title="a.md", content="a")
    id_b = db.put(project="proj", title="b.md", content="b")
    id_c = db.put(project="proj", title="c.md", content="c")
    monkeypatch.setattr("nexus.mcp.core._t2_ctx", lambda: _NonClosingT2Ctx(db))

    # Without confirm — rejected
    result = memory_consolidate(
        action="merge",
        project="proj",
        keep_id=id_a,
        delete_ids=f"{id_b},{id_c}",
        merged_content="merged",
    )
    assert "Error" in result
    assert "confirm_destructive" in result
    assert db.get(id=id_b) is not None  # untouched

    # With confirm — proceeds
    result = memory_consolidate(
        action="merge",
        project="proj",
        keep_id=id_a,
        delete_ids=f"{id_b},{id_c}",
        merged_content="merged",
        confirm_destructive=True,
    )
    assert "Merged" in result
    assert db.get(id=id_b) is None
    assert db.get(id=id_c) is None


def test_mcp_memory_consolidate_merge_single_delete_no_confirm(
    db: T2Database, monkeypatch
) -> None:
    """Merging a single entry does NOT require confirm_destructive."""
    from nexus.mcp.core import memory_consolidate

    id_a = db.put(project="proj", title="a.md", content="a")
    id_b = db.put(project="proj", title="b.md", content="b")
    monkeypatch.setattr("nexus.mcp.core._t2_ctx", lambda: _NonClosingT2Ctx(db))

    result = memory_consolidate(
        action="merge",
        project="proj",
        keep_id=id_a,
        delete_ids=str(id_b),
        merged_content="merged",
    )
    assert "Merged" in result
    assert db.get(id=id_b) is None


def test_mcp_memory_consolidate_with_real_t2_ctx(tmp_path, monkeypatch) -> None:
    """Integration test using the real T2Database construction path.

    Unlike the other tests which use _NonClosingT2Ctx to keep a shared
    fixture alive, this test exercises the real _t2_ctx() behavior: each
    MCP call opens/closes a fresh connection. Catches bugs that would only
    surface with real connection lifecycle (schema visibility, migration
    guard behavior on reopened paths, etc.).
    """
    from nexus.db.t2 import T2Database
    from nexus.mcp.core import memory_consolidate

    db_path = tmp_path / "real_t2.db"

    # Seed the DB with a few entries, then close — the MCP tool will
    # reopen it fresh via _t2_ctx.
    seed_db = T2Database(db_path)
    # Near-identical content so Jaccard similarity exceeds 0.7
    seed_db.put(project="proj", title="a.md",
                content="search engine architecture design patterns optimization benchmarks indexing")
    seed_db.put(project="proj", title="b.md",
                content="search engine architecture design patterns optimization benchmarks retrieval")
    seed_db.close()

    # Real _t2_ctx — fresh connection each call
    monkeypatch.setattr(
        "nexus.mcp.core._t2_ctx",
        lambda: T2Database(db_path),
    )

    # find-overlaps on a re-opened DB
    result = memory_consolidate(action="find-overlaps", project="proj")
    assert "overlapping pair" in result
    assert "a.md" in result

    # Backdate a.md so flag-stale has something to find
    backdate_db = T2Database(db_path)
    old_ts = (datetime.now(UTC) - timedelta(days=60)).strftime("%Y-%m-%dT%H:%M:%SZ")
    backdate_db.conn.execute(
        "UPDATE memory SET timestamp=?, last_accessed='' WHERE title='a.md'",
        (old_ts,),
    )
    backdate_db.conn.commit()
    backdate_db.close()

    # flag-stale on the same re-opened DB
    result = memory_consolidate(action="flag-stale", project="proj", idle_days=30)
    assert "a.md" in result


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
