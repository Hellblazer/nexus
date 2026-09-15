# SPDX-License-Identifier: AGPL-3.0-or-later
"""RDR-207 Phase 2 step 2 (bead nexus-l3yuc.10): the quarantine verbs, the
delete path that quarantine breaks, and the ``memory.quarantine`` doctor row.

The CLI tests run against the engine substrate (the ``db`` fixture: a freshly
minted tenant per test, so tenant-wide ``expire`` and ``reap`` see only this
test's rows). A row is quarantined the way production does it: a TTL, a
backdated timestamp, then ``expire``. A row is marked through the real
``POST /v1/memory/summaries`` route.
"""
from __future__ import annotations

from functools import partial
from unittest.mock import MagicMock, patch

import httpx
import pytest
from click.testing import CliRunner

import nexus.health as h
from nexus import hooks, mcp_infra
from nexus.cli import main
from nexus.db.t2 import T2Database
from nexus.db.t2.http_memory_store import HttpMemoryStore, MemoryExpireResult
from tests._t2_fixture_ops import backdate_memory, canonical_chunk_id
from tests.test_taxonomy import _seed_assignment, _seed_topic


@pytest.fixture
def runner() -> CliRunner:
    return CliRunner()


def _t2_cm(db: object) -> MagicMock:
    return MagicMock(__enter__=MagicMock(return_value=db), __exit__=MagicMock(return_value=False))


def _nx(runner: CliRunner, db: object, *args: str, input: str | None = None):
    with patch("nexus.commands.memory.t2_handle", return_value=_t2_cm(db)):
        return runner.invoke(main, ["memory", *args], input=input)


def _quarantine(db: T2Database, project: str, title: str, content: str = "stale") -> int:
    row_id = db.put(project=project, title=title, content=content, ttl=1)
    backdate_memory(db, project, title, days=2)
    swept = db.memory.expire()
    assert row_id in swept.quarantined_ids, (
        f"fixture: row {row_id} must be quarantined by expire; got {swept}"
    )
    return row_id


def _mark(db: T2Database, project: str, ids: list[int]) -> int:
    return db.memory.insert_summary(
        project=project, content="summary of the quarantined rows",
        source_ids=ids, model="stub",
    )


def _quarantined_ids(db: T2Database) -> set[int]:
    return {row["id"] for row in db.memory.list_quarantined()}


# ── list --quarantined ───────────────────────────────────────────────────────


def test_list_quarantined_is_the_one_listing_that_sees_quarantined_rows(
    runner: CliRunner, db: T2Database,
) -> None:
    db.put(project="proj", title="live.md", content="still here")
    q_id = _quarantine(db, "proj", "old.md")

    out = _nx(runner, db, "list", "--quarantined")
    assert out.exit_code == 0, out.output
    assert f"[{q_id}] proj/old.md" in out.output and "quarantined " in out.output
    assert "live.md" not in out.output, "a live row is not quarantined"
    assert "rolled up" not in out.output, "nothing is marked yet"

    plain = _nx(runner, db, "list")
    assert "live.md" in plain.output and "old.md" not in plain.output, (
        "plain list hides the quarantined row, as get and search do"
    )

    other = _nx(runner, db, "list", "--quarantined", "--project", "other")
    assert other.exit_code == 0 and "No quarantined entries found." in other.output

    _mark(db, "proj", [q_id])
    marked = _nx(runner, db, "list", "--quarantined")
    assert "rolled up " in marked.output, "a marked row shows its rollup stamp"


# ── restore ──────────────────────────────────────────────────────────────────


def test_restore_brings_a_quarantined_row_back_as_permanent(
    runner: CliRunner, db: T2Database,
) -> None:
    q_id = _quarantine(db, "proj", "old.md")
    assert db.get(id=q_id) is None

    out = _nx(runner, db, "restore", str(q_id))
    assert out.exit_code == 0, out.output
    assert "Restored: proj/old.md (permanent)" in out.output

    back = db.get(id=q_id)
    assert back is not None and back["content"] == "stale"
    assert back["ttl"] is None, "restore makes the row permanent, not re-TTL'd"
    assert q_id not in _quarantined_ids(db)

    again = _nx(runner, db, "restore", str(q_id))
    assert again.exit_code != 0 and f"no quarantined entry with id={q_id}" in again.output


