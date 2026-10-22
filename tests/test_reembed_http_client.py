# SPDX-License-Identifier: AGPL-3.0-or-later
"""RDR-210 Phase 6 Step 3 (nexus-cnxjk): ``HttpVectorClient``'s re-embed route methods.

The engine reads these request bodies strictly (an unknown field is a 400), so each method must send exactly
the fields its route takes: these tests pin the path, the verb and the body key set of every call.
"""
from __future__ import annotations

from typing import Any

import pytest

from nexus.db.http_vector_client import HttpVectorClient, VectorServiceError

BASE = "/v1/vectors/reembed"
JOB = "11111111-2222-3333-4444-555555555555"


@pytest.fixture
def wire(monkeypatch: pytest.MonkeyPatch):
    """Record every ``_post``/``_get``; ``answers`` maps a path to the body to return."""
    calls: list[tuple[str, str, dict | None]] = []
    answers: dict[str, Any] = {}

    def fake_post(path: str, body: dict, *, tenant: str = "default", timeout: float = 120) -> Any:
        calls.append(("POST", path, body))
        return answers.get(path, {})

    def fake_get(path: str, *, tenant: str = "default", timeout: float = 30) -> Any:
        calls.append(("GET", path, None))
        return answers.get(path, {})

    monkeypatch.setattr("nexus.db.http_vector_client._post", fake_post)
    monkeypatch.setattr("nexus.db.http_vector_client._get", fake_get)
    return SimpleWire(calls, answers)


class SimpleWire:
    def __init__(self, calls, answers) -> None:
        self.calls, self.answers = calls, answers


class TestPlan:
    def test_sends_the_source_and_the_target_name(self, wire):
        wire.answers[f"{BASE}/plan"] = {"chunks": 3}
        got = HttpVectorClient().reembed_plan("docs__s", target_collection="docs__t")
        assert got == {"chunks": 3}
        assert wire.calls == [("POST", f"{BASE}/plan", {"source_collection": "docs__s", "target_collection": "docs__t"})]

    def test_omits_the_target_when_none_is_given(self, wire):
        HttpVectorClient().reembed_plan("docs__s")
        assert wire.calls[0][2] == {"source_collection": "docs__s"}


class TestStart:
    def test_sends_exactly_the_start_fields(self, wire):
        HttpVectorClient().reembed_start(
            "docs__s", target_collection="docs__t", token_cap=777, keep_source=True, force_delete=True,
        )
        method, path, body = wire.calls[0]
        assert (method, path) == ("POST", f"{BASE}/jobs")
        assert body == {
            "source_collection": "docs__s", "target_collection": "docs__t", "token_cap": 777,
            "keep_source": True, "force_delete": True,
        }
        assert set(body) <= {"source_collection", "target_collection", "token_cap", "keep_source", "force_delete"}

    def test_defaults_send_the_flags_false_and_no_cap(self, wire):
        HttpVectorClient().reembed_start("docs__s")
        assert wire.calls[0][2] == {"source_collection": "docs__s", "keep_source": False, "force_delete": False}


class TestStatus:
    def test_reads_every_job_row(self, wire):
        wire.answers[f"{BASE}/status"] = {"jobs": [{"job_id": JOB}]}
        assert HttpVectorClient().reembed_status() == [{"job_id": JOB}]
        assert wire.calls == [("GET", f"{BASE}/status", None)]

    def test_a_body_without_a_jobs_list_is_an_error_not_an_empty_list(self, wire):
        wire.answers[f"{BASE}/status"] = {"unexpected": True}
        with pytest.raises(VectorServiceError):
            HttpVectorClient().reembed_status()


class TestControls:
    def test_pause_and_abort_send_no_body_fields(self, wire):
        c = HttpVectorClient()
        c.reembed_pause(JOB)
        c.reembed_abort(JOB)
        assert wire.calls == [
            ("POST", f"{BASE}/jobs/{JOB}/pause", {}),
            ("POST", f"{BASE}/jobs/{JOB}/abort", {}),
        ]

    def test_resume_carries_only_a_cap_when_one_is_given(self, wire):
        c = HttpVectorClient()
        c.reembed_resume(JOB)
        c.reembed_resume(JOB, token_cap=99)
        assert [b for _, _, b in wire.calls] == [{}, {"token_cap": 99}]
        assert all(p == f"{BASE}/jobs/{JOB}/resume" for _, p, _ in wire.calls)


