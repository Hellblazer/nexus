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
import time
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import Any

import pytest
import structlog

import nexus.daemon.storage_service_daemon as ssd
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


class _Clock:
    def __init__(self) -> None:
        self.t = 100.0

    def __call__(self) -> float:
        return self.t


def test_a_failure_backs_off_heartbeat_retries_but_not_publish(tmp_path, monkeypatch) -> None:
    sup, rec = _armed(tmp_path, monkeypatch)
    clock = _Clock()
    sup._monotonic = clock
    calls = {"n": 0}

    def failing(_port: int) -> bool:
        calls["n"] += 1
        raise RuntimeError("engine 503")

    rec.project = failing  # type: ignore[method-assign]
    sup._project_appliance_handoff(29517)
    assert calls["n"] == 1
    clock.t += ssd._APPLIANCE_RETRY_BACKOFF_S - 1
    sup._project_appliance_handoff(29517)
    assert calls["n"] == 1, "no retry inside the backoff"
    sup._project_appliance_handoff(29517, force=True)
    assert calls["n"] == 2, "publish always tries"
    clock.t += ssd._APPLIANCE_RETRY_BACKOFF_S
    sup._project_appliance_handoff(29517)
    assert calls["n"] == 3


def test_a_projector_that_cannot_be_built_never_escapes(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv(HANDOFF_FILE_ENV, str(tmp_path / "endpoint.json"))
    sup = _make_supervisor(tmp_path, _FakeClock(), supervised=True)

    def broken(_t: Path, _p: int) -> Any:
        raise ImportError("appliance module missing")

    monkeypatch.setattr(sup, "_build_appliance_projector", broken)
    sup._proc = _FakeProc(pid=46001)
    sup._service_port = 29517
    monkeypatch.setattr(sup, "_heartbeat_once_untimed", lambda _phases: (True, True))
    assert sup.heartbeat_once() == (True, True)


class _HangingHandler(BaseHTTPRequestHandler):
    def do_POST(self) -> None:  # noqa: N802
        time.sleep(3)

    def log_message(self, *_a: Any) -> None:
        pass


def test_a_hanging_admin_call_is_bounded_by_the_short_timeout(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(ssd, "_APPLIANCE_ADMIN_TIMEOUT_S", 0.3)
    server = HTTPServer(("127.0.0.1", 0), _HangingHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        monkeypatch.setenv(HANDOFF_FILE_ENV, str(tmp_path / "endpoint.json"))
        sup = _make_supervisor(tmp_path, _FakeClock(), supervised=True)
        started = time.monotonic()
        sup._project_appliance_handoff(server.server_port, force=True)
        elapsed = time.monotonic() - started
    finally:
        server.shutdown()
        server.server_close()
    assert elapsed < 2.0, f"a stalled issue call held the heartbeat thread for {elapsed:.1f}s"
    assert not (tmp_path / "endpoint.json").exists()


def test_a_handoff_without_a_fixed_port_warns(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv(HANDOFF_FILE_ENV, str(tmp_path / "endpoint.json"))
    monkeypatch.delenv("NX_SERVICE_FIXED_PORT", raising=False)
    sup = _make_supervisor(tmp_path, _FakeClock(), supervised=True)
    with structlog.testing.capture_logs() as logs:
        sup._build_appliance_projector(tmp_path / "endpoint.json", 29517)
    assert any(e.get("event") == "appliance_handoff_without_fixed_port" for e in logs)
