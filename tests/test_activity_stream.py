# SPDX-License-Identifier: AGPL-3.0-or-later
import json
import time
from pathlib import Path
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

from nexus.console.app import create_app
from nexus.console.watchers import JSONLTailWatcher


@pytest.fixture()
def client():
    return TestClient(create_app())


def test_root_redirects_to_activity(client):
    resp = client.get("/", follow_redirects=False)
    assert resp.status_code == 307
    assert "/activity" in resp.headers["location"]


def test_activity_page_renders(client):
    resp = client.get("/activity")
    assert resp.status_code == 200
    assert "Activity" in resp.text


def test_activity_events_endpoint(client):
    resp = client.get("/activity/events?scope=project")
    assert resp.status_code == 200


def test_activity_event_detail_missing(client):
    resp = client.get("/activity/event/nonexistent")
    assert resp.status_code == 200  # renders empty detail


# ── JSONLTailWatcher unit tests ──────────────────────────────────────────────

def test_watcher_reads_new_lines(tmp_path):
    path = tmp_path / "test.jsonl"
    events = []
    watcher = JSONLTailWatcher(path, callback=events.append)

    # File doesn't exist yet
    watcher._check_for_new_lines()
    assert events == []

    # Write initial data — watcher starts from end on first poll
    path.write_text('{"a": 1}\n{"a": 2}\n')
    watcher._offset = 0  # Force read from start for test
    watcher._last_mtime = 0.0
    watcher._check_for_new_lines()
    assert len(events) == 2
    assert events[0] == {"a": 1}


def test_watcher_handles_append(tmp_path):
    path = tmp_path / "test.jsonl"
    path.write_text('{"a": 1}\n')
    events = []
    watcher = JSONLTailWatcher(path, callback=events.append)
    watcher._offset = 0
    watcher._last_mtime = 0.0

    watcher._check_for_new_lines()
    assert len(events) == 1

    # Append new data
    time.sleep(0.01)  # ensure mtime changes
    with open(path, "a") as f:
        f.write('{"a": 2}\n')

    watcher._check_for_new_lines()
    assert len(events) == 2
    assert events[1] == {"a": 2}


def test_watcher_handles_truncation(tmp_path):
    path = tmp_path / "test.jsonl"
    path.write_text('{"a": 1}\n{"a": 2}\n{"a": 3}\n')
    events = []
    watcher = JSONLTailWatcher(path, callback=events.append)
    watcher._offset = path.stat().st_size  # Start at end
    watcher._last_mtime = path.stat().st_mtime

    # Truncate (simulates compact())
    time.sleep(0.01)
    path.write_text('{"b": 1}\n')

    watcher._check_for_new_lines()
    assert len(events) == 1
    assert events[0] == {"b": 1}


def test_watcher_skips_invalid_json(tmp_path):
    path = tmp_path / "test.jsonl"
    path.write_text('{"a": 1}\nnot-json\n{"a": 2}\n')
    events = []
    watcher = JSONLTailWatcher(path, callback=events.append)
    watcher._offset = 0
    watcher._last_mtime = 0.0

    watcher._check_for_new_lines()
    assert len(events) == 2  # skips the invalid line
