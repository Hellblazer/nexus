"""AC1–AC8: PM business logic — init, resume, status, phase, archive, restore, reference, search."""
from pathlib import Path
from unittest.mock import MagicMock, call, patch

import pytest

import nexus.pm as pm_mod
from nexus.pm import (
    pm_archive,
    pm_block,
    pm_init,
    pm_phase_next,
    pm_reference,
    pm_restore,
    pm_resume,
    pm_search,
    pm_status,
    pm_unblock,
)


# ── Fixtures ──────────────────────────────────────────────────────────────────

@pytest.fixture
def db(tmp_path: Path):
    """A real T2Database in a temp directory, auto-closed."""
    from nexus.db.t2 import T2Database
    d = T2Database(tmp_path / "memory.db")
    yield d
    d.close()


# ── AC1: pm_init creates the 5 standard docs ──────────────────────────────────

def test_pm_init_creates_all_standard_docs(db) -> None:
    """pm_init inserts exactly 5 standard T2 entries under {repo}_pm."""
    pm_init(db, project="myrepo")
    entries = db.list_entries(project="myrepo_pm")
    titles = {e["title"] for e in entries}
    assert titles == {
        "CONTINUATION.md",
        "METHODOLOGY.md",
        "AGENT_INSTRUCTIONS.md",
        "CONTEXT_PROTOCOL.md",
        "phases/phase-1/context.md",
    }


def test_pm_init_docs_have_pm_tag(db) -> None:
    """All standard docs are tagged with 'pm'."""
    pm_init(db, project="myrepo")
    for entry in db.list_entries(project="myrepo_pm"):
        row = db.get(project="myrepo_pm", title=entry["title"])
        assert row is not None
        assert "pm" in (row["tags"] or "")


def test_pm_init_docs_have_permanent_ttl(db) -> None:
    """Standard docs are stored with ttl=None (permanent)."""
    pm_init(db, project="myrepo")
    for entry in db.list_entries(project="myrepo_pm"):
        row = db.get(project="myrepo_pm", title=entry["title"])
        assert row is not None
        assert row["ttl"] is None


def test_pm_init_idempotent(db) -> None:
    """Calling pm_init twice does not create duplicate entries."""
    pm_init(db, project="myrepo")
    pm_init(db, project="myrepo")
    entries = db.list_entries(project="myrepo_pm")
    assert len(entries) == 5


# ── AC2: pm_resume returns CONTINUATION.md content, capped at 2000 chars ─────

def test_pm_resume_returns_continuation_content(db) -> None:
    """pm_resume returns the content of CONTINUATION.md."""
    pm_init(db, project="testrepo")
    db.put("testrepo_pm", "CONTINUATION.md", "# Continuation\n\nHello.", ttl=None)
    result = pm_resume(db, project="testrepo")
    assert "Hello." in result


def test_pm_resume_caps_at_2000_chars(db) -> None:
    """pm_resume returns at most 2000 characters."""
    pm_init(db, project="testrepo")
    long_content = "x" * 5000
    db.put("testrepo_pm", "CONTINUATION.md", long_content, ttl=None)
    result = pm_resume(db, project="testrepo")
    assert len(result) <= 2000


def test_pm_resume_returns_none_when_not_initialized(db) -> None:
    """pm_resume returns None if no CONTINUATION.md found."""
    result = pm_resume(db, project="nonexistent")
    assert result is None


# ── AC3: pm_status shows phase, agent, blockers ───────────────────────────────

def test_pm_status_shows_phase_agent_blockers(db) -> None:
    """pm_status returns a dict with phase, agent, and blockers fields."""
    pm_init(db, project="myrepo")
    status = pm_status(db, project="myrepo")
    assert "phase" in status
    assert "agent" in status
    assert "blockers" in status


def test_pm_status_phase_starts_at_1(db) -> None:
    """After init, phase is 1."""
    pm_init(db, project="myrepo")
    status = pm_status(db, project="myrepo")
    assert status["phase"] == 1


def test_pm_block_adds_blocker(db) -> None:
    """pm_block appends a bullet to BLOCKERS.md."""
    pm_init(db, project="myrepo")
    pm_block(db, project="myrepo", blocker="waiting on credentials")
    row = db.get(project="myrepo_pm", title="BLOCKERS.md")
    assert row is not None
    assert "waiting on credentials" in row["content"]


