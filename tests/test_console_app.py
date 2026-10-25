# SPDX-License-Identifier: AGPL-3.0-or-later
from datetime import datetime, timezone

import pytest
from fastapi.testclient import TestClient

from nexus.commands._helpers import t2_handle
from nexus.console.app import create_app
from nexus.console.config import ConsoleConfig
from nexus.console.threads import BeadsResult


def test_app_factory_creates_app():
    app = create_app()
    assert app is not None
    assert app.title == "nx console"


def test_root_redirects_to_threads():
    app = create_app()
    client = TestClient(app)
    resp = client.get("/", follow_redirects=False)
    assert resp.status_code == 307
    assert resp.headers["location"] == "/threads"


@pytest.mark.parametrize("path", ["/activity", "/campaigns", "/campaigns/anything"])
def test_retired_panels_are_gone(path):
    """nexus-i99vk: Activity and Campaigns read the retired catalog JSONL files."""
    client = TestClient(create_app())
    assert client.get(path).status_code == 404


@pytest.mark.t2_tenant
def test_health_and_threads_pages_render_against_real_t2():
    """One tenant serves both pages: the CI provisioned-tenant ceiling counts each tenant test."""
    client = TestClient(create_app(bd_runner=lambda: BeadsResult(())))
    resp = client.get("/health")
    assert resp.status_code == 200
    assert "text/html" in resp.headers["content-type"]

    # Threads reads T2 project "threads" through the CLI's own handle (nexus-i99vk).
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%MZ")
    with t2_handle() as db:
        db.memory.put(project="threads", title="nexus_c9c", content=(
            f"updated: {now}\n## Console panel\nref: nexus-i99vk\nstate: building\n"
            "waiting_on: review\nneeds_sam: choose the port\n"))
        db.memory.put(project="threads", title="other", content="## Quiet thread\nstate: idle\nneeds_sam: none\n")
        db.memory.put(project="not_threads", title="intruder", content="## Nope\nstate: x\n")
    html = client.get("/threads").text
    for expected in ("nexus_c9c", "other", "Console panel", "building", "review", "Quiet thread"):
        assert expected in html, expected
    assert "intruder" not in html  # another project must not leak onto the board
    assert "1 thing needs you." in html
    needs_you = html.split("<h3>Needs you</h3>", 1)[1].split("<h3>Sessions</h3>", 1)[0]
    assert "choose the port" in needs_you and "asked by nexus_c9c" in needs_you
    assert "Quiet thread" not in needs_you
    assert "thread-card stale" not in html


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


def test_no_page_loads_alpine():
    client = TestClient(create_app(bd_runner=lambda: BeadsResult(()), threads_reader=lambda: []))
    assert "alpine" not in client.get("/threads").text.lower()  # base.html is shared with Health
    assert client.get("/static/alpine.min.js").status_code == 404


def test_static_console_css_served():
    app = create_app()
    client = TestClient(app)
    resp = client.get("/static/console.css")
    assert resp.status_code == 200


def test_console_config_defaults():
    cfg = ConsoleConfig()
    assert cfg.port == 8765
    assert cfg.host == "127.0.0.1"
