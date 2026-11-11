# SPDX-License-Identifier: AGPL-3.0-or-later
"""nexus-f3yqd: the client for the engine's user-started chash CHECK VALIDATE job and the manifest repair.

Driven through a real ``HttpCatalogClient`` against a scripted fake engine (``ThreadingHTTPServer`` on port 0), so the
requests take the client's own transport, auth and the unprefixed mixin calls. The fake answers the shapes engine
0.1.190's handlers document (``ValidateConstraintsHandler``, ``ChashConformanceHandler``).
"""

from __future__ import annotations

import inspect
import json
import threading
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest
from click.testing import CliRunner

import nexus.health as health
from nexus.catalog.http_catalog_client import HttpCatalogClient
from nexus.cli import main
from nexus.commands import doctor
from nexus.commands.t3_cmds import chash_checks
from nexus.db import chash_constraints_job as job
from tests._settle import FakeClock


class _Engine:
    def __init__(self) -> None:
        self.flags: dict = {"chash_constraints_validate_job_supported": True,
                            "chash_conformance_constraints_supported": True}
        self.all_validated = False
        self.start_status = 202
        self.start_body: dict = {"job_id": "j1", "created": True}
        self.polls: list[tuple[int, dict]] = []  # answered in order; the last repeats
        self.repair_body: dict = {"documents": [], "count": 0, "capped": False, "dry_run": True}
        self.requests: list[tuple[str, str, dict | None]] = []


def _handler(engine: _Engine) -> type[BaseHTTPRequestHandler]:
    class H(BaseHTTPRequestHandler):
        def log_message(self, *_a) -> None:
            pass

        def _send(self, status: int, body: dict) -> None:
            data = json.dumps(body).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def do_GET(self) -> None:  # noqa: N802 — http.server's name
            engine.requests.append(("GET", self.path, None))
            if self.path == "/version":
                return self._send(200, engine.flags)
            if self.path == job.CONFORMANCE_PATH:
                return self._send(200, {"constraints": [], "all_validated": engine.all_validated})
            if self.path.startswith(job.JOB_PATH + "/"):
                status, body = engine.polls.pop(0) if len(engine.polls) > 1 else engine.polls[0]
                return self._send(status, body)
            self._send(404, {"error": "not found"})

        def do_POST(self) -> None:  # noqa: N802 — http.server's name
            n = int(self.headers.get("Content-Length") or 0)
            body = json.loads(self.rfile.read(n) or b"{}")
            engine.requests.append(("POST", self.path, body))
            if self.path == job.JOB_PATH:
                return self._send(engine.start_status, engine.start_body)
            if self.path == "/v1/catalog" + job.REPAIR_PATH:
                return self._send(200, engine.repair_body)
            self._send(404, {"error": "not found"})

    return H


@pytest.fixture
def engine() -> Iterator[_Engine]:
    yield _Engine()


@pytest.fixture
def client(engine: _Engine) -> Iterator[HttpCatalogClient]:
    server = ThreadingHTTPServer(("127.0.0.1", 0), _handler(engine))
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        with HttpCatalogClient(base_url=f"http://127.0.0.1:{server.server_port}", tenant="t", _token="tok") as c:
            yield c
    finally:
        server.shutdown()


def _running(done: int, total: int = 4) -> tuple[int, dict]:
    return 200, {"job_id": "j1", "state": "running", "tables_done": done, "tables_total": total,
                 "current": {"table": "nexus.chunks_m1_t1", "bytes": 2_000_000_000}}


def _done(**over) -> tuple[int, dict]:
    body = {"job_id": "j1", "state": "done", "dry_run": False, "tables_done": 4, "tables_total": 4,
            "validated": [{"table": "nexus.chunks", "constraint": "chunks_chash_len_check"}],
            "refused": [], "deferred": [], "all_validated": True}
    return 200, body | over


def _clock() -> FakeClock:
    return FakeClock(auto_advance=True)


def test_an_engine_without_the_flag_is_too_old_and_nothing_is_posted(client, engine) -> None:
    engine.flags = {}
    with pytest.raises(job.ValidateJobError, match="0.1.190"):
        job.start(client, dry_run=False)
    assert not [r for r in engine.requests if r[0] == "POST"]


def test_a_tenant_token_is_told_to_ask_the_operator(client, engine) -> None:
    engine.start_status, engine.start_body = 403, {"error": "operator privilege required"}
    with pytest.raises(job.ValidateJobError) as exc:
        job.start(client, dry_run=False)
    assert str(exc.value) == job.ASK_OPERATOR


