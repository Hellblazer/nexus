# SPDX-License-Identifier: AGPL-3.0-or-later
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from nexus.console.app import create_app
from nexus.console.watchers import SessionInfo, scan_sessions_sync


@pytest.fixture()
def client():
    return TestClient(create_app())


@pytest.mark.t2_tenant
def test_health_returns_200(client):
    resp = client.get("/health")
    assert resp.status_code == 200
    assert "text/html" in resp.headers["content-type"]


@pytest.mark.t2_tenant
def test_health_has_health_cards(client):
    resp = client.get("/health")
    assert "Health" in resp.text


@pytest.mark.t2_tenant
def test_health_refresh_endpoint(client):
    resp = client.get("/health/refresh")
    assert resp.status_code == 200


# ── Session scanner tests (nexus-8zfwv: t1_session_lease.* port) ────────────
#
# Ported off the RDR-149 P4 ``t1_addr.*`` ``ServiceRegistry`` lease format:
# ``T1LeasePublisher``, the only thing that ever published it, is retired
# (deleted at ff744321). T1 is one shared nexus-service now, so a session's
# lease carries only a bearer token + expiry -- no host/port/pid to scan.

def test_scan_sessions_empty_dir(tmp_path):
    results = scan_sessions_sync(tmp_path)
    assert results == []


def test_scan_sessions_no_dir():
    results = scan_sessions_sync(Path("/nonexistent/path"))
    assert results == []


def _write_t1_session_lease(config_dir: Path, session_id: str, *, expires_at: float) -> None:
    """Write a live lease via the REAL publisher (nexus.db.t1), never a
    hand-built filename -- the test must read the SAME file shape the
    production code writes."""
    import time

    from nexus.db.t1 import publish_t1_session_lease

    ttl_seconds = expires_at - time.time()
    publish_t1_session_lease(session_id, "tok", config_dir, ttl_seconds=ttl_seconds)


def test_scan_sessions_live_session(tmp_path):
    """A fresh lease (well inside its TTL) is detected as alive."""
    import time
    _write_t1_session_lease(tmp_path, "sess-A", expires_at=time.time() + 3600.0)
    results = scan_sessions_sync(tmp_path)
    assert len(results) == 1
    assert results[0].session_id == "sess-A"
    assert results[0].fresh is True


def test_scan_sessions_dead_pid(tmp_path):
    """An expired lease (past its expires_at) is not alive."""
    import time
    _write_t1_session_lease(tmp_path, "sess-stale", expires_at=time.time() - 1000.0)
    results = scan_sessions_sync(tmp_path)
    assert len(results) == 1
    assert results[0].fresh is False


def test_session_info_fields():
    info = SessionInfo(
        session_id="s1",
        expires_at=1234567890.0,
        fresh=True,
    )
    assert info.session_id == "s1"
    assert info.expires_at == 1234567890.0
    assert info.fresh is True


# ── System card: a soft warning is drawn as a warning, not a failure ────────


def _glyph_for(html: str, label: str) -> str:
    """The status glyph in the System card's row for *label*."""
    import re

    m = re.search(
        rf"<td[^>]*>\s*<span[^>]*>(&#x[0-9A-Fa-f]+;)</span>\s*</td>\s*<td>{re.escape(label)}</td>", html,
    )
    assert m, f"no row for {label!r} in the System card"
    return m.group(1)


def test_system_card_draws_a_warn_row_as_a_warning_not_a_failure(client, monkeypatch):
    """`HealthResult(ok=False, warn=True)` is a soft warning (never fatal); the card used to pass
    only `ok`, so every warn row was drawn as a red cross (7.86.1 shakeout)."""
    from nexus.health import HealthResult

    rows = [
        HealthResult(label="row-pass", ok=True, detail="fine"),
        HealthResult(label="row-warn", ok=False, warn=True, detail="soft"),
        HealthResult(label="row-fail", ok=False, detail="hard"),
    ]
    monkeypatch.setattr("nexus.health.run_health_checks", lambda *a, **k: (rows, True))
    monkeypatch.setattr(
        "nexus.console.routes.health._collect_aspect_queue_data", lambda: {"present": False},
    )
    resp = client.get("/health/refresh")
    assert resp.status_code == 200
    assert _glyph_for(resp.text, "row-pass") == "&#x2713;"
    assert _glyph_for(resp.text, "row-warn") == "&#x26A0;"
    assert _glyph_for(resp.text, "row-fail") == "&#x2717;"
