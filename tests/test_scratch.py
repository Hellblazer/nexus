"""AC2-AC6: T1 scratch operations."""
from pathlib import Path

import pytest

from nexus.db.t1 import T1Database
from nexus.db.t2 import T2Database

_SESSION = "test-session-0000-0000-0000-000000000000"
_SESSION_B = "test-session-bbbb-bbbb-bbbb-bbbbbbbbbbbb"


@pytest.fixture
def t1(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> T1Database:
    monkeypatch.setenv("HOME", str(tmp_path))
    db = T1Database(session_id=_SESSION)
    db.clear()
    yield db
    db.clear()


@pytest.fixture
def two_sessions(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    import chromadb
    monkeypatch.setenv("HOME", str(tmp_path))
    shared = chromadb.EphemeralClient()
    db_a = T1Database(session_id=_SESSION, client=shared)
    db_b = T1Database(session_id=_SESSION_B, client=shared)
    db_a.clear(); db_b.clear()
    yield db_a, db_b
    db_a.clear(); db_b.clear()


# ── AC2: put / get ────────────────────────────────────────────────────────────

def test_scratch_put_returns_id(t1: T1Database) -> None:
    assert isinstance(t1.put("hello scratch", tags="test"), str)

def test_scratch_get_by_id(t1: T1Database) -> None:
    doc_id = t1.put("some content", tags="alpha,beta")
    r = t1.get(doc_id)
    assert r["content"] == "some content"
    assert r["tags"] == "alpha,beta" and r["session_id"] == _SESSION

def test_scratch_get_missing_returns_none(t1: T1Database) -> None:
    assert t1.get("nonexistent-id") is None


# ── AC3: semantic search ──────────────────────────────────────────────────────

def test_scratch_search_returns_relevant_results(t1: T1Database) -> None:
    ml1 = t1.put("training neural networks with gradient descent optimisation")
    ml2 = t1.put("machine learning model evaluation loss function accuracy")
    t1.put("cooking pasta with tomato sauce and fresh basil leaves")
    top_ids = [r["id"] for r in t1.search("deep learning neural network training")[:2]]
    assert ml1 in top_ids or ml2 in top_ids

def test_scratch_search_empty_collection_returns_empty(t1: T1Database) -> None:
    assert t1.search("anything") == []

def test_scratch_search_respects_n_results(t1: T1Database) -> None:
    for i in range(5):
        t1.put(f"document number {i} about various topics")
    assert len(t1.search("document", n_results=2)) <= 2


# ── AC4: flag / unflag ────────────────────────────────────────────────────────

def test_scratch_flag_explicit_destination(t1: T1Database) -> None:
    doc_id = t1.put("important finding")
    t1.flag(doc_id, project="myproj", title="findings.md")
    e = t1.get(doc_id)
    assert e["flagged"] is True
    assert e["flush_project"] == "myproj" and e["flush_title"] == "findings.md"

def test_scratch_flag_auto_destination(t1: T1Database) -> None:
    doc_id = t1.put("auto-flagged content")
    t1.flag(doc_id)
    e = t1.get(doc_id)
    assert e["flagged"] is True and e["flush_project"] == "scratch_sessions"
    assert _SESSION in e["flush_title"] and doc_id in e["flush_title"]

def test_scratch_unflag(t1: T1Database) -> None:
    doc_id = t1.put("will be unflagged")
    t1.flag(doc_id, project="p", title="t.md")
    t1.unflag(doc_id)
    e = t1.get(doc_id)
    assert e["flagged"] is False and e["flush_project"] == "" and e["flush_title"] == ""

@pytest.mark.parametrize("method", ["flag", "unflag"])
def test_scratch_flag_unflag_missing_raises(t1: T1Database, method: str) -> None:
    with pytest.raises(KeyError):
        getattr(t1, method)("no-such-id")


# ── AC5: promote → T2 ────────────────────────────────────────────────────────

def test_scratch_promote_to_t2(t1: T1Database, db: T2Database) -> None:
    doc_id = t1.put("promote me to T2", tags="important")
    report = t1.promote(doc_id, project="myproj", title="promoted.md", t2=db)
    r = db.get(project="myproj", title="promoted.md")
    assert r is not None and r["content"] == "promote me to T2" and r["tags"] == "important"
    assert report is not None  # PromotionReport returned

def test_scratch_promote_missing_raises(t1: T1Database, db: T2Database) -> None:
    with pytest.raises(KeyError):
        t1.promote("no-such-id", project="p", title="t.md", t2=db)


def test_promote_returns_new_when_no_overlap(t1: T1Database, db: T2Database) -> None:
    """promote() returns action='new' when T2 has no matching content."""
    doc_id = t1.put("completely unique content xyzzy42")
    report = t1.promote(doc_id, project="proj", title="new.md", t2=db)
    assert report.action == "new"
    assert report.existing_title is None
    assert report.merged is False


def test_promote_returns_overlap_detected_when_fts5_overlap(t1: T1Database, db: T2Database) -> None:
    """promote() returns action='overlap_detected' when T2 has similar content.

    Note: merged=False because the new entry is written as a separate row;
    the agent must decide whether to actually merge.
    """
    # Pre-populate T2 with overlapping content (FTS5 is AND-of-terms)
    db.put(project="proj", title="existing.md", content="authentication design patterns")
    doc_id = t1.put("authentication design patterns")
    report = t1.promote(doc_id, project="proj", title="new-auth.md", t2=db)
    assert report.action == "overlap_detected"
    assert report.existing_title == "existing.md"
    assert report.merged is False  # overlap detected, not actually merged


def test_promote_writes_to_t2_regardless_of_action(t1: T1Database, db: T2Database) -> None:
    """Content is written to T2 whether action is 'new' or 'overlap_detected'."""
    db.put(project="proj", title="old.md", content="overlap content here")
    doc_id = t1.put("overlap content here")
    report = t1.promote(doc_id, project="proj", title="also-new.md", t2=db)
    # Regardless of report.action, the entry should exist in T2
    r = db.get(project="proj", title="also-new.md")
    assert r is not None
    assert r["content"] == "overlap content here"


def test_promote_overlap_detected_on_superset_content(t1: T1Database, db: T2Database) -> None:
    """Regression (v3.8.0 shakeout): superset content triggers overlap_detected.

    Pre-v3.8.1, ``T1.promote()`` used the scratch entry's full first-100-char
    snippet as an FTS5 MATCH query. FTS5 MATCH is implicit-AND, so any
    scratch content with even one token not present in the existing T2
    entry would return zero matches and falsely report ``action=new``.

    The fix uses only the first 3 non-stopword tokens for candidate
    retrieval and then confirms with Jaccard similarity on the full
    word sets. This test pins the superset case that was the v3.8.0
    shakeout failure: scratch content contains all of the existing T2
    entry's words plus one extra.
    """
    db.put(
        project="proj",
        title="base.md",
        content="alpha beta gamma delta epsilon zeta eta theta iota kappa lambda",
    )
    doc_id = t1.put(
        "alpha beta gamma delta epsilon zeta eta theta iota kappa lambda EXTRA"
    )
    report = t1.promote(doc_id, project="proj", title="superset.md", t2=db)
    assert report.action == "overlap_detected", (
        f"superset content should fire overlap_detected, got {report.action}"
    )
    assert report.existing_title == "base.md"
    assert report.merged is False


def test_promote_returns_new_when_jaccard_below_threshold(
    t1: T1Database, db: T2Database,
) -> None:
    """Short subset content reports new, not overlap_detected.

    Three non-stopword tokens against an 11-token base entry gives a
    Jaccard of 3/11 ≈ 0.27 which is below the 0.5 promote threshold.
    The content is "similar" by containment but semantically distinct
    — a 3-word summary vs an 11-word paragraph — so ``new`` is the
    honest answer.
    """
    db.put(
        project="proj",
        title="paragraph.md",
        content="alpha beta gamma delta epsilon zeta eta theta iota kappa lambda",
    )
    doc_id = t1.put("alpha beta gamma")
    report = t1.promote(doc_id, project="proj", title="short.md", t2=db)
    assert report.action == "new", (
        f"3-of-11 subset is below Jaccard 0.5 threshold, should be new, got {report.action}"
    )


def test_promote_returns_new_when_content_too_short(
    t1: T1Database, db: T2Database,
) -> None:
    """Scratch content with fewer than 3 non-stopword tokens always returns new.

    Very short entries (one or two words) don't benefit from overlap
    detection — there's not enough signal to compute a meaningful
    Jaccard or a sensible FTS5 query. Return ``new`` without a search.
    """
    db.put(project="proj", title="existing.md", content="some existing content")
    doc_id = t1.put("hi")  # 1 token, below the 3-token minimum
    report = t1.promote(doc_id, project="proj", title="tiny.md", t2=db)
    assert report.action == "new"


def test_promote_overlap_ignores_unrelated_content(
    t1: T1Database, db: T2Database,
) -> None:
    """Completely unrelated content under the same project reports new."""
    db.put(
        project="proj",
        title="auth.md",
        content="authentication design patterns using JWT with refresh rotation",
    )
    doc_id = t1.put("completely unrelated kubernetes scheduling topology constraints")
    report = t1.promote(doc_id, project="proj", title="k8s.md", t2=db)
    assert report.action == "new"


# ── AC6: clear ────────────────────────────────────────────────────────────────

def test_scratch_clear_removes_all(t1: T1Database) -> None:
    for _ in range(3):
        t1.put("doc")
    assert t1.clear() == 3 and t1.list_entries() == []

def test_scratch_clear_empty_returns_zero(t1: T1Database) -> None:
    assert t1.clear() == 0


# ── list ──────────────────────────────────────────────────────────────────────

def test_scratch_list_returns_own_session_entries(t1: T1Database) -> None:
    t1.put("entry one", tags="a"); t1.put("entry two", tags="b")
    entries = t1.list_entries()
    assert len(entries) == 2 and all(e["session_id"] == _SESSION for e in entries)


# ── persist flag on put ───────────────────────────────────────────────────────

@pytest.mark.parametrize("kwargs,expect_proj,expect_title", [
    (dict(persist=True), "scratch_sessions", None),
    (dict(persist=True, flush_project="p", flush_title="f.md"), "p", "f.md"),
])
def test_scratch_put_persist(t1: T1Database, kwargs, expect_proj, expect_title) -> None:
    doc_id = t1.put("persist content", **kwargs)
    e = t1.get(doc_id)
    assert e["flagged"] is True and e["flush_project"] == expect_proj
    if expect_title:
        assert e["flush_title"] == expect_title
    else:
        assert doc_id in e["flush_title"]


# ── Cross-session isolation ──────────────────────────────────────────────────

def test_list_entries_scoped_to_session(two_sessions) -> None:
    db_a, db_b = two_sessions
    db_a.put("A1"); db_a.put("A2"); db_b.put("B1")
    assert len(db_a.list_entries()) == 2
    assert all(e["session_id"] == _SESSION for e in db_a.list_entries())
    assert len(db_b.list_entries()) == 1 and db_b.list_entries()[0]["session_id"] == _SESSION_B

def test_clear_does_not_delete_other_session(two_sessions) -> None:
    db_a, db_b = two_sessions
    db_a.put("A"); db_b.put("B1"); db_b.put("B2")
    assert db_a.clear() == 1 and db_a.list_entries() == [] and len(db_b.list_entries()) == 2

def test_search_is_scoped_to_session(two_sessions) -> None:
    db_a, db_b = two_sessions
    db_a.put("neural network gradient descent training")
    db_b.put("convolutional neural network image classification")
    for db, own, other in [(db_a, _SESSION, _SESSION_B), (db_b, _SESSION_B, _SESSION)]:
        sids = {r["session_id"] for r in db.search("neural network machine learning", n_results=10)}
        assert own in sids and other not in sids

def test_flagged_entries_scoped_to_session(two_sessions) -> None:
    db_a, db_b = two_sessions
    id_a = db_a.put("A flagged"); db_a.put("A unflagged")
    id_b = db_b.put("B flagged")
    db_a.flag(id_a); db_b.flag(id_b)
    fa, fb = db_a.flagged_entries(), db_b.flagged_entries()
    assert len(fa) == 1 and fa[0]["id"] == id_a
    assert len(fb) == 1 and fb[0]["id"] == id_b

def test_session_end_orphan_recovery(two_sessions) -> None:
    db_a, db_b = two_sessions
    db_a.put("A cleared"); db_b.put("B1"); db_b.put("B2")
    db_a.clear()
    b = db_b.list_entries()
    assert len(b) == 2 and all(e["session_id"] == _SESSION_B for e in b)
    assert db_a.list_entries() == []


# ── _t1() session stability ─────────────────────────────────────────────────

def test_t1_auto_create_session_when_no_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import warnings
    from nexus.commands.scratch import _t1
    from nexus.session import write_claude_session_id
    monkeypatch.setenv("HOME", str(tmp_path))
    write_claude_session_id("known-stable-session-id")
    with warnings.catch_warnings(record=True):
        warnings.simplefilter("always")
        first, second = _t1(), _t1()
    assert first._session_id == "known-stable-session-id" == second._session_id


# ── Edge cases ───────────────────────────────────────────────────────────────

def test_scratch_flag_unflag_reflag(t1: T1Database) -> None:
    doc_id = t1.put("reflag test")
    t1.flag(doc_id, project="p", title="t.md")
    assert t1.get(doc_id)["flagged"] is True
    t1.unflag(doc_id)
    assert t1.get(doc_id)["flagged"] is False
    t1.flag(doc_id, project="p2", title="t2.md")
    e = t1.get(doc_id)
    assert e["flagged"] is True and e["flush_project"] == "p2"

def test_scratch_unflag_already_unflagged(t1: T1Database) -> None:
    doc_id = t1.put("not flagged")
    t1.unflag(doc_id)
    assert t1.get(doc_id)["flagged"] is False

def test_scratch_unicode_content(t1: T1Database) -> None:
    doc_id = t1.put("训练神经网络 🚀", tags="中文,ML")
    e = t1.get(doc_id)
    assert e["content"] == "训练神经网络 🚀" and e["tags"] == "中文,ML"

def test_scratch_duplicate_content_different_ids(t1: T1Database) -> None:
    id1, id2 = t1.put("same"), t1.put("same")
    assert id1 != id2 and len(t1.list_entries()) == 2

def test_scratch_flagged_entries_excludes_unflagged(t1: T1Database) -> None:
    t1.put("u1"); t1.put("u2")
    fid = t1.put("will be flagged")
    t1.flag(fid)
    flagged = t1.flagged_entries()
    assert len(flagged) == 1 and flagged[0]["id"] == fid


# ── delete ───────────────────────────────────────────────────────────────────

def test_t1_delete_happy_path(t1: T1Database) -> None:
    doc_id = t1.put(content="goodbye")
    assert t1.delete(doc_id) is True and t1.get(doc_id) is None

def test_t1_delete_not_found_returns_false(t1: T1Database) -> None:
    assert t1.delete("nonexistent-id-00000000-0000-0000-0000") is False

def test_t1_delete_session_isolation(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    sa = T1Database(session_id="session-aaaa-0000-0000-0000-000000000000")
    sb = T1Database(session_id="session-bbbb-0000-0000-0000-000000000000", client=sa._client)
    doc_id = sa.put(content="owned by A")
    assert sb.delete(doc_id) is False and sa.get(doc_id) is not None
    sa.clear(); sb.clear()

def test_t1_delete_does_not_affect_other_entries(t1: T1Database) -> None:
    id1, id2 = t1.put(content="keep me"), t1.put(content="delete me")
    t1.delete(id2)
    assert t1.get(id1) is not None and t1.get(id2) is None


# ── pagination ───────────────────────────────────────────────────────────────

@pytest.mark.parametrize("method,expected", [("list_entries", 5), ("clear", 5)])
def test_t1_pagination(t1: T1Database, method: str, expected: int) -> None:
    for i in range(5):
        t1.put(content=f"entry {i}")
    result = getattr(t1, method)()
    if method == "list_entries":
        assert len(result) == expected
    else:
        assert result == expected and len(t1.list_entries()) == 0


# ── access tracking (RDR-057 P1-1b, nexus-jpsj) ────────────────────────────


def test_new_entry_has_access_count_zero(t1: T1Database) -> None:
    doc_id = t1.put(content="fresh entry")
    raw = t1._col.get(ids=[doc_id], include=["metadatas"])
    meta = raw["metadatas"][0]
    assert meta["access_count"] == 0
    assert meta["last_accessed"] == ""


def test_get_increments_access_count(t1: T1Database) -> None:
    doc_id = t1.put(content="trackable entry")
    t1.get(doc_id)
    raw = t1._col.get(ids=[doc_id], include=["metadatas"])
    assert raw["metadatas"][0]["access_count"] == 1


def test_get_three_times_access_count_three(t1: T1Database) -> None:
    doc_id = t1.put(content="multi access")
    t1.get(doc_id)
    t1.get(doc_id)
    t1.get(doc_id)
    raw = t1._col.get(ids=[doc_id], include=["metadatas"])
    assert raw["metadatas"][0]["access_count"] == 3


def test_search_increments_access_count(t1: T1Database) -> None:
    doc_id = t1.put(content="searchable unique content zygomorphic")
    t1.search("zygomorphic")
    raw = t1._col.get(ids=[doc_id], include=["metadatas"])
    assert raw["metadatas"][0]["access_count"] == 1


def test_last_accessed_updated_after_get(t1: T1Database) -> None:
    doc_id = t1.put(content="timestamp check")
    t1.get(doc_id)
    raw = t1._col.get(ids=[doc_id], include=["metadatas"])
    ts = raw["metadatas"][0]["last_accessed"]
    assert ts != ""
    # Should be a valid ISO timestamp
    from datetime import datetime
    datetime.fromisoformat(ts)


def test_get_preserves_existing_metadata(t1: T1Database) -> None:
    """Access tracking must not wipe session_id, tags, or flagged."""
    doc_id = t1.put(content="preserve me", tags="important")
    t1.get(doc_id)
    raw = t1._col.get(ids=[doc_id], include=["metadatas"])
    meta = raw["metadatas"][0]
    assert meta["session_id"] == _SESSION
    assert meta["tags"] == "important"
    assert meta["access_count"] == 1


def test_get_return_value_reflects_updated_access_count(t1: T1Database) -> None:
    """C-1 fix: get() return value shows the incremented access_count, not stale."""
    doc_id = t1.put(content="return value check")
    result = t1.get(doc_id)
    assert result["access_count"] == 1  # return value reflects the update
    result2 = t1.get(doc_id)
    assert result2["access_count"] == 2


def test_promote_does_not_increment_access_count(t1: T1Database, db: T2Database) -> None:
    """I-2 fix: promote() does not inflate access_count."""
    doc_id = t1.put(content="promote without tracking")
    t1.promote(doc_id, project="proj", title="p.md", t2=db)
    raw = t1._col.get(ids=[doc_id], include=["metadatas"])
    assert raw["metadatas"][0]["access_count"] == 0


def test_access_count_update_failure_does_not_raise(t1: T1Database) -> None:
    """If the access count update fails, get() should still return the entry."""
    doc_id = t1.put(content="resilient entry")
    original_update = t1._col.update

    def broken_update(**kwargs):
        raise RuntimeError("simulated failure")

    t1._col.update = broken_update
    try:
        result = t1.get(doc_id)
        assert result is not None
        assert result["content"] == "resilient entry"
    finally:
        t1._col.update = original_update
