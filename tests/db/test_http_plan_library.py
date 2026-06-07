# SPDX-License-Identifier: AGPL-3.0-or-later
"""Contract tests for HttpPlanLibrary.

Test approach: in-process fake HTTP server implementing the /v1/plans/*
contract. The fake server mirrors the REAL Java PlanHandler shape faithfully.

This verifies:
  - HttpPlanLibrary makes correct HTTP calls (right paths, headers, payloads)
  - Response -> Python dict mapping is correct (types, None/empty normalization)
  - HTTP error codes map to the expected Python exceptions
  - Auth header and X-Nexus-Tenant header are sent on every request
  - import_plan fidelity: counters/timestamps preserved verbatim

Full cross-language end-to-end is in tests/db/test_http_plan_library_integration.py
(marked integration).
"""
from __future__ import annotations

import json
import socket
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import Any
from urllib.parse import parse_qs, urlparse

import pytest

from nexus.db.t2.http_plan_library import DEFAULT_TENANT, HttpPlanLibrary

TOKEN = "fake-plan-service-token-xyz"

# ── In-process fake server ────────────────────────────────────────────────────

# Shared in-memory store: id -> plan_dict
_STORE: dict[int, dict[str, Any]] = {}
_STORE_LOCK = threading.Lock()
_ID_SEQ = [0]


def _next_id() -> int:
    _ID_SEQ[0] += 1
    return _ID_SEQ[0]


def _make_plan(
    project: str,
    query: str,
    plan_json: str,
    outcome: str = "success",
    tags: str = "",
    **kwargs: Any,
) -> dict[str, Any]:
    """Create a faithful replica of Java PlanHandler's recordToMap output."""
    return {
        "id":               _next_id(),
        "project":          project,
        "query":            query,
        "plan_json":        plan_json,
        "outcome":          outcome,
        "tags":             tags,
        "created_at":       kwargs.get("created_at", "2026-06-01T00:00:00Z"),
        "ttl":              kwargs.get("ttl"),
        "name":             kwargs.get("name"),
        "verb":             kwargs.get("verb"),
        "scope":            kwargs.get("scope"),
        "dimensions":       kwargs.get("dimensions"),
        "default_bindings": kwargs.get("default_bindings"),
        "parent_dims":      kwargs.get("parent_dims"),
        "use_count":        kwargs.get("use_count", 0),
        "last_used":        kwargs.get("last_used"),
        "match_count":      kwargs.get("match_count", 0),
        "match_conf_sum":   kwargs.get("match_conf_sum", 0.0),
        "success_count":    kwargs.get("success_count", 0),
        "failure_count":    kwargs.get("failure_count", 0),
        "scope_tags":       kwargs.get("scope_tags", ""),
        "match_text":       kwargs.get("match_text", ""),
        "disabled_at":      kwargs.get("disabled_at"),
    }