class TestServedModels:
    def test_reads_version_embedding_models(self, wire):
        wire.answers["/version"] = {"embedding_models": ["m-a", "m-b"]}
        assert HttpVectorClient().served_embedding_models() == ["m-a", "m-b"]

    @pytest.mark.parametrize("body", [{}, {"embedding_models": "m-a"}, {"embedding_models": [1]}, "text"])
    def test_cannot_tell_is_none(self, wire, body):
        wire.answers["/version"] = body
        assert HttpVectorClient().served_embedding_models() is None

    def test_an_unreachable_engine_is_none(self, monkeypatch):
        def boom(path, **kw):
            raise VectorServiceError("down")

        monkeypatch.setattr("nexus.db.http_vector_client._get", boom)
        assert HttpVectorClient().served_embedding_models() is None


class TestProductionWriteGuard:
    """The start and the three controls mutate the engine's job table, so a dev-checkout process is refused them
    unless it carries a reason (nexus-a2qhz); plan and status only read and are never refused."""

    @pytest.fixture(autouse=True)
    def _guard_engaged(self, monkeypatch: pytest.MonkeyPatch, tmp_path):
        from nexus.db import http_vector_client as hvc
        from nexus.db import service_endpoint

        monkeypatch.delenv(service_endpoint.PROD_WRITE_OPT_IN_ENV, raising=False)
        monkeypatch.setattr(service_endpoint, "_test_only_opt_in_reason", None)
        monkeypatch.setattr(service_endpoint, "_dev_checkout_root", lambda start=None: tmp_path)
        monkeypatch.setattr(hvc, "_resolve_endpoint", lambda: ("http://127.0.0.1:1", "tok"))
        self.sent: list[str] = []

        def fake_request(method, path, **kw):
            self.sent.append(f"{method} {path}")
            return {"jobs": []}

        monkeypatch.setattr(hvc, "_request", fake_request)

    @pytest.mark.parametrize("call", [
        lambda c: c.reembed_start("docs__s"),
        lambda c: c.reembed_pause(JOB),
        lambda c: c.reembed_resume(JOB),
        lambda c: c.reembed_abort(JOB),
    ], ids=["start", "pause", "resume", "abort"])
    def test_a_dev_checkout_is_refused_the_write_before_any_request(self, call):
        from nexus.db.service_endpoint import ProductionWriteGuardError

        with pytest.raises(ProductionWriteGuardError):
            call(HttpVectorClient())
        assert self.sent == []

    def test_plan_and_status_only_read_and_are_not_refused(self):
        client = HttpVectorClient()
        client.reembed_plan("docs__s")
        client.reembed_status()
        assert self.sent == ["POST /v1/vectors/reembed/plan", "GET /v1/vectors/reembed/status"]

    def test_a_reason_lets_the_write_through(self, monkeypatch):
        from nexus.db import service_endpoint

        monkeypatch.setattr(service_endpoint, "_test_only_opt_in_reason", "test: reason")
        HttpVectorClient().reembed_pause(JOB)
        assert self.sent == [f"POST /v1/vectors/reembed/jobs/{JOB}/pause"]

    def test_write_path_predicate(self):
        from nexus.db.http_vector_client import _is_t3_write_path

        assert _is_t3_write_path("/v1/vectors/reembed/jobs")
        assert _is_t3_write_path(f"/v1/vectors/reembed/jobs/{JOB}/abort")
        assert not _is_t3_write_path("/v1/vectors/reembed/plan")
        assert not _is_t3_write_path("/v1/vectors/reembed/status")
        assert not _is_t3_write_path(f"/v1/vectors/reembed/jobs/{JOB}/other")