# ── reap ─────────────────────────────────────────────────────────────────────


def test_reap_deletes_only_marked_rows_and_their_topic_assignments(
    runner: CliRunner, db: T2Database,
) -> None:
    """Residual 2 of the plan, decided here: reap runs the taxonomy cascade
    client-side, as ``nx memory delete`` does, so a reaped row leaves no
    topic_assignments behind."""
    marked_title, kept_title = canonical_chunk_id("reap-marked"), canonical_chunk_id("reap-kept")
    marked_id = _quarantine(db, "proj", marked_title)
    kept_id = _quarantine(db, "proj", kept_title)
    topic_id = _seed_topic(db.taxonomy, "reap-topic", collection="proj", doc_count=2)
    _seed_assignment(db.taxonomy, marked_title, topic_id)
    _seed_assignment(db.taxonomy, kept_title, topic_id)
    _mark(db, "proj", [marked_id])

    declined = _nx(runner, db, "reap", input="n\n")
    assert declined.exit_code != 0 and f"[{marked_id}] proj/{marked_title}" in declined.output
    assert _quarantined_ids(db) == {marked_id, kept_id}, "a declined prompt deletes nothing"

    out = _nx(runner, db, "reap", "--yes")
    assert out.exit_code == 0, out.output
    assert "Reaped 1 entry." in out.output
    assert _quarantined_ids(db) == {kept_id}, "the unmarked row is never reaped"
    assert sorted(db.taxonomy.get_all_topic_doc_ids(topic_id)) == [kept_title], (
        "the reaped row's topic assignment must go with it; the kept row's stays"
    )


def test_reap_with_nothing_marked_touches_nothing(runner: CliRunner, db: T2Database) -> None:
    q_id = _quarantine(db, "proj", "old.md")
    out = _nx(runner, db, "reap", "--yes")
    assert out.exit_code == 0, out.output
    assert "Nothing to reap" in out.output
    assert _quarantined_ids(db) == {q_id}


# ── summaries ────────────────────────────────────────────────────────────────


def test_summaries_lists_and_shows_by_id(runner: CliRunner, db: T2Database) -> None:
    empty = _nx(runner, db, "summaries")
    assert empty.exit_code == 0 and "No summaries found." in empty.output

    q_id = _quarantine(db, "proj", "old.md")
    s_id = _mark(db, "proj", [q_id])

    listed = _nx(runner, db, "summaries")
    assert listed.exit_code == 0, listed.output
    assert f"[{s_id}] proj  (stub, " in listed.output and "1 source)" in listed.output

    shown = _nx(runner, db, "summaries", str(s_id))
    assert shown.exit_code == 0, shown.output
    assert f"sources: {q_id}" in shown.output
    assert "summary of the quarantined rows" in shown.output

    other = _nx(runner, db, "summaries", "--project", "other")
    assert "No summaries found." in other.output

    missing = _nx(runner, db, "summaries", str(s_id + 1000))
    assert missing.exit_code != 0 and "summary not found" in missing.output


# ── delete reaches a quarantined row ────────────────────────────────────────


def test_delete_by_id_removes_an_unmarked_quarantined_row_and_its_topic_assignments(
    runner: CliRunner, db: T2Database,
) -> None:
    """RDR-207 §Day 2 Operations names ``nx memory delete <id>`` as the
    explicit single deletion for a quarantined row. Without the fallback to
    the quarantined listing this fails with "entry not found", because get
    hides the row."""
    title = canonical_chunk_id("delete-quarantined")
    q_id = _quarantine(db, "proj", title)
    topic_id = _seed_topic(db.taxonomy, "delete-topic", collection="proj", doc_count=1)
    _seed_assignment(db.taxonomy, title, topic_id)

    out = _nx(runner, db, "delete", "--id", str(q_id), "--yes")
    assert out.exit_code == 0, out.output
    assert f"Deleted: proj/{title} (quarantined)" in out.output
    assert q_id not in _quarantined_ids(db), "the row itself is gone"
    assert db.taxonomy.get_all_topic_doc_ids(topic_id) == [], "and its topic assignment"


