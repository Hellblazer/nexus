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


# ── AC1: pm_init creates the 4 standard docs ──────────────────────────────────

def test_pm_init_creates_all_standard_docs(db) -> None:
    """pm_init inserts exactly 4 standard T2 entries under {repo}."""
    pm_init(db, project="myrepo")
    entries = db.list_entries(project="myrepo")
    titles = {e["title"] for e in entries}
    assert titles == {
        "METHODOLOGY.md",
        "BLOCKERS.md",
        "CONTEXT_PROTOCOL.md",
        "phases/phase-1/context.md",
    }


def test_pm_init_docs_have_pm_tag(db) -> None:
    """All standard docs are tagged with 'pm'."""
    pm_init(db, project="myrepo")
    for entry in db.list_entries(project="myrepo"):
        row = db.get(project="myrepo", title=entry["title"])
        assert row is not None
        assert "pm" in (row["tags"] or "")


def test_pm_init_docs_have_permanent_ttl(db) -> None:
    """Standard docs are stored with ttl=None (permanent)."""
    pm_init(db, project="myrepo")
    for entry in db.list_entries(project="myrepo"):
        row = db.get(project="myrepo", title=entry["title"])
        assert row is not None
        assert row["ttl"] is None


def test_pm_init_idempotent(db) -> None:
    """Calling pm_init twice does not create duplicate entries."""
    pm_init(db, project="myrepo")
    pm_init(db, project="myrepo")
    entries = db.list_entries(project="myrepo")
    assert len(entries) == 4


# ── AC2: pm_resume returns computed continuation, capped at 2000 chars ────────

def test_pm_resume_returns_computed_content(db) -> None:
    """pm_resume returns computed markdown with phase and activity info."""
    pm_init(db, project="testrepo")
    result = pm_resume(db, project="testrepo")
    assert result is not None
    assert "testrepo" in result
    assert "Phase: 1" in result


def test_pm_resume_includes_blockers(db) -> None:
    """pm_resume includes blockers in the output."""
    pm_init(db, project="testrepo")
    pm_block(db, project="testrepo", blocker="waiting on creds")
    result = pm_resume(db, project="testrepo")
    assert "waiting on creds" in result


def test_pm_resume_caps_at_2000_chars(db) -> None:
    """pm_resume returns at most 2000 characters."""
    pm_init(db, project="testrepo")
    # Add a very long phase context to push the output past 2000 chars
    db.put("testrepo", "phases/phase-1/context.md", "x" * 5000, tags="pm,phase:1,context", ttl=None)
    result = pm_resume(db, project="testrepo")
    assert len(result) <= 2000


def test_pm_resume_returns_none_when_not_initialized(db) -> None:
    """pm_resume returns None if no PM docs found for project."""
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
    row = db.get(project="myrepo", title="BLOCKERS.md")
    assert row is not None
    assert "waiting on credentials" in row["content"]


def test_pm_unblock_removes_blocker(db) -> None:
    """pm_unblock removes the nth blocker by 1-based line number."""
    pm_init(db, project="myrepo")
    pm_block(db, project="myrepo", blocker="blocker one")
    pm_block(db, project="myrepo", blocker="blocker two")
    pm_unblock(db, project="myrepo", line=1)
    row = db.get(project="myrepo", title="BLOCKERS.md")
    assert row is not None
    assert "blocker one" not in row["content"]
    assert "blocker two" in row["content"]


# ── AC4: pm_phase_next creates new phase doc ──────────────────────────────────

def test_pm_phase_next_creates_new_phase_doc(db) -> None:
    """pm_phase_next creates phases/phase-2/context.md after init (phase 1)."""
    pm_init(db, project="myrepo")
    pm_phase_next(db, project="myrepo")
    row = db.get(project="myrepo", title="phases/phase-2/context.md")
    assert row is not None
    assert "Phase 2" in row["content"]


def test_pm_phase_next_increments_correctly(db) -> None:
    """Two pm_phase_next calls produce phases 2 and 3."""
    pm_init(db, project="myrepo")
    pm_phase_next(db, project="myrepo")
    pm_phase_next(db, project="myrepo")
    row = db.get(project="myrepo", title="phases/phase-3/context.md")
    assert row is not None


