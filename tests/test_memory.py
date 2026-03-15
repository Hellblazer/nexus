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


def test_search_raises_on_malformed_fts5_query(db: T2Database) -> None:
    """FTS5 queries with bare operators (AND, OR alone) raise ValueError, not OperationalError."""
    import pytest
    db.put(project="proj", title="doc.md", content="some content")

    with pytest.raises(ValueError, match="Invalid search query"):
        db.search("AND")


def test_search_glob_raises_on_malformed_fts5_query(db: T2Database) -> None:
    """search_glob with bare FTS5 operator raises ValueError."""
    import pytest
    db.put(project="proj_rdr", title="doc.md", content="some content")

    with pytest.raises(ValueError, match="Invalid search query"):
        db.search_glob("NOT", "*_rdr")



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
    """promote fails with helpful message when T3 credentials are absent."""
    row_id = db.put(project="p", title="note.md", content="hello", ttl=30)

    with patch("nexus.commands.memory.T2Database", return_value=db):
        with patch("nexus.commands.memory.get_credential", return_value=""):
            with patch("nexus.config.is_local_mode", return_value=False):
                result = runner.invoke(
                    main, ["memory", "promote", str(row_id), "--collection", "knowledge__p"]
                )

    assert result.exit_code != 0
    assert "not set" in result.output.lower() or "config init" in result.output.lower()


def test_promote_cmd_entry_not_found_exits(runner: CliRunner, mem_home: Path, db: T2Database) -> None:
    """promote with non-existent id exits with error."""
    with patch("nexus.commands.memory.T2Database", return_value=db):
        result = runner.invoke(
            main, ["memory", "promote", "9999", "--collection", "knowledge__p"]
        )

    assert result.exit_code != 0
    assert "not found" in result.output.lower() or "9999" in result.output


def test_promote_cmd_calls_t3_put(runner: CliRunner, mem_home: Path, db: T2Database) -> None:
    """promote with valid id + credentials calls t3.put and echoes doc_id."""
    row_id = db.put(project="proj", title="doc.md", content="the content", ttl=7, tags="ai")

    mock_t3 = MagicMock()
    mock_t3.put.return_value = "abc123"
    # Wire up context manager so `with T3Database(...) as t3:` yields mock_t3.
    mock_t3.__enter__ = MagicMock(return_value=mock_t3)
    mock_t3.__exit__ = MagicMock(return_value=False)

    with patch("nexus.commands.memory.T2Database", return_value=db):
        with patch("nexus.commands.memory.get_credential", return_value="fake-key"):
            with patch("nexus.config.is_local_mode", return_value=False):
              with patch("nexus.db.make_t3", return_value=mock_t3):
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
    mock_t3.__enter__ = MagicMock(return_value=mock_t3)
    mock_t3.__exit__ = MagicMock(return_value=False)
    mock_t3.put.return_value = "def456"

    with patch("nexus.commands.memory.T2Database", return_value=db):
        with patch("nexus.commands.memory.get_credential", return_value="fake-key"):
            with patch("nexus.config.is_local_mode", return_value=False):
              with patch("nexus.db.make_t3", return_value=mock_t3):
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
    mock_t3.__enter__ = MagicMock(return_value=mock_t3)
    mock_t3.__exit__ = MagicMock(return_value=False)
    mock_t3.put.return_value = "ghi789"

    _t2_cm = MagicMock(__enter__=MagicMock(return_value=db))
    with patch("nexus.commands.memory.T2Database", return_value=_t2_cm):
        with patch("nexus.commands.memory.get_credential", return_value="fake-key"):
            with patch("nexus.config.is_local_mode", return_value=False):
              with patch("nexus.db.make_t3", return_value=mock_t3):
                result = runner.invoke(
                    main,
                    ["memory", "promote", str(row_id), "--collection", "knowledge__proj", "--remove"],
                )

    assert result.exit_code == 0, result.output
    assert db.get(project="proj", title="tmp.md") is None
    assert "removed" in result.output.lower()


# ── nexus-mox: promote_cmd credential guard ───────────────────────────────────

def test_promote_cmd_missing_database_raises(runner: CliRunner, mem_home: Path, db: T2Database) -> None:
    """promote fails when chroma_database is absent even if api keys are present."""
    row_id = db.put(project="p", title="note.md", content="hello", ttl=30)

    def cred_side_effect(key: str) -> str:
        return "" if key == "chroma_database" else "fake-value"

    with patch("nexus.commands.memory.T2Database", return_value=db):
        with patch("nexus.commands.memory.get_credential", side_effect=cred_side_effect):
            with patch("nexus.config.is_local_mode", return_value=False):
                result = runner.invoke(
                    main, ["memory", "promote", str(row_id), "--collection", "knowledge__p"]
                )

    assert result.exit_code != 0
    assert "chroma_database" in result.output
    assert "not set" in result.output.lower()


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
    mock_t3.__enter__ = MagicMock(return_value=mock_t3)
    mock_t3.__exit__ = MagicMock(return_value=False)
    mock_t3.put.return_value = "xyz000"

    with patch("nexus.commands.memory.T2Database", return_value=db):
        with patch("nexus.commands.memory.get_credential", return_value="fake-key"):
            with patch("nexus.config.is_local_mode", return_value=False):
              with patch("nexus.db.make_t3", return_value=mock_t3):
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
    mock_t3.__enter__ = MagicMock(return_value=mock_t3)
    mock_t3.__exit__ = MagicMock(return_value=False)
    mock_t3.put.return_value = "perm-id"

    with patch("nexus.commands.memory.T2Database", return_value=db):
        with patch("nexus.commands.memory.get_credential", return_value="fake-key"):
            with patch("nexus.config.is_local_mode", return_value=False):
              with patch("nexus.db.make_t3", return_value=mock_t3):
                runner.invoke(
                    main,
                    ["memory", "promote", str(row_id), "--collection", "knowledge__proj"],
                )

    call_kwargs = mock_t3.put.call_args.kwargs
    assert call_kwargs.get("expires_at", "MISSING") == "", "permanent entry must have expires_at=''"


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


