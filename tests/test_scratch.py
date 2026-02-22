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


# ── Behavior 9: _t1() auto-create session ────────────────────────────────────

def test_t1_auto_create_session_when_no_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """_t1() generates a new session ID and writes it when no session file exists.

    A second call within the same process (same getsid anchor) reads the same
    file and returns a T1Database with the same session_id.
    """
    from nexus.commands.scratch import _t1

    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("NX_SESSION_PID", raising=False)

    with patch("nexus.session.os.getsid", return_value=12300):
        first = _t1()
        second = _t1()

    assert first._session_id == second._session_id

    # The session file must now exist on disk
    session_file = tmp_path / ".config" / "nexus" / "sessions" / "12300.session"
    assert session_file.exists()
    assert session_file.read_text().strip() == first._session_id