# ── AC5: pm_archive — two-phase, idempotency ──────────────────────────────────

def test_pm_archive_calls_haiku_then_t3(db) -> None:
    """pm_archive synthesizes via Haiku then upserts to T3 collection."""
    pm_init(db, project="myrepo")

    mock_col = MagicMock()
    mock_col.get.return_value = {"ids": [], "metadatas": []}
    mock_t3 = MagicMock()
    mock_t3.search.return_value = []  # no prior archive
    mock_t3.get_or_create_collection.return_value = mock_col

    with patch("nexus.pm._synthesize_haiku", return_value="# Archive summary") as mock_h:
        with patch("nexus.pm.t3_knowledge", return_value=mock_t3):
            pm_archive(db, project="myrepo", status="completed", archive_ttl=90)

    mock_h.assert_called_once()
    mock_t3.upsert_chunks.assert_called_once()


def test_pm_archive_decays_t2_after_t3(db) -> None:
    """pm_archive sets TTL on T2 docs only after T3 write succeeds."""
    pm_init(db, project="myrepo")

    mock_col = MagicMock()
    mock_col.get.return_value = {"ids": [], "metadatas": []}
    mock_t3 = MagicMock()
    mock_t3.search.return_value = []
    mock_t3.get_or_create_collection.return_value = mock_col

    with patch("nexus.pm._synthesize_haiku", return_value="# summary"):
        with patch("nexus.pm.t3_knowledge", return_value=mock_t3):
            pm_archive(db, project="myrepo", status="completed", archive_ttl=90)

    # All docs should now have ttl set (decay applied)
    for entry in db.list_entries(project="myrepo"):
        row = db.get(project="myrepo", title=entry["title"])
        assert row is not None
        assert row["ttl"] == 90


def test_pm_archive_idempotent_skips_synthesis(db) -> None:
    """pm_archive skips Haiku if T3 synthesis matches current T2 state (metadata-only check)."""
    pm_init(db, project="myrepo")

    # Count active docs and get max timestamp
    entries = db.list_entries(project="myrepo")
    doc_count = len(entries)
    rows = [db.get(project="myrepo", title=e["title"]) for e in entries]
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
        with patch("nexus.pm.t3_knowledge", return_value=mock_t3):
            pm_archive(db, project="myrepo", status="completed", archive_ttl=90)

    mock_h.assert_not_called()


def test_pm_archive_aborts_on_haiku_failure(db) -> None:
    """pm_archive aborts without touching T2 if Haiku synthesis fails."""
    pm_init(db, project="myrepo")

    mock_col = MagicMock()
    mock_col.get.return_value = {"ids": [], "metadatas": []}
    mock_t3 = MagicMock()
    mock_t3.search.return_value = []
    mock_t3.get_or_create_collection.return_value = mock_col

    with patch("nexus.pm._synthesize_haiku", side_effect=RuntimeError("API error")):
        with patch("nexus.pm.t3_knowledge", return_value=mock_t3):
            with pytest.raises(RuntimeError, match="API error"):
                pm_archive(db, project="myrepo", status="completed", archive_ttl=90)

    # T2 docs should be untouched (no TTL decay)
    for entry in db.list_entries(project="myrepo"):
        row = db.get(project="myrepo", title=entry["title"])
        assert row is not None
        assert row["ttl"] is None


# ── AC6: pm_restore — reverse decay, partial expiry ──────────────────────────

def test_pm_restore_reverses_decay(db) -> None:
    """pm_restore sets ttl=None and restores pm, tags on archived docs."""
    pm_init(db, project="myrepo")
    # Simulate archive decay: set ttl and replace tags
    for entry in db.list_entries(project="myrepo"):
        row = db.get(project="myrepo", title=entry["title"])
        assert row is not None
        tags = (row["tags"] or "").replace("pm,", "pm-archived,")
        db.put("myrepo", entry["title"], row["content"], tags=tags, ttl=90)

    pm_restore(db, project="myrepo")

    for entry in db.list_entries(project="myrepo"):
        row = db.get(project="myrepo", title=entry["title"])
        assert row is not None
        assert row["ttl"] is None
        assert "pm," in (row["tags"] or "")
        assert "pm-archived," not in (row["tags"] or "")


