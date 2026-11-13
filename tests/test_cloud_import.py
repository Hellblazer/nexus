# SPDX-License-Identifier: AGPL-3.0-or-later
"""``nx backup restore``, the client half (nexus-csayy.6, RDR-248): the session driver and the verb against a
stand-in for the engine's import routes served over a real socket (``tests/_fake_import_engine.py``).

Each test is one client behaviour of the RDR's Test Plan and Failure Modes: the file is verified before anything is
sent; parts are cut at row boundaries, each a whole COPY stream with its own SHA-256; a part is retried on a
transport failure, a 5xx or a hash refusal; ``part_in_flight`` waits and ``part_already_staged`` is success;
``restore_busy`` is retried every 60 s until the engine's bound; a lost connection is followed by a status read;
after "no such session" the marker decides; a 409 for a stale session deletes it once; a restore never reports
committed unless the engine said so (or the marker names the file). The round trip against the REAL engine is
``tests/test_nx_backup_cloud_roundtrip.py``.
"""

from __future__ import annotations

import hashlib
import json
import struct
from pathlib import Path

import pytest
from click.testing import CliRunner

from nexus.backup import cloud_import as ci
from nexus.backup.cloud_export import ExportClient, run_export
from nexus.backup.cloud_import import (
    ImportClient,
    RestoreFailed,
    RestoreOutcomeUnknown,
    RestoreRefused,
    run_restore,
)
from nexus.backup.tenant_file import count_copy_rows, index_file, iter_copy_tuples
from nexus.cli import main
from tests._fake_export_engine import FakeEngine, Reply, TableSpec
from tests._fake_import_engine import FakeImportEngine, IReq

_SIG = b"PGCOPY\n\xff\r\n\0"


class Creds:
    def __init__(self, *tokens: str) -> None:
        self.tokens = list(tokens or ("tok",))

    def bearer(self) -> str:
        return self.tokens[0]

    def rejected(self, sent: str) -> bool:
        if len(self.tokens) > 1 and sent == self.tokens[0]:
            self.tokens.pop(0)
            return True
        return False


class Clock:
    """The injected sleep and monotonic clock: a sleep advances time and nothing actually waits."""

    def __init__(self) -> None:
        self.now = 0.0
        self.sleeps: list[float] = []

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.now += seconds

    def monotonic(self) -> float:
        return self.now


def _client(engine: FakeImportEngine, clock: Clock, creds: Creds | None = None) -> ImportClient:
    return ImportClient(engine.base_url, creds or Creds(), sleep=clock.sleep, monotonic=clock.monotonic)


def _rows(n: int, size: int = 500, start: int = 0) -> list[list[bytes | None]]:
    return [[b"%d" % (start + i), (b"%d-" % (start + i)) + b"x" * size] for i in range(n)]


def _tables() -> list[TableSpec]:
    return [
        TableSpec("memory", "data", [("id", "bigint"), ("content", "text")], [_rows(13), _rows(5, start=13)]),
        TableSpec("frecency", "derived", [("key", "text")], [[[b"a"], [b"b"]]]),
        TableSpec("plans", "data", [("id", "bigint")], []),
    ]


@pytest.fixture
def backup_file(tmp_path: Path) -> Path:
    """A real ``.nxtenant`` file, taken by the real export driver from the export stand-in."""
    dest = tmp_path / "in.nxtenant"
    with FakeEngine(_tables()) as export_engine:
        run_export(ExportClient(export_engine.base_url, Creds(), sleep=lambda _s: None), dest)
    return dest


def _file_pages(path: Path) -> dict[str, list[bytes]]:
    """The COPY bodies of the file's pages by table, read with this test's own frame parser (not the client's)."""
    raw = path.read_bytes()
    pos, out = 10, {}
    while pos < len(raw):
        kind, length = struct.unpack_from(">cQ", raw, pos)
        payload = raw[pos + 9: pos + 9 + length]
        pos += 9 + length
        if kind == b"P":
            (mlen,) = struct.unpack_from(">I", payload, 0)
            out.setdefault(json.loads(payload[4: 4 + mlen])["table"], []).append(payload[4 + mlen:])
    return out


