# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""Store-level self-heal + bypass-guard tests for HttpDocumentHighlightsStore
(nexus-f2qvx.2 — mixin-adoption sweep, batch B).

``tests/db/test_refreshable_client.py`` (nexus-bikit.2/.3) already proves the
MIXIN self-heals over a synthetic ``/v1/echo`` endpoint, and
``tests/db/test_http_memory_store_selfheal.py`` (nexus-bikit.4) proves it for
the canonical first adopter. This file proves the same behaviour end-to-end
through a REAL ``HttpDocumentHighlightsStore`` instance against REAL
document-highlights-service paths (``/v1/aspects/highlights/upsert`` write,
``/v1/aspects/highlights/get`` read) — the thing an adopter can get wrong
even with a correct mixin, e.g. by leaving one inline ``self._client.*``
call site un-migrated (exactly what the bypass-guard test below catches
mechanically).

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

from nexus.db.t2.document_highlights import HighlightRecord
from nexus.db.t2.http_document_highlights_store import HttpDocumentHighlightsStore

# ── Rotatable-bearer fake highlights service ────────────────────────────────

_INITIAL_BEARER = "selfheal-highlights-initial-bearer"

_VALID_BEARER: str = _INITIAL_BEARER
#: "METHOD /path" -> inbound request count, INCLUDING 401s — lets tests
#: assert "retried exactly once" by counting round trips.
_REQUEST_COUNT: dict[str, int] = {}
#: In-memory ``{doc_id: row}`` store, reset per test.
_ROWS: dict[str, dict[str, Any]] = {}


def _reset_fake_service_state() -> None:
    global _VALID_BEARER
    _VALID_BEARER = _INITIAL_BEARER
    _REQUEST_COUNT.clear()
    _ROWS.clear()


class _RotatableBearerHighlightsHandler(BaseHTTPRequestHandler):
    """Minimal ``/v1/aspects/highlights/upsert`` + ``/v1/aspects/highlights/get``
    stub with a single rotatable bearer token — just enough surface for one
    write-path and one read-path self-heal test."""

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
        if path != "/v1/aspects/highlights/upsert":
            self._send(404, {"error": "not found"})
            return
        if not self._check_bearer():
            return
        length = int(self.headers.get("Content-Length", "0"))
        body = json.loads(self.rfile.read(length)) if length else {}
        _ROWS[body["doc_id"]] = body
        self._send(200, {"written": True})

    def do_GET(self):  # noqa: N802
        parsed = urlparse(self.path)
        path = parsed.path
        self._record("GET", path)
        if path != "/v1/aspects/highlights/get":
            self._send(404, {"error": "not found"})
            return
        if not self._check_bearer():
            return
        params = {k: v[0] for k, v in parse_qs(parsed.query).items()}
        doc_id = params.get("doc_id", "")
        row = _ROWS.get(doc_id)
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
    server = HTTPServer(("127.0.0.1", port), _RotatableBearerHighlightsHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()

    monkeypatch.setenv("NX_SERVICE_HOST", "127.0.0.1")
    monkeypatch.setenv("NX_SERVICE_PORT", str(port))
    monkeypatch.setenv("NX_SERVICE_TOKEN", _VALID_BEARER)
    monkeypatch.delenv("NX_SERVICE_URL", raising=False)

    yield port

    server.shutdown()
    server.server_close()


def _record(**kwargs: Any) -> HighlightRecord:
    defaults: dict[str, Any] = {
        "doc_id": "1.2.3",
        "source_uri": "x-devonthink://abc",
        "collection": "c",
        "highlights_md": "# Highlights",
        "mentions_md": "",
        "ingested_at": "2026-07-12T00:00:00Z",
    }
    defaults.update(kwargs)
    return HighlightRecord(**defaults)


class TestHttpDocumentHighlightsStoreSelfHeal:
    """Store-level (not mixin-level) proof that adoption actually wired
    every call site through the self-healing transport."""

    def test_upsert_selfheals_on_rotated_bearer(
        self, fake_service, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Write-path: upsert() must self-heal after a sibling process
        rotates the bearer token, not surface a 401 to the caller."""
        store = HttpDocumentHighlightsStore()

        assert store.upsert(_record(doc_id="selfheal-1")) is True
        assert _REQUEST_COUNT["POST /v1/aspects/highlights/upsert"] == 1

        global _VALID_BEARER
        _VALID_BEARER = "rotated-bearer-write-path"
        monkeypatch.setenv("NX_SERVICE_TOKEN", _VALID_BEARER)

        assert store.upsert(_record(doc_id="selfheal-2")) is True
        # 1 (baseline) + 1 (401 on stale header) + 1 (retry, succeeds) == 3.
        assert _REQUEST_COUNT["POST /v1/aspects/highlights/upsert"] == 3, (
            "expected exactly one failed attempt followed by one successful "
            "retry on the WRITE path — not a retry loop"
        )
        store.close()

    def test_get_selfheals_on_rotated_bearer(
        self, fake_service, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Read-path: get() must self-heal too — an upsert()-only fix would
        leave every other read (get_by_source_uri, list, ...) still
        vulnerable to the exact bug this mixin exists to close."""
        store = HttpDocumentHighlightsStore()
        store.upsert(_record(doc_id="findme"))
        assert "GET /v1/aspects/highlights/get" not in _REQUEST_COUNT

        baseline = store.get("findme")
        assert baseline is not None
        assert baseline.doc_id == "findme"
        assert _REQUEST_COUNT["GET /v1/aspects/highlights/get"] == 1

        global _VALID_BEARER
        _VALID_BEARER = "rotated-bearer-read-path"
        monkeypatch.setenv("NX_SERVICE_TOKEN", _VALID_BEARER)

        result = store.get("findme")
        assert result is not None
        assert result.doc_id == "findme"
        # 1 (baseline) + 1 (401 on stale header) + 1 (retry, succeeds) == 3.
        assert _REQUEST_COUNT["GET /v1/aspects/highlights/get"] == 3, (
            "expected exactly one failed attempt followed by one successful "
            "retry on the READ path — not a retry loop"
        )
        store.close()

    def test_get_missing_still_returns_none_not_an_exception(
        self, fake_service
    ) -> None:
        """404-as-None contract must survive adoption: a genuine not-found
        is NOT retryable and NOT an exception to get()'s caller, even
        though the mixin's _get raises httpx.HTTPStatusError internally
        for ANY non-2xx."""
        store = HttpDocumentHighlightsStore()
        assert store.get("never-seen-doc-id") is None
        store.close()


class TestNoBypassOfMixinTransport:
    """Scripted regression guard (mirrors
    tests/db/test_http_memory_store_selfheal.py): a future edit must not
    reintroduce a direct ``self._client.<verb>(...)`` call that bypasses
    the mixin's self-healing ``_get``/``_post``/``_delete`` wrappers."""

    _BYPASS_PATTERN = re.compile(r"self\._client\.(get|post|put|delete|patch|request)\(")

    def test_http_document_highlights_store_has_zero_inline_client_call_sites(
        self,
    ) -> None:
        source_path = (
            Path(__file__).resolve().parent.parent.parent
            / "src" / "nexus" / "db" / "t2" / "http_document_highlights_store.py"
        )
        source = source_path.read_text()
        matches = self._BYPASS_PATTERN.findall(source)
        assert matches == [], (
            f"found {len(matches)} inline self._client.<verb>(...) call "
            f"site(s) in http_document_highlights_store.py that bypass "
            f"RefreshableHttpStoreMixin's self-healing transport: {matches}"
        )