def test_pm_unblock_removes_blocker(db) -> None:
    """pm_unblock removes the nth blocker by 1-based line number."""
    pm_init(db, project="myrepo")
    pm_block(db, project="myrepo", blocker="blocker one")
    pm_block(db, project="myrepo", blocker="blocker two")
    pm_unblock(db, project="myrepo", line=1)
    row = db.get(project="myrepo_pm", title="BLOCKERS.md")
    assert row is not None
    assert "blocker one" not in row["content"]
    assert "blocker two" in row["content"]


# ── AC4: pm_phase_next creates new phase doc ──────────────────────────────────

def test_pm_phase_next_creates_new_phase_doc(db) -> None:
    """pm_phase_next creates phases/phase-2/context.md after init (phase 1)."""
    pm_init(db, project="myrepo")
    pm_phase_next(db, project="myrepo")
    row = db.get(project="myrepo_pm", title="phases/phase-2/context.md")
    assert row is not None
    assert "Phase 2" in row["content"]


def test_pm_phase_next_updates_continuation(db) -> None:
    """pm_phase_next updates CONTINUATION.md to reference the new phase."""
    pm_init(db, project="myrepo")
    pm_phase_next(db, project="myrepo")
    cont = db.get(project="myrepo_pm", title="CONTINUATION.md")
    assert cont is not None
    assert "phase-2" in cont["content"] or "phase 2" in cont["content"].lower()


def test_pm_phase_next_increments_correctly(db) -> None:
    """Two pm_phase_next calls produce phases 2 and 3."""
    pm_init(db, project="myrepo")
    pm_phase_next(db, project="myrepo")
    pm_phase_next(db, project="myrepo")
    row = db.get(project="myrepo_pm", title="phases/phase-3/context.md")
    assert row is not None


# ── AC5: pm_archive — two-phase, idempotency ──────────────────────────────────

def test_pm_archive_calls_haiku_then_t3(db) -> None:
    """pm_archive synthesizes via Haiku then writes to T3."""
    pm_init(db, project="myrepo")

    mock_t3 = MagicMock()
    mock_t3.search.return_value = []  # no prior archive
    mock_t3.put.return_value = "doc-id-1"

    with patch("nexus.pm._synthesize_haiku", return_value="# Archive summary") as mock_h:
        with patch("nexus.pm._make_t3", return_value=mock_t3):
            pm_archive(db, project="myrepo", status="completed", archive_ttl=90)

    mock_h.assert_called_once()
    mock_t3.put.assert_called_once()


def test_pm_archive_decays_t2_after_t3(db) -> None:
    """pm_archive sets TTL on T2 docs only after T3 write succeeds."""
    pm_init(db, project="myrepo")

    mock_t3 = MagicMock()
    mock_t3.search.return_value = []
    mock_t3.put.return_value = "doc-id-1"

    with patch("nexus.pm._synthesize_haiku", return_value="# summary"):
        with patch("nexus.pm._make_t3", return_value=mock_t3):
            pm_archive(db, project="myrepo", status="completed", archive_ttl=90)

    # All docs should now have ttl set (decay applied)
    for entry in db.list_entries(project="myrepo_pm"):
        row = db.get(project="myrepo_pm", title=entry["title"])
        assert row is not None
        assert row["ttl"] == 90


def test_pm_archive_idempotent_skips_synthesis(db) -> None:
    """pm_archive skips Haiku if T3 synthesis matches current T2 state."""
    pm_init(db, project="myrepo")

    # Count active docs and get max timestamp
    entries = db.list_entries(project="myrepo_pm")
    doc_count = len(entries)
    rows = [db.get(project="myrepo_pm", title=e["title"]) for e in entries]
    max_ts = max(r["timestamp"] for r in rows if r)

    existing_synthesis = {
        "id": "existing-id",
        "content": "# prior summary",
        "store_type": "pm-archive",
        "pm_doc_count": doc_count,
        "pm_latest_timestamp": max_ts,
    }
    mock_t3 = MagicMock()
    mock_t3.search.return_value = [existing_synthesis]
    mock_t3.put.return_value = "doc-id-2"

    with patch("nexus.pm._synthesize_haiku") as mock_h:
        with patch("nexus.pm._make_t3", return_value=mock_t3):
            pm_archive(db, project="myrepo", status="completed", archive_ttl=90)

    mock_h.assert_not_called()


