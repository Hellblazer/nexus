# SPDX-License-Identifier: AGPL-3.0-or-later
"""RDR-193 Step 6 (nexus-u5m5g.7): the HTTP client's reconcile stage, commit and poll, and ``resolve_or_register``.

The shared job contract (RDR-193, "shared job contract"), counted at the HTTP client: every request the client sends
is recorded by a scripted server, so a test asserts how many submits and polls a case made, not only what it returned.

* ``409`` on the commit: poll the named running job and never resubmit.
* ``404`` on a poll: log ``engine_job_lost`` and resubmit once.
* ``503`` on the submit, or a ``failed`` job whose reason is ``transient_db_error``: resubmit at most three times with
  backoff, then raise.
* any other ``failed``: raise, never continue as if the job had succeeded.

The answers the scripted server gives are the shapes ``CatalogHandler.handleReconcile*`` and
``handleResolveOrRegister`` produce (read from the handler, not guessed).
"""
from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import Any

import pytest
import structlog

from nexus.catalog import reconcile_client as hcc
from nexus.db.t2 import _refreshable_client
from nexus.catalog.http_catalog_client import CatalogReconcileError, HttpCatalogClient

OUTCOME = {"rows": [{"file_path": "a.py", "tumbler": "1.1.1", "action": "created"}], "trashed": [], "renamed": [],
           "swept": [], "duplicate_path": [], "deleted_skipped_foreign_owner": [], "sensitivity_pending": []}


class _Script:
    """Per (method, path) a queue of canned ``(status, body)`` answers; the last one repeats. Every request is kept."""

    def __init__(self) -> None:
        self.answers: dict[tuple[str, str], list[tuple[int, dict]]] = {}
        self.requests: list[tuple[str, str, dict | None]] = []
        self.lock = threading.Lock()

    def on(self, method: str, path: str, *answers: tuple[int, dict]) -> None:
        self.answers[(method, path)] = list(answers)

    def count(self, method: str, path: str) -> int:
        return sum(1 for m, p, _ in self.requests if (m, p) == (method, path))

    def bodies(self, method: str, path: str) -> list[dict | None]:
        return [b for m, p, b in self.requests if (m, p) == (method, path)]


def _handler(script: _Script) -> type[BaseHTTPRequestHandler]:
    class H(BaseHTTPRequestHandler):
        def log_message(self, *a: Any) -> None:  # noqa: D401 - silence
            return

        def _serve(self, method: str) -> None:
            path = self.path.split("?")[0]
            length = int(self.headers.get("Content-Length") or 0)
            body = json.loads(self.rfile.read(length)) if length else None
            with script.lock:
                script.requests.append((method, path, body))
                queue = script.answers.get((method, path))
                if not queue:
                    status, out = 404, {"error": f"no scripted answer for {method} {path}"}
                else:
                    status, out = queue[0] if len(queue) == 1 else queue.pop(0)
            raw = json.dumps(out).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)

        def do_GET(self) -> None:  # noqa: N802
            self._serve("GET")

        def do_POST(self) -> None:  # noqa: N802
            self._serve("POST")

    return H


@pytest.fixture
def script() -> _Script:
    return _Script()


@pytest.fixture
def client(script: _Script, monkeypatch: pytest.MonkeyPatch):
    server = HTTPServer(("127.0.0.1", 0), _handler(script))
    threading.Thread(target=server.serve_forever, daemon=True).start()
    # no real waiting: the schedule keeps its length (the contract is the number of resubmits), not its seconds
    monkeypatch.setattr(hcc, "_RECONCILE_RESUBMIT_BACKOFF_S", (0.0, 0.0, 0.0))
    monkeypatch.setattr(hcc, "_RECONCILE_POLL_INTERVAL_S", 0.0)
    # The transport's own gateway ladder is off here, so each case counts the CLIENT's submits; the cases that are about
    # the transport (a 502 ridden out, the bound of the two together) switch it back on.
    monkeypatch.setattr(_refreshable_client, "_GATEWAY_RETRY_SLEEPS", ())
    c = HttpCatalogClient(base_url=f"http://127.0.0.1:{server.server_address[1]}", _token="t")
    try:
        yield c
    finally:
        c.close()
        server.shutdown()


COMMIT = "/v1/catalog/reconcile/commit"
STAGE = "/v1/catalog/reconcile/stage"


def _poll(job: str) -> str:
    return f"/v1/catalog/reconcile/{job}"