def test_delete_by_project_and_title_reaches_a_quarantined_row(
    runner: CliRunner, db: T2Database,
) -> None:
    q_id = _quarantine(db, "proj", "old.md")
    out = _nx(runner, db, "delete", "--project", "proj", "--title", "old.md", "--yes")
    assert out.exit_code == 0, out.output
    assert q_id not in _quarantined_ids(db)


def test_facade_delete_by_id_cascades_for_a_quarantined_row(db: T2Database) -> None:
    title = canonical_chunk_id("facade-quarantined")
    q_id = _quarantine(db, "proj", title)
    topic_id = _seed_topic(db.taxonomy, "facade-topic", collection="proj", doc_count=1)
    _seed_assignment(db.taxonomy, title, topic_id)

    assert db.delete(id=q_id) is True
    assert q_id not in _quarantined_ids(db)
    assert db.taxonomy.get_all_topic_doc_ids(topic_id) == [], (
        "an id-only delete must resolve (project, title) from the quarantined "
        "listing, or purge_assignments_for_doc never runs"
    )


# ── argument errors and an engine older than RDR-207 ────────────────────────


@pytest.mark.parametrize("args", [["restore"], ["restore", "abc"], ["summaries", "abc"]])
def test_argument_errors(runner: CliRunner, args: list[str]) -> None:
    out = runner.invoke(main, ["memory", *args])
    assert out.exit_code == 2, out.output


def _route_missing() -> httpx.HTTPStatusError:
    request = httpx.Request("GET", "http://engine.test/v1/memory/quarantined")
    return httpx.HTTPStatusError(
        "404", request=request, response=httpx.Response(404, request=request),
    )


@pytest.mark.parametrize(
    "args",
    [["list", "--quarantined"], ["reap", "--yes"], ["restore", "5"], ["summaries"]],
)
def test_an_engine_without_the_routes_is_a_clean_error(runner: CliRunner, args: list[str]) -> None:
    store = MagicMock()
    for name in ("list_quarantined", "reap", "restore", "list_summaries"):
        getattr(store, name).side_effect = _route_missing()
    store.find_quarantined.return_value = None
    out = _nx(runner, MagicMock(memory=store), *args)
    assert out.exit_code == 1, out.output
    assert "predates RDR-207 quarantine" in out.output
    assert "Traceback" not in out.output


def test_delete_against_an_engine_without_the_routes_is_still_not_found(runner: CliRunner) -> None:
    """The delete fallback calls the REAL find_quarantined, whose listing 404s
    on an engine older than RDR-207; that must read as the old "entry not
    found", never a crash."""
    store = MagicMock()
    store.get.return_value = None
    store.list_quarantined.side_effect = _route_missing()
    store.find_quarantined = partial(HttpMemoryStore.find_quarantined, store)
    out = _nx(runner, MagicMock(memory=store), "delete", "--id", "5", "--yes")
    assert out.exit_code == 1, out.output
    assert "entry not found" in out.output and "Traceback" not in out.output
    store.list_quarantined.assert_called_once()
    store.delete.assert_not_called()


# ── the memory.quarantine doctor row ────────────────────────────────────────


class _FakeStore:
    closed = False

    def __init__(self, quarantined=(), summaries=(), exc: Exception | None = None) -> None:
        self._quarantined = list(quarantined)
        self._summaries = list(summaries)
        self._exc = exc

    def list_quarantined(self, project=None):
        if self._exc is not None:
            raise self._exc
        return self._quarantined

    def list_summaries(self, project=None):
        return self._summaries

    def close(self) -> None:
        self.closed = True


def _row(monkeypatch: pytest.MonkeyPatch, factory, *, warn: bool = False) -> h.HealthResult:
    monkeypatch.setattr("nexus.db.t2.http_memory_store.HttpMemoryStore", factory)
    [result] = h._check_memory_quarantine()
    assert result.label == "memory.quarantine"
    assert (result.ok, result.warn) == (not warn, warn), result.detail
    return result


def test_doctor_row_is_not_applicable_without_engine_backed_t2(monkeypatch) -> None:
    def _no_engine(*a, **k):
        raise RuntimeError("no service registered")

    result = _row(monkeypatch, _no_engine)
    assert result.detail == "not applicable (no engine-backed T2 on this box)"


