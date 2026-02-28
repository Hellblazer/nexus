"""AC3-AC5: nx memory put/get/search/expire behavior."""
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from click.testing import CliRunner

from nexus.cli import main
from nexus.db.t2 import T2Database


def test_memory_put_upsert(db: T2Database) -> None:
    """Put twice with same project+title → single row, updated content."""
    db.put(project="proj", title="file.md", content="first")
    db.put(project="proj", title="file.md", content="updated")

    count = db.conn.execute(
        "SELECT COUNT(*) FROM memory WHERE project='proj' AND title='file.md'"
    ).fetchone()[0]
    assert count == 1

    content = db.conn.execute(
        "SELECT content FROM memory WHERE project='proj' AND title='file.md'"
    ).fetchone()[0]
    assert content == "updated"


def test_memory_get_by_project_title(db: T2Database) -> None:
    """Deterministic retrieval by (project, title) returns correct entry."""
    db.put(project="proj_a", title="notes.md", content="hello world")

    result = db.get(project="proj_a", title="notes.md")
    assert result is not None
    assert result["content"] == "hello world"
    assert result["project"] == "proj_a"
    assert result["title"] == "notes.md"


def test_memory_get_by_id(db: T2Database) -> None:
    """Retrieval by numeric ID returns correct entry."""
    row_id = db.put(project="p", title="x.md", content="by id")
    result = db.get(id=row_id)
    assert result is not None
    assert result["content"] == "by id"


def test_memory_get_missing_returns_none(db: T2Database) -> None:
    result = db.get(project="no", title="such.md")
    assert result is None


def test_memory_search_fts5(db: T2Database) -> None:
    """Insert content; search by keyword; ranked results returned."""
    db.put(project="p", title="alpha.md", content="The quick brown fox")
    db.put(project="p", title="beta.md", content="A lazy dog sleeping")
    db.put(project="p", title="gamma.md", content="The quick fox jumps high")

    results = db.search("quick fox")
    titles = {r["title"] for r in results}
    # Both docs containing "quick" AND "fox" must appear
    assert "alpha.md" in titles
    assert "gamma.md" in titles
    assert "beta.md" not in titles


def test_memory_search_scoped_to_project(db: T2Database) -> None:
    """Search scoped to a project excludes other projects."""
    db.put(project="proj_a", title="a.md", content="authentication token")
    db.put(project="proj_b", title="b.md", content="authentication token")

    results = db.search("authentication", project="proj_a")
    assert all(r["project"] == "proj_a" for r in results)
    assert len(results) == 1


def test_memory_expire_ttl(db: T2Database) -> None:
    """Entry with expired TTL is removed by expire()."""
    db.put(project="proj", title="old.md", content="stale", ttl=1)

    # Backdate the timestamp by 2 days to simulate time passing
    past = (datetime.now(UTC) - timedelta(days=2)).strftime("%Y-%m-%dT%H:%M:%SZ")
    db.conn.execute("UPDATE memory SET timestamp=? WHERE title='old.md'", (past,))
    db.conn.commit()

    assert db.conn.execute("SELECT COUNT(*) FROM memory WHERE title='old.md'").fetchone()[0] == 1

    deleted = db.expire()
    assert deleted == 1
    assert db.conn.execute("SELECT COUNT(*) FROM memory WHERE title='old.md'").fetchone()[0] == 0


def test_memory_expire_permanent_not_deleted(db: T2Database) -> None:
    """Entry with ttl=None (permanent) is NOT removed by expire()."""
    db.put(project="proj", title="perm.md", content="keep forever", ttl=None)

    past = (datetime.now(UTC) - timedelta(days=365)).strftime("%Y-%m-%dT%H:%M:%SZ")
    db.conn.execute("UPDATE memory SET timestamp=? WHERE title='perm.md'", (past,))
    db.conn.commit()

    db.expire()
    assert db.conn.execute("SELECT COUNT(*) FROM memory WHERE title='perm.md'").fetchone()[0] == 1


def test_memory_list_by_project(db: T2Database) -> None:
    """list_entries filtered by project returns only matching entries."""
    db.put(project="proj_a", title="x.md", content="x")
    db.put(project="proj_a", title="y.md", content="y")
    db.put(project="proj_b", title="z.md", content="z")

    entries = db.list_entries(project="proj_a")
    assert len(entries) == 2
    titles = {e["title"] for e in entries}
    assert titles == {"x.md", "y.md"}


# ── FTS5 safety: malformed query raises ValueError ────────────────────────────

def test_decay_project_single_pm_tag(db: T2Database) -> None:
    """decay_project correctly replaces 'pm' when it is the only tag (no trailing comma)."""
    db.put(project="proj", title="note.md", content="x", tags="pm")
    db.decay_project("proj", ttl=30)
    row = db.get(project="proj", title="note.md")
    assert row is not None
    assert row["tags"] == "pm-archived"


