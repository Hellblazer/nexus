# SPDX-License-Identifier: AGPL-3.0-or-later
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from click.testing import CliRunner

from nexus.cli import main
from nexus.db.t2 import T2Database
from tests._t2_fixture_ops import backdate_memory, memory_row


# ── T2 database layer ───────────────────────────────────────────────────────


def test_memory_put_upsert(db: T2Database) -> None:
    db.put(project="proj", title="file.md", content="first")
    db.put(project="proj", title="file.md", content="updated")
    rows = [r for r in db.memory.get_all("proj") if r["title"] == "file.md"]
    assert len(rows) == 1, (
        f"put must UPSERT on (project, title), not append; got {len(rows)} rows"
    )
    assert rows[0]["content"] == "updated"


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


# ── resolve_title: exact-then-prefix fallback (nexus-e59o) ─────────────────


def test_resolve_title_exact_match_wins(db: T2Database) -> None:
    """Exact (project, title) match returns the entry with no candidates."""
    db.put(project="p", title="088-research-1", content="short")
    db.put(project="p", title="088-research-1: full-suffix", content="long")
    entry, candidates = db.resolve_title(project="p", title="088-research-1")
    # Exact match wins even though a prefix-only collision exists.
    assert entry is not None and entry["content"] == "short"
    assert candidates == []


def test_resolve_title_unique_prefix_match(db: T2Database) -> None:
    """No exact match + exactly one prefix candidate returns that candidate."""
    db.put(
        project="p", title="088-research-1: RDR-092 baseline", content="body",
    )
    entry, candidates = db.resolve_title(project="p", title="088-research-1")
    assert entry is not None
    assert entry["title"] == "088-research-1: RDR-092 baseline"
    assert candidates == []


def test_resolve_title_ambiguous_prefix_returns_candidates(
    db: T2Database,
) -> None:
    """Multiple prefix matches returns (None, [candidates]) for UX surfacing."""
    db.put(project="p", title="088-research-1: first", content="a")
    db.put(project="p", title="088-research-1b: other", content="b")
    entry, candidates = db.resolve_title(project="p", title="088-research-1")
    assert entry is None
    titles = sorted(c["title"] for c in candidates)
    assert titles == ["088-research-1: first", "088-research-1b: other"]


def test_resolve_title_no_match_returns_empty(db: T2Database) -> None:
    """Nothing matches returns (None, [])."""
    entry, candidates = db.resolve_title(project="p", title="no-such-prefix")
    assert entry is None
    assert candidates == []


def test_resolve_title_escapes_like_wildcards(db: T2Database) -> None:
    """A literal '%' or '_' in the title prefix must not become a wildcard."""
    db.put(project="p", title="a_b_c", content="underscore-literal")
    db.put(project="p", title="axb", content="not-a-match-for-underscore")
    entry, candidates = db.resolve_title(project="p", title="a_")
    # Only the literal 'a_' prefix matches 'a_b_c', not 'axb'.
    assert entry is not None
    assert entry["title"] == "a_b_c"
    assert candidates == []


def test_resolve_title_scoped_to_project(db: T2Database) -> None:
    """Prefix fallback honours the project boundary."""
    db.put(project="p1", title="088-research-1: in-p1", content="one")
    db.put(project="p2", title="088-research-1: in-p2", content="two")
    entry, candidates = db.resolve_title(project="p1", title="088-research-1")
    assert entry is not None
    assert entry["project"] == "p1"
    assert candidates == []


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
    backdate_memory(db, "proj", "old.md", days=2)
    assert db.expire() == 1
    assert memory_row(db, "proj", "old.md") is None


def test_memory_expire_permanent_not_deleted(db: T2Database) -> None:
    db.put(project="proj", title="perm.md", content="keep forever", ttl=None)
    backdate_memory(db, "proj", "perm.md", days=365)
    db.expire()
    assert memory_row(db, "proj", "perm.md") is not None, (
        "ttl=None means permanent; expire() must not delete it at any age"
    )


def test_memory_list_by_project(db: T2Database) -> None:
    db.put(project="proj_a", title="x.md", content="x")
    db.put(project="proj_a", title="y.md", content="y")
    db.put(project="proj_b", title="z.md", content="z")
    assert {e["title"] for e in db.list_entries(project="proj_a")} == {"x.md", "y.md"}


# ── query grammar: plain text, and the empty-tsquery gap ─────────────────────


