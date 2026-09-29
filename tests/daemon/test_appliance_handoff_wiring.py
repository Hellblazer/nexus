# SPDX-License-Identifier: AGPL-3.0-or-later
"""nexus-ijue9.29 review round: the supervisor actually reaches the projection.

The engine-backed tests call ``_project_appliance_handoff`` directly, so they
cannot see the call from ``_publish`` disappear. These pin the wiring (publish
and every healthy heartbeat reach the projector) and, in the default test tier,
that the credential is requested as ``mint-locked``: a port-0 HTTP server
records the issue request the real HttpTokenStore sends.
"""
from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import Any

import pytest

from nexus.daemon.appliance_handoff import HANDOFF_FILE_ENV
from tests.daemon.test_storage_service_daemon import _FakeClock, _FakeProc, _make_supervisor


class _Recorder:
    def __init__(self) -> None:
        self.ports: list[int] = []

    def project(self, port: int) -> bool:
        self.ports.append(port)
        return True


def _armed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv(HANDOFF_FILE_ENV, str(tmp_path / "endpoint.json"))
    sup = _make_supervisor(tmp_path, _FakeClock(), supervised=True)
    rec = _Recorder()
    monkeypatch.setattr(sup, "_build_appliance_projector", lambda _t, _p: rec)
    return sup, rec


def test_publish_projects_the_handoff(tmp_path, monkeypatch) -> None:
    sup, rec = _armed(tmp_path, monkeypatch)
    sup._proc = _FakeProc(pid=46001)
    sup._publish(29517)
    assert rec.ports == [29517]


def test_publish_without_the_env_projects_nothing(tmp_path, monkeypatch) -> None:
    monkeypatch.delenv(HANDOFF_FILE_ENV, raising=False)
    sup = _make_supervisor(tmp_path, _FakeClock(), supervised=True)
    built: list[Any] = []
    monkeypatch.setattr(sup, "_build_appliance_projector", lambda *a: built.append(a))
    sup._proc = _FakeProc(pid=46001)
    sup._publish(29517)
    assert built == []


@pytest.mark.parametrize("verdict,projected", [((True, True), True), ((True, False), False), ((False, True), False)])
def test_only_a_healthy_heartbeat_reprojects(tmp_path, monkeypatch, verdict, projected) -> None:
    sup, rec = _armed(tmp_path, monkeypatch)
    sup._proc = _FakeProc(pid=46001)
    sup._service_port = 29517
    monkeypatch.setattr(sup, "_heartbeat_once_untimed", lambda _phases: verdict)
    assert sup.heartbeat_once() == verdict
    assert rec.ports == ([29517] if projected else [])


def test_a_projection_failure_never_escapes_the_supervisor(tmp_path, monkeypatch) -> None:
    sup, rec = _armed(tmp_path, monkeypatch)

    def boom(_port: int) -> bool:
        raise OSError(13, "Permission denied")

    rec.project = boom  # type: ignore[method-assign]
    sup._proc = _FakeProc(pid=46001)
    sup._publish(29517)  # must not raise


class _IssueHandler(BaseHTTPRequestHandler):
    bodies: list[dict] = []

    def do_POST(self) -> None:  # noqa: N802 — http.server's name
        length = int(self.headers.get("Content-Length", "0"))
        body = json.loads(self.rfile.read(length) or b"{}")
        type(self).bodies.append({"path": self.path, "body": body, "auth": self.headers.get("Authorization")})
        payload = json.dumps({
            "tenant": body.get("tenant"), "token": "mint-from-fake", "token_hash": "h" * 64,
            "scope": body.get("scope"),
        }).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def log_message(self, *_a: Any) -> None:
        pass


def test_the_credential_is_requested_as_mint_locked_with_the_root_bearer(tmp_path, monkeypatch) -> None:
    _IssueHandler.bodies = []
    server = HTTPServer(("127.0.0.1", 0), _IssueHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        monkeypatch.setenv(HANDOFF_FILE_ENV, str(tmp_path / "endpoint.json"))
        monkeypatch.delenv("NX_SERVICE_TOKEN", raising=False)   # the root then comes from pg_credentials
        sup = _make_supervisor(tmp_path, _FakeClock(), supervised=True)
        projector = sup._build_appliance_projector(tmp_path / "endpoint.json", server.server_port)
        projector._issue()
    finally:
        server.shutdown()
        server.server_close()
    [req] = [b for b in _IssueHandler.bodies if b["path"] == "/v1/service-tokens/issue"]
    assert req["body"]["scope"] == "mint-locked"
    assert req["body"]["tenant"] == "default"
    assert req["body"]["label"] == "appliance-windows-client"
    assert req["auth"] == "Bearer root-token-from-creds-deadbeef", "issued with the root bearer"
