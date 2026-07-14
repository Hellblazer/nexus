# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""Store-level self-heal + bypass-guard tests for HttpPlanLibrary
(nexus-f2qvx.1 — mixin-adoption sweep, batch A).

Before this adoption, ``get_plan``, ``get_plan_by_dimensions``,
``delete_plan``, ``list_active_plans``, ``list_plans``, and ``plan_exists``
all called ``self._client.<verb>(...)`` INLINE — exactly the read/delete-path
401 gap the mixin exists to close (relay item #4: "convert EVERY inline
self._client.<verb>(...) call site to the mixin's _post/_get/_delete —
including READ paths, not just the write path that already used _post").
This file proves the read path actually self-heals now, through a REAL
``HttpPlanLibrary`` instance, and that ``get_plan``'s 404-as-``None``
contract survives the conversion.

Harness: a minimal rotatable-bearer fake service, same shape as
``_RotatableBearerMemoryHandler`` in the memory-store self-heal tests.
"""
from __future__ import annotations

import json
import re
import socket
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

import pytest

from nexus.db.t2.http_plan_library import HttpPlanLibrary

# ── Rotatable-bearer fake plans service ─────────────────────────────────────

_INITIAL_BEARER = "selfheal-plans-initial-bearer"

_VALID_BEARER: str = _INITIAL_BEARER
#: "METHOD /path" -> inbound request count, INCLUDING 401s — lets tests
#: assert "retried exactly once" by counting round trips.
_REQUEST_COUNT: dict[str, int] = {}
#: In-memory ``{id: plan_dict}`` store, reset per test.
_PLANS: dict[int, dict[str, Any]] = {}
_ID_SEQ = [0]


def _reset_fake_service_state() -> None:
    global _VALID_BEARER
    _VALID_BEARER = _INITIAL_BEARER
    _REQUEST_COUNT.clear()
    _PLANS.clear()
    _ID_SEQ[0] = 0


class _RotatableBearerPlansHandler(BaseHTTPRequestHandler):
    """Minimal ``/v1/plans/save`` + ``/v1/plans/get`` stub with a single
    rotatable bearer token — just enough surface for one write-path and
    one read-path self-heal test. The read path (``plans/get``) is one of
    the EXACT pre-adoption bypass sites (``get_plan`` called
    ``self._client.get`` directly, with a hand-rolled 404-as-None check
    that never went through the mixin's raise-on-non-2xx contract)."""

    def log_message(self, fmt, *args):  # noqa: A002 — matches BaseHTTPRequestHandler signature
        pass  # suppress test noise

    def _send(self, status: int, body: Any) -> None:
        payload = json.dumps(body).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def _record(self, method: str, path: str) -> None:
        key = f"{method} {path}"
        _REQUEST_COUNT[key] = _REQUEST_COUNT.get(key, 0) + 1

    def _check_bearer(self) -> bool:
        auth = self.headers.get("Authorization", "")
        if auth != f"Bearer {_VALID_BEARER}":
            self._send(401, {"error": "unauthorized"})
            return False
        return True

    def do_POST(self):  # noqa: N802
        path = self.path.split("?")[0]
        self._record("POST", path)
        if path != "/v1/plans/save":
            self._send(404, {"error": "not found"})
            return
        if not self._check_bearer():
            return
        length = int(self.headers.get("Content-Length", "0"))
        body = json.loads(self.rfile.read(length)) if length else {}
        _ID_SEQ[0] += 1
        row = {
            "id":         _ID_SEQ[0],
            "query":      body.get("query", ""),
            "plan_json":  body.get("plan_json", ""),
            "outcome":    body.get("outcome", "success"),
            "tags":       body.get("tags", ""),
            "project":    body.get("project", ""),
            "created_at": "2026-07-12T00:00:00Z",
        }
        _PLANS[row["id"]] = row
        self._send(200, {"id": row["id"]})

    def do_GET(self):  # noqa: N802
        parsed = urlparse(self.path)
        path = parsed.path
        self._record("GET", path)
        if path != "/v1/plans/get":
            self._send(404, {"error": "not found"})
            return
        if not self._check_bearer():
            return
        params = {k: v[0] for k, v in parse_qs(parsed.query).items()}
        plan_id = params.get("id")
        if plan_id is None:
            self._send(404, {"error": "not found"})
            return
        row = _PLANS.get(int(plan_id))
        if row is None:
            self._send(404, {"error": "not found"})
            return
        self._send(200, dict(row))


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture
def fake_service(monkeypatch: pytest.MonkeyPatch) -> Any:
    _reset_fake_service_state()
    port = _free_port()
    server = HTTPServer(("127.0.0.1", port), _RotatableBearerPlansHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()

    monkeypatch.setenv("NX_SERVICE_HOST", "127.0.0.1")
    monkeypatch.setenv("NX_SERVICE_PORT", str(port))
    monkeypatch.setenv("NX_SERVICE_TOKEN", _VALID_BEARER)
    monkeypatch.delenv("NX_SERVICE_URL", raising=False)

    yield port

    server.shutdown()
    server.server_close()


class TestHttpPlanLibrarySelfHeal:
    """Store-level (not mixin-level) proof that adoption actually wired
    every call site through the self-healing transport."""

    def test_save_plan_selfheals_on_rotated_bearer(
        self, fake_service, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Write-path: save_plan() must self-heal after a sibling process
        rotates the bearer token, not surface a 401 to the caller."""
        store = HttpPlanLibrary()

        pid = store.save_plan(query="selfheal query 1", plan_json="{}")
        assert isinstance(pid, int)
        assert _REQUEST_COUNT["POST /v1/plans/save"] == 1

        global _VALID_BEARER
        _VALID_BEARER = "rotated-bearer-write-path"
        monkeypatch.setenv("NX_SERVICE_TOKEN", _VALID_BEARER)

        pid2 = store.save_plan(query="selfheal query 2", plan_json="{}")
        assert isinstance(pid2, int)
        assert pid2 != pid
        # 1 (baseline) + 1 (401 on stale header) + 1 (retry, succeeds) == 3.
        assert _REQUEST_COUNT["POST /v1/plans/save"] == 3, (
            "expected exactly one failed attempt followed by one successful "
            "retry on the WRITE path — not a retry loop"
        )
        store.close()

    def test_get_plan_selfheals_on_rotated_bearer(
        self, fake_service, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Read-path: get_plan() must self-heal too — a save_plan()-only
        fix would leave every other read (list_plans, plan_exists, ...)
        still vulnerable to the exact bug this mixin exists to close."""
        store = HttpPlanLibrary()
        pid = store.save_plan(query="findme-plan", plan_json="{}")
        assert "GET /v1/plans/get" not in _REQUEST_COUNT

        baseline = store.get_plan(pid)
        assert baseline is not None
        assert baseline["query"] == "findme-plan"
        assert _REQUEST_COUNT["GET /v1/plans/get"] == 1

        global _VALID_BEARER
        _VALID_BEARER = "rotated-bearer-read-path"
        monkeypatch.setenv("NX_SERVICE_TOKEN", _VALID_BEARER)

        result = store.get_plan(pid)
        assert result is not None
        assert result["query"] == "findme-plan"
        # 1 (baseline) + 1 (401 on stale header) + 1 (retry, succeeds) == 3.
        assert _REQUEST_COUNT["GET /v1/plans/get"] == 3, (
            "expected exactly one failed attempt followed by one successful "
            "retry on the READ path — not a retry loop"
        )
        store.close()

    def test_get_plan_missing_still_returns_none_not_an_exception(
        self, fake_service
    ) -> None:
        """404-as-None contract must survive adoption: a genuine
        not-found is NOT retryable and NOT an exception to get_plan()'s
        caller, even though the mixin's _get raises
        httpx.HTTPStatusError internally for ANY non-2xx."""
        store = HttpPlanLibrary()
        assert store.get_plan(999999) is None
        store.close()


class TestNoBypassOfMixinTransport:
    """Scripted regression guard (mirrors
    tests/db/test_http_memory_store_selfheal.py): a future edit must not
    reintroduce a direct ``self._client.<verb>(...)`` call that bypasses
    the mixin's self-healing ``_get``/``_post``/``_delete`` wrappers —
    exactly the class of gap this adoption closed (get_plan,
    get_plan_by_dimensions, delete_plan, list_active_plans, list_plans,
    and plan_exists were all inline pre-adoption)."""

    _BYPASS_PATTERN = re.compile(r"self\._client\.(get|post|put|delete|patch|request)\(")

    def test_http_plan_library_has_zero_inline_client_call_sites(self) -> None:
        source_path = (
            Path(__file__).resolve().parent.parent.parent
            / "src" / "nexus" / "db" / "t2" / "http_plan_library.py"
        )
        source = source_path.read_text()
        matches = self._BYPASS_PATTERN.findall(source)
        assert matches == [], (
            f"found {len(matches)} inline self._client.<verb>(...) call "
            f"site(s) in http_plan_library.py that bypass "
            f"RefreshableHttpStoreMixin's self-healing transport: {matches}"
        )
