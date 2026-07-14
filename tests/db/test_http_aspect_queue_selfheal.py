# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""Store-level self-heal + bypass-guard + timeout-passthrough tests for
HttpAspectQueue (nexus-f2qvx.2 — mixin-adoption sweep, batch B).

``tests/db/test_refreshable_client.py`` (nexus-bikit.2/.3) already proves the
MIXIN self-heals over a synthetic ``/v1/echo`` endpoint, and
``tests/db/test_http_memory_store_selfheal.py`` (nexus-bikit.4) proves it for
the canonical first adopter. This file proves the same behaviour end-to-end
through a REAL ``HttpAspectQueue`` instance against REAL
aspect-queue-service paths (``/v1/aspects/queue/enqueue`` write,
``/v1/aspects/queue/pending_count`` read) — the thing an adopter can get
wrong even with a correct mixin, e.g. by leaving one inline
``self._client.*`` call site un-migrated (exactly what the bypass-guard test
below catches mechanically).

``HttpAspectQueue`` is the one store in this sweep with its own public
``timeout`` constructor kwarg (pre-dating the mixin) — the mixin's
``__init__`` grew a matching optional ``timeout`` kwarg (additive; see
``src/nexus/db/t2/_refreshable_client.py``) so this store could thread its
own kwarg through unchanged. ``TestTimeoutPassthrough`` proves that wiring
actually reaches the underlying ``httpx.Client``.

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

import httpx
import pytest

from nexus.db.t2.http_aspect_queue import HttpAspectQueue

# ── Rotatable-bearer fake aspect-queue service ──────────────────────────────

_INITIAL_BEARER = "selfheal-aspect-queue-initial-bearer"

_VALID_BEARER: str = _INITIAL_BEARER
#: "METHOD /path" -> inbound request count, INCLUDING 401s — lets tests
#: assert "retried exactly once" by counting round trips.
_REQUEST_COUNT: dict[str, int] = {}
#: Number of successfully-enqueued rows, reset per test.
_PENDING_COUNT = [0]


def _reset_fake_service_state() -> None:
    global _VALID_BEARER
    _VALID_BEARER = _INITIAL_BEARER
    _REQUEST_COUNT.clear()
    _PENDING_COUNT[0] = 0