def test_start_and_wait_reports_progress_and_reads_all_validated(client, engine) -> None:
    engine.polls = [_running(1), _running(3), _done()]
    seen: list[str] = []
    started = job.start(client, dry_run=False)
    body = job.wait(client, started["job_id"], on_progress=lambda b: seen.append(job.progress_line(b)), clock=_clock())
    assert ("POST", job.JOB_PATH, {"dry_run": False}) in engine.requests
    assert seen == ["Validating the chash CHECKs: 1 of 4 tables, on nexus.chunks_m1_t1 (2.0 GB).",
                    "Validating the chash CHECKs: 3 of 4 tables, on nexus.chunks_m1_t1 (2.0 GB)."]
    assert job.verdict(body).ok is True


def test_done_is_not_success_when_a_check_is_refused_and_the_next_step_is_named(engine) -> None:
    refused = {"table": "nexus.catalog_document_chunks", "constraint": "cdc_chash_len_check",
               "reason": "check_violation", "precheck": "unavailable", "bad_rows": None,
               "next_step": "nx t3 repair-chash --apply"}
    v = job.verdict(_done(refused=[refused], all_validated=False)[1])
    assert v.ok is False and "repair" in v.summary
    assert any("nx t3 repair-chash --apply" in line for line in v.lines)


def test_deferred_means_rerun(engine) -> None:
    v = job.verdict(_done(deferred=[{"table": "nexus.chunks", "constraint": "c", "reason": "lock_timeout"}],
                          all_validated=False)[1])
    assert v.ok is False and "run this again" in v.summary


def test_done_with_all_validated_false_and_nothing_listed_is_not_success() -> None:
    assert job.verdict(_done(all_validated=None)[1]).ok is False


def test_a_failed_job_is_not_success() -> None:
    v = job.verdict({"state": "failed", "error": "lost the admin connection"})
    assert v.ok is False and "lost the admin connection" in v.summary


def test_a_poll_the_engine_no_longer_holds_says_start_again(client, engine) -> None:
    engine.polls = [(404, {"error": "no such constraint validate job"})]
    with pytest.raises(job.ValidateJobError, match="start it again"):
        job.status(client, "gone")


def test_a_start_with_the_other_dry_run_names_the_running_job(client, engine) -> None:
    engine.start_status, engine.start_body = 409, {"error": "job j9 is running with dry_run true", "job_id": "j9"}
    with pytest.raises(job.ValidateJobError, match="already running"):
        job.start(client, dry_run=False)


def test_a_dry_run_changes_nothing_and_says_so() -> None:
    v = job.verdict(_done(dry_run=True, would_validate=[{"table": "nexus.chunks", "constraint": "c"}])[1])
    assert v.ok is True and "nothing was changed" in v.summary


class TestDoctorFix:
    def _run(self, client, **kw) -> tuple[bool, list[str]]:
        lines: list[str] = []
        ok = job.doctor_fix_lines(client, dry_run=kw.get("dry_run", False), echo=lines.append)
        return ok, lines

    def test_already_validated_starts_nothing(self, client, engine) -> None:
        engine.all_validated = True
        ok, lines = self._run(client)
        assert ok and "already validated" in lines[0]
        assert not [r for r in engine.requests if r[0] == "POST"]

    def test_not_validated_starts_and_waits(self, client, engine, monkeypatch) -> None:
        monkeypatch.setattr(job, "POLL_EVERY_S", 0.0)
        engine.polls = [_running(2), _done()]
        ok, lines = self._run(client)
        assert ok is True
        assert lines[0].startswith("chash CHECKs: validate job j1 started")
        assert lines[-1] == "chash CHECKs: every chash CHECK is validated (1 validated by this run)"

    def test_a_refusal_fails_the_fix(self, client, engine, monkeypatch) -> None:
        monkeypatch.setattr(job, "POLL_EVERY_S", 0.0)
        engine.polls = [_done(refused=[{"table": "t", "constraint": "c", "reason": "check_violation"}],
                              all_validated=False)]
        ok, _lines = self._run(client)
        assert ok is False

    def test_a_managed_install_is_told_to_ask_the_operator_and_does_not_fail(self, client, engine) -> None:
        engine.start_status, engine.start_body = 403, {"error": "operator privilege required"}
        ok, lines = self._run(client)
        assert ok is True and lines == [f"chash CHECKs: {job.ASK_OPERATOR}"]

    def test_an_old_engine_is_named_and_does_not_fail(self, client, engine) -> None:
        engine.flags = {"chash_conformance_constraints_supported": True}
        ok, lines = self._run(client)
        assert ok is True and "0.1.190" in lines[0]


