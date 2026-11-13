# SPDX-License-Identifier: AGPL-3.0-or-later
"""``nx backup`` in cloud mode, the client half (nexus-csayy.3, RDR-248): the session driver against a stand-in
for the engine's export routes served over a real socket (``tests/_fake_export_engine.py``).

Each test is one acceptance criterion of the bead: the file the driver writes verifies; an interrupt leaves no
partial file and sends a best-effort DELETE; a token that expires mid-export resumes from the last key; N failed
pages close the session and remove the partial file; an engine without the routes is refused naming the version
it needs. The happy path against the REAL engine is ``tests/test_nx_backup_cloud_roundtrip.py``.
"""

from __future__ import annotations

import os
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from nexus.backup import cloud_export as ce
from nexus.backup.cloud_export import ExportClient, ExportRefused, run_export
from nexus.backup.pg_backup import BackupFailed, BackupRefused
from nexus.backup.tenant_file import verify_file
from tests._module_seam import setattr_in
from tests._fake_export_engine import FakeEngine, Reply, TableSpec, standard_tables


class Creds:
    """Bearer tokens in order: each ``rejected`` moves to the next, the way a refreshed token arrives."""

    def __init__(self, *tokens: str) -> None:
        self.tokens = list(tokens)
        self.rejections: list[str] = []

    def bearer(self) -> str:
        return self.tokens[0]

    def rejected(self, sent: str) -> bool:
        self.rejections.append(sent)
        if len(self.tokens) > 1 and sent == self.tokens[0]:
            self.tokens.pop(0)
            return True
        return False


class Sleeps:
    def __init__(self) -> None:
        self.seconds: list[float] = []

    def __call__(self, s: float) -> None:
        self.seconds.append(s)


def _client(engine: FakeEngine, creds: Creds | None = None, sleeps: Sleeps | None = None, **kw: object) -> ExportClient:
    return ExportClient(engine.base_url, creds or Creds("tok"), sleep=sleeps or Sleeps(), **kw)  # type: ignore[arg-type]


def _entries(directory: Path) -> set[str]:
    return {p.name for p in directory.iterdir()}


# -- the happy path: the file is the engine's pages, verified ------------------------------------------


def test_an_export_writes_a_file_that_verifies_and_closes_its_session(tmp_path: Path) -> None:
    dest = tmp_path / "out" / "acme.nxtenant"
    with FakeEngine(standard_tables()) as engine:
        result = run_export(_client(engine), dest)
        assert engine.deleted == engine.opened and len(engine.opened) == 1, "one session, closed"
        pulled = [(r.query["table"], r.query.get("after")) for r in engine.requests("rows")]
    assert pulled == [
        ("memory", None), ("memory", "memory:0"), ("memory", "memory:1"),
        ("frecency", None), ("plans", None),
    ], "every table from its first page, each next page after the previous page's last key"
    assert result.path == dest and dest.stat().st_size == result.size
    report = verify_file(dest)
    assert [(t.name, t.rows, t.pages) for t in report.tables] == [("memory", 6, 3), ("frecency", 2, 1), ("plans", 0, 1)]
    assert report.header["tenant_id"] == "acme" and report.header["schema_changeset_count"] == 572
    assert "session_id" not in report.header and "expires_at" not in report.header
    assert result.rows_by_class == {"data": 6, "derived": 2}
    assert _entries(dest.parent) == {"acme.nxtenant"}


@pytest.mark.skipif(os.name == "nt", reason="POSIX file modes")
def test_the_file_is_owner_only(tmp_path: Path) -> None:
    dest = tmp_path / "out" / "acme.nxtenant"
    with FakeEngine(standard_tables()) as engine:
        run_export(_client(engine), dest)
    assert (dest.stat().st_mode & 0o777) == 0o600


def test_a_destination_named_after_the_tenant_when_none_is_given(tmp_path: Path) -> None:
    with FakeEngine(standard_tables(), tenant="acme/co") as engine:
        result = run_export(
            _client(engine), lambda tenant: tmp_path / ce.default_file_name(tenant, now=datetime(2026, 11, 12, 1, 2, 3, tzinfo=UTC)),
        )
    assert result.path.name == "nexus-acme_co-20261112T010203Z.nxtenant"


def test_sensitive_documents_are_counted_from_the_catalog_documents_pages(tmp_path: Path) -> None:
    tables = [TableSpec(
        "catalog_documents", "data", [("tumbler", "text"), ("sensitive", "boolean")],
        [[[b"1.1.1", b"\x00"], [b"1.1.2", b"\x01"]], [[b"1.1.3", b"\x01"]]],
    )]
    with FakeEngine(tables) as engine:
        result = run_export(_client(engine), tmp_path / "s.nxtenant")
    assert result.sensitive_documents == 2


