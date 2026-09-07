"""nexus-h5olw — anonymous daily install ping: opt-out, throttle, payload, wire."""

from __future__ import annotations

import json
import socket
import threading
import uuid
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import pytest
from click.testing import CliRunner

from nexus import install_ping as ip
from nexus.cli import main


@pytest.fixture
def cfg(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("NEXUS_CONFIG_DIR", str(tmp_path))
    monkeypatch.delenv("NX_NO_TELEMETRY", raising=False)
    monkeypatch.delenv("NX_SERVICE_URL", raising=False)
    monkeypatch.chdir(tmp_path)  # no per-repo .nexus.yml leaks in
    return tmp_path


class _Recorder(BaseHTTPRequestHandler):
    bodies: list[dict] = []
    status = 202

    def do_POST(self) -> None:  # noqa: N802
        n = int(self.headers.get("Content-Length", "0"))
        _Recorder.bodies.append(json.loads(self.rfile.read(n)))
        self.send_response(_Recorder.status)
        self.end_headers()
        self.wfile.write(b'{"ok":true}')

    def log_message(self, *_: object) -> None:
        pass


@pytest.fixture
def receiver(monkeypatch: pytest.MonkeyPatch):
    _Recorder.bodies = []
    _Recorder.status = 202
    srv = HTTPServer(("127.0.0.1", 0), _Recorder)
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    monkeypatch.setenv("NX_INSTALL_PING_URL", f"http://127.0.0.1:{srv.server_port}/v1/install-ping")
    yield _Recorder
    srv.shutdown()


# ── opt-out ────────────────────────────────────────────────────────────────

def test_default_is_on(cfg: Path) -> None:
    assert ip.telemetry_enabled() is True
    assert ip.telemetry_status() == {"enabled": True, "source": "default"}


@pytest.mark.parametrize("value", ["1", "true", "yes"])
def test_env_opt_out(cfg: Path, monkeypatch: pytest.MonkeyPatch, value: str) -> None:
    monkeypatch.setenv("NX_NO_TELEMETRY", value)
    assert ip.telemetry_enabled() is False
    assert ip.telemetry_status()["source"].startswith("env NX_NO_TELEMETRY=")


def test_env_zero_means_unset(cfg: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("NX_NO_TELEMETRY", "0")
    assert ip.telemetry_enabled() is True


@pytest.mark.parametrize("raw", ["telemetry:\n  enabled: false\n", "telemetry:\n  enabled: 'off'\n"])
def test_config_opt_out(cfg: Path, raw: str) -> None:
    (cfg / "config.yml").write_text(raw)
    assert ip.telemetry_enabled() is False
    assert ip.telemetry_status() == {"enabled": False, "source": "config telemetry.enabled"}


def test_ping_in_background_returns_none_when_opted_out(cfg: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("NX_NO_TELEMETRY", "1")
    called: list[bool] = []
    assert ip.ping_in_background(lambda: called.append(True)) is None
    assert called == []


# ── install id and throttle ────────────────────────────────────────────────

def test_install_id_is_uuid_and_stable(cfg: Path) -> None:
    first = ip.install_id()
    uuid.UUID(first)
    assert ip.install_id() == first
    assert (cfg / "install_id").read_text().strip() == first


def test_corrupt_install_id_is_reminted(cfg: Path) -> None:
    (cfg / "install_id").write_text("garbage\n")
    fresh = ip.install_id()
    uuid.UUID(fresh)
    assert (cfg / "install_id").read_text().strip() == fresh


def test_due_never_pinged_then_throttled_24h(cfg: Path) -> None:
    now = 1_800_000_000.0
    assert ip.due(now) is True
    ip._mark_pinged(now, None)
    assert ip.due(now + ip.PING_INTERVAL_S - 1) is False
    assert ip.due(now + ip.PING_INTERVAL_S) is True


# ── payload ────────────────────────────────────────────────────────────────

def test_payload_has_exactly_six_fields(cfg: Path) -> None:
    p = ip.build_payload()
    assert set(p) == {"install_id", "client_version", "mode", "os", "arch", "python"}
    uuid.UUID(p["install_id"])
    assert p["mode"] == "local"
    assert p["python"].count(".") == 1
    assert all(isinstance(v, str) and v for v in p.values())


def test_mode_is_cloud_when_service_url_configured(cfg: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("NX_SERVICE_URL", "https://example.invalid")
    assert ip.build_payload()["mode"] == "cloud"


# ── wire ───────────────────────────────────────────────────────────────────

def test_ping_if_due_sends_marks_and_throttles(cfg: Path, receiver: type[_Recorder]) -> None:
    now = 1_800_000_000.0
    assert ip.ping_if_due(now) is True
    assert len(receiver.bodies) == 1
    assert receiver.bodies[0]["install_id"] == ip.install_id()
    assert ip.last_ping_at() == pytest.approx(now)
    assert ip.ping_if_due(now + 60) is False
    assert len(receiver.bodies) == 1


def test_non_2xx_is_false_and_not_marked(cfg: Path, receiver: type[_Recorder]) -> None:
    receiver.status = 429
    assert ip.ping_if_due(1_800_000_000.0) is False
    assert ip.last_ping_at() is None


def test_unreachable_target_is_false_never_raises(cfg: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()  # nothing listens here now
    monkeypatch.setenv("NX_INSTALL_PING_URL", f"http://127.0.0.1:{port}/v1/install-ping")
    assert ip.ping_if_due(1_800_000_000.0) is False
    assert ip.last_ping_at() is None


def test_ping_in_background_runs_target_on_daemon_thread(cfg: Path) -> None:
    done = threading.Event()
    t = ip.ping_in_background(done.set)
    assert t is not None and t.daemon
    assert done.wait(5)


# ── CLI ────────────────────────────────────────────────────────────────────

def test_nx_telemetry_off_status_on(cfg: Path) -> None:
    r = CliRunner()
    out = r.invoke(main, ["telemetry", "status"])
    assert out.exit_code == 0, out.output
    assert "install ping: on (default)" in out.output
    assert "last ping:    never" in out.output

    assert r.invoke(main, ["telemetry", "off"]).exit_code == 0
    assert "enabled: false" in (cfg / "config.yml").read_text()
    out = r.invoke(main, ["telemetry", "status"])
    assert "install ping: off (config telemetry.enabled)" in out.output

    assert r.invoke(main, ["telemetry", "on"]).exit_code == 0
    out = r.invoke(main, ["telemetry", "status"])
    assert "install ping: on (default)" in out.output