def test_pm_archive_aborts_on_haiku_failure(db) -> None:
    """pm_archive aborts without touching T2 if Haiku synthesis fails."""
    pm_init(db, project="myrepo")

    mock_t3 = MagicMock()
    mock_t3.search.return_value = []

    with patch("nexus.pm._synthesize_haiku", side_effect=RuntimeError("API error")):
        with patch("nexus.pm._make_t3", return_value=mock_t3):
            with pytest.raises(RuntimeError, match="API error"):
                pm_archive(db, project="myrepo", status="completed", archive_ttl=90)

    # T2 docs should be untouched (no TTL decay)
    for entry in db.list_entries(project="myrepo_pm"):
        row = db.get(project="myrepo_pm", title=entry["title"])
        assert row is not None
        assert row["ttl"] is None


# ── AC6: pm_restore — reverse decay, partial expiry ──────────────────────────

def test_pm_restore_reverses_decay(db) -> None:
    """pm_restore sets ttl=None and restores pm, tags on archived docs."""
    pm_init(db, project="myrepo")
    # Simulate archive decay: set ttl and replace tags
    for entry in db.list_entries(project="myrepo_pm"):
        row = db.get(project="myrepo_pm", title=entry["title"])
        assert row is not None
        tags = (row["tags"] or "").replace("pm,", "pm-archived,")
        db.put("myrepo_pm", entry["title"], row["content"], tags=tags, ttl=90)

    pm_restore(db, project="myrepo")

    for entry in db.list_entries(project="myrepo_pm"):
        row = db.get(project="myrepo_pm", title=entry["title"])
        assert row is not None
        assert row["ttl"] is None
        assert "pm," in (row["tags"] or "")
        assert "pm-archived," not in (row["tags"] or "")


def test_pm_restore_partial_expiry_warns(db, caplog) -> None:
    """pm_restore warns (not fails) when only some docs have expired."""
    import logging
    pm_init(db, project="myrepo")
    # Keep only CONTINUATION.md (simulate others expired — delete via db.delete()
    # so FTS5 triggers fire correctly)
    for entry in db.list_entries(project="myrepo_pm"):
        if entry["title"] != "CONTINUATION.md":
            db.delete("myrepo_pm", entry["title"])
    # Mark remaining as archived
    row = db.get(project="myrepo_pm", title="CONTINUATION.md")
    assert row is not None
    db.put("myrepo_pm", "CONTINUATION.md", row["content"],
           tags="pm-archived,phase:1,context", ttl=90)

    with caplog.at_level(logging.WARNING):
        pm_restore(db, project="myrepo")  # should not raise

    assert "expired" in caplog.text.lower()


def test_pm_restore_fails_if_all_expired(db) -> None:
    """pm_restore raises if all docs have expired (none remain in T2)."""
    # Don't init — project has no entries at all
    with pytest.raises(Exception, match="fully expired|no.*docs|not found"):
        pm_restore(db, project="myrepo")


# ── AC7: pm_reference — dispatch rules ───────────────────────────────────────

def test_pm_reference_dispatch_semantic_for_quoted_query(db) -> None:
    """Quoted query dispatches to T3 semantic search."""
    mock_t3 = MagicMock()
    mock_t3.search.return_value = [{"content": "result", "distance": 0.1}]

    with patch("nexus.pm._make_t3", return_value=mock_t3):
        results = pm_reference(db, query='"caching decisions"')

    mock_t3.search.assert_called_once()


def test_pm_reference_dispatch_semantic_for_question(db) -> None:
    """Query containing '?' dispatches to T3 semantic search."""
    mock_t3 = MagicMock()
    mock_t3.search.return_value = []

    with patch("nexus.pm._make_t3", return_value=mock_t3):
        pm_reference(db, query="how did we handle auth?")

    mock_t3.search.assert_called_once()


def test_pm_reference_dispatch_project_name_for_bare_word(db) -> None:
    """Bare identifier (no spaces, no special chars) uses metadata-only filter."""
    pm_init(db, project="myrepo")
    mock_t3 = MagicMock()
    # Return a pm-archive result matching the project
    mock_t3.get_or_create_collection.return_value = MagicMock(
        get=MagicMock(return_value={"ids": ["id1"], "documents": ["summary"],
                                    "metadatas": [{"project": "myrepo"}]})
    )

    with patch("nexus.pm._make_t3", return_value=mock_t3):
        results = pm_reference(db, query="myrepo")

    # Should use collection.get with where filter, not col.query (semantic)
    mock_t3.search.assert_not_called()