def test_no_sensitive_documents_when_none_are_flagged(tmp_path: Path) -> None:
    with FakeEngine(standard_tables()) as engine:
        assert run_export(_client(engine), tmp_path / "s.nxtenant").sensitive_documents == 0


def test_an_existing_destination_is_refused_before_a_session_is_opened(tmp_path: Path) -> None:
    dest = tmp_path / "mine.nxtenant"
    dest.write_bytes(b"precious")
    with FakeEngine(standard_tables()) as engine:
        with pytest.raises(BackupRefused, match="already exists"):
            run_export(_client(engine), dest)
        assert not engine.requests("open"), "the engine is not touched"
    assert dest.read_bytes() == b"precious"
    assert _entries(tmp_path) == {"mine.nxtenant"}


def test_a_destination_that_appears_during_the_download_is_never_overwritten(tmp_path: Path) -> None:
    dest = tmp_path / "mine.nxtenant"
    with FakeEngine(standard_tables()) as engine:
        def appear(req) -> Reply | None:
            if req.kind == "rows" and req.nth == 0:
                dest.write_bytes(b"someone else got here first")
            return None
        engine.script.append(appear)
        with pytest.raises(BackupRefused, match="already exists"):
            run_export(_client(engine), dest)
        assert engine.deleted == engine.opened, "the session is closed"
    assert dest.read_bytes() == b"someone else got here first"
    assert _entries(tmp_path) == {"mine.nxtenant"}, "the partial file is gone"


# -- an engine without the routes is refused, naming the version it needs --------------------------------


def test_an_engine_without_the_export_flag_is_refused_naming_the_version_it_needs(tmp_path: Path) -> None:
    with FakeEngine(standard_tables(), supported=False, release="0.1.189") as engine:
        with pytest.raises(ExportRefused) as err:
            run_export(_client(engine), tmp_path / "x.nxtenant")
        assert not engine.requests("open"), "an older engine is never asked to open a session"
    text = str(err.value)
    assert "0.1.191" in text and "0.1.189" in text and "tenant export" in text
    assert _entries(tmp_path) == set()


def test_an_engine_with_a_newer_export_format_is_refused_naming_the_format(tmp_path: Path) -> None:
    with FakeEngine(standard_tables(), format_version=2) as engine:
        with pytest.raises(ExportRefused, match="format 2"):
            run_export(_client(engine), tmp_path / "x.nxtenant")


def test_an_unreachable_engine_is_refused_not_retried(tmp_path: Path) -> None:
    with FakeEngine(standard_tables()) as engine:
        url = engine.base_url
    client = ExportClient(url, Creds("tok"), sleep=Sleeps())
    with pytest.raises(ExportRefused, match="unreachable"):
        run_export(client, tmp_path / "x.nxtenant")


def test_a_header_with_no_changeset_count_is_refused_and_the_session_closed(tmp_path: Path) -> None:
    with FakeEngine(standard_tables(), changesets=None) as engine:
        with pytest.raises(ExportRefused, match="schema_changeset_count"):
            run_export(_client(engine), tmp_path / "x.nxtenant")
        assert engine.deleted == engine.opened and len(engine.opened) == 1
    assert _entries(tmp_path) == set()


# -- credentials -----------------------------------------------------------------------------------------


def test_a_root_token_is_refused_saying_which_token_to_use(tmp_path: Path) -> None:
    with FakeEngine(standard_tables()) as engine:
        engine.script.append(lambda r: Reply(403, {"error": "an export is read with the tenant's own token", "reason": "export_scope_forbidden"}) if r.kind == "open" else None)
        with pytest.raises(ExportRefused, match="tenant or data token"):
            run_export(_client(engine), tmp_path / "x.nxtenant")


def test_a_rejected_token_with_no_fresh_one_is_refused(tmp_path: Path) -> None:
    with FakeEngine(standard_tables(), accepted={"other"}) as engine:
        creds = Creds("tok")
        with pytest.raises(ExportRefused, match="rejected"):
            run_export(_client(engine, creds), tmp_path / "x.nxtenant")
    assert creds.rejections == ["tok"]