def _file_header_payload(path: Path) -> bytes:
    raw = path.read_bytes()
    kind, length = struct.unpack_from(">cQ", raw, 10)
    assert kind == b"H"
    return raw[19: 19 + length]


def _restore(engine: FakeImportEngine, path: Path, clock: Clock | None = None, **kw: object):
    clock = clock or Clock()
    notes: list[str] = []
    result = run_restore(_client(engine, clock), path, on_note=notes.append, poll_interval_s=2.0, **kw)  # type: ignore[arg-type]
    return result, clock, notes


def _tamper(path: Path, where: int) -> None:
    raw = bytearray(path.read_bytes())
    raw[where] ^= 0xFF
    path.write_bytes(bytes(raw))


def _engine_runs_the_request_then_drops(engine: FakeImportEngine, kind: str, nths: range, *, process_first: bool) -> None:
    """The answer to *kind* requests number *nths* is lost; with *process_first* the first of them reached the engine."""

    def entry(req: IReq) -> Reply | None:
        if req.kind != kind or req.nth not in nths:
            return None
        if process_first and req.nth == nths[0]:
            engine._answer(req)
        return Reply(drop=True)

    engine.script.append(entry)


# -- the verb's gate and the verify-before-upload rule ---------------------------------------------------------


def test_an_engine_without_the_import_routes_is_refused_naming_the_release_and_nothing_is_opened(backup_file: Path) -> None:
    with FakeImportEngine(supported=False, release="0.1.191") as engine:
        with pytest.raises(RestoreRefused, match=r"0\.1\.192") as err:
            _restore(engine, backup_file)
        assert "0.1.191" in str(err.value) and "import" in str(err.value)
        assert engine.requests("open") == [], "no import request is sent to an engine that has no import routes"


