# SPDX-License-Identifier: AGPL-3.0-or-later
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from click.testing import CliRunner

from nexus.cli import main
from nexus.db.t2 import T2Database


# ── T2 database layer ───────────────────────────────────────────────────────


def test_memory_put_upsert(db: T2Database) -> None:
    db.put(project="proj", title="file.md", content="first")
    db.put(project="proj", title="file.md", content="updated")
    row = db.memory.conn.execute(
        "SELECT COUNT(*), MAX(content) FROM memory WHERE project='proj' AND title='file.md'"
    ).fetchone()
    assert row == (1, "updated")


def test_memory_get_by_project_title(db: T2Database) -> None:
    db.put(project="proj_a", title="notes.md", content="hello world")
    result = db.get(project="proj_a", title="notes.md")
    assert result is not None
    assert (result["content"], result["project"], result["title"]) == (
        "hello world", "proj_a", "notes.md"
    )


def test_memory_get_by_id(db: T2Database) -> None:
    row_id = db.put(project="p", title="x.md", content="by id")
    assert db.get(id=row_id)["content"] == "by id"


def test_memory_get_missing_returns_none(db: T2Database) -> None:
    assert db.get(project="no", title="such.md") is None


def test_memory_search_fts5(db: T2Database) -> None:
    db.put(project="p", title="alpha.md", content="The quick brown fox")
    db.put(project="p", title="beta.md", content="A lazy dog sleeping")
    db.put(project="p", title="gamma.md", content="The quick fox jumps high")
    assert {r["title"] for r in db.search("quick fox")} == {"alpha.md", "gamma.md"}


def test_memory_search_scoped_to_project(db: T2Database) -> None:
    db.put(project="proj_a", title="a.md", content="authentication token")
    db.put(project="proj_b", title="b.md", content="authentication token")
    results = db.search("authentication", project="proj_a")
    assert len(results) == 1 and results[0]["project"] == "proj_a"


def test_memory_expire_ttl(db: T2Database) -> None:
    db.put(project="proj", title="old.md", content="stale", ttl=1)
    past = (datetime.now(UTC) - timedelta(days=2)).strftime("%Y-%m-%dT%H:%M:%SZ")
    db.memory.conn.execute("UPDATE memory SET timestamp=? WHERE title='old.md'", (past,))
    db.memory.conn.commit()
    assert db.expire() == 1
    assert db.memory.conn.execute("SELECT COUNT(*) FROM memory WHERE title='old.md'").fetchone()[0] == 0


def test_memory_expire_permanent_not_deleted(db: T2Database) -> None:
    db.put(project="proj", title="perm.md", content="keep forever", ttl=None)
    past = (datetime.now(UTC) - timedelta(days=365)).strftime("%Y-%m-%dT%H:%M:%SZ")
    db.memory.conn.execute("UPDATE memory SET timestamp=? WHERE title='perm.md'", (past,))
    db.memory.conn.commit()
    db.expire()
    assert db.memory.conn.execute("SELECT COUNT(*) FROM memory WHERE title='perm.md'").fetchone()[0] == 1


def test_memory_list_by_project(db: T2Database) -> None:
    db.put(project="proj_a", title="x.md", content="x")
    db.put(project="proj_a", title="y.md", content="y")
    db.put(project="proj_b", title="z.md", content="z")
    assert {e["title"] for e in db.list_entries(project="proj_a")} == {"x.md", "y.md"}


# ── FTS5 safety ──────────────────────────────────────────────────────────────


@pytest.mark.parametrize("method,args", [
    ("search", ("AND",)),
    ("search_glob", ("NOT", "*_rdr")),
])
def test_malformed_fts5_query_raises_valueerror(db: T2Database, method: str, args: tuple) -> None:
    db.put(project="proj_rdr", title="doc.md", content="some content")
    with pytest.raises(ValueError, match="Invalid search query"):
        getattr(db, method)(*args)


# ── T2 session delegation ───────────────────────────────────────────────────


def test_t2_uses_session_module_for_session_id(db: T2Database) -> None:
    # After RDR-063 Phase 1 step 2, memory-domain methods (including put())
    # live in nexus.db.t2.memory_store, so the session-id import binding
    # moved with them. Patch the new location to verify the wiring.
    import nexus.db.t2.memory_store as mem_mod
    import nexus.session as sess_mod
    assert mem_mod._read_session_id is sess_mod.read_session_id
    with patch("nexus.db.t2.memory_store._read_session_id", return_value="test-sid-xyz"):
        row_id = db.put(project="p", title="t.md", content="x")
    assert db.get(id=row_id)["session"] == "test-sid-xyz"


