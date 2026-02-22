"""AC2-AC6: T1 scratch operations."""
from pathlib import Path

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