class _RotatableBearerAspectQueueHandler(BaseHTTPRequestHandler):
    """Minimal ``/v1/aspects/queue/enqueue`` + ``/v1/aspects/queue/pending_count``
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
        if path != "/v1/aspects/queue/enqueue":
            self._send(404, {"error": "not found"})
            return
        if not self._check_bearer():
            return
        length = int(self.headers.get("Content-Length", "0"))
        json.loads(self.rfile.read(length)) if length else {}
        _PENDING_COUNT[0] += 1
        self._send(200, {})

    def do_GET(self):  # noqa: N802
        parsed = urlparse(self.path)
        path = parsed.path
        self._record("GET", path)
        if path != "/v1/aspects/queue/pending_count":
            self._send(404, {"error": "not found"})
            return
        if not self._check_bearer():
            return
        self._send(200, {"count": _PENDING_COUNT[0]})


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture
def fake_service(monkeypatch: pytest.MonkeyPatch) -> Any:
    _reset_fake_service_state()
    port = _free_port()
    server = HTTPServer(("127.0.0.1", port), _RotatableBearerAspectQueueHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()

    monkeypatch.setenv("NX_SERVICE_HOST", "127.0.0.1")
    monkeypatch.setenv("NX_SERVICE_PORT", str(port))
    monkeypatch.setenv("NX_SERVICE_TOKEN", _VALID_BEARER)
    monkeypatch.delenv("NX_SERVICE_URL", raising=False)

    yield port

    server.shutdown()
    server.server_close()


class TestHttpAspectQueueSelfHeal:
    """Store-level (not mixin-level) proof that adoption actually wired
    every call site through the self-healing transport."""

    def test_enqueue_selfheals_on_rotated_bearer(
        self, fake_service, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Write-path: enqueue() must self-heal after a sibling process
        rotates the bearer token, not surface a 401 to the caller."""
        store = HttpAspectQueue()

        store.enqueue("selfheal-coll", "doc1.pdf")
        assert _REQUEST_COUNT["POST /v1/aspects/queue/enqueue"] == 1

        global _VALID_BEARER
        _VALID_BEARER = "rotated-bearer-write-path"
        monkeypatch.setenv("NX_SERVICE_TOKEN", _VALID_BEARER)

        store.enqueue("selfheal-coll", "doc2.pdf")
        # 1 (baseline) + 1 (401 on stale header) + 1 (retry, succeeds) == 3.
        assert _REQUEST_COUNT["POST /v1/aspects/queue/enqueue"] == 3, (
            "expected exactly one failed attempt followed by one successful "
            "retry on the WRITE path — not a retry loop"
        )
        assert _PENDING_COUNT[0] == 2
        store.close()

    def test_pending_count_selfheals_on_rotated_bearer(
        self, fake_service, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Read-path: pending_count() must self-heal too — an enqueue()-only
        fix would leave every other read (is_drained, list_pending, ...)
        still vulnerable to the exact bug this mixin exists to close."""
        store = HttpAspectQueue()
        store.enqueue("selfheal-coll-r", "doc.pdf")
        assert "GET /v1/aspects/queue/pending_count" not in _REQUEST_COUNT

        baseline = store.pending_count()
        assert baseline == 1
        assert _REQUEST_COUNT["GET /v1/aspects/queue/pending_count"] == 1

        global _VALID_BEARER
        _VALID_BEARER = "rotated-bearer-read-path"
        monkeypatch.setenv("NX_SERVICE_TOKEN", _VALID_BEARER)

        result = store.pending_count()
        assert result == 1
        # 1 (baseline) + 1 (401 on stale header) + 1 (retry, succeeds) == 3.
        assert _REQUEST_COUNT["GET /v1/aspects/queue/pending_count"] == 3, (
            "expected exactly one failed attempt followed by one successful "
            "retry on the READ path — not a retry loop"
        )
        store.close()


class TestRenameLockAcceptedButIgnored:
    """rename_lock stays a pure constructor-parity no-op post-adoption
    (verbatim behavior preserved from the pre-mixin constructor)."""

    def test_rename_lock_accepted_but_ignored(self) -> None:
        lock = threading.RLock()
        store = HttpAspectQueue(base_url="http://test", _token="tok", rename_lock=lock)
        assert store.rename_lock is lock
        store.close()

    def test_rename_lock_defaults_to_a_new_rlock(self) -> None:
        store = HttpAspectQueue(base_url="http://test", _token="tok")
        assert isinstance(store.rename_lock, type(threading.RLock()))
        store.close()


class TestTimeoutPassthrough:
    """HttpAspectQueue's own public ``timeout`` kwarg must actually reach
    the underlying httpx.Client — this is the additive mixin change this
    batch required (RefreshableHttpStoreMixin.__init__ grew a matching
    optional ``timeout`` kwarg, defaulting to the same 30.0s every other
    adopter already got, so this store could thread ITS kwarg through
    instead of it being silently dropped)."""

    def test_default_timeout_is_30_seconds(self) -> None:
        store = HttpAspectQueue(base_url="http://test", _token="tok")
        assert store._client.timeout == httpx.Timeout(30.0)
        store.close()

    def test_explicit_timeout_reaches_the_underlying_client(self) -> None:
        store = HttpAspectQueue(base_url="http://test", _token="tok", timeout=5.0)
        assert store._client.timeout == httpx.Timeout(5.0)
        store.close()

    def test_explicit_timeout_differs_from_default(self) -> None:
        """Belt-and-suspenders: prove the two constructions are not
        coincidentally equal (i.e. this isn't testing a no-op)."""
        default_store = HttpAspectQueue(base_url="http://test", _token="tok")
        custom_store = HttpAspectQueue(base_url="http://test", _token="tok", timeout=90.0)
        assert default_store._client.timeout != custom_store._client.timeout
        default_store.close()
        custom_store.close()


class TestNoBypassOfMixinTransport:
    """Scripted regression guard (mirrors
    tests/db/test_http_memory_store_selfheal.py): a future edit must not
    reintroduce a direct ``self._client.<verb>(...)`` call that bypasses
    the mixin's self-healing ``_get``/``_post``/``_delete`` wrappers."""

    _BYPASS_PATTERN = re.compile(r"self\._client\.(get|post|put|delete|patch|request)\(")

    def test_http_aspect_queue_has_zero_inline_client_call_sites(self) -> None:
        source_path = (
            Path(__file__).resolve().parent.parent.parent
            / "src" / "nexus" / "db" / "t2" / "http_aspect_queue.py"
        )
        source = source_path.read_text()
        matches = self._BYPASS_PATTERN.findall(source)
        assert matches == [], (
            f"found {len(matches)} inline self._client.<verb>(...) call "
            f"site(s) in http_aspect_queue.py that bypass "
            f"RefreshableHttpStoreMixin's self-healing transport: {matches}"
        )
