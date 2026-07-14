# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""Store-level self-heal + bypass-guard tests for HttpChashIndex
(nexus-f2qvx.3 — mixin-adoption sweep, batch C).

``tests/db/test_http_chash_index.py`` already covers the contract
(upsert/lookup/count/etc.) via an in-process ``HTTPServer`` fake that pins
BOTH ``base_url`` and ``_token`` explicitly — a fully-pinned test double,
which per ``RefreshableHttpStoreMixin._invalidate_and_reresolve``'s own
contract CANNOT self-heal (nothing to re-resolve). Proving genuine
self-heal (rotate-then-retry-and-succeed) therefore needs a harness where
the token is resolved from the environment, not pinned via ``_token=`` —
same shape as ``tests/db/test_http_memory_store_selfheal.py``'s
``_RotatableBearerMemoryHandler``, adapted to the chash endpoints
(``/v1/chash/upsert`` write, ``/v1/chash/is_empty`` read).
"""
from __future__ import annotations

import json
import re
import socket
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import pytest

from nexus.db.t2.http_chash_index import HttpChashIndex

# ── Rotatable-bearer fake chash service ──────────────────────────────────────

_INITIAL_BEARER = "selfheal-chash-initial-bearer"

_VALID_BEARER: str = _INITIAL_BEARER
#: "METHOD /path" -> inbound request count, INCLUDING 401s — lets tests
#: assert "retried exactly once" by counting round trips.
_REQUEST_COUNT: dict[str, int] = {}
#: (chash, collection) -> record, reset per test.
_ROWS: dict[tuple[str, str], dict[str, Any]] = {}


def _reset_fake_service_state() -> None:
    global _VALID_BEARER
    _VALID_BEARER = _INITIAL_BEARER
    _REQUEST_COUNT.clear()
    _ROWS.clear()


class _RotatableBearerChashHandler(BaseHTTPRequestHandler):
    """Minimal ``/v1/chash/upsert`` + ``.../is_empty`` stub with a single
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
        if path != "/v1/chash/upsert":
            self._send(404, {"error": "not found"})
            return
        if not self._check_bearer():
            return
        length = int(self.headers.get("Content-Length", "0"))
        body = json.loads(self.rfile.read(length)) if length else {}
        _ROWS[(body["chash"], body["collection"])] = {
            "chash": body["chash"],
            "physical_collection": body["collection"],
            "created_at": "2026-06-01T00:00:00Z",
        }
        self._send(200, {"ok": True})

    def do_GET(self):  # noqa: N802
        parsed = urlparse(self.path)
        path = parsed.path
        self._record("GET", path)
        if path != "/v1/chash/is_empty":
            self._send(404, {"error": "not found"})
            return
        if not self._check_bearer():
            return
        self._send(200, {"empty": len(_ROWS) == 0})


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture
def fake_service(monkeypatch: pytest.MonkeyPatch) -> Any:
    _reset_fake_service_state()
    port = _free_port()
    server = HTTPServer(("127.0.0.1", port), _RotatableBearerChashHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()

    monkeypatch.setenv("NX_SERVICE_HOST", "127.0.0.1")
    monkeypatch.setenv("NX_SERVICE_PORT", str(port))
    monkeypatch.setenv("NX_SERVICE_TOKEN", _VALID_BEARER)
    monkeypatch.delenv("NX_SERVICE_URL", raising=False)

    yield port

    server.shutdown()
    server.server_close()


class TestHttpChashIndexSelfHeal:
    """Store-level (not mixin-level) proof that adoption actually wired
    every call site through the self-healing transport."""

    def test_upsert_selfheals_on_rotated_bearer(
        self, fake_service, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Write-path: upsert() must self-heal after a sibling process
        rotates the bearer token, not surface a 401 to the caller."""
        store = HttpChashIndex()

        store.upsert(chash="selfheal-c1", collection="selfheal-col")
        assert _REQUEST_COUNT["POST /v1/chash/upsert"] == 1

        global _VALID_BEARER
        _VALID_BEARER = "rotated-bearer-write-path"
        monkeypatch.setenv("NX_SERVICE_TOKEN", _VALID_BEARER)

        store.upsert(chash="selfheal-c2", collection="selfheal-col")
        # 1 (baseline) + 1 (401 on stale header) + 1 (retry, succeeds) == 3.
        assert _REQUEST_COUNT["POST /v1/chash/upsert"] == 3, (
            "expected exactly one failed attempt followed by one successful "
            "retry on the WRITE path — not a retry loop"
        )
        assert len(_ROWS) == 2
        store.close()

    def test_is_empty_selfheals_on_rotated_bearer(
        self, fake_service, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Read-path: is_empty() must self-heal too — an upsert()-only fix
        would leave every other read (lookup, count_for_collection, ...)
        still vulnerable to the exact bug this mixin exists to close."""
        store = HttpChashIndex()

        assert "GET /v1/chash/is_empty" not in _REQUEST_COUNT
        baseline = store.is_empty()
        assert baseline is True
        assert _REQUEST_COUNT["GET /v1/chash/is_empty"] == 1

        global _VALID_BEARER
        _VALID_BEARER = "rotated-bearer-read-path"
        monkeypatch.setenv("NX_SERVICE_TOKEN", _VALID_BEARER)

        result = store.is_empty()
        assert result is True
        # 1 (baseline) + 1 (401 on stale header) + 1 (retry, succeeds) == 3.
        assert _REQUEST_COUNT["GET /v1/chash/is_empty"] == 3, (
            "expected exactly one failed attempt followed by one successful "
            "retry on the READ path — not a retry loop"
        )
        store.close()


class TestNoBypassOfMixinTransport:
    """Scripted regression guard (mirrors
    tests/db/test_http_centroid_store_selfheal.py): a future edit must not
    reintroduce a direct ``self._client.<verb>(...)`` call that bypasses
    the mixin's self-healing ``_get``/``_post``/``_delete`` wrappers."""

    _BYPASS_PATTERN = re.compile(r"self\._client\.(get|post|put|delete|patch|request)\(")

    def test_http_chash_index_has_zero_inline_client_call_sites(self) -> None:
        source_path = (
            Path(__file__).resolve().parent.parent.parent
            / "src" / "nexus" / "db" / "t2" / "http_chash_index.py"
        )
        source = source_path.read_text()
        matches = self._BYPASS_PATTERN.findall(source)
        assert matches == [], (
            f"found {len(matches)} inline self._client.<verb>(...) call "
            f"site(s) in http_chash_index.py that bypass "
            f"RefreshableHttpStoreMixin's self-healing transport: {matches}"
        )