def test_a_token_that_expires_mid_export_is_refreshed_and_the_export_resumes_from_the_last_key(tmp_path: Path) -> None:
    dest = tmp_path / "x.nxtenant"
    with FakeEngine(standard_tables(), accepted={"old"}) as engine:
        def expire(req) -> Reply | None:  # the old token stops working at the third page request
            if req.kind == "rows" and req.nth == 2:
                engine.accepted.clear()
                engine.accepted.add("new")
            return None
        engine.script.append(expire)
        creds = Creds("old", "new")
        run_export(_client(engine, creds), dest)
        rows = engine.requests("rows")
        assert len(engine.opened) == 1, "the session is the tenant's, not the token's: no new session"
        assert creds.rejections == ["old"]
    assert [r.bearer for r in rows] == ["old", "old", "old", "new", "new", "new"]
    # the request the new token made is the page the old token was refused: same table, same key
    assert rows[3].query == rows[2].query == {"table": "memory", "after": "memory:1"}
    verify_file(dest)


# -- page failures: bounded retries from the same key ----------------------------------------------------


def test_a_dropped_connection_costs_one_page_not_the_export(tmp_path: Path) -> None:
    sleeps = Sleeps()
    with FakeEngine(standard_tables()) as engine:
        engine.script.append(lambda r: Reply(drop=True) if r.kind == "rows" and r.nth == 1 else None)
        run_export(_client(engine, sleeps=sleeps), tmp_path / "x.nxtenant")
        rows = engine.requests("rows")
    assert rows[1].query == rows[2].query, "asked again from the same key"
    assert sleeps.seconds == [ce.BACKOFF_LADDER[0]]
    verify_file(tmp_path / "x.nxtenant")


def test_a_page_cut_mid_transfer_is_asked_for_again_from_the_same_key_and_the_file_verifies(tmp_path: Path) -> None:
    """The engine sent every header and half the body, then the connection closed (the edge's 60-second cut, a reset)."""
    sleeps = Sleeps()
    dest = tmp_path / "x.nxtenant"
    with FakeEngine(standard_tables()) as engine:
        def cut(req) -> Reply | None:  # the SECOND page of memory, which has a key to resume from
            if req.kind == "rows" and req.nth == 1:
                reply = engine._answer(req)
                assert isinstance(reply.body, bytes) and len(reply.body) > 8
                reply.cut_at = len(reply.body) // 2
                return reply
            return None
        engine.script.append(cut)
        run_export(_client(engine, sleeps=sleeps), dest)
        rows = engine.requests("rows")
    assert rows[1].query == {"table": "memory", "after": "memory:0"}, "the cut page is the one after the first page's key"
    assert rows[2].query == rows[1].query, "asked again from the same key, not from the start of the table"
    assert sleeps.seconds == [ce.BACKOFF_LADDER[0]]
    report = verify_file(dest)
    assert [(t.name, t.rows, t.pages) for t in report.tables] == [("memory", 6, 3), ("frecency", 2, 1), ("plans", 0, 1)], (
        "no page twice, none lost"
    )


def test_a_page_whose_hash_is_wrong_is_asked_for_again(tmp_path: Path) -> None:
    with FakeEngine(standard_tables()) as engine:
        def corrupt(req) -> Reply | None:
            if req.kind == "rows" and req.nth == 1:
                reply = engine._answer(req)
                reply.body = reply.body[:-3] + b"\x00\x00\x00"  # same length, different bytes
                return reply
            return None
        engine.script.append(corrupt)
        run_export(_client(engine), tmp_path / "x.nxtenant")
        assert len(engine.requests("rows")) == 6
    verify_file(tmp_path / "x.nxtenant")


def test_a_page_that_claims_the_wrong_row_count_is_asked_for_again(tmp_path: Path) -> None:
    with FakeEngine(standard_tables()) as engine:
        def lie(req) -> Reply | None:
            if req.kind == "rows" and req.nth == 0:
                reply = engine._answer(req)
                reply.headers["X-Nexus-Export-Rows"] = "9"
                return reply
            return None
        engine.script.append(lie)
        run_export(_client(engine), tmp_path / "x.nxtenant")
    verify_file(tmp_path / "x.nxtenant")


def test_n_failed_pages_close_the_session_remove_the_partial_file_and_say_run_again(tmp_path: Path) -> None:
    dest = tmp_path / "out" / "x.nxtenant"
    sleeps = Sleeps()
    with FakeEngine(standard_tables()) as engine:
        engine.script.append(lambda r: Reply(500, {"error": "boom", "reason": "internal"}) if r.kind == "rows" and r.nth >= 1 else None)
        with pytest.raises(BackupFailed) as err:
            run_export(_client(engine, sleeps=sleeps), dest)
        assert engine.deleted == engine.opened, "the session is closed"
        failing = [r for r in engine.requests("rows") if r.nth >= 1]
        assert len(failing) == ce.PAGE_ATTEMPTS and len({tuple(r.query.items()) for r in failing}) == 1
    text = str(err.value)
    assert "memory" in text and "run nx backup again" in text
    assert sleeps.seconds == list(ce.BACKOFF_LADDER)
    assert not dest.exists() and _entries(dest.parent) == set(), "no partial file"