# ── T2 delete ────────────────────────────────────────────────────────────────


def test_memory_delete_by_project_title(db: T2Database) -> None:
    db.put(project="p", title="a.md", content="hello")
    assert db.delete(project="p", title="a.md") is True
    assert db.get(project="p", title="a.md") is None


def test_memory_delete_by_id(db: T2Database) -> None:
    row_id = db.put(project="p", title="b.md", content="world")
    assert db.delete(id=row_id) is True
    assert db.get(id=row_id) is None


@pytest.mark.parametrize("kwargs", [
    {"project": "no", "title": "such.md"},
    {"id": 99999},
])
def test_memory_delete_missing_returns_false(db: T2Database, kwargs: dict) -> None:
    assert db.delete(**kwargs) is False


def test_memory_delete_invalid_args_raises(db: T2Database) -> None:
    with pytest.raises(ValueError):
        db.delete(project="p")


def test_memory_delete_fts5_not_searchable(db: T2Database) -> None:
    db.put(project="p", title="c.md", content="unique canary token xyzzy")
    db.delete(project="p", title="c.md")
    assert not db.search("canary xyzzy")


# ── Promote command helpers ──────────────────────────────────────────────────


@pytest.fixture
def runner() -> CliRunner:
    return CliRunner()


@pytest.fixture
def mem_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("HOME", str(tmp_path))
    return tmp_path


def _mock_t3(put_return: str = "abc123") -> MagicMock:
    m = MagicMock()
    m.__enter__ = MagicMock(return_value=m)
    m.__exit__ = MagicMock(return_value=False)
    m.put.return_value = put_return
    return m


def _t2_cm(db: T2Database) -> MagicMock:
    return MagicMock(__enter__=MagicMock(return_value=db), __exit__=MagicMock(return_value=False))


def _promote(runner, db, row_id, col="knowledge__proj", extra=None, use_cm=False):
    mt3 = _mock_t3()
    t2 = _t2_cm(db) if use_cm else db
    args = ["memory", "promote", str(row_id), "--collection", col, *(extra or [])]
    with (
        patch("nexus.commands.memory.T2Database", return_value=t2),
        patch("nexus.commands.memory.get_credential", return_value="fake-key"),
        patch("nexus.config.is_local_mode", return_value=False),
        patch("nexus.db.make_t3", return_value=mt3),
    ):
        result = runner.invoke(main, args)
    return result, mt3


# ── Promote tests ────────────────────────────────────────────────────────────


def test_promote_no_credentials(runner: CliRunner, mem_home: Path, db: T2Database) -> None:
    row_id = db.put(project="p", title="note.md", content="hello", ttl=30)
    with (
        patch("nexus.commands.memory.T2Database", return_value=db),
        patch("nexus.commands.memory.get_credential", return_value=""),
        patch("nexus.config.is_local_mode", return_value=False),
    ):
        result = runner.invoke(main, ["memory", "promote", str(row_id), "--collection", "knowledge__p"])
    assert result.exit_code != 0
    assert "not set" in result.output.lower() or "config init" in result.output.lower()


def test_promote_entry_not_found(runner: CliRunner, mem_home: Path, db: T2Database) -> None:
    with patch("nexus.commands.memory.T2Database", return_value=db):
        result = runner.invoke(main, ["memory", "promote", "9999", "--collection", "knowledge__p"])
    assert result.exit_code != 0
    assert "not found" in result.output.lower() or "9999" in result.output


def test_promote_calls_t3_put(runner: CliRunner, mem_home: Path, db: T2Database) -> None:
    row_id = db.put(project="proj", title="doc.md", content="the content", ttl=7, tags="ai")
    result, mt3 = _promote(runner, db, row_id)
    assert result.exit_code == 0, result.output
    mt3.put.assert_called_once()
    kw = mt3.put.call_args.kwargs
    assert (kw["collection"], kw["content"], kw["title"], kw["ttl_days"]) == (
        "knowledge__proj", "the content", "doc.md", 7
    )
    assert "abc123" in result.output


def test_promote_permanent_entry(runner: CliRunner, mem_home: Path, db: T2Database) -> None:
    row_id = db.put(project="proj", title="perm.md", content="forever", ttl=None)
    _, mt3 = _promote(runner, db, row_id)
    kw = mt3.put.call_args.kwargs
    assert kw["ttl_days"] == 0
    assert kw.get("expires_at", "MISSING") == ""


