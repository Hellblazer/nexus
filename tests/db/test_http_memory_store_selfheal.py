# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""Store-level self-heal + bypass-guard tests for HttpMemoryStore
(nexus-bikit.4 — the canonical first ``RefreshableHttpStoreMixin``
consumer, template for the nexus-f2qvx sweep).

``tests/db/test_refreshable_client.py`` (nexus-bikit.2/.3) already proves
the MIXIN self-heals over a synthetic ``/v1/echo`` endpoint. This file
proves the same behaviour end-to-end through a REAL ``HttpMemoryStore``
instance against REAL memory-service paths (``/v1/memory/put`` and
``/v1/memory/get``) — the thing an adopter can get wrong even with a
correct mixin, e.g. by leaving one inline ``self._client.*`` call site
un-migrated (exactly what the bypass-guard test below catches
mechanically, per this project's "gates must be scripted, not ambient"
convention).

Harness: a minimal rotatable-bearer fake service (mirrors
``tests/db/test_refreshable_client.py``'s ``_FakeHandler`` pattern, not
the larger faithful-replica server in ``tests/db/test_http_memory_store.py``
which hardcodes a single fixed token with no rotation lever).
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

from nexus.db.t2.http_memory_store import HttpMemoryStore

# ── Rotatable-bearer fake memory service ────────────────────────────────────

_INITIAL_BEARER = "selfheal-initial-bearer"

_VALID_BEARER: str = _INITIAL_BEARER
#: "METHOD /path" -> inbound request count, INCLUDING 401s — lets tests
#: assert "retried exactly once" by counting round trips.
_REQUEST_COUNT: dict[str, int] = {}
#: In-memory ``{(project, title): entry}`` store, reset per test.
_ENTRIES: dict[tuple[str, str], dict[str, Any]] = {}
_ID_SEQ = [0]


def _reset_fake_service_state() -> None:
    global _VALID_BEARER
    _VALID_BEARER = _INITIAL_BEARER
    _REQUEST_COUNT.clear()
    _ENTRIES.clear()
    _ID_SEQ[0] = 0