class _FakePlanHandler(BaseHTTPRequestHandler):
    """In-process stub of PlanHandler (Java)."""

    def log_message(self, fmt, *args):  # suppress server log noise
        pass

    def _check_auth(self) -> bool:
        auth   = self.headers.get("Authorization", "")
        tenant = self.headers.get("X-Nexus-Tenant", "")
        if auth != f"Bearer {TOKEN}":
            self._send(401, {"error": "unauthorized"})
            return False
        if not tenant:
            self._send(400, {"error": "missing X-Nexus-Tenant header"})
            return False
        return True

    def _body(self) -> dict[str, Any]:
        length = int(self.headers.get("Content-Length", "0"))
        raw = self.rfile.read(length)
        return json.loads(raw) if raw else {}

    def _send(self, status: int, data: Any) -> None:
        body = json.dumps(data).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _parsed_path(self):
        return urlparse(self.path)

    def _qs(self) -> dict[str, str]:
        parsed = parse_qs(urlparse(self.path).query)
        return {k: v[0] for k, v in parsed.items()}

    def do_POST(self):
        if not self._check_auth():
            return
        pp = self._parsed_path().path
        body = self._body()

        if pp == "/v1/plans/save":
            with _STORE_LOCK:
                # Upsert by (project, query)
                existing = next(
                    (p for p in _STORE.values()
                     if p["project"] == body.get("project", "")
                     and p["query"] == body["query"]),
                    None,
                )
                if existing:
                    existing.update({
                        "plan_json": body.get("plan_json", existing["plan_json"]),
                        "outcome":   body.get("outcome", existing["outcome"]),
                        "tags":      body.get("tags", ""),
                        "verb":      body.get("verb"),
                        "scope_tags": body.get("scope_tags", ""),
                        "match_text": body.get("match_text", ""),
                    })
                    self._send(200, {"id": existing["id"]})
                else:
                    plan = _make_plan(
                        body.get("project", ""),
                        body["query"],
                        body.get("plan_json", "{}"),
                        body.get("outcome", "success"),
                        body.get("tags", ""),
                        name=body.get("name"),
                        verb=body.get("verb"),
                        scope=body.get("scope"),
                        dimensions=body.get("dimensions"),
                        default_bindings=body.get("default_bindings"),
                        parent_dims=body.get("parent_dims"),
                        ttl=body.get("ttl"),
                        scope_tags=body.get("scope_tags", ""),
                        match_text=body.get("match_text", ""),
                    )
                    _STORE[plan["id"]] = plan
                    self._send(200, {"id": plan["id"]})

        elif pp == "/v1/plans/import":
            with _STORE_LOCK:
                existing = next(
                    (p for p in _STORE.values()
                     if p["project"] == body.get("project", "")
                     and p["query"] == body["query"]),
                    None,
                )
                if existing:
                    # Fidelity: update all fields including counters from source
                    existing.update({
                        "plan_json":        body.get("plan_json", existing["plan_json"]),
                        "outcome":          body.get("outcome", "success"),
                        "tags":             body.get("tags", ""),
                        "created_at":       body.get("created_at", existing["created_at"]),
                        "ttl":              body.get("ttl"),
                        "verb":             body.get("verb"),
                        "use_count":        body.get("use_count", 0),
                        "last_used":        body.get("last_used"),
                        "match_count":      body.get("match_count", 0),
                        "match_conf_sum":   body.get("match_conf_sum", 0.0),
                        "success_count":    body.get("success_count", 0),
                        "failure_count":    body.get("failure_count", 0),
                        "scope_tags":       body.get("scope_tags", ""),
                        "match_text":       body.get("match_text", ""),
                        "disabled_at":      body.get("disabled_at"),
                    })
                    self._send(200, {"id": existing["id"]})
                else:
                    plan = _make_plan(
                        body.get("project", ""),
                        body["query"],
                        body.get("plan_json", "{}"),
                        body.get("outcome", "success"),
                        body.get("tags", ""),
                        created_at=body.get("created_at", "1970-01-01T00:00:00Z"),
                        ttl=body.get("ttl"),
                        name=body.get("name"),
                        verb=body.get("verb"),
                        scope=body.get("scope"),
                        dimensions=body.get("dimensions"),
                        default_bindings=body.get("default_bindings"),
                        parent_dims=body.get("parent_dims"),
                        use_count=body.get("use_count", 0),
                        last_used=body.get("last_used"),
                        match_count=body.get("match_count", 0),
                        match_conf_sum=body.get("match_conf_sum", 0.0),
                        success_count=body.get("success_count", 0),
                        failure_count=body.get("failure_count", 0),
                        scope_tags=body.get("scope_tags", ""),
                        match_text=body.get("match_text", ""),
                        disabled_at=body.get("disabled_at"),
                    )
                    _STORE[plan["id"]] = plan
                    self._send(200, {"id": plan["id"]})

        elif pp == "/v1/plans/delete":
            qs = self._qs()
            pid = int(qs.get("id", "0"))
            with _STORE_LOCK:
                deleted = pid in _STORE
                _STORE.pop(pid, None)
            self._send(200, {"deleted": deleted})

        elif pp == "/v1/plans/disable":
            pid = int(body.get("id", 0))
            with _STORE_LOCK:
                if pid in _STORE:
                    _STORE[pid]["disabled_at"] = "2026-06-06T12:00:00Z"
                    self._send(200, {"updated": True})
                else:
                    self._send(200, {"updated": False})

        elif pp == "/v1/plans/enable":
            pid = int(body.get("id", 0))
            with _STORE_LOCK:
                if pid in _STORE:
                    _STORE[pid]["disabled_at"] = None
                    self._send(200, {"updated": True})
                else:
                    self._send(200, {"updated": False})

        elif pp == "/v1/plans/set_scope_tags":
            pid  = int(body.get("id", 0))
            stags = body.get("scope_tags", "")
            with _STORE_LOCK:
                if pid in _STORE:
                    _STORE[pid]["scope_tags"] = stags
                    self._send(200, {"updated": True})
                else:
                    self._send(200, {"updated": False})

        elif pp == "/v1/plans/search":
            q    = body.get("query", "").lower()
            proj = body.get("project", "")
            lim  = int(body.get("limit", 5))
            with _STORE_LOCK:
                results = [
                    p for p in _STORE.values()
                    if p.get("disabled_at") is None
                    and q in (p.get("match_text") or "").lower()
                    and (not proj or p["project"] == proj)
                ][:lim]
            self._send(200, results)

        elif pp == "/v1/plans/metrics/match":
            pid  = int(body.get("id", 0))
            conf = body.get("confidence")
            with _STORE_LOCK:
                if pid in _STORE:
                    _STORE[pid]["match_count"] = _STORE[pid].get("match_count", 0) + 1
                    if conf is not None:
                        _STORE[pid]["match_conf_sum"] = (
                            _STORE[pid].get("match_conf_sum", 0.0) + float(conf)
                        )
            self._send(200, {"ok": True})

        elif pp == "/v1/plans/metrics/run_start":
            pid = int(body.get("id", 0))
            with _STORE_LOCK:
                if pid in _STORE:
                    _STORE[pid]["use_count"]  = _STORE[pid].get("use_count", 0) + 1
                    _STORE[pid]["last_used"]  = "2026-06-06T12:00:00Z"
            self._send(200, {"ok": True})

        elif pp == "/v1/plans/metrics/run_outcome":
            pid     = int(body.get("id", 0))
            success = bool(body.get("success", False))
            with _STORE_LOCK:
                if pid in _STORE:
                    if success:
                        _STORE[pid]["success_count"] = _STORE[pid].get("success_count", 0) + 1
                    else:
                        _STORE[pid]["failure_count"] = _STORE[pid].get("failure_count", 0) + 1
            self._send(200, {"ok": True})

        else:
            self._send(404, {"error": "not found"})

    def do_GET(self):
        if not self._check_auth():
            return
        pp = self._parsed_path().path
        qs = self._qs()

        if pp == "/v1/plans/get":
            pid = qs.get("id")
            proj = qs.get("project", "")
            dims = qs.get("dimensions")
            with _STORE_LOCK:
                if pid is not None:
                    plan = _STORE.get(int(pid))
                elif proj and dims:
                    plan = next(
                        (p for p in _STORE.values()
                         if p["project"] == proj and p.get("dimensions") == dims),
                        None,
                    )
                else:
                    plan = None
            if plan is None:
                self._send(404, {"error": "not found"})
            else:
                self._send(200, plan)

        elif pp == "/v1/plans/list_active":
            outcome = qs.get("outcome", "success")
            proj    = qs.get("project", "")
            with _STORE_LOCK:
                results = [
                    p for p in _STORE.values()
                    if p.get("outcome") == outcome
                    and p.get("disabled_at") is None
                    and (not proj or p["project"] == proj)
                ]
            self._send(200, results)

        elif pp == "/v1/plans/list":
            lim              = int(qs.get("limit", "20"))
            proj             = qs.get("project", "")
            include_disabled = qs.get("include_disabled", "false") == "true"
            with _STORE_LOCK:
                results = [
                    p for p in _STORE.values()
                    if (include_disabled or p.get("disabled_at") is None)
                    and (not proj or p["project"] == proj)
                ][:lim]
            self._send(200, results)

        elif pp == "/v1/plans/exists":
            query = qs.get("query", "")
            tag   = qs.get("tag", "")
            with _STORE_LOCK:
                exists = any(
                    p["query"] == query
                    and ("," + (p.get("tags") or "") + ",").find("," + tag + ",") >= 0
                    for p in _STORE.values()
                )
            self._send(200, {"exists": exists})

        else:
            self._send(404, {"error": "not found"})

    def do_DELETE(self):
        if not self._check_auth():
            return
        pp  = self._parsed_path().path
        qs  = self._qs()
        if pp == "/v1/plans/delete":
            pid = int(qs.get("id", "0"))
            with _STORE_LOCK:
                deleted = pid in _STORE
                _STORE.pop(pid, None)
            self._send(200, {"deleted": deleted})
        else:
            self._send(404, {"error": "not found"})