def _succeeded(job: str = "j1") -> tuple[int, dict]:
    return 200, {"job_id": job, "status": "succeeded", "outcome": OUTCOME, "http_status": 200}


def _running(job: str = "j1") -> tuple[int, dict]:
    return 200, {"job_id": job, "status": "running", "stage": "committing"}


def _failed(reason: str, http_status: int, job: str = "j1") -> tuple[int, dict]:
    return 200, {"job_id": job, "status": "failed", "error": f"commit said {reason}", "reason": reason,
                 "http_status": http_status}


# ── stage ────────────────────────────────────────────────────────────────────────────────────────────────────────

def test_stage_posts_the_run_header_and_one_page_and_returns_the_staged_count(client, script) -> None:
    script.on("POST", STAGE, (200, {"staged": 2}))
    rows = [{"kind": "present", "seq": 0, "file_path": "a.py"}, {"kind": "keep", "seq": 1, "file_path": "b.py"}]
    assert client.reconcile_stage("1.9", "run-1", "complete", 2, rows) == 2
    assert script.bodies("POST", STAGE) == [
        {"owner": "1.9", "run_id": "run-1", "walk": "complete", "expected_rows": 2, "rows": rows}]


def test_stage_is_sent_once_whatever_the_answer(client, script) -> None:
    """A page the engine staged and whose answer was lost would be a duplicate on a resend, so the transport never
    resends it blindly: a 503 is one request, and it raises."""
    import httpx

    script.on("POST", STAGE, (503, {"error": "busy"}))
    with pytest.raises(httpx.HTTPStatusError):
        client.reconcile_stage("1.9", "run-1", "complete", 1, [{"kind": "present", "seq": 0, "file_path": "a.py"}])
    assert script.count("POST", STAGE) == 1


# ── commit: the job contract ─────────────────────────────────────────────────────────────────────────────────────

def test_commit_submits_once_polls_until_succeeded_and_returns_the_outcome(client, script) -> None:
    script.on("POST", COMMIT, (202, {"job_id": "j1"}))
    script.on("GET", _poll("j1"), _running(), _running(), _succeeded())
    assert client.reconcile_commit("1.9", "run-1") == OUTCOME
    assert script.bodies("POST", COMMIT) == [{"owner": "1.9", "run_id": "run-1"}]
    assert script.count("GET", _poll("j1")) == 3


def test_a_409_polls_the_named_running_job_and_never_resubmits(client, script) -> None:
    """Another run of the owner is committing: its job is waited for, ours is not submitted again. Its outcome is
    another run's, so it is never returned as ours: the call raises once the named job is over."""
    script.on("POST", COMMIT, (409, {"error": "another run of this owner is committing", "job_id": "other"}))
    script.on("GET", _poll("other"), _running(), _succeeded("other"))
    with pytest.raises(CatalogReconcileError) as info:
        client.reconcile_commit("1.9", "run-1")
    assert info.value.reason == "commit_conflict" and info.value.job_id == "other"
    assert script.count("POST", COMMIT) == 1, "a 409 is never answered with a second submit"
    assert script.count("GET", _poll("other")) == 2, "the named job is polled to its end"


def test_a_409_whose_named_job_was_lost_raises_commit_conflict_and_not_the_private_loss(client, script) -> None:
    """r1 fix 4: the poll of the OTHER run's job can answer 404 (an engine restart). That is not this run's lost
    job, so it is neither resubmitted nor leaked as the private ``_ReconcileJobLost``: the contract is that a commit
    that did not end in an outcome raises ``CatalogReconcileError``, and this run was not committed."""
    script.on("POST", COMMIT, (409, {"error": "another run of this owner is committing", "job_id": "other"}))
    script.on("GET", _poll("other"), (404, {"error": "no such reconcile job"}))
    with pytest.raises(CatalogReconcileError) as info:
        client.reconcile_commit("1.9", "run-1")
    assert info.value.reason == "commit_conflict" and info.value.job_id == "other"
    assert "lost" in info.value.error
    assert script.count("POST", COMMIT) == 1 and script.count("GET", _poll("other")) == 1


