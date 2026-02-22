"""AC1–AC8+promote: PM business logic — init, resume, status, phase, archive, restore, reference, search, promote."""
from pathlib import Path
from unittest.mock import MagicMock, call, patch

import pytest

import nexus.pm as pm_mod
from nexus.pm import (
    pm_archive,
    pm_block,
    pm_init,
    pm_phase_next,
    pm_promote,
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
    """pm_archive synthesizes via Haiku then upserts to T3 collection."""
    pm_init(db, project="myrepo")

    mock_col = MagicMock()
    mock_t3 = MagicMock()
    mock_t3.search.return_value = []  # no prior archive
    mock_t3.get_or_create_collection.return_value = mock_col

    with patch("nexus.pm._synthesize_haiku", return_value="# Archive summary") as mock_h:
        with patch("nexus.pm.make_t3", return_value=mock_t3):
            pm_archive(db, project="myrepo", status="completed", archive_ttl=90)

    mock_h.assert_called_once()
    mock_t3.upsert_chunks.assert_called_once()


def test_pm_archive_decays_t2_after_t3(db) -> None:
    """pm_archive sets TTL on T2 docs only after T3 write succeeds."""
    pm_init(db, project="myrepo")

    mock_t3 = MagicMock()
    mock_t3.search.return_value = []
    mock_t3.get_or_create_collection.return_value = MagicMock()

    with patch("nexus.pm._synthesize_haiku", return_value="# summary"):
        with patch("nexus.pm.make_t3", return_value=mock_t3):
            pm_archive(db, project="myrepo", status="completed", archive_ttl=90)

    # All docs should now have ttl set (decay applied)
    for entry in db.list_entries(project="myrepo_pm"):
        row = db.get(project="myrepo_pm", title=entry["title"])
        assert row is not None
        assert row["ttl"] == 90


def test_pm_archive_idempotent_skips_synthesis(db) -> None:
    """pm_archive skips Haiku if T3 synthesis matches current T2 state (metadata-only check)."""
    pm_init(db, project="myrepo")

    # Count active docs and get max timestamp
    entries = db.list_entries(project="myrepo_pm")
    doc_count = len(entries)
    rows = [db.get(project="myrepo_pm", title=e["title"]) for e in entries]
    max_ts = max(r["timestamp"] for r in rows if r)

    # The idempotency check now uses col.get() instead of t3.search()
    mock_col = MagicMock()
    mock_col.get.return_value = {
        "ids": ["existing-id"],
        "metadatas": [{
            "store_type": "pm-archive",
            "pm_doc_count": doc_count,
            "pm_latest_timestamp": max_ts,
            "chunk_total": 1,
        }],
    }
    mock_t3 = MagicMock()
    mock_t3.get_or_create_collection.return_value = mock_col

    with patch("nexus.pm._synthesize_haiku") as mock_h:
        with patch("nexus.pm.make_t3", return_value=mock_t3):
            pm_archive(db, project="myrepo", status="completed", archive_ttl=90)

    mock_h.assert_not_called()


def test_pm_archive_aborts_on_haiku_failure(db) -> None:
    """pm_archive aborts without touching T2 if Haiku synthesis fails."""
    pm_init(db, project="myrepo")

    mock_t3 = MagicMock()
    mock_t3.search.return_value = []

    with patch("nexus.pm._synthesize_haiku", side_effect=RuntimeError("API error")):
        with patch("nexus.pm.make_t3", return_value=mock_t3):
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


def test_pm_restore_partial_expiry_warns(db, capsys) -> None:
    """pm_restore warns (not fails) when only some docs have expired."""
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

    pm_restore(db, project="myrepo")  # should not raise

    captured = capsys.readouterr()
    assert "expired" in (captured.out + captured.err).lower()


def test_pm_restore_fails_if_all_expired(db) -> None:
    """pm_restore raises if all docs have expired (none remain in T2)."""
    # Don't init — project has no entries at all
    with pytest.raises(Exception, match="fully expired|no.*docs|not found"):
        pm_restore(db, project="myrepo")


# ── AC7: pm_reference — dispatch rules ───────────────────────────────────────

def test_pm_reference_dispatch_semantic_for_quoted_query(db) -> None:
    """Quoted query fans out to all knowledge__pm__ collections via T3 semantic search."""
    mock_t3 = MagicMock()
    mock_t3.list_collections.return_value = [
        {"name": "knowledge__pm__myrepo", "count": 3}
    ]
    mock_t3.search.return_value = [{"content": "result", "distance": 0.1}]

    with patch("nexus.pm.make_t3", return_value=mock_t3):
        results = pm_reference(db, query='"caching decisions"')

    mock_t3.search.assert_called_once()
    # Verify it searched the discovered pm collection
    search_collections = mock_t3.search.call_args[0][1]
    assert "knowledge__pm__myrepo" in search_collections


def test_pm_reference_semantic_returns_empty_when_no_pm_collections(db) -> None:
    """Semantic query returns [] gracefully when no knowledge__pm__ collections exist."""
    mock_t3 = MagicMock()
    mock_t3.list_collections.return_value = []  # no pm collections

    with patch("nexus.pm.make_t3", return_value=mock_t3):
        results = pm_reference(db, query='"caching decisions"')

    assert results == []
    mock_t3.search.assert_not_called()


def test_pm_reference_dispatch_semantic_for_question(db) -> None:
    """Query containing '?' dispatches to T3 semantic search across pm collections."""
    mock_t3 = MagicMock()
    mock_t3.list_collections.return_value = [
        {"name": "knowledge__pm__proj", "count": 1}
    ]
    mock_t3.search.return_value = []

    with patch("nexus.pm.make_t3", return_value=mock_t3):
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

    with patch("nexus.pm.make_t3", return_value=mock_t3):
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

    with patch("nexus.pm.make_t3", return_value=mock_t3):
        with pytest.raises(ValueError, match="No PM docs found"):
            pm_archive(db, project="nonexistent", status="completed", archive_ttl=90)


# ── Behavior: pm_archive uses returned doc_id for metadata update ─────────────

def test_pm_archive_upsert_includes_required_metadata(db) -> None:
    """pm_archive upsert includes store_type and required metadata fields in a single call."""
    pm_init(db, project="myrepo")

    mock_col = MagicMock()
    mock_t3 = MagicMock()
    mock_t3.search.return_value = []
    mock_t3.get_or_create_collection.return_value = mock_col

    with patch("nexus.pm._synthesize_haiku", return_value="# summary"):
        with patch("nexus.pm.make_t3", return_value=mock_t3):
            pm_archive(db, project="myrepo", status="completed", archive_ttl=90)

    mock_t3.upsert_chunks.assert_called_once()
    # Metadata must include store_type so idempotency and search filtering work
    metadatas = mock_t3.upsert_chunks.call_args.kwargs["metadatas"]
    assert metadatas is not None and len(metadatas) == 1
    meta = metadatas[0]
    assert meta["store_type"] == "pm-archive", "store_type must survive upsert"
    assert meta["project"] == "myrepo"
    assert meta["status"] == "completed"
    assert "pm_doc_count" in meta
    assert "pm_latest_timestamp" in meta
    # col.update() must NOT be called (old bug: it wiped store_type)
    mock_col.update.assert_not_called()


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


# ── nexus-dsu: pm_unblock raises IndexError on out-of-range line ──────────────

def test_pm_unblock_raises_for_out_of_range_line(db) -> None:
    """pm_unblock raises IndexError when line number exceeds blocker count."""
    pm_init(db, project="myrepo")
    pm_block(db, project="myrepo", blocker="blocker A")

    with pytest.raises(IndexError, match="No blocker at line"):
        pm_unblock(db, project="myrepo", line=99)


def test_pm_unblock_raises_for_zero_line(db) -> None:
    """pm_unblock raises IndexError for line=0 (1-based lines)."""
    pm_init(db, project="myrepo")
    pm_block(db, project="myrepo", blocker="blocker A")

    with pytest.raises(IndexError):
        pm_unblock(db, project="myrepo", line=0)


# ── nexus-iyz: pm_archive writes chunk_total in metadata ─────────────────────

def test_pm_archive_writes_chunk_total_in_metadata(db) -> None:
    """pm_archive includes chunk_total in every chunk's metadata."""
    pm_init(db, project="myrepo")

    upserted_metadatas: list[dict] = []

    mock_col = MagicMock()
    mock_col.get.return_value = {"ids": [], "metadatas": []}  # no prior synthesis

    def capture_upsert_chunks(collection, ids, documents, metadatas):
        upserted_metadatas.extend(metadatas)

    mock_t3 = MagicMock()
    mock_t3.get_or_create_collection.return_value = mock_col
    mock_t3.upsert_chunks.side_effect = capture_upsert_chunks

    synthesis = "# Archive\n\n## Key Decisions\n- Used SQLite"
    with patch("nexus.pm._synthesize_haiku", return_value=synthesis):
        with patch("nexus.pm.make_t3", return_value=mock_t3):
            pm_archive(db, project="myrepo", status="completed", archive_ttl=90)

    assert len(upserted_metadatas) >= 1
    for meta in upserted_metadatas:
        assert "chunk_total" in meta, "chunk_total missing from archive metadata"
        assert meta["chunk_total"] == len(upserted_metadatas)


# ── nexus-zii: pm_reference does not create empty collection ─────────────────

def test_pm_reference_returns_empty_when_collection_does_not_exist(db) -> None:
    """pm_reference returns [] for bare project name when T3 collection doesn't exist."""
    mock_t3 = MagicMock()
    mock_t3.collection_exists.return_value = False  # collection not yet created

    with patch("nexus.pm.make_t3", return_value=mock_t3):
        results = pm_reference(db, query="myrepo")

    assert results == []
    mock_t3.get_or_create_collection.assert_not_called()


# ── AC9: pm_promote — promote T2 PM doc to T3 ────────────────────────────────

def test_pm_promote_happy_path_returns_t3_doc_id(db) -> None:
    """pm_promote fetches the T2 doc and writes it to T3, returning the doc ID."""
    pm_init(db, project="myrepo")
    db.put("myrepo_pm", "CONTINUATION.md", "# Continuation\n\nPromote me.", tags="pm,context", ttl=None)

    mock_t3 = MagicMock()
    mock_t3.put.return_value = "abc123def456789a"

    doc_id = pm_promote(
        db_t2=db,
        db_t3=mock_t3,
        project="myrepo",
        title="CONTINUATION.md",
        collection="knowledge__pm__myrepo",
        ttl_days=0,
    )

    assert doc_id == "abc123def456789a"
    mock_t3.put.assert_called_once()
    call_kwargs = mock_t3.put.call_args.kwargs
    assert call_kwargs["collection"] == "knowledge__pm__myrepo"
    assert call_kwargs["title"] == "CONTINUATION.md"
    assert "Promote me." in call_kwargs["content"]
    assert call_kwargs["store_type"] == "pm-promoted"


def test_pm_promote_missing_doc_raises(db) -> None:
    """pm_promote raises KeyError (or similar) when the T2 document is not found."""
    mock_t3 = MagicMock()

    with pytest.raises((KeyError, ValueError, LookupError), match="not found|CONTINUATION"):
        pm_promote(
            db_t2=db,
            db_t3=mock_t3,
            project="myrepo",
            title="CONTINUATION.md",
            collection="knowledge__pm__myrepo",
            ttl_days=0,
        )

    mock_t3.put.assert_not_called()


def test_pm_promote_ttl_translation_permanent(db) -> None:
    """T2 ttl=None (permanent) → T3 ttl_days=0, expires_at='' (permanent)."""
    pm_init(db, project="myrepo")
    db.put("myrepo_pm", "CONTINUATION.md", "# Content", tags="pm", ttl=None)

    mock_t3 = MagicMock()
    mock_t3.put.return_value = "someid"

    pm_promote(
        db_t2=db,
        db_t3=mock_t3,
        project="myrepo",
        title="CONTINUATION.md",
        collection="knowledge__pm__myrepo",
        ttl_days=0,
    )

    call_kwargs = mock_t3.put.call_args.kwargs
    assert call_kwargs["ttl_days"] == 0
    assert call_kwargs["expires_at"] == ""


def test_pm_promote_ttl_translation_with_ttl(db) -> None:
    """T2 doc with ttl_days=30 → T3 ttl_days=30, expires_at non-empty."""
    pm_init(db, project="myrepo")
    db.put("myrepo_pm", "BLOCKERS.md", "- blocker one", tags="pm,blockers", ttl=30)

    mock_t3 = MagicMock()
    mock_t3.put.return_value = "someid"

    pm_promote(
        db_t2=db,
        db_t3=mock_t3,
        project="myrepo",
        title="BLOCKERS.md",
        collection="knowledge__pm__myrepo",
        ttl_days=30,
    )

    call_kwargs = mock_t3.put.call_args.kwargs
    assert call_kwargs["ttl_days"] == 30
    assert call_kwargs["expires_at"] != ""
