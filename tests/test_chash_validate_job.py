# SPDX-License-Identifier: AGPL-3.0-or-later
"""nexus-f3yqd: the client for the engine's user-started chash CHECK VALIDATE job and the manifest repair.

Driven through a real ``HttpCatalogClient`` against a scripted fake engine (``ThreadingHTTPServer`` on port 0), so the
requests take the client's own transport, auth and the unprefixed mixin calls. The fake's bodies follow engine
0.1.190's handlers as of its review-fix commit 550d4e9f4 (``ValidateConstraintsJobs.Progress``: ``validated`` is a list
of table names, the counts, ``refused`` entries with ``tenants``; the reason strings ``ChashConstraintValidator``
emits).
"""

from __future__ import annotations

import json
import threading
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from types import SimpleNamespace

import httpx
import pytest
from click.testing import CliRunner

import nexus.health as health
from nexus.catalog.http_catalog_client import HttpCatalogClient
from nexus.cli import main
from nexus.commands.t3_cmds import chash_checks
from nexus.db.admin import chash_validate as job
from tests._settle import FakeClock

_CONFORMANCE_FALSE = {"constraints": [
    {"table": "nexus.chunks", "name": "chunks_chash_len_check", "present": True, "validated": False,
     "definition_matches": True},
    {"table": "nexus.catalog_document_chunks", "name": "cdc_chash_len_check", "present": True, "validated": False,
     "definition_matches": True}], "all_validated": False}


class _Engine:
    def __init__(self) -> None:
        self.flags: dict = {"chash_constraints_validate_job_supported": True,
                            "chash_conformance_constraints_supported": True}
        self.conformance: dict = _CONFORMANCE_FALSE
        self.start_status = 202
        self.start_body: dict = {"job_id": "j1", "created": True}
        self.polls: list[tuple[int, dict]] = [(404, {"error": "no such constraint validate job"})]
        self.repair_body: dict = {"documents": [], "count": 0, "capped": False, "dry_run": True}
        self.requests: list[tuple[str, str, dict | None]] = []


def _handler(engine: _Engine) -> type[BaseHTTPRequestHandler]:
    class H(BaseHTTPRequestHandler):
        def log_message(self, format: str, *args: object) -> None:  # noqa: A002 — http.server's signature
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
            if self.path == "/v1/schema/chash-conformance":
                return self._send(200, engine.conformance)
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


@pytest.fixture(autouse=True)
def _no_gateway_ladder(monkeypatch: pytest.MonkeyPatch) -> None:
    """The client's own 5xx retry ladder sleeps 2+5+10 s per request; this file's own tolerance is under test."""
    monkeypatch.setattr("nexus.db.t2._refreshable_client._GATEWAY_RETRY_SLEEPS", (0.0,))


@pytest.fixture
def engine() -> Iterator[_Engine]:
    yield _Engine()


@pytest.fixture
def client(engine: _Engine, monkeypatch: pytest.MonkeyPatch) -> Iterator[HttpCatalogClient]:
    monkeypatch.setattr("nexus.config.is_local_mode", lambda: True)
    server = ThreadingHTTPServer(("127.0.0.1", 0), _handler(engine))
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        with HttpCatalogClient(base_url=f"http://127.0.0.1:{server.server_port}", tenant="t", _token="tok") as c:
            yield c
    finally:
        server.shutdown()


def _progress(state: str = "running", **over) -> dict:
    body = {"job_id": "j1", "state": state, "stage": "validating", "elapsed_ms": 10, "dry_run": False,
            "tables_done": 1, "tables_total": 4, "current": {"table": "nexus.chunks_m1_t1", "bytes": 2_000_000_000},
            "validated": [], "validated_count": 0, "already_valid_count": 0, "refused": [], "refused_count": 0,
            "deferred": [], "deferred_count": 0, "all_validated": None, "safe_to_stop": True, "on_stop": "resumes"}
    return body | over


def _done(**over) -> dict:
    return _progress("done", tables_done=4, current=None, validated=["nexus.chunks_m1_t1", "nexus.chunks"],
                     validated_count=2, already_valid_count=2, all_validated=True) | over


def _refusal(table: str = "nexus.catalog_document_chunks", reason: str = "bad_rows", **over) -> dict:
    e = {"table": table, "constraint": "cdc_chash_len_check", "reason": reason, "bad_rows": None,
         "bad_rows_capped": False, "example_chashes": ["ab" * 16], "precheck": "unavailable",
         "tenants": [{"tenant": "t1", "bad_rows": 3}]}
    return e | over