def test_stopword_only_query_raises_instead_of_silent_empty(
    db: T2Database,
) -> None:
    """A query of only stopwords raises, rather than returning [] silently.

    nexus-senub, FIXED 2026-08-08. The bead was filed as "malformed query
    silently returns 0 rows where SQLite raised". That mechanism died with
    SQLite: FTS5 treated AND/NOT as OPERATORS (bare = syntax error), while the
    engine uses PostgreSQL plainto_tsquery, whose whole purpose is accepting
    arbitrary text WITHOUT a syntax error. There is no malformed-query class.

    The REAL, narrower gap: plainto_tsquery strips English stopwords, so a
    query made ENTIRELY of them reduces to an EMPTY tsquery on the 'english'
    leg — the only leg that reaches ``content`` — which matches nothing not
    "nothing relevant", but nothing, unconditionally. Proven here by seeding a
    row that literally contains the word and showing it is still unreachable
    by content search.

    The engine (``MemoryRepository.guardAgainstDegenerateQuery``) now detects
    this AFTER a search comes back empty and returns a 400 naming the query,
    rather than 200-with-``[]``; ``HttpMemoryStore.search`` converts that 400
    to a ``ValueError`` matching the retired SQLite arm's contract in spirit.
    A query that resolves a REAL hit through the 'simple' title/tag leg still
    returns normally — the guard only replaces an AMBIGUOUS ``[]`` with a
    named reason, it never discards a result (covered at the Java level by
    ``MemoryRepositoryTest.search_stopwordQuery_stillFindsRealTitleHit_doesNotRaise``).
    """
    db.put(project="proj_rdr", title="operators.md", content="clause and clause")

    with pytest.raises(ValueError, match="and"):
        db.search("and")

    # NON-VACUITY: the search path itself works — a non-stopword term in the
    # same row matches. So the raise above is the stopword reduction, not a
    # broken fixture or a dead search.
    hits = db.search("clause")
    assert {row["title"] for row in hits} == {"operators.md"}, (
        "the control term must match, or this test proves nothing about "
        "stopwords"
    )


# ── T2 session delegation ───────────────────────────────────────────────────


def test_t2_uses_session_module_for_session_id(db: T2Database) -> None:
    """``put`` resolves an unset session through the session module.

    nexus-aqbrk: this used to patch a module-local alias
    (``memory_store._read_session_id``) and assert its identity against
    ``nexus.session.read_claude_session_id``. The fallback chain now has ONE
    owner shared by both the SQLite and service stores
    (``nexus.db.t2._attribution.resolve_attribution``), so that alias no
    longer exists — and patching a name nothing calls is worse than not
    patching at all: the test would have gone green against the REAL
    resolver, which returns ``None`` under the suite's isolated config dir,
    and the assertion would have been vacuous rather than failing.

    Patching the canonical function is also strictly better than the alias
    form it replaces: it no longer depends on an import-binding detail that
    was itself a documented footgun (``from X import Y as Z`` captures the
    object at import time, so patching the source module would NOT have
    propagated).
    """
    with patch("nexus.session.read_claude_session_id", return_value="test-sid-xyz"):
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
        patch("nexus.commands.memory.t2_handle", return_value=t2),
        patch("nexus.config.get_credential", return_value="fake-key"),
        patch("nexus.config.is_local_mode", return_value=False),
        patch("nexus.db.make_t3", return_value=mt3),
    ):
        result = runner.invoke(main, args)
    return result, mt3


# ── Promote tests ────────────────────────────────────────────────────────────


# test_promote_no_credentials removed (nexus-c7aj3): the promote-path
# credential pre-flight is deleted; the no-creds success scenario is pinned
# in tests/test_c7aj3_service_mode_cred_gates.py.


def test_promote_entry_not_found(runner: CliRunner, mem_home: Path, db: T2Database) -> None:
    with patch("nexus.commands.memory.t2_handle", return_value=db):
        result = runner.invoke(main, ["memory", "promote", "9999", "--collection", "knowledge__p"])
    assert result.exit_code != 0
    assert "not found" in result.output.lower() or "9999" in result.output


def test_promote_calls_t3_put(runner: CliRunner, mem_home: Path, db: T2Database) -> None:
    row_id = db.put(project="proj", title="doc.md", content="the content", ttl=7, tags="ai")
    result, mt3 = _promote(runner, db, row_id)
    assert result.exit_code == 0, result.output
    mt3.put.assert_called_once()
    kw = mt3.put.call_args.kwargs
    # RDR-103 Phase 5: ``t3_collection_name`` auto-promotes
    # ``--collection knowledge__proj`` to a conformant 4-segment name.
    assert (kw["collection"], kw["content"], kw["title"], kw["ttl_days"]) == (
        "knowledge__proj__voyage-context-3__v1", "the content", "doc.md", 7
    )
    assert "abc123" in result.output