# ── AC8: pm_search — FTS5 scoped to _pm namespaces ───────────────────────────

def test_pm_search_finds_pm_docs(db) -> None:
    """pm_search returns T2 FTS5 matches scoped to *_pm projects."""
    pm_init(db, project="myrepo")
    db.put("myrepo_pm", "CONTINUATION.md",
           "# Continuation\n\nDecided to use ChromaDB.", tags="pm,context", ttl=None)

    results = pm_search(db, query="ChromaDB")
    assert len(results) >= 1
    assert any("ChromaDB" in r["content"] for r in results)


def test_pm_search_does_not_bleed_into_non_pm_projects(db) -> None:
    """pm_search ignores entries from non-pm projects."""
    db.put("myrepo_active", "notes.md", "ChromaDB is great", tags="notes", ttl=30)
    results = pm_search(db, query="ChromaDB")
    assert all("_pm" in r.get("project", "") for r in results)


def test_pm_search_scoped_to_project(db) -> None:
    """pm_search --project limits results to that project."""
    pm_init(db, project="repoA")
    pm_init(db, project="repoB")
    db.put("repoA_pm", "CONTINUATION.md", "We chose Postgres here", tags="pm", ttl=None)
    db.put("repoB_pm", "CONTINUATION.md", "We chose MySQL here", tags="pm", ttl=None)

    results = pm_search(db, query="Postgres", project="repoA")
    assert all(r.get("project") == "repoA_pm" for r in results)


# ── Behavior: pm_archive raises ValueError on empty project ───────────────────

def test_pm_archive_raises_value_error_for_empty_project(db) -> None:
    """pm_archive raises ValueError (not RuntimeError) when project has no docs."""
    mock_t3 = MagicMock()
    mock_t3.search.return_value = []

    with patch("nexus.pm._make_t3", return_value=mock_t3):
        with pytest.raises(ValueError, match="No PM docs found"):
            pm_archive(db, project="nonexistent", status="completed", archive_ttl=90)


# ── Behavior: pm_archive uses returned doc_id for metadata update ─────────────

def test_pm_archive_uses_put_return_id_for_metadata_update(db) -> None:
    """pm_archive passes the ID returned by t3.put() to col.update(), not a re-query."""
    pm_init(db, project="myrepo")

    mock_t3 = MagicMock()
    mock_t3.search.return_value = []
    mock_t3.put.return_value = "exact-doc-id"

    mock_col = MagicMock()
    mock_t3.get_or_create_collection.return_value = mock_col

    with patch("nexus.pm._synthesize_haiku", return_value="# summary"):
        with patch("nexus.pm._make_t3", return_value=mock_t3):
            pm_archive(db, project="myrepo", status="completed", archive_ttl=90)

    mock_col.update.assert_called_once()
    update_call_ids = mock_col.update.call_args.kwargs["ids"]
    assert update_call_ids == ["exact-doc-id"], (
        "col.update() must use the ID returned by t3.put(), not a re-query result"
    )


# ── Behavior: pm_phase_next CONTINUATION.md tag reflects current phase ────────

def test_pm_phase_next_continuation_tag_reflects_new_phase(db) -> None:
    """After pm_phase_next, CONTINUATION.md is tagged with the new phase number."""
    pm_init(db, project="myrepo")
    pm_phase_next(db, project="myrepo")

    cont = db.get(project="myrepo_pm", title="CONTINUATION.md")
    assert cont is not None
    assert "phase:2" in (cont["tags"] or ""), (
        f"Expected 'phase:2' in tags, got: {cont['tags']!r}"
    )


def test_pm_phase_next_continuation_tag_increments_correctly(db) -> None:
    """After two pm_phase_next calls, CONTINUATION.md is tagged 'phase:3'."""
    pm_init(db, project="myrepo")
    pm_phase_next(db, project="myrepo")
    pm_phase_next(db, project="myrepo")

    cont = db.get(project="myrepo_pm", title="CONTINUATION.md")
    assert cont is not None
    assert "phase:3" in (cont["tags"] or ""), (
        f"Expected 'phase:3' in tags, got: {cont['tags']!r}"
    )
