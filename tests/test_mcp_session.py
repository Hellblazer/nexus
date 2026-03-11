# SPDX-License-Identifier: AGPL-3.0-or-later
"""T1 PPID chain session sharing tests for MCP server context.

Validates that T1Database instances sharing a session see each other's entries,
and that find_ancestor_session resolves the correct server address.
"""
from __future__ import annotations

import json
import os
import tempfile
import time
from pathlib import Path

import chromadb
import pytest

from nexus.db.t1 import T1Database
from nexus.session import find_ancestor_session


def test_t1_shared_session_via_injected_client():
    """Two T1Database instances sharing a client and session_id see each other's entries."""
    client = chromadb.EphemeralClient()
    sid = "shared-session-42"

    t1a = T1Database(session_id=sid, client=client)
    t1b = T1Database(session_id=sid, client=client)

    doc_id = t1a.put("shared entry from A")
    entry = t1b.get(doc_id)
    assert entry is not None
    assert entry["content"] == "shared entry from A"

    # Search from B finds A's entry
    results = t1b.search("shared entry", n_results=5)
    assert any("shared entry from A" in r["content"] for r in results)


def test_t1_session_isolation():
    """Two T1Database instances with different session_ids don't see each other."""
    client = chromadb.EphemeralClient()

    t1a = T1Database(session_id="session-alpha", client=client)
    t1b = T1Database(session_id="session-beta", client=client)

    t1a.put("alpha only")
    t1b.put("beta only")

    alpha_list = t1a.list_entries()
    beta_list = t1b.list_entries()

    assert all(e["content"] != "beta only" for e in alpha_list)
    assert all(e["content"] != "alpha only" for e in beta_list)


def test_find_ancestor_session_resolves_record():
    """find_ancestor_session resolves a session record keyed to the current PID."""
    with tempfile.TemporaryDirectory() as tmpdir:
        sessions_dir = Path(tmpdir)
        pid = os.getpid()

        record = {
            "session_id": "test-session-id",
            "server_host": "127.0.0.1",
            "server_port": 9999,
            "server_pid": 12345,
            "created_at": time.time(),
        }
        session_file = sessions_dir / f"{pid}.session"
        session_file.write_text(json.dumps(record))

        result = find_ancestor_session(sessions_dir=sessions_dir, start_pid=pid)
        assert result is not None
        assert result["session_id"] == "test-session-id"
        assert result["server_host"] == "127.0.0.1"
        assert result["server_port"] == 9999


def test_find_ancestor_session_no_record():
    """find_ancestor_session returns None when no session files exist."""
    with tempfile.TemporaryDirectory() as tmpdir:
        result = find_ancestor_session(sessions_dir=Path(tmpdir), start_pid=os.getpid())
        assert result is None


def test_find_ancestor_session_skips_stale():
    """find_ancestor_session skips records older than 24 hours."""
    with tempfile.TemporaryDirectory() as tmpdir:
        sessions_dir = Path(tmpdir)
        pid = os.getpid()

        record = {
            "session_id": "stale-session",
            "server_host": "127.0.0.1",
            "server_port": 8888,
            "server_pid": 99999,  # Non-existent PID — stop_t1_server will no-op
            "created_at": time.time() - 25 * 3600,  # 25 hours ago
        }
        session_file = sessions_dir / f"{pid}.session"
        session_file.write_text(json.dumps(record))

        result = find_ancestor_session(sessions_dir=sessions_dir, start_pid=pid)
        assert result is None