def test_promote_permanent_entry(runner: CliRunner, mem_home: Path, db: T2Database) -> None:
    row_id = db.put(project="proj", title="perm.md", content="forever", ttl=None)
    _, mt3 = _promote(runner, db, row_id)
    kw = mt3.put.call_args.kwargs
    assert kw["ttl_days"] == 0
    # nexus-v4paa fold: neither real T3 substrate accepts expires_at —
    # promote must not pass it (it was a TypeError, mock-shielded here).
    assert "expires_at" not in kw


def test_promote_remove_deletes_t2(runner: CliRunner, mem_home: Path, db: T2Database) -> None:
    row_id = db.put(project="proj", title="tmp.md", content="temp data", ttl=5)
    result, _ = _promote(runner, db, row_id, extra=["--remove"], use_cm=True)
    assert result.exit_code == 0, result.output
    assert db.get(project="proj", title="tmp.md") is None
    assert "removed" in result.output.lower()


# test_promote_missing_database removed (nexus-c7aj3): same as above.


def test_promote_honours_remaining_ttl(
    runner: CliRunner, mem_home: Path, db: T2Database,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """nexus-v4paa fold: the remaining TTL is expressed by SHRINKING
    ttl_days (the substrates compute expiry as indexed_at + ttl_days and
    accept no expires_at kwarg — the old pass-through was a TypeError
    against both real substrates, mock-shielded by this suite).

    The clock is shifted forward (fixed-clock rule) instead of
    backdating the row via raw SQLite, so this holds on the engine
    substrate too (Http stores expose no ``.conn``)."""
    row_id = db.put(project="proj", title="dated.md", content="content", ttl=10)

    class _FiveDaysLater:
        @staticmethod
        def now(tz=None):
            return datetime.now(tz) + timedelta(days=5)

        @staticmethod
        def fromisoformat(s):
            return datetime.fromisoformat(s)

    monkeypatch.setattr("nexus.commands.memory.datetime", _FiveDaysLater)
    result, mt3 = _promote(runner, db, row_id)
    assert result.exit_code == 0, result.output
    kw = mt3.put.call_args.kwargs
    assert "expires_at" not in kw
    assert kw["ttl_days"] == 5, (
        "5 of the 10 TTL days elapsed in T2 — the promoted entry gets "
        "the remaining 5, not a reset 10"
    )


# ── Delete CLI command ───────────────────────────────────────────────────────


def test_delete_cmd_by_project_title(runner: CliRunner, mem_home: Path, db: T2Database) -> None:
    db.put(project="proj", title="note.md", content="content to delete")
    with patch("nexus.commands.memory.t2_handle", return_value=_t2_cm(db)):
        result = runner.invoke(main, ["memory", "delete", "--project", "proj", "--title", "note.md", "--yes"])
    assert result.exit_code == 0 and "Deleted" in result.output
    assert db.get(project="proj", title="note.md") is None


def test_delete_cmd_by_id(runner: CliRunner, mem_home: Path, db: T2Database) -> None:
    row_id = db.put(project="proj", title="note.md", content="delete by id content")
    with patch("nexus.commands.memory.t2_handle", return_value=_t2_cm(db)):
        result = runner.invoke(main, ["memory", "delete", "--id", str(row_id), "--yes"])
    assert result.exit_code == 0 and "proj/note.md" in result.output
    assert db.get(id=row_id) is None


def test_delete_cmd_all(runner: CliRunner, mem_home: Path, db: T2Database) -> None:
    db.put(project="proj", title="a.md", content="a")
    db.put(project="proj", title="b.md", content="b")
    db.put(project="other", title="c.md", content="c")
    with patch("nexus.commands.memory.t2_handle", return_value=_t2_cm(db)):
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
    with patch("nexus.commands.memory.t2_handle", return_value=_t2_cm(db)):
        result = runner.invoke(main, ["memory", "delete", "--project", "no", "--title", "such.md", "--yes"])
    assert result.exit_code != 0 and "not found" in result.output


# ── Search CLI command: nexus-senub degenerate-query error path ────────────


def test_search_cmd_stopwordOnlyQuery_exitsNonZeroWithCleanMessage(
    runner: CliRunner, mem_home: Path, db: T2Database,
) -> None:
    """A stopword-only query must exit non-zero with the engine's message,
    not crash with an uncaught traceback.

    nexus-senub: HttpMemoryStore.search raises ValueError on the engine's 400
    for a degenerate query; the CLI command must catch it and re-raise as a
    click.ClickException (clean "Error: ..." message, exit code 1) rather
    than letting the ValueError propagate as a bare traceback.
    """
    db.put(project="proj_rdr", title="operators.md", content="clause and clause")
    with patch("nexus.commands.memory.t2_handle", return_value=_t2_cm(db)):
        result = runner.invoke(main, ["memory", "search", "and"])
    assert result.exit_code != 0, (
        "a stopword-only query must fail loudly, not exit 0 with 'No results found.'"
    )
    assert result.exception is None or isinstance(result.exception, SystemExit), (
        "must be a clean click error, not an uncaught traceback: "
        f"{result.exception!r}"
    )
    assert "and" in result.output


def test_search_cmd_realQuery_stillWorks(
    runner: CliRunner, mem_home: Path, db: T2Database,
) -> None:
    # NON-VACUITY companion: a normal query still exits 0 with a result line.
    db.put(project="proj_rdr", title="operators.md", content="clause and clause")
    with patch("nexus.commands.memory.t2_handle", return_value=_t2_cm(db)):
        result = runner.invoke(main, ["memory", "search", "clause"])
    assert result.exit_code == 0
    assert "operators.md" in result.output


# ── Get CLI command: prefix-match UX (nexus-e59o) ───────────────────────────


def test_get_cmd_exact_title_match(
    runner: CliRunner, mem_home: Path, db: T2Database,
) -> None:
    """Exact title match returns the content (regression guard — baseline)."""
    db.put(project="p", title="note.md", content="content-body-sentinel")
    with patch("nexus.commands.memory.t2_handle", return_value=_t2_cm(db)):
        result = runner.invoke(
            main, ["memory", "get", "--project", "p", "--title", "note.md"],
        )
    assert result.exit_code == 0
    assert "content-body-sentinel" in result.output


def test_get_cmd_unique_prefix_match_resolves(
    runner: CliRunner, mem_home: Path, db: T2Database,
) -> None:
    """Short-form --title resolves to the unique full title (e59o main case)."""
    db.put(
        project="nexus_rdr",
        title="088-research-1: RDR-092 baseline for Gap 4 spike",
        content="baseline-body-xyz",
    )
    with patch("nexus.commands.memory.t2_handle", return_value=_t2_cm(db)):
        result = runner.invoke(
            main, ["memory", "get", "--project", "nexus_rdr",
                   "--title", "088-research-1"],
        )
    assert result.exit_code == 0
    assert "baseline-body-xyz" in result.output


def test_get_cmd_ambiguous_prefix_lists_candidates(
    runner: CliRunner, mem_home: Path, db: T2Database,
) -> None:
    """Multiple prefix matches must list candidates and fail loud (not silent-pick)."""
    db.put(project="p", title="088-research-1: first", content="one")
    db.put(project="p", title="088-research-1b: second", content="two")
    with patch("nexus.commands.memory.t2_handle", return_value=_t2_cm(db)):
        result = runner.invoke(
            main, ["memory", "get", "--project", "p",
                   "--title", "088-research-1"],
        )
    assert result.exit_code != 0
    out = result.output
    assert "Ambiguous" in out
    assert "088-research-1: first" in out
    assert "088-research-1b: second" in out


def test_get_cmd_no_match_still_fails_loud(
    runner: CliRunner, mem_home: Path, db: T2Database,
) -> None:
    """Zero-match continues to fail — behaviour preserved from baseline."""
    with patch("nexus.commands.memory.t2_handle", return_value=_t2_cm(db)):
        result = runner.invoke(
            main, ["memory", "get", "--project", "none", "--title", "missing"],
        )
    assert result.exit_code != 0
    assert "not found" in result.output


def test_get_cmd_exact_wins_over_prefix(
    runner: CliRunner, mem_home: Path, db: T2Database,
) -> None:
    """When an exact entry exists alongside a longer-title match, exact wins."""
    db.put(project="p", title="088-research-1", content="short-entry")
    db.put(project="p", title="088-research-1: full-suffix", content="long-entry")
    with patch("nexus.commands.memory.t2_handle", return_value=_t2_cm(db)):
        result = runner.invoke(
            main, ["memory", "get", "--project", "p",
                   "--title", "088-research-1"],
        )
    assert result.exit_code == 0
    assert "short-entry" in result.output
    assert "long-entry" not in result.output