def _deferral(reason: str, **over) -> dict:
    return {"table": "nexus.chunks_m1_t1", "constraint": "chunks_chash_len_check", "reason": reason,
            "precheck": "clean"} | over


def _clock() -> FakeClock:
    return FakeClock(auto_advance=True)


class TestStart:
    def test_an_engine_without_the_flag_is_too_old_and_nothing_is_posted(self, client, engine) -> None:
        engine.flags = {"chash_conformance_constraints_supported": True}
        with pytest.raises(job.CannotRun, match="0.1.190"):
            job.start(client, dry_run=False)
        assert not [r for r in engine.requests if r[0] == "POST"]

    def test_unreadable_capabilities_are_not_called_too_old(self, client, engine) -> None:
        engine.flags = {}
        with pytest.raises(job.CannotRun, match="could not read the engine's capabilities"):
            job.start(client, dry_run=False)

    def test_a_managed_install_without_the_flag_is_told_to_ask_the_operator(self, client, engine, monkeypatch) -> None:
        engine.flags = {"chash_conformance_constraints_supported": True}  # an edge that trims the flag
        monkeypatch.setattr("nexus.config.is_local_mode", lambda: False)
        with pytest.raises(job.AskOperator, match="ask the operator"):
            job.start(client, dry_run=False)

    def test_a_403_names_the_root_token_locally_and_asks_the_operator_on_managed(
        self, client, engine, monkeypatch,
    ) -> None:
        engine.start_status, engine.start_body = 403, {"error": "operator privilege required"}
        with pytest.raises(job.AskOperator, match="nx config set service_token"):
            job.start(client, dry_run=False)
        monkeypatch.setattr("nexus.config.is_local_mode", lambda: False)
        with pytest.raises(job.AskOperator, match="ask the operator"):
            job.start(client, dry_run=False)

    @pytest.mark.parametrize(("status", "text"), [(409, "already running"), (503, "right now")])
    def test_a_busy_engine_is_a_rerun_not_a_cannot(self, client, engine, status, text) -> None:
        engine.start_status, engine.start_body = status, {"error": "busy", "job_id": "j9"}
        with pytest.raises(job.ValidateJobError, match=text) as exc:
            job.start(client, dry_run=False)
        assert exc.value.exit_code == 1


class TestDryRunNeverWrites:
    """A dry run must post dry_run=true on every path: a dry run turned into a real VALIDATE is a write the user did
    not ask for (review r2)."""

    def test_start_posts_the_flag_it_was_given(self, client, engine) -> None:
        job.start(client, dry_run=True)
        job.start(client, dry_run=False)
        posts = [r[2] for r in engine.requests if r[0] == "POST" and r[1] == job.JOB_PATH]
        assert posts == [{"dry_run": True}, {"dry_run": False}]

    def test_doctor_fix_dry_run_posts_a_dry_run(self, client, engine, monkeypatch) -> None:
        monkeypatch.setattr(job, "POLL_EVERY_S", 0.0)
        engine.polls = [(200, _done(dry_run=True, would_validate=["nexus.chunks"], would_validate_count=1))]
        job.doctor_fix_lines(client, dry_run=True, echo=lambda _l: None, clock=_clock())
        assert [r[2] for r in engine.requests if r[0] == "POST"] == [{"dry_run": True}]

    def test_the_doctor_entry_point_passes_dry_run_through(self, client, engine, monkeypatch) -> None:
        monkeypatch.setattr(chash_checks, "_client", lambda: client)
        monkeypatch.setattr(client, "close", lambda: None)
        monkeypatch.setattr(job, "POLL_EVERY_S", 0.0)
        engine.polls = [(200, _done(dry_run=True, would_validate=[], would_validate_count=0))]
        chash_checks.doctor_chash_fix(dry_run=True)
        assert [r[2] for r in engine.requests if r[0] == "POST"] == [{"dry_run": True}]

    def test_the_verb_with_dry_run_posts_a_dry_run(self, client, engine, monkeypatch) -> None:
        monkeypatch.setattr(chash_checks, "_client", lambda: client)
        monkeypatch.setattr(client, "close", lambda: None)
        monkeypatch.setattr(job, "POLL_EVERY_S", 0.0)
        engine.polls = [(200, _done(dry_run=True, would_validate=["nexus.chunks"], would_validate_count=1))]
        r = CliRunner().invoke(main, ["t3", "validate-chash-checks", "--dry-run"])
        assert r.exit_code == 0, r.output
        assert [q[2] for q in engine.requests if q[0] == "POST"] == [{"dry_run": True}]

    def test_a_count_that_is_not_an_int_does_not_crash_the_verdict(self) -> None:
        assert job.verdict(_done(refused_count="many", deferred_count=None)).ok is True