def test_a_503_with_retry_after_waits_as_told_and_one_without_uses_the_ladder(tmp_path: Path) -> None:
    sleeps = Sleeps()
    with FakeEngine(standard_tables()) as engine:
        def busy(req) -> Reply | None:
            if req.kind == "rows" and req.nth == 0:
                return Reply(503, {"error": "busy", "reason": "export_page_busy"}, {"Retry-After": "7"})
            if req.kind == "rows" and req.nth == 2:
                return Reply(503, {"error": "database connection pool exhausted, retry"})
            return None
        engine.script.append(busy)
        run_export(_client(engine, sleeps=sleeps), tmp_path / "x.nxtenant")
    assert sleeps.seconds == [7.0, ce.BACKOFF_LADDER[0]]


def test_a_page_cancelled_at_the_engines_budget_is_a_failed_page_and_ends_naming_the_budget(tmp_path: Path) -> None:
    """503 export_page_over_budget takes the five tries and the backoff ladder, never the busy budget, and the message
    names the engine's page budget and says to report it."""
    sleeps = Sleeps()
    with FakeEngine(standard_tables()) as engine:
        engine.script.append(lambda r: Reply(503, {"error": "over", "reason": "export_page_over_budget", "page_budget_ms": 20000}) if r.kind == "rows" else None)
        with pytest.raises(BackupFailed, match="20 s page budget") as err:
            run_export(_client(engine, sleeps=sleeps), tmp_path / "x.nxtenant")
        failing = engine.requests("rows")
    text = str(err.value)
    assert "report" in text and "export_page_over_budget" in text and "will not help" in text
    assert len(failing) == ce.PAGE_ATTEMPTS and len({tuple(r.query.items()) for r in failing}) == 1, "the same page, tried the attempts"
    assert sleeps.seconds == list(ce.BACKOFF_LADDER), "the ladder, not a Retry-After wait and not the busy budget"
    assert sum(sleeps.seconds) < ce.BUSY_WAIT_BUDGET_S
    assert _entries(tmp_path) == set()


def test_a_page_over_the_budget_once_is_retried_and_the_export_completes(tmp_path: Path) -> None:
    sleeps = Sleeps()
    with FakeEngine(standard_tables()) as engine:
        engine.script.append(lambda r: Reply(503, {"error": "over", "reason": "export_page_over_budget"}) if r.kind == "rows" and r.nth == 0 else None)
        run_export(_client(engine, sleeps=sleeps), tmp_path / "x.nxtenant")
    assert sleeps.seconds == [ce.BACKOFF_LADDER[0]]


def test_a_page_in_flight_409_waits_its_retry_after(tmp_path: Path) -> None:
    sleeps = Sleeps()
    with FakeEngine(standard_tables()) as engine:
        engine.script.append(lambda r: Reply(409, {"error": "x", "reason": "page_in_flight"}, {"Retry-After": "1"}) if r.kind == "rows" and r.nth == 0 else None)
        run_export(_client(engine, sleeps=sleeps), tmp_path / "x.nxtenant")
    assert sleeps.seconds == [1.0]


def test_a_400_from_the_engine_is_not_retried(tmp_path: Path) -> None:
    with FakeEngine(standard_tables()) as engine:
        engine.script.append(lambda r: Reply(400, {"error": "x", "reason": "malformed_after"}) if r.kind == "rows" and r.nth == 1 else None)
        with pytest.raises(BackupFailed, match="malformed_after"):
            run_export(_client(engine), tmp_path / "x.nxtenant")
        assert len(engine.requests("rows")) == 2
    assert _entries(tmp_path) == set()


# -- open: capacity and a session left behind --------------------------------------------------------------


def test_a_full_engine_is_waited_on_then_refused_with_a_time_to_try_again(tmp_path: Path) -> None:
    sleeps = Sleeps()
    with FakeEngine(standard_tables()) as engine:
        engine.script.append(lambda r: Reply(503, {"error": "full", "reason": "export_capacity_full", "retry_after_seconds": 60}, {"Retry-After": "60"}) if r.kind == "open" else None)
        with pytest.raises(ExportRefused, match="export sessions"):
            run_export(_client(engine, sleeps=sleeps, open_wait_budget_s=150.0), tmp_path / "x.nxtenant")
    assert sleeps.seconds == [60.0, 60.0, 60.0]


def test_a_full_engine_that_frees_a_slot_is_used(tmp_path: Path) -> None:
    sleeps = Sleeps()
    with FakeEngine(standard_tables()) as engine:
        engine.script.append(lambda r: Reply(503, {"error": "full", "reason": "export_capacity_full"}, {"Retry-After": "5"}) if r.kind == "open" and r.nth == 0 else None)
        run_export(_client(engine, sleeps=sleeps), tmp_path / "x.nxtenant")
    assert sleeps.seconds == [5.0]


