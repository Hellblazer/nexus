# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""The T3 retry loop and the shared gateway hold, against a real loopback server (nexus-cphvg).

The loop learns the endpoint of an attempt from ``_request_once`` itself, so
these run the real ``_request_once`` against a server on port 0 and count how
often the endpoint is resolved.
"""
from __future__ import annotations

import json
import threading
import time
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import pytest

import nexus.db.http_vector_client as hv
from nexus.db import gateway_hold
from tests._module_seam import module_time


class _Server:
    def __init__(self, statuses: list[int]) -> None:
        owner = self
        self.statuses = list(statuses)
        self.requests = 0

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self) -> None:  # noqa: N802 — http.server's name
                owner.requests += 1
                status = owner.statuses.pop(0) if owner.statuses else 200
                body = json.dumps({"ok": True} if status == 200 else {"error": "edge"}).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *args: object) -> None:
                pass

        self.httpd = HTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.httpd.server_address[1]}"

    def close(self) -> None:
        self.httpd.shutdown()
        self.httpd.server_close()


@pytest.fixture
def server(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Iterator[_Server]:
    monkeypatch.setenv("NEXUS_CONFIG_DIR", str(tmp_path))
    hv.reset_connection_pool_for_tests()
    srv = _Server([503, 200])
    resolved: list[int] = []

    def resolve() -> tuple[str, str]:
        resolved.append(1)
        return srv.url, "tok"

    monkeypatch.setattr(hv, "_resolve_endpoint", resolve)
    monkeypatch.setattr(srv, "resolved", resolved, raising=False)
    module_time(monkeypatch, hv).sleep = lambda s: None
    yield srv
    srv.close()
    hv.reset_connection_pool_for_tests()


def _get() -> object:
    return hv._request("GET", "/v1/vectors/probe", tenant="default", timeout=5, body=None)


def test_a_503_then_a_success_resolves_the_endpoint_once_per_attempt(
    server: _Server, tmp_path: Path,
) -> None:
    assert _get() == {"ok": True}
    assert server.requests == 2
    assert len(server.resolved) == 2  # _request_once's, and nothing else: no hold, so the wait never asked
    # the fake sleep passed no time, so the hold the 503 recorded is still in force after the success
    assert json.loads((tmp_path / gateway_hold.HOLD_FILENAME).read_text())["url"] == server.url


def test_the_503_is_recorded_for_the_endpoint_the_attempt_reached(server: _Server, tmp_path: Path) -> None:
    server.statuses[:] = [503, 503, 503, 503]  # the ladder gives up: the hold stays on disk
    with pytest.raises(Exception, match="503"):
        _get()
    state = json.loads((tmp_path / gateway_hold.HOLD_FILENAME).read_text())
    assert state["url"] == server.url


def test_a_success_here_leaves_a_hold_recorded_for_another_endpoint(server: _Server, tmp_path: Path) -> None:
    server.statuses[:] = [200]
    (tmp_path / gateway_hold.HOLD_FILENAME).write_text(json.dumps({
        "not_before": time.time() - 1.0, "strikes": 2, "last_503": time.time(), "url": "http://127.0.0.1:1",
    }))
    assert _get() == {"ok": True}
    assert (tmp_path / gateway_hold.HOLD_FILENAME).exists()


def test_a_success_here_clears_this_endpoints_own_expired_hold(server: _Server, tmp_path: Path) -> None:
    server.statuses[:] = [200]
    (tmp_path / gateway_hold.HOLD_FILENAME).write_text(json.dumps({
        "not_before": time.time() - 1.0, "strikes": 2, "last_503": time.time(), "url": server.url,
    }))
    assert _get() == {"ok": True}
    assert not (tmp_path / gateway_hold.HOLD_FILENAME).exists()