class TestWait:
    def test_progress_prints_on_change_and_reads_the_end(self, client, engine) -> None:
        engine.polls = [(200, _progress()), (200, _progress()), (200, _progress(tables_done=3)), (200, _done())]
        seen: list[str] = []
        body = job.wait(client, "j1", on_progress=seen.append, clock=_clock())
        assert seen == ["Validating the chash CHECKs: 1 of 4 tables, on nexus.chunks_m1_t1 (2.0 GB).",
                        "Validating the chash CHECKs: 3 of 4 tables, on nexus.chunks_m1_t1 (2.0 GB)."]
        assert job.verdict(body).ok is True

    def test_a_poll_5xx_is_ridden_out(self, client, engine) -> None:
        engine.polls = [(503, {"error": "starting"}), (502, {"error": "gateway"}), (200, _done())]
        assert job.wait(client, "j1", clock=_clock())["state"] == "done"

    def test_too_many_failed_polls_give_up_naming_the_job(self, client, engine) -> None:
        engine.polls = [(503, {"error": "down"})]
        with pytest.raises(job.ValidateJobError, match="--job j1") as exc:
            job.wait(client, "j1", clock=_clock())
        assert exc.value.exit_code == 1

    @pytest.mark.parametrize("failure", [httpx.ConnectError("refused"), RuntimeError("cannot self-heal")])
    def test_a_transport_failure_is_transient_and_then_names_the_job(self, client, monkeypatch, failure) -> None:
        def _down(*_a, **_k):
            raise failure

        monkeypatch.setattr(job.RefreshableHttpStoreMixin, "_get", _down)
        with pytest.raises(job.ValidateJobError, match="--job j1"):
            job.wait(client, "j1", clock=_clock())

    def test_a_job_the_engine_no_longer_holds_says_start_again(self, client, engine) -> None:
        with pytest.raises(job.ValidateJobError, match="resumes") as exc:
            job.status(client, "gone")
        assert exc.value.exit_code == 1


class TestVerdict:
    def test_a_dry_run_with_a_deferral_is_not_clean(self) -> None:
        body = _done(dry_run=True, all_validated=False, would_validate=[], would_validate_count=0,
                     deferred=[_deferral("precheck_unavailable")], deferred_count=1)
        v = job.verdict(body)
        assert v.ok is False and "a real run decides it" in " ".join(v.lines)

    def test_a_clean_dry_run_changes_nothing(self) -> None:
        v = job.verdict(_done(dry_run=True, would_validate=["nexus.chunks"], would_validate_count=1))
        assert v.ok is True and "nothing was changed" in v.summary

    def test_a_statement_timeout_names_the_setting_not_a_rerun(self) -> None:
        v = job.verdict(_done(all_validated=False, deferred=[_deferral("statement_timeout")], deferred_count=1))
        assert v.ok is False and "nexus.validate.statement_timeout_ms" in " ".join(v.lines)

    def test_constraint_absent_is_not_a_rerun(self) -> None:
        v = job.verdict(_done(all_validated=False, deferred=[_deferral("constraint_absent")], deferred_count=1))
        assert "no rerun fixes" in " ".join(v.lines)

    def test_a_lock_timeout_is_a_rerun(self) -> None:
        v = job.verdict(_done(all_validated=False, deferred=[_deferral("lock_timeout")], deferred_count=1))
        assert v.ok is False and "busy; run this again" in " ".join(v.lines)

    def test_a_definition_mismatch_is_a_schema_problem_not_a_row_repair(self) -> None:
        v = job.verdict(_done(all_validated=False, refused=[_refusal(reason="definition_mismatch")], refused_count=1))
        text = " ".join(v.lines)
        assert v.ok is False and "schema" in text and "repair-chash" not in text

    def test_a_manifest_refusal_names_the_tenants_and_says_no_engine_has_the_repair_yet(self) -> None:
        v = job.verdict(_done(all_validated=False, refused=[_refusal()], refused_count=1))
        text = "\n".join(v.lines)
        assert "tenants: t1 (3)" in text and "no released engine has yet" in text

    def test_a_manifest_refusal_names_the_repair_once_the_engine_has_it(self) -> None:
        v = job.verdict(_done(all_validated=False, refused=[_refusal()], refused_count=1), repair_ready=True)
        assert "nx t3 repair-chash --all" in "\n".join(v.lines)

    def test_a_chunk_row_refusal_says_no_client_repair_exists(self) -> None:
        v = job.verdict(_done(all_validated=False, refused=[_refusal(table="nexus.chunks_m1_t1", bad_rows=7)],
                              refused_count=1))
        assert "7 bad row(s)" in "\n".join(v.lines) and "no client repair" in "\n".join(v.lines)

    def test_counts_beyond_the_listed_cap_are_reported(self) -> None:
        v = job.verdict(_done(all_validated=False, refused=[_refusal()], refused_count=250))
        assert "250 CHECK(s) refused" in v.summary and "cut short" in "\n".join(v.lines)

    def test_done_with_all_validated_null_is_not_success(self) -> None:
        assert job.verdict(_done(all_validated=None)).ok is False

    def test_a_failed_job_is_not_success(self) -> None:
        v = job.verdict({"state": "failed", "error": "lost the admin connection"})
        assert v.ok is False and "lost the admin connection" in v.summary