@pytest.mark.parametrize("damage", ["a byte in a page", "the trailer cut off", "a byte in the header"])
def test_a_file_that_fails_verification_is_never_uploaded(backup_file: Path, damage: str) -> None:
    size = backup_file.stat().st_size
    if damage == "a byte in a page":
        _tamper(backup_file, size // 2)
    elif damage == "the trailer cut off":
        backup_file.write_bytes(backup_file.read_bytes()[: size - 40])
    else:
        _tamper(backup_file, 40)
    with FakeImportEngine() as engine:
        with pytest.raises(RestoreRefused, match="does not verify"):
            _restore(engine, backup_file)
        assert [r.kind for r in engine.log if r.kind not in ("version",)] == [], "not one import request left the client"


# -- open, parts, close -----------------------------------------------------------------------------------------


def test_the_open_carries_the_file_header_its_sha256_and_the_row_bytes(backup_file: Path) -> None:
    with FakeImportEngine() as engine:
        _restore(engine, backup_file)
        body = json.loads(engine.requests("open")[0].body)
    header_payload = _file_header_payload(backup_file)
    assert body["header_sha256"] == hashlib.sha256(header_payload).hexdigest()
    assert body["row_bytes"] == sum(len(b) for pages in _file_pages(backup_file).values() for b in pages)
    expected = json.loads(header_payload)
    assert {k: v for k, v in body.items() if k not in ("header_sha256", "row_bytes")} == expected


def test_each_page_is_cut_at_row_boundaries_into_whole_copy_streams_with_their_own_sha256(backup_file: Path) -> None:
    with FakeImportEngine(part_bytes=2048) as engine:
        _restore(engine, backup_file)
        session = engine.open_session()
        parts = engine.requests("part")
        memory = engine.staged_rows("memory", session)
    # every part on the wire: a whole COPY stream, within the engine's part size, carrying the SHA-256 of its own bytes
    for req in parts:
        assert req.body.startswith(_SIG) and req.body.endswith(struct.pack(">h", -1))
        assert len(req.body) <= 2048
        assert req.headers["X-Nexus-Part-Sha256"] == hashlib.sha256(req.body).hexdigest()
        assert count_copy_rows(req.body, fields=2 if req.query["table"] == "memory" else None) >= 1
    # parts are numbered from 0 within the table, in order, and staged once each
    assert [p for p, _ in memory] == list(range(len(memory))) and len(memory) > 2
    assert [int(r.query["part"]) for r in parts if r.query["table"] == "memory"] == list(range(len(memory)))
    # no row is split, lost or reordered: the staged rows are the file's rows
    original = [[bytes(f) if f else None for f in t] for body in _file_pages(backup_file)["memory"] for t in iter_copy_tuples(body)]
    staged = [[bytes(f) if f else None for f in t] for _, body in memory for t in iter_copy_tuples(body)]
    assert staged == original and len(original) == 18


def test_tables_close_at_the_trailers_count_in_the_engines_load_order_and_skipped_tables_are_not_sent(backup_file: Path) -> None:
    with FakeImportEngine(part_bytes=2048, skipped={"frecency": "rebuildable"}) as engine:
        result, _, _ = _restore(engine, backup_file)
        closes = [(r.query["table"], int(r.query["rows"])) for r in engine.requests("close")]
        sent_tables = {r.query["table"] for r in engine.requests("part")}
    assert closes == [("memory", 18), ("plans", 0)], "the trailer's row count per table; the skipped table is not closed"
    assert sent_tables == {"memory"}, "an empty table sends no part and a skipped one none either"
    assert result.skipped_at_open == {"frecency": "rebuildable"}


# -- retries ----------------------------------------------------------------------------------------------------


@pytest.mark.parametrize("fault", ["connection dropped", "hash refused", "engine 500"])
def test_a_part_that_fails_is_sent_again_and_staged_once(backup_file: Path, fault: str) -> None:
    def entry(req: IReq) -> Reply | None:
        if req.kind != "part" or req.nth != 1:
            return None
        return {
            "connection dropped": Reply(drop=True),
            "hash refused": Reply(400, {"error": "the part's SHA-256 is not the one sent", "reason": "part_sha256_mismatch"}),
            "engine 500": Reply(500, {"error": "boom", "reason": "internal"}),
        }[fault]

    with FakeImportEngine(part_bytes=2048) as engine:
        engine.script.append(entry)
        clock = Clock()
        _restore(engine, backup_file, clock)
        parts = [(r.query["table"], r.query["part"]) for r in engine.requests("part")]
        staged = engine.staged_rows("memory", engine.open_session())
    assert parts.count(("memory", "1")) == 2, "part 1 went out twice"
    assert [p for p, _ in staged] == list(range(len(staged))), "and was staged once"
    assert clock.sleeps and clock.sleeps[0] == 1.0, "after the first rung of the backoff ladder"


def test_a_part_that_keeps_failing_ends_the_run_and_deletes_the_session(backup_file: Path) -> None:
    with FakeImportEngine(part_bytes=2048) as engine:
        engine.script.append(lambda req: Reply(500, {"error": "boom", "reason": "internal"}) if req.kind == "part" else None)
        with pytest.raises(RestoreFailed, match="failed 5 times"):
            _restore(engine, backup_file)
        assert len(engine.deleted) == 1 and not engine.sessions, "a failure before the commit deletes the staged session"
        assert engine.requests("commit") == []


def test_part_in_flight_waits_and_asks_again_without_taking_a_retry(backup_file: Path) -> None:
    def entry(req: IReq) -> Reply | None:
        if req.kind == "part" and req.nth < 3:
            return Reply(409, {"error": "another request is running", "reason": "part_in_flight"}, {"Retry-After": "1"})
        return None

    with FakeImportEngine(part_bytes=2048) as engine:
        engine.script.append(entry)
        result, clock, _ = _restore(engine, backup_file)
    assert clock.sleeps[:3] == [1.0, 1.0, 1.0] and result.outcome.source == "status"


def test_part_in_flight_that_never_clears_ends_the_run_naming_it(backup_file: Path) -> None:
    with FakeImportEngine(part_bytes=2048) as engine:
        engine.script.append(lambda req: Reply(409, {"error": "busy", "reason": "part_in_flight"}, {"Retry-After": "1"}) if req.kind == "part" else None)
        with pytest.raises(RestoreFailed, match="part_in_flight"):
            _restore(engine, backup_file)
        assert len(engine.deleted) == 1


def test_a_part_the_engine_says_is_already_staged_is_a_success(backup_file: Path) -> None:
    def entry(req: IReq) -> Reply | None:
        if req.kind == "part" and req.nth == 1:
            engine._answer(req)   # the engine has the part (the answer to an earlier send was lost)...
            return Reply(409, {"error": "already staged", "reason": "part_already_staged"})
        return None

    with FakeImportEngine(part_bytes=2048) as engine:
        engine.script.append(entry)
        result, _, _ = _restore(engine, backup_file)
        assert len(engine.requests("part")) == len({(r.query["table"], r.query["part"]) for r in engine.requests("part")}), "not sent again"
    assert result.outcome.source == "status"


def test_a_token_that_expires_mid_session_is_replaced_and_the_same_request_repeated(backup_file: Path) -> None:
    with FakeImportEngine(accepted={"new"}) as engine:
        clock = Clock()
        result = run_restore(ImportClient(engine.base_url, Creds("old", "new"), sleep=clock.sleep, monotonic=clock.monotonic),
                             backup_file, poll_interval_s=2.0)
        assert [r.bearer for r in engine.requests("open")] == ["old", "new"]
    assert result.outcome.source == "status"


def test_a_root_token_is_refused_saying_which_token_to_use(backup_file: Path) -> None:
    with FakeImportEngine() as engine:
        engine.script.append(lambda req: Reply(403, {"error": "no", "reason": "import_scope_forbidden"}) if req.kind == "open" else None)
        with pytest.raises(RestoreRefused, match="tenant or data token"):
            _restore(engine, backup_file)


# -- the stale session on open ----------------------------------------------------------------------------------


def test_a_409_naming_an_import_session_deletes_it_once_and_reopens(backup_file: Path) -> None:
    from tests._fake_import_engine import _Session  # noqa: PLC0415

    with FakeImportEngine() as engine:
        engine.sessions["stale-1"] = _Session("stale-1", {}, [])
        result, _, _ = _restore(engine, backup_file)
        deletes = engine.requests("delete")
        assert [d.path for d in deletes] == ["/v1/tenant/import/stale-1"] and engine.deleted == ["stale-1"]
        assert len(engine.requests("open")) == 2
    assert result.outcome.source == "status"


def test_a_409_naming_an_export_session_deletes_it_through_the_export_route(backup_file: Path) -> None:
    with FakeImportEngine() as engine:
        engine.tenant_has_export_session = "exp-1"

        def entry(req: IReq) -> Reply | None:
            if req.kind == "delete" and req.path == "/v1/tenant/export/exp-1":
                engine.tenant_has_export_session = None
                return Reply(body={"closed": True})
            return None

        engine.script.append(entry)
        _restore(engine, backup_file)
        assert [d.path for d in engine.requests("delete")] == ["/v1/tenant/export/exp-1"]


def test_a_stale_session_that_will_not_clear_is_refused_after_one_delete(backup_file: Path) -> None:
    from tests._fake_import_engine import _Session  # noqa: PLC0415

    with FakeImportEngine() as engine:
        engine.script.append(lambda req: Reply(409, {"error": "x", "reason": "session_exists", "session_id": "stuck", "session_kind": "import"}) if req.kind == "open" else None)
        engine.sessions["stuck"] = _Session("stuck", {}, [])
        with pytest.raises(RestoreRefused, match="cannot clear"):
            _restore(engine, backup_file)
        assert len(engine.requests("delete")) == 1, "deleted once, not in a loop"


def test_a_stale_import_that_is_loading_is_left_alone_because_deleting_it_would_cancel_the_load(backup_file: Path) -> None:
    from tests._fake_import_engine import _Session  # noqa: PLC0415

    with FakeImportEngine() as engine:
        engine.sessions["busy"] = _Session("busy", {}, [], state="loading", status="loading")
        with pytest.raises(RestoreRefused, match="loading on the engine"):
            _restore(engine, backup_file)
        assert engine.requests("delete") == []


# -- open refusals --------------------------------------------------------------------------------------------------


def test_a_tenant_that_is_not_empty_is_refused_and_a_file_already_restored_says_so(backup_file: Path) -> None:
    refuse = Reply(409, {"error": "tenant t holds rows in memory", "reason": "tenant_not_empty", "table": "memory"})
    with FakeImportEngine() as engine:
        engine.script.append(lambda req: refuse if req.kind == "open" else None)
        with pytest.raises(RestoreRefused, match="not empty") as err:
            _restore(engine, backup_file)
        assert "already restored" not in str(err.value)
        engine.marker = {"header_sha256": index_file(backup_file).header_sha256, "session_id": "s-9", "committed_at": "T"}
        with pytest.raises(RestoreRefused, match="already restored into this tenant"):
            _restore(engine, backup_file)
        assert engine.requests("part") == []


def test_an_open_at_the_session_cap_waits_the_retry_after_then_opens(backup_file: Path) -> None:
    def entry(req: IReq) -> Reply | None:
        if req.kind == "open" and req.nth < 2:
            return Reply(503, {"error": "full", "reason": "import_capacity_full"}, {"Retry-After": "7"})
        return None

    with FakeImportEngine() as engine:
        engine.script.append(entry)
        _, clock, _ = _restore(engine, backup_file)
    assert clock.sleeps[:2] == [7.0, 7.0]


# -- commit: restore_busy, the status poll, lost connections ------------------------------------------------------


def test_restore_busy_keeps_the_upload_and_the_commit_is_retried_every_60_seconds(backup_file: Path) -> None:
    with FakeImportEngine(busy_commits=2) as engine:
        result, clock, notes = _restore(engine, backup_file)
        assert len(engine.requests("open")) == 1 and len(engine.requests("commit")) == 3, "one session, committed on the third try"
        assert engine.requests("part") and len({r.path for r in engine.requests("part")}) == 1
        assert engine.deleted == []
    assert clock.sleeps.count(60.0) == 2 and any("Another restore is loading" in n for n in notes)
    assert result.outcome.source == "status"


def test_restore_busy_until_the_engine_closes_the_session_at_its_bound_reports_the_bound_and_does_not_commit(backup_file: Path) -> None:
    bound = "failed: restore-wait bound: no load slot in 60 seconds"
    answered = {"n": 0}

    def entry(req: IReq) -> Reply | None:
        if req.kind == "commit" and req.nth >= 2:
            answered["n"] += 1
            return Reply(200, {"session_id": "x", "state": "failed", "status": bound, "notes": []})   # a 200 that carries failed
        return None

    with FakeImportEngine(busy_commits=99) as engine:
        engine.script.append(entry)
        with pytest.raises(RestoreFailed, match="restore-wait bound"):
            _restore(engine, backup_file)
    assert answered["n"] == 1


def test_a_client_that_waits_past_the_restore_wait_bound_gives_up_and_deletes_the_session(backup_file: Path) -> None:
    with FakeImportEngine(busy_commits=10_000, load_max_seconds=300) as engine:
        with pytest.raises(RestoreFailed, match="load slot"):
            _restore(engine, backup_file)
        assert len(engine.deleted) == 1, "a bounded failure that cannot be loading deletes the session"


@pytest.mark.parametrize("where", [
    "the commit answers 200 failed", "the status answers 200 failed", "the status text says failed under another state",
])
def test_a_200_that_carries_failed_is_a_failure_never_a_commit(backup_file: Path, where: str) -> None:
    state = "loading" if "another state" in where else "failed"
    failed = {"session_id": "x", "state": state, "status": "failed: tenant not empty: memory", "notes": []}

    def entry(req: IReq) -> Reply | None:
        if where.startswith("the commit") and req.kind == "commit":
            return Reply(200, failed)
        if where.startswith("the status") and req.kind == "status":
            return Reply(200, failed)
        return None

    with FakeImportEngine() as engine:
        engine.script.append(entry)
        with pytest.raises(RestoreFailed, match="tenant not empty: memory") as err:
            _restore(engine, backup_file)
    assert "committed" not in str(err.value).replace("not commit", "")


def test_the_status_poll_follows_the_load_until_it_commits_and_the_report_is_returned(backup_file: Path) -> None:
    report = {"tables": [{"table": "memory", "staged": 18, "inserted": 18, "seeded_skipped": 0, "dropped": 0}], "soft_references": []}
    with FakeImportEngine(load_polls=3, report=report) as engine:
        result, clock, notes = _restore(engine, backup_file)
        assert len(engine.requests("status")) == 3, "the commit answer was the first read; three more followed it"
        assert engine.deleted == [], "a session that committed is never deleted"
    assert result.outcome.report == report and result.outcome.source == "status"
    assert any(n.startswith("Loading:") for n in notes)


def test_a_commit_whose_answer_is_lost_is_followed_by_a_status_read(backup_file: Path) -> None:
    with FakeImportEngine(load_polls=1) as engine:
        _engine_runs_the_request_then_drops(engine, "commit", range(0, 5), process_first=True)
        result, _, _ = _restore(engine, backup_file)
        assert len(engine.requests("commit")) == 5 and engine.requests("status"), "five lost answers, then the status route"
        assert engine.deleted == [], "a commit that may have started the load never deletes the session"
    assert result.outcome.source == "status"


def test_a_commit_that_never_reached_the_engine_is_sent_again_when_the_status_says_staging(backup_file: Path) -> None:
    with FakeImportEngine() as engine:
        _engine_runs_the_request_then_drops(engine, "commit", range(0, 5), process_first=False)
        result, _, _ = _restore(engine, backup_file)
        assert len(engine.requests("commit")) == 6
    assert result.outcome.source == "status"


def test_a_lost_connection_during_the_poll_is_followed_by_more_status_reads(backup_file: Path) -> None:
    with FakeImportEngine(load_polls=2) as engine:
        _engine_runs_the_request_then_drops(engine, "status", range(0, 5), process_first=False)
        result, _, _ = _restore(engine, backup_file)
    assert result.outcome.source == "status"


def test_a_status_that_cannot_be_read_to_the_end_of_the_load_bound_is_an_unknown_outcome_never_committed(backup_file: Path) -> None:
    with FakeImportEngine(load_max_seconds=60) as engine:
        engine.script.append(lambda req: Reply(drop=True) if req.kind == "status" else None)
        with pytest.raises(RestoreOutcomeUnknown, match="not known|could not be read"):
            _restore(engine, backup_file)
        assert engine.deleted == []


# -- after "no such session": the marker ------------------------------------------------------------------------------


def _forget_the_session_after_commit(engine: FakeImportEngine) -> None:
    """The engine restarts as the load starts: status and commit now answer 404."""

    def entry(req: IReq) -> Reply | None:
        if req.kind == "status" and req.nth == 0:
            engine.restart()
        return None

    engine.script.append(entry)


def test_after_no_such_session_a_marker_naming_this_file_reports_committed(backup_file: Path) -> None:
    sha = index_file(backup_file).header_sha256
    with FakeImportEngine() as engine:
        engine.marker = {"header_sha256": sha, "session_id": "s", "committed_at": "T", "row_counts": {"memory": 18}}
        _forget_the_session_after_commit(engine)
        result, _, _ = _restore(engine, backup_file)
    assert result.outcome.source == "marker" and result.outcome.marker is not None


@pytest.mark.parametrize("marker", [None, {"header_sha256": "f" * 64, "session_id": "other"}], ids=["no marker", "another file's marker"])
def test_after_no_such_session_a_marker_that_does_not_name_this_file_is_a_rollback(backup_file: Path, marker: dict | None) -> None:
    with FakeImportEngine() as engine:
        engine.marker = marker
        _forget_the_session_after_commit(engine)
        with pytest.raises(RestoreFailed, match="did not commit") as err:
            _restore(engine, backup_file)
    assert "The tenant is as it was" in str(err.value)


def test_after_no_such_session_an_unreadable_marker_is_an_unknown_outcome(backup_file: Path) -> None:
    with FakeImportEngine() as engine:
        _forget_the_session_after_commit(engine)
        engine.script.append(lambda req: Reply(drop=True) if req.kind == "marker" else None)
        with pytest.raises(RestoreOutcomeUnknown, match="marker could not be read"):
            _restore(engine, backup_file)


def test_a_load_the_engine_could_not_confirm_is_decided_by_the_marker(backup_file: Path) -> None:
    text = "failed: the commit was not confirmed (connection reset); read /v1/tenant/import-marker to learn whether it landed"
    unconfirmed = Reply(200, {"session_id": "x", "state": "failed", "status": text, "notes": []})
    sha = index_file(backup_file).header_sha256
    with FakeImportEngine() as engine:
        engine.script.append(lambda req: unconfirmed if req.kind == "status" else None)
        engine.marker = {"header_sha256": sha, "session_id": "s", "committed_at": "T"}
        assert _restore(engine, backup_file)[0].outcome.source == "marker"
    with FakeImportEngine() as engine:
        engine.script.append(lambda req: unconfirmed if req.kind == "status" else None)
        with pytest.raises(RestoreFailed, match="rolled the restore back"):
            _restore(engine, backup_file)


# -- cleanup on a stop ------------------------------------------------------------------------------------------------


def test_an_interrupt_during_the_upload_deletes_the_staged_session(backup_file: Path) -> None:
    def stop(*_: object) -> None:
        raise KeyboardInterrupt

    with FakeImportEngine(part_bytes=2048) as engine:
        with pytest.raises(KeyboardInterrupt):
            run_restore(_client(engine, Clock()), backup_file, on_progress=stop)
        assert len(engine.deleted) == 1 and engine.requests("commit") == []


def test_an_interrupt_while_the_load_runs_leaves_the_load_alone(backup_file: Path) -> None:
    notes: list[str] = []

    def note(msg: str) -> None:
        notes.append(msg)
        if msg.startswith("Loading:"):
            raise KeyboardInterrupt

    with FakeImportEngine(load_polls=5) as engine:
        with pytest.raises(KeyboardInterrupt):
            run_restore(_client(engine, Clock()), backup_file, on_note=note, poll_interval_s=2.0)
        assert engine.deleted == [], "a DELETE during the load would cancel it"
    assert any("may be loading" in n for n in notes)


# -- the verb -------------------------------------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))
    return home


def _point_at(monkeypatch: pytest.MonkeyPatch, engine: FakeImportEngine) -> None:
    monkeypatch.setenv("NX_SERVICE_URL", engine.base_url)
    monkeypatch.setenv("NX_SERVICE_TOKEN", "tok")
    monkeypatch.setattr(ci, "POLL_INTERVAL_S", 0.0)


def _flat(text: str) -> str:
    return " ".join(text.split())


def test_the_verb_prints_counts_by_class_skips_negations_drops_and_notes(
    cloud_mode: None, monkeypatch: pytest.MonkeyPatch, backup_file: Path,
) -> None:
    report = {
        "tables": [
            {"table": "memory", "staged": 18, "inserted": 18, "seeded_skipped": 0, "dropped": 0},
            {"table": "plans", "staged": 0, "inserted": 0, "seeded_skipped": 0, "dropped": 0},
            {"table": "taxonomy_centroids", "staged": 5, "inserted": 2, "seeded_skipped": 0, "dropped": 3},
            {"table": "catalog_meta", "staged": 2, "inserted": 1, "seeded_skipped": 1, "dropped": 0},
        ],
        "soft_references": [{"column": "memory_summaries.source_ids", "remapped": 4, "passed_through": 1, "negated": 2}],
        "skipped_tables": {"search_telemetry": "the target already holds rows for it"},
        "skipped_columns": ["memory.legacy_flag"],
    }
    with FakeImportEngine(report=report, notes=["the file's schema_changeset_count is 600 and this engine's is 604; the columns decide"]) as engine:
        _point_at(monkeypatch, engine)
        result = CliRunner().invoke(main, ["backup", "restore", str(backup_file)])
    assert result.exit_code == 0, result.output
    out = _flat(result.output)
    assert f"Restore committed: {backup_file}" in out
    assert "data: 21 rows loaded in 4 tables" in out
    assert "taxonomy_centroids: 3 rows dropped" in out
    assert "Skipped table search_telemetry: the target already holds rows for it" in out
    assert "Skipped column memory.legacy_flag" in out
    assert "memory_summaries.source_ids: 4 remapped, 1 passed through, 2 negated" in out
    assert "Note: the file's schema_changeset_count is 600" in out
    assert "Kept, not loaded again: 1 row the engine had already written" in out


def test_the_verb_refuses_a_damaged_file_with_a_nonzero_exit_and_sends_nothing(
    cloud_mode: None, monkeypatch: pytest.MonkeyPatch, backup_file: Path,
) -> None:
    _tamper(backup_file, backup_file.stat().st_size // 2)
    with FakeImportEngine() as engine:
        _point_at(monkeypatch, engine)
        result = CliRunner().invoke(main, ["backup", "restore", str(backup_file)])
        assert engine.requests("open") == []
    assert result.exit_code != 0 and "does not verify" in _flat(result.output)


def test_the_verb_reports_a_rolled_back_load_as_a_failure_and_never_as_committed(
    cloud_mode: None, monkeypatch: pytest.MonkeyPatch, backup_file: Path,
) -> None:
    with FakeImportEngine() as engine:
        _point_at(monkeypatch, engine)
        engine.script.append(lambda req: Reply(200, {"session_id": "x", "state": "failed", "status": "failed: load past its bound", "notes": []}) if req.kind == "commit" else None)
        result = CliRunner().invoke(main, ["backup", "restore", str(backup_file)])
    assert result.exit_code == 1
    assert "Restore committed" not in result.output and "load past its bound" in _flat(result.output)


def test_backup_without_a_subcommand_still_runs_the_backup_and_the_old_options_work() -> None:
    result = CliRunner().invoke(main, ["backup", "--help"])
    assert result.exit_code == 0 and "restore" in result.output and "--check" in result.output and "--to" in result.output
    clash = CliRunner().invoke(main, ["backup", "--to", "x", "restore", "f"])
    assert clash.exit_code != 0


def test_the_engine_is_asked_for_nothing_but_the_documented_routes(backup_file: Path) -> None:
    with FakeImportEngine(part_bytes=2048) as engine:
        _restore(engine, backup_file)
        seen = {(r.method, r.kind) for r in engine.log}
    assert seen == {("GET", "version"), ("POST", "open"), ("PUT", "part"), ("POST", "close"), ("POST", "commit"), ("GET", "status")}