def test_a_session_left_by_an_earlier_run_is_deleted_then_the_open_is_retried(tmp_path: Path) -> None:
    with FakeEngine(standard_tables()) as engine:
        engine.sessions.add("left-behind")
        engine.script.append(lambda r: Reply(409, {"error": "x", "reason": "export_session_exists", "session_id": "left-behind"}) if r.kind == "open" and r.nth == 0 else None)
        run_export(_client(engine), tmp_path / "x.nxtenant")
        assert engine.deleted[0] == "left-behind"
        assert len(engine.opened) == 1


def test_a_session_exists_answer_twice_is_a_refusal(tmp_path: Path) -> None:
    with FakeEngine(standard_tables()) as engine:
        engine.script.append(lambda r: Reply(409, {"error": "x", "reason": "export_session_exists", "session_id": "ghost"}) if r.kind == "open" else None)
        with pytest.raises(ExportRefused, match="already has an export session"):
            run_export(_client(engine), tmp_path / "x.nxtenant")


# -- a session the engine forgot ------------------------------------------------------------------------


@pytest.mark.parametrize("status", [404, 410])
def test_a_forgotten_session_starts_over_with_a_new_one(status: int, tmp_path: Path) -> None:
    dest = tmp_path / "x.nxtenant"
    notes: list[str] = []
    with FakeEngine(standard_tables()) as engine:
        def forget(req) -> Reply | None:
            if req.kind == "rows" and req.nth == 2:
                engine.sessions.clear()  # the engine restarted
                return Reply(status, {"error": "gone", "reason": "session_closed" if status == 410 else "no_such_session"})
            return None
        engine.script.append(forget)
        run_export(_client(engine), dest, on_note=notes.append)
        assert len(engine.opened) == 2
        rows = engine.requests("rows")
    assert rows[3].query == {"table": "memory"}, "the new session starts the first table over"
    assert any("starting over" in n for n in notes)
    assert [t.rows for t in verify_file(dest).tables] == [6, 2, 0], "the file is one clean export, not two stitched"
    assert _entries(tmp_path) == {"x.nxtenant"}


def test_a_session_gone_past_its_expiry_names_the_sixty_minute_limit(tmp_path: Path) -> None:
    skew = {"by": timedelta(0)}
    with FakeEngine(standard_tables(), expires_in_seconds=3600) as engine:
        def vanish(req) -> Reply | None:
            if req.kind == "rows" and req.nth == 1:
                skew["by"] = timedelta(hours=2)  # the client's clock is now past the session's expires_at
                engine.sessions.clear()
                return Reply(404, {"error": "gone", "reason": "no_such_session"})
            return None
        engine.script.append(vanish)
        client = _client(engine, now=lambda: datetime.now(UTC) + skew["by"])
        with pytest.raises(BackupFailed) as err:
            run_export(client, tmp_path / "x.nxtenant")
        assert len(engine.opened) == 1, "past the limit: no new session"
    assert "60-minute" in str(err.value)
    assert _entries(tmp_path) == set()


def test_a_session_forgotten_again_and_again_gives_up_and_says_run_again(tmp_path: Path) -> None:
    with FakeEngine(standard_tables()) as engine:
        def forget(req) -> Reply | None:
            if req.kind == "rows":
                engine.sessions.clear()
                return Reply(410, {"error": "gone", "reason": "session_closed"})
            return None
        engine.script.append(forget)
        with pytest.raises(BackupFailed, match="run nx backup again"):
            run_export(_client(engine), tmp_path / "x.nxtenant")
        assert len(engine.opened) == ce.MAX_RESTARTS + 1
    assert _entries(tmp_path) == set()


# -- interrupt -------------------------------------------------------------------------------------------


@pytest.mark.parametrize("exc", [KeyboardInterrupt, SystemExit])
def test_an_interrupt_leaves_no_partial_file_and_sends_a_best_effort_delete(exc: type[BaseException], tmp_path: Path) -> None:
    dest = tmp_path / "out" / "x.nxtenant"
    seen = []

    def progress(done: int, table: str, index: int, total: int, rows: int) -> None:
        seen.append(table)
        if len(seen) == 2:
            raise exc()

    with FakeEngine(standard_tables()) as engine:
        with pytest.raises(exc):
            run_export(_client(engine), dest, on_progress=progress)
        assert engine.deleted == engine.opened and len(engine.opened) == 1, "the snapshot is released at once"
    assert not dest.exists()
    assert _entries(dest.parent) == set(), "no .partial left"