class TestRepair:
    def test_an_engine_without_the_route_is_too_old(self, client, engine) -> None:
        with pytest.raises(job.ValidateJobError, match="no chash manifest repair"):
            job.repair(client, doc_id="1.2.3", dry_run=True)

    def test_repair_posts_the_document_and_reads_the_answer(self, client, engine) -> None:
        engine.flags["catalog_manifest_repair_chash_supported"] = True
        engine.repair_body = {"documents": [{"doc_id": "1.2.3", "bad_rows": 4, "marked_for_reindex": False}],
                              "count": 1, "capped": False, "dry_run": True}
        out = job.repair(client, doc_id="1.2.3", dry_run=True, limit=10)
        assert out["documents"][0]["bad_rows"] == 4
        assert ("POST", "/v1/catalog" + job.REPAIR_PATH, {"doc_id": "1.2.3", "dry_run": True, "limit": 10}) in engine.requests


class TestVerbs:
    """`nx t3 validate-chash-checks` and `nx t3 repair-chash` through the CLI, against the fake engine."""

    @pytest.fixture(autouse=True)
    def _use_fake(self, client, monkeypatch) -> None:
        monkeypatch.setattr(chash_checks, "_client", lambda: client)
        monkeypatch.setattr(client, "close", lambda: None)
        monkeypatch.setattr(job, "POLL_EVERY_S", 0.0)

    def _invoke(self, *args: str):
        return CliRunner().invoke(main, ["t3", *args])

    def test_validates_and_exits_0(self, engine) -> None:
        engine.polls = [_running(1), _done()]
        r = self._invoke("validate-chash-checks")
        assert r.exit_code == 0, r.output
        assert "Validate job j1 started" in r.stdout and "every chash CHECK is validated" in r.stdout

    def test_a_refusal_exits_1_and_names_the_repair(self, engine) -> None:
        engine.polls = [_done(refused=[{"table": "t", "constraint": "c", "reason": "check_violation",
                                        "next_step": "nx t3 repair-chash --all --apply"}], all_validated=False)]
        r = self._invoke("validate-chash-checks")
        assert r.exit_code == 1 and "nx t3 repair-chash --all --apply" in r.stdout

    def test_an_old_engine_exits_3(self, engine) -> None:
        engine.flags = {}
        r = self._invoke("validate-chash-checks")
        assert r.exit_code == 3 and "0.1.190" in r.stderr

    def test_no_wait_prints_the_poll_command(self, engine) -> None:
        r = self._invoke("validate-chash-checks", "--no-wait")
        assert r.exit_code == 0 and "nx t3 validate-chash-checks --job j1" in r.stdout
        assert not [q for q in engine.requests if q[0] == "GET" and q[1].startswith(job.JOB_PATH + "/")]

    def test_json_is_the_final_report(self, engine) -> None:
        engine.polls = [_done()]
        r = self._invoke("validate-chash-checks", "--json")
        assert r.exit_code == 0 and json.loads(r.stdout)["all_validated"] is True

    def test_repair_is_a_dry_run_unless_applied(self, engine) -> None:
        engine.flags["catalog_manifest_repair_chash_supported"] = True
        engine.repair_body = {"documents": [{"doc_id": "1.2.3", "bad_rows": 2, "marked_for_reindex": False}],
                              "count": 1, "capped": False, "dry_run": True}
        r = self._invoke("repair-chash", "1.2.3")
        assert r.exit_code == 0 and "nothing was changed" in r.stdout
        assert engine.requests[-1][2]["dry_run"] is True
        r = self._invoke("repair-chash", "1.2.3", "--apply")
        assert engine.requests[-1][2]["dry_run"] is False and "Repaired 1 document" in r.stdout

    def test_repair_needs_one_document_or_all(self) -> None:
        assert self._invoke("repair-chash").exit_code == 2
        assert self._invoke("repair-chash", "1.2.3", "--all").exit_code == 2


class TestDoctorWiring:
    def test_the_doctor_row_offers_the_job_only_when_the_engine_says_not_validated(self, monkeypatch) -> None:
        for answer, rows in ((False, 1), (True, 0), (None, 0)):
            monkeypatch.setattr(health, "_chash_constraints_validated_by_engine", lambda a=answer: a)
            out = health._check_chash_validate_offer()
            assert len(out) == rows
        monkeypatch.setattr(health, "_chash_constraints_validated_by_engine", lambda: False)
        row = health._check_chash_validate_offer()[0]
        assert row.warn is True and any("nx doctor --fix" in s for s in row.fix_suggestions)

    def test_doctor_fix_and_its_dry_run_start_the_job(self) -> None:
        src = inspect.getsource(doctor)
        assert "doctor_chash_fix(dry_run=True)" in src and "doctor_chash_fix(dry_run=False)" in src