def test_restore_project_single_pm_archived_tag(db: T2Database) -> None:
    """restore_project reverses decay correctly for the single-tag 'pm-archived' case."""
    db.put(project="proj", title="note.md", content="x", tags="pm-archived")
    db.restore_project("proj")
    row = db.get(project="proj", title="note.md")
    assert row is not None
    assert row["tags"] == "pm"


def test_search_raises_on_malformed_fts5_query(db: T2Database) -> None:
    """FTS5 queries with bare operators (AND, OR alone) raise ValueError, not OperationalError."""
    import pytest
    db.put(project="proj", title="doc.md", content="some content")

    with pytest.raises(ValueError, match="Invalid search query"):
        db.search("AND")


def test_search_glob_raises_on_malformed_fts5_query(db: T2Database) -> None:
    """search_glob with bare FTS5 operator raises ValueError."""
    import pytest
    db.put(project="proj_pm", title="doc.md", content="some content")

    with pytest.raises(ValueError, match="Invalid search query"):
        db.search_glob("NOT", "*_pm")


# ── nexus-ft2: restore_project returns list[str], not tuple ──────────────────

def test_restore_project_returns_list_of_titles(db: T2Database) -> None:
    """restore_project returns a plain list[str], not a tuple."""
    db.put(project="myproj", title="a.md", content="aaa", tags="pm-archived")
    db.put(project="myproj", title="b.md", content="bbb", tags="pm-archived")

    result = db.restore_project("myproj")

    assert isinstance(result, list), f"Expected list, got {type(result)}"
    assert set(result) == {"a.md", "b.md"}


# ── nexus-206: nx memory promote ──────────────────────────────────────────────

@pytest.fixture
def runner() -> CliRunner:
    return CliRunner()


@pytest.fixture
def mem_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Redirect HOME to tmp_path so memory.db goes to a temp dir."""
    monkeypatch.setenv("HOME", str(tmp_path))
    return tmp_path


def test_promote_cmd_no_credentials_raises(runner: CliRunner, mem_home: Path, db: T2Database) -> None:
    """promote fails with helpful message when voyage_api_key is absent."""
    row_id = db.put(project="p", title="note.md", content="hello", ttl=30)

    with patch("nexus.commands.memory.T2Database", return_value=db):
        with patch("nexus.commands.memory.t3_knowledge",
                   side_effect=RuntimeError("voyage_api_key not configured")):
            result = runner.invoke(
                main, ["memory", "promote", str(row_id), "--collection", "knowledge__p"]
            )

    assert result.exit_code != 0
    assert "voyage_api_key" in result.output.lower()


def test_promote_cmd_entry_not_found_exits(runner: CliRunner, mem_home: Path, db: T2Database) -> None:
    """promote with non-existent id exits with error."""
    with patch("nexus.commands.memory.T2Database", return_value=db):
        result = runner.invoke(
            main, ["memory", "promote", "9999", "--collection", "knowledge__p"]
        )

    assert result.exit_code != 0
    assert "not found" in result.output.lower() or "9999" in result.output


def test_promote_cmd_calls_t3_put(runner: CliRunner, mem_home: Path, db: T2Database) -> None:
    """promote with valid id calls t3_knowledge().put and echoes doc_id."""
    row_id = db.put(project="proj", title="doc.md", content="the content", ttl=7, tags="ai")

    mock_t3 = MagicMock()
    mock_t3.put.return_value = "abc123"

    with patch("nexus.commands.memory.T2Database", return_value=db):
        with patch("nexus.commands.memory.t3_knowledge", return_value=mock_t3):
            result = runner.invoke(
                main,
                ["memory", "promote", str(row_id), "--collection", "knowledge__proj"],
            )

    assert result.exit_code == 0, result.output
    mock_t3.put.assert_called_once()
    call_kwargs = mock_t3.put.call_args.kwargs
    assert call_kwargs["collection"] == "knowledge__proj"
    assert call_kwargs["content"] == "the content"
    assert call_kwargs["title"] == "doc.md"
    assert call_kwargs["ttl_days"] == 7
    assert "abc123" in result.output


def test_promote_cmd_permanent_entry_ttl_is_zero(runner: CliRunner, mem_home: Path, db: T2Database) -> None:
    """Permanent T2 entry (ttl=None) → T3 ttl_days=0."""
    row_id = db.put(project="proj", title="perm.md", content="forever", ttl=None)

    mock_t3 = MagicMock()
    mock_t3.put.return_value = "def456"

    with patch("nexus.commands.memory.T2Database", return_value=db):
        with patch("nexus.commands.memory.t3_knowledge", return_value=mock_t3):
            runner.invoke(
                main,
                ["memory", "promote", str(row_id), "--collection", "knowledge__proj"],
            )

    call_kwargs = mock_t3.put.call_args.kwargs
    assert call_kwargs["ttl_days"] == 0


def test_promote_cmd_remove_deletes_t2_entry(runner: CliRunner, mem_home: Path, db: T2Database) -> None:
    """--remove flag deletes the T2 entry after promoting."""
    row_id = db.put(project="proj", title="tmp.md", content="temp data", ttl=5)

    mock_t3 = MagicMock()
    mock_t3.put.return_value = "ghi789"

    _t2_cm = MagicMock(__enter__=MagicMock(return_value=db))
    with patch("nexus.commands.memory.T2Database", return_value=_t2_cm):
        with patch("nexus.commands.memory.t3_knowledge", return_value=mock_t3):
            result = runner.invoke(
                main,
                ["memory", "promote", str(row_id), "--collection", "knowledge__proj", "--remove"],
            )

    assert result.exit_code == 0, result.output
    assert db.get(project="proj", title="tmp.md") is None
    assert "removed" in result.output.lower()


# ── nexus-huj: promote_cmd expires_at computed from T2 timestamp ───────────────

def test_promote_cmd_expires_at_derived_from_t2_timestamp(
    runner: CliRunner, mem_home: Path, db: T2Database
) -> None:
    """promote passes expires_at computed from the T2 entry's timestamp, not now()."""
    from datetime import UTC, datetime, timedelta

    row_id = db.put(project="proj", title="dated.md", content="content", ttl=10)

    # Backdate the timestamp so we can confirm expires_at is T2-based, not now-based
    past = (datetime.now(UTC) - timedelta(days=5)).strftime("%Y-%m-%dT%H:%M:%SZ")
    db.conn.execute("UPDATE memory SET timestamp=? WHERE id=?", (past, row_id))
    db.conn.commit()

    mock_t3 = MagicMock()
    mock_t3.put.return_value = "xyz000"

    with patch("nexus.commands.memory.T2Database", return_value=db):
        with patch("nexus.commands.memory.t3_knowledge", return_value=mock_t3):
            result = runner.invoke(
                main,
                ["memory", "promote", str(row_id), "--collection", "knowledge__proj"],
            )

    assert result.exit_code == 0, result.output
    call_kwargs = mock_t3.put.call_args.kwargs

    # expires_at must be present and non-empty for a TTL entry
    assert call_kwargs.get("expires_at", "") != "", "expires_at must be set for TTL entry"

    # The value should be base_ts + 10 days; verify it is in the past relative to now+6d
    # (since base_ts is 5 days ago and ttl is 10 days, expires_at is ~5 days from now)
    expires = datetime.fromisoformat(call_kwargs["expires_at"])
    now = datetime.now(UTC)
    assert expires > now, "expires_at should be in the future (5 days out)"
    assert expires < now + timedelta(days=7), "expires_at should not be 10 days from now"