# ── Fixtures ─────────────────────────────────────────────────────────────────

@pytest.fixture(scope="module")
def fake_server():
    """Start the fake PlanHandler server on a random free port."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    srv = HTTPServer(("127.0.0.1", port), _FakePlanHandler)
    t   = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    yield f"http://127.0.0.1:{port}"
    srv.shutdown()


@pytest.fixture(autouse=True)
def clear_store():
    """Clear the in-memory store and reset ID counter between tests."""
    with _STORE_LOCK:
        _STORE.clear()
        _ID_SEQ[0] = 0
    yield
    with _STORE_LOCK:
        _STORE.clear()
        _ID_SEQ[0] = 0


@pytest.fixture
def client(fake_server):
    """HttpPlanLibrary connected to the fake server."""
    c = HttpPlanLibrary(base_url=fake_server, _token=TOKEN)
    yield c
    c.close()


# ── Tests ─────────────────────────────────────────────────────────────────────

class TestSavePlan:
    def test_save_returns_id(self, client):
        pid = client.save_plan(
            query="How to research RDR patterns",
            plan_json='{"steps":[]}',
            outcome="success",
            tags="research,rdr",
            project="nexus",
            verb="research",
        )
        assert isinstance(pid, int)
        assert pid > 0

    def test_save_roundtrip_get(self, client):
        pid = client.save_plan(
            query="Test round-trip query",
            plan_json='{"v":1}',
            outcome="success",
            tags="test",
            project="proj",
        )
        row = client.get_plan(pid)
        assert row is not None
        assert row["query"] == "Test round-trip query"
        assert row["plan_json"] == '{"v":1}'
        assert row["tags"] == "test"
        assert row["project"] == "proj"

    def test_save_conflict_upserts(self, client):
        pid1 = client.save_plan(
            query="Conflict query", plan_json='{"v":1}',
            outcome="success", project="proj",
        )
        pid2 = client.save_plan(
            query="Conflict query", plan_json='{"v":2}',
            outcome="success", project="proj",
        )
        assert pid2 == pid1, "same (project, query) must return same id"
        row = client.get_plan(pid1)
        assert row["plan_json"] == '{"v":2}', "plan_json must be updated on conflict"

    def test_save_missing_outcome_defaults_success(self, client):
        pid = client.save_plan(query="Default outcome", plan_json="{}")
        row = client.get_plan(pid)
        assert row["outcome"] == "success"


class TestGetPlan:
    def test_get_absent_returns_none(self, client):
        assert client.get_plan(99999) is None

    def test_get_by_dimensions(self, client):
        pid = client.save_plan(
            query="Dim query",
            plan_json="{}",
            project="dimproj",
            dimensions='{"verb":"research"}',
        )
        row = client.get_plan_by_dimensions(project="dimproj", dimensions='{"verb":"research"}')
        assert row is not None
        assert row["id"] == pid

    def test_get_by_dimensions_absent_returns_none(self, client):
        assert client.get_plan_by_dimensions(project="x", dimensions="{}") is None


class TestDeletePlan:
    def test_delete_existing(self, client):
        pid = client.save_plan(query="To delete", plan_json="{}")
        assert client.delete_plan(pid) == 1
        assert client.get_plan(pid) is None

    def test_delete_absent_returns_zero(self, client):
        assert client.delete_plan(99999) == 0


class TestDisableEnable:
    def test_disable_sets_disabled_at(self, client):
        pid = client.save_plan(query="Disable test", plan_json="{}")
        assert client.set_plan_disabled(pid)
        row = client.get_plan(pid)
        assert row["disabled_at"] is not None

    def test_enable_clears_disabled_at(self, client):
        pid = client.save_plan(query="Enable test", plan_json="{}")
        client.set_plan_disabled(pid)
        assert client.set_plan_enabled(pid)
        row = client.get_plan(pid)
        assert row["disabled_at"] is None

    def test_disable_absent_returns_false(self, client):
        assert not client.set_plan_disabled(99999)


class TestListActivePlans:
    def test_excludes_disabled(self, client):
        pid_active   = client.save_plan(query="Active plan", plan_json="{}", project="lp")
        pid_disabled = client.save_plan(query="Disabled plan", plan_json="{}", project="lp")
        client.set_plan_disabled(pid_disabled)

        active = client.list_active_plans(project="lp")
        ids = [p["id"] for p in active]
        assert pid_active in ids
        assert pid_disabled not in ids


class TestSearchPlans:
    def test_search_hits_match_text(self, client):
        # The save_plan synthesizes match_text from query+verb+name+scope internally.
        # Use import_plan to seed a plan with a specific match_text for search testing.
        client.import_plan(
            project="search-proj",
            query="Research knowledge repos",
            plan_json="{}",
            outcome="success",
            tags="",
            created_at="2026-01-01T00:00:00Z",
            match_text="Research knowledge repositories. research scope global",
        )
        results = client.search_plans("repositories")
        assert any("Research knowledge" in r["query"] for r in results), \
            "search must find plan by match_text content"

    def test_search_empty_returns_empty(self, client):
        results = client.search_plans("no-match-token-xyz-123")
        assert results == []


class TestPlanExists:
    def test_exists_true_for_tag(self, client):
        client.save_plan(
            query="Existence check query",
            plan_json="{}",
            tags="builtin-template,research",
        )
        assert client.plan_exists("Existence check query", "builtin-template")

    def test_exists_false_for_prefix(self, client):
        client.save_plan(
            query="Prefix check query",
            plan_json="{}",
            tags="builtin-template,research",
        )
        assert not client.plan_exists("Prefix check query", "builtin")

    def test_exists_false_when_absent(self, client):
        assert not client.plan_exists("No such query", "any-tag")


class TestMetrics:
    def test_increment_match_metrics_without_confidence(self, client):
        pid = client.save_plan(query="Metrics test", plan_json="{}")
        client.increment_match_metrics(pid, confidence=None)
        row = client.get_plan(pid)
        assert row["match_count"] == 1
        assert row["match_conf_sum"] == 0.0

    def test_increment_match_metrics_with_confidence(self, client):
        pid = client.save_plan(query="Conf metrics test", plan_json="{}")
        client.increment_match_metrics(pid, confidence=0.75)
        row = client.get_plan(pid)
        assert row["match_count"] == 1
        assert abs(row["match_conf_sum"] - 0.75) < 1e-9

    def test_increment_run_started(self, client):
        pid = client.save_plan(query="Run started test", plan_json="{}")
        client.increment_run_started(pid)
        row = client.get_plan(pid)
        assert row["use_count"] == 1
        assert row["last_used"] is not None

    def test_increment_run_outcome_success(self, client):
        pid = client.save_plan(query="Run outcome success", plan_json="{}")
        client.increment_run_outcome(pid, success=True)
        row = client.get_plan(pid)
        assert row["success_count"] == 1
        assert row["failure_count"] == 0

    def test_increment_run_outcome_failure(self, client):
        pid = client.save_plan(query="Run outcome failure", plan_json="{}")
        client.increment_run_outcome(pid, success=False)
        row = client.get_plan(pid)
        assert row["success_count"] == 0
        assert row["failure_count"] == 1


class TestImportPlan:
    def test_import_preserves_counters(self, client):
        pid = client.import_plan(
            project="etl-proj",
            query="ETL fidelity test",
            plan_json='{"etl":true}',
            outcome="success",
            tags="etl",
            created_at="2025-06-01T10:00:00Z",
            use_count=42,
            last_used="2025-06-05T12:00:00Z",
            match_count=99,
            match_conf_sum=12.5,
            success_count=40,
            failure_count=2,
            scope_tags="knowledge__nexus",
            match_text="ETL fidelity test. research scope global",
        )
        assert isinstance(pid, int)
        assert pid > 0

        row = client.get_plan(pid)
        assert row is not None
        assert row["use_count"] == 42
        assert row["match_count"] == 99
        assert abs(row["match_conf_sum"] - 12.5) < 1e-9
        assert row["success_count"] == 40
        assert row["failure_count"] == 2
        assert row["scope_tags"] == "knowledge__nexus"
        # created_at comes back as a string from the server
        assert "2025-06-01" in str(row.get("created_at", ""))

    def test_import_idempotent(self, client):
        kwargs: dict = dict(
            project="etl-idm",
            query="Idempotent import query",
            plan_json="{}",
            outcome="success",
            tags="",
            created_at="2025-06-01T10:00:00Z",
            use_count=5,
            match_count=3,
            match_conf_sum=1.5,
            success_count=3,
            failure_count=0,
        )
        pid1 = client.import_plan(**kwargs)
        pid2 = client.import_plan(**kwargs)
        assert pid2 == pid1, "idempotent re-import must return same id"

    def test_import_disabled_at(self, client):
        pid = client.import_plan(
            project="dis-proj",
            query="Disabled import",
            plan_json="{}",
            outcome="success",
            tags="",
            created_at="2025-01-01T00:00:00Z",
            use_count=0,
            match_count=0,
            match_conf_sum=0.0,
            success_count=0,
            failure_count=0,
            disabled_at="2025-06-01T12:00:00Z",
        )
        row = client.get_plan(pid)
        assert row["disabled_at"] is not None
        assert "2025-06-01" in str(row["disabled_at"])


class TestNormalize:
    """Verify normalization of numeric types from JSON responses."""

    def test_id_is_int(self, client):
        pid = client.save_plan(query="Normalize id", plan_json="{}")
        row = client.get_plan(pid)
        assert isinstance(row["id"], int)

    def test_counters_are_int(self, client):
        pid = client.save_plan(query="Normalize counters", plan_json="{}")
        row = client.get_plan(pid)
        for field in ("use_count", "match_count", "success_count", "failure_count"):
            assert isinstance(row[field], int), f"{field} must be int"

    def test_match_conf_sum_is_float(self, client):
        pid = client.save_plan(query="Normalize conf", plan_json="{}")
        row = client.get_plan(pid)
        assert isinstance(row["match_conf_sum"], float)

    def test_tags_default_empty_string(self, client):
        pid = client.save_plan(query="Tag default", plan_json="{}")
        row = client.get_plan(pid)
        assert row["tags"] == ""

    def test_scope_tags_default_empty_string(self, client):
        pid = client.save_plan(query="ScopeTags default", plan_json="{}")
        row = client.get_plan(pid)
        assert row["scope_tags"] == ""


class TestSetScopeTags:
    def test_set_scope_tags_updates(self, client):
        pid = client.save_plan(query="Scope tags plan", plan_json="{}")
        result = client.set_scope_tags(pid, "knowledge__nexus,rdr__nexus")
        assert result
        row = client.get_plan(pid)
        assert row["scope_tags"] == "knowledge__nexus,rdr__nexus"


class TestListPlans:
    def test_list_excludes_disabled_by_default(self, client):
        pid_a = client.save_plan(query="List plan A", plan_json="{}", project="list-proj")
        pid_d = client.save_plan(query="List plan D", plan_json="{}", project="list-proj")
        client.set_plan_disabled(pid_d)

        rows   = client.list_plans(project="list-proj")
        row_ids = [r["id"] for r in rows]
        assert pid_a in row_ids
        assert pid_d not in row_ids

    def test_list_include_disabled(self, client):
        pid_a = client.save_plan(query="List inc A", plan_json="{}", project="list-inc")
        pid_d = client.save_plan(query="List inc D", plan_json="{}", project="list-inc")
        client.set_plan_disabled(pid_d)

        rows   = client.list_plans(project="list-inc", include_disabled=True)
        row_ids = [r["id"] for r in rows]
        assert pid_a in row_ids
        assert pid_d in row_ids


class TestAuthErrors:
    def test_wrong_token_raises(self, fake_server):
        bad = HttpPlanLibrary(base_url=fake_server, _token="wrong-token")
        with pytest.raises(Exception, match="401"):
            bad.save_plan(query="Should fail", plan_json="{}")
        bad.close()
