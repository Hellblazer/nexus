# SPDX-License-Identifier: AGPL-3.0-or-later
import json
import socket
from pathlib import Path
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

from nexus.console.app import create_app
from nexus.console.watchers import SessionInfo, scan_sessions_sync


@pytest.fixture()
def client():
    return TestClient(create_app())


def test_health_returns_200(client):
    resp = client.get("/health")
    assert resp.status_code == 200
    assert "text/html" in resp.headers["content-type"]


def test_health_has_health_cards(client):
    resp = client.get("/health")
    assert "Health" in resp.text


def test_health_refresh_endpoint(client):
    resp = client.get("/health/refresh")
    assert resp.status_code == 200


# ── Session scanner tests ────────────────────────────────────────────────────

def test_scan_sessions_empty_dir(tmp_path):
    results = scan_sessions_sync(tmp_path)
    assert results == []


def test_scan_sessions_no_dir():
    results = scan_sessions_sync(Path("/nonexistent/path"))
    assert results == []


def test_scan_sessions_live_session(tmp_path):
    """Create a session file with our own PID — should be detected as alive."""
    import os
    session = {
        "session_id": "test-session",
        "server_host": "127.0.0.1",
        "server_port": 0,  # won't connect but PID is alive
        "server_pid": os.getpid(),
        "created_at": 1700000000.0,
    }
    (tmp_path / "test.session").write_text(json.dumps(session))
    results = scan_sessions_sync(tmp_path)
    assert len(results) == 1
    assert results[0].session_id == "test-session"
    assert results[0].pid_alive is True


def test_scan_sessions_dead_pid(tmp_path):
    """Session with a dead PID."""
    import subprocess
    proc = subprocess.Popen(["true"])
    proc.wait()

    session = {
        "session_id": "dead-session",
        "server_host": "127.0.0.1",
        "server_port": 12345,
        "server_pid": proc.pid,
        "created_at": 1700000000.0,
    }
    (tmp_path / "dead.session").write_text(json.dumps(session))
    results = scan_sessions_sync(tmp_path)
    assert len(results) == 1
    assert results[0].pid_alive is False


def test_session_info_fields():
    info = SessionInfo(
        session_id="s1",
        host="127.0.0.1",
        port=8080,
        pid=1234,
        pid_alive=True,
        tcp_reachable=False,
        created_at="2026-01-01T00:00:00",
    )
    assert info.session_id == "s1"
    assert info.pid_alive is True
    assert info.tcp_reachable is False
