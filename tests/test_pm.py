"""AC1–AC8+promote: PM business logic — init, resume, status, phase, archive, restore, reference, search, promote."""
from pathlib import Path
from unittest.mock import MagicMock, call, patch

import pytest

import nexus.pm as pm_mod
from nexus.pm import (
    pm_block,
    pm_init,
    pm_phase_next,
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


# ── Edge cases: pm_status ────────────────────────────────────────────────────

def test_pm_status_empty_blockers_list(db) -> None:
    """pm_status returns empty blockers list when BLOCKERS.md exists but has no bullet items."""
    pm_init(db, project="myrepo")
    # BLOCKERS.md is created at init with header but no bullets

    status = pm_status(db, project="myrepo")
    assert status["blockers"] == []




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