def test_doctor_row_counts_unmarked_marked_and_summaries(monkeypatch) -> None:
    store = _FakeStore(
        quarantined=[
            {"id": 1, "rolled_up_at": "2026-09-14T00:00:00Z"},
            {"id": 2, "rolled_up_at": None},
            {"id": 3, "rolled_up_at": None},
        ],
        summaries=[{"id": 9}],
    )
    result = _row(monkeypatch, lambda *a, **k: store)
    assert result.detail == (
        "2 quarantined without a rollup mark, 1 marked and awaiting "
        "`nx memory reap`, 1 summaries"
    )
    assert store.closed is True


def test_doctor_row_on_an_engine_older_than_rdr_207_reads_unread_not_clean(monkeypatch) -> None:
    result = _row(monkeypatch, lambda *a, **k: _FakeStore(exc=_route_missing()))
    assert result.detail.startswith("skipped (engine predates the RDR-207 quarantine routes")


def test_doctor_row_soft_warns_when_the_store_cannot_be_read(monkeypatch) -> None:
    """substantive-critic, nexus-l3yuc.14: an unreachable engine is a soft
    WARN, never a silent ok, as _check_t2_schema_applied does."""
    store = _FakeStore(exc=RuntimeError("connection refused"))
    result = _row(monkeypatch, lambda *a, **k: store, warn=True)
    assert result.detail.startswith("memory store unreachable (RuntimeError: connection refused)")
    assert store.closed is True


# ── expire wording against an engine that still deletes ─────────────────────
#
# substantive-critic, nexus-l3yuc.14: REQUIRED_ENGINE_VERSION still names an
# engine older than RDR-207, where expire deletes for real. Reporting that as
# "Quarantined" tells the user a gone row is recoverable.


@pytest.mark.parametrize(
    "deleted,quarantined,prefix,expected",
    [
        ([], [], "", "Quarantined 0 entries."),
        ([], [7], "", "Quarantined 1 entry."),
        ([1, 2], [], "memory ",
         "Deleted 2 memory entries (this engine predates RDR-207 quarantine, so expiry deleted them)."),
        ([1], [2, 3], "",
         "Deleted 1 entry (this engine predates RDR-207 quarantine, so expiry deleted them); quarantined 2."),
    ],
    ids=["nothing", "quarantined-one", "deleted-only", "both"],
)
def test_expire_result_describes_what_the_engine_did(deleted, quarantined, prefix, expected) -> None:
    result = MemoryExpireResult(deleted_ids=deleted, quarantined_ids=quarantined)
    assert result.describe(prefix=prefix) == expected


def test_expire_cmd_against_a_deleting_engine_says_deleted(runner: CliRunner) -> None:
    db = MagicMock()
    db.memory.expire.return_value = MemoryExpireResult(deleted_ids=[4, 5])
    out = _nx(runner, db, "expire")
    assert out.exit_code == 0, out.output
    assert "Deleted 2 entries (this engine predates RDR-207 quarantine" in out.output
    assert "Quarantined" not in out.output


def test_session_end_against_a_deleting_engine_says_deleted(tmp_path, monkeypatch) -> None:
    monkeypatch.delenv("NX_SESSION_ID", raising=False)
    result = MemoryExpireResult(deleted_ids=[1, 2, 3])
    monkeypatch.setattr(mcp_infra, "t2_index_write", lambda fn: (0, result))
    message = hooks.session_end_flush()
    assert "Deleted 3 memory entries (this engine predates RDR-207 quarantine" in message, message
    assert "Quarantined" not in message


def test_doctor_row_against_the_engine(db: T2Database) -> None:
    """The real route, through the row's own self-resolving store: empty reads
    ``none`` (what a virgin box with an engine shows), then one quarantined row
    is counted."""
    [empty] = h._check_memory_quarantine()
    assert empty.detail == "none", empty.detail

    _quarantine(db, "proj", "old.md")
    [one] = h._check_memory_quarantine()
    assert one.ok is True and one.warn is False
    assert one.detail.startswith("1 quarantined without a rollup mark, 0 marked"), one.detail