def test_promote_remove_deletes_t2(runner: CliRunner, mem_home: Path, db: T2Database) -> None:
    row_id = db.put(project="proj", title="tmp.md", content="temp data", ttl=5)
    result, _ = _promote(runner, db, row_id, extra=["--remove"], use_cm=True)
    assert result.exit_code == 0, result.output
    assert db.get(project="proj", title="tmp.md") is None
    assert "removed" in result.output.lower()


def test_promote_missing_database(runner: CliRunner, mem_home: Path, db: T2Database) -> None:
    row_id = db.put(project="p", title="note.md", content="hello", ttl=30)
    cred = lambda key: "" if key == "chroma_database" else "fake-value"  # noqa: E731
    with (
        patch("nexus.commands.memory.T2Database", return_value=db),
        patch("nexus.commands.memory.get_credential", side_effect=cred),
        patch("nexus.config.is_local_mode", return_value=False),
    ):
        result = runner.invoke(main, ["memory", "promote", str(row_id), "--collection", "knowledge__p"])
    assert result.exit_code != 0
    assert "chroma_database" in result.output and "not set" in result.output.lower()


def test_promote_expires_at_from_t2_timestamp(runner: CliRunner, mem_home: Path, db: T2Database) -> None:
    row_id = db.put(project="proj", title="dated.md", content="content", ttl=10)
    past = (datetime.now(UTC) - timedelta(days=5)).strftime("%Y-%m-%dT%H:%M:%SZ")
    db.memory.conn.execute("UPDATE memory SET timestamp=? WHERE id=?", (past, row_id))
    db.memory.conn.commit()
    result, mt3 = _promote(runner, db, row_id)
    assert result.exit_code == 0, result.output
    kw = mt3.put.call_args.kwargs
    assert kw.get("expires_at", "") != "", "expires_at must be set for TTL entry"
    expires = datetime.fromisoformat(kw["expires_at"])
    now = datetime.now(UTC)
    assert now < expires < now + timedelta(days=7)


# ── Delete CLI command ───────────────────────────────────────────────────────


def test_delete_cmd_by_project_title(runner: CliRunner, mem_home: Path, db: T2Database) -> None:
    db.put(project="proj", title="note.md", content="content to delete")
    with patch("nexus.commands.memory.T2Database", return_value=_t2_cm(db)):
        result = runner.invoke(main, ["memory", "delete", "--project", "proj", "--title", "note.md", "--yes"])
    assert result.exit_code == 0 and "Deleted" in result.output
    assert db.get(project="proj", title="note.md") is None


def test_delete_cmd_by_id(runner: CliRunner, mem_home: Path, db: T2Database) -> None:
    row_id = db.put(project="proj", title="note.md", content="delete by id content")
    with patch("nexus.commands.memory.T2Database", return_value=_t2_cm(db)):
        result = runner.invoke(main, ["memory", "delete", "--id", str(row_id), "--yes"])
    assert result.exit_code == 0 and "proj/note.md" in result.output
    assert db.get(id=row_id) is None


def test_delete_cmd_all(runner: CliRunner, mem_home: Path, db: T2Database) -> None:
    db.put(project="proj", title="a.md", content="a")
    db.put(project="proj", title="b.md", content="b")
    db.put(project="other", title="c.md", content="c")
    with patch("nexus.commands.memory.T2Database", return_value=_t2_cm(db)):
        result = runner.invoke(main, ["memory", "delete", "--project", "proj", "--all", "--yes"])
    assert result.exit_code == 0 and "Deleted 2" in result.output
    assert db.list_entries(project="proj") == []
    assert db.list_entries(project="other") != []


@pytest.mark.parametrize("args,expected_msg", [
    (["memory", "delete", "--all", "--yes"], "--all requires --project"),
    (["memory", "delete", "--id", "1", "--project", "p"], "mutually exclusive"),
])
def test_delete_cmd_rejected(runner: CliRunner, mem_home: Path, args: list, expected_msg: str) -> None:
    result = runner.invoke(main, args)
    assert result.exit_code != 0 and expected_msg in result.output


def test_delete_cmd_not_found(runner: CliRunner, mem_home: Path, db: T2Database) -> None:
    with patch("nexus.commands.memory.T2Database", return_value=_t2_cm(db)):
        result = runner.invoke(main, ["memory", "delete", "--project", "no", "--title", "such.md", "--yes"])
    assert result.exit_code != 0 and "not found" in result.output
