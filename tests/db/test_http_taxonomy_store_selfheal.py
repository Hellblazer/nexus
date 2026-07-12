# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""Store-level self-heal + bypass-guard tests for HttpTaxonomyStore
(nexus-f2qvx.1 — mixin-adoption sweep, batch A).

``tests/db/test_refreshable_client.py`` (nexus-bikit.2/.3) already proves the
MIXIN self-heals over a synthetic ``/v1/echo`` endpoint, and
``tests/db/test_http_memory_store_selfheal.py`` (nexus-bikit.4) proves it for
the canonical first adopter. This file proves the same behaviour end-to-end
through a REAL ``HttpTaxonomyStore`` instance against REAL taxonomy-service
paths (``/v1/taxonomy/meta/record`` write, ``/v1/taxonomy/meta/last_count``
read) — the thing an adopter can get wrong even with a correct mixin, e.g. by
leaving one inline ``self._client.*`` call site un-migrated (exactly what the
bypass-guard test below catches mechanically).

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

from nexus.db.t2.http_taxonomy_store import HttpTaxonomyStore

# ── Rotatable-bearer fake taxonomy service ──────────────────────────────────

_INITIAL_BEARER = "selfheal-taxonomy-initial-bearer"

_VALID_BEARER: str = _INITIAL_BEARER
#: "METHOD /path" -> inbound request count, INCLUDING 401s — lets tests
#: assert "retried exactly once" by counting round trips.
_REQUEST_COUNT: dict[str, int] = {}
#: In-memory ``{collection: last_discover_doc_count}`` store, reset per test.
_LAST_COUNTS: dict[str, int] = {}


def _reset_fake_service_state() -> None:
    global _VALID_BEARER
    _VALID_BEARER = _INITIAL_BEARER
    _REQUEST_COUNT.clear()
    _LAST_COUNTS.clear()


class _RotatableBearerTaxonomyHandler(BaseHTTPRequestHandler):
    """Minimal ``/v1/taxonomy/meta/record`` + ``/v1/taxonomy/meta/last_count``
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
        if path != "/v1/taxonomy/meta/record":
            self._send(404, {"error": "not found"})
            return
        if not self._check_bearer():
            return
        length = int(self.headers.get("Content-Length", "0"))
        body = json.loads(self.rfile.read(length)) if length else {}
        _LAST_COUNTS[body["collection"]] = int(body["doc_count"])
        self._send(200, {})

    def do_GET(self):  # noqa: N802
        parsed = urlparse(self.path)
        path = parsed.path
        self._record("GET", path)
        if path != "/v1/taxonomy/meta/last_count":
            self._send(404, {"error": "not found"})
            return
        if not self._check_bearer():
            return
        params = {k: v[0] for k, v in parse_qs(parsed.query).items()}
        collection = params.get("collection", "")
        if collection not in _LAST_COUNTS:
            self._send(404, {"error": "not found"})
            return
        self._send(200, {"count": _LAST_COUNTS[collection]})


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture
def fake_service(monkeypatch: pytest.MonkeyPatch) -> Any:
    _reset_fake_service_state()
    port = _free_port()
    server = HTTPServer(("127.0.0.1", port), _RotatableBearerTaxonomyHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()

    monkeypatch.setenv("NX_SERVICE_HOST", "127.0.0.1")
    monkeypatch.setenv("NX_SERVICE_PORT", str(port))
    monkeypatch.setenv("NX_SERVICE_TOKEN", _VALID_BEARER)
    monkeypatch.delenv("NX_SERVICE_URL", raising=False)

    yield port

    server.shutdown()
    server.server_close()


class TestHttpTaxonomyStoreSelfHeal:
    """Store-level (not mixin-level) proof that adoption actually wired
    every call site through the self-healing transport."""

    def test_record_discover_count_selfheals_on_rotated_bearer(
        self, fake_service, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Write-path: record_discover_count() must self-heal after a
        sibling process rotates the bearer token, not surface a 401 to
        the caller."""
        store = HttpTaxonomyStore()

        store.record_discover_count("selfheal-coll", 10)
        assert _REQUEST_COUNT["POST /v1/taxonomy/meta/record"] == 1
        assert _LAST_COUNTS["selfheal-coll"] == 10

        global _VALID_BEARER
        _VALID_BEARER = "rotated-bearer-write-path"
        monkeypatch.setenv("NX_SERVICE_TOKEN", _VALID_BEARER)

        store.record_discover_count("selfheal-coll", 20)
        # 1 (baseline) + 1 (401 on stale header) + 1 (retry, succeeds) == 3.
        assert _REQUEST_COUNT["POST /v1/taxonomy/meta/record"] == 3, (
            "expected exactly one failed attempt followed by one successful "
            "retry on the WRITE path — not a retry loop"
        )
        assert _LAST_COUNTS["selfheal-coll"] == 20
        store.close()

    def test_needs_rebalance_selfheals_on_rotated_bearer(
        self, fake_service, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Read-path: needs_rebalance() must self-heal too — a
        record_discover_count()-only fix would leave every OTHER read
        (get_topics, get_topic_by_id, ...) still vulnerable to the exact
        bug this mixin exists to close."""
        store = HttpTaxonomyStore()
        store.record_discover_count("selfheal-coll-r", 100)
        assert "GET /v1/taxonomy/meta/last_count" not in _REQUEST_COUNT

        baseline = store.needs_rebalance("selfheal-coll-r", 100)
        assert baseline is False  # same count, no growth
        assert _REQUEST_COUNT["GET /v1/taxonomy/meta/last_count"] == 1

        global _VALID_BEARER
        _VALID_BEARER = "rotated-bearer-read-path"
        monkeypatch.setenv("NX_SERVICE_TOKEN", _VALID_BEARER)

        result = store.needs_rebalance("selfheal-coll-r", 100)
        assert result is False
        # 1 (baseline) + 1 (401 on stale header) + 1 (retry, succeeds) == 3.
        assert _REQUEST_COUNT["GET /v1/taxonomy/meta/last_count"] == 3, (
            "expected exactly one failed attempt followed by one successful "
            "retry on the READ path — not a retry loop"
        )
        store.close()

    def test_needs_rebalance_no_prior_state_still_returns_true(
        self, fake_service
    ) -> None:
        """404-as-True contract must survive adoption: a genuinely
        never-discovered collection is NOT retryable and NOT an exception
        to needs_rebalance()'s caller, even though the mixin's _get raises
        httpx.HTTPStatusError internally for ANY non-2xx."""
        store = HttpTaxonomyStore()
        assert store.needs_rebalance("never-seen-collection", 5) is True
        store.close()


class TestNoBypassOfMixinTransport:
    """Scripted regression guard (mirrors
    tests/db/test_http_memory_store_selfheal.py): a future edit must not
    reintroduce a direct ``self._client.<verb>(...)`` call that bypasses
    the mixin's self-healing ``_get``/``_post``/``_delete`` wrappers."""

    _BYPASS_PATTERN = re.compile(r"self\._client\.(get|post|put|delete|patch|request)\(")

    def test_http_taxonomy_store_has_zero_inline_client_call_sites(self) -> None:
        source_path = (
            Path(__file__).resolve().parent.parent.parent
            / "src" / "nexus" / "db" / "t2" / "http_taxonomy_store.py"
        )
        source = source_path.read_text()
        matches = self._BYPASS_PATTERN.findall(source)
        assert matches == [], (
            f"found {len(matches)} inline self._client.<verb>(...) call "
            f"site(s) in http_taxonomy_store.py that bypass "
            f"RefreshableHttpStoreMixin's self-healing transport: {matches}"
        )
