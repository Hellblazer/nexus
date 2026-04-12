# SPDX-License-Identifier: AGPL-3.0-or-later
import pytest
from fastapi.testclient import TestClient

from nexus.console.app import create_app
from nexus.console.config import ConsoleConfig


def test_app_factory_creates_app():
    app = create_app()
    assert app is not None
    assert app.title == "nx console"


def test_root_redirects_to_activity():
    app = create_app()
    client = TestClient(app)
    resp = client.get("/", follow_redirects=False)
    assert resp.status_code == 307
    assert "/activity" in resp.headers["location"]


def test_activity_page_renders():
    app = create_app()
    client = TestClient(app)
    resp = client.get("/activity")
    assert resp.status_code == 200
    assert "text/html" in resp.headers["content-type"]


def test_health_page_renders():
    app = create_app()
    client = TestClient(app)
    resp = client.get("/health")
    assert resp.status_code == 200
    assert "text/html" in resp.headers["content-type"]


def test_campaigns_page_renders():
    app = create_app()
    client = TestClient(app)
    resp = client.get("/campaigns")
    assert resp.status_code == 200
    assert "text/html" in resp.headers["content-type"]


def test_static_htmx_served():
    app = create_app()
    client = TestClient(app)
    resp = client.get("/static/htmx.min.js")
    assert resp.status_code == 200


def test_static_pico_served():
    app = create_app()
    client = TestClient(app)
    resp = client.get("/static/pico.min.css")
    assert resp.status_code == 200


def test_static_alpine_served():
    app = create_app()
    client = TestClient(app)
    resp = client.get("/static/alpine.min.js")
    assert resp.status_code == 200


def test_static_console_css_served():
    app = create_app()
    client = TestClient(app)
    resp = client.get("/static/console.css")
    assert resp.status_code == 200


def test_scope_param_defaults_to_project():
    app = create_app()
    client = TestClient(app)
    resp = client.get("/activity")
    assert resp.status_code == 200


def test_scope_param_all():
    app = create_app()
    client = TestClient(app)
    resp = client.get("/activity?scope=all")
    assert resp.status_code == 200


def test_console_config_defaults():
    cfg = ConsoleConfig()
    assert cfg.port == 8765
    assert cfg.host == "127.0.0.1"