def test_an_interrupt_while_the_engine_is_gone_still_cleans_up_the_file(tmp_path: Path) -> None:
    dest = tmp_path / "out" / "x.nxtenant"

    def progress(done: int, table: str, index: int, total: int, rows: int) -> None:
        raise KeyboardInterrupt

    with FakeEngine(standard_tables()) as engine:
        engine.script.append(lambda r: Reply(drop=True) if r.kind == "delete" else None)  # the DELETE fails
        with pytest.raises(KeyboardInterrupt):
            run_export(_client(engine), dest, on_progress=progress)
    assert _entries(dest.parent) == set()


def test_progress_reports_bytes_table_and_rows(tmp_path: Path) -> None:
    calls: list[tuple[int, str, int, int, int]] = []
    with FakeEngine(standard_tables()) as engine:
        run_export(_client(engine), tmp_path / "x.nxtenant", on_progress=lambda *a: calls.append(a))
    assert [c[1] for c in calls] == ["memory"] * 3 + ["frecency", "plans"]
    assert calls[0][2:4] == (1, 3) and calls[-1][2:4] == (3, 3)
    assert [c[0] for c in calls] == sorted(c[0] for c in calls) and calls[-1][0] > 0
    assert calls[2][4] == 6, "rows so far"


# -- the destination's name ------------------------------------------------------------------------------


def test_default_file_name_is_tenant_and_utc_stamp() -> None:
    name = ce.default_file_name("acme", now=datetime(2026, 11, 12, 1, 2, 3, tzinfo=UTC))
    assert name == "nexus-acme-20261112T010203Z.nxtenant"
    assert ce.default_file_name("a b/../c", now=datetime(2026, 1, 1, tzinfo=UTC)) == "nexus-a_b_.._c-20260101T000000Z.nxtenant"


# -- fix round r1 ---------------------------------------------------------------------------------------------


def _interrupt_on_page(n: int):
    seen: list[str] = []

    def progress(done: int, table: str, index: int, total: int, rows: int) -> None:
        seen.append(table)
        if len(seen) == n:
            raise KeyboardInterrupt

    return progress


def test_a_second_interrupt_during_the_delete_still_leaves_no_partial_file(tmp_path: Path) -> None:
    """The file is removed BEFORE the network DELETE, so a second Ctrl-C (or a SIGHUP then SIGTERM) while the
    DELETE is in flight cannot leave a multi-gigabyte hidden partial in the backups directory."""
    dest = tmp_path / "out" / "x.nxtenant"
    with FakeEngine(standard_tables()) as engine:
        client = _client(engine)

        def delete_interrupted(*_a: object, **_k: object) -> None:
            raise KeyboardInterrupt

        client._http.delete = delete_interrupted  # type: ignore[method-assign]
        with pytest.raises(KeyboardInterrupt):
            run_export(client, dest, on_progress=_interrupt_on_page(2))
    assert _entries(dest.parent) == set(), "no .partial"


def test_an_interrupt_between_the_open_and_the_callers_assignment_still_deletes_the_session(tmp_path: Path) -> None:
    with FakeEngine(standard_tables()) as engine:
        client = _client(engine)
        real_open = client.open_session

        def open_then_interrupt():
            real_open()  # the engine has the session; the caller never receives it
            raise KeyboardInterrupt

        client.open_session = open_then_interrupt  # type: ignore[method-assign]
        with pytest.raises(KeyboardInterrupt):
            run_export(client, tmp_path / "x.nxtenant")
        assert len(engine.opened) == 1 and engine.deleted == engine.opened


