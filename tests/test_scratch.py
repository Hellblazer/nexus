"""AC2-AC6: T1 scratch operations."""
from pathlib import Path
from unittest.mock import patch

import pytest

from nexus.db.t1 import T1Database
from nexus.db.t2 import T2Database

_SESSION = "test-session-0000-0000-0000-000000000000"


@pytest.fixture
def t1(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> T1Database:
    # Redirect HOME so T1 uses a tmp directory, not ~/.config/nexus/scratch.
    monkeypatch.setenv("HOME", str(tmp_path))
    db = T1Database(session_id=_SESSION)
    db.clear()          # defensive: remove stale docs from previous test
    yield db
    db.clear()          # teardown


# ── AC2: put / get ────────────────────────────────────────────────────────────

def test_scratch_put_returns_id(t1: T1Database) -> None:
    doc_id = t1.put("hello scratch", tags="test")
    assert isinstance(doc_id, str) and len(doc_id) > 0


def test_scratch_get_by_id(t1: T1Database) -> None:
    doc_id = t1.put("some content", tags="alpha,beta")
    result = t1.get(doc_id)
    assert result is not None
    assert result["content"] == "some content"
    assert result["tags"] == "alpha,beta"
    assert result["session_id"] == _SESSION


def test_scratch_get_missing_returns_none(t1: T1Database) -> None:
    assert t1.get("nonexistent-id") is None


# ── AC3: semantic search ──────────────────────────────────────────────────────

def test_scratch_search_returns_relevant_results(t1: T1Database) -> None:
    """Semantic search ranks ML content above cooking when querying about ML."""
    ml_id1 = t1.put("training neural networks with gradient descent optimisation")
    ml_id2 = t1.put("machine learning model evaluation loss function accuracy")
    _cook = t1.put("cooking pasta with tomato sauce and fresh basil leaves")

    results = t1.search("deep learning neural network training")

    assert len(results) >= 1
    top_ids = [r["id"] for r in results[:2]]
    # At least one ML document should appear in the top-2
    assert ml_id1 in top_ids or ml_id2 in top_ids


def test_scratch_search_empty_collection_returns_empty(t1: T1Database) -> None:
    assert t1.search("anything") == []


def test_scratch_search_respects_n_results(t1: T1Database) -> None:
    for i in range(5):
        t1.put(f"document number {i} about various topics")
    results = t1.search("document", n_results=2)
    assert len(results) <= 2


# ── AC4: flag / unflag ────────────────────────────────────────────────────────

def test_scratch_flag_explicit_destination(t1: T1Database) -> None:
    doc_id = t1.put("important finding")
    t1.flag(doc_id, project="myproj", title="findings.md")

    entry = t1.get(doc_id)
    assert entry["flagged"] is True
    assert entry["flush_project"] == "myproj"
    assert entry["flush_title"] == "findings.md"


def test_scratch_flag_auto_destination(t1: T1Database) -> None:
    doc_id = t1.put("auto-flagged content")
    t1.flag(doc_id)

    entry = t1.get(doc_id)
    assert entry["flagged"] is True
    assert entry["flush_project"] == "scratch_sessions"
    assert _SESSION in entry["flush_title"]
    assert doc_id in entry["flush_title"]


def test_scratch_unflag(t1: T1Database) -> None:
    doc_id = t1.put("will be unflagged")
    t1.flag(doc_id, project="p", title="t.md")
    t1.unflag(doc_id)

    entry = t1.get(doc_id)
    assert entry["flagged"] is False
    assert entry["flush_project"] == ""
    assert entry["flush_title"] == ""


def test_scratch_flag_missing_raises(t1: T1Database) -> None:
    with pytest.raises(KeyError):
        t1.flag("no-such-id")


# ── AC5: promote → T2 ────────────────────────────────────────────────────────

def test_scratch_promote_to_t2(t1: T1Database, db: T2Database) -> None:
    doc_id = t1.put("promote me to T2", tags="important")
    t1.promote(doc_id, project="myproj", title="promoted.md", t2=db)

    result = db.get(project="myproj", title="promoted.md")
    assert result is not None
    assert result["content"] == "promote me to T2"
    assert result["tags"] == "important"


def test_scratch_promote_missing_raises(t1: T1Database, db: T2Database) -> None:
    with pytest.raises(KeyError):
        t1.promote("no-such-id", project="p", title="t.md", t2=db)


# ── AC6: clear ────────────────────────────────────────────────────────────────

def test_scratch_clear_removes_all_session_entries(t1: T1Database) -> None:
    t1.put("doc one")
    t1.put("doc two")
    t1.put("doc three")

    count = t1.clear()
    assert count == 3
    assert t1.list_entries() == []


def test_scratch_clear_empty_returns_zero(t1: T1Database) -> None:
    assert t1.clear() == 0


# ── list ──────────────────────────────────────────────────────────────────────

def test_scratch_list_returns_own_session_entries(t1: T1Database) -> None:
    t1.put("entry one", tags="a")
    t1.put("entry two", tags="b")

    entries = t1.list_entries()
    assert len(entries) == 2
    assert all(e["session_id"] == _SESSION for e in entries)


# ── persist flag on put ───────────────────────────────────────────────────────

def test_scratch_put_persist_sets_flag(t1: T1Database) -> None:
    doc_id = t1.put("auto persist", persist=True)
    entry = t1.get(doc_id)
    assert entry["flagged"] is True
    assert entry["flush_project"] == "scratch_sessions"
    assert doc_id in entry["flush_title"]


def test_scratch_put_persist_explicit_destination(t1: T1Database) -> None:
    doc_id = t1.put("persist here", persist=True, flush_project="p", flush_title="f.md")
    entry = t1.get(doc_id)
    assert entry["flagged"] is True
    assert entry["flush_project"] == "p"
    assert entry["flush_title"] == "f.md"


# ── Behavior 5: cross-session isolation via metadata ─────────────────────────

_SESSION_B = "test-session-bbbb-bbbb-bbbb-bbbbbbbbbbbb"


@pytest.fixture
def two_sessions(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Two T1Database instances sharing the same in-memory EphemeralClient."""
    import chromadb

    monkeypatch.setenv("HOME", str(tmp_path))
    shared_client = chromadb.EphemeralClient()
    db_a = T1Database(session_id=_SESSION, client=shared_client)
    db_b = T1Database(session_id=_SESSION_B, client=shared_client)
    db_a.clear()
    db_b.clear()
    yield db_a, db_b
    db_a.clear()
    db_b.clear()


def test_list_entries_scoped_to_session(two_sessions) -> None:
    """list_entries() returns only entries belonging to its own session."""
    db_a, db_b = two_sessions
    db_a.put("session A entry one")
    db_a.put("session A entry two")
    db_b.put("session B entry only")

    a_entries = db_a.list_entries()
    b_entries = db_b.list_entries()

    assert len(a_entries) == 2
    assert all(e["session_id"] == _SESSION for e in a_entries)
    assert len(b_entries) == 1
    assert b_entries[0]["session_id"] == _SESSION_B


def test_clear_does_not_delete_other_session_entries(two_sessions) -> None:
    """clear() removes only this session's entries; another session's entries survive."""
    db_a, db_b = two_sessions
    db_a.put("session A entry")
    db_b.put("session B survives")
    db_b.put("session B survives too")

    deleted = db_a.clear()

    assert deleted == 1
    assert db_a.list_entries() == []
    b_entries = db_b.list_entries()
    assert len(b_entries) == 2


# ── Behavior 6: search is session-scoped ─────────────────────────────────────

def test_search_is_scoped_to_session(two_sessions) -> None:
    """search() returns only entries belonging to the calling session.

    Per spec, T1 search is session-scoped: results from other sessions must
    not appear even when both sessions share the same underlying EphemeralClient.
    """
    db_a, db_b = two_sessions
    db_a.put("neural network gradient descent training")
    db_b.put("convolutional neural network image classification")

    # Search from db_a — must only surface db_a's own entry
    results = db_a.search("neural network machine learning", n_results=10)
    session_ids_in_results = {r["session_id"] for r in results}
    assert _SESSION in session_ids_in_results
    assert _SESSION_B not in session_ids_in_results

    # Search from db_b — must only surface db_b's own entry
    results_b = db_b.search("neural network machine learning", n_results=10)
    session_ids_b = {r["session_id"] for r in results_b}
    assert _SESSION_B in session_ids_b
    assert _SESSION not in session_ids_b


# ── Behavior 7: flagged_entries scoped to this session ────────────────────────

def test_flagged_entries_scoped_to_session(two_sessions) -> None:
    """flagged_entries() only returns flagged entries belonging to this session."""
    db_a, db_b = two_sessions
    id_a1 = db_a.put("session A flagged entry")
    db_a.put("session A unflagged entry")
    id_b1 = db_b.put("session B flagged entry")
    db_a.flag(id_a1)
    db_b.flag(id_b1)

    flagged_a = db_a.flagged_entries()
    flagged_b = db_b.flagged_entries()

    assert len(flagged_a) == 1
    assert flagged_a[0]["id"] == id_a1
    assert len(flagged_b) == 1
    assert flagged_b[0]["id"] == id_b1


# ── Behavior 8: SessionEnd orphan recovery ────────────────────────────────────

def test_session_end_orphan_recovery(two_sessions) -> None:
    """When session A ends (clear), session B's entries in the shared store survive."""
    db_a, db_b = two_sessions
    db_a.put("session A will be cleared")
    db_b.put("session B orphan survives one")
    db_b.put("session B orphan survives two")

    # Simulate session A ending: clear its entries
    db_a.clear()

    # Session B's entries are unaffected
    b_entries = db_b.list_entries()
    assert len(b_entries) == 2
    assert all(e["session_id"] == _SESSION_B for e in b_entries)
    # Session A has nothing left
    assert db_a.list_entries() == []


# ── Behavior 9: _t1() session stability from current_session ─────────────────

def test_t1_auto_create_session_when_no_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """_t1() uses the current_session flat file for stable session ID.

    When no T1 server record is found (EphemeralClient fallback), the session ID
    is read from the current_session file written by session_start.  Two calls
    within the same process return the same session_id.
    """
    import warnings
    from nexus.commands.scratch import _t1
    from nexus.session import write_claude_session_id

    monkeypatch.setenv("HOME", str(tmp_path))

    # Simulate session_start having run: write a known session ID to current_session.
    write_claude_session_id("known-stable-session-id")

    with warnings.catch_warnings(record=True):
        warnings.simplefilter("always")
        first = _t1()
        second = _t1()

    assert first._session_id == "known-stable-session-id"
    assert first._session_id == second._session_id


# ── T1 edge cases: flag/unflag sequences ─────────────────────────────────────

def test_scratch_flag_unflag_reflag(t1: T1Database) -> None:
    """Flag -> unflag -> re-flag works correctly."""
    doc_id = t1.put("reflag test")
    t1.flag(doc_id, project="p", title="t.md")
    assert t1.get(doc_id)["flagged"] is True

    t1.unflag(doc_id)
    assert t1.get(doc_id)["flagged"] is False

    t1.flag(doc_id, project="p2", title="t2.md")
    entry = t1.get(doc_id)
    assert entry["flagged"] is True
    assert entry["flush_project"] == "p2"


def test_scratch_unflag_already_unflagged(t1: T1Database) -> None:
    """Unflagging an unflagged entry is a no-op (does not crash)."""
    doc_id = t1.put("not flagged")
    # Entry starts unflagged (persist=False)
    t1.unflag(doc_id)  # should not raise
    assert t1.get(doc_id)["flagged"] is False


def test_scratch_unflag_missing_raises(t1: T1Database) -> None:
    with pytest.raises(KeyError):
        t1.unflag("no-such-id")


# ── T1 edge cases: unicode content ──────────────────────────────────────────

def test_scratch_unicode_content(t1: T1Database) -> None:
    """Non-ASCII content (CJK, emoji) round-trips correctly."""
    doc_id = t1.put("训练神经网络 🚀", tags="中文,ML")
    entry = t1.get(doc_id)
    assert entry["content"] == "训练神经网络 🚀"
    assert entry["tags"] == "中文,ML"


# ── T1 edge cases: duplicate content ────────────────────────────────────────

def test_scratch_duplicate_content_different_ids(t1: T1Database) -> None:
    """Two puts with identical content produce different IDs."""
    id1 = t1.put("same content")
    id2 = t1.put("same content")
    assert id1 != id2
    assert len(t1.list_entries()) == 2


# ── T1 edge cases: flagged_entries only returns flagged ─────────────────────

def test_scratch_flagged_entries_excludes_unflagged(t1: T1Database) -> None:
    t1.put("unflagged one")
    t1.put("unflagged two")
    flagged_id = t1.put("will be flagged")
    t1.flag(flagged_id)

    flagged = t1.flagged_entries()
    assert len(flagged) == 1
    assert flagged[0]["id"] == flagged_id


# ── nexus-st9u: T1Database.delete ────────────────────────────────────────────

def test_t1_delete_happy_path(t1: T1Database) -> None:
    """delete() removes the entry and returns True."""
    doc_id = t1.put(content="goodbye")
    assert t1.delete(doc_id) is True
    assert t1.get(doc_id) is None


def test_t1_delete_not_found_returns_false(t1: T1Database) -> None:
    """delete() on an unknown ID returns False without raising."""
    assert t1.delete("nonexistent-id-00000000-0000-0000-0000") is False


def test_t1_delete_session_isolation(tmp_path, monkeypatch) -> None:
    """delete() does not remove entries owned by a different session."""
    monkeypatch.setenv("HOME", str(tmp_path))
    session_a = T1Database(session_id="session-aaaa-0000-0000-0000-000000000000")
    session_b = T1Database(session_id="session-bbbb-0000-0000-0000-000000000000",
                           client=session_a._client)  # same underlying collection

    doc_id = session_a.put(content="owned by A")
    # Session B cannot see or delete A's entry
    assert session_b.delete(doc_id) is False
    # Entry still exists for session A
    assert session_a.get(doc_id) is not None
    session_a.clear()
    session_b.clear()


def test_t1_delete_does_not_affect_other_entries(t1: T1Database) -> None:
    """delete() removes only the targeted entry, leaving others intact."""
    id1 = t1.put(content="keep me")
    id2 = t1.put(content="delete me")
    t1.delete(id2)
    assert t1.get(id1) is not None
    assert t1.get(id2) is None