def test_pm_restore_partial_expiry_warns(db, capsys) -> None:
    """pm_restore warns (not fails) when only some docs have expired."""
    pm_init(db, project="myrepo")
    # Keep only METHODOLOGY.md (simulate others expired — delete via db.delete()
    # so FTS5 triggers fire correctly)
    for entry in db.list_entries(project="myrepo"):
        if entry["title"] != "METHODOLOGY.md":
            db.delete("myrepo", entry["title"])
    # Mark remaining as archived
    row = db.get(project="myrepo", title="METHODOLOGY.md")
    assert row is not None
    db.put("myrepo", "METHODOLOGY.md", row["content"],
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

    with patch("nexus.pm.t3_knowledge", return_value=mock_t3):
        results = pm_reference(db, query='"caching decisions"')

    mock_t3.search.assert_called_once()
    # Verify it searched the discovered pm collection
    search_collections = mock_t3.search.call_args[0][1]
    assert "knowledge__pm__myrepo" in search_collections


def test_pm_reference_semantic_returns_empty_when_no_pm_collections(db) -> None:
    """Semantic query returns [] gracefully when no knowledge__pm__ collections exist."""
    mock_t3 = MagicMock()
    mock_t3.list_collections.return_value = []  # no pm collections

    with patch("nexus.pm.t3_knowledge", return_value=mock_t3):
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

    with patch("nexus.pm.t3_knowledge", return_value=mock_t3):
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

    with patch("nexus.pm.t3_knowledge", return_value=mock_t3):
        results = pm_reference(db, query="myrepo")

    # Should use collection.get with where filter, not col.query (semantic)
    mock_t3.search.assert_not_called()


# ── AC8: pm_search — FTS5 scoped to pm-tagged entries ─────────────────────────

def test_pm_search_finds_pm_docs(db) -> None:
    """pm_search returns T2 FTS5 matches scoped to pm-tagged entries."""
    pm_init(db, project="myrepo")
    db.put("myrepo", "METHODOLOGY.md",
           "# Methodology\n\nDecided to use ChromaDB.", tags="pm,context", ttl=None)

    results = pm_search(db, query="ChromaDB")
    assert len(results) >= 1
    assert any("ChromaDB" in r["content"] for r in results)


def test_pm_search_does_not_bleed_into_non_pm_entries(db) -> None:
    """pm_search ignores entries without 'pm' tag."""
    db.put("myrepo", "notes.md", "ChromaDB is great", tags="notes", ttl=30)
    results = pm_search(db, query="ChromaDB")
    assert all("pm" in (r.get("tags") or "") for r in results)


def test_pm_search_scoped_to_project(db) -> None:
    """pm_search --project limits results to that project."""
    pm_init(db, project="repoA")
    pm_init(db, project="repoB")
    db.put("repoA", "METHODOLOGY.md", "We chose Postgres here", tags="pm", ttl=None)
    db.put("repoB", "METHODOLOGY.md", "We chose MySQL here", tags="pm", ttl=None)

    results = pm_search(db, query="Postgres", project="repoA")
    assert all(r.get("project") == "repoA" for r in results)


# ── Behavior: pm_archive raises ValueError on empty project ───────────────────

def test_pm_archive_raises_value_error_for_empty_project(db) -> None:
    """pm_archive raises ValueError (not RuntimeError) when project has no docs."""
    mock_t3 = MagicMock()
    mock_t3.search.return_value = []

    with patch("nexus.pm.t3_knowledge", return_value=mock_t3):
        with pytest.raises(ValueError, match="No PM docs found"):
            pm_archive(db, project="nonexistent", status="completed", archive_ttl=90)


# ── Behavior: pm_archive uses returned doc_id for metadata update ─────────────

def test_pm_archive_upsert_includes_required_metadata(db) -> None:
    """pm_archive upsert includes store_type and required metadata fields in a single call."""
    pm_init(db, project="myrepo")

    mock_col = MagicMock()
    mock_col.get.return_value = {"ids": [], "metadatas": []}
    mock_t3 = MagicMock()
    mock_t3.search.return_value = []
    mock_t3.get_or_create_collection.return_value = mock_col

    with patch("nexus.pm._synthesize_haiku", return_value="# summary"):
        with patch("nexus.pm.t3_knowledge", return_value=mock_t3):
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
        with patch("nexus.pm.t3_knowledge", return_value=mock_t3):
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

    with patch("nexus.pm.t3_knowledge", return_value=mock_t3):
        results = pm_reference(db, query="myrepo")

    assert results == []
    mock_t3.get_or_create_collection.assert_not_called()


# ── AC9: pm_promote — promote T2 PM doc to T3 ────────────────────────────────

def test_pm_promote_happy_path_returns_t3_doc_id(db) -> None:
    """pm_promote fetches the T2 doc and writes it to T3, returning the doc ID."""
    pm_init(db, project="myrepo")
    db.put("myrepo", "METHODOLOGY.md", "# Methodology\n\nPromote me.", tags="pm,context", ttl=None)

    mock_t3 = MagicMock()
    mock_t3.put.return_value = "abc123def456789a"

    doc_id = pm_promote(
        db_t2=db,
        db_t3=mock_t3,
        project="myrepo",
        title="METHODOLOGY.md",
        collection="knowledge__pm__myrepo",
        ttl_days=0,
    )

    assert doc_id == "abc123def456789a"
    mock_t3.put.assert_called_once()
    call_kwargs = mock_t3.put.call_args.kwargs
    assert call_kwargs["collection"] == "knowledge__pm__myrepo"
    assert call_kwargs["title"] == "METHODOLOGY.md"
    assert "Promote me." in call_kwargs["content"]
    assert call_kwargs["store_type"] == "pm-promoted"


def test_pm_promote_missing_doc_raises(db) -> None:
    """pm_promote raises KeyError (or similar) when the T2 document is not found."""
    mock_t3 = MagicMock()

    with pytest.raises((KeyError, ValueError, LookupError), match="not found|METHODOLOGY"):
        pm_promote(
            db_t2=db,
            db_t3=mock_t3,
            project="myrepo",
            title="METHODOLOGY.md",
            collection="knowledge__pm__myrepo",
            ttl_days=0,
        )

    mock_t3.put.assert_not_called()


def test_pm_promote_ttl_translation_permanent(db) -> None:
    """T2 ttl=None (permanent) → T3 ttl_days=0, expires_at='' (permanent)."""
    pm_init(db, project="myrepo")
    db.put("myrepo", "METHODOLOGY.md", "# Content", tags="pm", ttl=None)

    mock_t3 = MagicMock()
    mock_t3.put.return_value = "someid"

    pm_promote(
        db_t2=db,
        db_t3=mock_t3,
        project="myrepo",
        title="METHODOLOGY.md",
        collection="knowledge__pm__myrepo",
        ttl_days=0,
    )

    call_kwargs = mock_t3.put.call_args.kwargs
    assert call_kwargs["ttl_days"] == 0
    assert call_kwargs["expires_at"] == ""


def test_pm_promote_ttl_translation_with_ttl(db) -> None:
    """T2 doc with ttl_days=30 → T3 ttl_days=30, expires_at non-empty."""
    pm_init(db, project="myrepo")
    db.put("myrepo", "BLOCKERS.md", "- blocker one", tags="pm,blockers", ttl=30)

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


# ── Edge cases: _split_synthesis ──────────────────────────────────────────────

def test_split_synthesis_long_text_multiple_chunks() -> None:
    """_split_synthesis with text >3960 chars containing ## sections produces multiple chunks."""
    from nexus.pm import _split_synthesis

    # Build text with 6 sections, each ~1000 chars → total ~6000 chars (> 3960 limit)
    sections = []
    for i in range(6):
        sections.append(f"## Section {i}\n" + ("x" * 980) + "\n")
    text = "\n".join(sections)
    assert len(text) > 3960, f"Test setup: text must exceed 3960 chars, got {len(text)}"

    chunks = _split_synthesis(text)
    assert len(chunks) > 1, f"Expected multiple chunks for {len(text)}-char text, got {len(chunks)}"
    assert len(chunks) <= 3, f"Expected at most 3 chunks, got {len(chunks)}"
    # All chunks should be non-empty
    for chunk in chunks:
        assert len(chunk.strip()) > 0


def test_split_synthesis_short_text_single_chunk() -> None:
    """_split_synthesis with short text returns a single-element list."""
    from nexus.pm import _split_synthesis

    short_text = "## Summary\nThis is a short synthesis.\n\n## Outcome\nAll good."
    assert len(short_text) < 3960

    chunks = _split_synthesis(short_text)
    assert chunks == [short_text]


# ── Edge cases: _synthesize_haiku ─────────────────────────────────────────────

def test_synthesize_haiku_empty_response_raises() -> None:
    """_synthesize_haiku raises RuntimeError when Anthropic returns empty message.content."""
    from nexus.pm import _synthesize_haiku

    mock_message = MagicMock()
    mock_message.content = []  # empty content list

    with patch("anthropic.Anthropic") as mock_anthropic_cls:
        mock_client = mock_anthropic_cls.return_value
        mock_client.messages.create.return_value = mock_message
        with patch("nexus.pm.get_credential", return_value="fake-api-key"):
            with pytest.raises(RuntimeError, match="empty response"):
                _synthesize_haiku(
                    docs=[{"title": "METHODOLOGY.md", "content": "hello", "timestamp": "2026-01-01T00:00:00"}],
                    project="test",
                    status="completed",
                )


def test_synthesize_haiku_trims_long_content() -> None:
    """_synthesize_haiku trims input when total doc chars exceed 100K before calling Anthropic."""
    from nexus.pm import _STANDARD_DOCS, _synthesize_haiku

    # Build docs: 5 standard (small) + 50 "other" docs (each 3000 chars) = ~150K total
    standard_docs = [
        {"title": title, "content": "short content", "timestamp": f"2026-01-{i+1:02d}T00:00:00"}
        for i, title in enumerate(_STANDARD_DOCS.keys())
    ]
    other_docs = [
        {"title": f"extra-{i}.md", "content": "y" * 3000, "timestamp": f"2026-02-{(i % 28) + 1:02d}T00:00:00"}
        for i in range(50)
    ]
    all_docs = standard_docs + other_docs
    total_chars = sum(len(d["content"]) for d in all_docs)
    assert total_chars > 100_000, f"Test setup: need >100K chars, got {total_chars}"

    mock_message = MagicMock()
    mock_message.content = [MagicMock(text="# Archive synthesis")]

    captured_prompt = {}

    def capture_create(**kwargs):
        captured_prompt["messages"] = kwargs["messages"]
        return mock_message

    with patch("anthropic.Anthropic") as mock_anthropic_cls:
        mock_client = mock_anthropic_cls.return_value
        mock_client.messages.create.side_effect = capture_create
        with patch("nexus.pm.get_credential", return_value="fake-api-key"):
            result = _synthesize_haiku(all_docs, project="test", status="completed")

    # Verify Anthropic was called
    assert "messages" in captured_prompt
    # The prompt content sent to Anthropic should be shorter than the raw total
    prompt_text = captured_prompt["messages"][0]["content"]
    # The prompt should contain fewer chars than the raw 150K input
    # (standard ~65 chars + budget ~100K max for others + prompt boilerplate)
    assert len(prompt_text) < total_chars, (
        f"Expected trimmed prompt ({len(prompt_text)} chars) < raw total ({total_chars} chars)"
    )
    assert result == "# Archive synthesis"


# ── Edge cases: pm_block / pm_unblock ─────────────────────────────────────────

def test_pm_block_appends_newline_if_missing(db) -> None:
    """When existing BLOCKERS.md content doesn't end with newline, pm_block normalizes it."""
    # Directly write content without trailing newline
    db.put("myrepo", "BLOCKERS.md", "# Blockers", tags="pm,blockers", ttl=None)

    pm_block(db, project="myrepo", blocker="new issue")

    row = db.get(project="myrepo", title="BLOCKERS.md")
    assert row is not None
    content = row["content"]
    # The blocker should appear on its own line after a newline
    assert "# Blockers\n- new issue\n" in content


def test_pm_unblock_no_bullets_returns_early(db) -> None:
    """pm_unblock returns without error when BLOCKERS.md has no bullet items."""
    pm_init(db, project="myrepo")
    # BLOCKERS.md exists from init but has no bullets

    # Should not raise any exception (no bullets = IndexError for any line)
    with pytest.raises(IndexError):
        pm_unblock(db, project="myrepo", line=1)


# ── Edge cases: pm_restore ────────────────────────────────────────────────────

def test_pm_restore_warns_on_expired_docs(db) -> None:
    """pm_restore emits a structlog warning when some standard docs have expired before restore."""
    pm_init(db, project="myrepo")

    # Delete all docs except METHODOLOGY.md to simulate expiry
    for entry in db.list_entries(project="myrepo"):
        if entry["title"] != "METHODOLOGY.md":
            db.delete("myrepo", entry["title"])

    # Mark remaining as archived (so restore_project has something to restore)
    row = db.get(project="myrepo", title="METHODOLOGY.md")
    assert row is not None
    db.put("myrepo", "METHODOLOGY.md", row["content"],
           tags="pm-archived,phase:1,context", ttl=90)

    with patch.object(pm_mod, "_log") as mock_log:
        pm_restore(db, project="myrepo")

    mock_log.warning.assert_called_once()
    # Verify the warning mentions expired docs
    call_kwargs = mock_log.warning.call_args
    assert "expired" in call_kwargs[0][0]


# ── Edge cases: pm_reference ─────────────────────────────────────────────────

def test_pm_reference_no_collection_returns_early(db) -> None:
    """pm_reference returns empty list when archive collection doesn't exist for bare project name."""
    mock_t3 = MagicMock()
    mock_t3.collection_exists.return_value = False

    with patch("nexus.pm.t3_knowledge", return_value=mock_t3):
        results = pm_reference(db, query="nonexistent_project")

    assert results == []
    # Should not attempt to create or query the collection
    mock_t3.get_or_create_collection.assert_not_called()
    mock_t3.search.assert_not_called()


# ── Edge cases: pm_status ────────────────────────────────────────────────────

def test_pm_status_empty_blockers_list(db) -> None:
    """pm_status returns empty blockers list when BLOCKERS.md exists but has no bullet items."""
    pm_init(db, project="myrepo")
    # BLOCKERS.md is created at init with header but no bullets

    status = pm_status(db, project="myrepo")
    assert status["blockers"] == []


# ── Gap 1: _synthesize_haiku raises RuntimeError when API key is empty ──────

def test_synthesize_haiku_raises_when_api_key_empty() -> None:
    """_synthesize_haiku raises RuntimeError when get_credential returns empty string."""
    from nexus.pm import _synthesize_haiku

    with patch("nexus.pm.get_credential", return_value=""):
        with pytest.raises(RuntimeError, match="anthropic_api_key is required"):
            _synthesize_haiku(
                docs=[{"title": "METHODOLOGY.md", "content": "hello", "timestamp": "2026-01-01T00:00:00"}],
                project="test",
                status="completed",
            )


# ── Gap 2: _split_synthesis no-headers fallback ────────────────────────────

def test_split_synthesis_no_headers_returns_truncated_single_chunk() -> None:
    """_split_synthesis with long text and no ## headers returns a single element truncated to _SYNTHESIS_CHAR_LIMIT."""
    from nexus.pm import _SYNTHESIS_CHAR_LIMIT, _split_synthesis

    # Build a long string with NO ## headers, exceeding the char limit
    long_text = "x" * (_SYNTHESIS_CHAR_LIMIT + 2000)
    assert len(long_text) > _SYNTHESIS_CHAR_LIMIT

    chunks = _split_synthesis(long_text)
    assert len(chunks) == 1
    assert len(chunks[0]) == _SYNTHESIS_CHAR_LIMIT


# ── Gap 3: pm_status handles non-integer phase tag gracefully ──────────────

def test_pm_status_handles_non_integer_phase_tag(db) -> None:
    """pm_status does not crash when a T2 row has tags='phase:abc' (non-integer)."""
    pm_init(db, project="myrepo")
    # Insert a doc with a non-integer phase tag
    db.put("myrepo", "bad-phase.md", "content", tags="pm,phase:abc", ttl=None)

    # Should not raise — gracefully ignores the bad tag
    status = pm_status(db, project="myrepo")
    # Phase should still be 1 from the standard docs (the bad tag is ignored)
    assert status["phase"] == 1


# ── Gap 4: pm_archive handles non-integer phase tag in archive path ────────

def test_pm_archive_handles_non_integer_phase_tag(db) -> None:
    """pm_archive does not crash when a T2 row has tags='phase:abc' (non-integer)."""
    pm_init(db, project="myrepo")
    # Insert a doc with a non-integer phase tag
    db.put("myrepo", "bad-phase.md", "content", tags="pm,phase:abc", ttl=None)

    mock_col = MagicMock()
    mock_col.get.return_value = {"ids": [], "metadatas": []}
    mock_t3 = MagicMock()
    mock_t3.get_or_create_collection.return_value = mock_col

    with patch("nexus.pm._synthesize_haiku", return_value="# Archive summary"):
        with patch("nexus.pm.t3_knowledge", return_value=mock_t3):
            # Should not raise — gracefully ignores the bad phase tag
            pm_archive(db, project="myrepo", status="completed", archive_ttl=90)

    mock_t3.upsert_chunks.assert_called_once()
    # Verify phase_count defaults to 1 (from standard docs) despite the bad tag
    meta = mock_t3.upsert_chunks.call_args.kwargs["metadatas"][0]
    assert meta["phase_count"] == 1


# ── Gap 5: pm_archive stores multiple chunks for long synthesis ────────────

def test_pm_archive_stores_multiple_chunks_for_long_synthesis(db) -> None:
    """pm_archive stores multiple T3 chunks when synthesis text exceeds _SYNTHESIS_CHAR_LIMIT."""
    from nexus.pm import _SYNTHESIS_CHAR_LIMIT

    pm_init(db, project="myrepo")

    # Build a synthesis text with multiple ## sections totalling > _SYNTHESIS_CHAR_LIMIT
    sections = []
    for i in range(6):
        sections.append(f"## Section {i}\n" + ("y" * 980) + "\n")
    long_synthesis = "\n".join(sections)
    assert len(long_synthesis) > _SYNTHESIS_CHAR_LIMIT

    upserted_calls: list[dict] = []

    mock_col = MagicMock()
    mock_col.get.return_value = {"ids": [], "metadatas": []}

    def capture_upsert(collection, ids, documents, metadatas):
        upserted_calls.append({"ids": ids, "documents": documents, "metadatas": metadatas})

    mock_t3 = MagicMock()
    mock_t3.get_or_create_collection.return_value = mock_col
    mock_t3.upsert_chunks.side_effect = capture_upsert

    with patch("nexus.pm._synthesize_haiku", return_value=long_synthesis):
        with patch("nexus.pm.t3_knowledge", return_value=mock_t3):
            pm_archive(db, project="myrepo", status="completed", archive_ttl=90)

    # Should have been called multiple times (once per chunk)
    assert len(upserted_calls) > 1, (
        f"Expected multiple upsert calls for {len(long_synthesis)}-char synthesis, got {len(upserted_calls)}"
    )
    # Each chunk's metadata should have chunk_index and chunk_total
    for i, call_data in enumerate(upserted_calls):
        meta = call_data["metadatas"][0]
        assert meta["chunk_total"] == len(upserted_calls)
        assert meta["chunk_index"] == i


# ── nexus-pjsc.7: PM functions route to t3_knowledge() ───────────────────────

def test_pm_archive_uses_t3_knowledge(db) -> None:
    """P2: pm_archive calls t3_knowledge() not make_t3()."""
    pm_init(db, project="myrepo")

    mock_col = MagicMock()
    mock_col.get.return_value = {"ids": [], "metadatas": []}
    mock_t3 = MagicMock()
    mock_t3.get_or_create_collection.return_value = mock_col

    with patch("nexus.pm._synthesize_haiku", return_value="# summary"):
        with patch("nexus.pm.t3_knowledge", return_value=mock_t3):
            pm_archive(db, project="myrepo", status="completed", archive_ttl=90)

    mock_t3.upsert_chunks.assert_called_once()


def test_pm_reference_uses_t3_knowledge(db) -> None:
    """P2: pm_reference calls t3_knowledge() not make_t3()."""
    mock_t3 = MagicMock()
    mock_t3.list_collections.return_value = []

    with patch("nexus.pm.t3_knowledge", return_value=mock_t3):
        result = pm_reference(db, query='"caching decisions"')

    assert result == []


# ── I5: pm_search uses _client.get_collection not get_or_create_collection ────

def test_pm_reference_uses_get_collection_not_get_or_create(db) -> None:
    """I5: pm_reference reads an existing T3 collection without the side effect of creating it.

    After the collection_exists() guard confirms the collection is present,
    the code should use _client.get_collection() (read-only) rather than
    get_or_create_collection() which can silently create an empty collection
    if it lost a race or the existence check is wrong.
    """
    mock_t3 = MagicMock()
    mock_t3.collection_exists.return_value = True
    mock_col = MagicMock()
    mock_col.get.return_value = {"ids": [], "documents": [], "metadatas": []}
    mock_t3._client.get_collection.return_value = mock_col

    with patch("nexus.pm.t3_knowledge", return_value=mock_t3):
        result = pm_reference(db, "myproject")

    mock_t3.get_or_create_collection.assert_not_called()
    mock_t3._client.get_collection.assert_called_with("knowledge__pm__myproject")
    assert result == []


# ── C4: pm_reference must not open two t3_knowledge() instances ───────────────


def test_pm_reference_single_t3_instance_semantic(db) -> None:
    """C4: pm_reference creates exactly one t3_knowledge() for a semantic query."""
    mock_t3 = MagicMock()
    mock_t3.list_collections.return_value = []

    with patch("nexus.pm.t3_knowledge", return_value=mock_t3) as mock_factory:
        pm_reference(db, "how do I configure nexus?")

    mock_factory.assert_called_once()


def test_pm_reference_single_t3_instance_project(db) -> None:
    """C4: pm_reference creates exactly one t3_knowledge() for a project-name query."""
    mock_t3 = MagicMock()
    mock_t3.collection_exists.return_value = False

    with patch("nexus.pm.t3_knowledge", return_value=mock_t3) as mock_factory:
        pm_reference(db, "myproject")

    mock_factory.assert_called_once()


# ── I12: pm_archive idempotency must tolerate float pm_doc_count ──────────────


def test_pm_archive_idempotent_with_float_pm_doc_count(db) -> None:
    """I12: pm_archive skips synthesis when pm_doc_count stored as float (ChromaDB Cloud)."""
    pm_init(db, project="myrepo")
    entries = db.list_entries(project="myrepo")
    doc_count = len(entries)
    rows = [db.get(project="myrepo", title=e["title"]) for e in entries]
    max_ts = max(r["timestamp"] for r in rows if r)

    # Simulate ChromaDB returning int metadata as float (known Cloud client behaviour)
    mock_col = MagicMock()
    mock_col.get.return_value = {
        "ids": ["existing-id"],
        "metadatas": [{
            "store_type": "pm-archive",
            "pm_doc_count": float(doc_count),   # ← float, not int
            "pm_latest_timestamp": max_ts,
            "chunk_total": 1,
        }],
    }
    mock_t3 = MagicMock()
    mock_t3.get_or_create_collection.return_value = mock_col

    with patch("nexus.pm._synthesize_haiku") as mock_h, \
         patch("nexus.pm.t3_knowledge", return_value=mock_t3):
        pm_archive(db, project="myrepo", status="completed", archive_ttl=90)

    # float(4) == int(4) in Python, so idempotency should fire and skip synthesis
    mock_h.assert_not_called()