def test_an_interrupt_while_the_partial_file_is_being_made_leaves_none(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    dest = tmp_path / "out" / "x.nxtenant"

    def interrupted_fdopen(fd: int, *_a: object, **_k: object):
        raise KeyboardInterrupt

    setattr_in(monkeypatch, ce, "os.fdopen", interrupted_fdopen)
    with FakeEngine(standard_tables()) as engine:
        with pytest.raises(KeyboardInterrupt):
            run_export(_client(engine), dest)
        assert engine.deleted == engine.opened
    assert _entries(dest.parent) == set()


def test_a_full_disk_fails_with_the_free_space_message_and_leaves_nothing(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import errno  # noqa: PLC0415

    from nexus.backup.tenant_file import TenantFileWriter  # noqa: PLC0415

    class FullDisk(TenantFileWriter):
        def add_page(self, *a, **k):  # type: ignore[no-untyped-def]
            if self._current is not None and self._current.pages >= 1:
                raise OSError(errno.ENOSPC, "No space left on device")
            return super().add_page(*a, **k)

    monkeypatch.setattr(ce, "TenantFileWriter", FullDisk)
    dest = tmp_path / "out" / "x.nxtenant"
    with FakeEngine(standard_tables()) as engine:
        with pytest.raises(BackupFailed) as err:
            run_export(_client(engine), dest)
        assert engine.deleted == engine.opened
    text = str(err.value)
    assert "No space left on device" in text and "Free some space" in text and "run nx backup again" in text
    assert _entries(dest.parent) == set()


def test_a_session_exists_answer_with_no_id_is_waited_out_then_the_open_succeeds(tmp_path: Path) -> None:
    sleeps = Sleeps()
    with FakeEngine(standard_tables()) as engine:
        engine.script.append(lambda r: Reply(409, {"error": "x", "reason": "export_session_exists"}) if r.kind == "open" and r.nth < 2 else None)
        run_export(_client(engine, sleeps=sleeps), tmp_path / "x.nxtenant")
    assert sleeps.seconds == list(ce.BACKOFF_LADDER[:2])


def test_a_session_exists_answer_with_no_id_forever_is_refused_after_the_ladder(tmp_path: Path) -> None:
    sleeps = Sleeps()
    with FakeEngine(standard_tables()) as engine:
        engine.script.append(lambda r: Reply(409, {"error": "x", "reason": "export_session_exists"}) if r.kind == "open" else None)
        with pytest.raises(ExportRefused, match="already has an export session"):
            run_export(_client(engine, sleeps=sleeps), tmp_path / "x.nxtenant")
    assert sleeps.seconds == list(ce.BACKOFF_LADDER)


def test_busy_answers_have_their_own_budget_and_do_not_use_up_the_attempts(tmp_path: Path) -> None:
    """Ten 503 export_page_busy answers in a row (a tenant create holding a lock) is more than the five attempts a
    failing page gets, and the export still completes."""
    sleeps = Sleeps()
    with FakeEngine(standard_tables()) as engine:
        engine.script.append(lambda r: Reply(503, {"error": "busy", "reason": "export_page_busy"}, {"Retry-After": "1"}) if r.kind == "rows" and r.nth < 10 else None)
        run_export(_client(engine, sleeps=sleeps), tmp_path / "x.nxtenant")
    assert sleeps.seconds == [1.0] * 10


def test_a_page_that_stays_busy_past_the_budget_fails_and_says_run_again(tmp_path: Path) -> None:
    sleeps = Sleeps()
    with FakeEngine(standard_tables()) as engine:
        engine.script.append(lambda r: Reply(409, {"error": "x", "reason": "page_in_flight"}, {"Retry-After": "30"}) if r.kind == "rows" else None)
        with pytest.raises(BackupFailed, match="page_in_flight") as err:
            run_export(_client(engine, sleeps=sleeps), tmp_path / "x.nxtenant")
    assert "run nx backup again" in str(err.value) and sum(sleeps.seconds) >= ce.BUSY_WAIT_BUDGET_S
    assert _entries(tmp_path) == set()


@pytest.mark.parametrize("body", [b"not json at all", b'{"no": "session id"}', b"[]"])
def test_an_open_answer_that_is_not_a_session_header_is_a_clear_failure(body: bytes, tmp_path: Path) -> None:
    with FakeEngine(standard_tables()) as engine:
        engine.script.append(lambda r: Reply(200, body, {"Content-Type": "application/json"}) if r.kind == "open" else None)
        with pytest.raises(BackupFailed, match="not an export session header") as err:
            run_export(_client(engine), tmp_path / "x.nxtenant")
    assert "Traceback" not in str(err.value)
    assert _entries(tmp_path) == set()


def test_a_transient_5xx_from_version_is_retried(tmp_path: Path) -> None:
    sleeps = Sleeps()
    with FakeEngine(standard_tables()) as engine:
        engine.script.append(lambda r: Reply(502, {"error": "bad gateway"}) if r.kind == "version" and r.nth < 2 else None)
        run_export(_client(engine, sleeps=sleeps), tmp_path / "x.nxtenant")
    assert sleeps.seconds == list(ce.BACKOFF_LADDER[:2])


def test_a_body_that_will_not_decode_is_a_failed_page_not_a_traceback(tmp_path: Path) -> None:
    sleeps = Sleeps()
    with FakeEngine(standard_tables()) as engine:
        engine.script.append(
            lambda r: Reply(200, b"this is not gzip", {"Content-Encoding": "gzip"}) if r.kind == "rows" and r.nth == 0 else None
        )
        run_export(_client(engine, sleeps=sleeps), tmp_path / "x.nxtenant")
    assert sleeps.seconds == [ce.BACKOFF_LADDER[0]]
    verify_file(tmp_path / "x.nxtenant")


def test_waiting_for_a_free_export_slot_says_so(tmp_path: Path) -> None:
    notes: list[str] = []
    with FakeEngine(standard_tables()) as engine:
        engine.script.append(lambda r: Reply(503, {"error": "full", "reason": "export_capacity_full"}, {"Retry-After": "5"}) if r.kind == "open" and r.nth == 0 else None)
        run_export(_client(engine), tmp_path / "x.nxtenant", on_note=notes.append)
    assert any("limit of concurrent export sessions" in n and "5 s" in n for n in notes), notes


def test_the_old_engine_refusal_also_names_the_edge_allowlist(tmp_path: Path) -> None:
    with FakeEngine(standard_tables(), supported=False, release="0.1.191") as engine:
        with pytest.raises(ExportRefused) as err:
            run_export(_client(engine), tmp_path / "x.nxtenant")
    text = str(err.value)
    assert "tenant_export_supported" in text and "edge" in text, "a current engine behind an un-updated edge reads as old"


# -- the export format is read from the session header when /version has only the booleans ---------------------


def test_a_version_without_the_format_integer_and_a_format_1_header_succeeds(tmp_path: Path) -> None:
    with FakeEngine(standard_tables(), version_format_field=False) as engine:
        run_export(_client(engine), tmp_path / "x.nxtenant")
    verify_file(tmp_path / "x.nxtenant")


def test_a_header_with_an_unknown_format_is_refused_and_its_session_deleted(tmp_path: Path) -> None:
    with FakeEngine(standard_tables(), version_format_field=False, format_version=2) as engine:
        with pytest.raises(ExportRefused, match="format version 2"):
            run_export(_client(engine), tmp_path / "x.nxtenant")
        assert len(engine.opened) == 1 and engine.deleted == engine.opened
    assert _entries(tmp_path) == set()


# -- round 2 ------------------------------------------------------------------------------------------------------


@pytest.mark.parametrize("token", ["abc\ndef", "abc\r\ndef", "abc\x00def", "abc\x7fdef"])
def test_a_token_with_a_control_character_is_refused_before_any_request_and_never_printed(token: str, tmp_path: Path) -> None:
    with FakeEngine(standard_tables(), accepted={token}) as engine:
        with pytest.raises(ExportRefused) as err:
            run_export(_client(engine, Creds(token)), tmp_path / "x.nxtenant")
        assert engine.log == [], "not even /version was asked"
    text = str(err.value)
    assert "control character" in text and "service_token" in text
    assert "abc" not in text and "def" not in text, "the value is never in the message"
    assert _entries(tmp_path) == set()


def test_a_token_that_turns_bad_after_the_first_request_is_still_refused_not_quoted(tmp_path: Path) -> None:
    class Turns(Creds):
        def __init__(self) -> None:
            super().__init__("good")
            self.calls = 0

        def bearer(self) -> str:
            self.calls += 1
            return "good" if self.calls <= 2 else "bad\r\ntoken"

    with FakeEngine(standard_tables(), accepted={"good"}) as engine:
        with pytest.raises(ExportRefused) as err:
            run_export(_client(engine, Turns()), tmp_path / "x.nxtenant")
    assert "control character" in str(err.value) and "bad" not in str(err.value)


def test_the_transport_error_text_never_quotes_a_bearer_value() -> None:
    from nexus._log_safe_url import scrub_transport_error  # noqa: PLC0415

    for shown in (
        "Illegal header value b'Bearer abc\\r\\ndef'",
        "Illegal header value b'Bearer s3cr3t-token-value'",
        "bad header: Authorization: bearer  s3cr3t-token-value",
    ):
        out = scrub_transport_error(RuntimeError(shown), "https://api.example.com")
        assert "abc" not in out and "def" not in out and "s3cr3t" not in out, out
        assert "Bearer <redacted>" in out
    plain = scrub_transport_error(RuntimeError("connection refused"), "https://api.example.com")
    assert plain == "connection refused", "text with no bearer is returned as it was"


def test_an_interrupt_inside_the_files_close_still_removes_it(tmp_path: Path) -> None:
    partial = ce._Partial()
    ce._open_partial(tmp_path / "x.nxtenant", partial)
    assert len(_entries(tmp_path)) == 1
    real = partial.fh

    class InterruptedClose:
        def close(self) -> None:
            real.close()  # the handle is released (Windows cannot unlink an open file), then the interrupt lands
            raise KeyboardInterrupt

    partial.fh = InterruptedClose()
    with pytest.raises(KeyboardInterrupt):
        partial.discard()
    assert _entries(tmp_path) == set()