def test_a_409_whose_named_job_failed_raises_commit_conflict_and_not_that_jobs_reason(client, script) -> None:
    """r1 fix 4: the other job's failure is the other run's. Carrying its reason (here ``run_not_found``) would make
    this run look as if its own staged rows were missing; the reason is ``commit_conflict``, and the text names why."""
    script.on("POST", COMMIT, (409, {"error": "another run of this owner is committing", "job_id": "other"}))
    script.on("GET", _poll("other"), _running("other"), _failed("run_not_found", 422, "other"))
    with pytest.raises(CatalogReconcileError) as info:
        client.reconcile_commit("1.9", "run-1")
    assert info.value.reason == "commit_conflict" and info.value.job_id == "other"
    assert "run_not_found" in info.value.error, "the other job's end is named in the text"
    assert script.count("POST", COMMIT) == 1


def test_a_stage_answered_409_commit_in_progress_raises_a_catalog_reconcile_error(client, script) -> None:
    """r1 fix 4: staging into a run the engine is already committing is refused (409, reason ``commit_in_progress``).
    It raises the client's own error with the engine's reason, never a bare ``HTTPStatusError``, and is sent once."""
    script.on("POST", STAGE, (409, {"error": "run run-1 is being committed", "reason": "commit_in_progress"}))
    with pytest.raises(CatalogReconcileError) as info:
        client.reconcile_stage("1.9", "run-1", "complete", 1, [{"kind": "present", "seq": 0, "file_path": "a.py"}])
    assert info.value.reason == "commit_in_progress" and info.value.http_status == 409
    assert script.count("POST", STAGE) == 1


def _shared_hold_must_stay_untouched(monkeypatch) -> list:
    """Turn the transport's gateway ladder back ON and record every write to the shared gateway hold, so a case can
    prove the commit submit never reaches either (r2: two stacked ladders turned a full registry into a machine-wide
    hold of minutes)."""
    from nexus.db import gateway_hold

    writes: list = []
    monkeypatch.setattr(_refreshable_client, "_GATEWAY_RETRY_SLEEPS", (0.0, 0.0, 0.0))
    monkeypatch.setattr(gateway_hold, "record_503", lambda sleep_s, **kw: writes.append(sleep_s) or 0.0)
    return writes


def test_a_gateway_502_on_the_submit_is_resubmitted_by_the_client_ladder_alone(client, script, monkeypatch) -> None:
    """The submit is sent once by the transport; the client ladder resubmits a 502 (safe: one ``{owner, run_id}``
    answers the running job or the stored outcome)."""
    writes = _shared_hold_must_stay_untouched(monkeypatch)
    script.on("POST", COMMIT, (502, {"error": "bad gateway"}), (202, {"job_id": "j1"}))
    script.on("GET", _poll("j1"), _succeeded())
    assert client.reconcile_commit("1.9", "run-1") == OUTCOME
    assert script.count("POST", COMMIT) == 2, "the 502 and one client resubmit"
    assert script.bodies("POST", COMMIT)[0] == script.bodies("POST", COMMIT)[1]
    assert writes == []


def test_a_registry_that_stays_full_costs_four_posts_and_never_writes_the_shared_hold(client, script, monkeypatch) -> None:
    """One ladder: the first submit and three resubmits, each sent once. Before r2 the transport resent each submit
    up to four times (16 POSTs) and wrote the shared gateway hold on every 503."""
    writes = _shared_hold_must_stay_untouched(monkeypatch)
    script.on("POST", COMMIT, (503, {"error": "the registry is full"}))
    with pytest.raises(CatalogReconcileError) as info:
        client.reconcile_commit("1.9", "run-1")
    assert info.value.reason == "registry_busy" and info.value.http_status == 503
    assert script.count("POST", COMMIT) == 4
    assert writes == []


def test_a_gateway_504_that_persists_raises_commit_unreachable_after_the_budget(client, script, monkeypatch) -> None:
    writes = _shared_hold_must_stay_untouched(monkeypatch)
    script.on("POST", COMMIT, (504, {"error": "gateway timeout"}))
    with pytest.raises(CatalogReconcileError) as info:
        client.reconcile_commit("1.9", "run-1")
    assert info.value.reason == "commit_unreachable" and info.value.http_status == 504
    assert script.count("POST", COMMIT) == 4
    assert writes == []


def test_a_submit_answered_400_is_not_resubmitted(client, script) -> None:
    script.on("POST", COMMIT, (400, {"error": "bad run"}))
    with pytest.raises(Exception) as info:
        client.reconcile_commit("1.9", "run-1")
    assert not (isinstance(info.value, CatalogReconcileError) and info.value.reason in {"registry_busy", "commit_unreachable"})
    assert script.count("POST", COMMIT) == 1