def test_promote_cmd_permanent_expires_at_is_empty(
    runner: CliRunner, mem_home: Path, db: T2Database
) -> None:
    """Permanent T2 entry (ttl=None) → expires_at='' passed to t3.put()."""
    row_id = db.put(project="proj", title="perm2.md", content="forever", ttl=None)

    mock_t3 = MagicMock()
    mock_t3.put.return_value = "perm-id"

    with patch("nexus.commands.memory.T2Database", return_value=db):
        with patch("nexus.commands.memory.t3_knowledge", return_value=mock_t3):
            runner.invoke(
                main,
                ["memory", "promote", str(row_id), "--collection", "knowledge__proj"],
            )

    call_kwargs = mock_t3.put.call_args.kwargs
    assert call_kwargs.get("expires_at", "MISSING") == "", "permanent entry must have expires_at=''"


# ── nexus-pjsc.6: promote_cmd routes to t3_knowledge() ───────────────────────

def test_promote_cmd_routes_to_knowledge_store(
    runner: CliRunner, mem_home: Path, db: T2Database
) -> None:
    """P2: promote_cmd uses t3_knowledge() not T3Database constructor directly."""
    row_id = db.put(project="proj", title="doc.md", content="the content", ttl=7)

    mock_t3 = MagicMock()
    mock_t3.put.return_value = "promoted-id"

    with patch("nexus.commands.memory.T2Database", return_value=db):
        with patch("nexus.commands.memory.t3_knowledge", return_value=mock_t3):
            result = runner.invoke(
                main,
                ["memory", "promote", str(row_id), "--collection", "knowledge__proj"],
            )

    assert result.exit_code == 0, result.output
    mock_t3.put.assert_called_once()
    assert "promoted-id" in result.output


# ── nexus-28b: t2._read_session_id delegates to session.read_session_id ──────

def test_t2_uses_session_module_for_session_id(db: T2Database) -> None:
    """T2Database.put uses nexus.session.read_session_id, not a local re-implementation."""
    import nexus.db.t2 as t2_mod
    import nexus.session as sess_mod

    # Verify the module-level alias points to the same function as session.read_session_id
    assert t2_mod._read_session_id is sess_mod.read_session_id

    # Functional: patch the name in t2's namespace (since it's a bound import)
    with patch("nexus.db.t2._read_session_id", return_value="test-sid-xyz"):
        row_id = db.put(project="p", title="t.md", content="x")

    row = db.get(id=row_id)
    assert row["session"] == "test-sid-xyz"