class _RotatableBearerMemoryHandler(BaseHTTPRequestHandler):
    """Minimal ``/v1/memory/put`` + ``/v1/memory/get`` stub with a single
    rotatable bearer token — just enough surface for one write-path and
    one read-path self-heal test."""

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
        if path != "/v1/memory/put":
            self._send(404, {"error": "not found"})
            return
        if not self._check_bearer():
            return
        length = int(self.headers.get("Content-Length", "0"))
        body = json.loads(self.rfile.read(length)) if length else {}
        key = (body["project"], body["title"])
        entry = _ENTRIES.get(key)
        if entry is None:
            _ID_SEQ[0] += 1
            entry = {
                "id": _ID_SEQ[0],
                "project": body["project"],
                "title": body["title"],
                "content": body["content"],
                "tags": body.get("tags", ""),
                "ttl": body.get("ttl"),
                "agent": body.get("agent"),
                "session": body.get("session"),
                "timestamp": "2026-07-12T00:00:00Z",
                "access_count": 0,
                "last_accessed": "",
            }
            _ENTRIES[key] = entry
        else:
            entry["content"] = body["content"]
        self._send(200, {"id": entry["id"]})

    def do_GET(self):  # noqa: N802
        parsed = urlparse(self.path)
        path = parsed.path
        self._record("GET", path)
        if path != "/v1/memory/get":
            self._send(404, {"error": "not found"})
            return
        if not self._check_bearer():
            return
        params = {k: v[0] for k, v in parse_qs(parsed.query).items()}
        entry = _ENTRIES.get((params.get("project", ""), params.get("title", "")))
        if entry is None:
            self._send(404, {"error": "not found"})
            return
        self._send(200, dict(entry))


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture
def fake_service(monkeypatch: pytest.MonkeyPatch) -> Any:
    _reset_fake_service_state()
    port = _free_port()
    server = HTTPServer(("127.0.0.1", port), _RotatableBearerMemoryHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()

    monkeypatch.setenv("NX_SERVICE_HOST", "127.0.0.1")
    monkeypatch.setenv("NX_SERVICE_PORT", str(port))
    monkeypatch.setenv("NX_SERVICE_TOKEN", _VALID_BEARER)
    monkeypatch.delenv("NX_SERVICE_URL", raising=False)

    yield port

    server.shutdown()
    server.server_close()


class TestHttpMemoryStoreSelfHeal:
    """Store-level (not mixin-level) proof that adoption actually wired
    every call site through the self-healing transport."""

    def test_put_selfheals_on_rotated_bearer(
        self, fake_service, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Write-path: put() must self-heal after a sibling process rotates
        the bearer token, not surface a 401 to the caller."""
        store = HttpMemoryStore()

        row_id = store.put("selfheal-proj", "w1", "before rotation", ttl=30)
        assert isinstance(row_id, int)
        assert _REQUEST_COUNT["POST /v1/memory/put"] == 1

        global _VALID_BEARER
        _VALID_BEARER = "rotated-bearer-write-path"
        monkeypatch.setenv("NX_SERVICE_TOKEN", _VALID_BEARER)

        row_id2 = store.put("selfheal-proj", "w1", "after rotation", ttl=30)
        assert row_id2 == row_id
        # 1 (baseline) + 1 (401 on stale header) + 1 (retry, succeeds) == 3.
        assert _REQUEST_COUNT["POST /v1/memory/put"] == 3, (
            "expected exactly one failed attempt followed by one successful "
            "retry on the WRITE path — not a retry loop"
        )
        store.close()

    def test_get_selfheals_on_rotated_bearer(
        self, fake_service, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Read-path: get() must self-heal too — a put()-only fix would
        leave get()/list_entries()/etc. still vulnerable to the exact bug
        this mixin exists to close (bead's own explicit callout)."""
        store = HttpMemoryStore()
        store.put("selfheal-proj", "r1", "read path content", ttl=30)
        assert "GET /v1/memory/get" not in _REQUEST_COUNT

        baseline = store.get(project="selfheal-proj", title="r1")
        assert baseline is not None
        assert baseline["content"] == "read path content"
        assert _REQUEST_COUNT["GET /v1/memory/get"] == 1

        global _VALID_BEARER
        _VALID_BEARER = "rotated-bearer-read-path"
        monkeypatch.setenv("NX_SERVICE_TOKEN", _VALID_BEARER)

        result = store.get(project="selfheal-proj", title="r1")
        assert result is not None
        assert result["content"] == "read path content"
        # 1 (baseline) + 1 (401 on stale header) + 1 (retry, succeeds) == 3.
        assert _REQUEST_COUNT["GET /v1/memory/get"] == 3, (
            "expected exactly one failed attempt followed by one successful "
            "retry on the READ path — not a retry loop"
        )
        store.close()

    def test_get_missing_still_returns_none_not_an_exception(
        self, fake_service
    ) -> None:
        """404-as-None contract (relay item #2) must survive adoption: a
        genuine not-found is NOT retryable and NOT an exception to get()'s
        caller, even though the mixin's _get raises httpx.HTTPStatusError
        internally for ANY non-2xx."""
        store = HttpMemoryStore()
        result = store.get(project="selfheal-proj", title="does-not-exist")
        assert result is None
        store.close()


class TestNoBypassOfMixinTransport:
    """Scripted regression guard (deferred from nexus-bikit.3 review,
    due in THIS bead): a future edit must not reintroduce a direct
    ``self._client.<verb>(...)`` call that bypasses the mixin's
    self-healing ``_get``/``_post``/``_delete`` wrappers — exactly the
    class of gap this adoption exists to close. Scripted per this
    project's "functional gates must be self-provisioning, never ambient
    machine state" convention (no reliance on a human remembering to grep
    before merging)."""

    _BYPASS_PATTERN = re.compile(r"self\._client\.(get|post|put|delete|patch|request)\(")

    def test_http_memory_store_has_zero_inline_client_call_sites(self) -> None:
        source_path = (
            Path(__file__).resolve().parent.parent.parent
            / "src" / "nexus" / "db" / "t2" / "http_memory_store.py"
        )
        source = source_path.read_text()
        matches = self._BYPASS_PATTERN.findall(source)
        assert matches == [], (
            f"found {len(matches)} inline self._client.<verb>(...) call "
            f"site(s) in http_memory_store.py that bypass "
            f"RefreshableHttpStoreMixin's self-healing transport: {matches}"
        )
