# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""Store-level self-heal + bypass-guard tests for HttpTelemetryStore
(nexus-f2qvx.1 — mixin-adoption sweep, batch A).

Before this adoption, ``get_relevance_log`` and ``query_collection_stats``
called ``self._client.get(...)`` INLINE — exactly the read-path 401 gap the
mixin exists to close (relay item #4: "convert EVERY inline
self._client.<verb>(...) call site to the mixin's _post/_get/_delete —
including READ paths, not just the write path that already used _post").
This file proves the read path actually self-heals now, through a REAL
``HttpTelemetryStore`` instance, not just the mixin in isolation.

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

from nexus.db.t2.http_telemetry_store import HttpTelemetryStore

# ── Rotatable-bearer fake telemetry service ─────────────────────────────────

_INITIAL_BEARER = "selfheal-telemetry-initial-bearer"

_VALID_BEARER: str = _INITIAL_BEARER
#: "METHOD /path" -> inbound request count, INCLUDING 401s — lets tests
#: assert "retried exactly once" by counting round trips.
_REQUEST_COUNT: dict[str, int] = {}
#: In-memory relevance_log rows, reset per test.
_ROWS: list[dict[str, Any]] = []
_ID_SEQ = [0]


def _reset_fake_service_state() -> None:
    global _VALID_BEARER
    _VALID_BEARER = _INITIAL_BEARER
    _REQUEST_COUNT.clear()
    _ROWS.clear()
    _ID_SEQ[0] = 0


class _RotatableBearerTelemetryHandler(BaseHTTPRequestHandler):
    """Minimal ``/v1/telemetry/relevance/log`` + ``/v1/telemetry/relevance/query``
    stub with a single rotatable bearer token — just enough surface for one
    write-path and one read-path self-heal test. The read path
    (``relevance/query``) is the EXACT pre-adoption bypass site
    (``get_relevance_log`` called ``self._client.get`` directly)."""

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
        if path != "/v1/telemetry/relevance/log":
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
            "chunk_id":   body.get("chunk_id", ""),
            "collection": body.get("collection", ""),
            "action":     body.get("action", ""),
            "session_id": body.get("session_id", ""),
            "timestamp":  "2026-07-12T00:00:00Z",
        }
        _ROWS.append(row)
        self._send(200, {"id": row["id"]})

    def do_GET(self):  # noqa: N802
        parsed = urlparse(self.path)
        path = parsed.path
        self._record("GET", path)
        if path != "/v1/telemetry/relevance/query":
            self._send(404, {"error": "not found"})
            return
        if not self._check_bearer():
            return
        params = {k: v[0] for k, v in parse_qs(parsed.query).items()}
        query = params.get("query", "")
        matches = [r for r in _ROWS if not query or r["query"] == query]
        self._send(200, matches)


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture
def fake_service(monkeypatch: pytest.MonkeyPatch) -> Any:
    _reset_fake_service_state()
    port = _free_port()
    server = HTTPServer(("127.0.0.1", port), _RotatableBearerTelemetryHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()

    monkeypatch.setenv("NX_SERVICE_HOST", "127.0.0.1")
    monkeypatch.setenv("NX_SERVICE_PORT", str(port))
    monkeypatch.setenv("NX_SERVICE_TOKEN", _VALID_BEARER)
    monkeypatch.delenv("NX_SERVICE_URL", raising=False)

    yield port

    server.shutdown()
    server.server_close()


class TestHttpTelemetryStoreSelfHeal:
    """Store-level (not mixin-level) proof that adoption actually wired
    every call site through the self-healing transport."""

    def test_log_relevance_selfheals_on_rotated_bearer(
        self, fake_service, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Write-path: log_relevance() must self-heal after a sibling
        process rotates the bearer token, not surface a 401 to the
        caller."""
        store = HttpTelemetryStore()

        rid = store.log_relevance("selfheal query 1", "chunk-1", "store_put")
        assert isinstance(rid, int)
        assert _REQUEST_COUNT["POST /v1/telemetry/relevance/log"] == 1

        global _VALID_BEARER
        _VALID_BEARER = "rotated-bearer-write-path"
        monkeypatch.setenv("NX_SERVICE_TOKEN", _VALID_BEARER)

        rid2 = store.log_relevance("selfheal query 2", "chunk-2", "store_put")
        assert isinstance(rid2, int)
        assert rid2 != rid
        # 1 (baseline) + 1 (401 on stale header) + 1 (retry, succeeds) == 3.
        assert _REQUEST_COUNT["POST /v1/telemetry/relevance/log"] == 3, (
            "expected exactly one failed attempt followed by one successful "
            "retry on the WRITE path — not a retry loop"
        )
        store.close()

    def test_get_relevance_log_selfheals_on_rotated_bearer(
        self, fake_service, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Read-path: get_relevance_log() must self-heal too — this is
        the EXACT pre-adoption bypass site (inline
        ``self._client.get("/v1/telemetry/relevance/query", ...)``), the
        class of gap this mixin adoption exists to close."""
        store = HttpTelemetryStore()
        store.log_relevance("findme-query", "chunk-r1", "store_put")
        assert "GET /v1/telemetry/relevance/query" not in _REQUEST_COUNT

        baseline = store.get_relevance_log(query="findme-query")
        assert len(baseline) == 1
        assert baseline[0]["chunk_id"] == "chunk-r1"
        assert _REQUEST_COUNT["GET /v1/telemetry/relevance/query"] == 1

        global _VALID_BEARER
        _VALID_BEARER = "rotated-bearer-read-path"
        monkeypatch.setenv("NX_SERVICE_TOKEN", _VALID_BEARER)

        result = store.get_relevance_log(query="findme-query")
        assert len(result) == 1
        assert result[0]["chunk_id"] == "chunk-r1"
        # 1 (baseline) + 1 (401 on stale header) + 1 (retry, succeeds) == 3.
        assert _REQUEST_COUNT["GET /v1/telemetry/relevance/query"] == 3, (
            "expected exactly one failed attempt followed by one successful "
            "retry on the READ path — not a retry loop"
        )
        store.close()


class TestNoBypassOfMixinTransport:
    """Scripted regression guard (mirrors
    tests/db/test_http_memory_store_selfheal.py): a future edit must not
    reintroduce a direct ``self._client.<verb>(...)`` call that bypasses
    the mixin's self-healing ``_get``/``_post``/``_delete`` wrappers —
    exactly the class of gap this adoption closed (get_relevance_log and
    query_collection_stats were both inline pre-adoption)."""

    _BYPASS_PATTERN = re.compile(r"self\._client\.(get|post|put|delete|patch|request)\(")

    #: Chartered exemptions: methods whose ENTIRE PURPOSE is bypassing the
    #: self-healing transport, each with a review-verified latency rationale.
    #: nexus-ov13k: query_tier_writes_once is the session-end summary's
    #: single-attempt read — the mixin's retry ladder (gateway backoff +
    #: evidence-gated lease-wait) has a 20-50s worst case that must never
    #: run at session close. Every OTHER call site must use _get/_post.
    _CHARTERED_BYPASS_METHODS = ("query_tier_writes_once",)

    def test_http_telemetry_store_has_zero_inline_client_call_sites(self) -> None:
        source_path = (
            Path(__file__).resolve().parent.parent.parent
            / "src" / "nexus" / "db" / "t2" / "http_telemetry_store.py"
        )
        source = source_path.read_text()
        # Blank out chartered methods' bodies before scanning: an inline
        # call site is allowed ONLY inside an explicitly chartered method.
        scan = source
        for name in self._CHARTERED_BYPASS_METHODS:
            start = scan.find(f"def {name}(")
            assert start != -1, f"chartered method {name} vanished — update the charter"
            nxt = scan.find("\n    def ", start + 1)
            scan = scan[:start] + scan[nxt if nxt != -1 else len(scan):]
        matches = self._BYPASS_PATTERN.findall(scan)
        assert matches == [], (
            f"found {len(matches)} inline self._client.<verb>(...) call "
            f"site(s) in http_telemetry_store.py outside the chartered "
            f"bypass methods {self._CHARTERED_BYPASS_METHODS}: {matches}"
        )