class TestDoctorFix:
    @pytest.fixture(autouse=True)
    def _instant(self, monkeypatch) -> None:
        monkeypatch.setattr(job, "POLL_EVERY_S", 0.0)

    def _run(self, client, **kw) -> tuple[bool, list[str]]:
        lines: list[str] = []
        ok = job.doctor_fix_lines(client, dry_run=kw.get("dry_run", False), echo=lines.append, clock=_clock())
        return ok, lines

    def test_already_validated_starts_nothing(self, client, engine) -> None:
        engine.conformance = {"constraints": [dict(c, validated=True) for c in _CONFORMANCE_FALSE["constraints"]],
                              "all_validated": True}
        ok, lines = self._run(client)
        assert ok and "already validated" in lines[0]
        assert not [r for r in engine.requests if r[0] == "POST"]

    def test_not_validated_starts_and_waits(self, client, engine) -> None:
        engine.polls = [(200, _progress()), (200, _done())]
        ok, lines = self._run(client)
        assert ok is True
        assert lines[0].startswith("chash CHECKs: validate job j1 started") and "--job j1" in lines[0]
        assert lines[-1].startswith("chash CHECKs: every chash CHECK is validated")

    def test_a_refusal_returns_false_with_next_steps(self, client, engine) -> None:
        engine.polls = [(200, _done(all_validated=False, refused=[_refusal()], refused_count=1))]
        ok, lines = self._run(client)
        assert ok is False and any("next:" in line for line in lines)

    def test_a_managed_install_says_ask_the_operator(self, client, engine, monkeypatch) -> None:
        monkeypatch.setattr("nexus.config.is_local_mode", lambda: False)
        engine.start_status, engine.start_body = 403, {"error": "operator privilege required"}
        ok, lines = self._run(client)
        assert ok is True and "ask the operator" in lines[-1]

    def test_an_unreadable_engine_says_could_not_tell(self, client, engine) -> None:
        engine.flags = {}
        ok, lines = self._run(client)
        assert ok is True and "could not tell" in lines[0]

    def test_engine_gone_mid_job_is_reported_not_raised(self, client, engine) -> None:
        engine.polls = [(503, {"error": "down"})]
        ok, lines = self._run(client)
        assert ok is False and "--job j1" in lines[-1]


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
        engine.polls = [(200, _progress()), (200, _done())]
        r = self._invoke("validate-chash-checks")
        assert r.exit_code == 0, r.output
        assert "Validate job j1 started" in r.stdout and "every chash CHECK is validated" in r.stdout

    def test_a_refusal_exits_1_with_its_next_step(self, engine) -> None:
        engine.polls = [(200, _done(all_validated=False, refused=[_refusal()], refused_count=1))]
        r = self._invoke("validate-chash-checks")
        assert r.exit_code == 1 and "next:" in r.stdout

    def test_an_old_engine_exits_3(self, engine) -> None:
        engine.flags = {"chash_conformance_constraints_supported": True}
        r = self._invoke("validate-chash-checks")
        assert r.exit_code == 3 and "0.1.190" in r.stderr

    def test_a_lost_job_exits_1_not_3(self) -> None:
        r = self._invoke("validate-chash-checks", "--job", "gone")
        assert r.exit_code == 1 and "resumes" in r.stderr

    def test_no_wait_prints_the_follow_command_and_json_when_asked(self, engine) -> None:
        r = self._invoke("validate-chash-checks", "--no-wait")
        assert r.exit_code == 0 and "nx t3 validate-chash-checks --job j1" in r.stdout
        r = self._invoke("validate-chash-checks", "--no-wait", "--json")
        assert json.loads(r.stdout)["job_id"] == "j1"
        assert not [q for q in engine.requests if q[0] == "GET" and q[1].startswith(job.JOB_PATH + "/")]

    def test_json_is_the_final_report(self, engine) -> None:
        engine.polls = [(200, _done())]
        r = self._invoke("validate-chash-checks", "--json")
        assert r.exit_code == 0 and json.loads(r.stdout)["all_validated"] is True

    def test_repair_without_an_engine_route_exits_3_and_changes_nothing(self, engine) -> None:
        r = self._invoke("repair-chash", "1.2.3", "--apply")
        assert r.exit_code == 3 and "no released engine" in r.stderr
        assert not [q for q in engine.requests if q[0] == "POST"]

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
        monkeypatch.setattr("nexus.config.is_local_mode", lambda: True)
        for answer, rows in ((False, 1), (True, 0), (None, 0)):
            monkeypatch.setattr(health, "_chash_constraints_validated_by_engine", lambda a=answer: a)
            assert len(health._check_chash_validate_offer()) == rows
        monkeypatch.setattr(health, "_chash_constraints_validated_by_engine", lambda: False)
        row = health._check_chash_validate_offer()[0]
        assert row.warn is True and row.fix_suggestions[0].startswith("nx t3 validate-chash-checks")

    def test_on_a_managed_install_the_row_says_ask_the_operator(self, monkeypatch) -> None:
        monkeypatch.setattr("nexus.config.is_local_mode", lambda: False)
        monkeypatch.setattr(health, "_chash_constraints_validated_by_engine", lambda: False)
        assert "operator" in health._check_chash_validate_offer()[0].fix_suggestions[0]

    @pytest.fixture
    def doctor_env(self, monkeypatch) -> list[bool]:
        calls: list[bool] = []
        monkeypatch.setattr(chash_checks, "doctor_chash_fix", lambda *, dry_run: calls.append(dry_run) or False)
        monkeypatch.setattr("nexus.commands.doctor._say_if_the_engine_is_migrating", lambda: None)
        monkeypatch.setattr("nexus.catalog.factory.make_catalog_reader", lambda **_k: object())
        monkeypatch.setattr("nexus.garbage.catalog_garbage", lambda _r: SimpleNamespace(
            error=None, orphaned_links=0, trash_documents=0, stranded_chunks=0))
        monkeypatch.setattr("nexus.garbage.reclaim_catalog_garbage", lambda _w, reader=None: SimpleNamespace(
            links_deleted=0, trash_documents=0, stranded_chunks=0))
        monkeypatch.setattr("nexus.commands.catalog._get_catalog_writer", lambda: SimpleNamespace(close=lambda: None))
        monkeypatch.setattr("nexus.health.vacuum_tuple_tables", lambda dry_run: [])
        monkeypatch.setattr("nexus.config.is_local_mode", lambda: False)  # HNSW tuning then returns at once
        return calls

    def test_doctor_fix_dry_run_starts_a_dry_run_job(self, doctor_env) -> None:
        r = CliRunner().invoke(main, ["doctor", "--fix", "--dry-run"])
        assert r.exit_code == 0, r.output
        assert doctor_env == [True]

    def test_doctor_fix_starts_the_job_and_a_refusal_does_not_stop_the_later_steps(self, doctor_env) -> None:
        r = CliRunner().invoke(main, ["doctor", "--fix"])
        assert r.exit_code == 0, r.output
        assert doctor_env == [False]
        assert "server-side" in r.stdout, "the HNSW step after the job still ran"