def test_a_409_whose_named_job_poll_errors_raises_commit_conflict_not_the_raw_error(client, script) -> None:
    """r2 N1: the other job's poll result is discarded, so any error from it (here a 500) is still this run's
    ``commit_conflict``, never a raw HTTP error that the hook's catch-all would hide."""
    script.on("POST", COMMIT, (409, {"error": "owner is committing", "job_id": "other"}))
    script.on("GET", _poll("other"), (500, {"error": "boom"}))
    with pytest.raises(CatalogReconcileError) as info:
        client.reconcile_commit("1.9", "run-1")
    assert info.value.reason == "commit_conflict" and info.value.job_id == "other"
    assert "could not be polled" in str(info.value)
    assert script.count("POST", COMMIT) == 1


def test_a_404_poll_logs_engine_job_lost_and_resubmits_once(client, script) -> None:
    script.on("POST", COMMIT, (202, {"job_id": "j1"}), (202, {"job_id": "j2"}))
    script.on("GET", _poll("j1"), (404, {"error": "no such reconcile job"}))
    script.on("GET", _poll("j2"), _succeeded("j2"))
    with structlog.testing.capture_logs() as logs:
        assert client.reconcile_commit("1.9", "run-1") == OUTCOME
    assert script.count("POST", COMMIT) == 2
    assert script.bodies("POST", COMMIT)[0] == script.bodies("POST", COMMIT)[1], "the same {owner, run_id} is resubmitted"
    lost = [e for e in logs if e.get("event") == "engine_job_lost"]
    assert len(lost) == 1 and lost[0]["job_id"] == "j1"


def test_a_second_lost_job_raises_and_does_not_resubmit_again(client, script) -> None:
    script.on("POST", COMMIT, (202, {"job_id": "j1"}), (202, {"job_id": "j2"}))
    script.on("GET", _poll("j1"), (404, {"error": "no such reconcile job"}))
    script.on("GET", _poll("j2"), (404, {"error": "no such reconcile job"}))
    with pytest.raises(CatalogReconcileError) as info:
        client.reconcile_commit("1.9", "run-1")
    assert info.value.reason == "engine_job_lost"
    assert script.count("POST", COMMIT) == 2, "one resubmit, then loud"


def test_a_503_on_submit_is_resubmitted_at_most_three_times(client, script) -> None:
    script.on("POST", COMMIT, (503, {"error": "full"}), (503, {"error": "full"}), (503, {"error": "full"}),
              (202, {"job_id": "j1"}))
    script.on("GET", _poll("j1"), _succeeded())
    assert client.reconcile_commit("1.9", "run-1") == OUTCOME
    assert script.count("POST", COMMIT) == 4, "the first send and three resubmits"


def test_a_503_that_never_clears_raises_after_the_third_resubmit(client, script) -> None:
    script.on("POST", COMMIT, (503, {"error": "the registry is full"}))
    with pytest.raises(CatalogReconcileError) as info:
        client.reconcile_commit("1.9", "run-1")
    assert info.value.reason == "registry_busy" and info.value.http_status == 503
    assert script.count("POST", COMMIT) == 4
    assert script.count("GET", _poll("j1")) == 0, "no job was ever handed out"


def test_a_transient_db_error_is_resubmitted_at_most_three_times(client, script) -> None:
    script.on("POST", COMMIT, (202, {"job_id": "j1"}), (202, {"job_id": "j2"}), (202, {"job_id": "j3"}),
              (202, {"job_id": "j4"}))
    for job in ("j1", "j2", "j3"):
        script.on("GET", _poll(job), _failed("transient_db_error", 503, job))
    script.on("GET", _poll("j4"), _succeeded("j4"))
    assert client.reconcile_commit("1.9", "run-1") == OUTCOME
    assert script.count("POST", COMMIT) == 4


def test_a_transient_db_error_that_keeps_coming_raises_after_the_third_resubmit(client, script) -> None:
    script.on("POST", COMMIT, *[(202, {"job_id": f"j{i}"}) for i in range(1, 6)])
    for i in range(1, 6):
        script.on("GET", _poll(f"j{i}"), _failed("transient_db_error", 503, f"j{i}"))
    with pytest.raises(CatalogReconcileError) as info:
        client.reconcile_commit("1.9", "run-1")
    assert info.value.reason == "transient_db_error" and info.value.http_status == 503
    assert script.count("POST", COMMIT) == 4