# ── nexus-tjsu: nx memory delete ─────────────────────────────────────────────

def test_memory_delete_by_project_title(db: T2Database) -> None:
    """delete() by project+title removes the entry and returns True."""
    db.put(project="p", title="a.md", content="hello")
    assert db.delete(project="p", title="a.md") is True
    assert db.get(project="p", title="a.md") is None


def test_memory_delete_by_id(db: T2Database) -> None:
    """delete() by numeric id removes the entry and returns True."""
    row_id = db.put(project="p", title="b.md", content="world")
    assert db.delete(id=row_id) is True
    assert db.get(id=row_id) is None


def test_memory_delete_missing_returns_false(db: T2Database) -> None:
    """delete() on a non-existent entry returns False."""
    assert db.delete(project="no", title="such.md") is False
    assert db.delete(id=99999) is False


def test_memory_delete_invalid_args_raises(db: T2Database) -> None:
    """delete() with neither id nor project+title raises ValueError."""
    with pytest.raises(ValueError):
        db.delete(project="p")  # title missing


def test_memory_delete_fts5_not_searchable_after_delete(db: T2Database) -> None:
    """After deleting an entry, FTS5 search no longer returns it."""
    db.put(project="p", title="c.md", content="unique canary token xyzzy")
    db.delete(project="p", title="c.md")
    results = db.search("canary xyzzy")
    assert not results


def _t2_cm(db: T2Database):
    """Return a mock context manager that yields db without closing it."""
    from unittest.mock import MagicMock
    cm = MagicMock()
    cm.__enter__ = MagicMock(return_value=db)
    cm.__exit__ = MagicMock(return_value=False)
    return cm


def test_memory_delete_cmd_by_project_title(runner: CliRunner, mem_home: Path, db: T2Database) -> None:
    """delete --project/--title removes the entry."""
    db.put(project="proj", title="note.md", content="content to delete")
    with patch("nexus.commands.memory.T2Database", return_value=_t2_cm(db)):
        result = runner.invoke(main, ["memory", "delete", "--project", "proj", "--title", "note.md", "--yes"])
    assert result.exit_code == 0, result.output
    assert "Deleted" in result.output
    assert db.get(project="proj", title="note.md") is None


def test_memory_delete_cmd_by_id(runner: CliRunner, mem_home: Path, db: T2Database) -> None:
    """delete --id shows project/title in confirmation and removes the entry."""
    row_id = db.put(project="proj", title="note.md", content="delete by id content")
    with patch("nexus.commands.memory.T2Database", return_value=_t2_cm(db)):
        result = runner.invoke(main, ["memory", "delete", "--id", str(row_id), "--yes"])
    assert result.exit_code == 0, result.output
    assert "proj/note.md" in result.output
    assert db.get(id=row_id) is None


def test_memory_delete_cmd_all(runner: CliRunner, mem_home: Path, db: T2Database) -> None:
    """delete --project --all --yes removes all entries in the project."""
    db.put(project="proj", title="a.md", content="a")
    db.put(project="proj", title="b.md", content="b")
    db.put(project="other", title="c.md", content="c")
    with patch("nexus.commands.memory.T2Database", return_value=_t2_cm(db)):
        result = runner.invoke(main, ["memory", "delete", "--project", "proj", "--all", "--yes"])
    assert result.exit_code == 0, result.output
    assert "Deleted 2" in result.output
    assert db.list_entries(project="proj") == []
    assert db.list_entries(project="other") != []  # other project untouched


def test_memory_delete_cmd_all_without_project_rejected(runner: CliRunner, mem_home: Path) -> None:
    """delete --all without --project is rejected."""
    result = runner.invoke(main, ["memory", "delete", "--all", "--yes"])
    assert result.exit_code != 0
    assert "--all requires --project" in result.output


def test_memory_delete_cmd_id_with_project_rejected(runner: CliRunner, mem_home: Path) -> None:
    """delete --id combined with --project is rejected."""
    result = runner.invoke(main, ["memory", "delete", "--id", "1", "--project", "p"])
    assert result.exit_code != 0
    assert "mutually exclusive" in result.output


def test_memory_delete_cmd_not_found(runner: CliRunner, mem_home: Path, db: T2Database) -> None:
    """delete with non-existent project/title exits non-zero."""
    with patch("nexus.commands.memory.T2Database", return_value=_t2_cm(db)):
        result = runner.invoke(main, ["memory", "delete", "--project", "no", "--title", "such.md", "--yes"])
    assert result.exit_code != 0
    assert "not found" in result.output
