# SPDX-License-Identifier: AGPL-3.0-or-later
import json
from pathlib import Path
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

from nexus.console.app import create_app
from nexus.console.routes.campaigns import _load_campaign_summary, SYSTEM_CREATORS


@pytest.fixture()
def client():
    return TestClient(create_app())


def test_campaigns_landing_200(client):
    resp = client.get("/campaigns?scope=all")
    assert resp.status_code == 200
    assert "text/html" in resp.headers["content-type"]


def test_campaigns_page_has_sections(client):
    resp = client.get("/campaigns")
    assert "Campaigns" in resp.text


def test_campaign_detail_unknown(client):
    resp = client.get("/campaigns/nonexistent-campaign-xyz")
    assert resp.status_code == 200  # renders empty detail


def test_system_creators_list():
    assert "index_hook" in SYSTEM_CREATORS
    assert "filepath_extractor" in SYSTEM_CREATORS
    assert "auto-linker" in SYSTEM_CREATORS


# ── Campaign summary loading ─────────────────────────────────────────────────

def test_load_campaign_summary_empty(tmp_path):
    path = tmp_path / "links.jsonl"
    result = _load_campaign_summary(path)
    assert result == {}


def test_load_campaign_summary_groups_by_creator(tmp_path):
    path = tmp_path / "links.jsonl"
    lines = [
        json.dumps({"created_by": "index_hook", "link_type": "implements-heuristic",
                     "created_at": "2026-04-05T18:00:00+00:00", "_deleted": False}),
        json.dumps({"created_by": "index_hook", "link_type": "implements-heuristic",
                     "created_at": "2026-04-05T19:00:00+00:00", "_deleted": False}),
        json.dumps({"created_by": "research-agent", "link_type": "cites",
                     "created_at": "2026-04-06T10:00:00+00:00", "_deleted": False}),
    ]
    path.write_text("\n".join(lines) + "\n")
    result = _load_campaign_summary(path)
    assert "index_hook" in result
    assert result["index_hook"]["count"] == 2
    assert "research-agent" in result
    assert result["research-agent"]["count"] == 1


def test_load_campaign_summary_skips_deleted(tmp_path):
    path = tmp_path / "links.jsonl"
    lines = [
        json.dumps({"created_by": "agent", "link_type": "cites",
                     "created_at": "2026-04-05T18:00:00+00:00", "_deleted": True}),
    ]
    path.write_text("\n".join(lines) + "\n")
    result = _load_campaign_summary(path)
    assert result == {}
