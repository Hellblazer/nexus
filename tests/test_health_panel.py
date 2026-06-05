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


def _write_t1_lease(config_dir: Path, session_id: str, host: str, port: int,
                    *, heartbeat_epoch: float, ttl: float = 30.0,
                    server_pid: int = 4242) -> None:
    """Write a RDR-149 T1 lease record at ``t1_addr.<session_id>``."""
    from nexus.daemon.service_registry import LeaseRecord

    record = LeaseRecord(
        scope_key=session_id,
        generation=1,
        owner_token="tok",
        heartbeat_epoch=heartbeat_epoch,
        ttl=ttl,
        endpoint={"host": host, "port": port, "server_pid": server_pid},
        version="1.0.0",
        payload={"session_id": session_id, "server_pid": server_pid},
    )
    (config_dir / f"t1_addr.{session_id}").write_text(record.to_json())


def test_scan_sessions_live_session(tmp_path):
    """A fresh lease (recent heartbeat) is detected as alive (RDR-149 P4)."""
    import time
    _write_t1_lease(tmp_path, "sess-A", "127.0.0.1", 0, heartbeat_epoch=time.time())
    results = scan_sessions_sync(tmp_path)
    assert len(results) == 1
    assert results[0].session_id == "sess-A"
    assert results[0].pid_alive is True


def test_scan_sessions_dead_pid(tmp_path):
    """An expired lease (heartbeat older than TTL) is not alive (RDR-149 P4)."""
    import time
    _write_t1_lease(
        tmp_path, "sess-stale", "127.0.0.1", 12345,
        heartbeat_epoch=time.time() - 1000.0, ttl=30.0,
    )
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