@pytest.mark.parametrize(("reason", "status"), [("run_not_found", 422), ("incomplete_stage", 422),
                                                ("commit_timeout", 500), ("job_stopped", 500)])
def test_any_other_failed_job_raises_with_the_engines_reason_and_is_never_resubmitted(client, script, reason, status) -> None:
    script.on("POST", COMMIT, (202, {"job_id": "j1"}))
    script.on("GET", _poll("j1"), _running(), _failed(reason, status))
    with pytest.raises(CatalogReconcileError) as info:
        client.reconcile_commit("1.9", "run-1")
    assert info.value.reason == reason and info.value.http_status == status and reason in info.value.error
    assert script.count("POST", COMMIT) == 1


def test_a_job_that_never_ends_raises_at_the_poll_deadline(client, script, monkeypatch) -> None:
    monkeypatch.setattr(hcc, "_RECONCILE_POLL_DEADLINE_S", 0.05)
    monkeypatch.setattr(hcc, "_RECONCILE_POLL_INTERVAL_S", 0.01)
    script.on("POST", COMMIT, (202, {"job_id": "j1"}))
    script.on("GET", _poll("j1"), _running())
    with pytest.raises(CatalogReconcileError) as info:
        client.reconcile_commit("1.9", "run-1")
    assert info.value.reason == "poll_deadline"


def test_a_succeeded_job_without_an_outcome_is_not_taken_for_success(client, script) -> None:
    script.on("POST", COMMIT, (202, {"job_id": "j1"}))
    script.on("GET", _poll("j1"), (200, {"job_id": "j1", "status": "succeeded", "http_status": 200}))
    with pytest.raises(CatalogReconcileError) as info:
        client.reconcile_commit("1.9", "run-1")
    assert info.value.reason == "no_outcome"


# ── resolve_or_register ──────────────────────────────────────────────────────────────────────────────────────────

ROR = "/v1/catalog/doc/resolve_or_register"


def test_resolve_or_register_sends_what_it_is_given_and_reads_a_minted_row(client, script) -> None:
    script.on("POST", ROR, (200, {"tumbler": "1.4.7", "created": True}))
    out = client.resolve_or_register(
        "standalone-docs", source_uri="file:///x/a.md", title="A", year=2020, source_mtime=12.5,
        content_type="prose", physical_collection="docs__x", sensitive=True,
    )
    assert (str(out.tumbler), out.created, out.foreign_owner) == ("1.4.7", True, False)
    assert script.bodies("POST", ROR) == [{
        "owner_name": "standalone-docs", "source_uri": "file:///x/a.md", "title": "A", "year": 2020,
        "source_mtime": 12.5, "content_type": "prose", "physical_collection": "docs__x", "sensitive": True}]


def test_resolve_or_register_omits_what_it_was_not_given(client, script) -> None:
    script.on("POST", ROR, (200, {"tumbler": "1.4.7", "created": False}))
    out = client.resolve_or_register("standalone-docs", file_path="/x/a.md", title="A", title_fallback=True)
    assert (str(out.tumbler), out.created, out.foreign_owner) == ("1.4.7", False, False)
    assert script.bodies("POST", ROR) == [
        {"owner_name": "standalone-docs", "file_path": "/x/a.md", "title": "A", "title_fallback": True}]


def test_resolve_or_register_reports_a_foreign_owner_row_as_not_created(client, script) -> None:
    script.on("POST", ROR, (200, {"tumbler": "1.2.3", "foreign_owner": True}))
    out = client.resolve_or_register("standalone-docs", source_uri="file:///x/a.md", title="A")
    assert (str(out.tumbler), out.created, out.foreign_owner) == ("1.2.3", False, True)


def test_resolve_or_register_needs_an_identity(client, script) -> None:
    with pytest.raises(ValueError, match="source_uri"):
        client.resolve_or_register("standalone-docs", title="A")
    assert script.requests == []


# ── the factory's write whitelist ────────────────────────────────────────────────────────────────────────────────

def test_the_three_methods_are_service_only_write_ops() -> None:
    from nexus.catalog.factory import _SERVICE_ONLY_WRITE_OPS

    assert {"reconcile_stage", "reconcile_commit", "resolve_or_register"} <= _SERVICE_ONLY_WRITE_OPS
